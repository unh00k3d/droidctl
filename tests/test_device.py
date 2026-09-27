"""device.py without a phone: a11y settings helpers, selection, state, and the socket client."""
import json
import pathlib
import re
import socket
import threading
import time

import pytest

from droidctl import device as dev
from droidctl.core import UserError

ROOT = pathlib.Path(__file__).resolve().parent.parent
TALKBACK = "com.google.android.marvin.talkback/com.google.android.marvin.talkback.TalkBackService"
BITWARDEN = "com.x8bit.bitwarden/com.x8bit.bitwarden.Accessibility.AccessibilityService"


# --- enabled_accessibility_services ---------------------------------------
@pytest.mark.parametrize("raw", [None, "", "null", "  null\n"])
def test_unset_setting_parses_to_an_empty_list(raw):
    assert dev.parse_services(raw) == []


def test_add_appends_and_keeps_others_exactly():
    before = [TALKBACK, BITWARDEN]
    after, added = dev.add_service(before)
    assert added and after == [TALKBACK, BITWARDEN, dev.COMPONENT]
    assert before == [TALKBACK, BITWARDEN], "the input must not be mutated"


def test_add_to_an_unset_setting():
    after, added = dev.add_service(dev.parse_services("null"))
    assert added and dev.join_services(after) == dev.COMPONENT


@pytest.mark.parametrize("ours", [
    "dev.droidctl.agent/.AgentService",
    "dev.droidctl.agent/dev.droidctl.agent.AgentService",
    "DEV.droidctl.agent/.agentservice",
])
def test_ours_is_recognised_in_either_spelling(ours):
    after, added = dev.add_service([TALKBACK, ours])
    assert not added and after == [TALKBACK, ours]
    kept, removed = dev.remove_service([TALKBACK, ours, BITWARDEN])
    assert removed and kept == [TALKBACK, BITWARDEN]


def test_remove_leaves_a_look_alike_alone():
    other = "dev.droidctl.agent2/.AgentService"
    kept, removed = dev.remove_service([other])
    assert not removed and kept == [other]


def test_roundtrip_restores_the_original_string():
    raw = f"{TALKBACK}:{BITWARDEN}"
    after, _ = dev.add_service(dev.parse_services(raw))
    kept, _ = dev.remove_service(dev.parse_services(dev.join_services(after)))
    assert dev.join_services(kept) == raw


# --- device selection -----------------------------------------------------
def test_select_the_only_ready_device():
    assert dev.select_serial(None, [("A", "device"), ("B", "unauthorized")]) == "A"


def test_select_the_requested_device():
    assert dev.select_serial("B", [("A", "device"), ("B", "device")]) == "B"


@pytest.mark.parametrize("requested,devices,needle", [
    (None, [], "no device attached"),
    (None, [("A", "unauthorized")], "not ready: A (unauthorized)"),
    (None, [("A", "device"), ("B", "device")], "2 devices attached"),
    ("C", [("A", "device")], "C is not attached"),
    ("A", [("A", "offline")], "A is offline"),
])
def test_selection_failures_are_no_device(requested, devices, needle):
    with pytest.raises(UserError) as e:
        dev.select_serial(requested, devices)
    assert e.value.kind == "no-device" and needle in str(e.value)


def test_several_devices_hint_at_dash_d():
    with pytest.raises(UserError) as e:
        dev.select_serial(None, [("A", "device"), ("B", "device")])
    assert "-d" in e.value.hint


def test_find_forward_matches_serial_and_remote():
    text = ("B tcp:7001 localabstract:droidctl\n"
            "A tcp:7002 tcp:8080\n"
            "A tcp:7003 localabstract:droidctl\n")
    assert dev.find_forward("A", text) == 7003
    assert dev.find_forward("C", text) is None


