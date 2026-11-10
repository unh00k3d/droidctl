"""iOS I2: the action commands against FakeRunner (through FakeUsbmux), on REAL runner trees.

FakeRunner replays one captured tree and answers inputs with the runner's reply shapes; these
tests check what the host sends (actions, handles, the iOS settle rule) and how it reads the
replies. What the phone does with them is covered by tests/e2e/ios/test_i2.py (DTA ground truth).
"""
import pytest

from droidctl import act, cli
from droidctl import device as dev
from droidctl.core import UserError
from droidctl.ios import client as ic
from droidctl.ios import mapping
from tests.fakes import FakeRunner, FakeUsbmux
from tests.test_ios_mapping import load, walk

UDID = "00008101-00AAAAAAAAAAAAAA"


@pytest.fixture
def make_phone(monkeypatch, tmp_path):
    made = []

    def make(fixture="testapp_main"):
        r = FakeRunner(fixture)
        m = FakeUsbmux({UDID: {27100: r.port}})
        monkeypatch.setenv("USBMUXD_SOCKET_ADDRESS", m.address)
        monkeypatch.setenv("DROIDCTL_HOME", str(tmp_path))
        made.append((m, r))
        return r
    act._TREE_CACHE.clear()
    yield make
    act.close_sessions()
    act._TREE_CACHE.clear()
    for m, r in made:
        m.close()
        r.close()


def run(*argv):
    a = cli.build_parser().parse_args([*argv, "-d", UDID])
    try:
        return a.fn(a)
    finally:
        act.close_sessions()


def native(r, pred):
    return next(n for a in r.tree["apps"] for n in walk(a["root"]) if pred(n))


# -- the client: the iOS settle rule and the runner's own methods
def test_every_settle_spec_becomes_the_measured_ios_rule():
    assert ic.ios_settle(None) is None
    assert ic.ios_settle({"quiet_ms": 450, "first_ms": 600, "timeout_ms": 2000}) == {"quiet_ms": 100, "timeout_ms": 3000}
    assert ic.ios_settle({"timeout_ms": 8000, "tree": False}) == {"quiet_ms": 100, "timeout_ms": 8000, "tree": False}


def test_a_settled_reply_carries_a_mapped_tree(make_phone):
    r = make_phone()
    c = ic.IosClient(UDID)
    try:
        t = c.tree()
        res = c.act(t["dump"], 5, action="click", settle={"quiet_ms": 150, "timeout_ms": 2000})
    finally:
        c.close()
    assert r.params["act"]["settle"] == {"quiet_ms": 100, "timeout_ms": 3000}
    assert "windows" in res["tree"] and "apps" not in res["tree"]


def test_the_clipboard_is_unsupported(make_phone):
    make_phone()
    with pytest.raises(UserError) as e:
        ic.IosClient(UDID).clipboard(set="x")
    assert e.value.kind == "unsupported"


@pytest.mark.parametrize("code,kind", [(-32011, "occluded"), (-32012, "not-focused"), (-32010, "screen-off")])
def test_runner_error_codes_are_typed(code, kind):
    assert dev.DEVICE_ERRORS[code][0] == kind


# -- wait_for on the mapped tree
def test_wait_for_reads_labels_as_text_and_respects_gone():
    t = mapping.convert(load("testapp_main"))
    assert ic.check(t, {"text": "press me"})[0]                  # a button's title is its desc on iOS
    assert ic.check(t, {"text": "Press me", "exact": True})[1]["node"]["desc"] == "Press me"
    assert not ic.check(t, {"text": "Press", "exact": True})[0]
    assert ic.check(t, {"text": "nothing like this", "gone": True})[0]
    assert ic.check(t, {"window": "dev.droidctl.s1.testapp"})[0]
    assert not ic.check(t, {"pkg": "com.apple.Preferences"})[0]


def test_wait_for_times_out_typed_and_refuses_android_only_conditions(make_phone):
    make_phone()
    c = ic.IosClient(UDID)
    try:
        assert c.wait_for(timeout_ms=500, text="Press me")["matched"]
        with pytest.raises(UserError) as e:
            c.wait_for(timeout_ms=300, text="nothing like this")
        assert e.value.kind == "timeout"
        with pytest.raises(UserError) as e:
            c.wait_for(timeout_ms=300, toast="")
        assert e.value.kind == "unsupported"
    finally:
        c.close()


# -- mapping for actions
def test_the_focused_field_is_marked_and_offers_set_text():
    s = mapping.convert(load("testapp_keyboard"))
    fields = [n for w in s["windows"] for n in walk(w["root"]) if "editable" in n["flags"]]
    focused = [n for n in fields if "focused" in n["flags"]]
    assert [n.get("id") for n in focused] == ["dta_field"]
    assert "set_text" in focused[0]["actions"] and "focus" not in focused[0]["actions"]


def test_the_navigation_back_button_is_marked():
    s = mapping.convert(load("settings_general"))
    backs = [n for w in s["windows"] for n in walk(w["root"]) if n.get("nav_back")]
    assert [n.get("desc") for n in backs] == ["Settings"]


