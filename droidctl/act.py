"""Actions: resolve a target, act on the node, settle, verify with a diff.

Every command here takes the parsed argparse namespace and returns a payload
dict (never prints, never exits), so the CLI, `run` and the daemon all call the
same functions. Per-device state lives in a `Session` (a warm agent connection
plus the saved snapshot), cached per process by `get_session`; the daemon keeps
them alive between calls.

The tap policy (PLAN.md "Actions"):
  1. resolve the ref or locator; a text inside a row acts on its clickable ancestor;
  2. ACTION_CLICK on the node, with the settle folded into the same request;
  3. only if the device reports performed:false, one gesture tap at the centre
     of the element's largest uncovered area ("gesture-fallback").
performed:true means the view's click handler already ran, so it is never
tapped a second time, even without a TYPE_VIEW_CLICKED event or a visible
change (TESTAPP click_no_event measured a double trigger when we did); the
result carries a warning instead, and --method gesture is the explicit override.
"""
import argparse
import os
import re
import shlex
import subprocess
import sys
import time

from droidctl import device as dev
from droidctl import resolve as R
from droidctl import snapshot as S
from droidctl.core import UserError

DIFF_MAX = 80
SETTLE_QUIET_MS = 150
SETTLE_FIRST_MS = 600
SETTLE_CAP_MS = 2000


# --------------------------------------------------------------------------
# session: one warm agent connection + the saved snapshot, per device
# --------------------------------------------------------------------------
class Session:
    def __init__(self, serial, auto_setup=True):
        self.serial = serial
        self.auto_setup = auto_setup
        self._client = None
        self.info = None
        self._state = None
        self._state_loaded = False

    # -- connection
    @property
    def client(self):
        if self._client is None:
            self._connect()
        return self._client

    def _connect(self):
        try:
            self._client, self.info = dev.connect(self.serial)
        except UserError as e:
            if e.kind != "not-installed" or not self.auto_setup:
                raise
            self._setup()
            self._client, self.info = dev.connect(self.serial)
            return
        # an older agent than the one we bundle: upgrade in place (install -r keeps it enabled)
        if (self.auto_setup and (self.info.get("versionCode") or 0) < dev.AGENT_VERSION_CODE
                and os.path.exists(dev.APK_PATH)):
            self.close()
            self._setup(reinstall=True)
            self._client, self.info = dev.connect(self.serial)

    def _setup(self, reinstall=False):
        from droidctl import cli
        cli.cmd_setup(argparse.Namespace(device=self.serial, reinstall=reinstall, json=True))

    @property
    def sdk(self):
        _ = self.client
        return (self.info or {}).get("sdk") or 0

    def call(self, method, *args, retry=True, **kw):
        """A typed AgentClient call (or a raw RPC method); read-only calls reconnect
        once if the link dropped. Actions pass retry=False: never act twice."""
        def go():
            fn = getattr(self.client, method, None)
            return fn(*args, **kw) if fn is not None else self.client.call(method, *args, **kw)
        try:
            return go()
        except UserError as e:
            if e.kind != "connection" or not retry:
                if e.kind == "connection":
                    self.close()
                raise
            self.close()
            return go()

    def close(self):
        if self._client is not None:
            self._client.close()
        self._client = None

    # -- the saved snapshot (refs)
    @property
    def state(self):
        if not self._state_loaded:
            self._state = S.load_state(self.serial)
            self._state_loaded = True
        return self._state

    def save(self, snap):
        self._state = S.to_state(snap, self.serial)
        self._state_loaded = True
        S.save_state(self.serial, self._state)
        return self._state

    # -- device reads
    def tree(self):
        return self.call("tree", timeout=15.0)

    def snap(self):
        return S.build(self.tree())

    # -- adb (setup-type work; lazy adbutils)
    def adb(self, *cmd, timeout=30, check=True):
        """`adb -s SERIAL shell ...` via the adb binary (stdlib; no adbutils import)."""
        try:
            out = subprocess.run([dev.adb_path(), "-s", self.serial, *cmd],
                                 capture_output=True, text=True, timeout=timeout)
        except (subprocess.SubprocessError, OSError) as e:
            raise UserError(f"adb {' '.join(cmd[:3])}: {e}", "adb")
        if check and out.returncode != 0:
            raise UserError(f"adb {' '.join(cmd[:3])} failed: {(out.stderr or out.stdout).strip()[:300]}", "adb")
        return out.stdout

    def shell(self, *cmd, timeout=30, check=True):
        return self.adb("shell", *cmd, timeout=timeout, check=check)


_SESSIONS = {}


def get_session(serial, auto_setup=True):
    s = _SESSIONS.get(serial)
    if s is None:
        s = _SESSIONS[serial] = Session(serial, auto_setup)
    s.auto_setup = auto_setup
    return s


def close_sessions():
    for s in _SESSIONS.values():
        s.close()
    _SESSIONS.clear()


def session_for(a):
    serial = dev.resolve_serial(getattr(a, "device", None))
    return get_session(serial, auto_setup=not getattr(a, "no_auto_setup", False))


# --------------------------------------------------------------------------
# targets
# --------------------------------------------------------------------------
class Target:
    """What an action acts on: a node (handle/click in a dump) and/or a screen point."""

    def __init__(self, handle=None, click=None, dump=None, tap=None, res=None, pre=None,
                 tier=None, via=None, rec=None, point=False):
        self.handle, self.click, self.dump, self.tap = handle, click, dump, tap
        self.res, self.pre, self.tier, self.via, self.rec = res, pre, tier, via, rec
        self.point = point

    @property
    def occluded(self):
        return self.res.occluded if self.res is not None else None

    @property
    def node(self):
        return self.res.node if self.res is not None else None

    def describe(self):
        if self.point:
            return {"point": list(self.tap)}
        if self.res is not None:
            d = self.res.to_json()
            return {k: d[k] for k in ("ref", "tier", "via", "score", "moved", "occluded", "tap", "element")}
        r = self.rec or {}
        return {"ref": r.get("ref"), "tier": 0, "via": "fast-path", "tap": self.tap,
                "element": {"ref": r.get("ref"), "role": r.get("role"), "label": r.get("label"),
                            "id": r.get("id"), "bounds": r.get("bounds")}}


LOCATOR_KEYS = ("id", "text", "desc", "cls", "role", "right_of", "left_of", "above", "below", "near")


def _locator(a):
    return {k: getattr(a, k, None) for k in LOCATOR_KEYS if getattr(a, k, None) is not None}


