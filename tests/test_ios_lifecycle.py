"""iOS: the guided setup's checklist and steps, the runner's start/connect, teardown, doctor, and
the syslog relay sharing the iPhone with Xcode. Everything outside droidctl (xcodebuild,
devicectl, lockdown) is faked here; the same paths run on the phone in tests/e2e/ios."""
import os
import plistlib
import socket
import struct
import threading
import time

import pytest

from droidctl import device as dev
from droidctl.core import UserError
from droidctl.ios import devices, lockdown, manage, runner, setup, syslog, transport
from tests.fakes import FakeRunner, FakeUsbmux

UDID = "00008101-00AAAAAAAAAAAAAA"
TEAM = "TEAMID0000"
PHONE = {"udid": UDID, "cdid": "CD-1", "model": "iPhone 12", "os": "18.7.8", "developer_mode": "enabled"}


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("DROIDCTL_HOME", str(tmp_path))
    return tmp_path


def ctx(team=TEAM, ask=None):
    return setup.Ctx(UDID, team, lambda line: None, ask)


# --------------------------------------------------------------------------
# the checklist loop
# --------------------------------------------------------------------------
def fake_steps(monkeypatch, fail_times):
    """Two steps; the second needs the user `fail_times` times before it passes."""
    left = {"n": fail_times}

    def ok(c):
        return "fine"

    def needs(c):
        if left["n"]:
            left["n"] -= 1
            raise setup.Need("screen-off", "the iPhone is locked", ["Unlock it."], "locked")
        return "unlocked"
    monkeypatch.setattr(setup, "STEPS", [("one", "first", ok), ("two", "second", needs)])
    monkeypatch.setattr(setup, "POLL_S", 0.0)
    monkeypatch.setattr(setup, "profile_expiry", lambda t: None)


def test_non_interactive_setup_stops_with_the_step_and_instructions(monkeypatch):
    fake_steps(monkeypatch, 1)
    with pytest.raises(UserError) as e:
        setup.run(UDID, TEAM, rerun="droidctl setup -d X")
    assert e.value.kind == "screen-off"
    assert e.value.data == {"step": "two", "reason": "locked", "instructions": ["Unlock it."],
                            "rerun": "droidctl setup -d X", "done": ["one"]}
    assert "Unlock it." in e.value.hint and "droidctl setup -d X" in e.value.hint


def test_interactive_setup_says_what_to_do_once_and_carries_on_by_itself(monkeypatch):
    fake_steps(monkeypatch, 3)
    said = []
    out = setup.run(UDID, TEAM, interactive=True, say=said.append)
    assert out["ok"] and out["steps"] == ["one", "two"]
    assert sum("Unlock it." in s for s in said) == 1             # retried 3 times, told once
    assert said[-1] == "✓ [2/2] second: unlocked"


def test_interactive_setup_gives_up_after_the_step_timeout(monkeypatch):
    fake_steps(monkeypatch, 10 ** 6)
    monkeypatch.setattr(setup, "STEP_TIMEOUT_S", 0.05)
    with pytest.raises(UserError) as e:
        setup.run(UDID, TEAM, interactive=True, say=lambda s: None)
    assert e.value.kind == "timeout" and e.value.data["step"] == "two"


# --------------------------------------------------------------------------
# the steps
# --------------------------------------------------------------------------
class Proc:
    def __init__(self, rc, out):
        self.returncode, self.stdout, self.stderr = rc, out, ""


@pytest.mark.parametrize("rc,out,reason", [
    (69, "You have not agreed to the Xcode license agreements.", "xcode-license"),
    (1, "xcodebuild: error: plug-in failed to load", "xcode-first-launch"),
])
def test_xcode_problems_say_the_one_command_that_fixes_them(monkeypatch, rc, out, reason):
    monkeypatch.setattr(devices, "developer_dir", lambda: "/X.app/Contents/Developer")
    monkeypatch.setattr(setup.subprocess, "run", lambda *a, **k: Proc(rc, out))
    with pytest.raises(setup.Need) as e:
        setup.step_xcode(ctx())
    assert e.value.reason == reason


def test_no_xcode_says_to_install_it(monkeypatch):
    monkeypatch.setattr(devices, "developer_dir", lambda: None)
    with pytest.raises(setup.Need) as e:
        setup.step_xcode(ctx())
    assert e.value.reason == "no-xcode" and "App Store" in e.value.instructions[0]


