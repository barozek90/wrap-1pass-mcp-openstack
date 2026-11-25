import pytest

from conftest import make_item
from mcp_1wrap.errors import WrapError
from mcp_1wrap.launcher import prepare
from mcp_1wrap.profile import BUILTIN_OPENSTACK, load_profile, profiles_dir


def write(fake_op, name, body):
    d = profiles_dir({"HOME": str(fake_op.home)})
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.toml").write_text(body)


def test_default_is_builtin_openstack(fake_op):
    assert load_profile(None, None, fake_op.environ()) is BUILTIN_OPENSTACK


def test_named_profile_from_xdg_config_home(fake_op, tmp_path):
    cfg = tmp_path / "cfg"
    (cfg / "mcp-1wrap" / "profiles").mkdir(parents=True)
    (cfg / "mcp-1wrap" / "profiles" / "mine.toml").write_text(
        'vault = "Infra"\ntimeout = 5\nunset = ["X"]\n[[provider]]\ntype = "openstack"\ntag = "t"\n')
    p = load_profile("mine", None, fake_op.environ(XDG_CONFIG_HOME=str(cfg)))
    assert (p.name, p.vault, p.timeout, p.unset) == ("mine", "Infra", 5, ("X",))
    assert p.providers[0].options == {"tag": "t"}


def test_missing_profile_lists_available(fake_op):
    write(fake_op, "mine", '[[provider]]\ntype = "openstack"\n')
    with pytest.raises(WrapError, match=r"nie znaleziono profilu 'nope'.*dostępne: mine, openstack"):
        load_profile("nope", None, fake_op.environ())


def test_builtin_name_works_without_file(fake_op):
    assert load_profile("openstack", None, fake_op.environ()) is BUILTIN_OPENSTACK


def test_profile_name_is_validated(fake_op):
    with pytest.raises(WrapError, match="niepoprawna nazwa profilu"):
        load_profile("../evil", None, fake_op.environ())


@pytest.mark.parametrize("body,message", [
    ("not = [valid", "niepoprawny TOML"),
    ('bogus = 1\n[[provider]]\ntype = "openstack"\n', "nieznane klucze: bogus"),
    ('vault = 1\n[[provider]]\ntype = "openstack"\n', "'vault' musi być tekstem"),
    ('timeout = 0\n[[provider]]\ntype = "openstack"\n', "'timeout' musi być dodatnią"),
    ('unset = "X"\n[[provider]]\ntype = "openstack"\n', "'unset' musi być listą"),
    ("vault = 'x'\n", "brak sekcji \\[\\[provider\\]\\]"),
    ('[[provider]]\ntype = "nope"\n', "nieznany typ 'nope' \\(dostępne: env, openstack\\)"),
    ('[[provider]]\ntype = "openstack"\ntga = "x"\n', "nieznane opcje dostawcy 'openstack': tga"),
])
def test_profile_validation(fake_op, tmp_path, body, message):
    path = tmp_path / "p.toml"
    path.write_text(body)
    with pytest.raises(WrapError, match=message):
        load_profile(None, str(path), fake_op.environ())


def test_profile_vault_and_tag_are_used_but_environment_wins(fake_op, args, tmp_path):
    fake_op.set_db({"items": {"Infra": [make_item("A", tags=["t"])], "Other": [make_item("B", tags=["t"])]}, "refs": {}})
    path = tmp_path / "p.toml"
    path.write_text('vault = "Infra"\n[[provider]]\ntype = "openstack"\ntag = "t"\n')
    prep = prepare(args(profile_file=str(path)), fake_op.environ())
    assert "'Infra'" in prep.summary[0]
    prep = prepare(args(profile_file=str(path)), fake_op.environ(OP_VAULT="Other"))
    assert "'Other'" in prep.summary[0]
