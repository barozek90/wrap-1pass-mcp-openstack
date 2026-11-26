"""Command line entry point: ``mcp-1wrap [options] -- COMMAND [ARGS...]``."""

from __future__ import annotations

import argparse
import os
import sys

from mcp_1wrap import __version__, fanout
from mcp_1wrap.errors import WrapError
from mcp_1wrap.launcher import apply_providers, execute, setup


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-1wrap",
        description="Prepare secrets from 1Password and start an MCP server.",
    )
    parser.add_argument("--profile", help="profile name (~/.config/mcp-1wrap/profiles/NAME.toml); default: built-in 'openstack'")
    parser.add_argument("--profile-file", help="explicit path to a profile TOML file")
    parser.add_argument("--check", action="store_true",
                        help="prepare everything, print a summary (no secrets) and exit without starting the server")
    parser.add_argument("--debug", action="store_true", help="print environment diagnostics to stderr")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="server command and its arguments, after '--'")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        session = setup(args, os.environ)
        spec = session.profile.fanout
        if spec is not None:
            if not args.check:
                return fanout.run(session)
            if not session.env.get("OS_CLOUD", "").strip():
                return fanout.check(session)
        prepared = apply_providers(session)
        if args.check:
            print(f"profil: {prepared.profile.name} ({prepared.profile.source})")
            print(f"polecenie: {prepared.command or '<nie podano>'}")
            if spec is not None:
                print(f"tryb: fan-out (argument '{spec.argument}'); poniżej jedna chmura z OS_CLOUD")
            for line in prepared.summary:
                print(line)
            print(f"zmienne ustawiane dla serwera: {', '.join(prepared.set_vars) or '<brak>'}")
            return 0
        execute(prepared)
    except WrapError as exc:
        print(f"mcp-1wrap: BŁĄD {exc}", file=sys.stderr)
        return 1
    return 0
