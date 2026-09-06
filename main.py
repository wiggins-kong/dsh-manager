"""DSH-manager — pywebview 入口 + JS API 桥。

运行:  python main.py
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
import webbrowser
from pathlib import Path

import webview

from dsh_core import DSHManager, REPO_URL

BASE_DIR = Path(__file__).parent
WEB_DIR = BASE_DIR / "web"


APP_VERSION = "1.1.0"   # 应用版本号(发版时与 git tag 同步更新)

# ---- 单实例锁 (Windows Named Mutex) ----
_MUTEX_NAME = "Global\\DSHManagerSingleInstance_v1"


def _acquire_single_instance() -> bool:
    """尝试获取单实例互斥锁。返回 True 表示是第一个实例, False 表示已有实例在运行。"""
    if os.name != "nt":
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        mutex = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(mutex)
            # 尝试激活已有实例窗口
            hwnd = user32.FindWindowW(None, "DSH-manager")
            if hwnd:
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
            return False
        # 注意: 不要 CloseHandle(mutex), 需保持到进程退出
        return True
    except Exception:  # noqa: BLE001
        return True


def _initial_titlebar_theme():
    """窗口显示时的初始染色: 应用界面跟随系统深浅色, 标题栏与之对齐。"""
    _apply_titlebar_theme("dark" if _system_dark() else "light")


class Api:
    """暴露给前端 JS 的方法 (window.pywebview.api.*)。"""

    def __init__(self):
        self.m = DSHManager()

    def sync_theme(self, mode: str) -> None:
        """前端主题变化 (跟随系统) 时同步原生标题栏颜色。"""
        if mode in ("dark", "light"):
            _apply_titlebar_theme(mode)

    # 辅助: 把耗时操作丢到工作线程, 用 Event 等结果, 同时不阻塞 UI
    def _run_sync(self, fn):
        out, done = {}, threading.Event()
        def work():
            try:
                out["ok"] = fn()
            except Exception as e:  # noqa: BLE001  (跨线程传错误)
                out["err"] = f"{type(e).__name__}: {e}"
            finally:
                done.set()
        threading.Thread(target=work, daemon=True).start()
        done.wait()
        if "err" in out:
            raise RuntimeError(out["err"])
        return out["ok"]

    # 日志推送: pywebview 的 js_api 参数无法传 JS 函数(JSON 序列化后变 null),
    # 所以不能用 on_log 回调。改为 Python 主动 evaluate_js 推送日志行,
    # evaluate_js 线程安全, 可从任意工作线程调用, 实时到达前端。
    def _emit_log(self, line: str) -> None:
        try:
            win = webview.windows[0] if webview.windows else None
            if win is None:
                return
            win.evaluate_js(f"window.__dsh_log({json.dumps(str(line))});")
        except Exception:  # noqa: BLE001  页面未就绪等场景下静默丢弃
            pass

    def _emit_ws_progress(self, done: int, total: int, name: str) -> None:
        """工作区迁移的逐版本进度推送, 机制同 _emit_log。"""
        self._emit_js("window.__dsh_ws_progress", [int(done), int(total), str(name)])

    def _emit_ws_cleanup(self, done: int, total: int, name: str) -> None:
        """工作区迁移失败后清理半成品的进度推送。"""
        self._emit_js("window.__dsh_ws_cleanup", [int(done), int(total), str(name)])

    def _emit_js(self, fn: str, args: list) -> None:
        """按 fn(args...) 形式向前端 evaluate_js 推送, 线程安全, 失败静默丢弃。"""
        try:
            win = webview.windows[0] if webview.windows else None
            if win is None:
                return
            payload = ", ".join(json.dumps(a) for a in args)
            win.evaluate_js(f"{fn}({payload});")
        except Exception:  # noqa: BLE001  页面未就绪等场景下静默丢弃
            pass

    # ---------- state ----------
    def get_state(self) -> dict:
        node = self.m.node_available()
        log_dir = str(self.m.data_dir / "logs")
        return {
            "versions": list(self.m.config.get("cached_versions", [])),
            "local": self.m.local_repos(),
            "proxy": self.m.config["proxy"],
            "theme": self.m.config.get("theme", "system"),
            "node": node,
            "running": self.m.running,
            "running_tag": self.m.config.get("last_tag"),
            "running_port": self.m._srv_port if self.m.running else None,
            "workspace": str(self.m.repos_dir),
            "log_dir": log_dir,
            "version": APP_VERSION,
        }

    # ---------- versions ----------
    def fetch_versions(self, force: bool = False) -> list[str]:
        return self._run_sync(lambda: self.m.fetch_versions(force=bool(force)))

    # ---------- clone ----------
    def clone(self, tag: str) -> dict:
        return self._run_sync(lambda: {"path": self.m.clone_tag(tag, on_log=self._emit_log)})

    def delete(self, tag: str) -> bool:
        """删除某版本已下载的源码(运行中的版本会被拒绝)。"""
        return self._run_sync(lambda: self.m.delete_tag(str(tag)))

    # ---------- run / stop ----------
    def start(self, tag: str) -> dict:
        repo = self.m.repo_dir_for(tag) if tag else None
        if not repo or not (repo / "package.json").exists():
            raise FileNotFoundError(f"未下载版本 {tag} 的源码，请先下载")
        return self._run_sync(lambda: self.m.start_dsh(repo, on_log=self._emit_log))

    def stop(self) -> bool:
        return self._run_sync(lambda: self.m.stop_dsh())

    def open_web(self):
        self._run_sync(lambda: webbrowser.open(self.m.web_url()))
        return True

    def open_log_dir(self):
        """用系统文件管理器打开日志目录。"""
        log_dir = str(self.m.data_dir / "logs")
        os.makedirs(log_dir, exist_ok=True)
        if os.name == "nt":
            os.startfile(log_dir)
        else:
            self._run_sync(lambda: webbrowser.open("file://" + log_dir))
        return log_dir

    # ---------- proxy / theme ----------
    def save_proxy(self, enabled, host, port):
        self._run_sync(lambda: self.m.save_proxy(bool(enabled), str(host), int(port)))

    def set_theme(self, theme: str) -> str:
        return self._run_sync(lambda: self.m.set_theme(theme))

    # ---------- workspace ----------
    def preview_workspace(self, path: str) -> dict:
        return self._run_sync(lambda: self.m.preview_workspace(path))

    def plan_workspace(self, path: str) -> dict:
        """迁移预检: 报告同名冲突/可移动数量, 不动任何文件。"""
        return self._run_sync(lambda: self.m.plan_workspace_move(path))

    def apply_workspace(self, path: str, on_old: str,
                        resolutions: dict | None = None) -> dict:
        return self._run_sync(
            lambda: self.m.apply_workspace(
                path, on_old, on_progress=self._emit_ws_progress,
                on_cleanup=self._emit_ws_cleanup, resolutions=resolutions))

    def pick_folder(self, initial_dir: str = "") -> str | None:
        """打开 Windows 原生目录选择对话框。经 js_api 直接调用(在 GUI 线程上执行)。"""
        win = webview.windows[0] if webview.windows else None
        if win is None:
            return None
        res = win.create_file_dialog(webview.FOLDER_DIALOG, directory=str(initial_dir or ""))
        if isinstance(res, (list, tuple)):
            return res[0] if res else None
        return res

    # ---------- node download ----------
    def open_node_download(self):
        self._run_sync(lambda: webbrowser.open(
            "https://nodejs.org/zh-cn/download"))
        return True


def _icon_path() -> str:
    """窗口图标路径: 打包后用 _MEIPASS 资源目录, 开发/源码用项目目录。"""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", ""))
    else:
        base = BASE_DIR
    return str(base / "assets" / "app.ico")


def _system_dark() -> bool:
    """Windows 系统应用是否为深色模式(注册表 AppsUseLightTheme, 0=深色)。非 Windows 视为深色。"""
    if os.name != "nt":
        return True
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def _colorref(hex_color: str) -> int:
    """#RRGGBB → Win32 COLORREF (0x00BBGGRR)。"""
    h = hex_color.lstrip("#")
    return int(h[0:2], 16) | (int(h[2:4], 16) << 8) | (int(h[4:6], 16) << 16)