def attach(monkeypatch, phones, paired=True):
    monkeypatch.setattr(transport, "list_devices", lambda timeout=3.0: [{"udid": u} for u in phones])
    monkeypatch.setattr(transport, "is_paired", lambda u: paired)
    monkeypatch.setattr(devices, "list_devices", lambda: [dict(PHONE, udid=u) for u in phones])
    popened = []
    monkeypatch.setattr(setup.subprocess, "Popen", lambda cmd, **k: popened.append(cmd))
    monkeypatch.setattr(devices, "xcode_env", lambda: {})
    return popened


def test_the_one_attached_iphone_is_picked_and_several_need_a_choice(monkeypatch):
    attach(monkeypatch, [UDID])
    c = setup.Ctx(None, None, lambda s: None)
    assert setup.step_attached(c) == UDID and c.udid == UDID
    attach(monkeypatch, [UDID, "00008101-00BBBBBBBBBBBBBB"])
    with pytest.raises(UserError) as e:
        setup.step_attached(setup.Ctx(None, None, lambda s: None))
    assert e.value.kind == "no-device" and "-d <UDID>" in e.value.hint


def test_no_iphone_says_to_connect_and_unlock(monkeypatch):
    attach(monkeypatch, [])
    with pytest.raises(setup.Need) as e:
        setup.step_attached(ctx())
    assert e.value.reason == "not-attached"


def test_an_unpaired_iphone_is_asked_for_trust_once(monkeypatch):
    popened = attach(monkeypatch, [UDID], paired=False)
    c = ctx()
    for _ in range(3):
        with pytest.raises(setup.Need) as e:
            setup.step_attached(c)
    assert e.value.reason == "not-paired"
    assert popened == [["xcrun", "devicectl", "manage", "pair", "--device", "CD-1", "-q"]]


def test_developer_mode_off_reveals_the_switch_once(monkeypatch):
    monkeypatch.setattr(devices, "list_devices", lambda: [dict(PHONE, developer_mode="disabled")])
    revealed = []
    monkeypatch.setattr(setup, "reveal_developer_mode", revealed.append)
    c = ctx()
    for _ in range(3):
        with pytest.raises(setup.Need) as e:
            setup.step_developer_mode(c)
    assert e.value.reason == "developer-mode" and revealed == [UDID]
    assert "Privacy & Security" in e.value.instructions[0]


def test_developer_mode_on_reports_the_phone_and_old_ios_is_unsupported(monkeypatch):
    monkeypatch.setattr(devices, "list_devices", lambda: [PHONE])
    c = ctx()
    assert setup.step_developer_mode(c) == "iPhone 12 on iOS 18.7.8" and c.cdid == "CD-1"
    monkeypatch.setattr(devices, "list_devices", lambda: [dict(PHONE, os="16.7")])
    with pytest.raises(UserError) as e:
        setup.step_developer_mode(ctx())
    assert e.value.kind == "unsupported"


def test_a_named_team_must_be_signed_in(monkeypatch):
    monkeypatch.setattr(setup, "all_teams", lambda: [{"team": TEAM, "name": "A", "free": True}])
    assert setup.step_team(ctx(team=TEAM)) == TEAM
    with pytest.raises(UserError) as e:
        setup.step_team(ctx(team="OTHERTEAM1"))
    assert e.value.kind == "signing" and TEAM in e.value.hint


def test_a_locked_iphone_waits_for_unlock(monkeypatch):
    monkeypatch.setattr(devices, "lock_state", lambda cdid: {"locked": True, "unlocked_since_boot": True})
    with pytest.raises(setup.Need) as e:
        setup.step_unlocked(ctx())
    assert e.value.reason == "locked"


def test_a_build_refused_for_signing_becomes_a_need(monkeypatch):
    monkeypatch.setattr(setup, "profile_expiry", lambda t: None)

    def fail(u, t, resign=False):
        raise UserError("Xcode can't sign", "signing", hint="sign in", data={"reason": "signing"})
    monkeypatch.setattr(runner, "build", fail)
    with pytest.raises(setup.Need) as e:
        setup.step_build(ctx())
    assert e.value.kind == "signing" and e.value.instructions == ["sign in"]


