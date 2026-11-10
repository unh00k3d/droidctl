"""iOS: the runner tree -> raw tree mapping, rendered through the unchanged snapshot layer,
on REAL runner trees captured from the iPhone 12 (tests/fixtures/ios-runner)."""
import json
import os

import pytest

from droidctl import snapshot as S
from droidctl.ios import mapping

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "ios-runner")


def load(name):
    with open(os.path.join(FIX, name + ".json")) as f:
        return json.load(f)["tree"]


def snap(name):
    return S.build(mapping.convert(load(name)))


def els(s):
    return {(e.role, e.label): e for e in s.elements}


def walk(n):
    yield n
    for c in n.get("children") or []:
        yield from walk(c)


def test_handles_and_uids_pass_through_for_act():
    raw = mapping.convert(load("settings_general"))
    native = {n["handle"]: n for a in load("settings_general")["apps"] for n in walk(a["root"])}
    for w in raw["windows"]:
        for n in walk(w["root"]):
            assert n["handle"] in native
            assert n.get("uid") == native[n["handle"]].get("uid")


def test_settings_general_is_a_list_with_hidden_rows():
    s = snap("settings_general")
    lists = [e for e in s.elements if e.role == "list"]
    assert len(lists) == 1
    rows = [e.label for e in s.elements if e.role == "row"]
    assert rows[:3] == ["General · Manage your overall setup and preferences for iPhone, such as software updates, "
                        "device language, CarPlay, AirDrop, and more.", "About", "Software Update · 1"] or \
        rows[1:3] == ["About", "Software Update · 1"]
    assert "Shut Down" not in rows                    # far below the fold: counted in more↓, not shown
    # R3: no chevrons or scroll bars as elements
    assert not any("chevron" in (e.label or "") or "scroll bar" in (e.label or "") for e in s.elements)


def test_screen_and_units_come_from_the_runner():
    raw = mapping.convert(load("settings_general"))
    assert (raw["screen"]["w"], raw["screen"]["h"]) == (390, 844)
    assert raw["front"] == "com.apple.Preferences"


def test_test_app_controls():
    s = snap("testapp_main")
    e = els(s)
    assert ("switch", "Switch") in e and "checked" not in e[("switch", "Switch")].node.flags
    assert ("switch", "SwiftUI Toggle") in e
    slider = next(x for x in s.elements if x.role == "seekbar")
    assert slider.node.raw["range"] == {"min": 0, "max": 100, "cur": 50.0}
    assert ("button", "Web Button") in e
    # one web view, not three nested scroll containers (WKWebView reports 58 three times)
    webs = [n for w in mapping.convert(load("testapp_main"))["windows"] for n in walk(w["root"])
            if n["class"] == "android.webkit.WebView"]
    assert len(webs) == 1
    tbl = next(x for x in s.elements if x.role == "list")
    assert tbl.node.raw["collection"]["rows"] == 200


def test_an_alert_is_a_dialog():
    s = snap("testapp_alert")
    assert s.dialog
    labels = {e.label for e in s.elements}
    assert {"Test alert", "Pick one", "Cancel", "OK"} <= labels
    assert not any(e.role == "scroll" for e in s.elements)   # the alert's own scroll views fit: layout


def test_the_keyboard_is_an_input_method_window():
    raw = mapping.convert(load("testapp_keyboard"))
    assert any(w["type"] == "input_method" for w in raw["windows"])
    s = S.build(raw)
    assert s.keyboard


@pytest.mark.parametrize("value,cur", [("50%", 50.0), ("%50", 50.0), ("12,5 %", 12.5), ("", None)])
def test_slider_values_are_localized_percentages(value, cur):
    r = mapping.slider_range(value)
    assert (r or {}).get("cur") == cur


def test_paired_switch_records_the_inner_touch_target():
    # R8 on a minimal shape taken from a real Settings row (outer labelled Switch, inner unlabelled one)
    outer = {"handle": 1, "type": 40, "label": "Airplane Mode", "value": "0", "frame": [16, 300, 358, 44],
             "children": [{"handle": 2, "type": 40, "value": "0", "frame": [309, 306, 51, 31]}]}
    n = mapping.Mapper("com.apple.Preferences").node(outer)
    assert n["touch"] == 2 and n["touch_rect"] == [309, 306, 360, 337]
    assert "children" not in n and "checkable" in n["flags"]


# -- Safari (real captures of public pages, 2026-10-22)
def test_a_web_pages_root_does_not_merge_the_page_into_one_row():
    s = snap("safari_example")
    texts = [e for e in s.elements if e.role == "text" and "documentation" in (e.label or "")]
    assert len(texts) >= 2
    assert len({e.rect[1] for e in texts}) == len(texts)      # one paragraph per line, not one row
    assert not any(e.label == "Toolbar" for e in s.elements)   # bars are containers
    assert any(e.role == "input" and e.node.raw.get("id") == "TabBarItemTitle" for e in s.elements)


def test_web_landmarks_are_not_text_and_a_long_page_scrolls():
    s = snap("safari_wikipedia")
    labels = {e.label for e in s.elements}
    assert not labels & {"banner", "navigation", "note", "Contents, navigation"}
    scrollers = [e for e in s.elements if e.node.scrollable and e.node.is_list]
    assert [e.node.cls for e in scrollers] == ["WebView"]     # the page, not Safari's tab strip


def test_safaris_back_button_is_the_back_button():
    raw = mapping.convert(load("safari_wikipedia"))
    backs = [n for w in raw["windows"] for n in walk(w["root"]) if n.get("nav_back")]
    assert [n.get("id") for n in backs] == ["BackButton"]


@pytest.mark.parametrize("name", sorted(f[:-5] for f in os.listdir(FIX) if f.endswith(".json")))
def test_only_real_back_buttons_are_back(name):
    """The back-like-name rule for custom bar buttons (a real app's "arrowBack", 2026-10-26; its tree
    stays out of the repo) must not find a Back button that UIKit's trait or Safari's id doesn't."""
    tree = load(name)
    native = {n["handle"]: n for a in tree["apps"] for n in walk(a["root"])}
    raw = mapping.convert(tree)
    for w in raw["windows"]:
        for n in walk(w["root"]):
            if n.get("nav_back"):
                x = native[n["handle"]]
                assert x.get("traits", 0) & mapping.TRAIT_BACK or x.get("id") == "BackButton" or \
                    mapping.BACKISH.search(f"{x.get('label') or ''} {x.get('id') or ''}"), (name, x)


@pytest.mark.parametrize("label,ident,back", [
    ("arrowBack", None, True), ("Back", None, True), ("Geri", None, True), (None, "navBack", True),
    ("chevron.left", None, True), ("Settings", None, False), ("Backup", None, False), ("Close", None, False)])
def test_back_like_names(label, ident, back):
    assert bool(mapping.BACKISH.search(f"{label or ''} {ident or ''}")) == back
