"""Smoke e2e on real apps already on the phone. READ-ONLY by design: launch,
snapshot, navigate and go back; never type, log in, take photos, open documents
or touch account areas. Every test ends on the home screen. A test skips when
its package isn't installed."""
import os
import time

import pytest

from tests.e2e.conftest import adb, dc


def installed(pkg):
    return bool(adb("shell", "pm", "path", pkg, check=False).strip())


def until(fn, timeout=8):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.3)
    return fn()


@pytest.fixture
def app():
    def use(pkg):
        if not installed(pkg):
            pytest.skip(f"{pkg} is not installed")
        return pkg
    yield use
    dc("home", ok=None)
    assert until(lambda: dc("current", ok=None).get("pkg") == "com.sec.android.app.launcher")


def test_settings_row_then_back(app):
    pkg = app("com.android.settings")
    r = dc("launch", pkg, "--stop")
    assert r["ok"]
    s = dc("snapshot", "--full")
    assert s["screen"]["pkg"] == pkg and s["tokens_est"] < 2000
    t = dc("tap", "--text", "Display")
    assert t["changed"] and t["new_screen"]
    assert "Brightness" in dc("snapshot", "--full")["text"]
    b = dc("back")
    assert b["new_screen"]
    assert "Display" in dc("snapshot", "--full")["text"]


def test_launcher_launch_and_home(app):
    app("com.sec.android.app.launcher")
    dc("home")
    s = dc("snapshot", "--full")
    assert s["screen"]["pkg"] == "com.sec.android.app.launcher" and s["elements"]
    r = dc("launch", "com.android.settings")
    assert r["ok"] and dc("current")["pkg"] == "com.android.settings"


def test_sahibinden_snapshot_scroll_back(app):
    pkg = app("com.sahibinden")
    dc("launch", pkg, "--timeout", "20")
    s = until(lambda: (lambda x: x if x["screen"]["pkg"] == pkg and len(x["elements"]) > 5 else None)(
        dc("snapshot", "--full")), timeout=15)
    assert s, "sahibinden never showed its home screen"
    assert s["tokens_est"] < 2000
    r = dc("swipe", "up")
    assert r["ok"]
    dc("back", ok=None)


def test_google_docs_snapshot_and_back(app):
    pkg = app("com.google.android.apps.docs")
    dc("launch", pkg, "--timeout", "20")
    s = until(lambda: (lambda x: x if x["screen"]["pkg"] == pkg else None)(dc("snapshot", "--full")), timeout=15)
    assert s and s["tokens_est"] < 2000
    dc("back", ok=None)


def test_open_camera_snapshot_and_back(app):
    pkg = app("net.sourceforge.opencamera")
    dc("launch", pkg, "--timeout", "20")
    s = until(lambda: (lambda x: x if x["screen"]["pkg"] in (pkg, "com.google.android.packageinstaller") else None)(
        dc("snapshot", "--full")), timeout=15)
    assert s, "Open Camera never came up"
    assert s["tokens_est"] < 2000
    dc("back", ok=None)                     # also declines a permission prompt, never grants it


@pytest.mark.skipif(not os.environ.get("DROIDCTL_E2E_REFUSING_PKG"),
                    reason="set DROIDCTL_E2E_REFUSING_PKG to an installed app that refuses rooted phones")
def test_an_app_that_refuses_a_rooted_phone_is_handled_gracefully(app):
    """Some apps (banking apps with an app shield) kill themselves at start on a
    rooted phone, with or without our service. droidctl must report that cleanly: a
    typed error or a result, never a hang or a crash, and the app is not left in
    front. Nothing is ever typed or tapped in it."""
    from droidctl.core import ERROR_KINDS
    pkg = app(os.environ["DROIDCTL_E2E_REFUSING_PKG"])
    t0 = time.time()
    r = dc("launch", pkg, "--timeout", "15", ok=None, timeout=90)
    assert time.time() - t0 < 60
    assert r.get("ok") or r["error"]["kind"] in ERROR_KINDS, r
    assert until(lambda: dc("current", ok=None).get("pkg") != pkg, timeout=20), "the app is still in front"
    s = dc("snapshot", "--full")
    if s["screen"]["dialog"]:
        dc("back", ok=None)                 # only ever dismiss a crash dialog