def _redraw_nonclient(hwnd: int) -> None:
    """切换一次激活状态触发非客户区重绘, 否则标题栏可能停留在旧颜色。"""
    WM_NCACTIVATE = 0x0086
    ctypes.windll.user32.SendMessageW(hwnd, WM_NCACTIVATE, 0, 0)
    ctypes.windll.user32.SendMessageW(hwnd, WM_NCACTIVATE, 1, 0)


# 原生标题栏配色 (底色, 文字色) — 与 web/style.css 各主题令牌一致
_TITLEBAR_COLORS = {
    "dark": ("#0f172a", "#e2e8f0"),
    "light": ("#eff3f8", "#0f172a"),
}


def _apply_titlebar_theme(theme: str = "dark"):
    """在 GUI 线程把原生标题栏染成与应用当前主题一致的颜色, 消除割裂。

    pywebview 未暴露该选项, 走 DWM:
    - Win11 (22000+): DWMWA_CAPTION_COLOR=35 / TEXT_COLOR=36 / BORDER_COLOR=34 直接指定颜色;
      颜色跟随应用主题而非系统深浅色。
    - 旧 Win10 不支持 35/36, 退回沉浸式深色模式 DWMWA_USE_IMMERSIVE_DARK_MODE=20/19。
    """
    if os.name != "nt":
        return
    try:
        win = webview.windows[0] if webview.windows else None
        native = getattr(win, "native", None)
        if native is None:
            return
        hwnd = int(native.Handle.ToInt64())  # .NET IntPtr → python int
        dwmapi = ctypes.windll.dwmapi

        def set_color(attr: int, color: int) -> bool:
            return dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(ctypes.c_int(color)), 4) == 0

        caption_hex, text_hex = _TITLEBAR_COLORS.get(theme, _TITLEBAR_COLORS["dark"])
        caption = _colorref(caption_hex)
        ok = set_color(35, caption)
        if ok:
            set_color(36, _colorref(text_hex))  # 标题文字
            set_color(34, caption)              # 窗口边框线与标题栏同色
            _redraw_nonclient(hwnd)
            return
        if _system_dark():
            for attr in (20, 19):  # 20 为正式值, 19 为早期 Win10 预览值
                if set_color(attr, 1):
                    break
            _redraw_nonclient(hwnd)
    except Exception:  # noqa: BLE001
        pass