def _ref_of(a):
    ref = getattr(a, "ref", None)
    pos = getattr(a, "target", None)
    if ref is None and pos is not None:
        m = re.fullmatch(r"\[?(\d+)\]?", str(pos).strip())
        if not m:
            raise UserError(f"{pos!r} is not a ref (use a number from `snapshot`, or --text/--id/…)",
                            "bad-args")
        ref = int(m.group(1))
    return ref


def _parse_point(s):
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*", s or "")
    if not m:
        raise UserError(f"--point {s!r}: expected X,Y in device pixels", "bad-args")
    return [int(float(m.group(1))), int(float(m.group(2)))]


def resolve_target(sess, a, fast=True):
    """A ref, a locator or --point -> Target. Unique or a typed error; never a guess."""
    point = getattr(a, "point", None)
    ref = _ref_of(a)
    loc = _locator(a)
    given = sum(bool(x) for x in (point, ref is not None, loc))
    if given == 0:
        raise UserError("no target: give a ref, a locator (--text/--id/--desc/--class/--role, "
                        "--right-of/…) or --point X,Y", "bad-args")
    if point and (ref is not None or loc):
        raise UserError("--point cannot be combined with a ref or a locator", "bad-args")
    if ref is not None and loc:
        raise UserError("give either a ref or a locator, not both", "bad-args")
    if point:
        return Target(tap=_parse_point(point), point=True, via="point")

    if ref is not None:
        state = sess.state
        if not state:
            raise UserError(f"no saved snapshot for {sess.serial}, so ref [{ref}] means nothing",
                            "stale-ref", hint="run: droidctl snapshot",
                            data={"reason": "gone", "ref": ref})
        if fast:
            gen = sess.call("gen").get("gen")
            rec = R.fast_path(state, ref, state.get("dump"), gen)
            if rec is not None:
                return Target(handle=rec["handle"], click=rec.get("click"), dump=rec["dump"],
                              tap=rec.get("tap"), tier=0, via="fast-path", rec=dict(rec, ref=ref))
        res = R.resolve_ref(state, ref, sess.tree())
    else:
        snap = sess.snap()
        res = R.find(snap, index=getattr(a, "index", None), **loc)
    return Target(handle=res.handle, click=res.click, dump=res.snap.dump, tap=res.tap, res=res,
                  pre=res.snap, tier=res.tier, via=res.via)


def _settle(a):
    """--settle MS|0 -> the device settle spec (None = no settle)."""
    ms = getattr(a, "settle", None)
    if ms == 0:
        return None
    return {"quiet_ms": ms or SETTLE_QUIET_MS, "first_ms": SETTLE_FIRST_MS,
            "timeout_ms": max(SETTLE_CAP_MS, (ms or 0) * 4)}


# --------------------------------------------------------------------------
# verify: new snapshot, diff, events -> the action result
# --------------------------------------------------------------------------
def _pre_lines(sess, t):
    """What the agent last saw: the fresh pre-action snapshot if we took one, else the saved state."""
    if t is not None and t.pre is not None:
        return {"lines": S.flat_lines(t.pre), "sig": t.pre.sig}
    st = sess.state or {}
    return {"lines": st.get("lines", []), "sig": st.get("sig")}


def _event_summary(events):
    out = []
    for e in events or []:
        typ = e.get("type")
        if typ in ("window_content", "scrolled", "windows_changed"):
            continue
        item = {"type": typ}
        for k in ("class", "text", "title", "shown", "pkg"):
            if e.get(k) not in (None, ""):
                item[k] = e[k]
        out.append(item)
    return out[:20]


def _toast(events):
    for e in events or []:
        if e.get("type") == "toast" and e.get("text"):
            return e["text"]
    return None


def finish(sess, a, method, reply, pre, t=None, warning=None, extra=None):
    """Build the post-action snapshot, diff it against `pre`, save it, shape the result."""
    tree = (reply or {}).get("tree") or sess.tree()
    post = S.build(tree)
    changes = S.diff(pre, post) if pre.get("lines") else []
    new_screen = bool(pre.get("sig")) and pre.get("sig") != post.sig
    changed = bool(changes) or new_screen
    sess.save(post)
    opts = S.Opts()
    if new_screen:
        text = S.render(post, opts)
    else:
        lines = changes[:DIFF_MAX]
        more = len(changes) - len(lines)
        text = "\n".join([S.header(post, opts)] + (lines or ["unchanged"])
                         + ([f"… {more} more changed lines (snapshot --diff shows them all)"] if more > 0 else []))
    events = (reply or {}).get("events") or []
    out = {
        "ok": True, "changed": changed, "method": method,
        "new_screen": new_screen,
        "diff": changes[:DIFF_MAX], "diff_total": len(changes),
        "refs": len(post.elements),
        "screen": {"pkg": post.pkg, "title": post.title or None, "sig": post.sig,
                   "keyboard": bool(post.keyboard), "dialog": post.dialog},
        "toast": _toast(events), "events": _event_summary(events),
        "text": text,
    }
    if reply:
        for k in ("performed", "clicked_event", "idle", "settle_ms"):
            if k in reply:
                out[k] = reply[k]
    if t is not None:
        out["target"] = t.describe()
    if warning:
        out["warning"] = warning
    if post.warnings:
        out["screen_warnings"] = post.warnings
    if extra:
        out.update(extra)
    if getattr(a, "expect_change", False) and not changed:
        raise UserError(f"{method}: nothing changed on screen", "no-change",
                        hint="the action ran; check the target, or drop --expect-change",
                        data={k: out[k] for k in ("method", "performed", "clicked_event", "target") if k in out})
    return out


# --------------------------------------------------------------------------
# node actions with the fast-path retry
# --------------------------------------------------------------------------
def _act(sess, t, action=None, custom=None, args=None, settle=None, force=False, use_click=False, a=None):
    """One `act` on the target; a stale fast-path handle is re-resolved once."""
    handle = (t.click if use_click else t.handle)
    if handle is None:
        return None
    try:
        return sess.call("act", t.dump, handle, action=action, custom=custom, args=args,
                         settle=settle, force=force, retry=False)
    except UserError as e:
        if e.kind != "stale-ref" or t.tier != 0 or a is None:
            raise
    # the device re-dumped or the node went away since the snapshot: resolve properly
    fresh = resolve_target(sess, a, fast=False)
    t.__dict__.update(fresh.__dict__)
    handle = (t.click if use_click else t.handle)
    if handle is None:
        return None
    return sess.call("act", t.dump, handle, action=action, custom=custom, args=args,
                     settle=settle, force=force, retry=False)


