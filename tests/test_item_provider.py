import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import make_item
from mcp_1wrap.errors import WrapError
from mcp_1wrap.launcher import prepare
from mcp_1wrap.op import OpClient
from mcp_1wrap.providers.base import Context
from mcp_1wrap.providers.item import check_url, names

SRC = str(Path(__file__).resolve().parents[1] / "src")
URL = "https://vlogs.example"
PROFILE = (
    '[[provider]]\ntype = "item"\n[provider.vars]\n'
    'VL_INSTANCE_ENTRYPOINT = { field = "vlogs_url", check = "url" }\n'
)


def cloud(title="EXAMPLE-CLOUD", url=URL, **kwargs):
    extra = {"vlogs_url": url} if url is not None else {}
    extra.update(kwargs.pop("extra", {}))
    return make_item(title, extra=extra, **kwargs)


def seed(fake_op, *items, vault="Private"):
    fake_op.set_db({"items": {vault: list(items)}, "refs": {}})


def profile_file(tmp_path, body=PROFILE):
    path = tmp_path / "p.toml"
    path.write_text(body)
    return str(path)


def run(fake_op, args, tmp_path, body=PROFILE, **env):
    return prepare(args(profile_file=profile_file(tmp_path, body)), fake_op.environ(**env))


def item_gets(fake_op):
    return [line for line in fake_op.calls() if line.startswith("item get")]


def run_cli(fake_op, *cli_args, extra_env=None):
    env = fake_op.environ(PYTHONPATH=SRC, **(extra_env or {}))
    return subprocess.run([sys.executable, "-m", "mcp_1wrap", *cli_args],
                          capture_output=True, text=True, env=env, timeout=60)


# ---- selection -------------------------------------------------------------

def test_field_becomes_variable(fake_op, args, tmp_path):
    seed(fake_op, cloud())
    prep = run(fake_op, args, tmp_path, OS_CLOUD="example-cloud")
    assert prep.env["VL_INSTANCE_ENTRYPOINT"] == URL
    assert prep.set_vars == ["VL_INSTANCE_ENTRYPOINT"]
    assert prep.summary == [
        f"item: chmura 'EXAMPLE-CLOUD': VL_INSTANCE_ENTRYPOINT <= pole 'vlogs_url' = {URL}"
    ]


def test_only_the_selected_cloud_is_fetched(fake_op, args, tmp_path):
    seed(fake_op, cloud("A", "https://a.example"), cloud("B", "https://b.example"))
    prep = run(fake_op, args, tmp_path, OS_CLOUD="b")
    assert prep.env["VL_INSTANCE_ENTRYPOINT"] == "https://b.example"
    assert item_gets(fake_op) == ["item get b --vault Private --format json"]


def test_a_cloud_must_be_named(fake_op, args, tmp_path):
    seed(fake_op, cloud("A"), cloud("B"))
    with pytest.raises(WrapError, match=r"\[item/list\] nie wskazano chmury .*dostępne: A, B"):
        run(fake_op, args, tmp_path)
    assert item_gets(fake_op) == []


def test_environment_wins_over_the_cloud_option(fake_op, args, tmp_path):
    seed(fake_op, cloud("A", "https://a.example"), cloud("B", "https://b.example"))
    body = ('[[provider]]\ntype = "item"\ncloud = "a"\n[provider.vars]\n'
            'VL_INSTANCE_ENTRYPOINT = { field = "vlogs_url", check = "url" }\n')
    assert run(fake_op, args, tmp_path, body).env["VL_INSTANCE_ENTRYPOINT"] == "https://a.example"
    prep = run(fake_op, args, tmp_path, body, OS_CLOUD="b")
    assert prep.env["VL_INSTANCE_ENTRYPOINT"] == "https://b.example"


def test_custom_tag_and_vault_from_environment(fake_op, args, tmp_path):
    seed(fake_op, cloud("A", tags=["prod"]), cloud("B", tags=["OS_TAG"]), vault="Infra")
    prep = run(fake_op, args, tmp_path, OP_VAULT="Infra", OP_TAG="prod", OS_CLOUD="a")
    assert prep.env["VL_INSTANCE_ENTRYPOINT"] == URL