def test_a_fresh_build_records_the_build_id(monkeypatch):
    monkeypatch.setattr(setup, "profile_expiry", lambda t: None)
    monkeypatch.setattr(runner, "build", lambda u, t, resign=False: None)
    assert setup.step_build(ctx()) == f"build {runner.build_id(TEAM)}"
    assert dev.device_state(UDID)["built"] == runner.build_id(TEAM)


def app_limit(apps):
    return UserError("3 apps", "signing", data={"reason": "app-limit", "apps": apps})


def test_the_app_limit_frees_only_droidctls_older_runners(monkeypatch):
    old = runner.runner_bundle("OTHERTEAM1")
    starts, removed = [], []

    def start(u, t):
        starts.append(t)
        if len(starts) == 1:
            raise app_limit([old, "dev.droidctl.s1.testapp", "com.example.app"])
        return {"version": "0.2.0", "build": "b"}
    monkeypatch.setattr(runner, "start", start)
    monkeypatch.setattr(devices, "uninstall", lambda cdid, b: removed.append(b))
    c = ctx()
    assert "answering" in setup.step_start(c)
    assert removed == [old] and len(starts) == 2


def test_the_app_limit_with_none_of_ours_asks_the_user_to_remove_an_app(monkeypatch):
    monkeypatch.setattr(runner, "start", lambda u, t: (_ for _ in ()).throw(app_limit(["com.example.app"])))
    removed = []
    monkeypatch.setattr(devices, "uninstall", lambda cdid, b: removed.append(b))
    with pytest.raises(setup.Need) as e:
        setup.step_start(ctx())
    assert e.value.reason == "app-limit" and "com.example.app" in e.value.instructions[0] and removed == []


def test_an_untrusted_profile_opens_the_settings_page_once(monkeypatch):
    def start(u, t):
        raise UserError("not trusted", "not-trusted", data={"reason": "untrusted"})
    monkeypatch.setattr(runner, "start", start)
    opened = []
    monkeypatch.setattr(setup, "open_settings", lambda cdid, url: opened.append(url))
    c = ctx()
    c.cdid = "CD-1"
    for _ in range(3):
        with pytest.raises(setup.Need) as e:
            setup.step_start(c)
    assert e.value.reason == "untrusted" and opened == [setup.TRUST_PAGE]


def test_the_settings_page_is_opened_through_devicectl(monkeypatch):
    calls = []
    monkeypatch.setattr(devices, "devicectl", lambda *a, **k: calls.append(a))
    setup.open_settings("CD-1", setup.TRUST_PAGE)
    assert calls == [("device", "process", "launch", "--device", "CD-1", "--terminate-existing",
                      "--payload-url", setup.TRUST_PAGE, "com.apple.Preferences")]


def test_without_xcode_settings_opens_at_its_main_screen(monkeypatch):
    """go-ios can't pass Settings a URL: launch it fresh, and the instructions start from there."""
    monkeypatch.setenv("DROIDCTL_IOS_BACKEND", "goios")
    calls = []
    monkeypatch.setattr(devices, "devicectl", lambda *a, **k: pytest.fail("devicectl without Xcode"))
    monkeypatch.setattr(devices, "launch", lambda cdid, bundle, terminate_existing=False:
                        calls.append((cdid, bundle, terminate_existing)))
    setup.open_settings("UDID-1", setup.TRUST_PAGE)
    assert calls == [("UDID-1", "com.apple.Preferences", True)]


# --------------------------------------------------------------------------
# the runner's start and connect, against FakeRunner through FakeUsbmux
# --------------------------------------------------------------------------
@pytest.fixture
def phone(monkeypatch):
    made = []

    def make(build=None):
        r = FakeRunner("testapp_main", build=build or runner.build_id(TEAM))
        m = FakeUsbmux({UDID: {runner.DEFAULT_PORT: r.port}})
        monkeypatch.setenv("USBMUXD_SOCKET_ADDRESS", m.address)
        made.append((m, r))
        return r
    yield make
    for m, r in made:
        m.close()
        r.close()


class Holder:
    """Stands in for the detached xcodebuild session: writes `log` to its output, then runs or exits."""
    started = []

    def __init__(self, cmd, stdout=None, exits=False, log="", **k):
        Holder.started.append(cmd)
        stdout.write(log)
        stdout.flush()
        self.pid, self._exits = 999999, exits

    def poll(self):
        return 1 if self._exits else None