def _keyboard_force(t, method):
    """Under the keyboard ACTION_CLICK is fine (no touch), but Android 9 reports the
    node visible:false, so the agent's visibility check needs `force`."""
    R.check_occlusion(t.res, method) if t.res is not None else None
    occ = t.occluded
    return bool(occ and occ.get("by") == "keyboard" and method != "gesture")


def _gesture(sess, typ, points, settle, ms=None):
    return sess.call("gesture", typ, points, ms=ms, settle=settle, retry=False)


def cmd_tap(a):
    sess = session_for(a)
    t = resolve_target(sess, a)
    pre = _pre_lines(sess, t)
    settle = _settle(a)
    method = getattr(a, "method", None) or "auto"
    if getattr(a, "double", False):
        return _gesture_tap(sess, a, t, pre, settle, "double")
    if t.point:
        return finish(sess, a, "gesture", _gesture(sess, "tap", [t.tap], settle), pre, t)
    if method == "gesture":
        if t.res is not None:
            R.check_occlusion(t.res, "gesture")
        return _gesture_tap(sess, a, t, pre, settle, "tap")
    force = _keyboard_force(t, method)
    if t.click is None:
        if method == "action":
            raise UserError("the element has no clickable node (and no clickable ancestor)", "disabled",
                            hint="--method gesture taps its centre instead")
        return _gesture_tap(sess, a, t, pre, settle, "tap",
                            warning="no clickable node: tapped the element's centre")
    r = _act(sess, t, action="click", settle=settle, force=force, use_click=True, a=a)
    if method == "action" or r is None:
        return finish(sess, a, "action", r, pre, t)
    # auto: the event-gated single fallback
    if r.get("performed") and r.get("clicked_event"):
        return finish(sess, a, "action", r, pre, t)
    if r.get("performed"):
        # performed:true means Android already ran the view's click handler. A
        # gesture on top would run it twice (measured on TESTAPP click_no_event:
        # the handler fired twice), so never fall back here; say so instead.
        post_tree = r.get("tree") or sess.tree()
        r["tree"] = post_tree
        post = S.build(post_tree)
        if S.diff(pre, post) or (pre.get("sig") and pre["sig"] != post.sig):
            return finish(sess, a, "action", r, pre, t)
        return finish(sess, a, "action", r, pre, t,
                      warning="ACTION_CLICK was performed but sent no click event and changed nothing; "
                              "not tapping again (that could trigger it twice). "
                              "If nothing happened, retry with --method gesture")
    why = "ACTION_CLICK was not performed"
    if t.tap is None:
        return finish(sess, a, "action", r, pre, t, warning=why + "; no tap point for a fallback")
    if t.res is not None and t.occluded:
        R.check_occlusion(t.res, "gesture")
    g = _gesture(sess, "tap", [t.tap], settle)
    return finish(sess, a, "gesture-fallback", g, pre, t,
                  warning=f"{why}; tapped once at {t.tap[0]},{t.tap[1]}")


def _gesture_tap(sess, a, t, pre, settle, typ, warning=None):
    if t.tap is None:
        raise UserError("the element has no visible area to tap", "offscreen")
    return finish(sess, a, "gesture", _gesture(sess, typ, [t.tap], settle), pre, t, warning=warning)


def cmd_long_press(a):
    sess = session_for(a)
    t = resolve_target(sess, a)
    pre = _pre_lines(sess, t)
    settle = _settle(a)
    method = getattr(a, "method", None) or "auto"
    if t.point or method == "gesture":
        if t.res is not None:
            R.check_occlusion(t.res, "gesture")
        return finish(sess, a, "gesture", _gesture(sess, "long", [t.tap], settle), pre, t)
    force = _keyboard_force(t, method)
    node = t.node
    use_click = not (node is not None and node.long_clickable)
    r = _act(sess, t, action="long_click", settle=settle, force=force, use_click=use_click, a=a)
    if r is not None and (r.get("performed") or method == "action"):
        return finish(sess, a, "action", r, pre, t)
    if t.tap is None:
        return finish(sess, a, "action", r, pre, t, warning="ACTION_LONG_CLICK was not performed")
    g = _gesture(sess, "long", [t.tap], settle)
    return finish(sess, a, "gesture-fallback", g, pre, t,
                  warning="ACTION_LONG_CLICK was not performed; long-pressed at the element's centre")


def _node_action(a, action, args=None, custom=None, name=None):
    sess = session_for(a)
    t = resolve_target(sess, a)
    if t.point:
        raise UserError(f"{name or action} needs an element, not --point", "bad-args")
    pre = _pre_lines(sess, t)
    force = _keyboard_force(t, "action")
    r = _act(sess, t, action=action, custom=custom, args=args, settle=_settle(a), force=force, a=a)
    if r is not None and not r.get("performed"):
        avail = r.get("available") or []
        raise UserError(f"{name or action} was not performed on that element"
                        + (f" (it offers: {', '.join(map(str, avail))})" if avail else ""),
                        "unsupported", data={"available": avail})
    return finish(sess, a, "action", r, pre, t)


def cmd_action(a):
    return _node_action(a, None, custom=a.label, name=f"action {a.label!r}")


def cmd_set(a):
    sess = session_for(a)
    t = resolve_target(sess, a)
    node = t.node
    if node is not None and node.editable and "set_progress" not in node.actions:
        a.content, a.append, a.clear, a.enter, a.stdin, a.file = a.value, False, False, False, False, None
        return _type(sess, a, t)
    try:
        value = float(a.value)
    except ValueError:
        raise UserError(f"set {a.value!r}: a range needs a number", "bad-args")
    pre = _pre_lines(sess, t)
    r = _act(sess, t, action="set_progress", args={"value": value}, settle=_settle(a), a=a)
    if r is not None and not r.get("performed"):
        raise UserError("the element does not accept a value (no set_progress)", "unsupported",
                        data={"available": r.get("available")})
    return finish(sess, a, "action", r, pre, t)


def cmd_focus(a):
    sess = session_for(a)
    t = resolve_target(sess, a)
    n = t.node
    if n is not None and "focused" in n.flags and "focus" not in n.actions:
        return {"ok": True, "changed": False, "method": "none", "already": True,
                "target": t.describe(), "text": "already focused"}
    return _node_action(a, "focus")


def cmd_expand(a):
    return _node_action(a, "expand")


def cmd_collapse(a):
    return _node_action(a, "collapse")


def cmd_dismiss(a):
    return _node_action(a, "dismiss")


# --------------------------------------------------------------------------
# scroll / swipe / gestures
# --------------------------------------------------------------------------
DIRS = ("up", "down", "left", "right")
_SCROLL = {"down": ("scroll_down", "scroll_forward"), "up": ("scroll_up", "scroll_backward"),
           "right": ("scroll_right", "scroll_forward"), "left": ("scroll_left", "scroll_backward")}


