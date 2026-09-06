import json
import os
import subprocess
import tempfile
from unittest import mock

import pytest

import dsh_core
from pathlib import Path
from dsh_core import DSHManager, sort_versions, _spawn_cmd


# ---------------- sort_versions ----------------

def test_sort_versions_desc():
    tags = ["v0.1.0-rc.6", "v0.1.0", "v2.0.0", "v0.9.0"]
    out = sort_versions(tags)
    assert out[0] == "v2.0.0"
    assert out[1] == "v0.9.0"
    # v0.1.0 > v0.1.0-rc.6
    assert out.index("v0.1.0") < out.index("v0.1.0-rc.6")


def test_sort_versions_filters_non_version():
    tags = ["v1.0.0", "docs-update", "master"]
    out = sort_versions(tags)
    assert "v1.0.0" in out
    assert "docs-update" not in out


def test_sort_versions_dsh_prefix():
    # 官方 tag 形如 dsh-v0.1.0-rc.x
    tags = ["dsh-v0.1.0-rc.7", "dsh-v0.1.0-rc.8", "master"]
    out = sort_versions(tags)
    assert out[0] == "dsh-v0.1.0-rc.8"
    assert out[1] == "dsh-v0.1.0-rc.7"
    assert "master" not in out


# ---------------- proxy ----------------

def _manager(tmp_path):
    return DSHManager(data_dir=str(tmp_path))


def test_get_proxy_disabled(tmp_path):
    m = _manager(tmp_path)
    assert m.get_proxy() is None
    assert m.git_proxy_args() == []


def test_get_proxy_enabled(tmp_path):
    m = _manager(tmp_path)
    m.save_proxy(True, "127.0.0.1", "7897")
    assert m.get_proxy() == {
        "http": "http://127.0.0.1:7897",
        "https": "http://127.0.0.1:7897",
    }


def test_git_proxy_args_enabled(tmp_path):
    m = _manager(tmp_path)
    m.save_proxy(True, "127.0.0.1", "7897")
    args = m.git_proxy_args()
    assert "-c" in args
    assert "http.proxy=http://127.0.0.1:7897" in args
    assert "https.proxy=http://127.0.0.1:7897" in args


# ---------------- config persistence ----------------

def test_config_persisted(tmp_path):
    m = _manager(tmp_path)
    m.save_proxy(True, "10.0.0.1", "1080")
    # new instance reads from disk
    m2 = _manager(tmp_path)
    assert m2.get_proxy() == {
        "http": "http://10.0.0.1:1080",
        "https": "http://10.0.0.1:1080",
    }


# ---------------- node check ----------------

@mock.patch("dsh_core.subprocess.run")
def test_node_available_all(mock_run, tmp_path):
    def fake_run(cmd, *a, **k):
        class R:
            returncode = 0
            stdout = ""
        if cmd[0] == "node":
            R.stdout = "v22.5.0\n"
        else:
            R.stdout = "9.12.0\n"
        return R()
    mock_run.side_effect = fake_run
    m = _manager(tmp_path)
    res = m.node_available()
    assert res["node"] is True
    assert res["pnpm"] is True
    assert res["node_version"] == "v22.5.0"


@mock.patch("dsh_core.subprocess.run")
def test_node_missing(mock_run, tmp_path):
    mock_run.side_effect = FileNotFoundError
    m = _manager(tmp_path)
    res = m.node_available()
    assert res["node"] is False


# ---------------- clone ----------------

@mock.patch("dsh_core.subprocess.Popen")
def test_clone_tag_command(mock_popen, tmp_path):
    class FakeProc:
        returncode = 0
        def __init__(self, *a, **k):
            self.stdout = iter([])  # 无输出
        def wait(self):
            return 0
    mock_popen.side_effect = FakeProc
    m = _manager(tmp_path)
    m.save_proxy(True, "127.0.0.1", "7897")
    path = m.clone_tag("v0.1.0")
    cmd = mock_popen.call_args[0][0]
    assert cmd[0] == "git"
    assert "--branch" in cmd
    assert "v0.1.0" in cmd
    assert "--depth" in cmd
    assert "http.proxy=http://127.0.0.1:7897" in " ".join(cmd)
    assert path.endswith("dsh-v0.1.0")


