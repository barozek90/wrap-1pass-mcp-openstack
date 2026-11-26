"""The gateway in-process: real providers and fake ``op``, fake backends, scripted client."""

import copy
import io
import json
import sys
import threading
import time

import pytest

from conftest import make_item
from mcp_client import McpClient, pipe_pair, tool_text
from mcp_1wrap import fanout
from mcp_1wrap.errors import OpError, WrapError
from mcp_1wrap.fanout import (
    BackendError, BackendGone, CallTimeout, Cancelled, Gateway, LineTooLong, Writer,
    backend_env, encode, inject_argument, negotiate, read_line,
)
from mcp_1wrap.launcher import apply_providers, setup

TEMPLATE = (
    '[fanout]\nprobe_env = { VL_INSTANCE_ENTRYPOINT = "http://127.0.0.1:9" }\n@EXTRA@'
    '[[provider]]\ntype = "item"\n[provider.vars]\n'
    'VL_INSTANCE_ENTRYPOINT = { field = "vlogs_url", check = "url" }\n'
)


def cloud(title="A", url="https://a.example", **kwargs):
    return make_item(title, extra={"vlogs_url": url} if url is not None else {}, **kwargs)


def reply(text):
    return {"jsonrpc": "2.0", "id": 0, "result": {"content": [{"type": "text", "text": text}]}}


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class FakeBackend:
    """Stands in for ``fanout.Backend``; behaviour comes from the shared ``Hub``."""

    def __init__(self, hub, label, command, args, env):
        self.hub, self.label, self.command, self.args, self.env = hub, label, command, args, env
        self.started_at = self.last_used = 0.0
        self.on_notification = None
        self.calls = []
        self.start_timeout = None
        self.request_timeouts = []
        self.closed = False
        self._lock = threading.Lock()
        self._inflight = 0
        self._dead = False
        self._retired = False

    def start(self, timeout):
        self.start_timeout = timeout
        if self.hub.start_gate is not None and self.label != "probe":
            self.hub.start_entered.set()
            self.hub.start_gate.wait(10)
        if self.label in self.hub.start_errors:
            raise BackendError(self.hub.start_errors[self.label])
        self.started_at = self.last_used = time.monotonic()

    def list_tools(self, timeout):
        return copy.deepcopy(self.hub.tools)

    def alive(self):
        with self._lock:
            return not (self._dead or self._retired or self.closed)

    @property
    def inflight(self):
        with self._lock:
            return self._inflight

    def try_retire(self):
        with self._lock:
            if self._dead or self._inflight:
                return False
            self._retired = True
            return True

    def kill(self):
        self._dead = True

    def close(self, grace=0.0):
        self.closed = True

    def request(self, method, params, timeout, cancelled=None):
        with self._lock:
            if self._dead or self._retired or self.closed:
                raise BackendGone("backend nie działa", sent=False)
            self._inflight += 1
        try:
            self.calls.append((method, copy.deepcopy(params)))
            self.request_timeouts.append(timeout)
            hook = self.hub.hooks.get(params.get("name"))
            if hook is not None:
                return hook(self, params, timeout, cancelled)
            return reply(json.dumps({"label": self.label,
                                     "entrypoint": self.env.get("VL_INSTANCE_ENTRYPOINT"),
                                     "arguments": params.get("arguments")}))
        finally:
            with self._lock:
                self._inflight -= 1
                self.last_used = time.monotonic()


class Hub:
    def __init__(self):
        self.tools = [
            {"name": "query", "description": "q",
             "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}},
                             "required": ["query"]}},
            {"name": "flags", "description": "f"},
            {"name": "echo", "description": "e", "inputSchema": {"type": "object"}},
        ]
        self.backends = []
        self.start_errors = {}      # label -> text of the BackendError start() raises
        self.hooks = {}             # tool name -> callable(backend, params, timeout, cancelled)
        self.release = threading.Event()
        self.start_gate = None      # an Event: backends other than the probe wait for it in start()
        self.start_entered = threading.Event()

    def factory(self, label, command, args, env):
        backend = FakeBackend(self, label, command, list(args), dict(env))
        self.backends.append(backend)
        return backend

    def named(self, label):
        return [b for b in self.backends if b.label == label]

    def wait_hook(self, backend, params, timeout, cancelled):
        """Holds the call until ``release`` is set; a cancel ends it at once."""
        deadline = time.monotonic() + 10
        while not self.release.is_set():
            if cancelled is not None and cancelled.is_set():
                raise Cancelled
            if time.monotonic() > deadline:
                raise CallTimeout("test deadline")
            time.sleep(0.005)
        return reply("released")