def _main_scroller(snap):
    """The largest scrollable element on screen (the thing `scroll down` means)."""
    cands = [e for e in snap.elements if e.node.scrollable and e.rect]
    if not cands:
        return None
    from droidctl import spatial as sp
    return max(cands, key=lambda e: sp.area(e.rect))


def _scroll_action(node, direction):
    specific, generic = _SCROLL[direction]
    acts = node.actions if node is not None else set()
    if specific in acts:
        return specific
    return generic


def cmd_scroll(a):
    sess = session_for(a)
    if a.ref is None and a.target is None and not _locator(a):
        snap = sess.snap()
        e = _main_scroller(snap)
        pre = {"lines": S.flat_lines(snap), "sig": snap.sig}
        if e is None:
            return _swipe(sess, a, a.direction, None, pre, why="nothing scrollable: swiped the screen")
        res = R.Resolution(e, snap, "locator", via="largest-scroller")
        t = Target(handle=res.handle, click=res.click, dump=snap.dump, tap=res.tap, res=res, pre=snap)
    else:
        t = resolve_target(sess, a)
        pre = _pre_lines(sess, t)
    node = t.node
    action = _scroll_action(node, a.direction)
    r = _act(sess, t, action=action, settle=_settle(a), a=a)
    if r is not None and r.get("performed"):
        return finish(sess, a, "action", r, pre, t, extra={"action": action})
    box = (t.res.elem.rect if t.res is not None and t.res.elem is not None else None)
    return _swipe(sess, a, a.direction, box, pre, why=f"{action} not performed: swiped instead")


def _swipe_points(direction, box, frac=0.4):
    l, top, r, b = box
    w, h = r - l, b - top
    cx, cy = l + 0.6 * w if direction in ("up", "down") else l + 0.5 * w, top + 0.5 * h
    # "scroll down" moves the content up: the finger swipes up
    if direction == "down":
        return [[cx, top + 0.7 * h], [cx, top + (0.7 - frac) * h]]
    if direction == "up":
        return [[cx, top + 0.3 * h], [cx, top + (0.3 + frac) * h]]
    if direction == "right":
        return [[l + 0.8 * w, cy], [l + (0.8 - frac) * w, cy]]
    return [[l + 0.2 * w, cy], [l + (0.2 + frac) * w, cy]]


def _screen_box(sess):
    st = sess.state
    if st and st.get("screen"):
        return tuple(st["screen"])
    info = sess.info or sess.call("ping")
    return (0, 0, info["screen"]["w"], info["screen"]["h"])


def _swipe(sess, a, direction, box, pre, why=None):
    box = box or _screen_box(sess)
    pts = _swipe_points(direction, box)
    g = _gesture(sess, "swipe", pts, _settle(a), ms=getattr(a, "ms", None) or 400)
    return finish(sess, a, "gesture", g, pre, None, warning=why)


def cmd_swipe(a):
    """swipe DIR: a finger swipe (DIR is the finger's direction), on an element or the screen."""
    sess = session_for(a)
    box = None
    t = None
    if a.ref is not None or a.target is not None or _locator(a):
        t = resolve_target(sess, a)
        if t.res is not None and t.res.elem is not None:
            box = t.res.elem.rect
        elif t.rec and t.rec.get("bounds"):
            box = tuple(t.rec["bounds"])
        pre = _pre_lines(sess, t)
    else:
        pre = _pre_lines(sess, None)
    box = box or _screen_box(sess)
    l, top, r, b = box
    w, h = r - l, b - top
    d = a.distance
    if a.direction in ("up", "down"):
        x = l + 0.6 * w if t is None else l + 0.5 * w
        y0, y1 = (0.7, 0.7 - d) if a.direction == "up" else (0.3, 0.3 + d)
        pts = [[x, top + y0 * h], [x, top + y1 * h]]
    else:
        y = top + 0.5 * h
        x0, x1 = (0.85, 0.85 - d) if a.direction == "left" else (0.15, 0.15 + d)
        pts = [[l + x0 * w, y], [l + x1 * w, y]]
    g = _gesture(sess, "swipe", pts, _settle(a), ms=a.ms)
    return finish(sess, a, "gesture", g, pre, t, extra={"points": [[int(p[0]), int(p[1])] for p in pts]})


def cmd_gesture(a):
    sess = session_for(a)
    pts = [_parse_point(p) for p in re.split(r"\s+|;", a.path.strip()) if p]
    if len(pts) < 2:
        raise UserError("--path needs at least two points: 'X,Y X,Y …'", "bad-args")
    pre = _pre_lines(sess, None)
    g = _gesture(sess, "path", pts, _settle(a), ms=a.ms)
    return finish(sess, a, "gesture", g, pre, None, extra={"points": pts})


def cmd_scroll_to(a):
    """Scroll the main (or given) container until an element matching the locator is on screen."""
    sess = session_for(a)
    loc = _locator(a)
    if not loc:
        raise UserError("scroll-to needs --text/--id/--desc", "bad-args")
    first = sess.snap()
    pre = {"lines": S.flat_lines(first), "sig": first.sig}
    snap = first
    scrolls, direction = 0, a.direction
    tried_back = False
    while True:
        try:
            res = R.find(snap, **loc)
            if res.elem is not None and not res.occluded:
                elem = res.elem
                sess.save(snap)
                return {"ok": True, "found": True, "scrolls": scrolls, "ref": elem.ref,
                        "changed": snap.sig != pre["sig"] or S.flat_lines(snap) != pre["lines"],
                        "element": R._describe(elem),
                        "text": S.header(snap, S.Opts()) + "\n" + S.element_line(elem, snap, S.Opts())}
        except UserError as e:
            if e.kind not in ("not-found", "offscreen", "occluded"):
                raise
        if scrolls >= a.max_scrolls:
            break
        cont = _main_scroller(snap)
        if cont is None:
            break
        res = R.Resolution(cont, snap, "locator")
        action = _scroll_action(cont.node, direction)
        r = sess.call("act", snap.dump, res.handle, action=action,
                      settle={"quiet_ms": 150, "first_ms": 400, "timeout_ms": 1500}, retry=False)
        scrolls += 1
        new = S.build(r.get("tree") or sess.tree()) if r.get("performed") else snap
        if not r.get("performed") or S.flat_lines(new) == S.flat_lines(snap):
            # the end of the list: try the other way once
            if tried_back or a.one_way:
                break
            tried_back = True
            direction = {"down": "up", "up": "down", "left": "right", "right": "left"}[direction]
        snap = new
    sess.save(snap)
    raise UserError(f"{' '.join(f'{k}={v}' for k, v in loc.items())} not found after {scrolls} scrolls",
                    "not-found", data={"scrolls": scrolls})


