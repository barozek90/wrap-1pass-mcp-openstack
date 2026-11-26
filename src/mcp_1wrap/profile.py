"""Profiles: what to prepare before the MCP server starts.

A profile is a TOML file in ``~/.config/mcp-1wrap/profiles/<name>.toml``
(``$XDG_CONFIG_HOME`` is honoured). It holds vault/tag names and field
mappings, never secrets.

    vault   = "Private"        # default vault
    timeout = 60               # seconds per 'op' call
    unset   = ["OP_TAG"]       # variables not passed on to the server

    [[provider]]
    type = "openstack"
    tag  = "OS_TAG"

    [[provider]]
    type = "env"
    [provider.vars]
    GITLAB_TOKEN = "op://Private/gitlab/token"

    [fanout]                   # optional: one MCP server for many clouds
    probe_env = { SERVER_URL = "http://127.0.0.1:9" }
    # argument = "cloud", allow_clouds = [...], inherit = [...],
    # start_timeout = 30, call_timeout = 600
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
_TOP_KEYS = {"vault", "timeout", "unset", "provider", "fanout"}
_FANOUT_KEYS = {"argument", "probe_env", "allow_clouds", "inherit", "start_timeout", "call_timeout"}
_ARGUMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,31}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEFAULT_START_TIMEOUT = 30
DEFAULT_CALL_TIMEOUT = 600


@dataclass(frozen=True)
class ProviderSpec:
    type: str
    options: dict[str, Any]


@dataclass(frozen=True)
class FanoutSpec:
    """``[fanout]``: run as one MCP server for many clouds instead of exec."""

    probe_env: dict[str, str]              # environment of the schema probe
    argument: str = "cloud"                # extra required argument of every tool
    allow_clouds: tuple[str, ...] = ()     # when set, the only clouds accepted
    inherit: tuple[str, ...] = ()          # extra variables passed to backends
    start_timeout: int = DEFAULT_START_TIMEOUT
    call_timeout: int = DEFAULT_CALL_TIMEOUT


@dataclass(frozen=True)
class Profile:
    name: str
    source: str
    vault: str | None
    timeout: int | None
    unset: tuple[str, ...]
    providers: tuple[ProviderSpec, ...]
    fanout: FanoutSpec | None = None


BUILTIN_OPENSTACK = Profile(
    name="openstack",
    source="wbudowany",
    vault=None,
    timeout=None,
    unset=("OP_TAG",),
    providers=(ProviderSpec("openstack", {}),),
)


def profiles_dir(environ: Mapping[str, str]) -> Path:
    xdg = environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg) if xdg else Path(environ.get("HOME") or "~").expanduser() / ".config"
    return base / "mcp-1wrap" / "profiles"


def _positive_int(place: str, raw: dict[str, Any], key: str, default: int, maximum: int) -> int:
    value = raw.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or not 0 < value <= maximum:
        raise WrapError(f"[{place}] '{key}' musi być dodatnią liczbą sekund (najwyżej {maximum})")
    return value


def _name_list(place: str, raw: dict[str, Any], key: str, *, env_names: bool) -> tuple[str, ...]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise WrapError(f"[{place}] '{key}' musi być listą tekstów")
    for entry in value:
        if env_names and not _ENV_NAME.match(entry):
            raise WrapError(f"[{place}] '{key}': niepoprawna nazwa zmiennej {entry!r}")
        if not env_names and (
            not entry.strip() or len(entry) > 128 or any(ord(c) < 32 or ord(c) == 127 for c in entry)
        ):
            raise WrapError(f"[{place}] '{key}': niepoprawna nazwa chmury {entry[:40]!r}")
    return tuple(value)


def _parse_fanout(where: str, raw: Any) -> FanoutSpec:
    place = f"{where}, fanout"
    if not isinstance(raw, dict):
        raise WrapError(f"[{place}] 'fanout' musi być tabelą")
    unknown = set(raw) - _FANOUT_KEYS
    if unknown:
        raise WrapError(f"[{place}] nieznane klucze: {', '.join(sorted(unknown))}")
    argument = raw.get("argument", "cloud")
    if not isinstance(argument, str) or not _ARGUMENT.match(argument):
        raise WrapError(f"[{place}] 'argument' musi być nazwą (litery, cyfry, '_', do 32 znaków)")
    probe = raw.get("probe_env")
    if not isinstance(probe, dict) or not probe:
        raise WrapError(f"[{place}] 'probe_env' jest wymagane: niepusta tabela ZMIENNA = \"wartość\"")
    for key, value in probe.items():
        if not _ENV_NAME.match(key):
            raise WrapError(f"[{place}] 'probe_env': niepoprawna nazwa zmiennej {key!r}")
        if not isinstance(value, str) or "\x00" in value:
            raise WrapError(f"[{place}] 'probe_env': wartość {key} musi być tekstem")
    return FanoutSpec(
        probe_env=dict(probe),
        argument=argument,
        allow_clouds=_name_list(place, raw, "allow_clouds", env_names=False),
        inherit=_name_list(place, raw, "inherit", env_names=True),
        start_timeout=_positive_int(place, raw, "start_timeout", DEFAULT_START_TIMEOUT, 3600),
        call_timeout=_positive_int(place, raw, "call_timeout", DEFAULT_CALL_TIMEOUT, 86400),
    )


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

    fanout = _parse_fanout(where, data["fanout"]) if "fanout" in data else None
    return Profile(name, str(path), vault, timeout, tuple(unset), tuple(specs), fanout)


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
