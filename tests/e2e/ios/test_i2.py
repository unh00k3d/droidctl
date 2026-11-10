"""I2 on a real iPhone: actions, typing, the keyboard, settle and waits, judged by the test app's
own DTA log (Documents/dta.log, pulled with devicectl), never by droidctl's output.

Uses the S1 test app (research/ios-spike/TestApp, bundle dev.droidctl.s1.testapp), which must be
installed: a free team's 3-app limit leaves no room for a dedicated iOS test app yet.
Run with DROIDCTL_IOS_UDID=<hardware UDID>, the phone unlocked."""
import json
import os
import subprocess
import sys
import tempfile
import time

import pytest

from droidctl.ios import devices

UDID = os.environ.get("DROIDCTL_IOS_UDID")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
# the S1 research app until droidctl's own test app (ios/testapp, scripts/ios_testapp.py) has a slot;
# its Main tab is the same screen, so: DROIDCTL_IOS_TESTAPP=dev.droidctl.r<hash>.testapp
APP = os.environ.get("DROIDCTL_IOS_TESTAPP", "dev.droidctl.s1.testapp")


def dc(*args, daemon=False, ok=True, timeout=120):
    exe = [os.path.join(os.path.dirname(sys.executable), "droidctl")] if daemon else [sys.executable, "-m", "droidctl"]
    p = subprocess.run(exe + [*args, "-d", UDID, "--json"], capture_output=True, text=True, timeout=timeout, cwd=ROOT)
    r = json.loads(p.stdout)
    if ok:
        assert r.get("ok"), r
    return r


class Dta:
    """The test app's DTA lines, read from its data container."""

    def __init__(self):
        self.cdid = devices.find(UDID)["cdid"]
        self.mark = 0

    def lines(self):
        with tempfile.TemporaryDirectory() as d:
            dst = os.path.join(d, "dta.log")
            devices.devicectl("device", "copy", "from", "--device", self.cdid, "--domain-type", "appDataContainer",
                              "--domain-identifier", APP, "--source", "Documents/dta.log", "--destination", dst,
                              timeout=60)
            with open(dst) as f:
                return [ln.split(" ", 1)[1] for ln in f.read().splitlines() if " " in ln]

    def since(self):
        """The DTA events since the last call, without the "DTA " prefix."""
        all_ = self.lines()
        new, self.mark = all_[self.mark:], len(all_)
        return [x[4:] if x.startswith("DTA ") else x for x in new]


@pytest.fixture(scope="module")
def dta():
    from droidctl.ios import runner
    from droidctl.ios.client import IosClient, ios_settle
    c, _ = runner.connect(UDID)
    try:
        devices.launch(devices.find(UDID)["cdid"], APP, terminate_existing=True)
        time.sleep(2)
        c.call("launch", {"pkg": APP, "settle": ios_settle({})}, timeout=40)
    finally:
        c.close()
    d = Dta()
    d.since()
    dc("snapshot")
    return d


def test_a_uikit_button_is_touched_once_after_the_refused_press(dta):
    r = dc("tap", "--id", "dta_button")
    assert r["via"] == "touch" and r["idle"]
    assert dta.since() == ["dta_button ax_activate", "dta_button touch_began", "dta_button action"]


def test_a_custom_view_is_pressed_without_a_touch(dta):
    r = dc("tap", "--id", "dta_custom")
    assert r["via"] == "ax"
    assert dta.since() == ["custom ax_activate"]


def test_a_switch_toggles_once_per_tap_and_the_diff_shows_it(dta):
    before = dc("snapshot", "--full")
    on = any(e.get("id") == "dta_switch" and "on" in e["annotations"] for e in before["elements"])
    r = dc("tap", "--id", "dta_switch")
    want = 0 if on else 1
    assert dta.since() == [f"switch value={want}"]
    assert any("Switch" in line for line in r["diff"])


def test_swiftui_and_web_controls_take_the_ax_press(dta):
    assert dc("tap", "--text", "SwiftUI Button")["via"] == "ax"
    r = dc("tap", "--text", "Web Button")
    assert r["via"] == "ax" and r["idle"] and r["settle_ms"] < 1000
    assert dta.since() == ["sui_button action", "web button click"]
    # the settle must not empty the web view (idle requests sent to WebContent did, 2/2)
    assert dc("wait", "--text", "Web Checkbox", "--timeout", "3")["matched"]


def test_type_sets_the_value_and_hide_keyboard_returns(dta):
    r = dc("type", "--id", "dta_field", "hello")
    # keys first (2026-10-26): the app's own input handling sees each character, as when a person types
    assert r["verified"] and r["via"] == "keys" and r["screen"]["keyboard"]
    got = dta.since()
    assert got[-1] == "field text=hello" and "field text=h" in got
    snap = dc("snapshot", "--full")
    assert any(e.get("id") == "dta_field" and "focused" in e["annotations"] for e in snap["elements"])
    h = dc("hide-keyboard")
    assert h["via"] == "return_key" and not h["screen"]["keyboard"]
    assert dta.since() == ["field return"]
    assert dc("hide-keyboard")["already"]


def test_type_enter_submits(dta):
    r = dc("type", "--id", "dta_field", "abc", "--enter")
    assert r["verified"]
    got = dta.since()
    assert "field text=abc" in got and got[-1] == "field return"


def test_an_alert_is_a_dialog_and_its_button_dismisses_it(dta):
    r = dc("tap", "--text", "Alert")
    assert r["screen"]["dialog"] and r["new_screen"]
    assert dc("wait", "--text", "Pick one", "--timeout", "3")["matched"]
    r = dc("tap", "--text", "OK")
    assert not r["screen"]["dialog"]
    assert dta.since()[-1] == "alert ok"


def test_an_ax_scroll_settles_after_the_scroll_ended(dta):
    r = dc("scroll", "down")
    assert r["via"] == "ax"
    got = dta.since()
    assert got[-1].startswith("scroll end y=")
    # the status label shows "scroll end": the settled tree was read after the scroll animation (the 200 ms floor)
    assert any("scroll end" in line for line in r["diff"])
    dc("scroll", "up")
    dta.since()


def test_scroll_to_and_tap_a_far_row(dta):
    r = dc("scroll-to", "--text", "Item 40")
    assert r["found"]
    dc("tap", "--text", "Item 40")
    assert dta.since()[-1] == "table select row=40"


def test_long_press_holds_one_touch(dta):
    r = dc("long-press", "--id", "dta_button")
    assert r["via"] == "touch" and r["settle_ms"] >= 1000
    assert dta.since() == ["dta_button touch_began", "dta_button action"]


def test_the_same_tap_through_the_daemon(dta):
    r = dc("tap", "--id", "dta_custom", daemon=True)
    assert r["mode"] == "daemon" and r["via"] == "ax"
    assert dta.since() == ["custom ax_activate"]


def test_wait_times_out_typed():
    r = dc("wait", "--text", "nothing like this", "--timeout", "1", ok=False)
    assert r["error"]["kind"] == "timeout"


def test_press_other_keys_is_unsupported():
    assert dc("press", "tab", ok=False)["error"]["kind"] == "unsupported"
