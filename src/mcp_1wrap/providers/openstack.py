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
    verify_cert                         default: true (false = ignore TLS certificate)
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from mcp_1wrap.errors import WrapError
from mcp_1wrap.fsutil import private_dir, write_private_atomic
from mcp_1wrap.providers import items
from mcp_1wrap.providers.base import Context, Result

NAME = "openstack"
LIST_STAGE = f"{NAME}/list"
REQUIRED = ["website", "username", "password"]
DEFAULTS = {
    "interface": "public",
    "identity_api_version": "3",
    "user_domain_name": "Default",
    "project_name": "admin",
    "project_domain_name": "Default",
    "verify_cert": "true",
}
PROJECT_SCOPE_FIELDS = ["project_name", "project_domain_name"]
_OPTIONS = {"tag", "vault", "cloud", "cloud_name_var"}
_TRUE = {"true", "yes", "1"}
_FALSE = {"false", "no", "0"}


def _parse_bool(stage: str, name: str, value: Any) -> bool:
    text = str(value).strip().casefold()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise WrapError(
        f"[{stage}] pole '{name}' ma niedozwoloną wartość (dozwolone: true, false)"
    )


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

    verify = _parse_bool(stage, "verify_cert", fields.get("verify_cert", DEFAULTS["verify_cert"]))

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
        "verify": verify,
    }
    return cloud, {"auth": {"password": fields["password"]}}


def apply(ctx: Context, options: dict[str, Any]) -> Result:
    sel = items.selection(ctx, options)
    cloud_name_var = options.get("cloud_name_var", "CLOUD_NAME")

    entries = items.list_entries(ctx, LIST_STAGE, sel)
    clouds: dict[str, Any] = {}
    secure: dict[str, Any] = {}
    for title, entry in items.pick(entries, sel, LIST_STAGE, f"OS_CLOUD='{sel.wanted_raw}'"):
        item = items.get_item(ctx, f"{NAME}/item '{title}'", sel, entry)
        clouds[title], secure[title] = build_cloud(title, item)

    # Same inputs -> same directory and content, so concurrent launchers
    # (terminal + desktop client) can safely replace the files atomically.
    digest = hashlib.sha256(
        f"{sel.vault}|{sel.tag}|{sel.wanted or '*'}".encode()
    ).hexdigest()[:10]
    out = private_dir(ctx.runtime() / f"openstack-{digest}")
    write_private_atomic(out / "clouds.yaml", json.dumps({"clouds": clouds}, indent=2))
    write_private_atomic(out / "secure.yaml", json.dumps({"clouds": secure}, indent=2))

    result = Result(
        env={
            "OS_CLIENT_CONFIG_FILE": str(out / "clouds.yaml"),
            "OS_CLIENT_SECURE_FILE": str(out / "secure.yaml"),
        }
    )
    if sel.wanted:
        exact = next(iter(clouds))
        result.env["OS_CLOUD"] = exact
        if cloud_name_var:
            result.env[cloud_name_var] = exact
    result.summary.append(
        f"openstack: sejf '{sel.vault}', tag '{sel.tag}', "
        f"chmury ({len(clouds)}): {', '.join(clouds)}"
    )
    insecure = [name for name, cloud in clouds.items() if cloud["verify"] is False]
    if insecure:
        result.summary.append(
            f"openstack: UWAGA, weryfikacja certyfikatu TLS wyłączona dla: {', '.join(insecure)}"
        )
    return result
