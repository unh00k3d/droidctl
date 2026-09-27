"""The action policy, against a scripted agent and REAL trees (tests/fixtures/resolve).

The fake agent only scripts *replies* (performed, clicked_event, which real tree
comes back after the settle); every tree is a real capture from the phone. The
one exception is marked: a copy of a real tree with one field's text set, to
stand for "the field took the text".
"""
import argparse
import copy
import json
import pathlib

import pytest

from droidctl import act
from droidctl import snapshot as S
from droidctl.core import UserError

FIX = pathlib.Path(__file__).resolve().parent / "fixtures" / "resolve"


def tree(name):
    return json.loads((FIX / f"{name}.json").read_text())["tree"]


class FakeClient:
    """Scripted agent: records every call; `act_replies` / `gesture_replies` are queues."""

    def __init__(self, current, gen=None):
        self.current = current                 # what `tree` returns right now
        self.gen_value = gen if gen is not None else current.get("gen")
        self.calls = []
        self.act_replies = []
        self.gesture_replies = []
        self.clip = "old clip"
        self.port = 0

    def gen(self):
        self.calls.append(("gen",))
        return {"gen": self.gen_value}

    def tree(self, not_important=False, timeout=10.0):
        self.calls.append(("tree",))
        return self.current

    def act(self, dump, handle, action=None, custom=None, args=None, settle=None, force=False, event_ms=None):
        self.calls.append(("act", dump, handle, action or custom, args, bool(settle), force))
        r = self.act_replies.pop(0) if self.act_replies else {"performed": True}
        if isinstance(r, Exception):
            raise r
        if r.get("tree") is not None:
            self.current = r["tree"]
        return dict(r)

    def gesture(self, type, points, ms=None, settle=None):
        self.calls.append(("gesture", type, [list(p) for p in points]))
        r = self.gesture_replies.pop(0) if self.gesture_replies else {"performed": True}
        if r.get("tree") is not None:
            self.current = r["tree"]
        return dict(r)

    def clipboard(self, set=None):
        self.calls.append(("clipboard", set))
        prev, self.clip = self.clip, set
        return {"previous": prev, "set": True}

    def wait_idle(self, quiet_ms=150, timeout_ms=2000):
        return {"idle": True, "ms": 1, "events": []}

    def call(self, method, params=None, timeout=10.0):
        self.calls.append((method,))
        return {}

    def close(self):
        pass

    def count(self, kind, what=None):
        return sum(1 for c in self.calls if c[0] == kind and (what is None or c[3 if kind == "act" else 1] == what))


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DROIDCTL_HOME", str(tmp_path))
    act.close_sessions()
    yield tmp_path
    act.close_sessions()


def session(monkeypatch, pre_tree, shell=None):
    """A Session whose snapshot state is `pre_tree` (as if `snapshot` just ran)."""
    s = act.Session("FAKE")
    s._client = FakeClient(pre_tree)
    s.info = {"sdk": 28, "versionCode": 2, "screen": {"w": 1080, "h": 2220}}
    s.save(S.build(pre_tree))
    s.shell_calls = []
    s.shell = lambda *cmd, **kw: (s.shell_calls.append(cmd), shell(cmd) if shell else "")[1]
    monkeypatch.setattr(act, "session_for", lambda a: s)
    return s


def args(**kw):
    base = dict(device="FAKE", json=True, target=None, ref=None, id=None, text=None, desc=None, cls=None,
                role=None, index=None, right_of=None, left_of=None, above=None, below=None, near=None,
                point=None, double=False, method="auto", settle=None, expect_change=False,
                no_auto_setup=True)
    base.update(kw)
    return argparse.Namespace(**base)


def ref_of(snap_tree, role, label):
    s = S.build(snap_tree)
    return next(e.ref for e in s.elements if e.role == role and e.label_full == label)


def plus_ref():
    # the "+" on the Wireless Mouse row (the first "+")
    s = S.build(tree("cart_inc-a"))
    return next(e.ref for e in s.elements if e.label_full == "+")


