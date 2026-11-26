"""Shared selection of the 1Password items that stand for clouds.

Used by the ``openstack`` and ``item`` providers. Items are found by vault and
tag; the item title is the cloud name and is matched case-insensitively against
the requested cloud (``OS_CLOUD`` or the ``cloud`` profile option).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcp_1wrap.errors import WrapError
from mcp_1wrap.providers.base import Context

DEFAULT_TAG = "OS_TAG"


@dataclass(frozen=True)
class Selection:
    vault: str
    tag: str
    wanted: str | None   # casefolded cloud name; None selects every tagged item
    wanted_raw: str      # the requested name as given, for messages


def selection(ctx: Context, options: dict[str, Any]) -> Selection:
    """Environment (OP_VAULT, OP_TAG, OS_CLOUD) wins over profile options."""
    env = ctx.environ
    vault = env.get("OP_VAULT", "").strip() or options.get("vault") or ctx.vault
    tag = env.get("OP_TAG", "").strip() or options.get("tag") or DEFAULT_TAG
    wanted_raw = env.get("OS_CLOUD", "").strip() or options.get("cloud", "")
    wanted = wanted_raw.strip().casefold() or None
    return Selection(vault, tag, wanted, wanted_raw)


def list_entries(ctx: Context, stage: str, sel: Selection) -> list[dict[str, Any]]:
    """``op item list`` for the vault and tag; an empty result is an error."""
    entries = ctx.op.json(stage, "item", "list", "--vault", sel.vault, "--tags", sel.tag)
    if not entries:
        raise WrapError(f"[{stage}] brak itemów z tagiem '{sel.tag}' w sejfie '{sel.vault}'")
    return entries


def pick(
    entries: list[dict[str, Any]], sel: Selection, stage: str, requested: str
) -> list[tuple[str, dict[str, Any]]]:
    """The ``(title, entry)`` pairs chosen by ``sel``, in listing order.

    Titles that differ only in case are an error, but only among the entries
    that pass the ``wanted`` filter. ``requested`` names the request in the
    "no match" message.
    """
    picked: list[tuple[str, dict[str, Any]]] = []
    seen: dict[str, str] = {}
    for entry in entries:
        title = entry["title"].strip()
        folded = title.casefold()
        if sel.wanted and folded != sel.wanted:
            continue
        if folded in seen:
            raise WrapError(
                f"[{stage}] zduplikowana nazwa chmury '{title}' (koliduje z "
                f"'{seen[folded]}', porównanie bez rozróżniania wielkości liter)"
            )
        seen[folded] = title
        picked.append((title, entry))

    if not picked:
        available = ", ".join(sorted(e["title"] for e in entries))
        raise WrapError(
            f"[{stage}] żaden item z tagiem '{sel.tag}' nie pasuje do "
            f"{requested}. Dostępne: {available}"
        )
    return picked


def get_item(ctx: Context, stage: str, sel: Selection, entry: dict[str, Any]) -> dict[str, Any]:
    """``op item get`` for one listed entry."""
    return ctx.op.json(stage, "item", "get", entry["id"], "--vault", sel.vault)


def field_values(item: dict[str, Any]) -> dict[str, Any]:
    """Casefolded field label -> value for the fields that have both."""
    values: dict[str, Any] = {}
    for field in item.get("fields", []):
        label = (field.get("label") or "").strip().casefold()
        if label and field.get("value") not in (None, ""):
            values.setdefault(label, field["value"])
    return values
