"""Thin wrapper over the 1Password CLI (``op``).

Every call gets a timeout (``op`` may wait for an unlock prompt that a GUI
client never shows) and ``stdin=/dev/null`` (the MCP stdio stream must never
be touched). Error messages carry the stage and ``op``'s own stderr, never
secret values.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping
from typing import Any

from mcp_1wrap.errors import OpError

DEFAULT_TIMEOUT = 60


class OpClient:
    def __init__(self, environ: Mapping[str, str], timeout: int = DEFAULT_TIMEOUT) -> None:
        self._environ = dict(environ)
        self.timeout = timeout

    def _run(self, stage: str, args: list[str]) -> str:
        exe = shutil.which("op", path=self._environ.get("PATH"))
        if exe is None:
            raise OpError(
                f"[{stage}] polecenie 'op' (1Password CLI) nie jest w PATH "
                f"({self._environ.get('PATH', '')})"
            )
        try:
            result = subprocess.run(
                [exe, *args],
                check=True,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=self.timeout,
                env=self._environ,
            )
        except subprocess.TimeoutExpired:
            raise OpError(
                f"[{stage}] 'op' nie odpowiedziało w {self.timeout}s - odblokuj "
                f"1Password i sprawdź integrację CLI (Settings -> Developer)"
            ) from None
        except subprocess.CalledProcessError as exc:
            raise OpError(
                f"[{stage}] 'op {args[0]}' zwróciło kod {exc.returncode}: "
                f"{exc.stderr.strip()}"
            ) from None
        return result.stdout

    def json(self, stage: str, *args: str) -> Any:
        out = self._run(stage, [*args, "--format", "json"])
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            raise OpError(f"[{stage}] niepoprawny JSON z 'op': {exc}") from None

    def read(self, stage: str, ref: str) -> str:
        return self._run(stage, ["read", "--no-newline", ref])