def fake_xcode(monkeypatch, **holder):
    Holder.started = []
    seen_busy = []

    def popen(cmd, **k):
        seen_busy.append(syslog.xcode_is_busy(UDID))     # logs must have let go of the iPhone
        return Holder(cmd, exits=holder.get("exits", False), log=holder.get("log", ""), **k)
    monkeypatch.setattr(runner, "xctestrun", lambda t: "/x/r.xctestrun")
    monkeypatch.setattr(devices, "xcode_env", lambda: {})
    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    return seen_busy


def test_start_waits_for_our_build_with_logs_off_the_iphone(monkeypatch, phone):
    phone()
    busy = fake_xcode(monkeypatch)
    info = runner.start(UDID, TEAM, timeout=5)
    assert info["build"] == runner.build_id(TEAM) and busy == [True]
    assert not syslog.xcode_is_busy(UDID)                  # released afterwards
    st = dev.device_state(UDID)
    assert st["holder_pid"] == 999999 and st["bundle"] == runner.runner_bundle(TEAM)
    assert "-only-testing:" + runner.TEST in Holder.started[0]


def test_a_stale_runner_answering_is_refused(monkeypatch, phone):
    phone(build="old-build")
    fake_xcode(monkeypatch)
    with pytest.raises(UserError) as e:
        runner.start(UDID, TEAM, timeout=5)
    assert e.value.data["reason"] == "stale-build"


@pytest.mark.parametrize("log,kind,reason", [
    ("Testing failed: ... has not been explicitly trusted by the user", "not-trusted", "untrusted"),
    ("error: Timed out while enabling automation mode.", "screen-off", "locked-at-start"),
])
def test_a_failed_start_is_typed_from_xcodebuilds_output(monkeypatch, log, kind, reason):
    monkeypatch.setenv("USBMUXD_SOCKET_ADDRESS", FakeUsbmux({UDID: {}}).address)   # nothing answers
    fake_xcode(monkeypatch, exits=True, log=log)
    with pytest.raises(UserError) as e:
        runner.start(UDID, TEAM, timeout=5)
    assert e.value.kind == kind and e.value.data["reason"] == reason


def test_connect_adopts_a_running_runner_without_starting_one(monkeypatch, phone):
    phone()
    dev.update_device_state(UDID, backend="ios", team=TEAM, build_id=runner.build_id(TEAM))
    monkeypatch.setattr(runner, "start", lambda *a, **k: pytest.fail("started"))
    c, info = runner.connect(UDID)
    c.close()
    assert info["build"] == runner.build_id(TEAM)


def test_connect_without_setup_says_to_run_setup(monkeypatch):
    monkeypatch.setenv("USBMUXD_SOCKET_ADDRESS", FakeUsbmux({UDID: {}}).address)
    with pytest.raises(UserError) as e:
        runner.connect(UDID, timeout=1)
    assert e.value.kind == "not-installed" and "droidctl setup" in e.value.hint


def test_build_failures_are_typed_from_the_log(monkeypatch):
    monkeypatch.setattr(devices, "xcode_env", lambda: {})

    def run(cmd, stdout=None, **k):
        stdout.write('error: No Account for Team "X".\n')
        return Proc(65, "")
    monkeypatch.setattr(runner.subprocess, "run", run)
    with pytest.raises(UserError) as e:
        runner.build(UDID, TEAM)
    assert e.value.kind == "signing" and e.value.data["reason"] == "signing"


# --------------------------------------------------------------------------
# teardown and doctor
# --------------------------------------------------------------------------
def installed(monkeypatch, bundles):
    removed = []
    monkeypatch.setattr(runner, "stop", lambda u, quiet=False: {"stopped": True})
    monkeypatch.setattr(devices, "find", lambda u: PHONE)
    monkeypatch.setattr(devices, "devicectl", lambda *a, **k: {"apps": [
        {"bundleIdentifier": b, "builtByDeveloper": True, "removable": True} for b in bundles]})
    monkeypatch.setattr(devices, "uninstall", lambda cdid, b: removed.append(b))
    return removed


def test_teardown_removes_our_runner_and_says_the_trust_is_gone_when_nothing_else_is_left(monkeypatch):
    ours = runner.runner_bundle(TEAM)
    removed = installed(monkeypatch, [ours])
    dev.update_device_state(UDID, backend="ios", team=TEAM)
    out = manage.teardown(UDID)
    assert removed == [ours] and out["uninstalled"] == [ours]
    assert "Trust for your developer certificate is gone" in " ".join(out["not_undone"])
    assert out["build_cache"] == runner.derived_data(TEAM) and not dev.device_state(UDID)


