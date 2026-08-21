"""DSH 管理器核心逻辑。

负责: 配置持久化、代理设置、GitHub 版本查询、git 浅克隆、Node/PNPM 检测、
DSH 后台进程的启动与停止。界面无关, 便于单元测试。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests
from packaging.version import InvalidVersion, Version

REPO_URL = "https://github.com/deepseek-ai/deepseek-harness"
VERSION_RE = re.compile(r"^v?\d+\.\d+\.\d+")


def _spawn_cmd(argv: list[str]) -> list[str]:
    """Windows 上 pnpm 是 .cmd 垫片, subprocess 不带 shell 找不到, 需经 cmd 解析。"""
    if os.name == "nt" and argv and argv[0] == "pnpm":
        return ["cmd", "/c"] + argv
    return argv


def _exe_dir() -> Path:
    """打包(exe)时返回 exe 所在目录, 否则返回脚本目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def _default_data_dir() -> Path:
    return _exe_dir() / "data"


def _proxy_url(host: str, port) -> str:
    return f"http://{host}:{port}"


def _strip_prefix(tag: str) -> str:
    """去掉可选的 'dsh-' 与 'v' 前缀, 便于版本号解析。"""
    t = tag
    if t.startswith("dsh-"):
        t = t[4:]
    if t.startswith("v"):
        t = t[1:]
    return t


def _dir_clean(tag: str) -> str:
    """用于目录名的干净名称 (去掉 dsh- 前缀)。"""
    return tag[4:] if tag.startswith("dsh-") else tag


def sort_versions(tags: list[str]) -> list[str]:
    """把 tag 列表按版本号语义降序排序, 剔除不是版本号的 tag。"""
    parsed = []
    for tag in tags:
        raw = _strip_prefix(tag)
        try:
            v = Version(raw)
        except InvalidVersion:
            continue
        if not re.match(r"^\d+\.\d+\.\d+", raw):
            continue
        parsed.append((v, tag))
    # 降序; rc/预发布按 packaging 规则排在对应正式版之后
    parsed.sort(key=lambda x: x[0], reverse=True)
    return [tag for _, tag in parsed]


