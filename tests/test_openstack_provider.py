import json
import stat
from pathlib import Path

import pytest

from conftest import make_item
from mcp_1wrap.errors import WrapError
from mcp_1wrap.launcher import prepare
from mcp_1wrap.providers.openstack import build_cloud


def load(env_path):
    return json.loads(Path(env_path).read_text())["clouds"]


# ---- build_cloud: pure field logic ---------------------------------------

def test_defaults():
    cloud, secure = build_cloud("EXAMPLE-CLOUD", make_item("EXAMPLE-CLOUD"))
    assert cloud == {
        "auth": {
            "auth_url": "https://keystone.example:5000/v3",
            "username": "tester",
            "user_domain_name": "Default",
            "project_name": "admin",
            "project_domain_name": "Default",
        },
        "region_name": "EXAMPLE-CLOUD",
        "interface": "public",
        "identity_api_version": 3,
        "verify": True,
    }
    assert secure == {"auth": {"password": "s3cret-value"}}


def test_overrides_and_case_insensitive_labels():
    item = make_item("EXAMPLE-CLOUD", extra={
        "Region_Name": "REGION-X", "PROJECT_NAME": "proj", "project_domain_name": "dom",
        "user_domain_name": "udom", "interface": "internal", "identity_api_version": "3",
    })
    cloud, _ = build_cloud("EXAMPLE-CLOUD", item)
    assert cloud["region_name"] == "REGION-X"
    assert cloud["interface"] == "internal"
    assert cloud["auth"]["project_name"] == "proj"
    assert cloud["auth"]["project_domain_name"] == "dom"
    assert cloud["auth"]["user_domain_name"] == "udom"


def test_system_scope_drops_project_fields():
    cloud, _ = build_cloud("SYS", make_item("SYS", extra={"system_scope": "ALL"}))
    assert cloud["auth"]["system_scope"] == "all"
    assert "project_name" not in cloud["auth"]
    assert "project_domain_name" not in cloud["auth"]


def test_system_scope_conflicts_with_project_fields():
    item = make_item("SYS", extra={"system_scope": "all", "project_name": "p"})
    with pytest.raises(WrapError, match="wyklucza pola: project_name"):
        build_cloud("SYS", item)


def test_system_scope_invalid_value():
    with pytest.raises(WrapError, match="niedozwoloną wartość 'project'"):
        build_cloud("SYS", make_item("SYS", extra={"system_scope": "project"}))


def test_verify_cert_false_disables_verification():
    cloud, _ = build_cloud("C", make_item("C", extra={"verify_cert": "false"}))
    assert cloud["verify"] is False


@pytest.mark.parametrize("value", ["TRUE", "yes", "1", " true "])
def test_verify_cert_truthy_values(value):
    cloud, _ = build_cloud("C", make_item("C", extra={"verify_cert": value}))
    assert cloud["verify"] is True


@pytest.mark.parametrize("value", ["FALSE", "No", "0"])
def test_verify_cert_falsy_values(value):
    cloud, _ = build_cloud("C", make_item("C", extra={"verify_cert": value}))
    assert cloud["verify"] is False


def test_verify_cert_label_is_case_insensitive():
    cloud, _ = build_cloud("C", make_item("C", extra={"Verify_Cert": "false"}))
    assert cloud["verify"] is False


def test_verify_cert_invalid_value():
    with pytest.raises(WrapError, match="'verify_cert' ma niedozwoloną wartość") as exc:
        build_cloud("C", make_item("C", extra={"verify_cert": "maybe"}))
    assert "maybe" not in str(exc.value) and "s3cret-value" not in str(exc.value)


def test_insecure_cloud_is_warned_about_in_summary(fake_op, args):
    seed(fake_op, make_item("Safe"), make_item("Loose", extra={"verify_cert": "false"}))
    prep = prepare(args(), fake_op.environ())
    assert load(prep.env["OS_CLIENT_CONFIG_FILE"])["Loose"]["verify"] is False
    assert load(prep.env["OS_CLIENT_CONFIG_FILE"])["Safe"]["verify"] is True
    warning = [line for line in prep.summary if "weryfikacja certyfikatu TLS wyłączona" in line]
    assert len(warning) == 1 and "Loose" in warning[0] and "Safe" not in warning[0]


def test_website_falls_back_to_primary_url():
    item = make_item("C", urls=[{"label": "other", "primary": True, "href": "https://primary.example/v3"}])
    cloud, _ = build_cloud("C", item)
    assert cloud["auth"]["auth_url"] == "https://primary.example/v3"


def test_missing_fields_are_all_listed_without_leaking_values():
    item = make_item("C", website=None, username=None)
    with pytest.raises(WrapError) as exc:
        build_cloud("C", item)
    assert "brakuje wymaganych pól: website, username" in str(exc.value)
    assert "s3cret-value" not in str(exc.value)


