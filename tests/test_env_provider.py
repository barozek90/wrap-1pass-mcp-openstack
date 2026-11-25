import pytest

from conftest import make_item
from mcp_1wrap.errors import WrapError
from mcp_1wrap.launcher import prepare


def profile(tmp_path, body):
    path = tmp_path / "p.toml"
    path.write_text(body)
    return str(path)


def test_vars_are_resolved(fake_op, args, tmp_path):
    fake_op.set_db({"items": {}, "refs": {"op://Private/gitlab/token": "tok-123", "op://Private/x/y": "v"}})
    path = profile(tmp_path, '[[provider]]\ntype = "env"\n[provider.vars]\nGITLAB_TOKEN = "op://Private/gitlab/token"\nOTHER = "op://Private/x/y"\n')
    prep = prepare(args(profile_file=path), fake_op.environ())
    assert prep.env["GITLAB_TOKEN"] == "tok-123" and prep.env["OTHER"] == "v"
    assert prep.set_vars == ["GITLAB_TOKEN", "OTHER"]
    assert "tok-123" not in " ".join(prep.summary)


def test_unreadable_reference_names_the_variable(fake_op, args, tmp_path):
    path = profile(tmp_path, '[[provider]]\ntype = "env"\n[provider.vars]\nGITLAB_TOKEN = "op://Private/gitlab/token"\n')
    with pytest.raises(WrapError, match=r"\[env/GITLAB_TOKEN\]"):
        prepare(args(profile_file=path), fake_op.environ())


def test_empty_value_is_an_error(fake_op, args, tmp_path):
    fake_op.set_db({"items": {}, "refs": {"op://Private/a/b": ""}})
    path = profile(tmp_path, '[[provider]]\ntype = "env"\n[provider.vars]\nTOK = "op://Private/a/b"\n')
    with pytest.raises(WrapError, match=r"\[env/TOK\] referencja op://Private/a/b zwróciła pustą wartość"):
        prepare(args(profile_file=path), fake_op.environ())


@pytest.mark.parametrize("body,message", [
    ('[[provider]]\ntype = "env"\n', "wymaga niepustej tabeli"),
    ('[[provider]]\ntype = "env"\n[provider.vars]\n"1BAD" = "op://a/b/c"\n', "niepoprawna nazwa zmiennej"),
    ('[[provider]]\ntype = "env"\n[provider.vars]\nTOK = "plain-text-secret"\n', "referencją 'op://"),
    ('[[provider]]\ntype = "env"\nextra = 1\n[provider.vars]\nTOK = "op://a/b/c"\n', "nieznane opcje"),
])
def test_validation(fake_op, args, tmp_path, body, message):
    with pytest.raises(WrapError, match=message):
        prepare(args(profile_file=profile(tmp_path, body)), fake_op.environ())


def test_two_providers_setting_the_same_variable(fake_op, args, tmp_path):
    path = profile(tmp_path, '[[provider]]\ntype = "env"\n[provider.vars]\nOS_CLIENT_CONFIG_FILE = "op://a/b/c"\n\n[[provider]]\ntype = "openstack"\n')
    fake_op.set_db({"items": {"Private": [make_item("A")]}, "refs": {"op://a/b/c": "x"}})
    with pytest.raises(WrapError, match="tę samą zmienną: OS_CLIENT_CONFIG_FILE"):
        prepare(args(profile_file=path), fake_op.environ())