@mock.patch("dsh_core.subprocess.Popen")
def test_clone_reuses_complete(mock_popen, tmp_path):
    """package.json + .git 都齐全才算已下载, 复用不再重新克隆。"""
    m = _manager(tmp_path)
    d = _mk_repo(m, "v0.1.0")
    (d / ".git").mkdir()
    path = m.clone_tag("v0.1.0")
    mock_popen.assert_not_called()
    assert path == str(d)


@mock.patch("dsh_core.subprocess.Popen")
def test_clone_cleans_partial(mock_popen, tmp_path):
    """只有 .git 的残缺目录(下载中断残留)要清理并重新克隆。"""
    class FakeProc:
        returncode = 0
        def __init__(self, *a, **k):
            self.stdout = iter([])
        def wait(self):
            return 0
    mock_popen.side_effect = FakeProc
    m = _manager(tmp_path)
    d = m.repo_dir_for("v0.1.0")
    d.mkdir(parents=True)
    (d / ".git").mkdir()          # 只有 .git, 无源码 → 视为残缺
    path = m.clone_tag("v0.1.0")
    assert path == str(d)
    mock_popen.assert_called_once()      # 真的重新克隆了
    assert (d / ".dsh-tag").exists()     # 克隆完成标记


@mock.patch("dsh_core.subprocess.Popen")
def test_clone_fails_cleans_partial(mock_popen, tmp_path):
    """克隆失败时清掉半成品目录, 避免残留污染工作区。"""
    mock_popen.side_effect = RuntimeError("boom")
    m = _manager(tmp_path)
    d = m.repo_dir_for("v0.1.0")
    d.mkdir(parents=True)
    (d / ".git").mkdir()
    with pytest.raises(RuntimeError):
        m.clone_tag("v0.1.0")
    assert not d.exists()   # 失败残留被清理


# ---------------- fetch_versions ----------------

@mock.patch("dsh_core.requests.get")
def test_fetch_versions(mock_get, tmp_path):
    mock_get.return_value.json.return_value = [
        {"name": "v0.9.0"}, {"name": "v1.0.0"}, {"name": "master"}
    ]
    mock_get.return_value.raise_for_status.return_value = None
    m = _manager(tmp_path)
    versions = m.fetch_versions(force=True)
    assert versions[0] == "v1.0.0"
    assert versions[1] == "v0.9.0"


# ---------------- theme / local repos ----------------

def test_set_theme_persists(tmp_path):
    m = _manager(tmp_path)
    m.set_theme("light")
    m2 = _manager(tmp_path)
    assert m2.config["theme"] == "light"


def test_set_theme_system(tmp_path):
    m = _manager(tmp_path)
    assert m.set_theme("system") == "system"
    m2 = _manager(tmp_path)
    assert m2.config["theme"] == "system"


def test_local_repos(tmp_path):
    m = _manager(tmp_path)
    d = m.repos_dir / "dsh-v0.1.0"
    d.mkdir(parents=True)
    (d / "package.json").write_text("{}")
    assert m.local_repos() == [{"tag": "v0.1.0", "path": str(d)}]


def test_repo_dir_for_strips_prefix(tmp_path):
    m = _manager(tmp_path)
    # 关键: 不能重复 dsh- 前缀(曾导致"未下载"误报)
    assert m.repo_dir_for("dsh-v0.1.1-rc.1") == m.repos_dir / "dsh-v0.1.1-rc.1"
    assert m.repo_dir_for("v0.2.0") == m.repos_dir / "dsh-v0.2.0"


# ---------------- workspace ----------------

def _mk_repo(m, tag):
    d = m.repos_dir / f"dsh-{tag}"
    d.mkdir(parents=True)
    (d / "package.json").write_text("{}")
    (d / ".dsh-tag").write_text(tag)
    return d