class Harness:
    def __init__(self, session, hub, **kwargs):
        reader, to_server, from_server, writer = pipe_pair()
        self._streams = (reader, to_server, from_server, writer)
        self.gateway = Gateway(session, reader, writer, backend_factory=hub.factory, **kwargs)
        self.client = McpClient(to_server, from_server)
        self.thread = threading.Thread(target=self.gateway.serve, daemon=True)
        self.thread.start()

    def initialize(self, version="2025-06-18"):
        answer = self.client.request("initialize", {"protocolVersion": version, "capabilities": {},
                                                    "clientInfo": {"name": "t", "version": "0"}})
        self.client.notify("notifications/initialized")
        return answer

    def close(self):
        reader, to_server, from_server, writer = self._streams
        self.client.close_input()                 # EOF for the gateway, which then shuts down
        self.thread.join(10)
        writer.close()                            # EOF for the client's reader thread ...
        self.client.wait_for_end_of_output()      # ... so closing its stream cannot block
        from_server.close()
        reader.close()


@pytest.fixture
def make_gateway(fake_op, args, tmp_path):
    made = []

    def build(items=(), extra="", apply=None, **env):
        fake_op.set_db({"items": {"Private": list(items)}, "refs": {}})
        path = tmp_path / "fanout.toml"
        path.write_text(TEMPLATE.replace("@EXTRA@", extra))
        session = setup(args(profile_file=str(path)), fake_op.environ(**env))
        hub = Hub()
        harness = Harness(session, hub, **({"apply": apply} if apply else {}))
        made.append((harness, hub))
        return harness, hub

    yield build
    for harness, hub in made:
        hub.release.set()
        harness.close()


def op_calls(fake_op, prefix):
    return [line for line in fake_op.calls() if line.startswith(prefix)]


# ---- pure helpers ----------------------------------------------------------

def test_inject_argument_requires_the_argument_first():
    tool = {"name": "q", "inputSchema": {"type": "object", "required": ["x", "cloud"],
                                          "properties": {"x": {"type": "string"}}}}
    schema = inject_argument(tool, "cloud")["inputSchema"]
    assert schema["required"] == ["cloud", "x"]
    assert schema["properties"]["cloud"]["type"] == "string" and "enum" not in schema["properties"]["cloud"]
    assert tool["inputSchema"]["required"] == ["x", "cloud"]       # the original is untouched


@pytest.mark.parametrize("tool", [
    {"name": "bare"},
    {"name": "typed", "inputSchema": {"type": "object"}},
    {"name": "odd", "inputSchema": {"type": "object", "required": "x"}},
    {"name": "null", "inputSchema": {"properties": None}},
])
def test_inject_argument_builds_missing_parts(tool):
    schema = inject_argument(tool, "cloud", ["A", "B"])["inputSchema"]
    assert schema["type"] == "object" and schema["required"] == ["cloud"]
    assert schema["properties"]["cloud"]["enum"] == ["A", "B"]


def test_inject_argument_rejects_a_clash_and_a_bad_schema():
    clash = {"name": "q", "inputSchema": {"properties": {"cloud": {}}}}
    with pytest.raises(ValueError, match="ma już argument 'cloud'"):
        inject_argument(clash, "cloud")
    with pytest.raises(ValueError, match="niepoprawny schemat"):
        inject_argument({"name": "q", "inputSchema": {"properties": ["x"]}}, "cloud")


@pytest.mark.parametrize("asked,answered", [
    ("2025-11-25", "2025-11-25"), ("2025-06-18", "2025-06-18"), ("2024-11-05", "2024-11-05"),
    ("2026-07-28", "2025-11-25"), ("2099-01-01", "2025-11-25"), (None, "2025-11-25"), (5, "2025-11-25"),
])
def test_negotiate(asked, answered):
    assert negotiate(asked) == answered


def test_backend_env_keeps_only_what_is_allowed():
    base = {"PATH": "/p", "HOME": "/h", "LC_ALL": "C", "http_proxy": "x", "NO_PROXY": "y",
            "OP_TAG": "t", "SECRET": "s", "MCP_1WRAP_PROFILE": "p", "DOCKER_HOST": "d"}
    env = backend_env(base, {"URL": "u"}, inherit=["DOCKER_HOST", "SECRET"], unset=["SECRET"])
    assert env == {"PATH": "/p", "HOME": "/h", "LC_ALL": "C", "http_proxy": "x", "NO_PROXY": "y",
                   "DOCKER_HOST": "d", "URL": "u"}


def test_read_line():
    stream = io.BytesIO(b"a\r\nbb\n" + b"x" * 50 + b"\nok\nlast")
    assert read_line(stream, 10) == b"a"
    assert read_line(stream, 10) == b"bb"
    with pytest.raises(LineTooLong):
        read_line(stream, 10)
    assert read_line(stream, 10) == b"ok"          # the long line was skipped to its end
    assert read_line(stream, 10) == b"last"        # a last line without a newline still counts
    assert read_line(stream, 10) is None
    assert read_line(io.BytesIO(b"x" * 10 + b"\n"), 10) == b"x" * 10   # exactly at the limit


