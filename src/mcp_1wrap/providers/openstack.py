"""``openstack`` provider: one 1Password item per cloud -> clouds.yaml + secure.yaml.

Items are found by vault and tag. The item title is the cloud name (copied 1:1,
matched case-insensitively against ``OS_CLOUD``). Fields (labels are matched
case-insensitively):

    website (URL)                       required, Keystone auth URL
    username, password                  required
    region_name                         default: item title
    user_domain_name                    default: Default
    project_name, project_domain_name   defaults: admin, Default
    system_scope = all                  system scope (excludes project_* fields)
    interface                           default: public
    identity_api_version                default: 3
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from mcp_1wrap.errors import WrapError
from mcp_1wrap.fsutil import private_dir, write_private_atomic
from mcp_1wrap.providers.base import Context, Result

NAME = "openstack"
DEFAULT_TAG = "OS_TAG"
REQUIRED = ["website", "username", "password"]
DEFAULTS = {
    "interface": "public",
    "identity_api_version": "3",
    "user_domain_name": "Default",
    "project_name": "admin",
    "project_domain_name": "Default",
}
PROJECT_SCOPE_FIELDS = ["project_name", "project_domain_name"]
_OPTIONS = {"tag", "vault", "cloud", "cloud_name_var"}


def validate(options: dict[str, Any], where: str) -> None:
    unknown = set(options) - _OPTIONS
    if unknown:
        raise WrapError(f"[{where}] nieznane opcje dostawcy 'openstack': {', '.join(sorted(unknown))}")
    for key, value in options.items():
        if not isinstance(value, str):
            raise WrapError(f"[{where}] opcja '{key}' musi być tekstem")


def build_cloud(title: str, item: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Turn one 1Password item into (clouds.yaml entry, secure.yaml entry)."""
    stage = f"openstack/item '{title}'"

    fields: dict[str, Any] = {}
    for field in item.get("fields", []):
        label = (field.get("label") or "").strip().casefold()
        if label and field.get("value") not in (None, ""):
            fields.setdefault(label, field["value"])

    urls = item.get("urls") or []
    website = next(
        (u["href"] for u in urls if (u.get("label") or "").casefold() == "website"), None
    ) or next((u["href"] for u in urls if u.get("primary")), None)
    if website:
        fields["website"] = website

    missing = [name for name in REQUIRED if name not in fields]
    if missing:
        raise WrapError(f"[{stage}] brakuje wymaganych pól: {', '.join(missing)}")

    api_version = fields.get("identity_api_version", DEFAULTS["identity_api_version"])
    try:
        api_version = int(api_version)
    except (TypeError, ValueError):
        raise WrapError(
            f"[{stage}] pole 'identity_api_version' nie jest liczbą: {api_version!r}"
        ) from None

    auth: dict[str, Any] = {
        "auth_url": fields["website"],
        "username": fields["username"],
        "user_domain_name": fields.get("user_domain_name", DEFAULTS["user_domain_name"]),
    }

    system_scope = fields.get("system_scope")
    if system_scope is not None:
        if str(system_scope).strip().casefold() != "all":
            raise WrapError(
                f"[{stage}] pole 'system_scope' ma niedozwoloną wartość "
                f"{system_scope!r} (dozwolone: all)"
            )
        conflicting = [name for name in PROJECT_SCOPE_FIELDS if name in fields]
        if conflicting:
            raise WrapError(f"[{stage}] 'system_scope' wyklucza pola: {', '.join(conflicting)}")
        auth["system_scope"] = "all"
    else:
        for name in PROJECT_SCOPE_FIELDS:
            auth[name] = fields.get(name, DEFAULTS[name])

    cloud = {
        "auth": auth,
        "region_name": fields.get("region_name", title),
        "interface": fields.get("interface", DEFAULTS["interface"]),
        "identity_api_version": api_version,
    }
    return cloud, {"auth": {"password": fields["password"]}}


def apply(ctx: Context, options: dict[str, Any]) -> Result:
    env = ctx.environ
    vault = env.get("OP_VAULT", "").strip() or options.get("vault") or ctx.vault
    tag = env.get("OS_TAG", "").strip() or options.get("tag") or DEFAULT_TAG
    wanted_raw = env.get("OS_CLOUD", "").strip() or options.get("cloud", "")
    wanted = wanted_raw.strip().casefold() or None
    cloud_name_var = options.get("cloud_name_var", "CLOUD_NAME")

    items = ctx.op.json("openstack/list", "item", "list", "--vault", vault, "--tags", tag)
    if not items:
        raise WrapError(f"[openstack/list] brak itemów z tagiem '{tag}' w sejfie '{vault}'")

    clouds: dict[str, Any] = {}
    secure: dict[str, Any] = {}
    seen: dict[str, str] = {}
    for entry in items:
        title = entry["title"].strip()
        folded = title.casefold()
        if wanted and folded != wanted:
            continue
        if folded in seen:
            raise WrapError(
                f"[openstack/list] zduplikowana nazwa chmury '{title}' (koliduje z "
                f"'{seen[folded]}', porównanie bez rozróżniania wielkości liter)"
            )
        seen[folded] = title
        item = ctx.op.json(
            f"openstack/item '{title}'", "item", "get", entry["id"], "--vault", vault
        )
        clouds[title], secure[title] = build_cloud(title, item)

    if not clouds:
        available = ", ".join(sorted(e["title"] for e in items))
        raise WrapError(
            f"[openstack/list] żaden item z tagiem '{tag}' nie pasuje do "
            f"OS_CLOUD='{wanted_raw}'. Dostępne: {available}"
        )

    # Same inputs -> same directory and content, so concurrent launchers
    # (terminal + desktop client) can safely replace the files atomically.
    digest = hashlib.sha256(f"{vault}|{tag}|{wanted or '*'}".encode()).hexdigest()[:10]
    out = private_dir(ctx.runtime() / f"openstack-{digest}")
    write_private_atomic(out / "clouds.yaml", json.dumps({"clouds": clouds}, indent=2))
    write_private_atomic(out / "secure.yaml", json.dumps({"clouds": secure}, indent=2))

    result = Result(
        env={
            "OS_CLIENT_CONFIG_FILE": str(out / "clouds.yaml"),
            "OS_CLIENT_SECURE_FILE": str(out / "secure.yaml"),
        }
    )
    if wanted:
        exact = next(iter(clouds))
        result.env["OS_CLOUD"] = exact
        if cloud_name_var:
            result.env[cloud_name_var] = exact
    result.summary.append(
        f"openstack: sejf '{vault}', tag '{tag}', chmury ({len(clouds)}): {', '.join(clouds)}"
    )
    return result