# --------------------------------------------------------------------------
# type: set_text -> clipboard paste -> adb input text; read back until stable
# --------------------------------------------------------------------------
def _read_text(a):
    if getattr(a, "stdin", False):
        return sys.stdin.read().rstrip("\n")
    if getattr(a, "file", None):
        try:
            with open(a.file, encoding="utf-8") as f:
                return f.read().rstrip("\n")
        except OSError as e:
            raise UserError(f"cannot read {a.file}: {e.strerror}", "not-found")
    if a.content is None:
        if a.clear or a.enter:
            return ""
        raise UserError("type needs TEXT, --stdin or --file F", "bad-args")
    return a.content


def _field_value(n):
    """The text an input really holds (not its hint shown as text)."""
    if n is None:
        return None
    text = n.raw.get("text") or ""
    hint = n.raw.get("hint")
    if "showingHint" in n.flags or (hint and text == hint):
        return ""
    return text


def _all_nodes(snap):
    stack = list(snap.roots)
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n.children)


def _refind_field(snap, want):
    """The same input in a fresh snapshot: by resource id, else by box, else the focused one."""
    edits = [n for n in _all_nodes(snap) if n.editable]
    if want.get("id"):
        hits = [n for n in edits if n.raw.get("id") == want["id"]]
        if len(hits) == 1:
            return hits[0]
        if hits and want.get("rect"):
            return min(hits, key=lambda n: _dist(n.rect, want["rect"]))
    if want.get("rect"):
        same = [n for n in edits if n.rect and _dist(n.rect, want["rect"]) < 24]
        if len(same) == 1:
            return same[0]
    focused = [n for n in edits if "focused" in n.flags]
    return focused[0] if len(focused) == 1 else None


def _dist(r1, r2):
    return sum(abs(x - y) for x, y in zip(r1, r2))


def _want(t):
    n = t.node
    if n is not None:
        return {"id": n.raw.get("id"), "rect": n.rect, "password": "password" in n.flags,
                "value": _field_value(n)}
    r = t.rec or {}
    return {"id": r.get("id"), "rect": tuple(r["bounds"]) if r.get("bounds") else None,
            "password": False, "value": None}


def _readback(sess, want, first_tree=None, tries=6):
    """Read the field until the same value comes back twice (150 ms apart)."""
    last, tree = object(), first_tree
    snap = None
    for i in range(tries):
        if tree is None:
            time.sleep(0.15)
            tree = sess.tree()
        snap = S.build(tree)
        n = _refind_field(snap, want)
        val = _field_value(n)
        tree = None
        if val == last:
            return val, n, snap
        last = val
    return (None if last is object() else last), None, snap


def _input_text_arg(s):
    """`adb shell input text` escaping: spaces as %s, shell metacharacters quoted."""
    return shlex.quote(s.replace(" ", "%s"))


def _type(sess, a, t):
    text = _read_text(a)
    if t.point:
        raise UserError("type needs an input element, not --point", "bad-args")
    pre = _pre_lines(sess, t)
    want = _want(t)
    force = _keyboard_force(t, "action")
    current = want.get("value")
    if current is None and t.rec is not None:
        current = t.rec.get("text") or ""
    target = (current or "") + text if a.append else text
    steps, method, reply = [], None, None
    before = current

    def ok(val):
        return want["password"] or val == target

    # 1. ACTION_SET_TEXT (no tap, Unicode-safe)
    r = _act(sess, t, action="set_text", args={"text": target}, force=force,
             settle={"quiet_ms": 150, "first_ms": 400, "timeout_ms": 1500}, a=a)
    want = _want(t) if t.node is not None else want
    steps.append({"method": "set_text", "performed": bool(r and r.get("performed"))})
    val, node, snap = _readback(sess, want, (r or {}).get("tree"))
    method, reply = "set_text", r
    ignored = not (r and r.get("performed")) or (val == before and target != before)
    # 2. focus + clipboard paste (the field refused set_text)
    if ignored and not ok(val) and node is not None:
        prev = None
        try:
            sess.call("act", snap.dump, node.raw["handle"], action="focus", force=force, retry=False)
            clip = sess.call("clipboard", set=text if a.append else target)
            prev = clip.get("previous")
            cur = _field_value(node) or ""
            if not a.append and cur:
                sess.call("act", snap.dump, node.raw["handle"], action="set_selection",
                          args={"start": 0, "end": len(cur)}, force=force, retry=False)
            elif a.append:
                sess.call("act", snap.dump, node.raw["handle"], action="set_selection",
                          args={"start": len(cur), "end": len(cur)}, force=force, retry=False)
            r2 = sess.call("act", snap.dump, node.raw["handle"], action="paste", force=force,
                           settle={"quiet_ms": 150, "first_ms": 400, "timeout_ms": 1500}, retry=False)
            steps.append({"method": "paste", "performed": bool(r2.get("performed"))})
            val, node2, snap2 = _readback(sess, want, r2.get("tree"))
            method, reply = "paste", r2
            node, snap = node2 or node, snap2
        except UserError as e:
            steps.append({"method": "paste", "error": e.kind})
        finally:
            if prev is not None:
                try:
                    sess.call("clipboard", set=prev)
                except UserError:
                    pass
    # 3. adb input text (ASCII only), into the focused field
    if not ok(val) and (val == before and target != before) and text.isascii() and text:
        try:
            if node is not None:
                sess.call("act", snap.dump, node.raw["handle"], action="focus", force=force, retry=False)
            sess.shell("input", "text", _input_text_arg(text if a.append or not before else target))
            steps.append({"method": "input", "performed": True})
            val, node, snap = _readback(sess, want)
            method, reply = "input", None
        except UserError as e:
            steps.append({"method": "input", "error": e.kind})
    warning = None
    if not want["password"] and val != target:
        if val == before and target != before:
            raise UserError(f"the field did not take the text (it still shows {val!r})", "no-change",
                            data={"steps": steps, "value": val})
        warning = f"the field shows {val!r}, not {target!r} (a formatter, a length limit or auto-advance?)"
    if a.enter:
        _enter(sess, node, snap)
        steps.append({"method": "enter"})
        reply = None
    extra = {"value": None if want["password"] else val, "verified": want["password"] or val == target,
             "steps": steps}
    if want["password"]:
        extra["value_hidden"] = True
    return finish(sess, a, method, reply, pre, t, warning=warning, extra=extra)


