"""The gateway as a real process: ``python -m mcp_1wrap`` with a [fanout] profile."""

import json
import os
import shlex
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import make_item
from mcp_client import McpClient, tool_text
from procutil import FAKE, events, pid_gone, wait_for

SRC = str(Path(__file__).resolve().parents[1] / "src")
PROFILE = (
    '[fanout]\nprobe_env = { VL_INSTANCE_ENTRYPOINT = "http://127.0.0.1:9" }\n'
    'inherit = ["FAKE_MCP_LOG", "FAKE_MCP_IGNORE_EOF"]\n'
    '[[provider]]\ntype = "item"\n[provider.vars]\n'
    'VL_INSTANCE_ENTRYPOINT = { field = "vlogs_url", check = "url" }\n'
)


def cloud(title, url="https://vlogs.example"):
    return make_item(title, extra={"vlogs_url": url} if url else {})


def seed(fake_op, *items):
    fake_op.set_db({"items": {"Private": list(items)}, "refs": {}})


def start_pids(log):
    return [e["pid"] for e in events(log) if e["event"] == "start"]


@pytest.fixture
def gateway(fake_op, tmp_path):
    started = []

    def start(command=None, reader=True, **env):
        profile = tmp_path / "p.toml"
        profile.write_text(PROFILE)
        log = tmp_path / "fake.jsonl"
        environ = fake_op.environ(PYTHONPATH=SRC, FAKE_MCP_LOG=str(log),
                                  OP_SERVICE_ACCOUNT_TOKEN="fake-token", **env)
        proc = subprocess.Popen(
            [sys.executable, "-m", "mcp_1wrap", "--profile-file", str(profile), "--",
             *(command or [sys.executable, FAKE])],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environ)
        started.append((proc, log))
        return proc, McpClient(proc.stdin, proc.stdout) if reader else None, log

    yield start
    for proc, log in started:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for pid in start_pids(log):               # a failing test must not leave servers behind
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass


def handshake(client):
    answer = client.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                           "clientInfo": {"name": "t", "version": "0"}})
    client.notify("notifications/initialized")
    return answer


def test_end_to_end(gateway, fake_op):
    seed(fake_op, cloud("EXAMPLE-CLOUD"), cloud("OTHER", "https://other.example"))
    proc, client, log = gateway()
    assert handshake(client)["result"]["capabilities"] == {"tools": {}}
    assert fake_op.calls() == []                          # 1Password is not asked before the first call

    tools = client.request("tools/list")["result"]["tools"]
    assert {"query", "echo"} <= {t["name"] for t in tools}
    assert all(t["inputSchema"]["required"][0] == "cloud" for t in tools)

    seen = json.loads(tool_text(client.call("echo", {"cloud": "example-cloud", "text": "hi"})))
    assert seen["entrypoint"] == "https://vlogs.example" and seen["arguments"] == {"text": "hi"}
    assert seen["op_token_visible"] is False and seen["leaked"] == []   # the backend's environment is allow-listed
    other = json.loads(tool_text(client.call("echo", {"cloud": "other"})))
    assert other["entrypoint"] == "https://other.example"

    # a backend that prints junk, a big reply and a backend error all reach the client intact
    assert tool_text(client.call("noisy", {"cloud": "other"})) == "noisy ok"
    assert len(tool_text(client.call("big", {"cloud": "other", "size": 200000}))) == 200000
    failure = client.call("fail", {"cloud": "other"}, rid=77)
    assert failure["id"] == 77 and failure["error"]["message"] == "bad arguments"
    unknown = client.call("echo", {"cloud": "nope"})
    assert unknown["result"]["isError"] is True and "Dostępne: EXAMPLE-CLOUD, OTHER" in tool_text(unknown)

    pids = start_pids(log)
    assert len(pids) == 3                                  # the probe and one backend per cloud
    client.close_input()
    assert proc.wait(timeout=20) == 0
    err = proc.stderr.read().decode()
    assert "this is not json" in err                       # kept off the client's stream
    assert "fanout: chmura='other' narzędzie=noisy wynik=ok" in err
    assert "fake-token" not in err and "s3cret-value" not in err
    assert wait_for(lambda: all(pid_gone(p) for p in pids))


def test_sigterm_ends_the_gateway_and_its_backends(gateway, fake_op):
    seed(fake_op, cloud("A"))
    proc, client, log = gateway()
    handshake(client)
    assert not client.call("echo", {"cloud": "a"})["result"].get("isError")
    pids = start_pids(log)
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=20) == 0
    assert wait_for(lambda: all(pid_gone(p) for p in pids))


def test_sigint_ends_the_gateway_and_its_backends(gateway, fake_op):
    seed(fake_op, cloud("A"))
    proc, client, log = gateway()
    handshake(client)
    assert not client.call("echo", {"cloud": "a"})["result"].get("isError")
    pids = start_pids(log)
    proc.send_signal(signal.SIGINT)
    assert proc.wait(timeout=20) == 130
    assert wait_for(lambda: all(pid_gone(p) for p in pids))