# --- tap policy -------------------------------------------------------------
def test_click_with_its_event_is_done_no_fallback(home, monkeypatch):
    s = session(monkeypatch, tree("cart_inc-a"))
    s._client.act_replies = [{"performed": True, "clicked_event": True, "tree": tree("cart_inc-b")}]
    out = act.cmd_tap(args(target=str(plus_ref())))
    assert out["method"] == "action" and out["changed"] is True
    assert out["target"]["tier"] == 0                      # fast path: dump and gen unchanged
    assert s._client.count("act") == 1 and s._client.count("gesture") == 0
    assert any(line[0] in "+~" and '"2"' in line for line in out["diff"])


def test_performed_without_event_but_screen_changed_is_not_retried(home, monkeypatch):
    s = session(monkeypatch, tree("cart_inc-a"))
    s._client.act_replies = [{"performed": True, "clicked_event": False, "tree": tree("cart_inc-b")}]
    out = act.cmd_tap(args(target=str(plus_ref())))
    assert out["method"] == "action" and out["changed"]
    assert s._client.count("gesture") == 0


def test_silent_click_is_never_tapped_again(home, monkeypatch):
    """performed:true means the handler ran; a gesture on top would run it twice."""
    s = session(monkeypatch, tree("cart_inc-a"))
    s._client.act_replies = [{"performed": True, "clicked_event": False, "tree": tree("cart_inc-a")}]
    out = act.cmd_tap(args(target=str(plus_ref())))
    assert out["method"] == "action" and "warning" in out and not out["changed"]
    assert s._client.count("gesture") == 0
    assert s._client.count("act") == 1


def test_not_performed_falls_back_once(home, monkeypatch):
    s = session(monkeypatch, tree("cart_inc-a"))
    s._client.act_replies = [{"performed": False, "available": []}]
    out = act.cmd_tap(args(target=str(plus_ref())))
    assert out["method"] == "gesture-fallback"
    assert s._client.count("gesture") == 1


def test_no_fallback_when_the_click_took_the_screen_away(home, monkeypatch):
    """performed:false after the app crashed: the old coordinates now belong to
    another screen (measured: the launcher), so no positional tap."""
    s = session(monkeypatch, tree("cart_inc-a"))
    other = json.loads((FIX.parent / "trees" / "real-launcher-home.json").read_text())["tree"]
    s._client.act_replies = [{"performed": False, "available": [], "tree": other}]
    out = act.cmd_tap(args(target=str(plus_ref())))
    assert out["method"] == "action" and "not tapping by position" in out["warning"]
    assert s._client.count("gesture") == 0


def test_method_action_never_falls_back(home, monkeypatch):
    s = session(monkeypatch, tree("cart_inc-a"))
    s._client.act_replies = [{"performed": True, "clicked_event": False, "tree": tree("cart_inc-a")}]
    out = act.cmd_tap(args(target=str(plus_ref()), method="action"))
    assert out["method"] == "action" and out["changed"] is False
    assert s._client.count("gesture") == 0


def test_method_gesture_only_taps(home, monkeypatch):
    s = session(monkeypatch, tree("cart_inc-a"))
    s._client.gesture_replies = [{"performed": True, "tree": tree("cart_inc-b")}]
    out = act.cmd_tap(args(target=str(plus_ref()), method="gesture"))
    assert out["method"] == "gesture"
    assert s._client.count("act") == 0 and s._client.count("gesture") == 1


def test_expect_change_turns_no_change_into_an_error(home, monkeypatch):
    s = session(monkeypatch, tree("cart_inc-a"))
    s._client.act_replies = [{"performed": True, "clicked_event": True, "tree": tree("cart_inc-a")}]
    with pytest.raises(UserError) as e:
        act.cmd_tap(args(target=str(plus_ref()), expect_change=True))
    assert e.value.kind == "no-change"
    assert s._client.count("gesture") == 0                 # a clicked event means handled: no retry


def test_stale_fast_path_handle_is_resolved_again_before_acting(home, monkeypatch):
    s = session(monkeypatch, tree("cart_inc-a"))
    s._client.act_replies = [UserError("act: stale dump", "stale-ref"),
                             {"performed": True, "clicked_event": True, "tree": tree("cart_inc-b")}]
    out = act.cmd_tap(args(target=str(plus_ref())))
    acts = [c for c in s._client.calls if c[0] == "act"]
    assert len(acts) == 2                                   # the first never ran on the device
    assert out["target"]["tier"] != 0 and out["method"] == "action"


