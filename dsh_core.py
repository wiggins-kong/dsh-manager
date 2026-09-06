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


def _pnpm_env() -> dict:
    """pnpm 子进程环境变量。

    pnpm 11 在 `pnpm <script>`(如 `pnpm dsh web`)运行前会做依赖状态检查
    (verify-deps-before-run): 发现 node_modules 与 lockfile 不同步时自动执行
    install; 若需重建(移除)modules 目录, 会要求 TTY 交互确认——而管理器子进程
    无 TTY, pnpm 直接 abort(ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY),
    表现为\"后端进程在启动过程中退出\"。置 false 跳过该检查, 依赖是否就绪由
    管理器自身的 install/build 步骤控制。

    另注入 npm_config_enable_thin_lto=false: 官方 Windows 版 Node(v22+) 自身以
    thin LTO 编译, node-gyp 会把 enable_thin_lto=true 带进原生模块工程, 进而
    注入 lld 链接器专用的 -flto=thin / /opt:lldltojobs 参数; MSVC 的 link.exe
    不认识这些参数, 编译直接失败(LNK1117), 表现为 pnpm install 阶段部署失败。
    """
    env = dict(os.environ)
    env["pnpm_config_verify_deps_before_run"] = "false"
    env["npm_config_enable_thin_lto"] = "false"
    return env


def _hidden_popen_kwargs(extra_flags: int = 0) -> dict:
    """Windows 下用 CREATE_NO_WINDOW 隐藏子进程命令行窗口(不弹黑窗)。

    注意: 不加这个 flag 时, cmd /c pnpm ... / git 会弹出新的控制台窗口, 很难看。
    """
    if os.name != "nt":
        return {}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | extra_flags
    return {"creationflags": flags}


