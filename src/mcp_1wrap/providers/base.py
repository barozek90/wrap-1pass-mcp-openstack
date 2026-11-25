"""Types shared by all providers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from mcp_1wrap.op import OpClient


@dataclass
class Context:
    op: OpClient
    vault: str                       # profile / default vault
    environ: Mapping[str, str]
    runtime: Callable[[], Path]      # private runtime directory (created lazily)
    note: Callable[[str], None]      # message for stderr


@dataclass
class Result:
    env: dict[str, str] = field(default_factory=dict)   # variables for the server
    summary: list[str] = field(default_factory=list)    # lines for --check / --debug
