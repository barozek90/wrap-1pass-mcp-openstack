"""A scripted MCP client over two binary streams, for the gateway tests.

Every line the server writes must be one JSON object: a stray line makes
``next_message`` fail, which is how the tests check that stdout stays clean.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from typing import Any


class McpClient:
    def __init__(self, to_server, from_server) -> None:
        self._to = to_server
        self._lines: queue.Queue[bytes | None] = queue.Queue()
        self._stash: list[dict[str, Any]] = []
        self._next_id = 1
        self._pump_thread = threading.Thread(target=self._pump, args=(from_server,), daemon=True)
        self._pump_thread.start()

    def _pump(self, stream) -> None:
        try:
            for raw in stream:
                self._lines.put(raw)
        except (OSError, ValueError):
            pass
        self._lines.put(None)

    # -- sending -----------------------------------------------------------

    def send_raw(self, data: bytes) -> None:
        self._to.write(data)
        self._to.flush()

    def send(self, message: Any) -> None:
        self.send_raw(json.dumps(message).encode() + b"\n")

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)

    def start_request(self, method: str, params: Any = None, rid: Any = None) -> Any:
        if rid is None:
            rid = self._next_id
            self._next_id += 1
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        return rid

    def close_input(self) -> None:
        self._to.close()

    def wait_for_end_of_output(self, timeout: float = 10.0) -> None:
        """Returns once the server's output reached EOF (so the stream can be closed safely)."""
        self._pump_thread.join(timeout)

    # -- receiving ---------------------------------------------------------

    def next_message(self, timeout: float = 10.0) -> dict[str, Any]:
        raw = self._lines.get(timeout=timeout)
        if raw is None:
            raise EOFError("the server closed its output")
        message = json.loads(raw)       # a non-JSON line fails the test here
        assert isinstance(message, dict), message
        return message

    def wait_for(self, rid: Any, timeout: float = 10.0) -> dict[str, Any]:
        """The response with this id (other messages are kept for later)."""
        deadline = time.monotonic() + timeout
        for index, message in enumerate(self._stash):
            if message.get("id") == rid and type(message.get("id")) is type(rid):
                return self._stash.pop(index)
        while True:
            message = self.next_message(max(0.01, deadline - time.monotonic()))
            if "id" in message and message["id"] == rid and type(message["id"]) is type(rid):
                return message
            self._stash.append(message)

    def request(self, method: str, params: Any = None, rid: Any = None, timeout: float = 10.0) -> dict[str, Any]:
        return self.wait_for(self.start_request(method, params, rid), timeout)

    def call(self, name: str, arguments: dict[str, Any] | None = None, rid: Any = None,
             timeout: float = 10.0, **extra: Any) -> dict[str, Any]:
        params = {"name": name, "arguments": arguments or {}, **extra}
        return self.request("tools/call", params, rid, timeout)

    def silence(self, seconds: float = 0.4) -> list[dict[str, Any]]:
        """Everything that arrives within ``seconds`` (plus what was kept aside)."""
        got = list(self._stash)
        self._stash.clear()
        deadline = time.monotonic() + seconds
        while True:
            try:
                got.append(self.next_message(max(0.01, deadline - time.monotonic())))
            except (queue.Empty, EOFError):
                return got
            if time.monotonic() >= deadline:
                return got


def pipe_pair():
    """Two OS pipes as binary file objects: (server_reader, client_writer, client_reader, server_writer)."""
    s_in, c_out = os.pipe()
    c_in, s_out = os.pipe()
    return os.fdopen(s_in, "rb"), os.fdopen(c_out, "wb"), os.fdopen(c_in, "rb"), os.fdopen(s_out, "wb")


def tool_text(response: dict[str, Any]) -> str:
    return response["result"]["content"][0]["text"]
