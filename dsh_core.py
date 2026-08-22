"""DSH-manager 核心逻辑。

负责: 配置持久化、代理设置、GitHub 版本查询、git 浅克隆、Node/PNPM 检测、
DSH 后台进程的启动与停止。界面无关, 便于单元测试。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
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


def _hidden_popen_kwargs(extra_flags: int = 0) -> dict:
    """Windows 下用 CREATE_NO_WINDOW 隐藏子进程命令行窗口(不弹黑窗)。

    注意: 不加这个 flag 时, cmd /c pnpm ... / git 会弹出新的控制台窗口, 很难看。
    """
    if os.name != "nt":
        return {}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | extra_flags
    return {"creationflags": flags}


def _rmtree_force(path, on_log=None) -> None:
    """强制递归删除目录, 专为 Windows 编写。

    git 会把 `.git/objects` 里的 pack 文件设为**只读**(444), 直接 shutil.rmtree
    会抛 PermissionError 且 `ignore_errors=True` 会静默吞掉, 导致目录残留半截、
    用户毫无感知。这里用 onerror 回调先清只读属性再重试; 仍失败(如被其他进程
    占用)则明确报错, 绝不静默留下残留。
    """
    def _onerror(func, p, exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
            return
        except Exception as e:  # noqa: BLE001
            raise OSError(f"无法删除 {p} ({e}), 可能被其他程序占用") from e

    try:
        shutil.rmtree(path, onerror=_onerror)
    except OSError as e:
        if on_log:
            on_log(f"[删除失败] {path.name}: {e}")
        raise


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
    def repo_dir_for(self, tag: str) -> Path:
        """返回某 tag 源码应存放的目录路径(与 clone_tag 的命名一致, 去掉重复前缀)。"""
        return self.repos_dir / f"dsh-{_dir_clean(tag)}"

    def _downloaded(self, target: Path) -> bool:
        """只有 package.json + .git 都存在的目录才算真正的完整克隆。

        曾发生: git clone 中断只留下 .git 而没有源码文件, 若只看 .git 会误判为
        "已下载" 而不再重新克隆 → 界面显示已下载但实际没有源码。故必须两者兼有。
        """
        return (target / "package.json").exists() and (target / ".git").exists()

    def clone_tag(self, tag: str, on_log=None) -> str:
        target = self.repo_dir_for(tag)
        # 只有完整克隆才复用; 残缺(缺源码/只有.git)一律视为未下载
        if self._downloaded(target):
            if on_log:
                on_log(f"[提示] {tag} 已在本机, 无需重新下载")
            return str(target)
        # 清理历史下载失败留下的半成品目录, 保证重新克隆从干净状态开始
        if target.exists():
            if on_log:
                on_log(f"[清理] {tag} 克隆不完整, 移除残留目录: {target.name}")
            _rmtree_force(target, on_log)
        target.mkdir(parents=True, exist_ok=True)
        if on_log:
            on_log(f"[下载] git clone --depth 1 --branch {tag} …")
        try:
            cmd = ["git", "clone", "--depth", "1", "--branch", tag,
                   *self.git_proxy_args(), REPO_URL, str(target)]
            self._run_stream(cmd, on_log, cwd=self.data_dir)
        except Exception:
            # 克隆失败时清掉半成品, 避免残留目录污染工作区/下次误判
            if target.exists():
                try:
                    _rmtree_force(target, on_log)
                except Exception:  # noqa: BLE001  清理失败不掩盖最初错误
                    pass
            raise
        (target / ".dsh-tag").write_text(tag, encoding="utf-8")
        if on_log:
            on_log(f"[完成] {tag} 源码已下载到 {target}")
        return str(target)

    def _run_stream(self, cmd, on_log, cwd=None, check=True):
        cmd = _spawn_cmd(list(cmd))
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", cwd=cwd,
            **_hidden_popen_kwargs(),
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
        # 智能跳过已完成的步骤, 避免每次点"运行"都全量 install/build(很慢):
        #  - node_modules/.pnpm 存在 => 依赖已装, 跳过 pnpm install
        #  - apps/web/dist 存在     => 前端已构建, 跳过 pnpm run build
        installed = (repo_dir / "node_modules" / ".pnpm").exists()
        web_dist = repo_dir / "apps" / "web" / "dist"
        built = web_dist.exists()

        if on_log:
            on_log("[1/3] 依赖检查: " + ("跳过(pnpm 依赖已装)"
                                          if installed else "pnpm install …"))
        if not installed:
            self._run_stream(["pnpm", "install"], on_log, cwd=repo_dir)

        if on_log:
            on_log("[2/3] 前端构建: " + ("跳过(apps/web/dist 已存在)"
                                          if built else "pnpm run build …"))
        if not built:
            self._run_stream(["pnpm", "run", "build"], on_log, cwd=repo_dir)

        # 后台启动 dsh web; 加 --no-open 禁止 DSH 自动打开浏览器(管理器自带"打开前端"按钮)
        self.stop_dsh()
        # 清理可能残留占用该端口的孤儿 dsh node(直接关窗等场景遗留), 保证新后端能绑定端口
        self._kill_port_owner(port)
        if on_log:
            on_log(f"[3/3] 启动后端: pnpm dsh web --port {port} …")
        proc = subprocess.Popen(
            _spawn_cmd(["pnpm", "dsh", "web", "--port", str(port), "--no-open"]),
            cwd=repo_dir,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            **_hidden_popen_kwargs(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)),
        )
        self._proc = proc
        self._proc_pid = proc.pid

        def _pump():
            for line in proc.stdout:
                line = line.rstrip("\n")
                if on_log:
                    on_log(line)

        threading.Thread(target=_pump, daemon=True).start()

        # ① 快速存活检查: 启动后立刻崩溃(如脚本不存在/命令错误)则明确报错
        time.sleep(1.5)
        if proc.poll() is not None:
            self._proc = None
            self._proc_pid = None
            raise RuntimeError(
                f"后端进程启动后立即退出(exit {proc.returncode}), "
                "请查看上方日志中的具体错误")

        # ② 端口就绪探测: 轮询 HTTP 直到可访问, 区分 就绪/进程死亡/超时
        status = self._wait_ready(self.web_url())
        if status == "dead":
            self._proc = None
            self._proc_pid = None
            raise RuntimeError("后端进程在启动过程中退出, 请查看上方日志中的具体错误")
        if on_log:
            if status == "ready":
                on_log(f"[就绪] 后端已启动: {self.web_url()}")
            else:
                on_log(f"[提示] 进程仍在运行但端口暂未就绪(>60s), "
                       "可稍后点击「打开前端」重试")

        self.config["last_tag"] = self._tag_from_repo(repo_dir)
        self.save_config()
        return {"pid": proc.pid, "port": port, "status": "running"}

    def _wait_ready(self, url: str, timeout: float = 60.0,
                    interval: float = 1.0) -> str:
        """轮询 HTTP 端口直到可访问。返回 'ready' | 'dead' | 'timeout'。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                return "dead"
            try:
                requests.get(url, timeout=2)
                return "ready"
            except requests.RequestException:
                time.sleep(interval)
        return "timeout"

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

    def _kill_port_owner(self, port: int) -> None:
        """启动前强制清理占用指定端口(3080)的残留进程。

        场景: 用户直接关闭管理器窗口或上次进程未清理时, 残留的 dsh node 仍占着
        3080。此时新启动的 dsh web 会 bind 失败(端口被占), 而 `_wait_ready` 却会
        因"端口已有进程监听"而误判为成功 → 前端打开的其实是那个异常残留进程,
        表现为"后端起来了但前端打不开/404"。故启动前先清掉端口的监听者。
        """
        if os.name != "nt":
            return
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-NetTCPConnection -LocalPort %d -State Listen "
                 "-ErrorAction SilentlyContinue).OwningProcess" % port],
                capture_output=True, text=True, timeout=10, check=False)
            for tok in r.stdout.split():
                if tok.strip().isdigit():
                    subprocess.run(["taskkill", "/F", "/PID", tok.strip()],
                                   capture_output=True, timeout=10, check=False)
        except Exception:  # noqa: BLE001  尽力而为, 失败不阻塞启动
            pass

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

    def delete_tag(self, tag: str) -> bool:
        """删除某版本已下载的源码目录。正在运行的版本禁止删除。"""
        if self.running and self.config.get("last_tag") == tag:
            raise RuntimeError(f"版本 {tag} 正在运行, 请先停止后端再删除")
        target = self.repo_dir_for(tag)
        if not target.exists():
            return False
        _rmtree_force(target)   # 强制删除(.git 只读文件会先清属性再删)
        if target.exists():
            raise RuntimeError(
                f"{tag} 目录未完全删除, 可能被其他程序占用, 请关闭占用它的程序后重试")
        return True

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

