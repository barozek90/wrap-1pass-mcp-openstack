"""Repair the thin environment GUI clients start MCP servers with."""

from __future__ import annotations

import os
import pwd
import stat
from collections.abc import MutableMapping


def harden(env: MutableMapping[str, str]) -> None:
    """Fill in HOME, PATH, XDG_RUNTIME_DIR and the DBus address when missing."""
    uid = os.getuid()
    if not env.get("HOME"):
        try:
            env["HOME"] = pwd.getpwuid(uid).pw_dir
        except KeyError:
            pass

    extra = []
    if env.get("HOME"):
        extra.append(f"{env['HOME']}/.local/bin")
    extra += ["/usr/local/bin", "/usr/bin", "/bin"]
    current = [p for p in env.get("PATH", "").split(":") if p]
    merged: list[str] = []
    for entry in extra + current:
        if entry not in merged:
            merged.append(entry)
    env["PATH"] = ":".join(merged)

    if not env.get("XDG_RUNTIME_DIR"):
        candidate = f"/run/user/{uid}"
        if os.path.isdir(candidate):
            env["XDG_RUNTIME_DIR"] = candidate

    xdg = env.get("XDG_RUNTIME_DIR")
    if xdg and not env.get("DBUS_SESSION_BUS_ADDRESS"):
        bus = os.path.join(xdg, "bus")
        try:
            if stat.S_ISSOCK(os.stat(bus).st_mode):
                env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
        except OSError:
            pass
