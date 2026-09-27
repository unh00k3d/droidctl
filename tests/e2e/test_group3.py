"""TESTAPP group 3: controls and click semantics. Ground truth: DTA lines."""
import time

import pytest

from tests.e2e.conftest import dc


def kind(p):
    return (p.get("error") or {}).get("kind")


def el(snap, label=None, pred=None):
    hits = [e for e in snap["elements"]
            if (label is None or e["label"] == label) and (pred is None or pred(e))]
    assert len(hits) == 1, (label, [e["label"] for e in snap["elements"]])
    return hits[0]


def until(fn, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.2)
    return fn()


@pytest.mark.parametrize("name", ["buttons", "buttons_compose"])
def test_unlabeled_button_is_named_by_its_id_and_tappable(scenario, name):
    sc = scenario(name)
    s = sc.snap()
    m = el(s, pred=lambda e: (e["id"] or "").endswith("mystery") or e.get("inferred") == "mystery")
    assert m["inferred"] == "mystery" and "mystery?" in s["text"]
    dc("tap", m["ref"])
    dc("tap", "--text", "Save")
    assert len(sc.dta("click", "mystery")) == 1 and len(sc.dta("click", "save")) == 1


def test_toggle_views_state_and_exactly_once(scenario):
    sc = scenario("toggle")
    r = dc("tap", "--text", "Wi-Fi")
    assert any("on" in x for x in r["diff"]), r["diff"]
    dc("tap", "--text", "Remember me")
    dc("tap", "--text", "Large")
    assert [e["state"] for e in sc.dta("click", "wifi")] == [True]
    assert [e["state"] for e in sc.dta("click", "remember")] == [True]
    assert len(sc.dta("click", "large")) == 1


def test_toggle_compose_exactly_once(scenario):
    sc = scenario("toggle_compose")
    dc("tap", "--id", "wifi")
    dc("tap", "--id", "remember")
    dc("tap", "--text", "Medium")
    assert [e["state"] for e in sc.dta("click", "wifi")] == [True]
    assert [e["state"] for e in sc.dta("click", "remember")] == [True]
    assert len(sc.dta("click", "medium")) == 1


@pytest.mark.parametrize("name", ["counter", "counter_compose"])
def test_ten_taps_count_ten(scenario, name):
    sc = scenario(name)
    r = dc("run", *sum((["--step", "tap --text Increment"] for _ in range(10)), []))
    assert r["ok"] and r["failed"] == 0
    assert len(sc.dta("click", "inc")) == 10
    assert "Count: 10" in sc.snap()["text"]


@pytest.mark.parametrize("name", ["row_nested", "row_nested_compose"])
def test_row_and_its_star_are_separate_refs(scenario, name):
    sc = scenario(name)
    s = sc.snap()
    row = el(s, "Alan Turing · Re: the paper")
    star = next(e for e in s["elements"] if e["label"] == "Star" and abs(e["tap"][1] - row["tap"][1]) < 60)
    dc("tap", star["ref"])
    assert len(sc.dta("click", "star1")) == 1 and not sc.dta("click", "row1")
    dc("tap", row["ref"])
    assert len(sc.dta("click", "row1")) == 1 and len(sc.dta("click", "star1")) == 1


def test_long_press_opens_the_menu_and_tap_does_not(scenario):
    sc = scenario("long_press")
    dc("tap", "--text", "Hold me")
    assert not sc.dta("long_press")
    dc("long-press", "--text", "Hold me")
    assert len(sc.dta("long_press", "hold")) == 1
    dc("back")


def test_double_tap_likes(scenario):
    sc = scenario("double_tap")
    dc("tap", "--text", "Photo (double-tap to like)", "--double")
    assert until(lambda: sc.dta("double_tap", "photo"))
    assert len(sc.dta("double_tap", "photo")) == 1


def test_custom_actions_compose_delete_without_a_gesture(scenario):
    sc = scenario("custom_actions_compose")
    s = sc.snap()
    m = el(s, "Message 2")
    assert "actions=[Archive, Delete]" in s["text"]
    r = dc("action", m["ref"], "Delete")
    assert r["method"] == "action"
    ev = sc.dta("action", "message2")
    assert len(ev) == 1 and ev[0]["name"] == "Delete"
    assert not sc.dta("click")


def test_swipe_only_delete_has_no_actions_and_swipes(scenario):
    sc = scenario("swipe_only_delete")
    s = sc.snap()
    assert "actions=" not in s["text"]
    note = el(s, "Note 2")
    dc("swipe", "left", note["ref"])
    ev = until(lambda: sc.dta("swipe_delete"))
    assert len(ev) == 1 and ev[0]["item"] == "Note 2"


def test_slider_compose_set(scenario):
    sc = scenario("slider_compose")
    assert "range=3/10" in sc.snap()["text"]
    dc("set", "--text", "Volume", "--role", "seekbar", "7")
    assert until(lambda: sc.dta("change", "volume"))
    assert sc.dta("change", "volume")[-1]["value"] == 7


