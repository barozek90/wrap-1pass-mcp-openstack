"""Private runtime directories and atomic 0600 writes for files with secrets."""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path

from mcp_1wrap.errors import WrapError

SHM_DIR = Path("/dev/shm")
RUN_USER_FMT = "/run/user/{uid}"


def usable(path: Path) -> bool:
    return path.is_dir() and os.access(path, os.W_OK | os.X_OK)


def private_dir(path: Path) -> Path:
    """Create ``path`` (0700) and verify it is ours, not a symlink, not open."""
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = path.lstat()
    except OSError as exc:
        raise WrapError(f"[write] nie mogę utworzyć {path}: {exc}") from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise WrapError(f"[write] {path} nie jest zwykłym katalogiem (symlink?)")
    if st.st_uid != os.getuid():
        raise WrapError(f"[write] {path} należy do innego użytkownika (uid {st.st_uid})")
    if st.st_mode & 0o077:
        raise WrapError(
            f"[write] {path} ma zbyt szerokie uprawnienia "
            f"({oct(st.st_mode & 0o777)}), oczekiwane 0700"
        )
    return path


def runtime_base(environ: Mapping[str, str], note: Callable[[str], None]) -> Path:
    """Where to keep files with secrets: RAM-backed user directories first."""
    uid = os.getuid()
    candidates = []
    xdg = environ.get("XDG_RUNTIME_DIR", "").strip()
    if xdg:
        candidates.append(Path(xdg))
    candidates.append(Path(RUN_USER_FMT.format(uid=uid)))
    for candidate in candidates:
        if usable(candidate):
            return candidate

    if usable(SHM_DIR):
        note(f"brak XDG_RUNTIME_DIR i /run/user/{uid}, używam {SHM_DIR} (tmpfs)")
        return private_dir(SHM_DIR / f"mcp-1wrap-{uid}")
    tmp = Path(environ.get("TMPDIR", "").strip() or "/tmp")
    note(f"UWAGA: brak katalogu w RAM, pliki z sekretami trafią na dysk w {tmp}")
    return private_dir(tmp / f"mcp-1wrap-{uid}")


def write_private_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` with mode 0600 via temp file + rename.

    Atomic replace keeps concurrent launchers (terminal and desktop client)
    from ever reading a half-written file.
    """
    tmp_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), 0o600)
            fh.write(text)
        os.replace(tmp_name, path)
    except OSError as exc:
        if tmp_name:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
        raise WrapError(f"[write] nie mogę zapisać {path}: {exc}") from None