def test_preview_same_path_no_prompt(tmp_path):
    m = _manager(tmp_path)
    p = m.preview_workspace(m.repos_dir)
    assert p["same"] is True
    assert p["will_prompt"] is False


def test_preview_new_path_no_source(tmp_path):
    m = _manager(tmp_path)
    newdir = tmp_path / "newrepo"
    p = m.preview_workspace(newdir)
    assert p["same"] is False
    assert p["old_count"] == 0
    assert p["will_prompt"] is False


def test_preview_new_path_with_source_prompts(tmp_path):
    m = _manager(tmp_path)
    _mk_repo(m, "v0.1.0")
    p = m.preview_workspace(tmp_path / "newrepo")
    assert p["old_count"] == 1
    assert p["will_prompt"] is True


def test_apply_move(tmp_path):
    m = _manager(tmp_path)
    _mk_repo(m, "v0.1.0")
    old = m.repos_dir
    newdir = tmp_path / "newrepo"
    res = m.apply_workspace(newdir, on_old="move")
    assert res["changed"] is True
    assert res["moved"] == 1
    assert (newdir / "dsh-v0.1.0" / "package.json").exists()
    assert not (old / "dsh-v0.1.0").exists()
    assert m.repos_dir == newdir
    # 持久化
    m2 = _manager(tmp_path)
    assert str(m2.repos_dir) == str(newdir)
    assert m2.local_repos()[0]["tag"] == "v0.1.0"


def test_apply_delete(tmp_path):
    m = _manager(tmp_path)
    _mk_repo(m, "v0.1.0")
    newdir = tmp_path / "newrepo"
    res = m.apply_workspace(newdir, on_old="delete")
    assert res["changed"] is True
    assert m.local_repos() == []
    assert m.repos_dir == newdir


def test_apply_leave(tmp_path):
    m = _manager(tmp_path)
    old = _mk_repo(m, "v0.1.0")
    newdir = tmp_path / "newrepo"
    res = m.apply_workspace(newdir, on_old="leave")
    assert res["changed"] is True
    assert (old / "package.json").exists()  # 旧源码仍在
    assert m.local_repos() == []            # 新路径无源码


def test_apply_same_noop(tmp_path):
    m = _manager(tmp_path)
    _mk_repo(m, "v0.1.0")
    res = m.apply_workspace(m.repos_dir, on_old="move")
    assert res["changed"] is False
    assert (m.repos_dir / "dsh-v0.1.0").exists()


def test_apply_move_stops_running_backend(tmp_path):
    """后端运行中 move/delete 必须先停后端, leave 不停。"""
    m = _manager(tmp_path)
    _mk_repo(m, "v0.1.0")
    newdir = tmp_path / "newrepo"
    with mock.patch.object(DSHManager, "running", new_callable=mock.PropertyMock,
                           return_value=True), \
         mock.patch.object(m, "stop_dsh", wraps=m.stop_dsh) as spy:
        res = m.apply_workspace(newdir, on_old="move")
        assert spy.called
        assert res["stopped_backend"] is True
    # leave 不应碰后端
    m2 = _manager(tmp_path / "d2")
    _mk_repo(m2, "v0.1.0")
    with mock.patch.object(DSHManager, "running", new_callable=mock.PropertyMock,
                           return_value=True), \
         mock.patch.object(m2, "stop_dsh", wraps=m2.stop_dsh) as spy2:
        m2.apply_workspace(tmp_path / "d2" / "nw", on_old="leave")
        assert not spy2.called


def test_apply_move_reports_progress(tmp_path):
    m = _manager(tmp_path)
    _mk_repo(m, "v0.1.0")
    _mk_repo(m, "v0.1.1")
    calls = []
    res = m.apply_workspace(tmp_path / "nw", on_old="move",
                            on_progress=lambda d, t, n: calls.append((d, t, n)))
    assert res["moved"] == 2
    assert calls[0] == (0, 2, "dsh-v0.1.0") or calls[0] == (0, 2, "dsh-v0.1.1")
    assert calls[-1] == (2, 2, "")