def test_api_version_must_be_number():
    with pytest.raises(WrapError, match="'identity_api_version' nie jest liczbą: 'v3'"):
        build_cloud("C", make_item("C", extra={"identity_api_version": "v3"}))


# ---- provider through prepare(): files, env, selection -------------------

def seed(fake_op, *items, vault="Private"):
    fake_op.set_db({"items": {vault: list(items)}, "refs": {}})


def test_all_tagged_items_are_written_with_exact_titles(fake_op, args):
    seed(fake_op, make_item("EXAMPLE-CLOUD"), make_item("Other"), make_item("Untagged", tags=[]))
    prep = prepare(args(), fake_op.environ())
    clouds = load(prep.env["OS_CLIENT_CONFIG_FILE"])
    secure = load(prep.env["OS_CLIENT_SECURE_FILE"])
    assert set(clouds) == {"EXAMPLE-CLOUD", "Other"}
    assert secure["EXAMPLE-CLOUD"]["auth"]["password"] == "s3cret-value"
    assert "OS_CLOUD" not in prep.env and "CLOUD_NAME" not in prep.env


def test_files_are_private(fake_op, args):
    seed(fake_op, make_item("A"))
    prep = prepare(args(), fake_op.environ())
    for var in ("OS_CLIENT_CONFIG_FILE", "OS_CLIENT_SECURE_FILE"):
        path = Path(prep.env[var])
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_os_cloud_selects_one_case_insensitively(fake_op, args):
    seed(fake_op, make_item("EXAMPLE-CLOUD"), make_item("Other"))
    prep = prepare(args(), fake_op.environ(OS_CLOUD="example-CLOUD"))
    assert prep.env["OS_CLOUD"] == "EXAMPLE-CLOUD"
    assert prep.env["CLOUD_NAME"] == "EXAMPLE-CLOUD"
    assert set(load(prep.env["OS_CLIENT_CONFIG_FILE"])) == {"EXAMPLE-CLOUD"}
    assert set(load(prep.env["OS_CLIENT_SECURE_FILE"])) == {"EXAMPLE-CLOUD"}


def test_os_cloud_without_match_lists_available(fake_op, args):
    seed(fake_op, make_item("A"), make_item("B"))
    with pytest.raises(WrapError, match=r"OS_CLOUD='nope'. Dostępne: A, B"):
        prepare(args(), fake_op.environ(OS_CLOUD="nope"))


def test_duplicate_names_differing_in_case(fake_op, args):
    seed(fake_op, make_item("Cloud1"), make_item("cloud1", item_id="c2"))
    with pytest.raises(WrapError, match="zduplikowana nazwa chmury 'cloud1'"):
        prepare(args(), fake_op.environ())


def test_custom_tag_and_vault_from_environment(fake_op, args):
    seed(fake_op, make_item("A", tags=["prod"]), make_item("B", tags=["OS_TAG"]), vault="Infra")
    prep = prepare(args(), fake_op.environ(OP_TAG="prod", OP_VAULT="Infra"))
    assert set(load(prep.env["OS_CLIENT_CONFIG_FILE"])) == {"A"}


def test_no_items_is_an_error(fake_op, args):
    seed(fake_op)
    with pytest.raises(WrapError, match="brak itemów z tagiem 'OS_TAG' w sejfie 'Private'"):
        prepare(args(), fake_op.environ())


def test_error_in_one_item_names_the_item(fake_op, args):
    seed(fake_op, make_item("Good"), make_item("Broken", username=None))
    with pytest.raises(WrapError, match=r"openstack/item 'Broken'\] brakuje wymaganych pól: username"):
        prepare(args(), fake_op.environ())


def test_same_inputs_reuse_directory(fake_op, args):
    seed(fake_op, make_item("A"))
    first = prepare(args(), fake_op.environ()).env["OS_CLIENT_CONFIG_FILE"]
    second = prepare(args(), fake_op.environ()).env["OS_CLIENT_CONFIG_FILE"]
    assert first == second
    other = prepare(args(), fake_op.environ(OS_CLOUD="a")).env["OS_CLIENT_CONFIG_FILE"]
    assert other != first


def test_cloud_name_var_can_be_disabled_or_renamed(fake_op, args, tmp_path):
    seed(fake_op, make_item("A"))
    profile = tmp_path / "p.toml"
    profile.write_text('[[provider]]\ntype = "openstack"\ncloud_name_var = "MY_CLOUD"\n')
    prep = prepare(args(profile_file=str(profile)), fake_op.environ(OS_CLOUD="a"))
    assert prep.env["MY_CLOUD"] == "A" and "CLOUD_NAME" not in prep.env
    profile.write_text('[[provider]]\ntype = "openstack"\ncloud_name_var = ""\n')
    prep = prepare(args(profile_file=str(profile)), fake_op.environ(OS_CLOUD="a"))
    assert "CLOUD_NAME" not in prep.env and prep.env["OS_CLOUD"] == "A"