def test_teardown_keeps_other_developer_apps_and_keep_app_keeps_ours(monkeypatch):
    ours = runner.runner_bundle(TEAM)
    removed = installed(monkeypatch, [ours, "dev.droidctl.s1.testapp"])
    out = manage.teardown(UDID)
    assert removed == [ours] and "Other developer apps remain (1)" in " ".join(out["not_undone"])
    removed = installed(monkeypatch, [ours])
    out = manage.teardown(UDID, keep_app=True)
    assert removed == [] and out["kept"] and "unchanged" in " ".join(out["not_undone"])


class Doctor:
    def __init__(self):
        self.results, self.skipped = {}, []

    def check(self, name, fn):
        ok, detail = fn()
        self.results[name] = (ok, detail)
        return ok

    def skip(self, *names):
        self.skipped += names


def test_doctor_without_xcode_skips_everything_after(monkeypatch):
    monkeypatch.setattr(devices, "developer_dir", lambda: None)
    d = Doctor()
    manage.doctor(UDID, d.check, d.skip)
    assert d.results["xcode"][0] is False and "App Store" in d.results["xcode"][1]
    assert d.skipped[0] == "device" and "runner" in d.skipped


def test_doctor_on_a_set_up_phone_with_the_runner_stopped(monkeypatch):
    monkeypatch.setattr(setup, "step_xcode", lambda c: "Xcode 26.2")
    monkeypatch.setattr(setup, "step_attached", lambda c: UDID)
    monkeypatch.setattr(devices, "list_devices", lambda: [PHONE])
    monkeypatch.setattr(setup, "all_teams", lambda: [{"team": TEAM, "name": "A", "free": True}])
    monkeypatch.setattr(devices, "lock_state", lambda cdid: {"locked": False, "unlocked_since_boot": True})
    monkeypatch.setattr(runner, "xctestrun", lambda t: "/x/r.xctestrun")
    monkeypatch.setattr(setup, "profile_expiry", lambda t: "2999-01-01T00:00:00Z")
    monkeypatch.setattr(manage, "_run", lambda cmd: "")
    monkeypatch.setenv("USBMUXD_SOCKET_ADDRESS", FakeUsbmux({UDID: {}}).address)
    dev.update_device_state(UDID, backend="ios", team=TEAM, built=runner.build_id(TEAM))
    d = Doctor()
    out = manage.doctor(UDID, d.check, d.skip)
    assert all(ok is not False for ok, _ in d.results.values()), d.results
    assert "not running" in d.results["runner"][1] and "verified" in d.results["pair"][1]
    assert out["platform"] == "ios" and not d.skipped


def test_doctor_says_when_the_sources_changed_since_the_build(monkeypatch):
    monkeypatch.setattr(runner, "xctestrun", lambda t: "/x/r.xctestrun")
    dev.update_device_state(UDID, backend="ios", team=TEAM, built="0123456789ab")
    monkeypatch.setattr(setup, "step_xcode", lambda c: "Xcode 26.2")
    monkeypatch.setattr(setup, "step_attached", lambda c: UDID)
    monkeypatch.setattr(devices, "list_devices", lambda: [PHONE])
    monkeypatch.setattr(setup, "all_teams", lambda: [{"team": TEAM, "name": "A", "free": True}])
    monkeypatch.setattr(devices, "lock_state", lambda cdid: {"locked": True, "unlocked_since_boot": True})
    monkeypatch.setattr(setup, "profile_expiry", lambda t: None)
    monkeypatch.setattr(manage, "_run", lambda cmd: "")
    monkeypatch.setenv("USBMUXD_SOCKET_ADDRESS", FakeUsbmux({UDID: {}}).address)
    d = Doctor()
    manage.doctor(UDID, d.check, d.skip)
    assert d.results["runner-build"][0] is False and "run: droidctl setup" in d.results["runner-build"][1]
    assert d.results["unlocked"][0] is False