def test_apply_move_failure_cleans_partial(tmp_path):
    """移动中途失败: 半成品被清理、旧路径完整、配置不切换、异常上抛。"""
    m = _manager(tmp_path)
    _mk_repo(m, "v0.1.0")
    _mk_repo(m, "v0.1.1")
    old = m.repos_dir
    newdir = tmp_path / "newrepo"
    real_move = dsh_core.shutil.move
    def fake_move(src, dst):
        if "v0.1.1" in src:
            # 模拟第一个目录已移走后, 第二个复制到一半失败
            Path(dst).mkdir(parents=True)
            (Path(dst) / "partial.bin").write_bytes(b"x")
            raise PermissionError("文件被占用")
        return real_move(src, dst)
    with mock.patch.object(dsh_core.shutil, "move", side_effect=fake_move):
        with pytest.raises(PermissionError):
            m.apply_workspace(newdir, on_old="move")
    # 已完整移走的 v0.1.0 保留在新路径(不是垃圾, 不误删)
    assert not (old / "dsh-v0.1.0").exists()
    assert (newdir / "dsh-v0.1.0" / "package.json").exists()
    # 失败的 v0.1.1: 旧路径原封不动, 新路径半成品被清掉
    assert (old / "dsh-v0.1.1" / "package.json").exists()
    assert not (newdir / "dsh-v0.1.1").exists()
    # 配置不切换
    assert m.repos_dir == old
    assert m.config["workspace"] != str(newdir)


def _mk_copy(dst: Path, src: Path):
    """模拟用户手动把 src 完整复制到 dst。"""
    dst.mkdir(parents=True)
    (dst / "package.json").write_text((src / "package.json").read_text())


def test_plan_move_reports_conflicts(tmp_path):
    m = _manager(tmp_path)
    _mk_repo(m, "v0.1.0")
    _mk_repo(m, "v0.1.1")
    newdir = tmp_path / "newrepo"
    # 用户手动复制了完整的 v0.1.0 和残缺的 v0.1.1 到新路径
    _mk_copy(newdir / "dsh-v0.1.0", m.repos_dir / "dsh-v0.1.0")
    (newdir / "dsh-v0.1.1").mkdir()
    plan = m.plan_workspace_move(newdir)
    assert plan["relation"] == "ok"
    assert plan["movable"] == 0
    by_name = {c["name"]: c["complete"] for c in plan["conflicts"]}
    assert by_name == {"dsh-v0.1.0": True, "dsh-v0.1.1": False}


def test_plan_move_detects_nested(tmp_path):
    m = _manager(tmp_path)
    _mk_repo(m, "v0.1.0")
    plan = m.plan_workspace_move(m.repos_dir / "sub")
    assert plan["relation"] == "nested"


def test_apply_move_skips_complete_existing_copy(tmp_path):
    """用户先手动复制完整源码到新路径再切换: 跳过、不删副本、照常切配置、不停后端。"""
    m = _manager(tmp_path)
    src = _mk_repo(m, "v0.1.0")
    newdir = tmp_path / "newrepo"
    manual = newdir / "dsh-v0.1.0"
    _mk_copy(manual, src)
    with mock.patch.object(DSHManager, "running", new_callable=mock.PropertyMock,
                           return_value=True), \
         mock.patch.object(m, "stop_dsh", wraps=m.stop_dsh) as spy:
        res = m.apply_workspace(newdir, on_old="move")
        assert not spy.called  # 没动任何旧文件, 后端不该停
    assert res["moved"] == 0
    assert res["skipped"] == ["dsh-v0.1.0"]
    assert res["stopped_backend"] is False
    assert (manual / "package.json").exists()   # 用户副本完好
    assert (src / "package.json").exists()      # 旧路径也未动
    assert m.repos_dir == newdir


