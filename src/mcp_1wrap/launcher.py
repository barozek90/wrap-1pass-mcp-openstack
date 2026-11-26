"""Prepare the environment and replace this process with the MCP server."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp_1wrap.envfix import harden
from mcp_1wrap.errors import WrapError
from mcp_1wrap.fsutil import private_dir, runtime_base
from mcp_1wrap.op import DEFAULT_TIMEOUT, OpClient
from mcp_1wrap.profile import Profile, load_profile
from mcp_1wrap.providers import REGISTRY
from mcp_1wrap.providers.base import Context

DEFAULT_VAULT = "Private"
ALWAYS_UNSET = ("MCP_1WRAP_DEBUG", "MCP_1WRAP_PROFILE")


def note(message: str) -> None:
    print(f"mcp-1wrap: {message}", file=sys.stderr, flush=True)


@dataclass
class Prepared:
    profile: Profile
    command: str | None
    args: list[str]
    env: dict[str, str]
    summary: list[str] = field(default_factory=list)
    set_vars: list[str] = field(default_factory=list)


def resolve_command(cmd: str, path: str) -> tuple[str, str | None]:
    """Find the server command; a stale absolute path falls back to PATH."""
    if "/" in cmd and os.path.isfile(cmd) and os.access(cmd, os.X_OK):
        return cmd, None
    name = os.path.basename(cmd)
    found = shutil.which(name, path=path)
    if not found:
        raise WrapError(f"[command] nie znaleziono '{cmd}' ani '{name}' w PATH={path}")
    warning = None
    if "/" in cmd:
        warning = f"'{cmd}' nie istnieje, używam {found} (popraw ścieżkę w configu klienta)"
    return found, warning


def _debug_report(env: Mapping[str, str]) -> None:
    note(f"uid={os.getuid()} HOME={env.get('HOME', '<brak>')} "
         f"XDG_RUNTIME_DIR={env.get('XDG_RUNTIME_DIR', '<brak>')}")
    note(f"DBUS={env.get('DBUS_SESSION_BUS_ADDRESS', '<brak>')} PATH={env.get('PATH', '')}")
    for tool in ("op", "python3", "uvx"):
        note(f"{tool}={shutil.which(tool, path=env.get('PATH')) or '<brak>'}")


@dataclass
class Session:
    """What is decided once per process, before any provider runs."""

    profile: Profile
    command: str | None          # resolved server command (None only with --check)
    command_line: list[str]      # server command and its arguments, without '--'
    env: dict[str, str]          # hardened copy of the environment
    timeout: int                 # seconds per 'op' call


def setup(args: Any, environ: Mapping[str, str]) -> Session:
    """Harden the environment, find the server command and load the profile.

    ``environ`` is not modified. Nothing here talks to 1Password.
    """
    env = dict(environ)
    harden(env)
    debug = bool(args.debug or env.get("MCP_1WRAP_DEBUG"))
    if debug:
        _debug_report(env)

    command_line = list(args.command or [])
    if command_line and command_line[0] == "--":
        command_line = command_line[1:]
    if not command_line and not args.check:
        raise WrapError("[command] brak polecenia serwera po '--' (użycie: mcp-1wrap [opcje] -- polecenie [argumenty])")

    command = None
    if command_line:
        command, warning = resolve_command(command_line[0], env["PATH"])
        if warning:
            note(f"UWAGA: {warning}")

    profile = load_profile(
        args.profile or env.get("MCP_1WRAP_PROFILE") or None, args.profile_file, env
    )

    try:
        timeout = int(env.get("OP_TIMEOUT", "") or profile.timeout or DEFAULT_TIMEOUT)
    except ValueError:
        raise WrapError("[config] OP_TIMEOUT musi być liczbą sekund") from None

    return Session(profile, command, command_line, env, timeout)


def make_context(session: Session, overrides: Mapping[str, str] | None = None) -> Context:
    """The context providers run in: ``session.env`` plus ``overrides``."""
    env = dict(session.env)
    if overrides:
        env.update(overrides)

    runtime_dir: list[Path] = []

    def runtime() -> Path:
        if not runtime_dir:
            runtime_dir.append(private_dir(runtime_base(env, note) / "mcp-1wrap"))
        return runtime_dir[0]

    return Context(
        op=OpClient(env, session.timeout),
        vault=session.profile.vault or DEFAULT_VAULT,
        environ=env,
        runtime=runtime,
        note=note,
    )


def apply_providers(
    session: Session, overrides: Mapping[str, str] | None = None
) -> Prepared:
    """Run the profile's providers and build the server's environment.

    ``overrides`` are added to the environment the providers see (for example
    ``OS_CLOUD`` to pick one cloud); ``session`` is not modified.
    """
    profile = session.profile
    context = make_context(session, overrides)
    env = context.environ

    merged: dict[str, str] = {}
    summary: list[str] = []
    for spec in profile.providers:
        result = REGISTRY[spec.type].apply(context, spec.options)
        clash = sorted(set(merged) & set(result.env))
        if clash:
            raise WrapError(
                f"[profile {profile.name}] kilku dostawców ustawia tę samą zmienną: {', '.join(clash)}"
            )
        merged.update(result.env)
        summary.extend(result.summary)

    child = dict(env)
    for name in (*ALWAYS_UNSET, *profile.unset):
        child.pop(name, None)
    child.update(merged)

    return Prepared(
        profile=profile,
        command=session.command,
        args=session.command_line[1:],
        env=child,
        summary=summary,
        set_vars=sorted(merged),
    )


def prepare(args: Any, environ: Mapping[str, str]) -> Prepared:
    """Do all the work except the final exec. ``environ`` is not modified."""
    return apply_providers(setup(args, environ))


def execute(prepared: Prepared) -> None:
    """Replace this process with the server. Only returns by raising."""
    assert prepared.command is not None
    try:
        os.execve(prepared.command, [prepared.command, *prepared.args], prepared.env)
    except OSError as exc:
        raise WrapError(f"[exec] nie mogę uruchomić {prepared.command}: {exc}") from None
