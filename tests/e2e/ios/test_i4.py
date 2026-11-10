"""I4 on a real iPhone: apps, launch (warm and cold), stop-app, open-url, shot --marks, mark and
logs through the daemon (the log history), home. The test app's DTA log and the phone's own
syslog are the ground truth. Run with DROIDCTL_IOS_UDID=<hardware UDID>, the phone unlocked."""
import os
import time

from tests.e2e.ios.test_i2 import APP, Dta, dc


def test_apps_lists_the_test_app_and_all_adds_system_apps():
    mine = dc("apps")["apps"]
    assert APP in mine and "com.apple.Preferences" not in mine
    assert "com.apple.Preferences" in dc("apps", "--all")["apps"]


def test_cold_launch_then_tap_logs_dta():
    d = Dta()
    d.since()
    r = dc("launch", APP, "--stop")
    assert r["pkg"] == APP and r["stopped"] and r["screen"]["pkg"] == APP and r["idle"]
    assert "app ready" in d.since()
    dc("tap", "--id", "dta_custom")
    assert d.since() == ["custom ax_activate"]


def test_stop_app_ends_the_process():
    dc("launch", APP)
    assert dc("stop-app", APP)["pids"]
    assert dc("stop-app", APP)["pids"] == []


def test_open_url_brings_safari_then_launch_returns():
    r = dc("open-url", "https://example.com")
    assert r["screen"]["pkg"] == "com.apple.mobilesafari"
    assert dc("launch", APP)["screen"]["pkg"] == APP


def test_shot_with_marks(tmp_path):
    dc("launch", APP)
    r = dc("shot", str(tmp_path / "s.jpg"), "--marks")
    assert r["source"] == "runner" and r["marks"] > 10 and (r["w"], r["h"]) == (585, 1266)
    assert os.path.getsize(r["path"]) > 10000


def test_marks_and_app_logs_through_the_daemon():
    dc("snapshot", daemon=True)                       # the daemon connects and starts its log ring
    time.sleep(1.0)
    label = f"e2e {time.time():.0f}"
    dc("mark", label, daemon=True)
    dc("tap", "--id", "dta_button", daemon=True)
    time.sleep(1.0)
    marks = dc("logs", "--marks", daemon=True)["lines"]
    assert any(label in x for x in marks)
    app = dc("logs", "--pkg", APP, "--max", "200", daemon=True)["lines"]
    assert any("DTA dta_button action" in x for x in app)


def test_home_screen_is_not_a_dialog():
    r = dc("home")
    assert r["screen"]["pkg"] == "com.apple.springboard" and not r["screen"]["dialog"]
    dc("launch", APP)