# --- host state -----------------------------------------------------------
def test_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("DROIDCTL_HOME", str(tmp_path / "home"))
    assert dev.device_state("A") == {}
    dev.update_device_state("A", port=7003, version_code=1)
    dev.update_device_state("A", restore={"services": "null", "a11y_enabled": "0"})
    assert dev.device_state("A") == {"port": 7003, "version_code": 1,
                                     "restore": {"services": "null", "a11y_enabled": "0"}}
    assert not list((tmp_path / "home").glob("*.tmp")), "atomic write left a temp file"
    dev.clear_device_state("A")
    assert dev.device_state("A") == {}


def test_corrupt_state_is_treated_as_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("DROIDCTL_HOME", str(tmp_path))
    (tmp_path / "state.json").write_text("{half")
    assert dev.load_state() == {"devices": {}}


def test_bundled_version_code_matches_the_agent_build():
    gradle = ROOT / "android" / "agent" / "build.gradle.kts"
    if not gradle.exists():
        pytest.skip("android/ is not part of the installed package")
    m = re.search(r"versionCode\s*=\s*(\d+)", gradle.read_text())
    assert m and int(m.group(1)) == dev.AGENT_VERSION_CODE


# --- the agent socket, against an in-process fake agent -------------------
class FakeAgent:
    """A one-connection NDJSON server; `script` maps a request to reply lines."""

    def __init__(self, script):
        self.script = script
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        conn, _ = self.srv.accept()
        r = conn.makefile("rb")
        with conn:
            while True:
                line = r.readline()
                if not line:
                    return
                replies = self.script(json.loads(line))
                if replies is None:
                    return                         # close without replying
                for msg in replies:
                    conn.sendall((msg if isinstance(msg, str) else json.dumps(msg)).encode() + b"\n")


PING = {"protocol": 1, "version": "0.1.0", "versionCode": 1, "sdk": 28, "peer_uid": 2000}


def test_ping_roundtrip_and_reuse():
    fake = FakeAgent(lambda req: [{"jsonrpc": "2.0", "id": req["id"], "result": PING}])
    with dev.AgentClient(fake.port) as c:
        assert c.call("ping") == PING
        assert c.call("ping") == PING, "the connection is persistent"


def test_notifications_and_stale_replies_are_skipped():
    fake = FakeAgent(lambda req: [
        {"jsonrpc": "2.0", "method": "event", "params": {"type": "toast"}},
        {"jsonrpc": "2.0", "id": req["id"] - 1, "result": "old"},
        {"jsonrpc": "2.0", "id": req["id"], "result": "new"},
    ])
    with dev.AgentClient(fake.port) as c:
        assert c.call("ping") == "new"


def test_rpc_errors_map_to_device():
    fake = FakeAgent(lambda req: [{"jsonrpc": "2.0", "id": req["id"],
                                   "error": {"code": -32601, "message": "method not found: nope"}}])
    with dev.AgentClient(fake.port) as c, pytest.raises(UserError) as e:
        c.call("nope")
    assert e.value.kind == "device" and "-32601" in str(e.value)


def test_unauthorized_line_is_reported_clearly():
    fake = FakeAgent(lambda req: [{"jsonrpc": "2.0", "id": None,
                                   "error": {"code": -32001, "message": "unauthorized uid 10123"}}])
    with dev.AgentClient(fake.port) as c, pytest.raises(UserError) as e:
        c.call("ping")
    assert e.value.kind == "device" and "refused" in str(e.value) and "10123" in str(e.value)


def test_eof_before_reply_is_a_connection_error():
    """adb accepts a forwarded connect even when nothing listens on the phone, then closes it."""
    fake = FakeAgent(lambda req: None)
    with dev.AgentClient(fake.port) as c, pytest.raises(UserError) as e:
        c.call("ping")
    assert e.value.kind == "connection"


def test_invalid_json_is_a_device_error():
    fake = FakeAgent(lambda req: ["not json"])
    with dev.AgentClient(fake.port) as c, pytest.raises(UserError) as e:
        c.call("ping")
    assert e.value.kind == "device"