def test_a_moved_screen_skips_the_fast_path(home, monkeypatch):
    s = session(monkeypatch, tree("cart_inc-a"))
    s._client.gen_value = (s.state["gen"] or 0) + 5         # something changed since the snapshot
    s._client.current = tree("cart_inc-b")
    s._client.act_replies = [{"performed": True, "clicked_event": True, "tree": tree("cart_inc-b")}]
    out = act.cmd_tap(args(target=str(plus_ref())))
    assert out["target"]["tier"] in (1, 2, 3, 4)
    act_call = next(c for c in s._client.calls if c[0] == "act")
    assert act_call[1] == S.build(tree("cart_inc-b")).dump  # acted in the fresh dump


def test_locator_is_unique_or_ambiguous(home, monkeypatch):
    session(monkeypatch, tree("cart_inc-a"))
    with pytest.raises(UserError) as e:
        act.cmd_tap(args(text="+"))                        # three "+" buttons
    assert e.value.kind == "ambiguous"


def test_spatial_locator_picks_the_right_row(home, monkeypatch):
    s = session(monkeypatch, tree("cart_inc-a"))
    s._client.act_replies = [{"performed": True, "clicked_event": True, "tree": tree("cart_inc-b")}]
    out = act.cmd_tap(args(text="+", right_of="Wireless Mouse"))
    assert out["method"] == "action"
    assert out["target"]["element"]["label"] == "+"
    b = out["target"]["element"]["bounds"]
    mouse = next(e for e in S.build(tree("cart_inc-a")).elements if e.label_full == "Wireless Mouse")
    assert b[1] < mouse.rect[3] and b[3] > mouse.rect[1]    # same row as Wireless Mouse


def test_point_is_an_explicit_gesture(home, monkeypatch):
    s = session(monkeypatch, tree("cart_inc-a"))
    out = act.cmd_tap(args(point="100,200"))
    assert out["method"] == "gesture"
    assert [c for c in s._client.calls if c[0] == "gesture"][0][2] == [[100, 200]]


def test_no_target_is_bad_args(home, monkeypatch):
    session(monkeypatch, tree("cart_inc-a"))
    with pytest.raises(UserError) as e:
        act.cmd_tap(args())
    assert e.value.kind == "bad-args"


def test_unknown_ref_is_stale(home, monkeypatch):
    session(monkeypatch, tree("cart_inc-a"))
    with pytest.raises(UserError) as e:
        act.cmd_tap(args(target="999"))
    assert e.value.kind == "stale-ref"


# --- type ladder --------------------------------------------------------------
def with_text(t, text):
    """A copy of a REAL tree with the message field's text set (the field took the text)."""
    t = copy.deepcopy(t)

    def walk(n):
        if (n.get("id") or "").endswith(":id/message"):
            n["text"] = text
            n["flags"] = [f for f in n.get("flags", []) if f != "showingHint"]
        for c in n.get("children", []):
            walk(c)
    for w in t["windows"]:
        if w.get("root"):
            walk(w["root"])
    return t


def type_args(**kw):
    base = dict(content="Merhaba dünya 😀", append=False, clear=False, enter=False, stdin=False, file=None)
    base.update(kw)
    return args(**base)


def test_type_uses_set_text_first_and_reads_back(home, monkeypatch):
    t0 = tree("under_keyboard-a")
    s = session(monkeypatch, t0)
    done = with_text(t0, "Merhaba dünya 😀")
    s._client.act_replies = [{"performed": True, "tree": done}]
    out = act.cmd_type(type_args(id="message"))
    assert out["method"] == "set_text" and out["value"] == "Merhaba dünya 😀" and out["verified"]
    assert [c[3] for c in s._client.calls if c[0] == "act"] == ["set_text"]
    assert s._client.count("clipboard") == 0


