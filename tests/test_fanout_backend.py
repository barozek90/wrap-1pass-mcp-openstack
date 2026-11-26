"""The real ``Backend`` against ``tests/fake_mcp_server.py`` (small subprocess tests)."""

import json
import os
import shlex
import sys
import threading
import time

import pytest

from mcp_1wrap import fanout
from mcp_1wrap.fanout import Backend, BackendError, BackendGone, CallTimeout, Cancelled
from procutil import FAKE, events, pid_gone, wait_for


def make_backend(tmp_path, command=None, args=None, **env):
    log = tmp_path / "fake.jsonl"
    full = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "FAKE_MCP_LOG": str(log),
            "VL_INSTANCE_ENTRYPOINT": "https://vlogs.example", **env}
    return Backend("t", command or sys.executable, args if args is not None else [FAKE], full), log


def text_of(reply):
    return reply["result"]["content"][0]["text"]


@pytest.fixture
def backend(tmp_path):
    made = []

    def build(**kwargs):
        b, log = make_backend(tmp_path, **kwargs)
        made.append(b)
        return b, log

    yield build
    for b in made:
        b.close(0.3)


def test_handshake_and_tool_list(backend):
    b, log = backend()
    b.start(10)
    assert b.alive()
    names = [t["name"] for t in b.list_tools(10)]
    assert "query" in names and "echo" in names
    methods = [e["method"] for e in events(log) if e["event"] == "recv"]
    assert methods[:3] == ["initialize", "notifications/initialized", "tools/list"]


def test_call_round_trip_shows_the_environment_it_was_given(backend):
    b, _ = backend()
    b.start(10)
    reply = b.request("tools/call", {"name": "echo", "arguments": {"text": "hi"}, "_meta": {"progressToken": 7}}, 10)
    seen = json.loads(text_of(reply))
    assert seen["entrypoint"] == "https://vlogs.example"
    assert seen["arguments"] == {"text": "hi"} and seen["meta"] == {"progressToken": 7}


def test_error_reply_is_returned_not_raised(backend):
    b, _ = backend()
    b.start(10)
    assert b.request("tools/call", {"name": "fail"}, 10)["error"]["code"] == -32602


def test_requests_are_in_flight_together(backend):
    b, log = backend()
    b.start(10)
    held = {}
    thread = threading.Thread(target=lambda: held.update(reply=b.request("tools/call", {"name": "hold"}, 10)))
    thread.start()
    assert wait_for(lambda: any(e.get("method") == "tools/call" for e in events(log)))
    assert b.inflight == 1
    assert text_of(b.request("tools/call", {"name": "release"}, 10)) == "released 1"
    thread.join(10)
    assert text_of(held["reply"]) == "released" and b.inflight == 0


def test_cancel_is_forwarded_and_the_backend_stays_usable(backend):
    b, log = backend()
    b.start(10)
    cancelled, outcome = threading.Event(), []

    def run():
        try:
            b.request("tools/call", {"name": "hold"}, 30, cancelled)
        except Cancelled:
            outcome.append("cancelled")

    thread = threading.Thread(target=run)
    thread.start()
    assert wait_for(lambda: any(e.get("method") == "tools/call" for e in events(log)))
    cancelled.set()
    thread.join(10)
    assert outcome == ["cancelled"]
    assert wait_for(lambda: any(e["event"] == "cancelled" for e in events(log)))
    assert text_of(b.request("tools/call", {"name": "query"}, 10)) == "query ok"
    assert b.inflight == 0


def test_timeout_cancels_the_call(backend):
    b, log = backend()
    b.start(10)
    with pytest.raises(CallTimeout):
        b.request("tools/call", {"name": "hold"}, 0.3)
    assert wait_for(lambda: any(e["event"] == "cancelled" for e in events(log)))
    assert b.alive()


def test_crash_fails_the_call_and_marks_the_backend_dead(backend):
    b, _ = backend()
    b.start(10)
    with pytest.raises(BackendGone) as crashed:
        b.request("tools/call", {"name": "crash"}, 10)
    assert crashed.value.sent and "kod wyjścia 3" in str(crashed.value)
    assert not b.alive()
    with pytest.raises(BackendGone) as later:
        b.request("tools/call", {"name": "query"}, 1)
    assert later.value.sent is False


