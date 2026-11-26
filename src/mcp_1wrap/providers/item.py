"""``item`` provider: fields of one cloud's 1Password item -> environment variables.

The item is found like in the ``openstack`` provider (vault, tag, cloud name),
but only the fields named in ``vars`` are kept; nothing else from the item is
used, printed or put in an error message.

    [[provider]]
    type = "item"
    tag  = "OS_TAG"
    # allow_hosts = ["*.intra.example"]   # hosts allowed for check = "url"
    # allow_http  = true                  # http for more than loopback
    [provider.vars]
    VL_INSTANCE_ENTRYPOINT = { field = "vlogs_url", check = "url" }
    SOME_NOTE = "note"                    # plain form: check = "text"

``check = "url"`` accepts only a plain endpoint address (see ``check_url``).
Exactly one cloud is served at a time: ``OS_CLOUD`` or the ``cloud`` option.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import re
from collections.abc import Sequence
from typing import Any
from urllib.parse import unquote, urlsplit

from mcp_1wrap.errors import WrapError
from mcp_1wrap.providers import items
from mcp_1wrap.providers.base import Context, Result

NAME = "item"
LIST_STAGE = f"{NAME}/list"
MAX_VALUE = 2048
_OPTIONS = {"tag", "vault", "cloud", "vars", "allow_hosts", "allow_http"}
_STRING_OPTIONS = ("tag", "vault", "cloud")
_VAR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CHECKS = ("text", "url")
_HOST = re.compile(r"^[A-Za-z0-9._-]+$")
_NUMERIC_HOST = re.compile(r"^(?:[0-9.]+|0[xX][0-9a-fA-F.]+)$")
_METADATA_HOSTS = {"metadata.google.internal", "metadata.goog", "instance-data",
                   "instance-data.ec2.internal"}
_AWS_IMDS_V6 = ipaddress.ip_network("fd00:ec2::/32")
_NAT64 = bytes.fromhex("0064ff9b" + "00" * 8)          # 64:ff9b::/96
_SIIT = bytes.fromhex("0000000000000000ffff0000")     # ::ffff:0:0:0/96


def _parse_var(where: str, name: str, spec: Any) -> tuple[str, str]:
    """``(label, check)`` of one ``vars`` entry, or a ``WrapError``."""
    if isinstance(spec, str):
        label, check = spec, "text"
    elif isinstance(spec, dict):
        unknown = set(spec) - {"field", "check"}
        if unknown:
            raise WrapError(f"[{where}] {name}: nieznane klucze: {', '.join(sorted(unknown))}")
        label, check = spec.get("field"), spec.get("check", "text")
        if not isinstance(label, str):
            raise WrapError(f"[{where}] {name}: brak klucza 'field' (etykieta pola itemu)")
        if check not in _CHECKS:
            raise WrapError(f"[{where}] {name}: 'check' musi być jednym z: {', '.join(_CHECKS)}")
    else:
        raise WrapError(f"[{where}] {name}: wartość musi być etykietą pola albo tabelą {{ field = ... }}")
    if not label.strip():
        raise WrapError(f"[{where}] {name}: etykieta pola jest pusta")
    return label.strip(), check


def validate(options: dict[str, Any], where: str) -> None:
    unknown = set(options) - _OPTIONS
    if unknown:
        raise WrapError(f"[{where}] nieznane opcje dostawcy 'item': {', '.join(sorted(unknown))}")
    for key in _STRING_OPTIONS:
        if key in options and not isinstance(options[key], str):
            raise WrapError(f"[{where}] opcja '{key}' musi być tekstem")
    variables = options.get("vars")
    if not isinstance(variables, dict) or not variables:
        raise WrapError(f"[{where}] dostawca 'item' wymaga niepustej tabeli [provider.vars]")
    for name, spec in variables.items():
        if not _VAR.match(name):
            raise WrapError(f"[{where}] niepoprawna nazwa zmiennej: {name!r}")
        _parse_var(where, name, spec)
    hosts = options.get("allow_hosts", [])
    if not isinstance(hosts, list) or not all(isinstance(h, str) and h.strip() for h in hosts):
        raise WrapError(f"[{where}] 'allow_hosts' musi być listą niepustych wzorców")
    if not isinstance(options.get("allow_http", False), bool):
        raise WrapError(f"[{where}] 'allow_http' musi być wartością true albo false")


def _ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _embedded_ipv4(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    """IPv4 addresses an IPv6 one carries: mapped, 6to4, Teredo, NAT64 and SIIT forms."""
    if not isinstance(address, ipaddress.IPv6Address):
        return []
    found = [a for a in (address.ipv4_mapped, address.sixtofour) if a is not None]
    if address.teredo:
        found.extend(address.teredo)
    packed = address.packed
    if packed[:12] in (_NAT64, _SIIT):
        found.append(ipaddress.IPv4Address(packed[12:]))
    return found


def _blocked(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Link-local, unspecified or multicast (also when hidden in IPv6) and the AWS IPv6 metadata range."""
    if any(a.is_link_local or a.is_unspecified or a.is_multicast
           for a in (address, *_embedded_ipv4(address))):
        return True
    return isinstance(address, ipaddress.IPv6Address) and address in _AWS_IMDS_V6