def _make_logging(on_log, log_file=None):
    """返回包装 on_log: 同时调用原始回调并写日志文件。

    后台进程的生命周期比 start_dsh 长(日志文件在启动流程结束时已关闭),
    后续输出若直接 write 会抛 ValueError 弄死 pump 线程, 故容忍已关闭的文件。
    """
    def wrapper(line: str) -> None:
        if on_log:
            on_log(line)
        if log_file is not None:
            try:
                log_file.write(line + "\n")
                log_file.flush()
            except ValueError:
                pass
    return wrapper


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
        self._auth_url: str | None = None  # dsh web 打印的带 token 认证 URL

    @staticmethod
    def _parse_auth_url(line: str) -> str | None:
        """从 dsh web 的输出行提取带 token 的认证 URL。

        新版 DSH web 有浏览器会话认证: 每次启动生成进程级 launch token,
        启动完成时打印 `dsh web: http://127.0.0.1:PORT/?token=...`;
        访问该 URL 会种下会话 cookie 后 303 跳回干净的 `/`。裸地址一律 401
        ("dsh web authentication required")。输出可能带 ANSI 颜色码, 先剥掉。
        """
        clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line)
        m = re.search(r"dsh web: (https?://[^\s]+)", clean)
        return m.group(1) if m else None

    # ---------- config ----------
    @staticmethod
    def _default_config() -> dict:
        return {
            "proxy": {"enabled": False, "host": "127.0.0.1", "port": 7897},
            "cached_versions": [],
            "last_tag": None,
            "theme": "system",
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
            env=_pnpm_env(), **_hidden_popen_kwargs(),
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
        from datetime import datetime

        repo_dir = Path(repo_dir)
        if not (repo_dir / "package.json").exists():
            raise FileNotFoundError(f"找不到 DSH 源码目录: {repo_dir}")

        # ---------- 日志落盘 ----------
        log_dir = self.data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        tag_str = self._tag_from_repo(repo_dir)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = log_dir / f"dsh-{_dir_clean(tag_str)}-{ts}.log"
        log_file = open(log_path, "a", encoding="utf-8")
        # 包装 on_log: UI 回调 + 日志文件 同时输出
        ui_log = on_log  # 保留原始引用
        _log = _make_logging(ui_log, log_file)
        try:
            return self._start_dsh_inner(repo_dir, port, _log, log_file, log_path, tag_str)
        finally:
            log_file.close()

    def _start_dsh_inner(self, repo_dir, port, on_log, log_file, log_path, tag_str) -> dict:
        """start_dsh 的内部实现(提取出来方便 finally 关闭日志文件)。"""
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
        self._auth_url = None
        # 智能端口探测: 优先用 preferred 端口; 若被残留进程占用则强杀, 被外部占用则自动递增换端口
        port = self._resolve_port(port, on_log)
        self._srv_port = port
        if on_log:
            on_log(f"[3/3] 启动后端: pnpm dsh web --port {port} …")
        proc = subprocess.Popen(
            _spawn_cmd(["pnpm", "dsh", "web", "--port", str(port), "--no-open"]),
            cwd=repo_dir,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env=_pnpm_env(),
            **_hidden_popen_kwargs(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)),
        )
        self._proc = proc
        self._proc_pid = proc.pid

        def _pump():
            for line in proc.stdout:
                line = line.rstrip("\n")
                auth = self._parse_auth_url(line)
                if auth:
                    self._auth_url = auth
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
                # dsh web 就绪后会紧接着打印带 token 的认证 URL; 稍等它出现,
                # 避免就绪日志和「打开前端」拿到的是 401 的裸地址
                deadline = time.time() + 5.0
                while (self._auth_url is None and time.time() < deadline
                       and self._proc is not None and self._proc.poll() is None):
                    time.sleep(0.2)
                on_log(f"[就绪] 后端已启动: {self.web_url()}")
            else:
                on_log(f"[提示] 进程仍在运行但端口暂未就绪(>60s), "
                       "可稍后点击「打开前端」重试")

        self.config["last_tag"] = self._tag_from_repo(repo_dir)
        self.save_config()
        return {"pid": proc.pid, "port": port, "status": "running", "log_path": str(log_path)}

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
        self._auth_url = None
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
        # 优先返回带 token 的认证 URL(新版 dsh web 裸地址会 401);
        # token 尚未解析到或旧版无认证时退回裸地址
        return self._auth_url or f"http://127.0.0.1:{self._srv_port}"

    def _resolve_port(self, preferred: int, on_log=None) -> int:
        """返回可用端口。preferred 空闲则直接用; 被占用则尝试杀残留,仍占则递增探测。

        策略: 从 preferred 开始依次探测(最多 10 个端口), 每个端口:
        - 空闲 → 直接使用
        - 被占用 → 尝试 taskkill /F /PID 杀掉(处理 DSH 残留); 杀后若仍被占(外部进程)则跳下一个
        - 全部占满 → 抛 RuntimeError
        """
        if os.name != "nt":
            return preferred
        for attempt in range(preferred, preferred + 10):
            occupant = self._port_occupant(attempt)
            if occupant is None:
                return attempt
            # 端口被占, 尝试杀掉(处理 DSH 残留进程)
            if on_log:
                on_log(f"[清理] 端口 {attempt} 被占用(PID {occupant}), 尝试释放…")
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(occupant)],
                               capture_output=True, timeout=10, check=False)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.3)
            # 杀后复查
            if self._port_occupant(attempt) is None:
                if on_log:
                    on_log(f"[清理] 端口 {attempt} 已释放")
                return attempt
            # 外部进程无法杀掉 → 换下一个端口
            if on_log:
                on_log(f"[端口] {attempt} 被外部进程占用, 尝试 {attempt + 1}")
        raise RuntimeError(
            f"无法找到可用端口({preferred}–{preferred + 9} 均被外部占用)")

    @staticmethod
    def _port_occupant(port: int) -> int | None:
        """返回占用指定端口的 PID, 无占用返回 None。仅 Windows。"""
        if os.name != "nt":
            return None
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 ("$p=(Get-NetTCPConnection -LocalPort %d -State Listen "
                  "-ErrorAction SilentlyContinue).OwningProcess;"
                  "if($p){($p|Sort-Object -Unique)[0]}else{$null}") % port],
                capture_output=True, text=True, timeout=10, check=False,
                **_hidden_popen_kwargs())
            tok = r.stdout.strip()
            return int(tok) if tok.isdigit() else None
        except Exception:  # noqa: BLE001
            return None

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

    @staticmethod
    def _workspace_relation(new: Path, old: Path) -> str:
        """新路径与旧路径的关系: 'same' | 'nested'(新在旧内部, 动文件会自我吞并) | 'ok'。"""
        new_r = os.path.normcase(str(new.resolve()))
        old_r = os.path.normcase(str(old.resolve()))
        if new_r == old_r:
            return "same"
        if new_r.startswith(old_r + os.sep):
            return "nested"
        return "ok"

    def plan_workspace_move(self, path: str | Path) -> dict:
        """迁移预检: 不动任何文件、不停后端, 只报告动手后会发生什么。

        返回 {relation, conflicts: [{name, complete}], movable, skippable}。
        conflicts: 新路径已存在的同名目录。complete=True 表示是完整源码
        (有 package.json), 移动时会跳过并视为已迁移; complete=False 是残缺
        副本(如手动复制到一半), 需要用户逐个决定删除还是跳过。
        """
        new = self._resolve_ws(path)
        old = self.repos_dir
        relation = self._workspace_relation(new, old)
        conflicts: list[dict] = []
        movable = 0
        if relation == "ok" and old.exists():
            for item in sorted(old.iterdir()):
                if not item.is_dir():
                    continue
                target = new / item.name
                if target.exists():
                    conflicts.append(
                        {"name": item.name,
                         "complete": (target / "package.json").exists()})
                else:
                    movable += 1
        return {"relation": relation, "conflicts": conflicts,
                "movable": movable, "skippable": len(conflicts)}

    def apply_workspace(self, path: str | Path, on_old: str = "leave",
                        on_progress=None, on_cleanup=None,
                        resolutions: dict | None = None) -> dict:
        """切换工作区。on_old: move=把旧源码移动到新路径 / delete=删掉旧源码 / leave=不动旧源码。

        on_progress(done, total, name): move/delete 的逐版本进度回调。
        on_cleanup(done, total, name): 失败后清理半成品期间的进度回调。
        resolutions: {目录名: "delete_copy"|"skip"}, 对应预检发现的残缺副本。

        move/delete 会动旧路径里的文件。后端正从旧路径运行时必须先停掉:
        node 进程持有 node_modules 的文件句柄, Windows 上整目录 os.rename 会
        因句柄占用失败, shutil.move 退回逐文件复制, 复制到锁定文件时中途抛错,
        留下"新路径半份、旧路径原封不动"的残局。故先停后端再动文件。

        失败清理只会删**本次尝试中自己复制出来的**目录; 动手前就存在的目标
        目录(用户手动放的)无论成败都不碰——之前"src 和 tgt 都在就删 tgt"的
        清理会把用户预先复制到新路径的完整副本当成半成品删光, 属于数据毁灭。
        """
        if on_old not in ("move", "delete", "leave"):
            on_old = "leave"
        resolutions = resolutions or {}
        new = self._resolve_ws(path)
        old = self.repos_dir
        relation = self._workspace_relation(new, old)
        if relation == "same":
            return {"changed": False, "moved": 0, "action": "none", "new_path": str(new)}
        if relation == "nested":
            raise RuntimeError(
                f"新路径 {new} 在旧路径 {old} 内部, 执行会把旧路径搬进自己, 已阻止")

        # 逐项决定处置; 未给决定的残缺副本直接拒绝, 不擅自删用户的文件
        items: list[tuple[Path, str]] = []  # (源, 处置: move|skip|delete_copy)
        if on_old == "move" and old.exists():
            for item in sorted(old.iterdir()):
                if not item.is_dir():
                    continue
                target = new / item.name
                if not target.exists():
                    items.append((item, "move"))
                elif (target / "package.json").exists():
                    items.append((item, "skip"))
                elif resolutions.get(item.name) == "delete_copy":
                    items.append((item, "delete_copy"))
                elif resolutions.get(item.name) == "skip":
                    items.append((item, "skip"))
                else:
                    raise RuntimeError(
                        f"新路径已存在残缺目录 {target}, 请先删除它或重新预检")

        # 只在真的要动旧路径文件时才停后端(全跳过时切换配置即可, 后端可继续跑)
        needs_file_ops = on_old == "delete" or any(a != "skip" for _, a in items)
        stopped_backend = False
        if needs_file_ops and self.running:
            self.stop_dsh()
            stopped_backend = True
        new.mkdir(parents=True, exist_ok=True)

        moved = 0
        skipped: list[str] = []
        if on_old == "move":
            total = len(items)
            attempted: list[tuple[Path, Path]] = []  # 本次尝试新建的目标, 失败可清理
            try:
                for i, (item, act) in enumerate(items):
                    if on_progress:
                        on_progress(i, total, item.name)
                    if act == "skip":
                        skipped.append(item.name)
                        continue
                    target = new / item.name
                    if act == "delete_copy":
                        _rmtree_force(target)
                    attempted.append((item, target))
                    shutil.move(str(item), str(target))
                    moved += 1
                if on_progress:
                    on_progress(total, total, "")
            except Exception:
                self._cleanup_partial(attempted, on_cleanup)
                raise
        elif on_old == "delete":
            if on_progress:
                on_progress(0, 1, "")
            shutil.rmtree(old, ignore_errors=True)
            if on_progress:
                on_progress(1, 1, "")
        self.config["workspace"] = str(new)
        self.repos_dir = new
        self.save_config()
        return {"changed": True, "moved": moved, "skipped": skipped,
                "action": on_old, "new_path": str(new),
                "stopped_backend": stopped_backend}

    @staticmethod
    def _cleanup_partial(attempted, on_cleanup=None) -> None:
        """失败后清理本次尝试复制出来的半成品。

        只删"旧路径原件还在"(即没移完)且属于本次尝试的目标目录;
        已完整移走的保留。用户预先存在的目录根本不会进 attempted。
        """
        doomed = [(src, tgt) for src, tgt in attempted
                  if src.exists() and tgt.exists()]
        total = len(doomed)
        for i, (src, tgt) in enumerate(doomed):
            if on_cleanup:
                on_cleanup(i, total, tgt.name)
            try:
                _rmtree_force(tgt)
            except Exception:  # noqa: BLE001  清理失败不掩盖最初错误
                pass
        if on_cleanup:
            on_cleanup(total, total, "")

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

