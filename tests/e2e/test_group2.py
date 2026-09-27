"""TESTAPP group 2: visibility, security, occlusion. Ground truth: DTA lines."""
import json
import time

from tests.e2e.conftest import dc


def kind(p):
    return (p.get("error") or {}).get("kind")


def labels(snap):
    return [e["label"] for e in snap["elements"]]


def test_flag_secure_shot_is_an_error_but_snapshot_works(scenario, tmp_path):
    sc = scenario("flag_secure")
    p = dc("shot", "--out", tmp_path / "s.jpg", ok=False)
    assert kind(p) == "secure-window", p
    assert not (tmp_path / "s.jpg").exists()     # never a black image passed off as a screenshot
    assert "Reveal" in labels(sc.snap())
    dc("tap", "--text", "Reveal")
    assert len(sc.dta("click", "reveal")) == 1


def test_sensitive_views_are_visible_below_api_34(scenario):
    """accessibilityDataSensitive needs API 34; on this API 28 phone the views are
    plain. The API 34 behaviour (isAccessibilityTool keeps them visible) is unverified."""
    sc = scenario("sensitive")
    s = sc.snap()
    assert "Pay" in labels(s) and any(l and l.startswith("Card ") for l in labels(s))
    dc("tap", "--text", "Pay")
    assert len(sc.dta("click", "pay")) == 1


def test_hidden_a11y_subtree_is_not_listed(scenario):
    """noHideDescendants removes the subtree from the platform's a11y tree itself,
    so no flag can show it (documented in AGENTS.md as a platform limit)."""
    sc = scenario("hidden_a11y")
    s = sc.snap()
    assert "Hidden action" not in labels(s) and "Visible" in labels(s)
    dc("tap", "--text", "Visible")
    assert len(sc.dta("click", "visible")) == 1


def test_password_value_is_never_printed(scenario):
    sc = scenario("password")
    r = dc("type", "--id", "password", "hunter22")
    blob = json.dumps(r)
    assert "hunter22" not in blob, blob
    assert r.get("value") is None and r.get("value_hidden")
    ev = sc.dta("text", "password")
    assert ev and ev[-1]["len"] == 8                 # ground truth: 8 chars went in
    s = sc.snap()
    assert "hunter22" not in s["text"]
    pw = next(e for e in s["elements"] if (e["id"] or "").endswith(":id/password"))
    assert "password" in pw["annotations"]


def test_overlay_blocker_is_occluded_but_action_works(scenario):
    sc = scenario("overlay_blocker")
    p = dc("tap", "--text", "Buy", ok=False)
    assert kind(p) == "occluded", p
    assert "blocker" in json.dumps(p["error"])     # names the covering node
    r = dc("tap", "--text", "Buy", "--method", "action")
    assert r["method"] == "action"
    assert len(sc.dta("click", "buy")) == 1 and not sc.dta("blocked")


def test_partial_drops_the_sliver_and_reaches_the_offscreen_one(scenario):
    sc = scenario("partial")
    s = sc.snap()
    assert "Five" not in labels(s) and "Thirty" in labels(s) and "Full" in labels(s)
    p = dc("tap", "--text", "Offscreen", ok=False)
    assert kind(p) in ("offscreen", "not-found"), p
    if kind(p) == "offscreen":
        assert p["error"].get("hint")
    dc("scroll-to", "--text", "Offscreen")
    dc("tap", "--text", "Offscreen")
    assert len(sc.dta("click", "offscreen")) == 1


def test_under_keyboard_gesture_is_occluded(scenario):
    sc = scenario("under_keyboard")
    deadline = time.time() + 5
    s = sc.snap()
    while not s["screen"]["keyboard"] and time.time() < deadline:
        time.sleep(0.3)
        s = sc.snap()
    assert s["screen"]["keyboard"]
    sub = next(e for e in s["elements"] if e["label"] == "Submit")
    assert "covered" in sub["annotations"]
    p = dc("tap", "--text", "Submit", "--method", "gesture", ok=False)
    assert kind(p) == "occluded", p
    assert not sc.dta("click", "submit")


def test_alpha_zero_invisible_and_gone_are_absent(scenario):
    sc = scenario("alpha_zero")
    ls = labels(sc.snap())
    assert "Invisible" not in ls and "Gone" not in ls
    assert "Ghost" not in ls           # the platform reports alpha 0 as not visible on API 28
    dc("tap", "--text", "Visible")
    assert len(sc.dta("click", "visible")) == 1


def test_zero_size_views_are_dropped(scenario):
    sc = scenario("zero_size")
    ls = labels(sc.snap())
    assert "Zero" not in ls and "One pixel" not in ls and "Normal" in ls
    dc("tap", "--text", "Normal")
    assert len(sc.dta("click", "normal")) == 1


def test_system_bars_tap_centres_avoid_the_bars(scenario):
    sc = scenario("system_bars")
    raw = dc("snapshot", "--raw")["raw"]
    bars = [w["bounds"] for w in raw["windows"] if w.get("pkg") == "com.android.systemui" and w.get("bounds")]
    s = sc.snap()
    rows = [e for e in s["elements"] if (e["label"] or "").startswith("Edge row")]
    assert rows
    for e in rows:
        x, y = e["tap"]
        for l, t, r, b in bars:
            assert not (l <= x < r and t <= y < b), (e, bars)
    last = rows[-1]
    dc("tap", last["ref"])
    got = sc.dta("click", "edge_row")
    assert len(got) == 1 and f"Edge row {got[0]['row']}" == last["label"]