def _apply_window_icon():
    """在 GUI 线程给 Windows 窗体设置标题栏图标 (native winforms Form.Icon)。

    pywebview 的 create_window / start 都未暴露 Windows 下的 window icon
    (icon 参数仅支持 GTK/QT), 因此走底层 .NET 设置。
    """
    try:
        import clr
        from System.Drawing import Icon
        win = webview.windows[0] if webview.windows else None
        native = getattr(win, "native", None)
        if native is not None and os.path.exists(_icon_path()):
            native.Icon = Icon(_icon_path())
    except Exception:  # noqa: BLE001
        pass


def main():
    if not _acquire_single_instance():
        print("[DSH-manager] 已有实例运行中, 已激活已有窗口。")
        return
    api = Api()
    window = webview.create_window(
        "DSH-manager",
        str(WEB_DIR / "index.html"),
        js_api=api,
        width=1080,
        height=720,
        min_size=(900, 600),
        background_color="#0f172a",
        frameless=False,
        easy_drag=False,
    )
    # 窗口显示后设置标题栏图标 + 按当前主题染色标题栏(pywebview 的 icon 参数不支持 Windows, 走 native)
    window.events.shown += _apply_window_icon
    window.events.shown += _initial_titlebar_theme
    webview.start(debug=(os.environ.get("DSH_DEBUG") == "1"))
    # 窗口显示后清理可能残留的 DSH 后端, 避免留下孤儿进程占端口
    try:
        api.m.stop_dsh()
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    main()