def test_encode_is_one_ascii_line():
    data = encode({"t": "żółć \n\"", "n": 1})
    assert data.endswith(b"\n") and data.count(b"\n") == 1 and data.isascii()
    assert json.loads(data) == {"t": "żółć \n\"", "n": 1}


def test_writer_reports_a_broken_stream_once():
    class Broken:
        def write(self, data):
            raise BrokenPipeError

        def flush(self):
            pass

    calls = []
    writer = Writer(Broken(), lambda: calls.append(1))
    assert writer.send({"a": 1}) is False and writer.send({"a": 2}) is False
    assert calls == [1]


def test_a_profile_without_fanout_is_refused(fake_op, args):
    session = setup(args(), fake_op.environ())
    with pytest.raises(WrapError, match="nie ma tabeli"):
        Gateway(session, io.BytesIO(), io.BytesIO())


# ---- handshake and housekeeping -------------------------------------------

def test_initialize_is_answered_without_touching_op(make_gateway, fake_op):
    h, _ = make_gateway([cloud()])
    result = h.initialize()["result"]
    assert result["protocolVersion"] == "2025-06-18" and result["capabilities"] == {"tools": {}}
    assert result["serverInfo"]["name"] == "mcp-1wrap-fanout"
    assert "'cloud'" in result["instructions"] and "untrusted" in result["instructions"]
    assert fake_op.calls() == []


@pytest.mark.parametrize("asked,answered", [("2024-11-05", "2024-11-05"), ("2099-01-01", "2025-11-25")])
def test_initialize_negotiates_the_version(make_gateway, asked, answered):
    h, _ = make_gateway()
    assert h.initialize(asked)["result"]["protocolVersion"] == answered


def test_ping_and_unsupported_methods(make_gateway):
    h, _ = make_gateway()
    h.initialize()
    assert h.client.request("ping")["result"] == {}
    for method in ("resources/list", "prompts/list", "server/discover", "logging/setLevel"):
        assert h.client.request(method)["error"]["code"] == -32601


def test_malformed_input_is_answered_or_ignored(make_gateway):
    h, _ = make_gateway()
    h.client.send_raw(b"not json\n")
    assert h.client.next_message()["error"]["code"] == -32700
    h.client.send_raw(b"[1, 2]\n")
    assert h.client.next_message()["error"]["code"] == -32600
    h.client.send_raw(b"\n  \n")                                   # blank lines
    h.client.send({"jsonrpc": "2.0", "id": 9, "result": {}})       # a response of the client
    h.client.send({"jsonrpc": "2.0", "id": True, "method": "ping"})
    answer = h.client.next_message()
    assert answer["error"]["code"] == -32600 and answer["id"] is None
    assert h.client.request("ping")["result"] == {}


def test_an_oversized_line_is_refused_and_the_stream_stays_in_sync(make_gateway, monkeypatch):
    monkeypatch.setattr(fanout, "MAX_CLIENT_LINE", 100)
    h, _ = make_gateway()
    h.client.send_raw(b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"pad":"' + b"x" * 500 + b'"}}\n')
    assert h.client.next_message()["error"]["code"] == -32700
    assert h.client.request("ping")["result"] == {}


# ---- tools/list ------------------------------------------------------------

def test_tools_list_adds_the_cloud_argument(make_gateway):
    h, hub = make_gateway()
    h.initialize()
    tools = {t["name"]: t for t in h.client.request("tools/list")["result"]["tools"]}
    query = tools["query"]["inputSchema"]
    assert query["required"] == ["cloud", "query"] and "enum" not in query["properties"]["cloud"]
    assert tools["flags"]["inputSchema"]["required"] == ["cloud"]           # it had no schema at all
    assert hub.tools[0]["inputSchema"]["required"] == ["query"]            # the probe's list is untouched


def test_allow_clouds_becomes_the_enum(make_gateway):
    h, _ = make_gateway(extra='allow_clouds = ["A", "b"]\n')
    h.initialize()
    schema = h.client.request("tools/list")["result"]["tools"][0]["inputSchema"]
    assert schema["properties"]["cloud"]["enum"] == ["A", "b"]


def test_the_probe_uses_probe_env_and_never_op(make_gateway, fake_op):
    h, hub = make_gateway([cloud()])
    h.initialize()
    h.client.request("tools/list")
    h.client.request("tools/list")                                       # cached: no second probe
    probes = hub.named("probe")
    assert len(probes) == 1 and probes[0].closed
    assert probes[0].env["VL_INSTANCE_ENTRYPOINT"] == "http://127.0.0.1:9"
    assert fake_op.calls() == []


def test_a_failed_probe_is_reported_and_not_cached(make_gateway):
    h, hub = make_gateway()
    hub.start_errors["probe"] = "brak serwera"
    h.initialize()
    answer = h.client.request("tools/list")
    assert answer["error"]["code"] == -32603 and "brak serwera" in answer["error"]["message"]
    del hub.start_errors["probe"]
    assert h.client.request("tools/list")["result"]["tools"]