def test_retired_backend_refuses_new_requests_but_only_when_idle(backend):
    b, log = backend()
    b.start(10)
    thread = threading.Thread(target=lambda: b.request("tools/call", {"name": "hold"}, 10))
    thread.start()
    assert wait_for(lambda: b.inflight == 1)
    assert b.try_retire() is False          # busy: cannot be retired
    b.request("tools/call", {"name": "release"}, 10)
    thread.join(10)
    assert b.try_retire() is True and not b.alive()
    with pytest.raises(BackendGone) as refused:
        b.request("tools/call", {"name": "query"}, 1)
    assert refused.value.sent is False


def test_start_fails_with_the_exit_code(backend):
    b, _ = backend(FAKE_MCP_EXIT_AT_START="1")
    with pytest.raises(BackendError, match="kod wyjścia 7"):
        b.start(10)


def test_start_times_out_when_the_server_is_silent(backend):
    b, _ = backend(FAKE_MCP_SILENT="1")
    with pytest.raises(BackendError):
        b.start(0.5)
    assert not b.alive()


def test_start_fails_for_a_missing_program(tmp_path):
    b, _ = make_backend(tmp_path, command=str(tmp_path / "no-such-program"), args=[])
    with pytest.raises(BackendError, match="nie mogę uruchomić"):
        b.start(5)


def test_non_json_lines_go_to_stderr_not_into_replies(backend, capfd):
    b, _ = backend()
    b.start(10)
    assert text_of(b.request("tools/call", {"name": "noisy"}, 10)) == "noisy ok"
    assert "this is not json" in capfd.readouterr().err


def test_an_oversized_line_kills_the_backend(backend, monkeypatch):
    monkeypatch.setattr(fanout, "MAX_BACKEND_LINE", 1000)
    b, _ = backend()
    b.start(10)
    with pytest.raises(BackendGone):
        b.request("tools/call", {"name": "big", "arguments": {"size": 5000}}, 10)
    assert not b.alive()


def test_absurdly_nested_json_from_the_backend_ends_it(backend):
    b, _ = backend()
    b.start(10)
    with pytest.raises(BackendGone):
        b.request("tools/call", {"name": "nested"}, 10)
    assert not b.alive()


def test_a_ping_from_the_backend_is_answered(backend):
    b, log = backend()
    b.start(10)
    assert text_of(b.request("tools/call", {"name": "askping"}, 10)) == "ping answered"
    answer = [e for e in events(log) if e["event"] == "ping_reply"][0]
    assert answer["result"] == {} and answer["error"] is None


def test_close_still_escalates_when_a_writer_is_stuck_on_a_full_pipe(backend):
    b, _ = backend(FAKE_MCP_STALL="1")
    b.start(10)                                  # the server answers initialize, then stops reading
    outcome = []

    def send_big():
        try:
            b.request("tools/call", {"name": "echo", "arguments": {"text": "x" * 400000}}, 60)
        except BackendGone as exc:
            outcome.append(exc)

    thread = threading.Thread(target=send_big)
    thread.start()
    assert wait_for(lambda: b.inflight == 1)
    time.sleep(0.3)                              # long enough for the write to fill the pipe and block
    started = time.monotonic()
    b.close(0.3)
    assert time.monotonic() - started < 5 and not b.alive()
    thread.join(10)
    assert not thread.is_alive() and len(outcome) == 1


def test_close_ends_the_process_and_may_be_repeated(backend):
    b, log = backend()
    b.start(10)
    b.close(2)
    b.close(2)
    assert wait_for(lambda: any(e["event"] == "eof" for e in events(log)))
    assert not b.alive()


def test_close_kills_a_child_that_ignores_end_of_input(backend):
    # sh stays the process group leader and runs the server as a child that
    # ignores EOF; only the group signal can end it.
    command = f"{shlex.quote(sys.executable)} {shlex.quote(FAKE)}; true"
    b, log = backend(command="sh", args=["-c", command], FAKE_MCP_IGNORE_EOF="1")
    b.start(10)
    pid = events(log)[0]["pid"]
    b.close(0.3)
    assert wait_for(lambda: pid_gone(pid))