def test_type_ladder_order_when_the_field_ignores_everything(home, monkeypatch):
    t0 = tree("under_keyboard-a")
    s = session(monkeypatch, t0)
    s._client.act_replies = [{"performed": True, "tree": t0}] + [{"performed": True}] * 5
    with pytest.raises(UserError) as e:
        act.cmd_type(type_args(id="message", content="hello"))
    assert e.value.kind == "no-change"
    order = [c[3] if c[0] == "act" else c[0] for c in s._client.calls if c[0] in ("act", "clipboard")]
    assert order[0] == "set_text"
    assert "paste" in order and order.index("paste") > order.index("set_text")
    # the clipboard was set for the paste and then restored
    clips = [c[1] for c in s._client.calls if c[0] == "clipboard"]
    assert clips[0] == "hello" and clips[-1] == "old clip"
    # ASCII text: the last rung is adb input text
    assert any(cmd[:2] == ("input", "text") for cmd in s.shell_calls)
    steps = [x["method"] for x in e.value.data["steps"]]
    assert steps == ["set_text", "paste", "input"]


def test_type_never_uses_adb_input_for_non_ascii(home, monkeypatch):
    t0 = tree("under_keyboard-a")
    s = session(monkeypatch, t0)
    s._client.act_replies = [{"performed": True, "tree": t0}] + [{"performed": True}] * 5
    with pytest.raises(UserError):
        act.cmd_type(type_args(id="message", content="ğüş"))
    assert not any(cmd[:2] == ("input", "text") for cmd in s.shell_calls)


def test_type_accepts_a_field_that_reformats(home, monkeypatch):
    t0 = tree("under_keyboard-a")
    s = session(monkeypatch, t0)
    shown = with_text(t0, "(555) 123")
    s._client.act_replies = [{"performed": True, "tree": shown}]
    out = act.cmd_type(type_args(id="message", content="555123"))
    assert out["method"] == "set_text" and out["value"] == "(555) 123"
    # same digits, punctuation added by the field's formatter: verified, no warning
    assert out["verified"] is True and not out.get("warning")
    assert s._client.count("clipboard") == 0                # changed, so not "ignored": no retyping


def test_enter_below_api_30_is_keyevent_66(home, monkeypatch):
    t0 = tree("under_keyboard-a")
    s = session(monkeypatch, t0)
    done = with_text(t0, "q")
    s._client.act_replies = [{"performed": True, "tree": done}]
    out = act.cmd_type(type_args(id="message", content="q", enter=True))
    assert ("input", "keyevent", "66") in s.shell_calls
    assert s._client.count("act", "focus") == 0            # this field already has input focus
    assert out["steps"][-1] == {"method": "enter", "via": "keyevent", "focused": True}


def _unfocused(t):
    t = json.loads(json.dumps(t))

    def walk(n):
        if n.get("flags"):
            n["flags"] = [f for f in n["flags"] if f != "focused"]
        for c in n.get("children", []):
            walk(c)
    for w in t["windows"]:
        if w.get("root"):
            walk(w["root"])
    return t


def test_enter_focuses_the_field_before_the_text_below_api_30(home, monkeypatch):
    """A key event goes to the input focus, which ACTION_SET_TEXT does not give, so
    --enter went nowhere (a banking QA app calculator). Focus comes BEFORE the text:
    that field rewrote "250" to "25" when it gained focus after being filled."""
    t0 = _unfocused(tree("under_keyboard-a"))
    s = session(monkeypatch, t0)
    s._client.act_replies = [{"performed": True},                                   # focus
                             {"performed": True, "tree": with_text(tree("under_keyboard-a"), "q")}]
    out = act.cmd_type(type_args(id="message", content="q", enter=True))
    acts = [c[3] for c in s._client.calls if c[0] == "act"]
    assert acts[:2] == ["focus", "set_text"] and acts.count("focus") == 1
    assert [x["method"] for x in out["steps"]] == ["focus", "set_text", "enter"]
    assert out["steps"][-1] == {"method": "enter", "via": "keyevent", "focused": True}
    assert ("input", "keyevent", "66") in s.shell_calls


# --- result shape -------------------------------------------------------------
def test_result_shape(home, monkeypatch):
    s = session(monkeypatch, tree("cart_inc-a"))
    s._client.act_replies = [{"performed": True, "clicked_event": True, "tree": tree("cart_inc-b"),
                              "events": [{"type": "clicked", "class": "android.widget.Button"},
                                         {"type": "toast", "text": "Saved!"},
                                         {"type": "window_content", "n": 3}]}]
    out = act.cmd_tap(args(target=str(plus_ref())))
    for k in ("ok", "changed", "method", "diff", "refs", "toast", "events", "screen", "text", "target"):
        assert k in out
    assert out["toast"] == "Saved!"
    assert [e["type"] for e in out["events"]] == ["clicked", "toast"]   # churn filtered out
    assert len(out["diff"]) <= act.DIFF_MAX
    # the new snapshot is saved: the next ref resolves on the fast path
    assert s.state["dump"] == S.build(tree("cart_inc-b")).dump