def test_apply_move_incomplete_conflict_requires_resolution(tmp_path):
    """残缺副本未给决定 → 拒绝执行, 不动任何文件; 给了决定按决定办。"""
    m = _manager(tmp_path)
    src = _mk_repo(m, "v0.1.0")
    newdir = tmp_path / "newrepo"
    broken = newdir / "dsh-v0.1.0"
    broken.mkdir(parents=True)
    (broken / "junk.txt").write_text("x")
    with pytest.raises(RuntimeError, match="残缺"):
        m.apply_workspace(newdir, on_old="move")
    assert (src / "package.json").exists()   # 什么都没动
    assert (broken / "junk.txt").exists()
    assert m.repos_dir != newdir
    # 决定=跳过: 两边都保留, 配置照常切换
    res = m.apply_workspace(newdir, on_old="move", resolutions={"dsh-v0.1.0": "skip"})
    assert res["moved"] == 0 and res["skipped"] == ["dsh-v0.1.0"]
    assert (src / "package.json").exists() and (broken / "junk.txt").exists()
    # 决定=删除副本: 副本被替换为旧路径源码
    m2 = _manager(tmp_path / "d2")
    src2 = _mk_repo(m2, "v0.1.0")
    nd2 = tmp_path / "d2" / "newrepo"
    (nd2 / "dsh-v0.1.0").mkdir(parents=True)
    (nd2 / "dsh-v0.1.0" / "junk.txt").write_text("x")
    res2 = m2.apply_workspace(nd2, on_old="move",
                              resolutions={"dsh-v0.1.0": "delete_copy"})
    assert res2["moved"] == 1
    assert (nd2 / "dsh-v0.1.0" / "package.json").exists()
    assert not (nd2 / "dsh-v0.1.0" / "junk.txt").exists()
    assert not src2.exists()


def test_apply_move_failure_never_deletes_preexisting_target(tmp_path):
    """回归: 失败清理只删本次尝试复制出的目录, 预存在的目标目录不碰。

    事故现场: 用户手动复制完整源码到新路径 → 程序 move 时目标已存在而失败
    → 旧清理逻辑把用户副本当半成品删光(数据毁灭)。现在:
    - 完整副本在执行前就被跳过, 根本不会进 attempted;
    - 残缺副本只有用户明确选择 delete_copy 才会删, 删完即进 attempted,
      其余任何失败路径都不得删除预存在目录。
    """
    m = _manager(tmp_path)
    _mk_repo(m, "v0.1.0")
    _mk_repo(m, "v0.1.1")
    newdir = tmp_path / "newrepo"
    manual = newdir / "dsh-v0.1.0"
    _mk_copy(manual, m.repos_dir / "dsh-v0.1.0")   # 用户手动放的完整副本
    real_move = dsh_core.shutil.move
    def fake_move(src, dst):
        if "v0.1.1" in str(src):
            Path(dst).mkdir(parents=True)
            (Path(dst) / "partial.bin").write_bytes(b"x")
            raise PermissionError("文件被占用")
        return real_move(src, dst)
    with mock.patch.object(dsh_core.shutil, "move", side_effect=fake_move):
        with pytest.raises(PermissionError):
            m.apply_workspace(newdir, on_old="move")
    # 关键断言: 用户手动副本原封不动
    assert (manual / "package.json").exists()
    # 我们自己复制出来的 v0.1.1 半成品被清掉
    assert not (newdir / "dsh-v0.1.1").exists()
    # 旧路径 v0.1.1 原件仍在
    assert (m.repos_dir / "dsh-v0.1.1" / "package.json").exists()


def test_apply_move_nested_rejected_without_touching_anything(tmp_path):
    """新路径在旧路径内部: 直接拒绝, 后端不停、文件不动。"""
    m = _manager(tmp_path)
    src = _mk_repo(m, "v0.1.0")
    with mock.patch.object(DSHManager, "running", new_callable=mock.PropertyMock,
                           return_value=True), \
         mock.patch.object(m, "stop_dsh", wraps=m.stop_dsh) as spy:
        with pytest.raises(RuntimeError, match="内部"):
            m.apply_workspace(src / "sub", on_old="move")
        assert not spy.called
    assert (src / "package.json").exists()


# ---------------- delete ----------------


def test_delete_tag(tmp_path):
    m = _manager(tmp_path)
    d = _mk_repo(m, "v0.1.0")
    assert m.delete_tag("v0.1.0") is True
    assert not d.exists()
    assert m.local_repos() == []