def _enter(sess, node, snap):
    if sess.sdk >= 30 and node is not None:
        try:
            r = sess.call("act", snap.dump, node.raw["handle"], action="ime_enter", retry=False)
            if r.get("performed"):
                return
        except UserError as e:
            if e.kind not in ("unsupported", "stale-ref"):
                raise
    sess.shell("input", "keyevent", "66")
    sess.call("wait_idle", 150, 2000)


def cmd_type(a):
    sess = session_for(a)
    # `type "hello"`: one positional that is not a ref is the text, into the focused input
    if (a.content is None and a.target is not None and not re.fullmatch(r"\[?\d+\]?", a.target.strip())
            and not (a.stdin or a.file)):
        a.content, a.target = a.target, None
    if (a.target is None and a.ref is None and not _locator(a) and not a.point):
        # no target: the focused input
        snap = sess.snap()
        focused = [e for e in snap.elements if e.node.editable and e.flag("focused")]
        if len(focused) != 1:
            raise UserError("no target and no single focused input: give a ref or --text/--id",
                            "bad-args" if not focused else "ambiguous")
        e = focused[0]
        res = R.Resolution(e, snap, "locator", via="focused")
        t = Target(handle=res.handle, click=res.click, dump=snap.dump, tap=res.tap, res=res, pre=snap)
    else:
        t = resolve_target(sess, a)
    return _type(sess, a, t)


# --------------------------------------------------------------------------
# global actions and keys
# --------------------------------------------------------------------------
def _global(a, name):
    sess = session_for(a)
    pre = _pre_lines(sess, None)
    r = sess.call("global_action", name, settle=_settle(a), retry=False)
    if not r.get("performed"):
        raise UserError(f"the system refused global action {name}", "device")
    return finish(sess, a, name, r, pre)


def cmd_back(a):
    return _global(a, "back")


def cmd_home(a):
    return _global(a, "home")


def cmd_recents(a):
    return _global(a, "recents")


def cmd_notifications(a):
    return _global(a, "notifications")


def cmd_quick_settings(a):
    return _global(a, "quick_settings")


KEYS = {"enter": 66, "back": 4, "home": 3, "del": 67, "backspace": 67, "delete": 112, "tab": 61,
        "escape": 111, "esc": 111, "search": 84, "menu": 82, "space": 62, "up": 19, "down": 20,
        "left": 21, "right": 22, "volume_up": 24, "volume_down": 25, "power": 26, "camera": 27,
        "app_switch": 187, "move_home": 122, "move_end": 123, "page_up": 92, "page_down": 93}


def cmd_press(a):
    sess = session_for(a)
    key = a.key.strip()
    code = KEYS.get(key.lower().replace("-", "_"))
    if code is None:
        if re.fullmatch(r"\d+", key):
            code = int(key)
        elif re.fullmatch(r"(KEYCODE_)?[A-Z0-9_]+", key.upper()):
            code = key.upper() if key.upper().startswith("KEYCODE_") else "KEYCODE_" + key.upper()
        else:
            raise UserError(f"unknown key {key!r} (a name like enter/tab/del, a KEYCODE_*, or a number)",
                            "bad-args")
    pre = _pre_lines(sess, None)
    sess.shell("input", "keyevent", str(code))
    s = _settle(a)
    r = sess.call("wait_idle", s["quiet_ms"], s["timeout_ms"]) if s else {}
    return finish(sess, a, "key", {"events": r.get("events")}, pre, extra={"key": key, "code": code})


# --------------------------------------------------------------------------
# wait / current / apps
# --------------------------------------------------------------------------
def cmd_wait(a):
    sess = session_for(a)
    cond = {"text": a.text, "id": a.id, "desc": a.desc, "activity": a.activity, "window": a.window,
            "pkg": a.pkg}
    if a.pkg and not any(cond[k] for k in ("text", "id", "desc", "activity", "window")) and a.toast is None:
        cond["window"], cond["pkg"] = a.pkg, None     # --pkg alone: an app window of that package
    if a.toast is not None:
        cond["toast"] = a.toast
    cond = {k: v for k, v in cond.items() if v is not None}
    if a.gone:
        cond["gone"] = True
    if not cond or list(cond) == ["gone"]:
        raise UserError("wait needs --text/--id/--desc/--activity/--toast/--window/--pkg", "bad-args")
    if a.exact:
        cond["exact"] = True
    timeout_ms = int(a.timeout * 1000)
    r = sess.call("wait_for", timeout_ms=timeout_ms, **cond)
    what = " ".join(f"{k}={v!r}" for k, v in cond.items() if k not in ("exact",))
    out = {"ok": True, "matched": True, "ms": r.get("ms"), "condition": cond,
           "text": f"matched {what} after {r.get('ms')} ms"}
    for k in ("node", "activity", "toast", "window"):
        if r.get(k) is not None:
            out[k] = r[k]
    return out


def _dumpsys_activity(sess):
    out = sess.shell("dumpsys", "activity", "activities", check=False, timeout=15)
    m = re.search(r"mResumedActivity: ActivityRecord\{\S+ \S+ (\S+)", out) or \
        re.search(r"ResumedActivity: ActivityRecord\{\S+ \S+ (\S+)", out)
    return m.group(1) if m else None


def cmd_current(a):
    sess = session_for(a)
    r = sess.call("current")
    act = r.get("activity")
    pkg = r.get("pkg")
    if not act:
        comp = _dumpsys_activity(sess)
        if comp:
            pkg, _, cls = comp.partition("/")
            act = cls if not cls.startswith(".") else pkg + cls
    out = {"ok": True, "pkg": pkg, "activity": act, "keyboard": bool(r.get("keyboard")),
           "windows": r.get("windows", []), "gen": r.get("gen")}
    out["text"] = (f"{pkg or '?'}/{S.short_activity(f'{pkg}/{act}').split('/', 1)[-1] if act else '?'}"
                   f"  keyboard={'shown' if out['keyboard'] else 'hidden'}  windows={len(out['windows'])}")
    return out


def _launcher_component(sess, pkg):
    out = sess.shell("cmd", "package", "resolve-activity", "--brief",
                     "-a", "android.intent.action.MAIN", "-c", "android.intent.category.LAUNCHER", pkg,
                     check=False)
    lines = [x.strip() for x in out.splitlines() if "/" in x]
    return lines[-1] if lines else None


def _installed(sess, pkg):
    return "package:" in sess.shell("pm", "path", pkg, check=False)