def test_a_client_that_stops_reading_ends_the_gateway(gateway, fake_op):
    seed(fake_op, cloud("A"))
    proc, _, _ = gateway(reader=False)
    proc.stdout.close()                                    # nobody reads the gateway's output any more
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode() + b"\n")
    proc.stdin.flush()
    assert proc.wait(timeout=20) == 0                      # stdin stayed open: only the write failure ended it


def test_killing_the_gateway_ends_the_backend_through_end_of_input(gateway, fake_op):
    seed(fake_op, cloud("A"))
    proc, client, log = gateway()
    handshake(client)
    client.call("echo", {"cloud": "a"})
    backend_pid = start_pids(log)[-1]
    proc.kill()                                            # no cleanup runs: only EOF can end the backend
    assert wait_for(lambda: pid_gone(backend_pid))


def test_shutdown_takes_down_a_backend_that_ignores_end_of_input(gateway, fake_op):
    seed(fake_op, cloud("A"))
    command = ["sh", "-c", f"{shlex.quote(sys.executable)} {shlex.quote(FAKE)}; true"]
    proc, client, log = gateway(command, FAKE_MCP_IGNORE_EOF="1")
    handshake(client)
    assert not client.call("echo", {"cloud": "a"})["result"].get("isError")
    pids = start_pids(log)
    client.close_input()
    assert proc.wait(timeout=20) == 0
    assert wait_for(lambda: all(pid_gone(p) for p in pids))


def test_a_missing_server_program_fails_before_any_protocol_traffic(gateway):
    proc, client, _ = gateway(["/no/such/program"])
    assert proc.wait(timeout=20) == 1
    assert proc.stdout.read() == b""
    assert b"[command] nie znaleziono" in proc.stderr.read()


def run_check(fake_op, tmp_path, profile_text=PROFILE, **env):
    profile = tmp_path / "p.toml"
    profile.write_text(profile_text)
    environ = fake_op.environ(PYTHONPATH=SRC, **env)
    return subprocess.run([sys.executable, "-m", "mcp_1wrap", "--check", "--profile-file", str(profile)],
                          capture_output=True, text=True, env=environ, timeout=60)


def test_check_succeeds_only_when_every_cloud_is_ready(fake_op, tmp_path):
    seed(fake_op, cloud("A", "https://a.example"), cloud("B", "https://b.example"))
    done = run_check(fake_op, tmp_path)
    assert done.returncode == 0, done.stderr
    assert "pokrycie: 2/2" in done.stdout


def test_check_only_looks_at_the_allowed_clouds(fake_op, tmp_path):
    seed(fake_op, cloud("A", "https://a.example"), cloud("B", None))
    profile = PROFILE.replace("[fanout]\n", '[fanout]\nallow_clouds = ["a"]\n')
    done = run_check(fake_op, tmp_path, profile)
    assert done.returncode == 0, done.stderr
    assert "chmury (1):" in done.stdout and "pokrycie: 1/1" in done.stdout and "  B:" not in done.stdout


def test_check_without_a_cloud_lister_fails_before_printing_a_report(fake_op, tmp_path):
    seed(fake_op, cloud("A"))
    profile = PROFILE.split("[[provider]]")[0] + '[[provider]]\ntype = "env"\n[provider.vars]\nX = "op://v/i/f"\n'
    done = run_check(fake_op, tmp_path, profile)
    assert done.returncode == 1 and done.stdout == ""
    assert "żaden dostawca profilu nie potrafi wypisać chmur" in done.stderr


def test_check_reports_the_coverage_of_every_cloud(fake_op, tmp_path):
    seed(fake_op, cloud("A", "https://a.example"), cloud("B", None), cloud("C", "http://insecure.example"))
    done = run_check(fake_op, tmp_path)
    assert done.returncode == 1, done.stderr              # not every cloud is ready
    out = done.stdout
    assert "tryb: fan-out (argument 'cloud')" in out and "chmury (3):" in out
    assert "  A: ok" in out and "VL_INSTANCE_ENTRYPOINT <= pole 'vlogs_url' = https://a.example" in out
    assert "  B: [item/'B'] brakuje pól: vlogs_url" in out
    assert "  C: [item/'C'] pole 'vlogs_url' (zmienna VL_INSTANCE_ENTRYPOINT) ma niedozwoloną wartość" in out
    assert "pokrycie: 1/3" in out
    assert "s3cret-value" not in out + done.stderr and "insecure.example" not in out


def test_check_with_a_cloud_checks_just_that_cloud(fake_op, tmp_path):
    seed(fake_op, cloud("A", "https://a.example"), cloud("B", "https://b.example"))
    done = run_check(fake_op, tmp_path, OS_CLOUD="b")
    assert done.returncode == 0, done.stderr
    assert "tryb: fan-out" in done.stdout and "= https://b.example" in done.stdout
    assert "a.example" not in done.stdout and "pokrycie" not in done.stdout
