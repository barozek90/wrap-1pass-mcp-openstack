"""Small helpers for tests that run real subprocesses."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

FAKE = str(Path(__file__).with_name("fake_mcp_server.py"))


def events(log: Path) -> list[dict]:
    """The events ``fake_mcp_server.py`` wrote to its log file."""
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def pid_gone(pid: int) -> bool:
    """True when the process no longer exists (a zombie counts as gone)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
    except OSError:
        return True