def _after_launch(sess, a, pkg, pre, t0):
    timeout_ms = int(getattr(a, "timeout", 10.0) * 1000)
    try:
        w = sess.call("wait_for", timeout_ms=timeout_ms, window=pkg)
    except UserError as e:
        if e.kind != "timeout":
            raise
        raise UserError(f"{pkg} did not come to the front within {timeout_ms / 1000:g}s", "timeout",
                        hint="check `droidctl logs --pkg " + pkg + "` for a crash")
    s = _settle(a) or {"quiet_ms": SETTLE_QUIET_MS, "timeout_ms": SETTLE_CAP_MS}
    idle = sess.call("wait_idle", s["quiet_ms"], max(s["timeout_ms"], 3000))
    out = finish(sess, a, "launch", {"events": idle.get("events")}, pre,
                 extra={"pkg": pkg, "launch_ms": round((time.monotonic() - t0) * 1000),
                        "wait_ms": w.get("ms")})
    # a launch always shows the new screen in full
    snap_text = S.render(S.build(sess.tree()), S.Opts()) if not out["new_screen"] else out["text"]
    out["text"] = snap_text
    return out


def cmd_launch(a):
    sess = session_for(a)
    pkg = a.pkg
    if not _installed(sess, pkg):
        raise UserError(f"{pkg} is not installed", "not-found", hint="see: droidctl apps")
    pre = _pre_lines(sess, None)
    t0 = time.monotonic()
    if a.stop or a.clear:
        sess.shell("am", "force-stop", pkg)
    if a.clear:
        out = sess.shell("pm", "clear", pkg, check=False)
        if "Success" not in out:
            raise UserError(f"pm clear {pkg} failed: {out.strip()}", "adb")
    comp = a.activity or _launcher_component(sess, pkg)
    if comp:
        if comp.startswith("."):
            comp = f"{pkg}/{comp}"
        elif "/" not in comp:
            comp = f"{pkg}/{comp}"
        # no -W: it can hang forever when the launch only brings a task forward
        out = sess.shell("am", "start", "-n", comp, check=False)
        if "Error" in out:
            raise UserError(f"am start {comp}: {out.strip()[:300]}", "adb")
    else:
        out = sess.shell("monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1", check=False)
        if "No activities found" in out:
            raise UserError(f"{pkg} has no launcher activity (pass --activity)", "not-found")
    return _after_launch(sess, a, pkg, pre, t0)


def cmd_open_url(a):
    sess = session_for(a)
    pre = _pre_lines(sess, None)
    t0 = time.monotonic()
    out = sess.shell("am", "start", "-a", "android.intent.action.VIEW", "-d", shlex.quote(a.url), check=False)
    if "Error" in out:
        raise UserError(f"nothing handles {a.url}: {out.strip()[:300]}", "not-found")
    s = _settle(a) or {"quiet_ms": SETTLE_QUIET_MS, "timeout_ms": SETTLE_CAP_MS}
    idle = sess.call("wait_idle", s["quiet_ms"], max(s["timeout_ms"], 3000))
    return finish(sess, a, "open-url", {"events": idle.get("events")}, pre,
                  extra={"url": a.url, "ms": round((time.monotonic() - t0) * 1000)})


def cmd_stop_app(a):
    sess = session_for(a)
    sess.shell("am", "force-stop", a.pkg)
    return {"ok": True, "pkg": a.pkg, "text": f"stopped {a.pkg}"}


def cmd_apps(a):
    serial = dev.resolve_serial(a.device)
    sess = get_session(serial, auto_setup=False)
    flag = [] if a.all else ["-3"]
    out = sess.shell("pm", "list", "packages", *flag)
    pkgs = sorted(x.split(":", 1)[1].strip() for x in out.splitlines() if x.startswith("package:"))
    if a.filter:
        pkgs = [p for p in pkgs if a.filter.lower() in p.lower()]
    return {"ok": True, "apps": pkgs, "count": len(pkgs), "text": "\n".join(pkgs) or "(none)"}


def cmd_install(a):
    serial = dev.resolve_serial(a.device)
    sess = get_session(serial, auto_setup=False)
    if not os.path.exists(a.apk):
        raise UserError(f"{a.apk} does not exist", "not-found")
    out = sess.adb("install", "-r", *(["-g"] if a.grant else []), a.apk, timeout=300, check=False)
    if "Success" not in out:
        raise UserError(f"install failed: {out.strip()[-300:]}", "adb")
    return {"ok": True, "apk": a.apk, "text": f"installed {os.path.basename(a.apk)}"}


# --------------------------------------------------------------------------
# logs / watch
# --------------------------------------------------------------------------
LEVELS = ("V", "D", "I", "W", "E", "F")


def cmd_logs(a):
    serial = dev.resolve_serial(a.device)
    sess = get_session(serial, auto_setup=False)
    cmd = ["logcat", "-d", "-v", "threadtime", "-t", str(max(1, a.max * (4 if a.pkg else 1)))]
    if a.pkg:
        pid = sess.shell("pidof", a.pkg, check=False).strip().split()
        if not pid:
            # not running (maybe it crashed): fall back to lines that mention the package
            cmd += ["*:" + (a.level or "V")]
            lines = [x for x in sess.adb(*cmd, timeout=30).splitlines() if a.pkg in x]
            lines = lines[-a.max:]
            return {"ok": True, "lines": lines, "pid": None, "text": "\n".join(lines) or "(no lines)"}
        cmd += ["--pid", pid[0]]
    if a.level:
        cmd += ["*:" + a.level]
    lines = sess.adb(*cmd, timeout=30).splitlines()
    lines = [x for x in lines if x and not x.startswith("--------- beginning")][-a.max:]
    return {"ok": True, "lines": lines, "text": "\n".join(lines) or "(no lines)"}


def cmd_watch(a):
    """Pushed device events, bounded by --max and --timeout (an unbounded tail hangs an agent)."""
    sess = session_for(a)
    _ = sess.client
    port = sess.client.port
    events = []
    types = [t.strip() for t in a.events.split(",")] if a.events else None
    with dev.AgentClient(port) as c:
        c.subscribe(types)
        deadline = time.monotonic() + a.timeout
        while len(events) < a.max and time.monotonic() < deadline:
            got = False
            for ev in c.notifications(timeout=min(0.5, max(0.05, deadline - time.monotonic()))):
                got = True
                if ev and ev.get("type") in ("window_content", "scrolled", "windows_changed") and not a.all:
                    continue
                if ev:
                    events.append(ev)
                if len(events) >= a.max:
                    break
            if not got and time.monotonic() >= deadline:
                break
        try:
            c.unsubscribe()
        except UserError:
            pass
    lines = [f"{e.get('type')}  " + " ".join(f"{k}={e[k]!r}" for k in ("pkg", "class", "text", "title", "shown")
                                              if e.get(k) not in (None, "")) for e in events]
    return {"ok": True, "events": events, "count": len(events), "text": "\n".join(lines) or "(no events)"}


