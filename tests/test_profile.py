from pathlib import Path

import pytest

from conftest import make_item
from mcp_1wrap.errors import WrapError
from mcp_1wrap.launcher import prepare
from mcp_1wrap.profile import BUILTIN_OPENSTACK, FanoutSpec, load_profile, profiles_dir


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
    ('[[provider]]\ntype = "nope"\n', "nieznany typ 'nope' \\(dostępne: env, item, openstack\\)"),
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


PROVIDER = '[[provider]]\ntype = "openstack"\n'
PROBE = '[fanout]\nprobe_env = { URL = "http://127.0.0.1:9" }\n'


@pytest.mark.parametrize("name", ["openstack.toml", "victorialogs.toml"])
def test_shipped_examples_are_valid_profiles(fake_op, name):
    path = Path(__file__).resolve().parents[1] / "examples" / name
    assert load_profile(None, str(path), fake_op.environ()).providers


def test_profiles_have_no_fanout_unless_they_ask(fake_op, tmp_path):
    assert BUILTIN_OPENSTACK.fanout is None
    path = tmp_path / "p.toml"
    path.write_text(PROVIDER)
    assert load_profile(None, str(path), fake_op.environ()).fanout is None


def test_fanout_defaults(fake_op, tmp_path):
    path = tmp_path / "p.toml"
    path.write_text(PROBE + PROVIDER)
    assert load_profile(None, str(path), fake_op.environ()).fanout == FanoutSpec(
        probe_env={"URL": "http://127.0.0.1:9"}, argument="cloud", allow_clouds=(), inherit=(),
        start_timeout=30, call_timeout=600)


def test_fanout_options(fake_op, tmp_path):
    path = tmp_path / "p.toml"
    path.write_text(PROBE + 'argument = "region"\nallow_clouds = ["A", "b"]\ninherit = ["DOCKER_HOST"]\n'
                    "start_timeout = 5\ncall_timeout = 60\n" + PROVIDER)
    assert load_profile(None, str(path), fake_op.environ()).fanout == FanoutSpec(
        probe_env={"URL": "http://127.0.0.1:9"}, argument="region", allow_clouds=("A", "b"),
        inherit=("DOCKER_HOST",), start_timeout=5, call_timeout=60)


@pytest.mark.parametrize("body,message", [
    ("fanout = 1\n" + PROVIDER, "'fanout' musi być tabelą"),
    ("[fanout]\n" + PROVIDER, "'probe_env' jest wymagane"),
    ("[fanout]\nprobe_env = {}\n" + PROVIDER, "'probe_env' jest wymagane"),
    ('[fanout]\nprobe_env = { "1BAD" = "x" }\n' + PROVIDER, "'probe_env': niepoprawna nazwa zmiennej"),
    ("[fanout]\nprobe_env = { URL = 1 }\n" + PROVIDER, "wartość URL musi być tekstem"),
    (PROBE + "bogus = 1\n" + PROVIDER, "nieznane klucze: bogus"),
    (PROBE + 'argument = "a b"\n' + PROVIDER, "'argument' musi być nazwą"),
    (PROBE + 'allow_clouds = "A"\n' + PROVIDER, "'allow_clouds' musi być listą tekstów"),
    (PROBE + 'allow_clouds = [""]\n' + PROVIDER, "niepoprawna nazwa chmury"),
    (PROBE + 'inherit = ["bad-name"]\n' + PROVIDER, "'inherit': niepoprawna nazwa zmiennej"),
    (PROBE + "start_timeout = 0\n" + PROVIDER, "'start_timeout' musi być dodatnią liczbą sekund"),
    (PROBE + "start_timeout = 3601\n" + PROVIDER, r"'start_timeout'.*\(najwyżej 3600\)"),
    (PROBE + "call_timeout = true\n" + PROVIDER, "'call_timeout' musi być dodatnią liczbą sekund"),
    (PROBE + "call_timeout = 86401\n" + PROVIDER, r"'call_timeout'.*\(najwyżej 86400\)"),
])
def test_fanout_validation(fake_op, tmp_path, body, message):
    path = tmp_path / "p.toml"
    path.write_text(body)
    with pytest.raises(WrapError, match=message):
        load_profile(None, str(path), fake_op.environ())
