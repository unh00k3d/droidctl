"""Every captured fixture is a well-formed raw tree (PROTOCOL.md "tree").

Fixtures are captured from real devices with `droidctl dump-fixture` / `make
fixtures`, never written by hand; these checks keep a truncated, degraded or
hand-edited file from silently feeding the snapshot and resolver tests.
"""
import json
import pathlib

import pytest

TREES = pathlib.Path(__file__).resolve().parent / "fixtures" / "trees"
FIXTURES = sorted(TREES.glob("*.json"))
WINDOW_TYPES = {"application", "input_method", "system", "accessibility_overlay",
                "split_screen_divider", "magnification_overlay"}
FLAGS = {"clickable", "longClickable", "checkable", "checked", "focusable", "focused", "selected",
         "enabled", "editable", "password", "scrollable", "heading", "showingHint"}


def nodes(tree):
    stack = [w["root"] for w in tree["windows"] if "root" in w]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n.get("children", []))


def test_there_are_real_fixtures():
    assert any(f.name.startswith("real-") for f in FIXTURES)


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_fixture_is_a_well_formed_raw_tree(path):
    doc = json.loads(path.read_text(encoding="utf-8"))
    meta, tree = doc["meta"], doc["tree"]
    assert meta["name"] == path.stem
    for key in ("model", "sdk", "screen", "package", "agent", "captured"):
        assert meta.get(key), f"meta.{key} missing"
    assert "serial" not in json.dumps(meta), "fixtures must not record the device serial"
    if tree["degraded"]:
        # before agent versionCode 3 a degraded dump was the previous screen's tree;
        # from 3 on it is a partial read of the current one and says what is missing
        assert meta["agent"]["versionCode"] >= 3, "a degraded dump from an old agent may be another screen's tree"
        assert tree.get("reason"), "a degraded dump must say why"
        missing = [n for n in nodes(tree) if n.get("truncated")] or \
                  [w for w in tree["windows"] if w.get("no_root")]
        assert missing, "a degraded dump must mark what it didn't read"
    assert isinstance(tree["dump"], int) and isinstance(tree["gen"], int)

    for w in tree["windows"]:
        assert w["type"] in WINDOW_TYPES or w["type"].startswith("type_")
        if w.get("unlisted"):          # from rootInActiveWindow: no window bounds (PROTOCOL.md)
            assert "bounds" not in w and w.get("root")
        else:
            assert len(w["bounds"]) == 4
    apps = [w for w in tree["windows"] if w["type"] == "application" and "root" in w]
    if "no root" in meta.get("note", "") or any(w.get("no_root") for w in tree["windows"]):
        # a real capture of the agent returning an app window without its tree
        # (TESTAPP slow_a11y); kept as the input for the snapshot's warning
        assert not apps
    else:
        assert apps, "no application window"
        assert meta["package"] in {w.get("pkg") for w in apps}, "meta.package is not on screen"

    handles = []
    for n in nodes(tree):
        handles.append(n["handle"])
        # raw, as Android reports them: clipped to the window, so a node lying
        # wholly outside it comes back *inverted* (left > right or top > bottom),
        # which the snapshot must read as empty. Seen on Samsung API 28.
        assert len(n["bounds"]) == 4 and all(isinstance(v, int) for v in n["bounds"]), n
        assert set(n.get("flags", [])) <= FLAGS, n.get("flags")
        for a in n.get("actions", []):
            assert isinstance(a, str) or isinstance(a.get("id"), int), a
        for key in ("text", "desc", "hint", "error", "state", "tooltip", "pane"):
            assert n.get(key, "x") != "", f"empty {key} must be omitted"
    assert len(handles) == len(set(handles)) == tree["nodes"], "handles must be unique and counted"