def test_a_tool_that_already_has_the_argument_is_an_error(make_gateway):
    h, hub = make_gateway()
    hub.tools.append({"name": "x", "inputSchema": {"type": "object", "properties": {"cloud": {}}}})
    h.initialize()
    assert "ma już argument 'cloud'" in h.client.request("tools/list")["error"]["message"]


# ---- tools/call ------------------------------------------------------------

def test_each_cloud_gets_its_own_backend(make_gateway, fake_op):
    h, hub = make_gateway([cloud("A", "https://a.example"), cloud("B", "https://b.example")])
    h.initialize()
    first = json.loads(tool_text(h.client.call("echo", {"cloud": "a", "text": "x"})))
    assert first["entrypoint"] == "https://a.example" and first["arguments"] == {"text": "x"}
    assert json.loads(tool_text(h.client.call("echo", {"cloud": " B "})))["entrypoint"] == "https://b.example"
    again = json.loads(tool_text(h.client.call("echo", {"cloud": "A"})))
    assert again["label"] == "a"                                          # same backend, any case
    assert [b.label for b in hub.backends if b.label != "probe"] == ["a", "B"]
    assert len(op_calls(fake_op, "item get")) == 2                        # resolved once per cloud
    backend = hub.named("a")[0]
    assert backend.command == sys.executable and backend.args == []
    assert backend.calls[0] == ("tools/call", {"name": "echo", "arguments": {"text": "x"}})


def test_meta_is_passed_on(make_gateway):
    h, hub = make_gateway([cloud()])
    h.initialize()
    h.client.call("echo", {"cloud": "a"}, _meta={"progressToken": 5})
    assert hub.named("a")[0].calls[0][1]["_meta"] == {"progressToken": 5}


def test_backends_get_an_allow_listed_environment(make_gateway):
    h, hub = make_gateway(
        [cloud()], extra='inherit = ["DOCKER_HOST"]\n',
        OP_SERVICE_ACCOUNT_TOKEN="tok", AWS_SECRET_ACCESS_KEY="k", SSH_AUTH_SOCK="/s", GOFLAGS="-x",
        DOCKER_HOST="unix:///d", HTTPS_PROXY="http://proxy.example", LC_ALL="C")
    h.initialize()
    h.client.request("tools/list")
    h.client.call("echo", {"cloud": "a"})
    for backend, url in ((hub.named("a")[0], "https://a.example"), (hub.named("probe")[0], "http://127.0.0.1:9")):
        env = backend.env
        assert env["VL_INSTANCE_ENTRYPOINT"] == url
        assert env["DOCKER_HOST"] == "unix:///d" and env["HTTPS_PROXY"] == "http://proxy.example"
        assert env["LC_ALL"] == "C" and "PATH" in env and "HOME" in env
        for name in ("OP_SERVICE_ACCOUNT_TOKEN", "AWS_SECRET_ACCESS_KEY", "SSH_AUTH_SOCK", "GOFLAGS",
                     "FAKE_OP_DB", "FAKE_OP_LOG", "OS_CLOUD", "OP_TAG"):
            assert name not in env


def test_unknown_cloud_lists_the_available_ones(make_gateway):
    h, hub = make_gateway([cloud("A"), cloud("B")])
    h.initialize()
    answer = h.client.call("echo", {"cloud": "nope"})
    text = tool_text(answer)
    assert answer["result"]["isError"] is True
    assert "nie pasuje do chmury 'nope'" in text and "Dostępne: A, B" in text
    assert hub.named("nope") == []


def test_a_missing_field_is_reported_without_secrets(make_gateway):
    h, _ = make_gateway([cloud("A", None)])
    h.initialize()
    answer = h.client.call("echo", {"cloud": "a"})
    assert answer["result"]["isError"] is True
    assert "brakuje pól: vlogs_url" in tool_text(answer) and "s3cret-value" not in tool_text(answer)


@pytest.mark.parametrize("arguments", [
    {}, {"cloud": 123}, {"cloud": ""}, {"cloud": "   "}, {"cloud": "x" * 129}, {"cloud": "a\x00b"},
    {"cloud": ["a"]}, {"cloud": None}, {"cloud": "a\u0085b"}, {"cloud": "a\u009b[31m"},
])
def test_an_invalid_cloud_argument_never_reaches_op(make_gateway, fake_op, arguments):
    h, _ = make_gateway([cloud()])
    h.initialize()
    assert h.client.call("echo", arguments)["result"]["isError"] is True
    assert fake_op.calls() == []


def test_invalid_call_shapes_are_tool_errors(make_gateway):
    h, _ = make_gateway([cloud()])
    h.initialize()
    for params in (None, "text", {"arguments": {}}, {"name": 5}, {"name": "echo", "arguments": ["x"]}):
        assert h.client.request("tools/call", params)["result"]["isError"] is True


