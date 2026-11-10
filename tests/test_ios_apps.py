"""iOS I4: apps, launch, stop-app, install, open-url, shot, mark and logs. The host side against
FakeRunner and REAL captures: devicectl JSON (tests/fixtures/ios-devicectl, scrubbed) and the
phone's syslog lines from the test app and the runner (tests/fixtures/ios-host)."""
import json
import os

import pytest

from droidctl import act
from droidctl.core import UserError
from droidctl.ios import devices, syslog
from tests.test_ios_act import UDID, make_phone, run  # noqa: F401 (fixture)

DC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "ios-devicectl")
HOST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "ios-host")


def dc(name):
    with open(os.path.join(DC, name + ".json")) as f:
        return json.load(f)["result"]


def syslog_lines():
    with open(os.path.join(HOST, "syslog_testapp.txt")) as f:
        return f.read().splitlines()


TESTAPP = devices.parse_apps(dc("apps_testapp"))[0]
PROCS = devices.parse_processes(dc("processes"))


@pytest.fixture
def iphone(make_phone, monkeypatch):
    """FakeRunner for the runner; the real captured devicectl answers for everything devicectl does."""
    r = make_phone("testapp_main")
    calls = []
    monkeypatch.setattr(devices, "find", lambda udid: {"udid": udid, "cdid": "CDID"})
    monkeypatch.setattr(devices, "app", lambda cdid, b: TESTAPP if b == TESTAPP["bundle"] else None)
    monkeypatch.setattr(devices, "apps", lambda cdid, all_apps=False: [TESTAPP])
    monkeypatch.setattr(devices, "processes", lambda cdid: PROCS)
    monkeypatch.setattr(devices, "terminate", lambda cdid, pid: calls.append(("terminate", pid)))
    monkeypatch.setattr(devices, "install", lambda cdid, path: calls.append(("install", path)))
    r.devicectl = calls
    return r


# -- devicectl parsing on real output
def test_an_apps_processes_are_found_by_its_bundle_path():
    pids = devices.pids_of(TESTAPP, PROCS)
    assert len(pids) == 1
    assert TESTAPP["bundle"] == "dev.droidctl.s1.testapp" and not TESTAPP["system"]


# -- syslog on real lines
def test_syslog_lines_parse():
    entries = [syslog.parse(x) for x in syslog_lines()]
    assert all(entries)
    mark = next(e for e in entries if e["msg"].startswith("droidctl-mark"))
    assert mark["proc"] == "DroidctlRunner-Runner" and mark["level"] == "Notice"


def test_syslog_filters_by_app_level_and_marks():
    entries = [syslog.parse(x) for x in syslog_lines()]
    app = [e for e in entries if syslog.keep(e, proc="TestApp")]
    assert app and all(e["proc"] == "TestApp" for e in app)
    errors = [e for e in entries if syslog.keep(e, level="E")]
    assert errors and all(e["level"] in ("Error", "Fault") for e in errors)
    marks = [e for e in entries if syslog.keep(e, tags=["droidctl-mark"])]
    assert [e["msg"] for e in marks] == ["droidctl-mark fixture step"]


def test_relay_bytes_split_into_lines():
    raw = b"\x00".join(x.encode() + b"\n" for x in syslog_lines()[:3])
    lines, rest = syslog.split(raw + b"Oct 21 partial")
    assert lines == syslog_lines()[:3] and rest == b"Oct 21 partial"


# -- the commands
def test_launch_uses_the_runner_with_the_ios_settle(iphone):
    out = run("launch", "dev.droidctl.s1.testapp")
    assert iphone.params["launch"] == {"pkg": "dev.droidctl.s1.testapp", "settle": {"quiet_ms": 100, "timeout_ms": 3000}}
    assert out["pkg"] == "dev.droidctl.s1.testapp" and out["stopped"] == []


def test_launch_stop_terminates_the_running_process_first(iphone):
    out = run("launch", "dev.droidctl.s1.testapp", "--stop")
    assert out["stopped"] == devices.pids_of(TESTAPP, PROCS)
    assert iphone.devicectl == [("terminate", out["stopped"][0])]


@pytest.mark.parametrize("argv,kind", [
    (["launch", "com.example.none"], "not-found"),
    (["launch", "dev.droidctl.s1.testapp", "--clear"], "unsupported"),
    (["install", "/nonexistent/x.app"], "not-found"),
])
def test_ios_refusals(iphone, argv, kind):
    with pytest.raises(UserError) as e:
        run(*argv)
    assert e.value.kind == kind


def test_install_refuses_droidctls_own_runner_and_a_wrong_file(iphone, tmp_path):
    import plistlib
    app = tmp_path / "DroidctlRunner-Runner.app"
    app.mkdir()
    (app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "dev.droidctl.rae309727.runner.xctrunner"}))
    with pytest.raises(UserError) as e:
        run("install", str(app))
    assert e.value.kind == "bad-args" and "setup" in str(e.value)
    apk = tmp_path / "x.apk"
    apk.write_bytes(b"")
    with pytest.raises(UserError) as e:
        run("install", str(apk))
    assert e.value.kind == "bad-args"
    other = tmp_path / "Other.app"
    other.mkdir()
    (other / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.example.other"}))
    run("install", str(other))
    assert iphone.devicectl == [("install", str(other))]


def test_stop_app_and_apps(iphone):
    out = run("stop-app", "dev.droidctl.s1.testapp")
    assert out["pids"] and iphone.devicectl == [("terminate", out["pids"][0])]
    a = run("apps", "--filter", "S1")                  # by name too
    assert a["apps"] == ["dev.droidctl.s1.testapp"] and a["names"]["dev.droidctl.s1.testapp"] == "droidctl S1"


def test_open_url_and_mark_go_to_the_runner(iphone):
    run("open-url", "https://example.com")
    assert iphone.params["open_url"]["url"] == "https://example.com"
    run("mark", "step one")
    assert iphone.params["mark"] == {"label": "step one"}


def test_shot_with_marks_comes_from_the_runner(iphone, tmp_path):
    out = run("shot", str(tmp_path / "s.jpg"), "--marks")
    assert out["source"] == "runner" and (out["w"], out["h"]) == (585, 1266) and out["marks"] > 0
    assert iphone.params["screenshot"]["scale"] == 0.5


def test_logs_from_the_daemons_history_are_filtered(iphone, monkeypatch):
    from droidctl.ios.client import IosClient
    monkeypatch.setattr(IosClient, "log_history",
                        lambda self: {"lines": syslog_lines(), "since": "t", "alive": True, "error": None})
    out = run("logs", "--pkg", "dev.droidctl.s1.testapp", "--max", "500")
    assert out["lines"] and all(" TestApp(" in x for x in out["lines"])
    out = run("logs", "--marks")
    assert len(out["lines"]) == 1 and "droidctl-mark fixture step" in out["lines"][0]


def test_logs_without_the_daemon_listen_live(iphone, monkeypatch):
    monkeypatch.setattr(syslog, "listen", lambda udid, s: syslog_lines())
    out = run("logs", "--level", "E")
    assert out["lines"] and "live only" in out["note"]

