"""I3 on a real iPhone: doctor, teardown (removes only droidctl's runner), then setup again.
Run with DROIDCTL_IOS_UDID=<hardware UDID>, the phone unlocked and set up once.
teardown leaves the S1 research apps on the phone, so the Trust stays and setup needs no tap."""
import os

from droidctl.ios import devices
from tests.e2e.ios.test_i2 import APP, dc

UDID = os.environ.get("DROIDCTL_IOS_UDID")


def droidctl_apps():
    cdid = devices.find(UDID)["cdid"]
    apps = devices.parse_apps(devices.devicectl("device", "info", "apps", "--device", cdid, timeout=60))
    return sorted(a["bundle"] for a in apps if a["bundle"].startswith("dev.droidctl."))


def test_doctor_is_green_with_the_runner_up():
    dc("snapshot")                                   # starts the runner if it is down
    r = dc("doctor")
    checks = {c["name"]: c for c in r["checks"]}
    assert r["ok"] and r["platform"] == "ios"
    assert checks["runner"]["ok"] and "answering" in checks["runner"]["detail"]
    assert checks["profile"]["ok"] and "expires" in checks["profile"]["detail"]
    assert any("Developer Mode" in x for x in r["not_undone_by_teardown"])


def test_teardown_removes_only_the_runner_and_setup_brings_it_back():
    before = droidctl_apps()
    r = dc("teardown")
    assert r["stopped"] and len(r["uninstalled"]) == 1 and r["uninstalled"][0].endswith(".runner.xctrunner")
    after = droidctl_apps()
    assert after == [b for b in before if b not in r["uninstalled"]]
    assert APP in after
    e = dc("snapshot", ok=False)
    assert e["error"]["kind"] == "not-installed"
    doc = dc("doctor", ok=False)
    assert not doc["ok"] and "not set up" in next(c["detail"] for c in doc["checks"] if c["name"] == "runner-build")
    s = dc("setup")
    assert s["ok"] and s["steps"][-1] == "start"
    assert dc("snapshot")["ok"]
