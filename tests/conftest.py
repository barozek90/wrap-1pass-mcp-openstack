"""Shared fixtures: a fake ``op`` executable backed by a JSON database."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

FAKE_OP = r'''#!__PYTHON__
import json, os, sys, time

db = json.load(open(os.environ["FAKE_OP_DB"]))
log = os.environ.get("FAKE_OP_LOG")
if log:
    with open(log, "a") as fh:
        fh.write(" ".join(sys.argv[1:]) + "\n")
if os.environ.get("FAKE_OP_SLEEP"):
    time.sleep(float(os.environ["FAKE_OP_SLEEP"]))

a = sys.argv[1:]


def opt(name):
    return a[a.index(name) + 1]


if a[:2] == ["item", "list"]:
    items = [i for i in db["items"].get(opt("--vault"), []) if opt("--tags") in i.get("tags", [])]
    print(json.dumps([{"id": i["id"], "title": i["title"]} for i in items]))
elif a[:2] == ["item", "get"]:
    for i in db["items"].get(opt("--vault"), []):
        if i["id"] == a[2]:
            print(json.dumps(i))
            break
    else:
        sys.stderr.write("[ERROR] item not found\n")
        sys.exit(1)
elif a[:1] == ["read"]:
    ref = a[-1]
    if ref in db.get("refs", {}):
        sys.stdout.write(db["refs"][ref])
    else:
        sys.stderr.write("[ERROR] could not read secret\n")
        sys.exit(1)
else:
    sys.exit(2)
'''


def make_item(title, item_id=None, tags=("OS_TAG",), website="https://keystone.example:5000/v3",
              username="tester", password="s3cret-value", extra=None, urls=None):
    fields = []
    if username is not None:
        fields.append({"label": "username", "value": username})
    if password is not None:
        fields.append({"label": "password", "value": password})
    for label, value in (extra or {}).items():
        fields.append({"label": label, "value": value})
    if urls is None:
        urls = [{"label": "website", "primary": True, "href": website}] if website else []
    return {"id": item_id or title.lower(), "title": title, "tags": list(tags),
            "fields": fields, "urls": urls}


class FakeOp:
    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        self.home.mkdir()
        # harden() puts $HOME/.local/bin first on PATH, so the fake op wins even
        # when a real op is installed in /usr/bin.
        self.bin = self.home / ".local" / "bin"
        self.bin.mkdir(parents=True)
        exe = self.bin / "op"
        exe.write_text(FAKE_OP.replace("__PYTHON__", sys.executable))
        exe.chmod(0o755)
        self.db_path = tmp_path / "db.json"
        self.log = tmp_path / "op.log"
        self.run = tmp_path / "run"
        self.run.mkdir(mode=0o700)
        self.set_db({"items": {}, "refs": {}})

    def set_db(self, db: dict) -> None:
        self.db_path.write_text(json.dumps(db))

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def environ(self, **extra: str) -> dict[str, str]:
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "XDG_RUNTIME_DIR": str(self.run),
            "FAKE_OP_DB": str(self.db_path),
            "FAKE_OP_LOG": str(self.log),
        }
        env.update(extra)
        return env


@pytest.fixture
def fake_op(tmp_path):
    return FakeOp(tmp_path)


def make_args(command=None, profile=None, profile_file=None, check=False, debug=False):
    return SimpleNamespace(command=command if command is not None else ["--", sys.executable],
                           profile=profile, profile_file=profile_file, check=check, debug=debug)


@pytest.fixture
def args():
    return make_args
