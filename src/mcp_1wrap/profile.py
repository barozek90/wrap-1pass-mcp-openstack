"""Profiles: what to prepare before the MCP server starts.

A profile is a TOML file in ``~/.config/mcp-1wrap/profiles/<name>.toml``
(``$XDG_CONFIG_HOME`` is honoured). It holds vault/tag names and field
mappings, never secrets.

    vault   = "Private"        # default vault
    timeout = 60               # seconds per 'op' call
    unset   = ["OS_TAG"]       # variables not passed on to the server

    [[provider]]
    type = "openstack"
    tag  = "OS_TAG"

    [[provider]]
    type = "env"
    [provider.vars]
    GITLAB_TOKEN = "op://Private/gitlab/token"
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp_1wrap.errors import WrapError
from mcp_1wrap.providers import REGISTRY

_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_TOP_KEYS = {"vault", "timeout", "unset", "provider"}


@dataclass(frozen=True)
class ProviderSpec:
    type: str
    options: dict[str, Any]


@dataclass(frozen=True)
class Profile:
    name: str
    source: str
    vault: str | None
    timeout: int | None
    unset: tuple[str, ...]
    providers: tuple[ProviderSpec, ...]


BUILTIN_OPENSTACK = Profile(
    name="openstack",
    source="wbudowany",
    vault=None,
    timeout=None,
    unset=("OS_TAG",),
    providers=(ProviderSpec("openstack", {}),),
)


def profiles_dir(environ: Mapping[str, str]) -> Path:
    xdg = environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg) if xdg else Path(environ.get("HOME") or "~").expanduser() / ".config"
    return base / "mcp-1wrap" / "profiles"


def _parse(path: Path, name: str) -> Profile:
    where = f"profile {path}"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise WrapError(f"[{where}] nie mogę odczytać pliku: {exc}") from None
    except tomllib.TOMLDecodeError as exc:
        raise WrapError(f"[{where}] niepoprawny TOML: {exc}") from None

    unknown = set(data) - _TOP_KEYS
    if unknown:
        raise WrapError(f"[{where}] nieznane klucze: {', '.join(sorted(unknown))}")

    vault = data.get("vault")
    if vault is not None and not isinstance(vault, str):
        raise WrapError(f"[{where}] 'vault' musi być tekstem")
    timeout = data.get("timeout")
    if timeout is not None and (not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0):
        raise WrapError(f"[{where}] 'timeout' musi być dodatnią liczbą sekund")
    unset = data.get("unset", [])
    if not isinstance(unset, list) or not all(isinstance(x, str) for x in unset):
        raise WrapError(f"[{where}] 'unset' musi być listą nazw zmiennych")

    specs = []
    entries = data.get("provider", [])
    if not isinstance(entries, list) or not entries:
        raise WrapError(f"[{where}] brak sekcji [[provider]]")
    for index, entry in enumerate(entries, start=1):
        options = dict(entry)
        kind = options.pop("type", None)
        place = f"{where}, provider #{index}"
        if kind not in REGISTRY:
            raise WrapError(
                f"[{place}] nieznany typ {kind!r} (dostępne: {', '.join(sorted(REGISTRY))})"
            )
        REGISTRY[kind].validate(options, place)
        specs.append(ProviderSpec(kind, options))

    return Profile(name, str(path), vault, timeout, tuple(unset), tuple(specs))


def load_profile(name: str | None, file: str | None, environ: Mapping[str, str]) -> Profile:
    if file:
        path = Path(file).expanduser()
        return _parse(path, path.stem)
    if not name:
        return BUILTIN_OPENSTACK
    if not _NAME.match(name):
        raise WrapError(f"[profile] niepoprawna nazwa profilu: {name!r}")
    path = profiles_dir(environ) / f"{name}.toml"
    if path.is_file():
        return _parse(path, name)
    if name == BUILTIN_OPENSTACK.name:
        return BUILTIN_OPENSTACK
    directory = profiles_dir(environ)
    available = sorted(p.stem for p in directory.glob("*.toml")) if directory.is_dir() else []
    available = sorted({*available, BUILTIN_OPENSTACK.name})
    raise WrapError(
        f"[profile] nie znaleziono profilu '{name}' w {directory} "
        f"(dostępne: {', '.join(available)})"
    )
