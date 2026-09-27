"""dump-fixture without a phone: a fake agent client stands in for the socket."""
import argparse
import json

import pytest

from droidctl import cli
from droidctl.core import UserError

TREE = {"gen": 5, "dump": 1, "degraded": False, "ms": 42, "nodes": 1,
        "screen": {"w": 1080, "h": 2220, "density": 420, "rotation": 0},
        "windows": [{"id": 1, "type": "application", "layer": 0, "bounds": [0, 0, 1080, 2220],
                     "pkg": "com.example", "root": {"handle": 1, "bounds": [0, 0, 1080, 2220]}}]}
PING = {"model": "SM-N950F", "manufacturer": "samsung", "sdk": 28, "release": "9",
        "screen": TREE["screen"], "version": "0.1.0", "versionCode": 1}


class FakeClient:
    def __init__(self, trees):
        self.trees = list(trees)

    def call(self, method, params=None, timeout=10.0):
        if method == "gen":
            return {"gen": 5}
        assert method == "tree"
        return self.trees.pop(0) if len(self.trees) > 1 else self.trees[0]

    def close(self):
        pass


def run(monkeypatch, tmp_path, trees, **kw):
    monkeypatch.setattr(cli.dev, "resolve_serial", lambda s: "SERIAL123")
    monkeypatch.setattr(cli.dev, "connect", lambda s: (FakeClient(trees), dict(PING)))
    monkeypatch.setattr(cli, "_focused_activity", lambda s: "com.example/.Main")
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    a = argparse.Namespace(name="probe", device=None, pkg="com.example", not_important=False,
                           dir=str(tmp_path), timeout=1.0, allow_degraded=False, json=True)
    for k, v in kw.items():
        setattr(a, k, v)
    return cli.cmd_dump_fixture(a)


def test_saves_meta_and_tree_without_the_serial(monkeypatch, tmp_path):
    p = run(monkeypatch, tmp_path, [TREE])
    doc = json.loads((tmp_path / "probe.json").read_text())
    assert p["ok"] and doc["tree"] == TREE
    assert doc["meta"]["package"] == "com.example"
    assert doc["meta"]["activity"] == "com.example/.Main"
    assert "SERIAL123" not in (tmp_path / "probe.json").read_text()


def test_refuses_a_degraded_dump(monkeypatch, tmp_path):
    bad = dict(TREE, degraded=True, reason="timeout")
    with pytest.raises(UserError) as e:
        run(monkeypatch, tmp_path, [bad])
    assert e.value.kind == "timeout"
    assert not (tmp_path / "probe.json").exists()


def test_retries_until_the_dump_is_complete(monkeypatch, tmp_path):
    bad = dict(TREE, degraded=True, reason="timeout")
    assert run(monkeypatch, tmp_path, [bad, TREE])["degraded"] is False


def test_rejects_bad_names(monkeypatch, tmp_path):
    with pytest.raises(UserError) as e:
        run(monkeypatch, tmp_path, [TREE], name="../escape")
    assert e.value.kind == "bad-args"