def test_allow_clouds_limits_the_calls(make_gateway, fake_op):
    h, _ = make_gateway([cloud("A"), cloud("B", "https://b.example")], extra='allow_clouds = ["a"]\n')
    h.initialize()
    refused = h.client.call("echo", {"cloud": "B"})
    assert refused["result"]["isError"] is True and "niedozwolona w profilu" in tool_text(refused)
    assert fake_op.calls() == []
    assert not h.client.call("echo", {"cloud": "A"})["result"].get("isError")


def test_backend_errors_are_passed_through_with_the_clients_id(make_gateway):
    h, hub = make_gateway([cloud()])
    hub.hooks["fail"] = lambda b, p, t, c: {"jsonrpc": "2.0", "id": 0,
                                           "error": {"code": -32602, "message": "bad arguments"}}
    h.initialize()
    answer = h.client.call("fail", {"cloud": "a"}, rid=41)
    assert answer["id"] == 41 and answer["error"] == {"code": -32602, "message": "bad arguments"}


def test_progress_notifications_are_forwarded(make_gateway):
    h, hub = make_gateway([cloud()])

    def progress(backend, params, timeout, cancelled):
        backend.on_notification({"jsonrpc": "2.0", "method": "notifications/progress",
                                 "params": {"progressToken": 5, "progress": 1}})
        return reply("done")

    hub.hooks["work"] = progress
    h.initialize()
    rid = h.client.start_request("tools/call", {"name": "work", "arguments": {"cloud": "a"},
                                                "_meta": {"progressToken": 5}})
    assert h.client.next_message()["method"] == "notifications/progress"
    assert tool_text(h.client.wait_for(rid)) == "done"


def test_the_audit_line_names_cloud_and_tool_but_not_the_query(make_gateway, capsys):
    h, _ = make_gateway([cloud()])
    h.initialize()
    h.client.call("echo", {"cloud": "a", "text": "secret-query-text"})
    err = capsys.readouterr().err
    assert "fanout: chmura='a' narzędzie=echo wynik=ok" in err and "secret-query-text" not in err


# ---- starting, failing, reusing --------------------------------------------

def test_a_failed_start_is_remembered_briefly(make_gateway, fake_op):
    h, hub = make_gateway([cloud()])
    hub.start_errors["a"] = "boom"
    h.initialize()
    assert "boom" in tool_text(h.client.call("echo", {"cloud": "a"}))
    ops = len(fake_op.calls())
    assert "boom" in tool_text(h.client.call("echo", {"cloud": "a"}))     # remembered
    assert len(fake_op.calls()) == ops and len(hub.named("a")) == 1
    del hub.start_errors["a"]
    h.gateway._slots["a"].fail_until = 0.0                                 # the memory expires
    assert not h.client.call("echo", {"cloud": "a"})["result"].get("isError")


def test_a_burst_of_failures_stops_new_starts(make_gateway, fake_op, monkeypatch):
    monkeypatch.setattr(fanout, "FAILURE_BURST", 3)
    h, hub = make_gateway([cloud(f"c{i}") for i in range(5)])
    hub.start_errors.update({f"c{i}": "boom" for i in range(5)})
    h.initialize()
    for i in range(3):
        assert "boom" in tool_text(h.client.call("echo", {"cloud": f"c{i}"}))
    ops = len(fake_op.calls())
    assert "zbyt wiele nieudanych prób" in tool_text(h.client.call("echo", {"cloud": "c3"}))
    assert len(fake_op.calls()) == ops


def test_an_old_idle_backend_is_restarted_and_sees_the_current_value(make_gateway, fake_op, monkeypatch):
    h, hub = make_gateway([cloud("A", "https://a.example")])
    h.initialize()
    h.client.call("echo", {"cloud": "a"})
    h.client.call("echo", {"cloud": "a"})
    assert len(hub.named("a")) == 1
    fake_op.set_db({"items": {"Private": [cloud("A", "https://new.example")]}, "refs": {}})
    monkeypatch.setattr(fanout, "BACKEND_MAX_AGE", 0.0)
    assert json.loads(tool_text(h.client.call("echo", {"cloud": "a"})))["entrypoint"] == "https://new.example"
    assert len(hub.named("a")) == 2 and wait_for(lambda: hub.named("a")[0].closed)


def test_the_least_recently_used_idle_backend_makes_room(make_gateway, monkeypatch):
    monkeypatch.setattr(fanout, "MAX_BACKENDS", 2)
    h, hub = make_gateway([cloud("a"), cloud("b"), cloud("c")])
    h.initialize()
    for name in ("a", "b", "c"):
        assert not h.client.call("echo", {"cloud": name})["result"].get("isError")
    assert wait_for(lambda: hub.named("a")[0].closed) and not hub.named("b")[0].closed
    assert not h.client.call("echo", {"cloud": "a"})["result"].get("isError")
    assert len(hub.named("a")) == 2 and wait_for(lambda: hub.named("b")[0].closed)