# --------------------------------------------------------------------------
# syslog: lockdown's handshake and sharing the iPhone with Xcode
# --------------------------------------------------------------------------
class FakeLockdown:
    """lockdownd without TLS (EnableSessionSSL false): records requests, hands out a service
    port that streams `lines`."""

    def __init__(self, lines):
        self.requests = []
        self.lines = lines
        self.sock, self.svc = socket.socket(), socket.socket()
        for s in (self.sock, self.svc):
            s.bind(("127.0.0.1", 0))
            s.listen(4)
        self.port, self.svc_port = self.sock.getsockname()[1], self.svc.getsockname()[1]
        threading.Thread(target=self._lockdown, daemon=True).start()
        threading.Thread(target=self._service, daemon=True).start()

    def _lockdown(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:                       # closed by a test that never connected
            return
        with conn:
            while True:
                try:
                    msg = lockdown.recv(conn)
                except (OSError, struct.error, UserError):
                    return
                self.requests.append(msg["Request"])
                reply = {"StartSession": {"SessionID": "S1", "EnableSessionSSL": False},
                         "StartService": {"Port": 62999, "EnableServiceSSL": False},
                         "StopSession": {}}[msg["Request"]]
                lockdown.send(conn, reply)

    def _service(self):
        try:
            conn, _ = self.svc.accept()
            with conn:
                for line in self.lines:
                    conn.sendall(line.encode() + b"\n\x00")
                time.sleep(3)
        except OSError:
            return

    def close(self):
        self.sock.close()
        self.svc.close()


LINE = "Oct 21 22:12:26 iPhone TestApp(UIKitCore)[1986] <Notice>: hello"


@pytest.fixture
def fake_lockdown(monkeypatch):
    ld = FakeLockdown([LINE] * 3)
    m = FakeUsbmux({UDID: {lockdown.LOCKDOWN_PORT: ld.port, 62999: ld.svc_port}})
    monkeypatch.setenv("USBMUXD_SOCKET_ADDRESS", m.address)
    monkeypatch.setattr(lockdown, "pair_record", lambda u, timeout=5.0: {"HostID": "H", "SystemBUID": "B"})
    yield ld
    m.close()
    ld.close()


def test_the_relay_is_started_through_lockdown_and_its_session_ended(fake_lockdown):
    got = syslog.listen(UDID, 1.0)
    assert fake_lockdown.requests == ["StartSession", "StartService", "StopSession"]
    assert got == [LINE] * 3 and syslog.parse(got[0])["proc"] == "TestApp"


def test_an_unpaired_mac_is_told_to_tap_trust(monkeypatch):
    m = FakeUsbmux({UDID: {}}, paired=[])
    monkeypatch.setenv("USBMUXD_SOCKET_ADDRESS", m.address)
    try:
        with pytest.raises(UserError) as e:
            lockdown.pair_record(UDID)
    finally:
        m.close()
    assert e.value.kind == "no-device" and "Trust" in e.value.hint


def wait_until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_the_ring_reads_and_lets_go_when_xcode_needs_the_iphone(fake_lockdown):
    ring = syslog.Ring(UDID)
    try:
        assert wait_until(lambda: len(ring.snapshot()) == 3)
        assert ring.reading and os.path.exists(syslog._flag(UDID, "syslog-reading"))
        with syslog.xcode_busy(UDID, wait=3.0):
            # the block only starts once every reader let go
            assert not ring.reading and not os.path.exists(syslog._flag(UDID, "syslog-reading"))
    finally:
        ring.close()


def test_the_ring_stays_off_while_droidctl_is_idle(fake_lockdown):
    ring = syslog.Ring(UDID, active=lambda: False)
    try:
        time.sleep(0.8)
        assert not ring.reading and ring.snapshot() == [] and fake_lockdown.requests == []
    finally:
        ring.close()


def test_a_stale_busy_flag_left_by_a_crash_is_ignored(monkeypatch):
    path = syslog._flag(UDID, "xcode-busy")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").close()
    assert syslog.xcode_is_busy(UDID)
    old = time.time() - syslog.BUSY_STALE_S - 5
    os.utime(path, (old, old))
    assert not syslog.xcode_is_busy(UDID)


def test_amfi_reveal_sends_action_zero(monkeypatch):
    sent = []
    a, b = socket.socketpair()
    monkeypatch.setattr(lockdown, "start_service", lambda u, svc, timeout=5.0: (sent.append(svc), a)[1])

    def reply():
        sent.append(lockdown.recv(b))
        lockdown.send(b, {"success": True})
    t = threading.Thread(target=reply)
    t.start()
    setup.reveal_developer_mode(UDID)
    t.join(2)
    b.close()
    assert sent == ["com.apple.amfi.lockdown", {"action": 0}]


# -- loading catch-up: an action settled on a spinner it brought up (2026-10-26, real apps: rates, login)
class _Snaps:
    def __init__(self, seq):
        self.seq, self.reads = list(seq), 0

    def tree(self):
        self.reads += 1
        return self.seq.pop(0) if len(self.seq) > 1 else self.seq[0]

    def build(self, s):
        return s


def _snap(*roles):
    """Roles; "row+spinner" is a row whose cell holds only a spinner (it reads as row "In progress")."""
    from types import SimpleNamespace as NS
    spin = NS(raw={}, cls="ProgressBar", children=[])

    def node(r):
        return NS(raw={}, cls="Other", actionable=r.startswith(("button", "row")),
                  children=[spin] if r.endswith("+spinner") else [])
    return NS(elements=[NS(role=r.split("+")[0], node=node(r)) for r in roles])


def test_a_spinner_inside_a_row_is_waited_for(monkeypatch):
    from droidctl import act
    monkeypatch.setattr(act.time, "sleep", lambda s: None)
    sess = _Snaps([_snap("heading", "row+spinner"), _snap("heading", "row", "row")])
    post, ms, still = act._loading_catch_up(sess, {"lines": ['[1] button "Transactions"']},
                                            _snap("heading", "row+spinner"))
    assert [e.role for e in post.elements] == ["heading", "row", "row"] and not still and sess.reads == 2


def test_an_action_waits_for_the_spinner_it_brought_up(monkeypatch):
    from droidctl import act
    monkeypatch.setattr(act.time, "sleep", lambda s: None)
    sess = _Snaps([_snap("progress"), _snap("progress"), _snap("heading", "row")])
    post, ms, still = act._loading_catch_up(sess, {"lines": ['[1] button "Kurlar"']}, _snap("progress"))
    assert [e.role for e in post.elements] == ["heading", "row"] and not still and sess.reads == 3


def test_a_spinner_that_was_there_before_is_not_waited_for():
    from droidctl import act
    sess = _Snaps([_snap("row")])
    post, ms, still = act._loading_catch_up(sess, {"lines": ["[1] progress", '[2] button "x"']},
                                            _snap("progress", "row"))
    assert (ms, still, sess.reads) == (0, False, 0)


def test_a_splash_after_a_screen_with_targets_is_waited_out(monkeypatch):
    from droidctl import act
    monkeypatch.setattr(act.time, "sleep", lambda s: None)
    sess = _Snaps([_snap("text"), _snap("button", "button")])
    post, ms, still = act._loading_catch_up(sess, {"lines": ['[1] button "Default Login"']}, _snap("text"))
    assert [e.role for e in post.elements] == ["button", "button"] and not still


def test_a_spinner_that_never_goes_is_waited_for_once_then_reported(monkeypatch):
    from droidctl import act
    clock = iter(range(0, 100))
    monkeypatch.setattr(act.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(act.time, "sleep", lambda s: None)
    post, ms, still = act._loading_catch_up(_Snaps([_snap("progress")]), {"lines": ["[1] row"]}, _snap("progress"))
    assert still and ms >= act.LOADING_WAIT_S * 1000


def _scr(sig, overlaps):
    from types import SimpleNamespace as NS
    return NS(sig=sig, overlaps=[0] * overlaps, elements=[])


def test_a_screen_read_mid_push_is_read_again_until_it_is_one_screen(monkeypatch):
    from droidctl import act
    monkeypatch.setattr(act.time, "sleep", lambda s: None)
    sess = _Snaps([_scr("b", 4), _scr("c", 0)])
    post, ms = act._transition_catch_up(sess, _scr("a", 5))
    assert post.sig == "c" and sess.reads == 2


def test_a_screen_laid_out_overlapping_costs_one_more_read():
    from droidctl import act
    sess = _Snaps([_scr("a", 4)])
    post, ms = act._transition_catch_up(sess, _scr("a", 4))
    assert post.sig == "a" and sess.reads == 1


def test_few_overlaps_are_not_a_transition():
    from droidctl import act
    sess = _Snaps([_scr("x", 0)])
    post, ms = act._transition_catch_up(sess, _scr("a", 2))
    assert (post.sig, ms, sess.reads) == ("a", 0, 0)