def test_delete_tag_not_exists(tmp_path):
    m = _manager(tmp_path)
    assert m.delete_tag("v0.1.0") is False


def test_delete_tag_running_forbidden(tmp_path):
    m = _manager(tmp_path)
    _mk_repo(m, "v0.1.0")
    m._proc = mock.Mock()
    m._proc.poll.return_value = None   # 模拟进程存活
    m.config["last_tag"] = "v0.1.0"
    with pytest.raises(RuntimeError):
        m.delete_tag("v0.1.0")
    assert m.repo_dir_for("v0.1.0").exists()   # 未被删除


def test_delete_tag_readonly_git_objects(tmp_path):
    """git 的 .git/objects 只读 pack 文件也能被强删, 不残留半截目录。"""
    m = _manager(tmp_path)
    d = _mk_repo(m, "v0.1.0")
    pack = d / ".git" / "objects"
    pack.mkdir(parents=True)
    f = pack / "x.pack"
    f.write_bytes(b"x" * 16)
    os.chmod(f, 0o444)                        # 只读: S_IREAD
    assert m.delete_tag("v0.1.0") is True
    assert not d.exists()                     # 整个目录被清掉, 无残留


# ---------------- stop ----------------

@mock.patch("dsh_core.subprocess.run")
def test_stop_dsh(mock_run, tmp_path):
    mock_run.return_value.returncode = 0
    m = _manager(tmp_path)
    m._proc_pid = 1234
    assert m.stop_dsh() is True
    cmd = mock_run.call_args[0][0]
    assert "taskkill" in cmd or "kill" in " ".join(cmd)


# ---------------- port resolve ----------------

def test_port_resolves_free(tmp_path):
    """端口空闲时 _resolve_port 直接返回 preferred。"""
    m = _manager(tmp_path)
    with mock.patch.object(type(m), '_port_occupant', return_value=None):
        assert m._resolve_port(3080) == 3080