def test_no_reply_times_out():
    fake = FakeAgent(lambda req: [])
    with dev.AgentClient(fake.port) as c, pytest.raises(UserError) as e:
        c.call("ping", timeout=0.2)
    assert e.value.kind == "timeout"


def test_nothing_listening_is_a_connection_error():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]          # bound but not listening: connect is refused
        with pytest.raises(UserError) as e:
            dev.AgentClient(port, timeout=1)
    assert e.value.kind == "connection"


def test_rtt_stats():
    st = dev.rtt_stats([4.0, 1.0, 3.0, 2.0])
    assert (st["n"], st["min"], st["median"], st["max"]) == (4, 1.0, 2.5, 4.0)


def test_importing_the_cli_does_not_load_heavy_modules():
    """The hot path must stay stdlib: adbutils/requests/PIL/rich load only when needed."""
    import subprocess
    import sys
    code = ("import sys, droidctl.cli; "
            "print(sorted(m for m in ('adbutils','requests','PIL','rich') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


# --- the service being unbound mid-request (uiautomator/Appium) -----------------
class RebindingAgent:
    """Serves connections one after another. `unbind()` makes it drop the live
    connection at its next request, then accept-and-close `gap` connections (as
    adb does while nothing listens on the phone) before serving normally again."""

    def __init__(self, gap=3):
        self.gap, self.pending_unbind, self.refuse = gap, False, 0
        self.executed = []
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def unbind(self):
        self.pending_unbind = True

    def _serve(self):
        while True:
            conn, _ = self.srv.accept()
            if self.refuse:
                self.refuse -= 1
                conn.close()
                continue
            r = conn.makefile("rb")
            with conn:
                while True:
                    line = r.readline()
                    if not line:
                        break
                    req = json.loads(line)
                    self.executed.append(req["method"])
                    if self.pending_unbind:
                        self.pending_unbind, self.refuse = False, self.gap
                        conn.shutdown(socket.SHUT_RDWR)   # like onUnbind closing the socket
                        break                  # the request ran, but no reply ever comes
                    conn.sendall((json.dumps({"jsonrpc": "2.0", "id": req["id"],
                                              "result": {"m": req["method"]}}) + "\n").encode())


def test_a_read_survives_the_service_being_unbound():
    fake = RebindingAgent()
    with dev.AgentClient(fake.port) as c:
        assert c.call("ping") == {"m": "ping"}
        fake.unbind()
        assert c.call("tree") == {"m": "tree"}          # waited for the agent, then retried once
        assert fake.executed.count("tree") == 2


def test_an_action_is_never_retried_after_an_unbind():
    fake = RebindingAgent()
    with dev.AgentClient(fake.port) as c:
        c.call("ping")
        fake.unbind()
        with pytest.raises(UserError) as e:
            c.call("act", {"dump": 1, "handle": 2, "action": "click"})
        assert e.value.kind == "connection" and e.value.data["maybe_performed"]
        assert "snapshot" in e.value.hint
        assert fake.executed.count("act") == 1           # sent once, never repeated
        assert c.call("ping") == {"m": "ping"}           # the next call reconnects
        assert fake.executed.count("act") == 1


def test_a_phone_without_the_agent_still_fails_fast():
    fake = FakeAgent(lambda req: None)
    t = time.monotonic()
    with dev.AgentClient(fake.port) as c, pytest.raises(UserError) as e:
        c.call("ping")
    assert e.value.kind == "connection" and time.monotonic() - t < 1.0


def test_an_agent_that_never_comes_back_is_a_clear_connection_error(monkeypatch):
    monkeypatch.setattr(dev.AgentClient, "REBIND_WAIT", 0.5)
    fake = RebindingAgent(gap=10 ** 6)
    with dev.AgentClient(fake.port) as c:
        c.call("ping")
        fake.unbind()
        with pytest.raises(UserError) as e:
            c.call("tree")
        assert e.value.kind == "connection" and "did not come back" in str(e.value)
