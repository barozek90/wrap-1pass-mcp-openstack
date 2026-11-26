"""A tiny MCP server over stdio for the fan-out tests (stdlib only).

Behaviour is steered by environment variables:

  FAKE_MCP_LOG            jsonl file with one line per event (start, recv, cancelled, eof)
  FAKE_MCP_EXIT_AT_START  exit with code 7 before reading anything
  FAKE_MCP_SILENT         never answer ``initialize``
  FAKE_MCP_IGNORE_EOF     keep running after stdin is closed (only a signal stops it, or 60 s)
  FAKE_MCP_STALL          after answering ``initialize`` stop reading stdin for good

Tools: query, flags (schemas only), echo (what the server saw), hold/release
(answer a call only after another one arrives), crash, noisy (prints a line that
is not JSON-RPC), nested (prints absurdly nested JSON), askping (asks the client
a ping and answers once it replied), big (a long reply), fail (a JSON-RPC error).
"""

import json
import os
import sys
import time

LOG = os.environ.get("FAKE_MCP_LOG")

TOOLS = [
    {"name": "query", "description": "Run a query",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}},
                     "required": ["query"]}},
    {"name": "flags", "description": "List flags", "inputSchema": {"type": "object"}},
    {"name": "echo", "description": "Echo what the server saw",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
    {"name": "hold", "description": "Answered by release", "inputSchema": {"type": "object"}},
    {"name": "release", "description": "Answers every held call", "inputSchema": {"type": "object"}},
    {"name": "crash", "description": "Exit at once", "inputSchema": {"type": "object"}},
    {"name": "noisy", "description": "Print a non-JSON line", "inputSchema": {"type": "object"}},
    {"name": "nested", "description": "Print nested JSON", "inputSchema": {"type": "object"}},
    {"name": "askping", "description": "Ping the client", "inputSchema": {"type": "object"}},
    {"name": "big", "description": "A long reply", "inputSchema": {"type": "object"}},
    {"name": "fail", "description": "JSON-RPC error", "inputSchema": {"type": "object"}},
]


def log(**event):
    if LOG:
        with open(LOG, "a") as fh:
            fh.write(json.dumps(event) + "\n")


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def text_result(rid, text):
    send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": text}]}})


awaiting_ping = []


def call(rid, params, held):
    name, args = params.get("name"), params.get("arguments") or {}
    if name == "echo":
        text_result(rid, json.dumps({
            "entrypoint": os.environ.get("VL_INSTANCE_ENTRYPOINT"),
            "arguments": args,
            "meta": params.get("_meta"),
            "op_token_visible": "OP_SERVICE_ACCOUNT_TOKEN" in os.environ,
            "leaked": sorted(k for k in os.environ if k.startswith(("OP_", "OS_", "FAKE_OP", "AWS_"))),
        }))
    elif name == "hold":
        held.append(rid)
    elif name == "release":
        for hid in held:
            text_result(hid, "released")
        text_result(rid, f"released {len(held)}")
        held.clear()
    elif name == "crash":
        os._exit(3)
    elif name == "noisy":
        sys.stdout.write("this is not json\n")
        sys.stdout.flush()
        text_result(rid, "noisy ok")
    elif name == "nested":
        sys.stdout.write("[" * 200000 + "\n")
        sys.stdout.flush()
    elif name == "askping":
        awaiting_ping.append(rid)
        send({"jsonrpc": "2.0", "id": "srv-ping", "method": "ping"})
    elif name == "big":
        text_result(rid, "x" * int(args.get("size", 100000)))
    elif name == "fail":
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "bad arguments"}})
    elif name in ("query", "flags"):
        text_result(rid, f"{name} ok")
    else:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"unknown tool {name}"}})


def main():
    log(event="start", pid=os.getpid(), ppid=os.getppid())
    if os.environ.get("FAKE_MCP_EXIT_AT_START"):
        sys.exit(7)
    held = []
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        message = json.loads(raw)
        method, rid = message.get("method"), message.get("id")
        log(event="recv", method=method, id=rid)
        if method is None and rid == "srv-ping":
            log(event="ping_reply", result=message.get("result"), error=message.get("error"))
            if awaiting_ping:
                text_result(awaiting_ping.pop(0), "ping answered")
        elif method == "initialize":
            if os.environ.get("FAKE_MCP_SILENT"):
                continue
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                "serverInfo": {"name": "fake", "version": "0"}}})
            if os.environ.get("FAKE_MCP_STALL"):
                time.sleep(3600)
        elif method == "notifications/cancelled":
            cancelled = message["params"]["requestId"]
            log(event="cancelled", id=cancelled)
            if cancelled in held:
                held.remove(cancelled)
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            call(rid, message["params"], held)
        elif rid is not None:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "no such method"}})
    log(event="eof")
    deadline = time.monotonic() + 60       # never outlive a failed test for long
    while os.environ.get("FAKE_MCP_IGNORE_EOF") and time.monotonic() < deadline:
        time.sleep(0.1)


main()