def test_no_room_when_every_backend_is_busy(make_gateway, monkeypatch):
    monkeypatch.setattr(fanout, "MAX_BACKENDS", 1)
    h, hub = make_gateway([cloud("a"), cloud("b")])
    hub.hooks["hold"] = hub.wait_hook
    h.initialize()
    held = h.client.start_request("tools/call", {"name": "hold", "arguments": {"cloud": "a"}})
    assert wait_for(lambda: hub.named("a") and hub.named("a")[0].inflight == 1)
    assert "zbyt wiele chmur" in tool_text(h.client.call("echo", {"cloud": "b"}))
    hub.release.set()
    assert tool_text(h.client.wait_for(held)) == "released"


def test_a_backend_dying_in_a_call_is_reported_and_replaced(make_gateway):
    h, hub = make_gateway([cloud()])

    def die(backend, params, timeout, cancelled):
        backend.kill()
        raise BackendGone("backend zakończył działanie (kod wyjścia 3)", sent=True)

    hub.hooks["crash"] = die
    h.initialize()
    assert "zakończył działanie" in tool_text(h.client.call("crash", {"cloud": "a"}))
    assert not h.client.call("echo", {"cloud": "a"})["result"].get("isError")
    assert len(hub.named("a")) == 2


def test_a_backend_gone_before_the_send_is_retried_once(make_gateway):
    h, hub = make_gateway([cloud()])
    state = {"n": 0}

    def flaky(backend, params, timeout, cancelled):
        state["n"] += 1
        if state["n"] == 1:
            backend.kill()
            raise BackendGone("backend nie działa", sent=False)
        return reply("second")

    hub.hooks["flaky"] = flaky
    h.initialize()
    assert tool_text(h.client.call("flaky", {"cloud": "a"})) == "second"
    assert len(hub.named("a")) == 2


def test_a_call_timeout_is_reported(make_gateway):
    h, hub = make_gateway([cloud()])

    def slow(backend, params, timeout, cancelled):
        raise CallTimeout("przekroczono limit czasu (600 s)")

    hub.hooks["slow"] = slow
    h.initialize()
    answer = h.client.call("slow", {"cloud": "a"})
    assert answer["result"]["isError"] is True and "limit czasu" in tool_text(answer)


# ---- concurrency and cancellation ------------------------------------------

def test_two_calls_are_in_flight_together(make_gateway):
    h, hub = make_gateway([cloud("a"), cloud("b")])
    hub.hooks["hold"] = hub.wait_hook
    h.initialize()
    first = h.client.start_request("tools/call", {"name": "hold", "arguments": {"cloud": "a"}})
    second = h.client.start_request("tools/call", {"name": "hold", "arguments": {"cloud": "b"}})
    assert wait_for(lambda: all(hub.named(n) and hub.named(n)[0].inflight == 1 for n in "ab"))
    assert h.client.request("ping")["result"] == {}                       # answered while both wait
    hub.release.set()
    assert tool_text(h.client.wait_for(second)) == "released"
    assert tool_text(h.client.wait_for(first)) == "released"


def test_ids_of_different_types_are_distinct_and_duplicates_are_refused(make_gateway):
    h, hub = make_gateway([cloud()])
    hub.hooks["hold"] = hub.wait_hook
    h.initialize()
    params = {"name": "hold", "arguments": {"cloud": "a"}}
    h.client.start_request("tools/call", params, rid=1)
    h.client.start_request("tools/call", params, rid="1")                 # a different id
    assert wait_for(lambda: hub.named("a") and hub.named("a")[0].inflight == 2)
    h.client.start_request("tools/call", params, rid=1)                   # the same id again
    assert h.client.next_message()["error"]["code"] == -32600
    hub.release.set()
    assert tool_text(h.client.wait_for(1)) == "released" and tool_text(h.client.wait_for("1")) == "released"


def test_too_many_requests_in_flight_are_refused(make_gateway, monkeypatch):
    monkeypatch.setattr(fanout, "MAX_INFLIGHT", 2)
    h, hub = make_gateway([cloud()])
    hub.hooks["hold"] = hub.wait_hook
    h.initialize()
    params = {"name": "hold", "arguments": {"cloud": "a"}}
    held = [h.client.start_request("tools/call", params), h.client.start_request("tools/call", params)]
    assert wait_for(lambda: hub.named("a") and hub.named("a")[0].inflight == 2)
    assert "zajęta" in tool_text(h.client.call("echo", {"cloud": "a"}))
    hub.release.set()
    for rid in held:
        assert tool_text(h.client.wait_for(rid)) == "released"


