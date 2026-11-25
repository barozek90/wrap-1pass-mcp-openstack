import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import make_item
from mcp_1wrap.errors import WrapError
from mcp_1wrap.launcher import prepare, resolve_command

SRC = str(Path(__file__).resolve().parents[1] / "src")
DUMP = "import os, json; print(json.dumps(sorted(k for k in os.environ if k.startswith(('OS_','CLOUD_NAME','MCP_1WRAP','FAKE')))))"


def test_resolve_existing_absolute_path():
    assert resolve_command("/usr/bin/env", "/usr/bin:/bin") == ("/usr/bin/env", None)


def test_resolve_bare_name_from_path():
    found, warning = resolve_command("env", "/usr/bin:/bin")
    assert found.endswith("/env") and warning is None


def test_resolve_stale_absolute_path_falls_back_with_warning():
    found, warning = resolve_command("/home/nobody/.local/bin/env", "/usr/bin:/bin")
    assert found.endswith("/env") and "nie istnieje" in warning


def test_resolve_missing_command():
    with pytest.raises(WrapError, match=r"\[command\] nie znaleziono '/x/nope' ani 'nope'"):
        resolve_command("/x/nope", "/usr/bin:/bin")


def test_missing_command_fails_before_touching_1password(fake_op, args):
    with pytest.raises(WrapError, match=r"\[command\]"):
        prepare(args(command=["--", "/x/definitely-missing"]), fake_op.environ())
    assert fake_op.calls() == []


def test_no_command_is_an_error_unless_check(fake_op, args):
    fake_op.set_db({"items": {"Private": [make_item("A")]}, "refs": {}})
    with pytest.raises(WrapError, match="brak polecenia serwera"):
        prepare(args(command=[]), fake_op.environ())
    assert prepare(args(command=[], check=True), fake_op.environ()).command is None


def test_helper_variables_are_not_passed_to_the_server(fake_op, args):
    fake_op.set_db({"items": {"Private": [make_item("A")]}, "refs": {}})
    env = fake_op.environ(OS_TAG="OS_TAG", MCP_1WRAP_DEBUG="1", MCP_1WRAP_PROFILE="openstack")
    prep = prepare(args(), env)
    for name in ("OS_TAG", "MCP_1WRAP_DEBUG", "MCP_1WRAP_PROFILE"):
        assert name not in prep.env
    assert env["OS_TAG"] == "OS_TAG"  # the caller's mapping is untouched


def test_stale_path_is_repaired_for_the_server(fake_op, args):
    fake_op.set_db({"items": {"Private": [make_item("A")]}, "refs": {}})
    env = fake_op.environ()
    env["PATH"] = str(fake_op.bin)  # thin GUI-like PATH
    prep = prepare(args(), env)
    assert "/usr/bin" in prep.env["PATH"].split(":")


def run_cli(fake_op, *cli_args, extra_env=None):
    env = fake_op.environ(PYTHONPATH=SRC, **(extra_env or {}))
    return subprocess.run([sys.executable, "-m", "mcp_1wrap", *cli_args],
                          capture_output=True, text=True, env=env, timeout=60)


def test_end_to_end_exec_has_clean_stdout(fake_op):
    fake_op.set_db({"items": {"Private": [make_item("EXAMPLE-CLOUD")]}, "refs": {}})
    done = run_cli(fake_op, "--", sys.executable, "-c", DUMP,
                   extra_env={"OS_CLOUD": "example-cloud", "OS_TAG": "OS_TAG"})
    assert done.returncode == 0, done.stderr
    # stdout is exactly what the server printed: nothing from the launcher
    assert json.loads(done.stdout) == ["CLOUD_NAME", "FAKE_OP_DB", "FAKE_OP_LOG",
                                       "OS_CLIENT_CONFIG_FILE", "OS_CLIENT_SECURE_FILE", "OS_CLOUD"]


def test_end_to_end_error_goes_to_stderr_with_exit_1(fake_op):
    done = run_cli(fake_op, "--", sys.executable, "-c", "print('should not run')")
    assert done.returncode == 1
    assert done.stdout == ""
    assert "mcp-1wrap: BŁĄD [openstack/list] brak itemów z tagiem 'OS_TAG' w sejfie 'Private'" in done.stderr


def test_check_mode_prints_summary_without_secrets(fake_op):
    fake_op.set_db({"items": {"Private": [make_item("A"), make_item("B")]}, "refs": {}})
    done = run_cli(fake_op, "--check")
    assert done.returncode == 0, done.stderr
    assert "profil: openstack (wbudowany)" in done.stdout
    assert "chmury (2): A, B" in done.stdout
    assert "OS_CLIENT_CONFIG_FILE" in done.stdout
    assert "s3cret-value" not in done.stdout + done.stderr


def test_debug_report_goes_to_stderr(fake_op):
    fake_op.set_db({"items": {"Private": [make_item("A")]}, "refs": {}})
    done = run_cli(fake_op, "--debug", "--check")
    assert "mcp-1wrap: uid=" in done.stderr and "op=" in done.stderr
    assert "mcp-1wrap:" not in done.stdout
