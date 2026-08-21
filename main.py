"""DSH 管理器 — pywebview 入口 + JS API 桥。

运行:  python main.py
"""
from __future__ import annotations

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
    def clone(self, tag: str, on_log=None) -> dict:
        return self._run_sync(lambda: {"path": self.m.clone_tag(tag, on_log=on_log)})

    # ---------- run / stop ----------
    def start(self, tag: str, on_log=None) -> dict:
        if tag and not (self.m.repos_dir / f"dsh-{tag}").exists():
            raise FileNotFoundError(f"未下载版本 {tag} 的源码，请先下载")
        return self._run_sync(lambda: self.m.start_dsh(
            self.m.repos_dir / f"dsh-{tag}", on_log=on_log))

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

    # ---------- node download ----------
    def open_node_download(self):
        self._run_sync(lambda: webbrowser.open(
            "https://nodejs.org/zh-cn/download"))
        return True


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
    webview.start(debug=(os.environ.get("DSH_DEBUG") == "1"))


if __name__ == "__main__":
    main()