def test_port_resolves_after_kill(tmp_path):
    """端口被占, taskkill 后释放 → 返回 preferred。"""
    m = _manager(tmp_path)
    with mock.patch.object(type(m), '_port_occupant', side_effect=[1234, None]), \
         mock.patch("dsh_core.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        assert m._resolve_port(3080) == 3080


def test_port_resolves_foreign_skip(tmp_path):
    """端口被外部占用且杀不掉 → 换下一个端口。"""
    m = _manager(tmp_path)
    # 3080 被占且杀后仍被占, 3081 空闲
    with mock.patch.object(type(m), '_port_occupant',
                           side_effect=[9999, 9999, None]), \
         mock.patch("dsh_core.subprocess.run") as mock_run, \
         mock.patch("dsh_core.time.sleep"):
        mock_run.return_value.returncode = 0
        assert m._resolve_port(3080) == 3081


# ---------------- start (智能跳过 install/build) ----------------

def _mk_built_repo(m, tag):
    """构造一个依赖已装、前端已构建的完整 DSH 仓库。"""
    repo = m.repo_dir_for(tag)
    repo.mkdir(parents=True)
    (repo / "package.json").write_text("{}")
    (repo / "node_modules" / ".pnpm").mkdir(parents=True)
    (repo / "apps" / "web" / "dist").mkdir(parents=True)
    return repo


def _fake_popen(mock_popen):
    class FakeProc:
        returncode = 0
        def __init__(self, *a, **k):
            self.stdout = iter([])
            self.pid = 9999
        def poll(self):
            return None
        def wait(self):
            return 0
    mock_popen.side_effect = lambda *a, **k: FakeProc()


@mock.patch("dsh_core.time.sleep")
@mock.patch("dsh_core.requests.get")
@mock.patch("dsh_core.subprocess.run")
@mock.patch("dsh_core.subprocess.Popen")
@mock.patch.object(dsh_core.DSHManager, '_resolve_port',
                   lambda self, preferred, on_log=None: preferred)
def test_start_skips_install_and_build_when_ready(
        mock_popen, mock_run, mock_get, mock_sleep, tmp_path):
    """依赖已装 + 前端已构建 => 只启动 dsh web, 不再 install/build。"""
    mock_run.return_value.returncode = 0
    mock_get.return_value = None
    _fake_popen(mock_popen)
    m = _manager(tmp_path)
    repo = _mk_built_repo(m, "v0.1.0")
    m.start_dsh(repo, on_log=lambda l: None)
    assert mock_popen.call_count == 1
    cmd = mock_popen.call_args_list[0].args[0]
    assert cmd == _spawn_cmd(["pnpm", "dsh", "web", "--port", "3080", "--no-open"])


@mock.patch("dsh_core.time.sleep")
@mock.patch("dsh_core.requests.get")
@mock.patch("dsh_core.subprocess.run")
@mock.patch("dsh_core.subprocess.Popen")
@mock.patch.object(dsh_core.DSHManager, '_resolve_port',
                   lambda self, preferred, on_log=None: preferred)
def test_start_builds_when_missing(
        mock_popen, mock_run, mock_get, mock_sleep, tmp_path):
    """无依赖/无前端产物 => 依次 install -> build -> dsh web。"""
    mock_run.return_value.returncode = 0
    mock_get.return_value = None
    _fake_popen(mock_popen)
    m = _manager(tmp_path)
    repo = m.repo_dir_for("v0.1.0")
    repo.mkdir(parents=True)
    (repo / "package.json").write_text("{}")
    m.start_dsh(repo, on_log=lambda l: None)
    cmds = [c.args[0] for c in mock_popen.call_args_list]
    assert cmds[0] == _spawn_cmd(["pnpm", "install"])
    assert cmds[1] == _spawn_cmd(["pnpm", "run", "build"])
    assert cmds[2] == _spawn_cmd(["pnpm", "dsh", "web", "--port", "3080", "--no-open"])


@mock.patch("dsh_core.time.sleep")
@mock.patch("dsh_core.requests.get")
@mock.patch("dsh_core.subprocess.run")
@mock.patch("dsh_core.subprocess.Popen")
@mock.patch.object(dsh_core.DSHManager, '_resolve_port',
                   lambda self, preferred, on_log=None: preferred)
def test_start_pnpm_env_skips_deps_check(
        mock_popen, mock_run, mock_get, mock_sleep, tmp_path):
    """pnpm 子进程须带 pnpm_config_verify_deps_before_run=false。"""
    mock_run.return_value.returncode = 0
    mock_get.return_value = None
    _fake_popen(mock_popen)
    m = _manager(tmp_path)
    repo = m.repo_dir_for("v0.1.0")
    repo.mkdir(parents=True)
    (repo / "package.json").write_text("{}")
    m.start_dsh(repo, on_log=lambda l: None)
    for call in mock_popen.call_args_list:
        env = call.kwargs.get("env") or {}
        assert env.get("pnpm_config_verify_deps_before_run") == "false"


@mock.patch("dsh_core.time.sleep")
@mock.patch("dsh_core.requests.get")
@mock.patch("dsh_core.subprocess.run")
@mock.patch("dsh_core.subprocess.Popen")
@mock.patch.object(dsh_core.DSHManager, '_resolve_port',
                   lambda self, preferred, on_log=None: preferred)
def test_start_dsh_creates_log_file(
        mock_popen, mock_run, mock_get, mock_sleep, tmp_path):
    """start_dsh 在 data/logs/ 创建本次运行的日志文件。"""
    mock_run.return_value.returncode = 0
    mock_get.return_value = None
    _fake_popen(mock_popen)
    m = _manager(tmp_path)
    repo = _mk_built_repo(m, "v0.1.0")
    result = m.start_dsh(repo, on_log=lambda l: None)
    log_path = Path(result["log_path"])
    assert log_path.exists()
    assert log_path.suffix == ".log"
    assert "dsh-" in log_path.name
    log_content = log_path.read_text(encoding="utf-8")
    # 日志应包含端口信息或就绪信息
    assert "3080" in log_content or "启动后端" in log_content or "就绪" in log_content