def test_mark_boxes_scale_with_the_image(home):
    state = S.to_state(S.build(tree("cart_inc-a")), "X")
    w, h = state["screen"][2], state["screen"][3]
    full = dict(act.mark_boxes(state, w, h))
    half = dict(act.mark_boxes(state, w // 2, h // 2))
    ref = next(iter(full))
    assert all(abs(a / 2 - b) <= 1 for a, b in zip(full[ref], half[ref]))
    assert full[ref] == tuple(state["refs"][str(ref)]["bounds"])


def test_scroll_at_the_edge_reports_it_and_never_swipes(home, monkeypatch):
    """The list scrolls (scroll_forward) but not up: it is at the start. The old
    fallback swiped anyway, which in a pager or pull-to-refresh does something else."""
    t = json.loads((FIX.parent / "resolve" / "long_list_scroll-a.json").read_text())["tree"]
    s = session(monkeypatch, t)
    lst = next(e.ref for e in S.build(t).elements if e.node.scrollable)
    out = act.cmd_scroll(args(ref=lst, direction="up"))
    assert out["edge"] == "start" and not out["changed"] and "warning" in out
    assert s._client.count("gesture") == 0 and s._client.count("act") == 0


def test_gesture_taps_settle_longer_unless_told():
    """No clicked event anchors a gesture's settle, and an activity the app starts
    after the tab's own feedback came after 150 ms of quiet (a banking QA app tabs)."""
    base = act._settle(args())
    assert act._tap_settle(args(), base)["quiet_ms"] == act.GESTURE_TAP_QUIET_MS
    assert act._tap_settle(args(), base)["timeout_ms"] == base["timeout_ms"]
    told = act._settle(args(settle=80))
    assert act._tap_settle(args(settle=80), told)["quiet_ms"] == 80     # --settle wins
    assert act._tap_settle(args(settle=0), None) is None                 # --settle 0: none


def test_a_settled_no_root_frame_is_read_again(home, monkeypatch):
    """After `back` the returning activity's root was not fetched in time
    (degraded: no-root) and no later event re-dumped it: the result showed an
    empty screen (a banking QA app, 4 in 5). finish() reads again, bounded."""
    rootless = dict(_trees("testapp-slow_a11y")[0], ms=120)   # a quick no-root: window not ready
    assert rootless.get("reason") == "no-root"
    s = session(monkeypatch, tree("cart_inc-a"))
    s._client.current = tree("cart_inc-b")                  # what a fresh read returns
    s._client.tree = lambda **kw: tree("cart_inc-b")
    out = act.finish(s, args(), "back", {"performed": True, "tree": rootless},
                     act._pre_lines(s, None))
    assert out["screen"]["sig"] == S.build(tree("cart_inc-b")).sig
    assert not S.build(tree("cart_inc-b")).degraded


def test_session_tree_reads_past_an_activity_transition(home, monkeypatch):
    """A tree read mid-transition shows two screens; Session.tree() reads again
    (bounded) until the closing window is gone (real Settings back transition)."""
    mid = _trees("real-settings-back-transition")[0]
    done = tree("cart_inc-a")
    s = session(monkeypatch, done)
    frames = [mid, mid, done]
    s._client.tree = lambda **kw: frames.pop(0) if frames else done
    assert s.tree() is done and not frames


def test_a_slow_no_root_frame_is_not_read_again(home, monkeypatch):
    """A slow no-root dump is a busy app (ui_hang): re-reading only delays the answer."""
    rootless = _trees("testapp-slow_a11y")[0]                  # ms 1504
    s = session(monkeypatch, tree("cart_inc-a"))
    calls = []
    s._client.tree = lambda **kw: calls.append(1) or tree("cart_inc-b")
    out = act.finish(s, args(), "back", {"performed": True, "tree": rootless}, act._pre_lines(s, None))
    assert not calls and "degraded" in out["text"]


def test_swipe_points_scroll_down_moves_the_finger_up():
    (x0, y0), (x1, y1) = act._swipe_points("down", (0, 0, 1000, 2000))
    assert y1 < y0 and x0 == x1


def test_run_collects_step_results_and_stops_on_failure(home, monkeypatch):
    out = act.cmd_run(argparse.Namespace(step=["version", "tap", "version"], file=None,
                                         keep_going=False, device=None, json=True, no_auto_setup=True))
    assert out["ran"] == 2 and out["failed"] == 1 and out["ok"] is False
    assert out["results"][1]["error"]["kind"] in ("bad-args", "no-device")


def test_an_empty_clipboard_is_cleared_again_after_the_paste(home, monkeypatch):
    t0 = tree("under_keyboard-a")
    s = session(monkeypatch, t0)
    s._client.clip = None                              # nothing on the clipboard before
    s._client.act_replies = [{"performed": True, "tree": t0}] + [{"performed": True}] * 5
    with pytest.raises(UserError):
        act.cmd_type(type_args(id="message", content="hello"))
    clips = [c[1] for c in s._client.calls if c[0] == "clipboard"]
    assert clips[0] == "hello" and clips[-1] == ""    # our text never stays on the clipboard


def test_a_password_never_goes_through_the_clipboard(home, monkeypatch):
    t0 = json.loads((FIX.parent / "trees" / "testapp-password.json").read_text())["tree"]
    s = session(monkeypatch, t0)
    s._client.act_replies = [{"performed": False, "tree": t0}] + [{"performed": True}] * 5
    with pytest.raises(UserError) as e:
        act.cmd_type(type_args(id="password", content="hunter22"))
    assert s._client.count("clipboard") == 0
    steps = e.value.data["steps"]
    assert any(x.get("skipped") for x in steps if x["method"] == "paste")


def test_digits_after_a_locator_are_the_text_not_a_ref(home, monkeypatch):
    """`type --id otp1 123456`: an all-digit text must not be read as ref 123456."""
    t0 = tree("under_keyboard-a")
    s = session(monkeypatch, t0)
    s._client.act_replies = [{"performed": True, "tree": t0}]
    a = type_args(id="message", target="123456", content=None)
    with pytest.raises(UserError):          # the fake field never changes: no-change, not bad-args
        act.cmd_type(a)
    assert a.content == "123456" and a.target is None
    sets = [c for c in s._client.calls if c[0] == "act" and c[3] == "set_text"]
    assert sets and sets[0][4]["text"] == "123456"


# --- logs: a crashed app's stack comes from the crash buffer ----------------------
def test_crash_blocks_pick_the_package_crash_by_its_pid():
    """Captured from the SM-N950F after TESTAPP `crash`: the stack lines don't name
    the package, only the `Process: <pkg>, PID: N` line does."""
    import pathlib
    log = (pathlib.Path(__file__).parent / "fixtures" / "logs" / "crash-buffer.txt").read_text()
    lines = act.crash_blocks(log, "dev.droidctl.testapp")
    text = "\n".join(lines)
    assert "FATAL EXCEPTION" in text and "deliberate crash" in text
    assert len(lines) > 5 and all(" E AndroidRuntime" in x for x in lines)
    assert act.crash_blocks(log, "com.example.other") == []
    assert act.crash_blocks("", "dev.droidctl.testapp") == []


@pytest.mark.parametrize("val,target,same", [
    ("(555) 123 45 67", "5551234567", True),
    ("+90 555 123 45 67", "+905551234567", True),
    ("555123456", "5551234567", False),         # a length limit ate a digit
    ("Ada 5551234567", "5551234567", False),    # letters: not a formatter
    ("hello", "hello", False),                  # not numeric at all (plain equality handles it)
])
def test_formatted_digits_count_as_the_typed_value(val, target, same):
    assert act._same_digits(val, target) is same


def test_an_activity_event_with_a_lagging_window_list_is_caught_up(home, monkeypatch):
    """Android 9: the new activity's window_state arrives inside the settle, but the
    settled tree still shows the old screen (a banking QA app: Home tab). The result
    must report the new screen, not `unchanged`."""
    s = session(monkeypatch, tree("cart_inc-a"))
    ev = {"type": "window_state", "class": "com.example.DashboardActivity", "seq": 7}
    s._client.act_replies = [{"performed": True, "clicked_event": True, "tree": tree("cart_inc-a"),
                              "events": [ev]}]
    later = tree("cart_inc-b")
    real_tree = s._client.tree
    s._client.tree = lambda **kw: (real_tree(**kw), later)[1]
    out = act.cmd_tap(args(target=str(plus_ref())))
    assert out["changed"] and out["method"] == "action"


def test_no_catch_up_without_an_activity_event(home, monkeypatch):
    s = session(monkeypatch, tree("cart_inc-a"))
    s._client.act_replies = [{"performed": True, "clicked_event": True, "tree": tree("cart_inc-a"),
                              "events": [{"type": "clicked", "seq": 3}]}]
    out = act.cmd_tap(args(target=str(plus_ref())))
    assert not out["changed"] and s._client.count("tree") == 0


def test_catch_up_waits_past_a_rootless_frame(home, monkeypatch):
    """The first re-read can be the new activity with no root yet (degraded):
    that is a different sig, but not the screen to report."""
    s = session(monkeypatch, tree("cart_inc-a"))
    ev = {"type": "window_state", "class": "com.example.DashboardActivity", "seq": 7}
    s._client.act_replies = [{"performed": True, "clicked_event": True, "tree": tree("cart_inc-a"),
                              "events": [ev]}]
    rootless = json.loads((FIX.parent / "trees" / "testapp-slow_a11y.json").read_text())["tree"]
    frames = [rootless, tree("cart_inc-b")]
    s._client.tree = lambda **kw: frames.pop(0) if len(frames) > 1 else frames[0]
    out = act.cmd_tap(args(target=str(plus_ref())))
    assert out["changed"] and not out.get("screen_warnings")


def _wait_args(**kw):
    base = dict(device="FAKE", json=True, text=None, id=None, desc=None, role="progress", gone=True,
                exact=False, activity=None, toast=None, window=None, pkg=None, timeout=2.0,
                no_auto_setup=True)
    base.update(kw)
    return argparse.Namespace(**base)


def _trees(*names):
    return [json.loads((FIX.parent / "trees" / f"{n}.json").read_text())["tree"] for n in names]


def test_wait_role_gone_passes_a_rootless_frame_and_ends_when_the_spinner_goes(home, monkeypatch):
    """A bare spinner (no text, no id) is only reachable by role (a banking QA app login)."""
    frames = _trees("testapp-spinner_forever", "testapp-slow_a11y", "testapp-cart")
    s = session(monkeypatch, frames[0])
    s._client.tree = lambda **kw: frames.pop(0) if len(frames) > 1 else frames[0]
    out = act.cmd_wait(_wait_args())
    assert out["matched"] and not frames[1:]                  # consumed up to the loaded screen
    assert s.state["sig"] == S.build(frames[0]).sig           # the next ref resolves against it


def test_wait_role_shows_the_screen_it_saved(home, monkeypatch):
    """The saved state is what the next snapshot calls "unchanged", so the wait
    has to show every element of it, not just the header (a banking QA app tour)."""
    frames = _trees("testapp-spinner_forever", "testapp-cart")
    s = session(monkeypatch, frames[0])
    s._client.tree = lambda **kw: frames.pop(0) if len(frames) > 1 else frames[0]
    out = act.cmd_wait(_wait_args())
    loaded = S.build(frames[0])
    assert S.render(loaded, S.Opts()) in out["text"]
    assert all(f"[{e.ref}]" in out["text"] for e in loaded.elements)


def test_wait_role_times_out_on_a_spinner_that_never_ends(home, monkeypatch):
    s = session(monkeypatch, _trees("testapp-spinner_forever")[0])
    with pytest.raises(UserError) as e:
        act.cmd_wait(_wait_args(timeout=0.5))
    assert e.value.kind == "timeout"


def test_wait_role_refuses_device_side_conditions(home, monkeypatch):
    s = session(monkeypatch, _trees("testapp-cart")[0])
    with pytest.raises(UserError) as e:
        act.cmd_wait(_wait_args(id="spinner"))
    assert e.value.kind == "bad-args"
