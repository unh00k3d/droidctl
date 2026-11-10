"""iOS: the runner client and lifecycle without a phone (FakeUsbmux -> FakeRunner), device
selection with iPhones, and xcodebuild failure classification on real output."""
import os

import pytest

from droidctl import device as dev
from droidctl.core import UserError
from droidctl.ios import runner
from droidctl.ios.client import IosClient
from tests.fakes import FakeAdb, FakeAgent, FakeRunner, FakeUsbmux

UDID = "00008101-00AAAAAAAAAAAAAA"
LOGS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "ios-runner", "logs")


@pytest.fixture
def phone(monkeypatch, tmp_path):
    r = FakeRunner()
    m = FakeUsbmux({UDID: {27100: r.port}})
    monkeypatch.setenv("USBMUXD_SOCKET_ADDRESS", m.address)
    monkeypatch.setenv("DROIDCTL_HOME", str(tmp_path))
    yield r
    m.close()
    r.close()


def test_is_ios():
    assert dev.is_ios("00008101-000238323AFA001E")
    assert dev.is_ios("a" * 40)
    assert not dev.is_ios("R5CR709S2XF") and not dev.is_ios("emulator-5554") and not dev.is_ios(None)


def test_the_client_maps_trees_and_keeps_the_native_one(phone):
    c = IosClient(UDID)
    try:
        assert c.call("ping")["backend"] == "ios-runner"
        raw = c.call("tree")
        assert "windows" in raw and "apps" not in raw
        assert "apps" in c.native_tree()
        assert c.clone().call("gen")["gen"] == 3
    finally:
        c.close()


def test_unimplemented_methods_are_unsupported(phone):
    c = IosClient(UDID)
    with pytest.raises(UserError) as e:
        c.call("clipboard", {})
    assert e.value.kind == "unsupported"


def test_connect_adopts_a_running_runner(phone):
    client, info = dev.connect(UDID)
    try:
        assert isinstance(client, IosClient) and info["build"] == "test-build"
    finally:
        client.close()
    assert dev.backend_for(UDID) == "ios"


def test_no_runner_and_no_setup_is_not_installed(monkeypatch, tmp_path):
    m = FakeUsbmux({UDID: {}})
    monkeypatch.setenv("USBMUXD_SOCKET_ADDRESS", m.address)
    monkeypatch.setenv("DROIDCTL_HOME", str(tmp_path))
    try:
        with pytest.raises(UserError) as e:
            dev.connect(UDID)
        assert e.value.kind == "not-installed" and "droidctl setup -d " + UDID in e.value.hint
    finally:
        m.close()


def test_iphones_join_the_device_list(monkeypatch, phone):
    agent = FakeAgent()
    adb = FakeAdb({"R5CR709S2XF": agent})
    monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", str(adb.port))
    try:
        assert dev.list_devices() == [("R5CR709S2XF", "device"), (UDID, "device")]
        with pytest.raises(UserError) as e:
            dev.resolve_serial(None)
        assert e.value.kind == "no-device"            # two devices: pick one
        assert dev.resolve_serial(UDID) == UDID
    finally:
        adb.close()
        agent.close()


def test_an_iphone_alone_needs_no_adb(monkeypatch, phone):
    monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", "1")
    monkeypatch.setattr(dev, "start_adb_server", lambda: False)
    assert dev.list_devices() == [(UDID, "device")]
    assert dev.resolve_serial(None) == UDID


def test_the_passcode_prompt_at_start_is_screen_off():
    with open(os.path.join(LOGS, "automation_timeout.log")) as f:
        kind, msg, hint, reason = runner.classify_output(f.read())
    assert (kind, reason) == ("screen-off", "locked-at-start")
    assert "passcode" in hint


@pytest.mark.parametrize("text,kind", [
    ("Unable to launch dev.x because it has an invalid code signature, inadequate entitlements or its profile "
     "has not been explicitly trusted by the user.", "not-trusted"),
    ("error: No Account for Team \"ABC\". Add a new account in Accounts settings", "signing"),
    ("Runner (12) encountered an error (Early unexpected exit, operation never finished bootstrapping)", "device"),
    ("everything fine", None),
])
def test_classify_output(text, kind):
    got = runner.classify_output(text)
    assert (got[0] if got else None) == kind


def test_build_ids_follow_sources_and_team():
    assert runner.build_id("TEAMA") != runner.build_id("TEAMB")
    assert runner.build_id("TEAMA") == runner.build_id("TEAMA")
    assert runner.bundle_prefix("TEAMA").startswith("dev.droidctl.r")


def test_the_free_team_app_limit_names_the_apps():
    with open(os.path.join(LOGS, "free_team_app_limit.log")) as f:
        text = f.read()
    assert runner.classify_output(text)[3] == "app-limit"
    assert runner.app_limit_bundles(text) == ["dev.droidctl.s1.testapp", "dev.droidctl.jl3.runner.xctrunner",
                                              "dev.droidctl.s1.runner.xctrunner"]
