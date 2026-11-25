import os
import stat

import pytest

from mcp_1wrap import fsutil
from mcp_1wrap.errors import WrapError


def test_private_dir_is_0700(tmp_path):
    d = fsutil.private_dir(tmp_path / "a" / "b")
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


def test_private_dir_rejects_loose_permissions(tmp_path):
    d = tmp_path / "open"
    d.mkdir(mode=0o755)
    d.chmod(0o755)
    with pytest.raises(WrapError, match="zbyt szerokie uprawnienia"):
        fsutil.private_dir(d)


def test_private_dir_rejects_symlink(tmp_path):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(WrapError, match="symlink"):
        fsutil.private_dir(link)


def test_write_private_atomic(tmp_path):
    f = tmp_path / "secure.yaml"
    fsutil.write_private_atomic(f, "one")
    fsutil.write_private_atomic(f, "two")
    assert f.read_text() == "two"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["secure.yaml"]  # no temp leftovers


def test_runtime_base_prefers_xdg(tmp_path):
    notes = []
    assert fsutil.runtime_base({"XDG_RUNTIME_DIR": str(tmp_path)}, notes.append) == tmp_path
    assert notes == []


def test_runtime_base_falls_back_to_shm(tmp_path, monkeypatch):
    shm = tmp_path / "shm"
    shm.mkdir()
    monkeypatch.setattr(fsutil, "SHM_DIR", shm)
    monkeypatch.setattr(fsutil, "RUN_USER_FMT", str(tmp_path / "nope-{uid}"))
    notes = []
    base = fsutil.runtime_base({}, notes.append)
    assert base.parent == shm
    assert stat.S_IMODE(base.stat().st_mode) == 0o700
    assert "tmpfs" in notes[0]


def test_runtime_base_last_resort_warns_about_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(fsutil, "SHM_DIR", tmp_path / "missing")
    monkeypatch.setattr(fsutil, "RUN_USER_FMT", str(tmp_path / "nope-{uid}"))
    notes = []
    base = fsutil.runtime_base({"TMPDIR": str(tmp_path)}, notes.append)
    assert base.parent == tmp_path
    assert "dysk" in notes[0]
    assert os.getuid() == base.stat().st_uid