def check_url(text: str, *, allow_http: bool = False, allow_hosts: Sequence[str] = ()) -> str | None:
    """Why ``text`` is not an acceptable endpoint address, or ``None`` if it is.

    Accepted: ``https://host[:port][/path]`` (``http`` only for loopback unless
    ``allow_http``). Rejected: credentials, query, fragment, ``..`` segments
    (also percent-encoded), non-ASCII or numeric-trick hosts, a trailing dot,
    link-local/unspecified/multicast IPs (also inside IPv6), well-known metadata
    host names and, when ``allow_hosts`` is given, hosts that match none of its
    patterns. DNS names that resolve to such addresses are only caught by
    ``allow_hosts``.
    """
    if not text.isascii():
        return "adres musi zawierać wyłącznie znaki ASCII (domeny IDN w formie punycode)"
    if any(ch.isspace() for ch in text) or "\\" in text:
        return "adres zawiera białe znaki lub odwrotny ukośnik"
    if "?" in text or "#" in text:
        return "adres nie może zawierać zapytania ani fragmentu"
    try:
        parts = urlsplit(text)
        port = parts.port    # also validates the port range
    except ValueError:
        return "adres jest niepoprawny"
    if parts.scheme not in ("http", "https"):
        return "dozwolony jest tylko schemat https (http wyłącznie dla loopback)"
    host = parts.hostname
    if not host:
        return "brak hosta w adresie"
    if parts.netloc.endswith(":") or port == 0:
        return "port w adresie jest niepoprawny"
    if parts.username is not None or parts.password is not None:
        return "adres nie może zawierać danych logowania"
    path = unquote(parts.path)
    if "\\" in path or any(segment.split(";")[0] == ".." for segment in path.split("/")):
        return "ścieżka nie może zawierać '..' (także zakodowanego)"

    address = _ip(host)
    if address is None:
        if host.endswith("."):
            return "host nie może kończyć się kropką"
        if not _HOST.match(host):
            return "host zawiera niedozwolone znaki"
        if _NUMERIC_HOST.match(host):
            return "zapis liczbowy hosta (poza standardowym a.b.c.d) jest niedozwolony"
        if host in _METADATA_HOSTS:
            return "host usługi metadanych jest niedozwolony"
    elif _blocked(address):
        return "adres IP link-local, nieokreślony lub multicast jest niedozwolony"

    loopback = host == "localhost" or (address is not None and address.is_loopback)
    if parts.scheme == "http" and not loopback and not allow_http:
        return "http jest dozwolone tylko dla loopback (opcja 'allow_http')"
    if allow_hosts and not any(fnmatch.fnmatchcase(host, p.strip().lower()) for p in allow_hosts):
        return "host spoza listy 'allow_hosts'"
    return None


def _text_problem(text: str) -> str | None:
    if len(text) > MAX_VALUE:
        return f"wartość jest dłuższa niż {MAX_VALUE} znaków"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        return "wartość zawiera znaki sterujące"
    return None


def names(ctx: Context, options: dict[str, Any]) -> list[str]:
    """Titles of every item the selection could serve (the cloud names)."""
    entries = items.list_entries(ctx, LIST_STAGE, items.selection(ctx, options))
    return sorted({e["title"].strip() for e in entries}, key=lambda t: (t.casefold(), t))


def apply(ctx: Context, options: dict[str, Any]) -> Result:
    specs = {
        name: _parse_var(NAME, name, spec) for name, spec in sorted(options["vars"].items())
    }
    allow_hosts = tuple(h.strip().lower() for h in options.get("allow_hosts", []))
    allow_http = bool(options.get("allow_http", False))

    sel = items.selection(ctx, options)
    entries = items.list_entries(ctx, LIST_STAGE, sel)
    if sel.wanted is None:
        available = ", ".join(sorted({e["title"].strip() for e in entries}))
        raise WrapError(
            f"[{LIST_STAGE}] nie wskazano chmury (zmienna OS_CLOUD albo opcja 'cloud'); "
            f"dostępne: {available}"
        )
    title, entry = items.pick(entries, sel, LIST_STAGE, f"chmury '{sel.wanted_raw}'")[0]
    stage = f"{NAME}/'{title}'"
    values = items.field_values(items.get_item(ctx, stage, sel, entry))

    texts = {
        name: str(values.get(label.casefold(), "")).strip() for name, (label, _) in specs.items()
    }
    missing = [label for name, (label, _) in specs.items() if not texts[name]]
    if missing:
        raise WrapError(f"[{stage}] brakuje pól: {', '.join(dict.fromkeys(missing))}")

    result = Result()
    for name, (label, check) in specs.items():
        text = texts[name]
        problem = _text_problem(text)
        if problem is None and check == "url":
            problem = check_url(text, allow_http=allow_http, allow_hosts=allow_hosts)
        if problem:
            raise WrapError(
                f"[{stage}] pole '{label}' (zmienna {name}) ma niedozwoloną wartość: {problem}"
            )
        result.env[name] = text
        # A validated URL has no userinfo or query, so it is shown (--check, summary);
        # its path could still hold anything, which is why tokens never belong there.
        shown = f" = {text}" if check == "url" else ""
        result.summary.append(f"item: chmura '{title}': {name} <= pole '{label}'{shown}")
    return result