def test_unknown_cloud_lists_the_available_ones(fake_op, args, tmp_path):
    seed(fake_op, cloud("A"), cloud("B"))
    with pytest.raises(WrapError) as exc:
        run(fake_op, args, tmp_path, OS_CLOUD="nope")
    assert "nie pasuje do chmury 'nope'. Dostępne: A, B" in str(exc.value)
    assert "OS_CLOUD" not in str(exc.value)


def test_duplicates_only_matter_among_the_selected(fake_op, args, tmp_path):
    seed(fake_op, cloud("Cloud1"), cloud("cloud1", item_id="c2"), cloud("Other", "https://o.example"))
    assert run(fake_op, args, tmp_path, OS_CLOUD="other").env["VL_INSTANCE_ENTRYPOINT"] == "https://o.example"
    with pytest.raises(WrapError, match=r"\[item/list\] zduplikowana nazwa chmury 'cloud1'"):
        run(fake_op, args, tmp_path, OS_CLOUD="CLOUD1")


def test_no_items_is_an_error(fake_op, args, tmp_path):
    seed(fake_op)
    with pytest.raises(WrapError, match=r"\[item/list\] brak itemów z tagiem 'OS_TAG' w sejfie 'Private'"):
        run(fake_op, args, tmp_path, OS_CLOUD="a")


# ---- fields ----------------------------------------------------------------

def test_labels_are_case_insensitive_and_values_are_stripped(fake_op, args, tmp_path):
    seed(fake_op, make_item("A", extra={"VLOGS_URL": f"  {URL}\n"}))
    assert run(fake_op, args, tmp_path, OS_CLOUD="a").env["VL_INSTANCE_ENTRYPOINT"] == URL


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_or_blank_field_is_reported_by_label(fake_op, args, tmp_path, value):
    seed(fake_op, cloud("A", value))
    with pytest.raises(WrapError, match=r"\[item/'A'\] brakuje pól: vlogs_url") as exc:
        run(fake_op, args, tmp_path, OS_CLOUD="a")
    assert "s3cret-value" not in str(exc.value)


def test_all_missing_labels_are_listed_once(fake_op, args, tmp_path):
    body = ('[[provider]]\ntype = "item"\n[provider.vars]\nX = "one"\nY = "two"\nZ = "one"\n')
    seed(fake_op, make_item("A"))
    with pytest.raises(WrapError, match=r"brakuje pól: one, two$"):
        run(fake_op, args, tmp_path, body, OS_CLOUD="a")


@pytest.mark.parametrize("value,reason", [
    ("a\x00b", "znaki sterujące"),
    ("a\tb", "znaki sterujące"),
    ("a\nb", "znaki sterujące"),
    ("x" * 2049, "dłuższa niż 2048"),
])
def test_text_values_are_limited(fake_op, args, tmp_path, value, reason):
    body = '[[provider]]\ntype = "item"\n[provider.vars]\nNOTE = "note"\n'
    seed(fake_op, make_item("A", extra={"note": value}))
    with pytest.raises(WrapError, match=reason) as exc:
        run(fake_op, args, tmp_path, body, OS_CLOUD="a")
    assert "pole 'note' (zmienna NOTE)" in str(exc.value)


def test_plain_text_values_pass_through(fake_op, args, tmp_path):
    body = '[[provider]]\ntype = "item"\n[provider.vars]\nNOTE = "note"\n'
    seed(fake_op, make_item("A", extra={"note": "hello world"}))
    prep = run(fake_op, args, tmp_path, body, OS_CLOUD="a")
    assert prep.env["NOTE"] == "hello world"
    assert prep.summary == ["item: chmura 'A': NOTE <= pole 'note'"]  # text values are never shown


def test_rejected_url_is_named_but_never_echoed(fake_op, args, tmp_path):
    seed(fake_op, cloud("A", "http://internal.example:9428"))
    with pytest.raises(WrapError) as exc:
        run(fake_op, args, tmp_path, OS_CLOUD="a")
    message = str(exc.value)
    assert "pole 'vlogs_url' (zmienna VL_INSTANCE_ENTRYPOINT) ma niedozwoloną wartość: http" in message
    assert "internal.example" not in message