# --------------------------------------------------------------------------
# shot
# --------------------------------------------------------------------------
def cmd_shot(a):
    import base64
    sess = session_for(a)
    scale = 1.0 if a.full else a.scale
    crop = None
    state = sess.state
    if a.crop is not None:
        rec = (state or {}).get("refs", {}).get(str(a.crop))
        if not rec or not rec.get("bounds"):
            raise UserError(f"no element [{a.crop}] with a box in the last snapshot", "stale-ref",
                            hint="run: droidctl snapshot", data={"reason": "gone", "ref": a.crop})
        crop = rec["bounds"]
    if a.marks and not a.crop:
        snap = sess.snap()
        state = sess.save(snap)
    img = dev.screenshot(sess.client, sess.serial, scale=scale, quality=a.quality, crop=crop)
    data = base64.b64decode(img["data"])
    screen = (state or {}).get("screen") or [0, 0, (sess.info or {}).get("screen", {}).get("w", 0),
                                             (sess.info or {}).get("screen", {}).get("h", 0)]
    marks = 0
    if a.marks and state:
        data, marks = _draw_marks(data, state, img, crop)
    out_path = a.out or os.path.join(dev.home(), "shots", f"{sess.serial}-{int(time.time() * 1000)}.jpg")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(data)
    res = {"ok": True, "path": os.path.abspath(out_path), "w": img["w"], "h": img["h"],
           "scale": img.get("scale", scale), "source": img.get("source"), "bytes": len(data),
           "screen": [screen[2], screen[3]], "marks": marks,
           "text": f"{os.path.abspath(out_path)}  {img['w']}x{img['h']}  ({img.get('source')}"
                   + (f", {marks} marks" if marks else "") + ")"}
    if getattr(a, "base64", False):
        res["data"] = base64.b64encode(data).decode()
    return res


def mark_boxes(state, img_w, img_h, crop=None):
    """Ref boxes mapped from device px into image px: [(ref, (l, t, r, b))].

    The factor comes from the image itself (its size over the screen's or the
    crop's), so a resolution override or rounding can never misplace a box.
    """
    sw, sh = state["screen"][2], state["screen"][3]
    ox, oy, bw, bh = (crop[0], crop[1], crop[2] - crop[0], crop[3] - crop[1]) if crop else (0, 0, sw, sh)
    fx, fy = img_w / bw, img_h / bh
    out = []
    for k, r in state.get("refs", {}).items():
        b = r.get("bounds")
        if not b:
            continue
        box = ((b[0] - ox) * fx, (b[1] - oy) * fy, (b[2] - ox) * fx, (b[3] - oy) * fy)
        if box[2] <= 0 or box[3] <= 0 or box[0] >= img_w or box[1] >= img_h:
            continue
        out.append((int(k), tuple(round(v) for v in box)))
    return sorted(out)


def _draw_marks(data, state, img, crop):
    import io
    from PIL import Image, ImageDraw
    im = Image.open(io.BytesIO(data)).convert("RGB")
    d = ImageDraw.Draw(im)
    boxes = mark_boxes(state, im.width, im.height, crop)
    palette = [(230, 25, 75), (60, 180, 75), (0, 130, 200), (245, 130, 48), (145, 30, 180), (0, 128, 128)]
    for i, (ref, (l, t, r, b)) in enumerate(boxes):
        c = palette[i % len(palette)]
        d.rectangle([l, t, r, b], outline=c, width=2)
        label = str(ref)
        tw = 7 * len(label) + 4
        d.rectangle([l, t, l + tw, t + 13], fill=c)
        d.text((l + 2, t), label, fill=(255, 255, 255))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=img.get("quality", 80) if isinstance(img, dict) else 80)
    return buf.getvalue(), len(boxes)


# --------------------------------------------------------------------------
# run: many steps, one process, one session
# --------------------------------------------------------------------------
def cmd_run(a):
    from droidctl import cli
    if a.step:
        raw = a.step
    else:
        if a.file and a.file != "-":
            try:
                with open(a.file, encoding="utf-8") as f:
                    raw = list(f)
            except OSError as e:
                raise UserError(f"cannot read {a.file}: {e.strerror}", "not-found")
        else:
            raw = list(sys.stdin)
    steps = [s.strip() for s in raw if s.strip() and not s.strip().startswith("#")]
    if not steps:
        raise UserError("no steps given (use --step, a FILE, or stdin)", "bad-args")
    parser = cli.build_parser()
    results, failed = [], 0
    import contextlib
    import io
    for i, line in enumerate(steps, 1):
        try:
            tokens = shlex.split(line)
        except ValueError as e:
            tokens, err = None, str(e)
        sa = None
        if tokens:
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    sa = parser.parse_args(tokens)
                except SystemExit:
                    sa = None
            err = f"could not parse {line!r}"
        if sa is None or sa.cmd == "run":
            results.append({"step": i, "cmd": line, "ok": False,
                            "error": {"kind": "bad-args",
                                      "message": "run cannot nest" if sa is not None else err}})
            failed += 1
            if not a.keep_going:
                break
            continue
        if getattr(sa, "device", None) is None and getattr(a, "device", None):
            sa.device = a.device
        if hasattr(a, "no_auto_setup") and a.no_auto_setup:
            sa.no_auto_setup = True
        sa.json = True
        try:
            res = sa.fn(sa)
            results.append({"step": i, "cmd": line, "ok": not (isinstance(res, dict) and res.get("ok") is False),
                            "result": res})
        except UserError as e:
            error = {"kind": e.kind, "message": str(e)}
            if e.hint:
                error["hint"] = e.hint
            if getattr(e, "data", None):
                error["data"] = e.data
            results.append({"step": i, "cmd": line, "ok": False, "error": error})
            failed += 1
            if not a.keep_going:
                break
    text = []
    for r in results:
        head = f"── step {r['step']}: {r['cmd']}"
        if r["ok"]:
            body = (r.get("result") or {}).get("text") if isinstance(r.get("result"), dict) else None
            text.append(head + ("\n" + body if body else "  ok"))
        else:
            text.append(head + f"\n{r['error']['kind']}: {r['error']['message']}")
    return {"ok": failed == 0, "steps": len(steps), "ran": len(results), "failed": failed,
            "results": results, "text": "\n".join(text)}