class DSHManager:
    def __init__(self, data_dir: str | Path | None = None):
        self.data_dir = Path(data_dir) if data_dir else _default_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.data_dir / "config.json"
        self.config = self._default_config()
        self._load_config()
        # 源码目录: 优先用配置的工作区, 否则默认 data_dir/repos
        ws = self.config.get("workspace")
        self.repos_dir = Path(ws) if ws else (self.data_dir / "repos")
        self.repos_dir.mkdir(parents=True, exist_ok=True)
        self._proc = None          # 当前 DSH 子进程
        self._proc_pid: int | None = None
        self._srv_port = 3080

    # ---------- config ----------
    @staticmethod
    def _default_config() -> dict:
        return {
            "proxy": {"enabled": False, "host": "127.0.0.1", "port": 7897},
            "cached_versions": [],
            "last_tag": None,
            "theme": "dark",
            "workspace": None,
        }

    def _load_config(self):
        if self.config_path.exists():
            try:
                data = json.loads(self.config_path.read_text(encoding="utf-8"))
                merged = self._default_config()
                merged.update(data)
                self.config = merged
            except (json.JSONDecodeError, OSError):
                pass

    def save_config(self):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(self.config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ---------- proxy ----------
    def save_proxy(self, enabled: bool, host: str, port) -> None:
        self.config["proxy"] = {
            "enabled": bool(enabled),
            "host": str(host),
            "port": int(port),
        }
        self.save_config()

    def get_proxy(self) -> dict | None:
        p = self.config["proxy"]
        if p["enabled"] and p["host"]:
            url = _proxy_url(p["host"], p["port"])
            return {"http": url, "https": url}
        return None

    def git_proxy_args(self) -> list[str]:
        if self.get_proxy():
            p = self.config["proxy"]
            url = _proxy_url(p["host"], p["port"])
            return ["-c", f"http.proxy={url}", "-c", f"https.proxy={url}"]
        return []

    # ---------- node / pnpm ----------
    def node_available(self) -> dict:
        res = {"node": False, "pnpm": False, "node_version": None, "pnpm_version": None}
        try:
            r = subprocess.run(["node", "--version"], capture_output=True, text=True,
                               timeout=15, check=False)
            if r.returncode == 0:
                res["node"] = True
                res["node_version"] = (r.stdout or r.stderr).strip()
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            pass
        try:
            r = subprocess.run(_spawn_cmd(["pnpm", "--version"]), capture_output=True,
                               text=True, timeout=15, check=False)
            if r.returncode == 0:
                res["pnpm"] = True
                res["pnpm_version"] = (r.stdout or r.stderr).strip()
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            pass
        return res

    # ---------- versions ----------
    def fetch_versions(self, force: bool = False) -> list[str]:
        if not force and self.config.get("cached_versions"):
            return list(self.config["cached_versions"])
        url = f"https://api.github.com/repos/deepseek-ai/deepseek-harness/tags"
        params = {"per_page": 100}
        resp = requests.get(url, params=params, proxies=self.get_proxy(), timeout=30)
        resp.raise_for_status()
        tags = [item["name"] for item in resp.json()]
        versions = sort_versions(tags)
        self.config["cached_versions"] = versions
        self.save_config()
        return versions

    # ---------- clone ----------
    def clone_tag(self, tag: str, on_log=None) -> str:
        target = self.repos_dir / f"dsh-{_dir_clean(tag)}"
        # 已克隆过则直接复用
        if (target / ".git").exists():
            if on_log:
                on_log(f"[提示] {tag} 已在本机, 无需重新下载")
            return str(target)
        if target.exists() and any(target.iterdir()):
            raise FileExistsError(f"目标目录非空, 无法克隆: {target}")
        target.mkdir(parents=True, exist_ok=True)
        cmd = ["git", "clone", "--depth", "1", "--branch", tag,
               *self.git_proxy_args(), REPO_URL, str(target)]
        self._run_stream(cmd, on_log, cwd=self.data_dir)
        (target / ".dsh-tag").write_text(tag, encoding="utf-8")
        return str(target)

    def _run_stream(self, cmd, on_log, cwd=None, check=True):
        cmd = _spawn_cmd(list(cmd))
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", cwd=cwd,
        )
        for line in proc.stdout:
            line = line.rstrip("\n")
            if on_log:
                on_log(line)
        proc.wait()
        if check and proc.returncode != 0:
            raise RuntimeError(f"命令失败(exit {proc.returncode}): {' '.join(cmd)}")
        return proc.returncode

    # ---------- run / stop ----------
    def start_dsh(self, repo_dir: str | Path, port: int = 3080, on_log=None) -> dict:
        repo_dir = Path(repo_dir)
        if not (repo_dir / "package.json").exists():
            raise FileNotFoundError(f"找不到 DSH 源码目录: {repo_dir}")
        self._srv_port = port
        # 先确保依赖与构建产物就绪
        for cmd in (["pnpm", "install"], ["pnpm", "run", "build"]):
            self._run_stream(cmd, on_log, cwd=repo_dir)
        # 后台启动 dsh web
        self.stop_dsh()
        proc = subprocess.Popen(
            _spawn_cmd(["pnpm", "dsh", "web", "--port", str(port)]),
            cwd=repo_dir,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        self._proc = proc
        self._proc_pid = proc.pid

        def _pump():
            for line in proc.stdout:
                line = line.rstrip("\n")
                if on_log:
                    on_log(line)

        threading.Thread(target=_pump, daemon=True).start()
        self.config["last_tag"] = self._tag_from_repo(repo_dir)
        self.save_config()
        return {"pid": proc.pid, "port": port, "status": "running"}

    @staticmethod
    def _tag_from_repo(repo_dir: Path) -> str:
        marker = repo_dir / ".dsh-tag"
        if marker.exists():
            return marker.read_text(encoding="utf-8").strip()
        return repo_dir.name.removeprefix("dsh-")

    def stop_dsh(self) -> bool:
        if not self._proc_pid:
            return True
        pid = self._proc_pid
        if os.name == "nt":
            cmd = ["taskkill", "/T", "/F", "/PID", str(pid)]
        else:
            cmd = ["kill", "-TERM", str(pid)]
        try:
            subprocess.run(cmd, capture_output=True, timeout=20, check=False)
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            pass
        # 兜底: 若进程对象仍存活, 主动 terminate
        try:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
        except Exception:
            pass
        self._proc = None
        self._proc_pid = None
        return True

    def web_url(self) -> str:
        return f"http://127.0.0.1:{self._srv_port}"

    # ---------- app-facing helpers ----------
    def set_theme(self, theme: str) -> str:
        if theme not in ("dark", "light", "system"):
            theme = "dark"
        self.config["theme"] = theme
        self.save_config()
        return theme

    def _resolve_ws(self, path: str | Path) -> Path:
        p = Path(path).expanduser()
        return p if p.is_absolute() else (self.data_dir / p)

    def preview_workspace(self, path: str | Path) -> dict:
        """探测把工作区切到 path 后的情况(是否相同、旧路径源码数量)。用于前端决定是否弹窗。"""
        new = self._resolve_ws(path)
        old = self.repos_dir
        same = os.path.normcase(str(new.resolve())) == os.path.normcase(str(old.resolve()))
        old_count = 0
        if old.exists():
            old_count = sum(1 for x in old.iterdir()
                            if x.is_dir() and (x / "package.json").exists())
        return {
            "same": bool(same),
            "old_count": old_count,
            "old_path": str(old),
            "new_path": str(new),
            "will_prompt": bool((not same) and old_count > 0),
        }

    def apply_workspace(self, path: str | Path, on_old: str = "leave") -> dict:
        """切换工作区。on_old: move=把旧源码移动到新路径 / delete=删掉旧源码 / leave=不动旧源码。"""
        if on_old not in ("move", "delete", "leave"):
            on_old = "leave"
        new = self._resolve_ws(path)
        old = self.repos_dir
        if os.path.normcase(str(new.resolve())) == os.path.normcase(str(old.resolve())):
            return {"changed": False, "moved": 0, "action": "none", "new_path": str(new)}
        new.mkdir(parents=True, exist_ok=True)
        moved = 0
        if on_old == "move" and old.exists():
            for item in old.iterdir():
                if item.is_dir():
                    shutil.move(str(item), str(new / item.name))
                    moved += 1
        elif on_old == "delete" and old.exists():
            shutil.rmtree(old, ignore_errors=True)
        self.config["workspace"] = str(new)
        self.repos_dir = new
        self.save_config()
        return {"changed": True, "moved": moved, "action": on_old, "new_path": str(new)}

    def local_repos(self) -> list[dict]:
        """扫描已克隆的源码目录, 返回 [{tag, path}]。"""
        out = []
        if self.repos_dir.exists():
            for d in sorted(self.repos_dir.iterdir()):
                if d.is_dir() and (d / "package.json").exists():
                    marker = d / ".dsh-tag"
                    tag = marker.read_text(encoding="utf-8").strip() \
                        if marker.exists() else d.name.removeprefix("dsh-")
                    out.append({"tag": tag, "path": str(d)})
        return out

    @property
    def running(self) -> bool:
        # 进程对象存在且未退出
        if self._proc is not None:
            return self._proc.poll() is None
        return self._proc_pid is not None and self._pid_alive()

    def _pid_alive(self) -> bool:
        if self._proc_pid is None:
            return False
        if os.name == "nt":
            try:
                import ctypes
                PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                h = ctypes.windll.kernel32.OpenProcess(
                    PROCESS_QUERY_LIMITED_INFORMATION, False, int(self._proc_pid))
                if not h:
                    return False
                try:
                    code = ctypes.c_ulong()
                    ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
                    return code.value == 259  # STILL_ACTIVE
                finally:
                    ctypes.windll.kernel32.CloseHandle(h)
            except Exception:
                return False
        try:
            os.kill(self._proc_pid, 0)
            return True
        except OSError:
            return False