def test_other_fields_never_leak(fake_op, args, tmp_path):
    seed(fake_op, cloud("A", extra={"note": "private-note"}))
    prep = run(fake_op, args, tmp_path, OS_CLOUD="a")
    blob = json.dumps([prep.env, prep.summary])
    assert "s3cret-value" not in blob and "private-note" not in blob


def test_check_mode_shows_urls_but_no_secrets(fake_op, tmp_path):
    seed(fake_op, cloud("EXAMPLE-CLOUD"))
    done = run_cli(fake_op, "--check", "--profile-file", profile_file(tmp_path),
                   extra_env={"OS_CLOUD": "example-cloud"})
    assert done.returncode == 0, done.stderr
    assert f"VL_INSTANCE_ENTRYPOINT <= pole 'vlogs_url' = {URL}" in done.stdout
    assert "s3cret-value" not in done.stdout + done.stderr


def test_variable_clash_with_another_provider(fake_op, args, tmp_path):
    body = (PROFILE + '\n[[provider]]\ntype = "env"\n[provider.vars]\n'
            'VL_INSTANCE_ENTRYPOINT = "op://Private/a/b"\n')
    fake_op.set_db({"items": {"Private": [cloud("A")]}, "refs": {"op://Private/a/b": "v"}})
    with pytest.raises(WrapError, match="tę samą zmienną: VL_INSTANCE_ENTRYPOINT"):
        run(fake_op, args, tmp_path, body, OS_CLOUD="a")


# ---- check_url -------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "https://vlogs.example",
    "https://vlogs.example:9428/prefix",
    "HTTPS://VLOGS.EXAMPLE",
    "https://192.0.2.10",
    "https://198.51.100.7:9428",
    "https://[2001:db8::1]:9428",
    "http://127.0.0.1:9428",
    "http://localhost:9428",
    "http://[::1]:9428",
])
def test_check_url_accepts(text):
    assert check_url(text) is None


@pytest.mark.parametrize("text,reason", [
    ("vlogs.example", "schemat https"),
    ("ftp://vlogs.example", "schemat https"),
    ("http://vlogs.example", "allow_http"),
    ("https://user:pw@vlogs.example", "danych logowania"),
    ("https://@vlogs.example", "danych logowania"),
    ("https://vlogs.example/?q=1", "zapytania ani fragmentu"),
    ("https://vlogs.example/#frag", "zapytania ani fragmentu"),
    ("https://vlogs.example?", "zapytania ani fragmentu"),
    ("https://vlogs .example", "białe znaki"),
    ("https://vlogs.example\\@evil.example", "odwrotny ukośnik"),
    ("https://vlogs.example/a/../b", "'..'"),
    ("https://vlogs.example:99999", "niepoprawny"),
    ("http://[::1", "niepoprawny"),
    ("https://żółw.example", "ASCII"),
    ("https:///path", "brak hosta"),
    ("https://vl%6Fgs.example", "niedozwolone znaki"),
    ("https://169.254.169.254/latest", "link-local"),
    ("https://[fe80::1]/", "link-local"),
    ("https://[::ffff:169.254.169.254]/", "link-local"),
    ("https://[fd00:ec2::254]/", "link-local"),                 # AWS metadata over IPv6
    ("https://[64:ff9b::a9fe:a9fe]/", "link-local"),            # NAT64 form of 169.254.169.254
    ("https://[2002:a9fe:a9fe::]/", "link-local"),              # 6to4 form
    ("https://[::ffff:0:a9fe:a9fe]/", "link-local"),            # SIIT form
    ("https://[2001:0:4136:e378:8000:63bf:5601:5601]/", "link-local"),   # Teredo client 169.254.169.254
    ("https://metadata.google.internal/", "metadanych"),
    ("https://vlogs.example./", "kropką"),
    ("https://vlogs.example:/", "port"),
    ("https://vlogs.example:0/", "port"),
    ("https://vlogs.example/%2e%2e/a", "'..'"),
    ("https://vlogs.example/a/%2E%2E/b", "'..'"),
    ("https://vlogs.example/a/..;/b", "'..'"),
    ("https://vlogs.example/a%5cb", "'..'"),
    ("https://0.0.0.0", "nieokreślony"),
    ("https://224.0.0.1", "multicast"),
    ("https://2852039166/", "liczbowy"),
    ("https://0x7f.1/", "liczbowy"),
    ("https://010.0.0.1/", "liczbowy"),
])
def test_check_url_rejects(text, reason):
    assert reason in (check_url(text) or "")


