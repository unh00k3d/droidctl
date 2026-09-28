"""Backend B (UiAutomation over adb, PLAN.md "Device backends") on the phone.

The same host code drives the test app through the agent run by app_process instead
of the enabled service. Ground truth is DTA logcat, as everywhere. The module pins
DROIDCTL_BACKEND=uiautomation for every droidctl call and restores the phone after.
"""
import os
import time

import pytest

from tests.e2e.conftest import adb, dc
from tests.e2e.test_group3 import el, until

UA_PATTERN = "'dev.droidctl.agent.[S]hellMain'"   # never matches its own shell


def ua_pids():
    return [p for p in adb("shell", f"pgrep -f {UA_PATTERN}", check=False).split() if p.isdigit()]


def secure(key):
    return adb("shell", "settings", "get", "secure", key).strip()


@pytest.fixture(scope="module", autouse=True)
def backend_b():
    before = (secure("enabled_accessibility_services"), secure("accessibility_enabled"))
    old = os.environ.get("DROIDCTL_BACKEND")
    os.environ["DROIDCTL_BACKEND"] = "uiautomation"
    try:
        yield
    finally:
        dc("teardown", "--backend", "uiautomation", ok=None)
        if old is None:
            os.environ.pop("DROIDCTL_BACKEND", None)
        else:
            os.environ["DROIDCTL_BACKEND"] = old
        # backend B never touches the accessibility settings
        assert (secure("enabled_accessibility_services"), secure("accessibility_enabled")) == before


def test_setup_starts_the_agent_without_installing_or_enabling_anything():
    services = secure("enabled_accessibility_services")
    r = dc("setup", "--backend", "uiautomation")
    assert r["backend"] == "uiautomation" and r["agent"]["backend"] == "uiautomation"
    assert r["agent"]["peer_uid"] == 2000
    assert ua_pids()
    assert secure("enabled_accessibility_services") == services
    p = dc("ping")
    assert p["backend"] == "uiautomation" and p["versionCode"] >= 8


def test_doctor_reports_the_backend():
    r = dc("doctor", ok=None)
    names = {c["name"]: c for c in r["checks"]}
    assert r["backend"] == "uiautomation" and names["backend"]["detail"].startswith("uiautomation")
    assert names["socket"]["ok"] and names["peer-uid"]["ok"] and names["protocol"]["ok"], r


def test_tap_by_node_action(scenario):
    sc = scenario("counter")
    r = dc("tap", sc.ref("Increment"))
    assert r["method"] == "action" and r["changed"] and r["clicked_event"] is True
    assert len(sc.dta("click")) == 1


def test_gesture_tap_is_an_injected_touch(scenario):
    """touch_only reacts to real touches only: the injected MotionEvents must land once."""
    sc = scenario("touch_only")
    r = dc("tap", "--text", "Touch me", "--method", "gesture")
    assert r["method"] == "gesture"
    assert len(until(lambda: sc.dta("tap"))) == 1


def test_injected_swipe_deletes(scenario):
    sc = scenario("swipe_only_delete")
    dc("swipe", "left", el(sc.snap(), "Note 2")["ref"])
    ev = until(lambda: sc.dta("swipe_delete"))
    assert len(ev) == 1 and ev[0]["item"] == "Note 2"


def test_injected_path_drags(scenario):
    sc = scenario("drag_reorder")
    s = sc.snap()
    t1, t3 = el(s, "Task 1"), el(s, "Task 3")
    h1 = min((e for e in s["elements"] if e["label"] == "Drag handle"), key=lambda e: abs(e["tap"][1] - t1["tap"][1]))
    x, y0 = h1["tap"]
    y1 = t3["tap"][1] + (t3["bounds"][3] - t3["bounds"][1]) // 2
    dc("gesture", "--path", " ".join(f"{x},{int(y0 + (y1 - y0) * k / 8)}" for k in range(9)), "--ms", "1200")
    ev = until(lambda: sc.dta("reorder", "tasks"))
    assert ev and ev[-1]["order"][0] != "Task 1", ev


def test_long_press_and_unicode_type(scenario):
    sc = scenario("long_press")
    dc("long-press", "--text", "Hold me")
    assert len(until(lambda: sc.dta("long_press"))) == 1
    sc = scenario("unicode")
    text = "Merhaba dünya ğüşiöçĞÜŞİÖÇ 😀"
    r = dc("type", "--id", "field", "--stdin", stdin=text)
    assert r["value"] == text and sc.dta("text")[-1]["value"] == text


def test_events_arrive_through_the_uiautomation_listener(scenario):
    sc = scenario("snackbar_toast")
    r = dc("tap", "--text", "Toast")
    w = dc("wait", "--toast", "Saved", "--timeout", "3", ok=None)
    assert r.get("toast") == "Saved!" or w.get("matched"), (r, w)


def test_screenshot_falls_back_to_screencap():
    r = dc("shot", "--out", "/tmp/droidctl-ua-shot.jpg")
    assert r["source"] == "screencap"


def test_a_killed_agent_is_relaunched_by_the_next_command(scenario):
    """Hard part 1: the process dies (reboot, OEM killer); the next call brings it back."""
    adb("shell", f"pkill -f {UA_PATTERN}", check=False)
    assert until(lambda: not ua_pids(), 3) is True or not ua_pids()
    sc = scenario("counter")                     # its `wait` relaunches the agent
    assert ua_pids()
    dc("tap", sc.ref("Increment"))
    assert len(sc.dta("click")) == 1


def test_it_works_with_the_a11y_service_disabled(scenario):
    """The reason backend B exists: apps that hide from an enabled service. With
    droidctl's service off, B still sees and drives the UI."""
    dc("teardown", "--backend", "a11y", "--keep-apk")
    try:
        assert "dev.droidctl.agent" not in secure("enabled_accessibility_services")
        sc = scenario("counter")
        dc("tap", sc.ref("Increment"))
        assert len(sc.dta("click")) == 1
    finally:
        os.environ["DROIDCTL_BACKEND"] = "a11y"
        try:
            dc("setup", "--backend", "a11y")
        finally:
            os.environ["DROIDCTL_BACKEND"] = "uiautomation"
    dc("setup", "--backend", "uiautomation")


def test_teardown_leaves_nothing_behind():
    r = dc("teardown", "--backend", "uiautomation")
    assert r["uiautomation"]["removed"]
    time.sleep(0.3)
    assert not ua_pids()
    assert "droidctl-ua" not in adb("shell", "ls /data/local/tmp")
    assert "droidctl_ua" not in adb("forward", "--list")