def test_ping_is_answered_while_a_cloud_is_being_resolved(make_gateway):
    entered, gate = threading.Event(), threading.Event()

    def slow_apply(session, overrides):
        entered.set()
        gate.wait(10)
        return apply_providers(session, overrides)

    h, hub = make_gateway([cloud()], apply=slow_apply)
    h.initialize()
    rid = h.client.start_request("tools/call", {"name": "echo", "arguments": {"cloud": "a"}})
    assert entered.wait(5)
    assert h.client.request("ping")["result"] == {}
    gate.set()
    assert not h.client.wait_for(rid)["result"].get("isError")


def test_cancel_before_the_backend_request_exists(make_gateway):
    entered, gate = threading.Event(), threading.Event()

    def slow_apply(session, overrides):
        entered.set()
        gate.wait(10)
        return apply_providers(session, overrides)

    h, hub = make_gateway([cloud()], apply=slow_apply)
    h.initialize()
    h.client.start_request("tools/call", {"name": "echo", "arguments": {"cloud": "a"}}, rid=5)
    assert entered.wait(5)
    h.client.notify("notifications/cancelled", {"requestId": 5})
    assert h.client.request("ping")["result"] == {}                       # the cancel has been handled
    gate.set()
    assert wait_for(lambda: not h.gateway._pending)                       # the worker has finished
    assert hub.named("a") == []                                           # no backend was started for it
    assert h.client.silence(0.2) == []                                    # and nothing answered id 5


def test_cancel_during_the_backend_call_drops_the_reply(make_gateway):
    h, hub = make_gateway([cloud()])
    hub.hooks["hold"] = hub.wait_hook
    h.initialize()
    h.client.start_request("tools/call", {"name": "hold", "arguments": {"cloud": "a"}}, rid=7)
    assert wait_for(lambda: hub.named("a") and hub.named("a")[0].inflight == 1)
    h.client.notify("notifications/cancelled", {"requestId": 7, "reason": "user"})
    assert wait_for(lambda: hub.named("a")[0].inflight == 0)
    assert h.client.request("ping")["result"] == {}
    hub.release.set()
    assert wait_for(lambda: not h.gateway._pending)
    assert h.client.silence(0.2) == []


def test_a_cancel_after_completion_or_for_nothing_is_harmless(make_gateway):
    h, _ = make_gateway([cloud()])
    h.initialize()
    assert not h.client.call("echo", {"cloud": "a"}, rid=3)["result"].get("isError")
    for params in ({"requestId": 3}, {"requestId": 999}, {"requestId": True}, {}, None, "x"):
        h.client.notify("notifications/cancelled", params)
    assert h.client.request("ping")["result"] == {}


def test_closing_the_input_closes_every_backend(make_gateway):
    h, hub = make_gateway([cloud("a"), cloud("b")])
    h.initialize()
    h.client.call("echo", {"cloud": "a"})
    h.client.call("echo", {"cloud": "b"})
    h.client.close_input()
    h.thread.join(10)
    assert not h.thread.is_alive()
    assert all(b.closed for b in hub.backends)


# ---- robustness fixes found in the code review -----------------------------

def test_unknown_names_do_not_lock_out_a_real_cloud(make_gateway):
    h, _ = make_gateway([cloud("A")])
    h.initialize()
    for i in range(8):                                                    # more than FAILURE_BURST
        answer = h.client.call("echo", {"cloud": f"typo{i}"})
        assert answer["result"]["isError"] is True and "Dostępne: A" in tool_text(answer)
    assert not h.client.call("echo", {"cloud": "a"})["result"].get("isError")


def test_op_failures_do_count_toward_the_burst(make_gateway, monkeypatch):
    monkeypatch.setattr(fanout, "FAILURE_BURST", 3)

    def broken(session, overrides):
        raise OpError("[item/list] 'op' nie odpowiedziało w 60s")

    h, _ = make_gateway([cloud(f"c{i}") for i in range(4)], apply=broken)
    h.initialize()
    for i in range(3):
        assert "'op' nie odpowiedziało" in tool_text(h.client.call("echo", {"cloud": f"c{i}"}))
    assert "zbyt wiele nieudanych prób" in tool_text(h.client.call("echo", {"cloud": "c3"}))


def test_remembered_names_are_capped_and_old_ones_pruned(make_gateway, monkeypatch):
    monkeypatch.setattr(fanout, "MAX_SLOTS", 2)
    h, _ = make_gateway([cloud("A")])
    h.initialize()
    for name in ("x1", "x2"):
        assert "Dostępne" in tool_text(h.client.call("echo", {"cloud": name}))
    assert "zbyt wiele różnych nazw" in tool_text(h.client.call("echo", {"cloud": "x3"}))
    monkeypatch.setattr(fanout, "SLOT_IDLE", 0.0)
    for slot in h.gateway._slots.values():                                # their failure memory expires
        slot.fail_until = 0.0
    assert "Dostępne" in tool_text(h.client.call("echo", {"cloud": "x3"}))   # pruned: there is room again


