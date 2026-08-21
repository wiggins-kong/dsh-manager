import json
import os
import subprocess
import tempfile
from unittest import mock

import pytest

import dsh_core
from dsh_core import DSHManager, sort_versions


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


# ---------------- stop ----------------

@mock.patch("dsh_core.subprocess.run")
def test_stop_dsh(mock_run, tmp_path):
    mock_run.return_value.returncode = 0
    m = _manager(tmp_path)
    m._proc_pid = 1234
    assert m.stop_dsh() is True
    cmd = mock_run.call_args[0][0]
    assert "taskkill" in cmd or "kill" in " ".join(cmd)
