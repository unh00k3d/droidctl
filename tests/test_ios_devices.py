"""devicectl parsing against captured (scrubbed) devicectl JSON (tests/fixtures/ios-devicectl)."""
import json
import os

import pytest

from droidctl.core import UserError
from droidctl.ios import devices

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "ios-devicectl")


def load(name):
    with open(os.path.join(FIX, name + ".json")) as f:
        return json.load(f)


def test_list_keeps_physical_iphones_with_both_ids():
    ds = devices.parse_devices(devices.result_of(load("list_devices")))
    assert len(ds) == 2
    connected = [d for d in ds if d["connection"] == "connected"]
    assert len(connected) == 1
    d = connected[0]
    assert d["udid"].startswith("00008101-") and len(d["cdid"]) == 36
    assert (d["model"], d["os"], d["paired"], d["developer_mode"], d["transport"]) == \
        ("iPhone 12", "18.7.8", True, "enabled", "wired")
    assert [x["connection"] for x in ds if x is not d] == ["unavailable"]


def test_lock_state():
    assert devices.parse_lock_state(devices.result_of(load("lock_state_unlocked"))) == \
        {"locked": False, "unlocked_since_boot": True}


def test_apps():
    assert devices.parse_apps(devices.result_of(load("apps_one"))) == [
        {"bundle": "dev.droidctl.s1.testapp", "name": "droidctl S1", "version": "1.0", "build": "1", "developer": True,
         "system": False, "url": None}]
    assert devices.parse_apps(devices.result_of(load("apps_missing"))) == []


def test_a_failed_outcome_carries_devicectls_message():
    with pytest.raises(UserError) as e:
        devices.result_of(load("launch_missing"), ("device", "process", "launch"))
    assert e.value.kind == "device"
    assert "The application failed to launch. The requested application com.example.nothere is not installed." in str(e.value)
    assert e.value.data == {"domain": "com.apple.dt.CoreDeviceError", "code": 10002}


def test_developer_dir_prefers_the_env(monkeypatch):
    monkeypatch.setenv("DROIDCTL_DEVELOPER_DIR", "/X.app/Contents/Developer")
    assert devices.developer_dir() == "/X.app/Contents/Developer"


def test_no_xcode_is_missing_dep(monkeypatch):
    monkeypatch.setattr(devices, "developer_dir", lambda: None)
    with pytest.raises(UserError) as e:
        devices.xcode_env()
    assert e.value.kind == "missing-dep"
