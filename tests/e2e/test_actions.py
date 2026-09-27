"""PLAN.md "Verification: e2e on the phone", against the test app. Truth = DTA lines."""
import struct
import subprocess
import time

import pytest

from tests.e2e.conftest import ADB, SERIAL, adb, dc


def test_tap_changes_the_screen_and_reports_method_action(scenario):
    sc = scenario("counter")
    ref = sc.ref("Increment")
    r = dc("tap", ref)
    assert r["changed"] is True and r["method"] == "action" and r["clicked_event"] is True
    assert any("Count: 1" in line for line in r["diff"])
    assert len(sc.dta("click")) == 1


def test_a_toggle_is_tapped_exactly_once(scenario):
    sc = scenario("toggle")
    r = dc("tap", "--role", "switch")
    assert r["method"] == "action"
    clicks = sc.dta("click", "wifi")
    assert len(clicks) == 1 and clicks[0]["state"] is True
    assert r["changed"]


def test_swipe_to_delete_via_the_custom_action(scenario):
    sc = scenario("custom_actions")
    ref = sc.ref("Message 2")
    r = dc("action", ref, "Delete")
    assert r["changed"]
    acts = sc.dta("action")
    assert len(acts) == 1 and acts[0]["name"] == "Delete" and acts[0]["id"] == "message2"
    assert not any(e["label"] == "Message 2" for e in sc.snap()["elements"])


@pytest.mark.parametrize("text", ["Merhaba dünya ğüşiöçĞÜŞİÖÇ 😀", "quote ' \" $HOME `x` \\ end"])
def test_unicode_type_reads_back(scenario, text):
    sc = scenario("unicode")
    r = dc("type", "--id", "field", "--stdin", stdin=text)
    assert r["value"] == text and r["verified"] is True
    assert r["method"] == "set_text"
    last = sc.dta("text")[-1]
    assert last["value"] == text


def test_a_ref_from_the_previous_screen_is_stale_not_a_lookalike(scenario):
    sc = scenario("lookalike_ok")
    s = sc.snap()
    ok = next(e for e in s["elements"] if e["label"] == "OK")
    x, y = ok["tap"]
    adb("shell", "input", "tap", str(x), str(y))           # navigate behind droidctl's back
    dc("wait", "--text", "Screen B", "--timeout", "5")
    r = dc("tap", ok["ref"], ok=False)
    assert r["ok"] is False and r["error"]["kind"] == "stale-ref"
    assert len(sc.dta("click")) == 1                        # only the adb tap on screen A


def test_a_button_under_a_transparent_overlay_is_occluded(scenario):
    sc = scenario("overlay_blocker")
    r = dc("tap", "--text", "Buy", ok=False)
    assert r["ok"] is False and r["error"]["kind"] == "occluded"
    assert not sc.dta("click", "buy")


def test_a_button_under_the_keyboard_is_clicked_by_action(scenario):
    sc = scenario("under_keyboard")
    dc("tap", "--id", "message")                            # tapping the field shows the IME
    for _ in range(20):
        if dc("current")["keyboard"]:
            break
        time.sleep(0.2)
    s = sc.snap()
    assert s["screen"]["keyboard"], "the keyboard never showed"
    sub = [e for e in s["elements"] if e["label"] == "Submit"]      # TESTAPP: listed, flagged covered
    assert len(sub) == 1 and "covered" in sub[0]["annotations"]
    r = dc("tap", "--text", "Submit")
    assert r["method"] == "action"
    assert len(sc.dta("click", "submit")) == 1


def test_touch_only_view_warns_then_gesture_taps_once(scenario):
    """ACTION_CLICK is 'performed' on any clickable view, even one that only
    reacts to touch. auto must not guess (see click_no_event), so it warns; the
    explicit --method gesture then taps exactly once."""
    sc = scenario("touch_only")
    r = dc("tap", "--text", "Touch me")
    assert r["method"] == "action" and "warning" in r and not r["changed"]
    assert len(sc.dta("tap")) == 0
    r = dc("tap", "--text", "Touch me", "--method", "gesture")
    assert r["method"] == "gesture"
    assert len(sc.dta("tap")) == 1


def test_click_no_event_runs_exactly_once(scenario):
    """PLAN open question, answered: a view that handles ACTION_CLICK silently (no
    event, no change) must not get a gesture on top; that ran its handler twice.
    auto now stops after the performed action and warns instead."""
    sc = scenario("click_no_event")
    r = dc("tap", "--text", "Silent")
    assert r["method"] == "action" and "warning" in r
    assert len(sc.dta("click")) == 1