def test_check_url_options():
    assert check_url("http://vlogs.example", allow_http=True) is None
    patterns = ("*.intra.example",)
    assert check_url("https://vlogs.intra.example", allow_hosts=patterns) is None
    assert "allow_hosts" in check_url("https://vlogs.example", allow_hosts=patterns)


def test_allow_options_work_through_the_provider(fake_op, args, tmp_path):
    seed(fake_op, cloud("A", "http://vlogs.intra.example"))
    body = PROFILE.replace('type = "item"\n', 'type = "item"\nallow_http = true\n'
                           'allow_hosts = ["*.intra.example"]\n')
    assert run(fake_op, args, tmp_path, body, OS_CLOUD="a").env["VL_INSTANCE_ENTRYPOINT"] == (
        "http://vlogs.intra.example")
    seed(fake_op, cloud("A", "http://vlogs.other.example"))
    with pytest.raises(WrapError, match="allow_hosts"):
        run(fake_op, args, tmp_path, body, OS_CLOUD="a")


# ---- profile validation ----------------------------------------------------

@pytest.mark.parametrize("body,message", [
    ('[[provider]]\ntype = "item"\n', "wymaga niepustej tabeli"),
    ('[[provider]]\ntype = "item"\n[provider.vars]\n"1BAD" = "x"\n', "niepoprawna nazwa zmiennej"),
    ('[[provider]]\ntype = "item"\n[provider.vars]\nV = 1\n', "wartość musi być etykietą"),
    ('[[provider]]\ntype = "item"\n[provider.vars]\nV = { field = "x", chek = "url" }\n',
     "nieznane klucze: chek"),
    ('[[provider]]\ntype = "item"\n[provider.vars]\nV = { check = "url" }\n', "brak klucza 'field'"),
    ('[[provider]]\ntype = "item"\n[provider.vars]\nV = { field = "x", check = "ip" }\n',
     "'check' musi być"),
    ('[[provider]]\ntype = "item"\n[provider.vars]\nV = " "\n', "etykieta pola jest pusta"),
    ('[[provider]]\ntype = "item"\ntga = "x"\n[provider.vars]\nV = "x"\n',
     "nieznane opcje dostawcy 'item': tga"),
    ('[[provider]]\ntype = "item"\ntag = 1\n[provider.vars]\nV = "x"\n', "opcja 'tag' musi być tekstem"),
    ('[[provider]]\ntype = "item"\nallow_hosts = "x"\n[provider.vars]\nV = "x"\n',
     "'allow_hosts' musi być listą"),
    ('[[provider]]\ntype = "item"\nallow_http = "yes"\n[provider.vars]\nV = "x"\n',
     "'allow_http' musi być"),
])
def test_profile_validation(fake_op, args, tmp_path, body, message):
    with pytest.raises(WrapError, match=message):
        run(fake_op, args, tmp_path, body, OS_CLOUD="a")


# ---- names -----------------------------------------------------------------

def test_names_lists_unique_titles_of_the_selection(fake_op):
    seed(fake_op, cloud("b"), cloud("A"), cloud("C", tags=[]), cloud("b2", tags=["OS_TAG"]))
    environ = fake_op.environ()
    ctx = Context(op=OpClient(environ, 30), vault="Private", environ=environ,
                  runtime=lambda: Path(), note=lambda message: None)
    assert names(ctx, {}) == ["A", "b", "b2"]
