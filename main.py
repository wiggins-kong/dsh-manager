"""DSH 管理器 — pywebview 入口 + JS API 桥。

运行:  python main.py
"""
from __future__ import annotations

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


class Api:
    """暴露给前端 JS 的方法 (window.pywebview.api.*)。"""

    def __init__(self):
        self.m = DSHManager()

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

    # ---------- state ----------
    def get_state(self) -> dict:
        node = self.m.node_available()
        return {
            "versions": list(self.m.config.get("cached_versions", [])),
            "local": self.m.local_repos(),
            "proxy": self.m.config["proxy"],
            "theme": self.m.config.get("theme", "dark"),
            "node": node,
            "running": self.m.running,
            "running_tag": self.m.config.get("last_tag"),
            "workspace": str(self.m.repos_dir),
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

    # ---------- proxy / theme ----------
    def save_proxy(self, enabled, host, port):
        self._run_sync(lambda: self.m.save_proxy(bool(enabled), str(host), int(port)))

    def set_theme(self, theme: str) -> str:
        return self._run_sync(lambda: self.m.set_theme(theme))

    # ---------- workspace ----------
    def preview_workspace(self, path: str) -> dict:
        return self._run_sync(lambda: self.m.preview_workspace(path))

    def apply_workspace(self, path: str, on_old: str) -> dict:
        return self._run_sync(lambda: self.m.apply_workspace(path, on_old))

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
    api = Api()
    window = webview.create_window(
        "DeepSeek Harness 管理器",
        str(WEB_DIR / "index.html"),
        js_api=api,
        width=1080,
        height=720,
        min_size=(900, 600),
        background_color="#0f172a",
        frameless=False,
        easy_drag=False,
    )
    # 窗口显示后设置标题栏图标(pywebview 的 icon 参数不支持 Windows, 走 native.Icon)
    window.events.shown += _apply_window_icon
    webview.start(debug=(os.environ.get("DSH_DEBUG") == "1"))
    # 窗口显示后清理可能残留的 DSH 后端, 避免留下孤儿进程占端口
    try:
        api.m.stop_dsh()
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    main()