def test_with_allow_clouds_the_titles_of_other_clouds_stay_hidden(make_gateway):
    h, _ = make_gateway([cloud("B")], extra='allow_clouds = ["A"]\n')     # A has no item at all
    h.initialize()
    text = tool_text(h.client.call("echo", {"cloud": "A"}))
    assert "dozwolone: A" in text and "Dostępne" not in text and "B" not in text


def test_an_old_backend_that_is_busy_is_not_replaced(make_gateway, monkeypatch):
    h, hub = make_gateway([cloud()])
    hub.hooks["hold"] = hub.wait_hook
    h.initialize()
    held = h.client.start_request("tools/call", {"name": "hold", "arguments": {"cloud": "a"}})
    assert wait_for(lambda: hub.named("a") and hub.named("a")[0].inflight == 1)
    monkeypatch.setattr(fanout, "BACKEND_MAX_AGE", 0.0)
    assert not h.client.call("echo", {"cloud": "a"})["result"].get("isError")
    assert len(hub.named("a")) == 1 and not hub.named("a")[0].closed      # the busy one served it
    hub.release.set()
    assert tool_text(h.client.wait_for(held)) == "released"
    assert not h.client.call("echo", {"cloud": "a"})["result"].get("isError")
    assert len(hub.named("a")) == 2                                       # idle and old now: replaced


def test_a_backend_that_finishes_starting_after_shutdown_is_closed(make_gateway):
    h, hub = make_gateway([cloud()])
    hub.start_gate = threading.Event()
    h.initialize()
    h.client.start_request("tools/call", {"name": "echo", "arguments": {"cloud": "a"}})
    assert hub.start_entered.wait(5)
    h.client.close_input()                       # the client leaves while the backend is starting
    h.thread.join(10)
    assert not h.thread.is_alive()               # shutdown does not wait for that worker
    hub.start_gate.set()
    assert wait_for(lambda: hub.named("a")[0].closed)


def test_closing_does_not_wait_for_a_worker_stuck_in_op(make_gateway):
    entered, gate = threading.Event(), threading.Event()

    def stuck(session, overrides):
        entered.set()
        gate.wait(10)
        return apply_providers(session, overrides)

    h, hub = make_gateway([cloud()], apply=stuck)
    h.initialize()
    h.client.start_request("tools/call", {"name": "echo", "arguments": {"cloud": "a"}})
    assert entered.wait(5)
    started = time.monotonic()
    h.client.close_input()
    h.thread.join(10)
    assert not h.thread.is_alive() and time.monotonic() - started < 5
    gate.set()
    assert wait_for(lambda: not h.gateway._pending)
    assert hub.named("a") == []                  # the cancelled worker started nothing


def test_the_profile_timeouts_reach_the_backends(make_gateway):
    h, hub = make_gateway([cloud()], extra="start_timeout = 7\ncall_timeout = 70\n")
    h.initialize()
    h.client.request("tools/list")
    h.client.call("echo", {"cloud": "a"})
    assert hub.named("probe")[0].start_timeout == 7
    backend = hub.named("a")[0]
    assert backend.start_timeout == 7 and backend.request_timeouts == [70]


def test_refused_calls_leave_an_audit_line_too(make_gateway, capsys):
    h, _ = make_gateway([cloud("A")], extra='allow_clouds = ["a"]\n')
    h.initialize()
    h.client.call("echo", {"cloud": "B"})
    h.client.request("tools/call", {"name": "echo", "arguments": {}})
    err = capsys.readouterr().err
    assert "fanout: chmura='B' narzędzie=echo wynik=odrzucone" in err
    assert "fanout: chmura='?' narzędzie=echo wynik=odrzucone" in err


def test_absurdly_nested_input_is_a_parse_error(make_gateway):
    h, _ = make_gateway()
    h.client.send_raw(b"[" * 200000 + b"\n")
    assert h.client.next_message()["error"]["code"] == -32700
    assert h.client.request("ping")["result"] == {}


def test_an_unexpected_error_does_not_end_the_gateway(make_gateway, monkeypatch):
    def boom(self, rid, params):
        raise RuntimeError("bug")

    monkeypatch.setattr(Gateway, "_on_initialize", boom)
    h, _ = make_gateway()
    h.client.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert h.client.next_message()["error"]["code"] == -32603
    assert h.client.request("ping")["result"] == {}


def test_a_client_that_stopped_reading_triggers_the_hook(fake_op, args, tmp_path):
    path = tmp_path / "p.toml"
    path.write_text(TEMPLATE.replace("@EXTRA@", ""))
    session = setup(args(profile_file=str(path)), fake_op.environ())

    class Broken:
        def write(self, data):
            raise BrokenPipeError

        def flush(self):
            pass

    called = threading.Event()
    gateway = Gateway(session, io.BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'), Broken(),
                      on_broken=called.set)
    assert gateway.serve() == 0
    assert called.is_set()