def test_spinner_and_popup_menu_are_separate_windows(scenario):
    sc = scenario("spinner_dropdown")
    dc("tap", "--text", "Apple", "--role", "dropdown")
    s = sc.snap()
    assert s["screen"]["dialog"] or any(e["label"] == "Banana" for e in s["elements"]), s["text"]
    banana = el(s, "Banana")
    dc("tap", banana["ref"])
    assert until(lambda: [e for e in sc.dta("select", "fruit") if e["value"] == "Banana"])
    dc("tap", "--text", "Menu")
    assert len(sc.dta("click", "menu")) == 1
    s = sc.snap()
    dc("tap", el(s, "Duplicate")["ref"])
    ev = until(lambda: sc.dta("menu", "menu"))
    assert ev and ev[-1]["item"] == "Duplicate"


def test_tabs_and_pager(scenario):
    sc = scenario("tabs_pager")
    s = sc.snap()
    assert "Page Gamma content" not in s["text"]          # offscreen pages are not listed
    dc("tap", "--text", "Beta", "--role", "tab")
    assert until(lambda: [e for e in sc.dta("page") if e["name"] == "Beta"])
    dc("swipe", "left")
    assert until(lambda: [e for e in sc.dta("page") if e["name"] == "Gamma"])


def test_drawer_overflow_and_bottom_nav(scenario):
    sc = scenario("bottom_nav_drawer")
    dc("tap", "--desc", "Open navigation drawer")
    assert len(sc.dta("click", "hamburger")) == 1
    dc("tap", "--text", "Sent")
    ev = until(lambda: sc.dta("nav", "drawer"))
    assert ev and ev[-1]["item"] == "Sent"
    dc("tap", "--desc", "More options")
    dc("tap", "--text", "Help")
    ev = until(lambda: sc.dta("menu", "overflow"))
    assert ev and ev[-1]["item"] == "Help"
    dc("tap", "--text", "Search", "--role", "tab")
    ev = until(lambda: sc.dta("nav", "bottom_nav"))
    assert ev and ev[-1]["item"] == "Search"


def test_canvas_is_opaque_and_tap_point_is_in_device_px(scenario):
    sc = scenario("canvas")
    s = sc.snap()
    assert any("opaque view" in w for w in s["warnings"]), s["text"]
    raw = dc("snapshot", "--raw")["raw"]
    W, H = raw["screen"]["w"], raw["screen"]["h"]
    # the game view has no a11y node at all; it fills the space between the bars
    bars = [w["bounds"] for w in raw["windows"] if w.get("pkg") == "com.android.systemui" and w.get("bounds")]
    top = max([b[3] for b in bars if b[1] <= 0 and b[3] < H // 4] or [0])
    bottom = min([b[1] for b in bars if b[3] >= H and b[1] > H * 3 // 4] or [H])
    l, t, r, b = 0, top, W, bottom
    w3 = (r - l) // 3
    x, y = l + w3 + w3 // 2, t + (b - t) // 3 + (w3 - 40) // 2      # centre of drawn button "B"
    dc("tap", "--point", f"{x},{y}")
    ev = until(lambda: sc.dta("tap"))
    assert ev and ev[0]["id"] == "B"
    assert abs(ev[0]["x"] - x) <= 2 and abs(ev[0]["y"] - y) <= 2


def test_virtual_views_are_listed_and_clickable(scenario):
    sc = scenario("virtual_views")
    s = sc.snap()
    assert "grid" in s["text"] and "October 14" in s["text"]
    dc("tap", "--text", "October 14")
    ev = until(lambda: sc.dta("click", "day14"))
    assert len(ev) == 1 and ev[0]["via"] == "a11y"


def test_drag_reorder_via_gesture_path_and_refs_heal(scenario):
    sc = scenario("drag_reorder")
    s = sc.snap()
    t1 = el(s, "Task 1")
    handles = [e for e in s["elements"] if e["label"] == "Drag handle"]
    h1 = min(handles, key=lambda e: abs(e["tap"][1] - t1["tap"][1]))
    t3 = el(s, "Task 3")
    x, y0 = h1["tap"]
    y1 = t3["tap"][1] + (t3["bounds"][3] - t3["bounds"][1]) // 2
    path = " ".join(f"{x},{int(y0 + (y1 - y0) * k / 8)}" for k in range(9))
    dc("gesture", "--path", path, "--ms", "1200")
    ev = until(lambda: sc.dta("reorder", "tasks"))
    assert ev and ev[-1]["order"][0] != "Task 1", ev
    task2_ref = el(s, "Task 2")["ref"]               # an old ref from before the reorder
    r = dc("tap", task2_ref, ok=None)
    assert r.get("ok") or (r.get("error") or {}).get("kind") in ("stale-ref",), r
