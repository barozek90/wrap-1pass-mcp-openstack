"""``env`` provider: 1Password references -> environment variables.

    [[provider]]
    type = "env"
    [provider.vars]
    GITLAB_TOKEN = "op://Private/gitlab/token"
"""

from __future__ import annotations

import re
from typing import Any

from mcp_1wrap.errors import WrapError
from mcp_1wrap.providers.base import Context, Result

NAME = "env"
_VAR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate(options: dict[str, Any], where: str) -> None:
    unknown = set(options) - {"vars"}
    if unknown:
        raise WrapError(f"[{where}] nieznane opcje dostawcy 'env': {', '.join(sorted(unknown))}")
    variables = options.get("vars")
    if not isinstance(variables, dict) or not variables:
        raise WrapError(f"[{where}] dostawca 'env' wymaga niepustej tabeli [provider.vars]")
    for name, ref in variables.items():
        if not _VAR.match(name):
            raise WrapError(f"[{where}] niepoprawna nazwa zmiennej: {name!r}")
        if not isinstance(ref, str) or not ref.startswith("op://"):
            raise WrapError(f"[{where}] {name}: wartość musi być referencją 'op://...'")


def apply(ctx: Context, options: dict[str, Any]) -> Result:
    result = Result()
    for name, ref in sorted(options["vars"].items()):
        value = ctx.op.read(f"env/{name}", ref)
        if value == "":
            raise WrapError(f"[env/{name}] referencja {ref} zwróciła pustą wartość")
        result.env[name] = value
        result.summary.append(f"env: {name} <= {ref}")
    return result