# -- the commands
def test_tap_counts_a_delivered_touch_as_handled(make_phone):
    r = make_phone()
    out = run("tap", "--id", "dta_button")
    p = r.params["act"]
    assert p["action"] == "click" and p["handle"] == native(r, lambda n: n.get("id") == "dta_button")["handle"]
    assert out["via"] == "touch" and out["handled"] and r.calls["gesture"] == 0   # no host-side fallback


def test_back_taps_the_navigation_bars_back_button(make_phone):
    r = make_phone("settings_general")
    out = run("back")
    back = native(r, lambda n: n.get("label") == "Settings" and n["type"] == 9)
    assert r.params["act"]["handle"] == back["handle"] and r.params["act"]["action"] == "click"
    assert out["method"] == "back"


def test_back_without_a_back_button_is_unsupported(make_phone):
    make_phone("testapp_main")
    with pytest.raises(UserError) as e:
        run("back")
    assert e.value.kind == "unsupported"


def test_press_enter_types_a_newline_and_other_keys_are_unsupported(make_phone):
    r = make_phone("testapp_keyboard")
    run("press", "enter")
    assert r.params["type"]["text"] == "\n"
    with pytest.raises(UserError) as e:
        run("press", "tab")
    assert e.value.kind == "unsupported"


def test_hide_keyboard_asks_the_runner_and_judges_the_screen(make_phone):
    """The replayed tree keeps its keyboard: the screen says it is still up, whatever the runner
    replied (it once counted a keyboard sliding off as shown, and 0 read as success)."""
    r = make_phone("testapp_keyboard")
    with pytest.raises(UserError) as e:
        run("hide-keyboard")
    assert r.calls["hide_keyboard"] == 1 and e.value.kind == "no-change"


def test_adb_is_unsupported_on_an_iphone_not_an_adb_error(make_phone):
    make_phone()
    with pytest.raises(UserError) as e:
        act.get_session(UDID).adb("shell", "true")
    assert e.value.kind == "unsupported"
    act.close_sessions()


def test_type_uses_keys_into_the_focused_field_without_clipboard_or_adb(make_phone):
    """Keys first (2026-10-26: AX set_value bypassed a formatted field's model); what the field held
    is deleted first. The replayed tree shows "abc" focused before and after."""
    r = make_phone("testapp_keyboard")
    out = run("type", "--id", "dta_field", "abc")
    assert r.params["type"]["text"] == "\b\b\babc" and r.calls["act"] == 0
    assert out["verified"] and out["via"] == "keys" and r.calls["clipboard"] == 0


def test_type_into_a_field_that_leaves_once_the_keys_are_in_is_not_retyped(make_phone):
    """A login field submits itself at 6 digits and its dialog closes (a real app, 2026-10-26):
    the keys were taken. Once said not-found; a fallback could type them a second time elsewhere."""
    r = make_phone("testapp_keyboard")
    r.after_type = "settings_general"
    out = run("type", "--id", "dta_field", "abc")
    assert r.calls["type"] == 1 and r.calls["act"] == 0
    assert out["via"] == "keys" and not out["verified"] and "left the screen" in out["warning"]


def test_daemon_pooled_client_binds_the_ios_methods():
    from droidctl import daemon

    class Sess:
        serial, port, alive = UDID, 27100, True
        client = ic.IosClient.__new__(ic.IosClient)
    pc = daemon.PooledClient(Sess())
    assert pc.wait_for.__func__ is ic.IosClient.wait_for
    assert pc.hide_keyboard.__func__ is ic.IosClient.hide_keyboard


def test_an_action_resolves_on_the_last_tree_while_nothing_changed(make_phone, monkeypatch):
    """A locator tap right after a snapshot asks `gen` (~20 ms), not for the tree again (250-460 ms on a
    real app's screens); a changed `gen` reads the tree."""
    monkeypatch.setattr(act, "LATE_CHANGE_S", 0)    # the replayed screen never changes: don't poll it
    r = make_phone("testapp_main")
    run("snapshot")
    trees = r.calls["tree"]
    run("tap", "--id", "dta_custom")
    assert r.calls["tree"] == trees and r.calls["gen"] >= 1
    r.gen += 1
    run("tap", "--id", "dta_custom")
    assert r.calls["tree"] == trees + 1


def test_a_tap_that_changed_nothing_waits_briefly_for_a_late_push(make_phone):
    """The settle ended in the pause before the app pushed its next screen (a real app's
    Discover rows, 2026-10-26): read again, bounded, and report the new screen."""
    r = make_phone("testapp_main")
    late = load("settings_general")
    orig, reads = r._reply, []

    def reply(method, params):
        if method == "tree":
            reads.append(1)
            if len(reads) == 2:             # the first read resolves the tap; the push lands after the settle
                r.tree, r.gen = late, r.gen + 1
        return orig(method, params)
    r._reply = reply
    out = run("tap", "--id", "dta_custom")
    assert out["new_screen"] and out["loading_ms"] > 0
