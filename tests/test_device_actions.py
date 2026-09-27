"""Host wrappers for the M5 device methods (act, gesture, waits, events, screenshot),
exercised against an in-process fake agent. No phone needed."""
import json

import pytest

from droidctl import device as dev
from droidctl.core import ERROR_KINDS, UserError
from tests.test_device import FakeAgent


def echo_params(req):
    """Reply with what was sent, so a test can check the wire format."""
    return [{"jsonrpc": "2.0", "id": req["id"], "result": {"method": req["method"], "params": req.get("params")}}]


def test_act_sends_a_standard_action_with_settle():
    with dev.AgentClient(FakeAgent(echo_params).port) as c:
        r = c.act(3, 17, "click", settle={"quiet_ms": 150})
    assert r == {"method": "act", "params": {"dump": 3, "handle": 17, "action": "click",
                                             "settle": {"quiet_ms": 150}}}


def test_act_sends_a_custom_action_instead_of_a_name():
    with dev.AgentClient(FakeAgent(echo_params).port) as c:
        r = c.act(3, 17, custom="Archive", args={"x": 1}, force=True, event_ms=0)
    assert r["params"] == {"dump": 3, "handle": 17, "custom": "Archive", "args": {"x": 1},
                           "force": True, "event_ms": 0}


def test_gesture_points_are_integer_device_px():
    with dev.AgentClient(FakeAgent(echo_params).port) as c:
        r = c.gesture("swipe", [(10.7, 20.2), (30, 40)], ms=300)
    assert r["params"] == {"type": "swipe", "points": [[10, 20], [30, 40]], "ms": 300}


def test_wait_for_drops_unset_conditions():
    with dev.AgentClient(FakeAgent(echo_params).port) as c:
        r = c.wait_for(text="OK", id=None, timeout_ms=1500)
    assert r["params"] == {"text": "OK", "timeout_ms": 1500}


def test_global_and_events_and_subscribe_wire_format():
    with dev.AgentClient(FakeAgent(echo_params).port) as c:
        assert c.global_action("back")["params"] == {"name": "back"}
        assert c.events(since=5, limit=10)["params"] == {"since": 5, "limit": 10}
        assert c.subscribe(["toast"])["params"] == {"events": ["toast"]}
        assert c.subscribe()["params"] == {}
        assert c.clipboard("hi")["params"] == {"set": "hi"}


@pytest.mark.parametrize("code,kind", [
    (-32002, "stale-ref"), (-32003, "stale-ref"), (-32004, "disabled"), (-32005, "offscreen"),
    (-32006, "unsupported"), (-32007, "timeout"), (-32008, "secure-window"), (-32009, "device"),
])
def test_device_error_codes_map_to_error_kinds(code, kind):
    assert kind in ERROR_KINDS
    fake = FakeAgent(lambda req: [{"jsonrpc": "2.0", "id": req["id"],
                                   "error": {"code": code, "message": "nope"}}])
    with dev.AgentClient(fake.port) as c, pytest.raises(UserError) as e:
        c.act(1, 1, "click")
    assert e.value.kind == kind


def test_pushed_events_are_kept_for_notifications():
    """A notification that arrives while waiting for a reply must not be lost."""
    def script(req):
        return [{"jsonrpc": "2.0", "method": "event", "params": {"type": "clicked", "seq": 1}},
                {"jsonrpc": "2.0", "id": req["id"], "result": {"subscribed": "all"}},
                {"jsonrpc": "2.0", "method": "event", "params": {"type": "toast", "seq": 2}}]
    with dev.AgentClient(FakeAgent(script).port) as c:
        c.subscribe()
        got = [e["type"] for e in c.notifications(timeout=0.3)]
    assert got == ["clicked", "toast"]


def test_screenshot_falls_back_to_screencap_when_unsupported(monkeypatch):
    fake = FakeAgent(lambda req: [{"jsonrpc": "2.0", "id": req["id"],
                                   "error": {"code": -32006, "message": "needs API 30"}}])
    monkeypatch.setattr(dev, "screencap_jpeg", lambda serial, scale, quality, crop: {"source": "screencap", "serial": serial})
    with dev.AgentClient(fake.port) as c:
        assert dev.screenshot(c, "SER") == {"source": "screencap", "serial": "SER"}


def test_screenshot_secure_window_is_not_papered_over(monkeypatch):
    fake = FakeAgent(lambda req: [{"jsonrpc": "2.0", "id": req["id"],
                                   "error": {"code": -32008, "message": "secure"}}])
    monkeypatch.setattr(dev, "screencap_jpeg", lambda *a: pytest.fail("must not fall back"))
    with dev.AgentClient(fake.port) as c, pytest.raises(UserError) as e:
        dev.screenshot(c, "SER")
    assert e.value.kind == "secure-window"


def test_settle_budget_covers_the_device_wait():
    assert dev._settle_budget(None) == 5.0
    assert dev._settle_budget({"timeout_ms": 4000}) >= 4 + 2