def test_slider_set_value(scenario):
    sc = scenario("slider")
    r = dc("set", "--text", "Volume", "--role", "seekbar", "7")
    assert r["changed"]
    changes = sc.dta("change")
    assert changes and changes[-1].get("value") == 7


def test_long_press_opens_the_menu(scenario):
    sc = scenario("long_press")
    r = dc("long-press", "--text", "Hold me")
    assert r["method"] == "action"
    assert len(sc.dta("long_press")) == 1


def test_launch_and_wait(scenario):
    sc = scenario("counter")
    dc("home")
    r = dc("launch", "dev.droidctl.testapp")
    assert r["pkg"] == "dev.droidctl.testapp"
    cur = dc("current")
    assert cur["pkg"] == "dev.droidctl.testapp"
    w = dc("wait", "--pkg", "dev.droidctl.testapp", "--timeout", "3")
    assert w["matched"]
    del sc


def test_scroll_to_finds_a_far_row(scenario):
    scenario("long_list")
    r = dc("scroll-to", "--text", "Row 60")
    assert r["found"] and r["scrolls"] >= 1
    tap = dc("tap", r["ref"])
    assert tap["method"] == "action"


def test_toast_is_reported_and_waitable(scenario):
    sc = scenario("snackbar_toast")
    r = dc("tap", "--text", "Toast")
    w = dc("wait", "--toast", "Saved", "--timeout", "3", ok=None)
    assert r.get("toast") == "Saved!" or w.get("matched")
    del sc


def _png_size(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def test_shot_marks_agree_with_the_screen_under_the_resolution_override(scenario, tmp_path):
    scenario("cart")
    png = subprocess.run([ADB, "-s", SERIAL, "exec-out", "screencap", "-p"], capture_output=True,
                         check=True).stdout
    w, h = _png_size(png)
    ping = dc("ping")
    assert (w, h) == (ping["screen"]["w"], ping["screen"]["h"])   # 1080x2220 on the dev phone
    out = tmp_path / "s.jpg"
    r = dc("shot", "--marks", "--out", out)
    assert r["marks"] > 5 and out.exists()
    assert (r["w"], r["h"]) == (round(w * 0.5), round(h * 0.5))
    # the mark for "Checkout" sits where a tap at its centre lands (DTA)
    s = dc("snapshot", "--full")
    co = next(e for e in s["elements"] if e["label"] == "Checkout")
    sc = type("S", (), {})()
    adb("logcat", "-c")
    t = dc("tap", "--point", f"{co['tap'][0]},{co['tap'][1]}")
    assert t["method"] == "gesture"
    lines = adb("logcat", "-d", "-v", "raw", "DTA:I", "*:S")
    assert '"ev":"checkout"' in lines
    del sc


def test_back_is_a_global_action_and_its_dialog_is_in_the_diff(scenario):
    """TESTAPP back_confirm: the dialog is added ~100-200 ms after back with a quiet
    gap in between; settle must not return on the frame without it. Repeated,
    because it is a race."""
    for _ in range(5):
        sc = scenario("back_confirm")
        sc.snap()                     # the agent looks first; back's diff is against what it saw
        r = dc("back")
        assert r["method"] == "back"
        assert sc.dta("back")
        assert any("Exit?" in line or "Stay" in line for line in r["diff"]), r["diff"]


def test_a_huge_tree_is_a_partial_read_of_the_current_screen(scenario):
    """TESTAPP huge_tree (5,000 nodes, 100 deep): over the 2 s budget the agent
    returns what it read of THIS screen, marked degraded, never an older tree."""
    scenario("huge_tree")
    t0 = time.monotonic()
    s = dc("snapshot", timeout=60)
    assert time.monotonic() - t0 < 6
    assert 's:huge_tree' in s["text"] or "Huge tree" in s["text"]
    assert s.get("degraded") or "degraded=" in s["text"]
    assert "degraded tree (" in s["text"]


def test_a_blocked_accessibility_provider_is_flagged_within_the_budget(scenario):
    """TESTAPP slow_a11y: the app's provider sleeps 5 s on its UI thread; the dump
    must not wait for it, and says the app window has no tree."""
    scenario("slow_a11y")
    t0 = time.monotonic()
    s = dc("snapshot", timeout=60)
    assert time.monotonic() - t0 < 4.5
    assert "degraded=no-root" in s["text"] and "no tree for app window" in s["text"]
