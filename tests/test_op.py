import pytest

from mcp_1wrap.errors import OpError, WrapError
from mcp_1wrap.op import OpClient


def test_missing_op(fake_op):
    client = OpClient({"PATH": "/nonexistent"})
    with pytest.raises(OpError, match=r"\[x\] polecenie 'op'"):
        client.json("x", "item", "list")


def test_timeout(fake_op):
    client = OpClient(fake_op.environ(FAKE_OP_SLEEP="3"), timeout=1)
    with pytest.raises(OpError, match="nie odpowiedziało w 1s"):
        client.read("env/X", "op://v/i/f")


def test_nonzero_exit_reports_code_and_stderr(fake_op):
    client = OpClient(fake_op.environ())
    with pytest.raises(OpError, match=r"kod 1: \[ERROR\] could not read secret"):
        client.read("env/X", "op://v/i/f")


def test_op_failures_are_wrap_errors_too(fake_op):
    assert issubclass(OpError, WrapError)


def test_read_returns_value_verbatim(fake_op):
    fake_op.set_db({"items": {}, "refs": {"op://v/i/f": "abc\n def"}})
    assert OpClient(fake_op.environ()).read("env/X", "op://v/i/f") == "abc\n def"
