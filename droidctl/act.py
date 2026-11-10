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
# A gesture tap has no clicked event to anchor the settle, and the target's own
# feedback (a tab turning selected) is the first change; an activity the app then
# starts can come after 150 ms of quiet (a real app bottom tabs: START ~400 ms
# later, missed 1 in 3). Gesture taps are the rare path, so they wait longer.
GESTURE_TAP_QUIET_MS = 450


# --------------------------------------------------------------------------
# session: one warm agent connection + the saved snapshot, per device
# --------------------------------------------------------------------------
_TREE_CACHE = {}    # serial -> the last iOS tree read in this process (the daemon keeps it between commands)


class Session:
    def __init__(self, serial, auto_setup=True):
        self.serial = serial
        self.auto_setup = auto_setup
        self._client = None
        self.info = None
        self._state = None
        self._state_loaded = False
        self.from_cache = False                # the target was resolved on _TREE_CACHE's tree

    # -- connection
    @property
    def client(self):
        if self._client is None:
            self._connect()
        return self._client

    def _connect(self):
        if dev.is_ios(self.serial):             # the runner adopts or starts itself; setup is guided
            self._client, self.info = dev.connect(self.serial)
            return
        try:
            self._client, self.info = dev.connect(self.serial)
        except UserError as e:
            not_bound = e.kind == "connection" and (e.data or {}).get("reason") == "not-bound"
            if not (e.kind == "not-installed" or not_bound) or not self.auto_setup:
                raise
            self._setup()                       # installs/enables, or rebinds a dead service
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
    def ios(self):
        return dev.is_ios(self.serial)

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

    def save(self, snap, shown=True):
        """``shown``: the agent was shown this screen in full (or as a diff of
        one it saw), so a plain `snapshot` of the same screen may say `unchanged`."""
        S.carry_refs(self.state, snap)          # same screen: the agent's numbers stay valid
        self._state = S.to_state(snap, self.serial, shown=shown)
        self._state_loaded = True
        S.save_state(self.serial, self._state)
        return self._state

    # -- device reads
    def tree(self, cached=False):
        """The current tree, never a frame mid-activity-transition (bounded wait).

        ``cached`` (iOS, for resolving an action's target): the last tree this process read, when the
        runner has seen no change (its `gen`) and taken no other dump since: a full read costs 250-460 ms
        on a 300-450-node screen (real apps, 2026-10-26), `gen` ~20 ms. Never for `snapshot`/`wait`: a
        label can change without a notification (a live price)."""
        if cached and self.ios:
            c = _TREE_CACHE.get(self.serial)
            if c is not None:
                g = self.call("gen")
                if g.get("gen") == c.get("gen") and g.get("dump") == c.get("dump"):
                    self.from_cache = True
                    return c
        t = self.call("tree", timeout=15.0)
        deadline = time.monotonic() + TRANSITION_WAIT_S
        while S.leaving_windows(t) and time.monotonic() < deadline:
            time.sleep(0.1)
            t = self.call("tree", timeout=15.0)
        self.remember(t)
        return t

    def remember(self, tree):
        if self.ios and isinstance(tree, dict) and tree.get("dump") is not None:
            _TREE_CACHE[self.serial] = tree

    def forget(self):
        _TREE_CACHE.pop(self.serial, None)
        self.from_cache = False

    def snap(self, cached=False):
        return self.build(self.tree(cached=cached))

    def build(self, tree):
        """A snapshot numbered like the saved state (carry_refs), so every number an
        action result shows is the one a later `tap N` resolves. Built without it,
        a result after a scroll showed "Item 16" as [30] while the state saved it as
        [34]: [30] then acted on another element."""
        snap = S.build(tree)
        S.carry_refs(self.state, snap)
        return snap

    # -- adb (setup-type work; lazy adbutils)
    def adb(self, *cmd, timeout=30, check=True):
        """`adb -s SERIAL shell ...` via the adb binary (stdlib; no adbutils import)."""
        if self.ios:
            raise UserError("this command is not available on iOS yet", "unsupported",
                            hint="IOS-INTEGRATION.md §6 lists what works on an iPhone")
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
        self.scrolls = 0              # --scroll: how many scrolls brought it into view

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
            out = {k: d[k] for k in ("ref", "tier", "via", "score", "moved", "occluded", "tap", "element")}
            if self.scrolls:
                out["scrolls"] = self.scrolls
            return out
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
    if getattr(a, "scroll", False) and not loc:
        raise UserError("--scroll needs a locator (--text/--id/--desc/…); a ref that scrolled away "
                        "says so (offscreen) and names its scroll-to", "bad-args")
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
        tree = sess.tree()
        _awake(S.build(tree))
        try:
            res = R.resolve_ref(state, ref, tree)
        except UserError as e:
            raise _locked(e, S.build(tree))
    else:
        snap = sess.snap(cached=fast)
        _awake(snap)
        scrolls = 0
        try:
            try:
                res = R.find(snap, index=getattr(a, "index", None), **loc)
            except UserError as e:
                if not (getattr(a, "scroll", False) and e.kind in ("not-found", "offscreen")):
                    raise
                # --scroll: scroll it into view, then resolve it there (still unique or an error)
                found, scrolls, snap = _scroll_into_view(sess, a, loc, snap)
                if found is None:
                    raise UserError(f"{e} (and not after {scrolls} scrolls)", "not-found",
                                    data=dict(e.data or {}, scrolls=scrolls))
                res = R.find(snap, index=getattr(a, "index", None), **loc)
        except UserError as e:
            raise _locked(_not_found_here(e, snap, loc, a), snap)
        t = Target(handle=res.handle, click=res.click, dump=res.snap.dump, tap=res.tap, res=res,
                   pre=res.snap, tier=res.tier, via=res.via)
        t.scrolls = scrolls
        return t
    return Target(handle=res.handle, click=res.click, dump=res.snap.dump, tap=res.tap, res=res,
                  pre=res.snap, tier=res.tier, via=res.via)


def _not_found_here(err, snap, loc, a):
    """A locator miss says which screen it looked at, and whether a list could
    hold the target: "absent", "scrolled out" and "the wrong screen" looked the
    same before (a hardened finance app: the agent was on another activity)."""
    if err.kind != "not-found" or "tried" not in (err.data or {}):
        return err
    where = _where(snap)
    hint = err.hint
    if _main_scroller(snap) is not None and not getattr(a, "scroll", False):
        hint = ("a list here may hold it: add --scroll (scrolls it into view, then acts), "
                "or check the screen with droidctl snapshot --find TEXT")
    return UserError(f"{err} on {where}", err.kind, hint=hint,
                     data=dict(err.data, activity=snap.activity or None, pkg=snap.pkg))


WAKE_HINT = "wake it: droidctl press KEYCODE_WAKEUP (then unlock the phone if it asks)"


def _awake(snap):
    """Node actions are not user activity: a session of them lets the screen time out,
    and then nothing matches. Say so instead of not-found."""
    if snap.off:
        raise UserError("the screen is off", "screen-off", hint=WAKE_HINT)


def _locked(err, snap):
    """A failed match while the keyguard is up is about the lock screen, not the app."""
    if snap.locked and err.kind in ("not-found", "stale-ref", "ambiguous"):
        return UserError(f"the phone is locked ({err})", "screen-off", hint=WAKE_HINT, data=err.data)
    return err


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


CATCH_UP_S = 1.5
TRANSITION_WAIT_S = 1.0     # an activity animation is ~200-400 ms; bounded so nothing hangs
QUICK_DUMP_MS = 800         # a no-root dump faster than this was a window not ready, not a busy app


def _moved_on(events, pre):
    """The events say another activity came to the front, but the settled tree
    still shows the old screen: on Android 9 the window list can lag the
    TYPE_WINDOW_STATE_CHANGED event (a real app: Home tab -> DashboardActivity)."""
    was = pre.get("activity") or ""
    for e in events:
        cls = e.get("class") or ""
        if e.get("type") == "window_state" and cls.endswith("Activity") and not was.endswith(cls.split(".")[-1]):
            return True
    return False


def _catch_up(sess, pre, post):
    """Re-read the tree (bounded, only in the lagging case above) until it changes."""
    deadline = time.monotonic() + CATCH_UP_S
    changes, new_screen = [], False
    while time.monotonic() < deadline:
        time.sleep(0.15)
        post = sess.build(sess.tree())
        changes = S.diff(pre, post) if pre.get("lines") else []
        new_screen = bool(pre.get("sig")) and pre.get("sig") != post.sig
        if (changes or new_screen) and not _transitional(post):
            break
    return post, changes, new_screen


LOADING_WAIT_S = 8.0        # iOS: a spinner the action brought up (a real app's login: 6 s; rates: < 1 s)
_PROGRESS_LINE = re.compile(r"^\s*\[\d+\] progress\b")


def _spinners(snap):
    """Indeterminate spinners on screen, also one a row swallowed (a table cell holding only a spinner
    reads as row "In progress": a real app's Transactions, 2026-10-26)."""
    def spins(n):
        return n.cls.endswith("ProgressBar") and not n.raw.get("range")

    def inside(n):
        return any(spins(c) or inside(c) for c in n.children)
    return sum(1 for e in snap.elements
               if (e.role == "progress" and not e.node.raw.get("range")) or (e.role != "progress" and inside(e.node)))


SPLASH_WAIT_S = 3.0         # iOS: a splash (a logo and a name, nothing to act on); a real app's: ~1 s


def _actionable(snap):
    return sum(1 for e in snap.elements if e.node.actionable)


def _loading_catch_up(sess, pre, post):
    """iOS: the action settled on a loading spinner it brought up (the run loop idles while a request is
    out), or on a splash (nothing to act on where the screen before had something): read again until
    that is over, bounded -> (post, waited ms, still loading). A spinner that was there before the
    action (a banner's) is not waited for."""
    had_spinner = any(_PROGRESS_LINE.match(ln) for ln in pre.get("lines") or [])
    spinner = _spinners(post) and not had_spinner
    splash = not _actionable(post) and any(re.search(r"\] (button|input|row|switch|link)\b", ln)
                                           for ln in pre.get("lines") or [])
    if not (spinner or splash):
        return post, 0, False
    cap = LOADING_WAIT_S if spinner else SPLASH_WAIT_S
    t0 = time.monotonic()
    while time.monotonic() - t0 < cap:
        time.sleep(0.15)
        post = sess.build(sess.tree())
        if (had_spinner or not _spinners(post)) and _actionable(post):
            return post, int((time.monotonic() - t0) * 1000), False
    return post, int((time.monotonic() - t0) * 1000), True


TRANSITION_OVERLAPS = 3     # iOS: overlapping pairs that mean two screens are drawn at once
TRANSITION_CATCH_UP_S = 1.5


def _transition_catch_up(sess, post):
    """iOS: the settle ended while the app waited for its server, and the push that followed was read
    half-way: the old and the new screen overlapping (a real app's calculator, 2026-10-26). Read
    again until the screen stops changing, bounded -> (post, waited ms)."""
    if len(getattr(post, "overlaps", [])) < TRANSITION_OVERLAPS:
        return post, 0
    t0 = time.monotonic()
    while time.monotonic() - t0 < TRANSITION_CATCH_UP_S:
        time.sleep(0.15)
        nxt = sess.build(sess.tree())
        if nxt.sig == post.sig and len(nxt.overlaps) == len(post.overlaps):
            break                              # stable: laid out like that, not moving
        post = nxt
        if len(post.overlaps) < TRANSITION_OVERLAPS:
            break
    return post, int((time.monotonic() - t0) * 1000)


LATE_CHANGE_S = 1.2


def _late_change_catch_up(sess, pre, post):
    """iOS: a delivered tap settled with nothing changed, but the app was only waiting a moment before
    pushing (idle >100 ms between the touch and the push: a real app's Discover rows answered
    "unchanged" and the next screen came ~0.45 s later, 2026-10-26). Read again, bounded -> (post, ms)."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < LATE_CHANGE_S:
        time.sleep(0.15)
        nxt = sess.build(sess.tree())
        if nxt.sig != pre.get("sig") or S.diff(pre, nxt):
            post, moving_ms = _transition_catch_up(sess, nxt)
            return post, int((time.monotonic() - t0) * 1000)
    return post, int((time.monotonic() - t0) * 1000)


def _transitional(snap):
    """A frame mid-transition: an app window with no root yet, nothing drawn, or
    a closing activity window still stacked over the new one."""
    return bool(snap.degraded or getattr(snap, "incomplete", False) or not snap.elements
                or getattr(snap, "leaving", False))


HANDLED_NO_CHANGE = ("unchanged, but the app received the click (click event): it was handled; "
                     "do not tap again. Its effect may not be visible here (logged, background work)")


def finish(sess, a, method, reply, pre, t=None, warning=None, extra=None):
    """Build the post-action snapshot, diff it against `pre`, save it, shape the result."""
    tree = (reply or {}).get("tree")
    if tree is None or S.leaving_windows(tree):   # settled mid-transition: read again
        tree = sess.tree()
    else:
        sess.remember(tree)                       # the runner's latest dump: the next action resolves on it
    post = sess.build(tree)
    # Settled on a frame the new screen has not drawn into yet, or whose root the
    # agent could not fetch in time (after `back` the returning activity came back
    # degraded:no-root in 4 of 5, a real app, and no later event re-dumps it):
    # read again, bounded, until something is drawn. A screen that really is empty
    # (a canvas, a splash) costs TRANSITION_WAIT_S once and is reported as it is.
    # Not with --settle 0 (the caller asked for the frame as it is), and a no-root
    # only when the dump was quick: a slow no-root is a busy app (ui_hang), where
    # every read costs the 2 s budget and reading again only delays the answer.
    def not_drawn_yet(snap, raw):
        if not snap.elements and not snap.degraded:
            return True
        return snap.degraded == "no-root" and (raw or {}).get("ms", 0) < QUICK_DUMP_MS
    deadline = time.monotonic() + TRANSITION_WAIT_S
    raw = tree
    while (getattr(a, "settle", None) != 0 and pre.get("lines") and not_drawn_yet(post, raw)
           and time.monotonic() < deadline):
        time.sleep(0.12)
        raw = sess.tree()
        post = sess.build(raw)
    loading_ms, still_loading = 0, False
    if sess.ios and getattr(a, "settle", None) != 0 and pre.get("lines"):
        post, loading_ms, still_loading = _loading_catch_up(sess, pre, post)
        post, moving_ms = _transition_catch_up(sess, post)
        loading_ms += moving_ms
    events = (reply or {}).get("events") or []
    seqs = [e["seq"] for e in events if isinstance(e.get("seq"), int)]
    # the toasts this result reports must not come back in the next snapshot's header
    post.evseq = max(seqs) if seqs else pre.get("evseq")
    changes = S.diff(pre, post) if pre.get("lines") else []
    new_screen = bool(pre.get("sig")) and pre.get("sig") != post.sig
    changed = bool(changes) or new_screen
    if (not changed and sess.ios and (reply or {}).get("delivered") and getattr(a, "settle", None) != 0
            and pre.get("lines")):
        post, late_ms = _late_change_catch_up(sess, pre, post)
        changes = S.diff(pre, post)
        new_screen = pre.get("sig") != post.sig
        changed = bool(changes) or new_screen
        if changed:
            loading_ms += late_ms
    if not changed and _moved_on(events, pre):
        post, changes, new_screen = _catch_up(sess, pre, post)
        changed = bool(changes) or new_screen
    sess.save(post)
    # The app received the click, but nothing on screen shows it (a click that only
    # logs, starts work in the background, or opens a share sheet later). A bare
    # "unchanged" read as "missed" to agents: in the spatial A/B 12 of 13 failures
    # were a correct tap followed by a second one (`--method gesture`) to be sure.
    # iOS has no click event: the runner's `delivered` (an AX press that ran, or a touch that passed
    # its hit-test) is the same "the app got it" signal (IOS-INTEGRATION.md §5)
    handled = bool((reply or {}).get("clicked_event") or (reply or {}).get("delivered")) or any(
        e.get("type") in ("clicked", "long_clicked") for e in events)
    opts = S.Opts.from_env()
    if new_screen:
        text = S.render(post, opts)
    else:
        lines = changes[:DIFF_MAX]
        more = len(changes) - len(lines)
        quiet = [HANDLED_NO_CHANGE if handled and method != "gesture-fallback" else "unchanged"]
        text = "\n".join([S.header(post, opts)] + (lines or quiet)
                         + ([f"… {more} more changed lines (snapshot --diff shows them all)"] if more > 0 else []))
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
        for k in ("performed", "clicked_event", "idle", "settle_ms", "hit_test", "system_alert"):
            if k in reply:
                out[k] = reply[k]
        if reply.get("method") and sess.ios:
            out["via"] = reply["method"]        # ax | touch | keys | set_value
    if handled and not changed:
        out["handled"] = True             # delivered; don't repeat it
    if t is not None:
        out["target"] = t.describe()
    if warning:
        out["warning"] = warning
    if post.warnings:
        out["screen_warnings"] = post.warnings
    if loading_ms:
        out["loading_ms"] = loading_ms             # waited for a spinner the action brought up
    if still_loading:
        out["warning"] = (out.get("warning") + "; " if out.get("warning") else "") + \
            f"still loading after {LOADING_WAIT_S:.0f} s (a spinner is on screen): wait --role progress --gone"
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
    """One `act` on the target; a stale handle is re-resolved once, on a fresh read (by its locator or the
    ref's fingerprint: unique or an error). An app that reloads its table on every price tick replaces
    each cell's element between the read and the act (a real app, 2026-10-26)."""
    handle = (t.click if use_click else t.handle)
    if handle is None:
        return None
    try:
        return sess.call("act", t.dump, handle, action=action, custom=custom, args=args,
                         settle=settle, force=force, retry=False)
    except UserError as e:
        if e.kind != "stale-ref" or a is None:
            raise
    # the device re-dumped or the node went away since the snapshot: resolve properly
    sess.forget()
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
        return finish(sess, a, "gesture", _gesture(sess, "tap", [t.tap], _tap_settle(a, settle)), pre, t)
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
    if r.get("performed") and (r.get("clicked_event") or r.get("delivered")):
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
    moved = _screen_moved(sess, r, pre)
    if moved:
        return finish(sess, a, "action", r, pre, t, warning=f"{why}, and {moved}; not tapping by position")
    if t.res is not None and t.occluded:
        R.check_occlusion(t.res, "gesture")
    g = _gesture(sess, "tap", [t.tap], _tap_settle(a, settle))
    return finish(sess, a, "gesture-fallback", g, pre, t,
                  warning=f"{why}; tapped once at {t.tap[0]},{t.tap[1]}")


def _screen_moved(sess, r, pre):
    """Why a positional fallback would hit something else, or None if it is safe.

    performed:false can also mean the click did run and took the screen with it
    (the app crashed, a dialog closed, the activity finished). A tap at the old
    coordinates would then land on whatever is there now (measured: TESTAPP
    `crash` -> the launcher), so the fallback only fires on the same screen."""
    tree = (r or {}).get("tree") or sess.tree()
    if r is not None:
        r["tree"] = tree                  # finish() reuses it: no second dump
    post = S.build(tree)
    if pre.get("pkg") and post.pkg != pre.get("pkg"):
        return f"the screen is now {post.pkg}"
    if pre.get("sig") and post.sig != pre["sig"]:
        return "the screen changed"
    return None


def _tap_settle(a, settle):
    """The settle for a gesture tap: longer quiet unless --settle was given."""
    if settle is None or getattr(a, "settle", None) is not None:
        return settle
    return dict(settle, quiet_ms=max(settle["quiet_ms"], GESTURE_TAP_QUIET_MS))


def _gesture_tap(sess, a, t, pre, settle, typ, warning=None):
    if t.tap is None:
        raise UserError("the element has no visible area to tap", "offscreen")
    return finish(sess, a, "gesture", _gesture(sess, typ, [t.tap], _tap_settle(a, settle)), pre, t,
                  warning=warning)


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
    moved = _screen_moved(sess, r, pre)
    if moved:
        return finish(sess, a, "action", r, pre, t,
                      warning=f"ACTION_LONG_CLICK was not performed, and {moved}; not pressing by position")
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
_SCROLL_ACTIONS = {x for pair in _SCROLL.values() for x in pair}


def _main_scroller(snap, direction=None):
    """The largest scrollable element on screen that moves along ``direction``'s axis
    (the thing `scroll down` means). Not a pager for up/down: a news screen's tab
    pager was the largest scrollable, and `scroll down` switched it to the next tab."""
    cands = [e for e in snap.elements if e.node.scrollable and e.rect]
    if direction is not None:
        sideways = direction in ("left", "right")
        cands = [e for e in cands if S.is_horizontal(e.node) == sideways]
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
        e = _main_scroller(snap, a.direction)
        pre = {"lines": S.flat_lines(snap), "sig": snap.sig}
        if e is None:
            # no blind swipe of the whole screen: it moved whatever was under the
            # finger (a hardened finance app: it pushed a tab strip out of the tree,
            # and every ref on it went stale). A swipe has to be asked for.
            _awake(snap)
            finger = {"down": "up", "up": "down", "left": "right", "right": "left"}[a.direction]
            axis = "sideways" if a.direction in ("left", "right") else "up or down"
            raise UserError(f"nothing on this screen scrolls {axis} (screen: {_where(snap)})", "not-found",
                            hint=f"if the content moves by touch only: droidctl swipe {finger}",
                            data={"reason": "no-scrollable", "activity": snap.activity or None,
                                  "pkg": snap.pkg})
        res = R.Resolution(e, snap, "locator", via="largest-scroller")
        t = Target(handle=res.handle, click=res.click, dump=snap.dump, tap=res.tap, res=res, pre=snap)
    else:
        t = resolve_target(sess, a)
        pre = _pre_lines(sess, t)
    node = t.node
    acts = node.actions if node is not None else set((t.rec or {}).get("scroll") or ())
    action = _scroll_action(node, a.direction) if node is not None else (
        _SCROLL[a.direction][0] if _SCROLL[a.direction][0] in acts else _SCROLL[a.direction][1])
    sideways = _sideways(t)
    if (sideways is not None and action in ("scroll_forward", "scroll_backward")
            and sideways != (a.direction in ("left", "right"))):
        # the generic action follows the container's own axis: "down" on a pager is "next page"
        raise UserError(f"this container scrolls {'sideways' if sideways else 'up and down'}: "
                        f"scroll {a.direction} would move it the other way", "bad-args",
                        hint=f"use scroll {'left/right' if sideways else 'up/down'} on it, or another container")
    if acts & _SCROLL_ACTIONS and action not in acts:
        # the container scrolls, just not this way: it is at that edge. A swipe
        # would do nothing here at best, and inside a pager or a pull-to-refresh
        # it would switch pages or refresh (a real app tour)
        edge = "end" if action in ("scroll_forward", "scroll_down", "scroll_right") else "start"
        out = finish(sess, a, "none", {"performed": False}, pre, t,
                     warning=f"already at the {edge}: the list offers no {action}")
        out["edge"] = edge
        return out
    r = _act(sess, t, action=action, settle=_settle(a), a=a)
    if r is not None and r.get("performed"):
        return finish(sess, a, "action", r, pre, t, extra={"action": action})
    box = (t.res.elem.rect if t.res is not None and t.res.elem is not None else None)
    return _swipe(sess, a, a.direction, box, pre, why=f"{action} not performed: swiped instead")


def _sideways(t):
    """Whether the target container scrolls sideways (None: not a scroll container)."""
    if t.node is not None:
        return S.is_horizontal(t.node) if t.node.scrollable else None
    rec = t.rec or {}
    if not rec.get("scroll"):
        return None
    return (rec.get("role") == "pager" or bool({"scroll_left", "scroll_right"} & set(rec["scroll"]))
            or (rec.get("class") or "").endswith(("HorizontalScrollView", "ViewPager", "ViewPager2")))


def _swipe_points(direction, box, frac=0.4):
    l, top, r, b = box
    w, h = r - l, b - top
    cx, cy = l + 0.6 * w if direction in ("up", "down") else l + 0.5 * w, top + 0.5 * h
    # "scroll down" moves the content up: the finger swipes up
    if direction == "down":
        return [[cx, top + 0.7 * h], [cx, top + (0.7 - frac) * h]]
    if direction == "up":
        return [[cx, top + 0.3 * h], [cx, top + (0.3 + frac) * h]]
    hfrac = max(frac, 0.6)          # horizontal drags must pass half the width (see cmd_swipe)
    if direction == "right":
        return [[l + 0.8 * w, cy], [l + (0.8 - hfrac) * w, cy]]
    return [[l + 0.2 * w, cy], [l + (0.2 + hfrac) * w, cy]]


def _screen_box(sess):
    st = sess.state
    if st and st.get("screen"):
        return tuple(st["screen"])
    info = sess.info or sess.call("ping")
    return (0, 0, info["screen"]["w"], info["screen"]["h"])


def _swipe(sess, a, direction, box, pre, why=None):
    box = box or _screen_box(sess)
    pts = _swipe_points(direction, box)
    if sess.ios:
        # a drag that rests before lifting: no fling, so the page stops where the finger did and the
        # settle doesn't wait out a deceleration (2.6 s on Safari, 2026-10-22)
        g = sess.call("gesture", "swipe", pts, ms=getattr(a, "ms", None) or 400, settle=_settle(a),
                      hold_end_ms=SCROLL_DRAG_HOLD_MS, retry=False)
    else:
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
    if d is None:
        # Horizontal swipes (ViewPager pages, swipe-to-dismiss rows) must drag past
        # half the width: accessibility-dispatched gestures register no fling on the
        # SM-N950F (even a 150 ms swipe snapped back), and 0.4 did nothing on TESTAPP
        # tabs_pager and swipe_only_delete. Vertical scrolls don't need a fling.
        d = 0.7 if a.direction in ("left", "right") else 0.4
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


def _scroll_plan(snap, loc, direction):
    """(container, direction) for scroll-to. A target the tree already holds but
    the screen does not show (scrolled out) is scrolled into view by *its own*
    scrollable ancestor, towards the side it lies on: a tab strip or a pager is
    not the main list (a hardened finance app: the wanted tab sat in a
    horizontal tab strip, the main list scrolled 0 times). Otherwise the main
    list, along its own axis."""
    want = {k: loc[k] for k in ("id", "text", "desc", "cls") if k in loc}
    hidden = R._find_hidden(snap, lambda n: R._node_matches(n, **want)) if want else None
    cont = None
    if hidden is not None:
        cont = next((x.el for x in hidden.ancestors()
                     if x.scrollable and x.el is not None and x.el.node is x and x.el.rect), None)
        if direction is None:
            direction = R._direction(hidden.raw.get("bounds"), hidden)
    if cont is None:
        # with no side known, prefer the vertical main list (a pager would page through tabs)
        cont = _main_scroller(snap, direction or "down") or (None if direction else _main_scroller(snap))
    if direction is None and cont is not None:
        direction = "right" if S.is_horizontal(cont.node) else "down"
    return cont, direction or "down"


def _no_scrollable(snap, loc):
    what = " ".join(f"{k}={v}" for k, v in loc.items())
    return UserError(f"{what} is not on this screen, and nothing on it scrolls"
                     f" (screen: {_where(snap)})", "not-found",
                     hint="check you are on the right screen (droidctl current); a view that only "
                          "moves by touch needs an explicit droidctl swipe",
                     data={"reason": "no-scrollable", "activity": snap.activity or None, "pkg": snap.pkg})


def _where(snap):
    return S.short_activity(f"{snap.pkg}/{snap.activity}") if snap.activity else (snap.pkg or "?")


def cmd_scroll_to(a):
    """Scroll the target's container (or the main list) until an element matching the locator is on screen."""
    sess = session_for(a)
    loc = _locator(a)
    if not loc:
        raise UserError("scroll-to needs --text/--id/--desc", "bad-args")
    snap = sess.snap()
    _awake(snap)
    found, scrolls, snap = _scroll_into_view(sess, a, loc, snap)
    if found is None:
        raise UserError(f"{' '.join(f'{k}={v}' for k, v in loc.items())} not found after {scrolls} scrolls",
                        "not-found", data={"scrolls": scrolls})
    return found


def _scroll_into_view(sess, a, loc, first):
    """Scroll until ``loc`` is on screen -> (result dict or None, scrolls, last snap).
    The last snap is saved either way (the screen moved)."""
    pre = {"lines": S.flat_lines(first), "sig": first.sig}
    snap = first
    scrolls, direction = 0, getattr(a, "direction", None)
    tried_back = False
    while True:
        try:
            res = R.find(snap, **loc)
            if res.elem is not None and not res.occluded:
                elem = res.elem
                sess.save(snap, shown=False)
                return ({"ok": True, "found": True, "scrolls": scrolls, "ref": elem.ref,
                         "changed": snap.sig != pre["sig"] or S.flat_lines(snap) != pre["lines"],
                         "element": R._describe(elem),
                         "text": S.header(snap, S.Opts.from_env()) + "\n"
                         + S.element_line(elem, snap, S.Opts.from_env())}, scrolls, snap)
        except UserError as e:
            if e.kind not in ("not-found", "offscreen", "occluded"):
                raise
        if scrolls >= getattr(a, "max_scrolls", SCROLL_INTO_VIEW_MAX):
            break
        cont, direction = _scroll_plan(snap, loc, direction)
        if cont is None:
            if scrolls == 0:
                raise _no_scrollable(snap, loc)
            break
        res = R.Resolution(cont, snap, "locator")
        action = _scroll_action(cont.node, direction)
        r = sess.call("act", snap.dump, res.handle, action=action,
                      settle={"quiet_ms": 150, "first_ms": 400, "timeout_ms": 1500}, retry=False)
        scrolls += 1
        new = sess.build(r.get("tree") or sess.tree()) if r.get("performed") else snap
        if not r.get("performed") or S.flat_lines(new) == S.flat_lines(snap):
            # the end of the list: try the other way once
            if tried_back or a.one_way:
                break
            tried_back = True
            direction = {"down": "up", "up": "down", "left": "right", "right": "left"}[direction]
        snap = new
    sess.save(snap, shown=False)
    return None, scrolls, snap


SCROLL_INTO_VIEW_MAX = 15
SCROLL_DRAG_HOLD_MS = 150


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


def _refind_field(snap, want, focused_ok=True):
    """The same input in a fresh snapshot: by resource id, else by box, else
    (``focused_ok``) the focused one."""
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
    if not focused_ok:
        return None
    focused = [n for n in edits if "focused" in n.flags]
    return focused[0] if len(focused) == 1 else None


def _give_focus(sess, snap, node, want, force):
    """Input focus on this very field, verified -> (focused, node, snap).

    `adb input text` types into whatever holds input focus, and ACTION_FOCUS can be
    performed without the field taking focus. Unverified, the text of a second
    field went into the first, which still had focus (a hardened finance app login,
    two password fields with one id). ACTION_CLICK is what a person does next."""
    if "focused" in node.flags:
        return True, node, snap
    for action in ("focus", "click"):
        try:
            r = sess.call("act", snap.dump, node.raw["handle"], action=action, force=force,
                          settle={"quiet_ms": 150, "first_ms": 300, "timeout_ms": 1000}, retry=False)
        except UserError:
            continue
        snap2 = S.build(r.get("tree") or sess.tree())
        n2 = _refind_field(snap2, want, focused_ok=False)      # never "whichever is focused"
        if n2 is None:
            return False, node, snap
        node, snap = n2, snap2
        if "focused" in n2.flags:
            return True, node, snap
    return False, node, snap


def _dist(r1, r2):
    return sum(abs(x - y) for x, y in zip(r1, r2))


def _want(t):
    n = t.node
    if n is not None:
        return {"id": n.raw.get("id"), "rect": n.rect, "password": "password" in n.flags,
                "value": _field_value(n)}
    r = t.rec or {}
    return {"id": r.get("id"), "rect": tuple(r["bounds"]) if r.get("bounds") else None,
            "password": bool(r.get("password")), "value": None}


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


_NUMERIC_TYPED = re.compile(r"[\d\s()+\-./]+")


def _same_digits(val, target):
    """A phone/number formatter added punctuation: "(555) 123 45 67" is what the
    field shows for a typed "5551234567" (a real app login). Only for a typed
    value that is itself digits and separators."""
    if not val or not target or not _NUMERIC_TYPED.fullmatch(target):
        return False
    digits = lambda x: re.sub(r"\D", "", x)
    return digits(target) != "" and digits(val) == digits(target) and _NUMERIC_TYPED.fullmatch(val) is not None


def _input_text_arg(s):
    """`adb shell input text` escaping: spaces as %s, shell metacharacters quoted."""
    return shlex.quote(s.replace(" ", "%s"))


def _ios_keys(sess, t, want, target, text, force, a, steps):
    """iOS: focus the field, delete what it holds, type the keys -> (value, node, snap), or None when
    the field won't take the focus or the keys fail (the caller then sets the value)."""
    val, node, snap = _readback(sess, want)
    if node is None:
        return None
    focused, node, snap = _give_focus(sess, snap, node, want, force)
    if not focused:
        steps.append({"method": "keys", "skipped": "the field did not take input focus"})
        return None
    cur = "" if a.append else (_field_value(node) or "")
    try:
        sess.client.type_text("\b" * len(cur) + (text if a.append else target))
    except UserError as e:
        steps.append({"method": "keys", "error": e.kind})
        return None
    steps.append({"method": "keys", "performed": True})
    return _readback(sess, want)


def _type(sess, a, t):
    text = _read_text(a)
    if t.point:
        raise UserError("type needs an input element, not --point", "bad-args")
    pre = _pre_lines(sess, t)
    want = _want(t)
    force = _keyboard_force(t, "action")
    if want["password"] and a.append:
        raise UserError("can't --append to a password field: its current value is hidden",
                        "bad-args", hint="type the whole value (it replaces the field)")
    current = want.get("value")
    if current is None and t.rec is not None:
        current = t.rec.get("text") or ""
    target = (current or "") + text if a.append else text
    steps, method, reply = [], None, None
    before = current

    def ok(val):
        if want["password"]:
            # the platform exposes a password as bullets of the same length (measured on
            # API 28: "••••••••"); the length is all we may compare, and all we report
            return val is not None and len(val) == len(target)
        return val == target or _same_digits(val, target)

    # 0. --enter needs the input focus. Take it BEFORE the text, the order a person
    #    uses: some fields rewrite their value when they gain focus (a real app
    #    calculator: focusing a field holding "250" turned it into "25").
    if a.enter and not (t.node is not None and "focused" in t.node.flags):
        try:
            rf = _act(sess, t, action="focus", force=force, a=a)
            steps.append({"method": "focus", "performed": bool(rf and rf.get("performed"))})
        except UserError as e:
            steps.append({"method": "focus", "error": e.kind})

    # 1 (iOS). Keys into the focused field, as a person types, so the app's own input handling runs.
    #    AX set_value changed a formatted amount field's shown value but not the app's model, which
    #    put it back when editing ended (a real app's calculator, 2026-10-26); keys stuck
    #    ("10000" shown as "10.000", "100,00" after editing). set_value stays the fallback.
    got = _ios_keys(sess, t, want, target, text, force, a, steps) if sess.ios and t.node is not None else None
    # the keys went in and the field left the screen: the app took them and moved on (a login field
    # that submits itself at 6 digits, 2026-10-26). Never fall back: that would type them again.
    gone = got is not None and got[1] is None
    if gone:
        val, node, snap = got
        method, reply, ignored = "keys", None, False
    elif got is not None and ok(got[0]):
        val, node, snap = got
        method, reply, ignored = "keys", None, False
    else:
        # 1. ACTION_SET_TEXT (no tap, Unicode-safe)
        r = _act(sess, t, action="set_text", args={"text": target}, force=force,
                 settle={"quiet_ms": 150, "first_ms": 400, "timeout_ms": 1500}, a=a)
        want = _want(t) if t.node is not None else want
        steps.append({"method": "set_text", "performed": bool(r and r.get("performed"))})
        val, node, snap = _readback(sess, want, (r or {}).get("tree"))
        method, reply = "set_text", r
        ignored = not (r and r.get("performed")) or (val == before and target != before)
    # 2. focus + clipboard paste (the field refused set_text). Never for a password:
    #    the clipboard is readable by other apps and Samsung keeps a clipboard history.
    if ignored and not ok(val) and node is not None and sess.ios:
        pass                                    # no clipboard on iOS: keys into the focused field (3.)
    elif ignored and not ok(val) and node is not None and want["password"]:
        steps.append({"method": "paste", "skipped": "password field: never via the clipboard"})
    elif ignored and not ok(val) and node is not None:
        prev = None
        touched = False
        try:
            sess.call("act", snap.dump, node.raw["handle"], action="focus", force=force, retry=False)
            clip = sess.call("clipboard", set=text if a.append else target)
            touched = True
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
            if touched:
                # restore; an empty clipboard before means clear it, not leave our text
                try:
                    sess.call("clipboard", set=prev if prev is not None else "")
                except UserError:
                    pass
    # 3. adb input text (ASCII only): only once this field verifiably holds input focus
    unfocused = False
    if not gone and not ok(val) and (val == before and target != before) and (text.isascii() or sess.ios) and text:
        try:
            focused = False
            if node is not None:
                focused, node, snap = _give_focus(sess, snap, node, want, force)
            if not focused:
                unfocused = True
                steps.append({"method": "input", "skipped": "the field did not take input focus: "
                                                            "the keys would go to another field"})
            else:
                keys = text if a.append or not before else target
                if sess.ios:
                    sess.client.type_text(keys)
                else:
                    sess.shell("input", "text", _input_text_arg(keys))
                steps.append({"method": "keys" if sess.ios else "input", "performed": True})
                val, node, snap = _readback(sess, want)
                method, reply = "input", None
        except UserError as e:
            steps.append({"method": "input", "error": e.kind})
    hint = ("the app refused set_text and the field would not take focus: tap it "
            "(droidctl tap REF), then type again" if unfocused else "")
    warning = None
    if gone:
        warning = "the field left the screen once the keys were in (the app submitted it?): not verified"
    elif want["password"] and not ok(val):
        n_now, n_want = len(val or ""), len(target)
        if (val or "") == (before or "") and target != (before or ""):
            raise UserError(f"the password field did not take the text ({n_now} characters, not {n_want})",
                            "no-change", hint=hint, data={"steps": steps, "len": n_now})
        warning = f"the password field holds {n_now} characters, not {n_want} (a length limit?)"
    elif not want["password"] and val != target and not _same_digits(val, target):
        if val == before and target != before:
            raise UserError(f"the field did not take the text (it still shows {val!r})", "no-change",
                            hint=hint, data={"steps": steps, "value": val})
        warning = f"the field shows {val!r}, not {target!r} (a formatter, a length limit or auto-advance?)"
    if a.enter and gone:
        steps.append({"method": "enter", "skipped": "the field already left the screen"})
    elif a.enter:
        steps.append(dict(_enter(sess, node, snap), method="enter"))
        reply = None
    extra = {"value": None if want["password"] else val, "verified": ok(val),
             "steps": steps}
    if method == "keys":
        extra["via"] = "keys"
    if want["password"]:
        extra["value_hidden"] = True
    return finish(sess, a, method, reply, pre, t, warning=warning, extra=extra)


def _enter(sess, node, snap):
    """Press the IME action on `node`; returns how it was delivered."""
    if sess.ios:
        return _enter_ios(sess, node, snap)
    if sess.sdk >= 30 and node is not None:
        try:
            r = sess.call("act", snap.dump, node.raw["handle"], action="ime_enter", retry=False)
            if r.get("performed"):
                return {"via": "ime_enter"}
        except UserError as e:
            if e.kind not in ("unsupported", "stale-ref"):
                raise
    # A key event goes to whatever has input focus, and ACTION_SET_TEXT does not
    # give the field focus: without this, --enter on API < 30 went nowhere
    # (a real app calculator: the amount was never submitted).
    focused = node is not None and "focused" in node.flags
    if node is not None and not focused:
        try:
            focused = bool(sess.call("act", snap.dump, node.raw["handle"], action="focus",
                                     retry=False).get("performed"))
        except UserError:
            pass
    sess.shell("input", "keyevent", "66")
    sess.call("wait_idle", 150, 2000)
    return {"via": "keyevent", "focused": focused}


def _enter_ios(sess, node, snap):
    """Return into the focused field (a typed newline). setAttribute already focused it; if
    not, a checked touch on the field gives focus first."""
    if node is None:
        r = sess.client.type_text("\n", settle={})
        return {"via": "return", "idle": r.get("idle")}
    try:
        r = sess.call("act", snap.dump, node.raw["handle"], action="ime_enter", settle={}, retry=False)
        return {"via": "return", "idle": r.get("idle")}
    except UserError as e:
        if e.kind not in ("not-focused", "stale-ref"):
            raise
    snap = sess.snap()
    n = _refind_field(snap, {"id": node.raw.get("id"), "rect": node.rect}, focused_ok=False)
    if n is None:
        raise UserError("--enter: the field is gone", "stale-ref")
    if "focused" not in n.flags:
        sess.call("act", snap.dump, n.raw["handle"], action="focus", settle={}, retry=False)
        snap = sess.snap()
        n = _refind_field(snap, {"id": node.raw.get("id"), "rect": node.rect}) or n
    r = sess.call("act", snap.dump, n.raw["handle"], action="ime_enter", settle={}, retry=False)
    return {"via": "return", "focused_first": True, "idle": r.get("idle")}


def cmd_type(a):
    sess = session_for(a)
    # `type "hello"`: one positional that is not a ref is the text, into the focused input.
    # With a locator (`type --id otp1 123456`) a lone positional is always the text,
    # even when it is all digits: the locator already names the element.
    if (a.content is None and a.target is not None and not (a.stdin or a.file)
            and (not re.fullmatch(r"\[?\d+\]?", a.target.strip()) or _locator(a) or a.ref is not None)):
        a.content, a.target = a.target, None
    if a.content is None and not (a.stdin or a.file or a.clear or a.enter):
        if a.text is not None:
            # `type --id editText --text 514524` read as "type 514524": --text names the field
            raise UserError(f"nothing to type: --text {a.text!r} is a locator (it finds the field by its "
                            "text or hint); the text to type goes last", "bad-args",
                            hint=f"droidctl type --id ID {a.text!r} (or: type REF {a.text!r})")
        lone = a.target if a.target is not None else a.ref
        known = (sess.state or {}).get("refs", {}) | (sess.state or {}).get("retired", {})
        if lone is not None and str(lone).strip("[] ") not in known:
            raise UserError(f"nothing to type: a lone number is read as a ref, and there is no ref "
                            f"[{str(lone).strip('[] ')}] on this screen", "bad-args",
                            hint=f"to type digits, name the field: droidctl type REF {str(lone).strip('[] ')} "
                                 "(or --id/--text)")
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
        try:
            t = resolve_target(sess, a)
        except UserError as e:
            # `type --text Amount`: a form row and its own input share the label (a real app's
            # calculator, 2026-10-26). Only an input takes text: if exactly one of them is, it is meant.
            if e.kind != "ambiguous" or getattr(a, "role", None) is not None or not _locator(a):
                raise
            a.role = "input"
            try:
                t = resolve_target(sess, a)
            except UserError:
                a.role = None
                raise e from None
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
    if session_for(a).ios:
        return _back_ios(a)
    return _global(a, "back")


def _back_ios(a):
    """iOS has no system Back: tap the navigation bar's back button (UIKit marks it with a
    private trait), the way a person goes back."""
    sess = session_for(a)
    snap = sess.snap(cached=True)
    backs = [e for e in snap.elements if e.node.raw.get("nav_back")]
    if not backs:
        _awake(snap)
        if snap.dialog:
            # a dialog over the screen hides its back button: name the way out (a real app's validation
            # pop-up, 2026-10-26)
            btns = [e.label for e in snap.elements if e.region in ("dialog", "popup") and e.role == "button"
                    and e.label]
            raise UserError("a dialog is open (iOS has no system Back)", "unsupported",
                            hint="tap one of its buttons: " + (", ".join(f'"{b}"' for b in btns[:4]) or
                                                               "see droidctl snapshot"),
                            data={"buttons": btns})
        raise UserError("this screen has no back button (iOS has no system Back)", "unsupported",
                        hint="tap the screen's own close/cancel button, or: droidctl hide-keyboard")
    e = min(backs, key=lambda x: (x.rect[1], x.rect[0]))
    res = R.Resolution(e, snap, "locator", via="back-button")
    t = Target(handle=res.handle, click=res.click, dump=snap.dump, tap=res.tap, res=res, pre=snap)
    pre = {"lines": S.flat_lines(snap), "sig": snap.sig}
    try:
        r = _act(sess, t, action="click", settle=_settle(a), use_click=True)
    except UserError as e:
        if e.kind != "stale-ref" or not sess.from_cache:
            raise
        sess.forget()                             # something re-dumped since: once more, on a fresh read
        return _back_ios(a)
    return finish(sess, a, "back", r, pre, t)


def cmd_hide_keyboard(a):
    sess = session_for(a)
    pre = _pre_lines(sess, None)
    if sess.ios:
        try:
            r = sess.client.hide_keyboard(settle=_settle(a))
        except UserError as e:
            if e.kind != "unsupported":
                raise
            raise UserError(f"hide-keyboard: {e}".replace("hide_keyboard: ", ""), "unsupported",
                            hint="tap a label or another field (apps end editing on a tap elsewhere), or the "
                                 "screen's own Done/OK button; a typed newline would go into the field") from None
        if r.get("already"):
            return {"ok": True, "changed": False, "already": True, "text": "the keyboard is not shown"}
        # Judged on the mapped screen, not the runner's `hidden`: it counts a keyboard still sliding off
        # (S1: Return dismissed it, the runner said hidden 0). Once more after the animation if needed.
        kb = r.get("tree") is not None and bool(sess.build(r["tree"]).keyboard)
        if kb:
            time.sleep(0.3)
            kb = bool(sess.snap().keyboard)
            r = dict(r, tree=None)                  # finish reads the screen again
        if kb:
            raise UserError("the keyboard is still shown (the field takes Return as a new line?)", "no-change",
                            hint="tap outside the field, or the screen's own Done button",
                            data={"method": r.get("method")})
        return finish(sess, a, "hide-keyboard", r, pre, extra={"via": r.get("method")})
    if not (sess.call("current") or {}).get("keyboard"):
        return {"ok": True, "changed": False, "already": True, "text": "the keyboard is not shown"}
    r = sess.call("global_action", "back", settle=_settle(a), retry=False)
    return finish(sess, a, "hide-keyboard", r, pre, extra={"via": "back"})


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
    if sess.ios:
        if key.lower() not in ("enter", "return"):
            raise UserError(f"press {key}: on iOS only enter is available", "unsupported",
                            hint="back: droidctl back (the screen's back button); the keyboard: droidctl hide-keyboard")
        pre = _pre_lines(sess, None)
        r = sess.client.type_text("\n", settle=_settle(a))
        return finish(sess, a, "key", r, pre, extra={"key": "enter"})
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
def _wait_role(sess, a):
    """`wait --role progress --gone`: a condition on the snapshot's roles, which the
    device can't evaluate (a loading dialog is often a bare spinner with no text or
    id; a real app login). Snapshots between device idle waits, bounded by
    --timeout; a rootless or degraded frame is not "gone", it is not loaded yet."""
    if a.id or a.desc or a.activity or a.window or a.pkg or a.toast is not None:
        raise UserError("--role combines only with --text and --gone", "bad-args")
    t0 = time.monotonic()
    deadline = t0 + a.timeout
    want = (a.text or "").lower()

    def hit(snap):
        return any(e.role == a.role and (not want or (want == e.label_full.lower() if a.exact
                                                      else want in e.label_full.lower()))
                   for e in snap.elements)

    while True:
        snap = sess.build(sess.tree())
        present = hit(snap)
        done = (not present and not _transitional(snap)) if a.gone else present
        if done:
            sess.save(snap)
            ms = round((time.monotonic() - t0) * 1000)
            what = f"role={a.role!r}" + (f" text={a.text!r}" if a.text else "") + (" gone" if a.gone else "")
            return {"ok": True, "matched": True, "ms": ms,
                    "condition": {"role": a.role, "text": a.text, "gone": bool(a.gone)},
                    # the whole screen, not just its header: the state saved above is
                    # what the next `snapshot` compares against, so an agent that was
                    # only shown a header would get "unchanged" for a screen it never saw
                    "text": f"matched {what} after {ms} ms\n" + S.render(snap, S.Opts.from_env())}
        left = deadline - time.monotonic()
        if left <= 0:
            raise UserError(f"gave up after {a.timeout:g} s waiting for role={a.role!r}"
                            + (" to go" if a.gone else ""), "timeout")
        t = time.monotonic()
        sess.call("wait_idle", 150, int(min(left, 1.0) * 1000))
        if time.monotonic() - t < 0.15:        # already quiet: don't spin on an idle spinner
            time.sleep(min(0.15, max(0.0, deadline - time.monotonic())))


IDLE_QUIET_MS = 500


def _wait_idle(sess, a):
    """`wait --idle`: no condition, just until the screen stops changing (no
    content, window or scroll event for --quiet ms). A screen that never stops
    (a spinner, a ticker) ends as `timeout`, not as a false "idle"."""
    if any(getattr(a, k, None) for k in ("text", "id", "desc", "role", "activity", "window", "pkg")) \
            or a.toast is not None or a.gone:
        raise UserError("--idle takes no condition (only --quiet and --timeout)", "bad-args")
    quiet = a.quiet if a.quiet is not None else IDLE_QUIET_MS
    r = sess.call("wait_idle", quiet, int(a.timeout * 1000))
    if not r.get("idle"):
        raise UserError(f"the screen kept changing for {a.timeout:g} s (an animation or a live value?)",
                        "timeout", hint="snapshot anyway, or wait for a specific --text/--gone",
                        data={"ms": r.get("ms")})
    if sess.ios:                                # the runner's rule: idle callbacks, then 100 ms quiet
        from droidctl.ios.client import QUIET_MS
        quiet = QUIET_MS
    return {"ok": True, "idle": True, "ms": r.get("ms"), "quiet_ms": quiet,
            "text": f"idle after {r.get('ms')} ms (quiet for {quiet} ms)"}


def cmd_wait(a):
    sess = session_for(a)
    if getattr(a, "idle", False):
        return _wait_idle(sess, a)
    if getattr(a, "role", None):
        return _wait_role(sess, a)
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
        raise UserError("wait needs --idle or a condition: --text/--id/--desc/--role/--activity/--toast/"
                        "--window/--pkg", "bad-args")
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


def cmd_mark(a):
    """A marker line in the device's logcat (tag `droidctl`), stamped on the host
    clock too, to cut a capture (logcat, a proxy's history) into steps."""
    sess = session_for(a)
    if sess.ios:
        sess.call("mark", {"label": a.label})
        return {"ok": True, "label": a.label, "tag": MARK_TAG,
                "text": f"marked {a.label!r} (the iPhone's log; droidctl logs --marks)"}
    sess.shell("log", "-t", MARK_TAG, shlex.quote(a.label))   # adb shell re-splits its args
    return {"ok": True, "label": a.label, "tag": MARK_TAG,
            "text": f"marked {a.label!r} (logcat tag {MARK_TAG})"}


MARK_TAG = "droidctl-mark"      # not the agent's own "droidctl" tag: `logs --marks` shows marks only


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
    if not act and not sess.ios:
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
    snap_text = S.render(sess.build(sess.tree()), S.Opts.from_env()) if not out["new_screen"] else out["text"]
    out["text"] = snap_text
    return out


# --------------------------------------------------------------------------
# iPhone: apps through devicectl and the runner, logs through syslog_relay
# --------------------------------------------------------------------------
def _cdid(sess):
    from droidctl.ios import devices
    return devices.find(sess.serial)["cdid"]


def _ios_app(sess, bundle):
    from droidctl.ios import devices
    app = devices.app(_cdid(sess), bundle)
    if app is None:
        raise UserError(f"{bundle} is not installed on the iPhone", "not-found", hint="see: droidctl apps")
    return app


def _launch_ios(sess, a):
    from droidctl.ios import devices
    if a.clear:
        raise UserError("iOS has no 'clear data': uninstall and reinstall the app instead", "unsupported")
    if a.activity:
        raise UserError("iOS apps have no activities to start: launch the bundle id", "bad-args")
    app = _ios_app(sess, a.pkg)
    pre = _pre_lines(sess, None)
    t0 = time.monotonic()
    stopped = []
    if a.stop:
        cdid = _cdid(sess)
        for pid in devices.pids_of(app, devices.processes(cdid)):
            devices.terminate(cdid, pid)
            stopped.append(pid)
    s = _settle(a) or {"quiet_ms": SETTLE_QUIET_MS, "timeout_ms": SETTLE_CAP_MS}
    from droidctl.ios.client import ios_settle
    r = sess.call("launch", {"pkg": a.pkg, "settle": ios_settle(s)},
                  timeout=max(a.timeout, 10) + 20, retry=False)
    out = finish(sess, a, "launch", r, pre,
                 extra={"pkg": a.pkg, "launch_ms": round((time.monotonic() - t0) * 1000), "stopped": stopped})
    if not out["new_screen"]:
        out["text"] = S.render(sess.build(sess.tree()), S.Opts.from_env())
    return out


def _stop_app_ios(sess, a):
    from droidctl.ios import devices
    app = _ios_app(sess, a.pkg)
    cdid = _cdid(sess)
    pids = devices.pids_of(app, devices.processes(cdid))
    for pid in pids:
        devices.terminate(cdid, pid)
    return {"ok": True, "pkg": a.pkg, "pids": pids,
            "text": f"stopped {a.pkg}" if pids else f"{a.pkg} was not running"}


def _apps_ios(sess, a):
    from droidctl.ios import devices
    found = [x for x in devices.apps(_cdid(sess), all_apps=a.all) if a.all or not x["system"]]
    if a.filter:
        f = a.filter.lower()
        found = [x for x in found if f in x["bundle"].lower() or f in (x["name"] or "").lower()]
    found.sort(key=lambda x: x["bundle"])
    return {"ok": True, "apps": [x["bundle"] for x in found], "count": len(found),
            "names": {x["bundle"]: x["name"] for x in found},
            "text": "\n".join(f"{x['bundle']}  {x['name']}" for x in found) or "(none)"}


def _install_ios(sess, a):
    from droidctl.ios import devices
    path = a.apk.rstrip("/")
    if not os.path.exists(path):
        raise UserError(f"{a.apk} does not exist", "not-found")
    if not (path.endswith(".app") or path.endswith(".ipa")):
        raise UserError("an iPhone installs a .app (a device build) or an .ipa", "bad-args")
    if a.grant:
        raise UserError("iOS has no install-time permission grants: the app asks when it needs them", "bad-args")
    if path.endswith(".app"):
        import plistlib
        from droidctl.ios.manage import ours
        try:
            with open(os.path.join(path, "Info.plist"), "rb") as f:
                bundle = plistlib.load(f).get("CFBundleIdentifier")
        except (OSError, plistlib.InvalidFileException):
            bundle = None
        if ours(bundle):
            # replacing it under a running session ended the session (2026-10-21)
            raise UserError("that is droidctl's own runner: droidctl setup installs and starts it", "bad-args")
    devices.install(_cdid(sess), path)
    return {"ok": True, "apk": a.apk, "text": f"installed {os.path.basename(path)}"}


def _open_url_ios(sess, a):
    pre = _pre_lines(sess, None)
    t0 = time.monotonic()
    s = _settle(a) or {"quiet_ms": SETTLE_QUIET_MS, "timeout_ms": SETTLE_CAP_MS}
    from droidctl.ios.client import ios_settle
    r = sess.call("open_url", {"url": a.url, "settle": ios_settle(s)},
                  timeout=30, retry=False)
    return finish(sess, a, "open-url", r, pre, extra={"url": a.url, "ms": round((time.monotonic() - t0) * 1000)})


LOG_LISTEN_S = 2.0      # without the daemon there is no history: listen this long


def _logs_ios(sess, a):
    from droidctl.ios import devices, syslog
    hist = sess.client.log_history() if hasattr(sess.client, "log_history") else None
    note = None
    if hist is None:
        lines = syslog.listen(sess.serial, LOG_LISTEN_S)
        note = (f"an iPhone's log is live only: these are the {LOG_LISTEN_S:g} s after the call "
                "(the daemon keeps a history of recent lines)")
    else:
        lines = hist["lines"]
        note = f"since the daemon connected ({hist['since']})" + ("" if hist["alive"] else
                                                                  f"; the log stream is down: {hist['error']}")
    proc, pids = None, []
    if a.pkg:
        app = _ios_app(sess, a.pkg)
        procs = devices.processes(_cdid(sess))
        pids = devices.pids_of(app, procs)
        exe = next((p["path"] for p in procs if p["pid"] in pids), None)
        proc = os.path.basename(exe) if exe else os.path.basename((app.get("url") or "").rstrip("/"))[:-4]
    tags = list(getattr(a, "tag", None) or []) + (["droidctl-mark"] if getattr(a, "marks", False) else [])
    kept = [x for x in lines if syslog.keep(syslog.parse(x), proc, pids, a.level, tags)][-a.max:]
    return {"ok": True, "lines": kept, "crash": [], "pid": pids[0] if pids else None, "note": note,
            "text": "\n".join(kept) or "(no lines)"}


def cmd_launch(a):
    sess = session_for(a)
    if sess.ios:
        return _launch_ios(sess, a)
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
    if sess.ios:
        return _open_url_ios(sess, a)
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
    if sess.ios:
        return _stop_app_ios(sess, a)
    sess.shell("am", "force-stop", a.pkg)
    return {"ok": True, "pkg": a.pkg, "text": f"stopped {a.pkg}"}


def cmd_apps(a):
    serial = dev.resolve_serial(a.device)
    sess = get_session(serial, auto_setup=False)
    if sess.ios:
        return _apps_ios(sess, a)
    flag = [] if a.all else ["-3"]
    out = sess.shell("pm", "list", "packages", *flag)
    pkgs = sorted(x.split(":", 1)[1].strip() for x in out.splitlines() if x.startswith("package:"))
    if a.filter:
        pkgs = [p for p in pkgs if a.filter.lower() in p.lower()]
    return {"ok": True, "apps": pkgs, "count": len(pkgs), "text": "\n".join(pkgs) or "(none)"}


def cmd_install(a):
    serial = dev.resolve_serial(a.device)
    sess = get_session(serial, auto_setup=False)
    if sess.ios:
        return _install_ios(sess, a)
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


_CRASH_PROC = re.compile(r"\bProcess: (\S+), PID: (\d+)")


def crash_blocks(crash_log, pkg):
    """The crash-buffer lines of ``pkg``'s crashes (FATAL EXCEPTION and its stack).

    A crashed app has no pid any more, and its stack lines don't name the package,
    so they are found by the PID on the crash's own "Process: <pkg>, PID: N" line."""
    lines = [x for x in crash_log.splitlines() if x and not x.startswith("--------- beginning")]
    pids = {m.group(2) for x in lines for m in [_CRASH_PROC.search(x)] if m and m.group(1) == pkg}
    return [x for x in lines if len(x.split()) > 2 and x.split()[2] in pids]


def cmd_logs(a):
    serial = dev.resolve_serial(a.device)
    sess = get_session(serial, auto_setup=False)
    if sess.ios:
        return _logs_ios(sess, a)
    tags = list(getattr(a, "tag", None) or []) + ([MARK_TAG] if getattr(a, "marks", False) else [])
    # logcat filterspecs: only these tags (at --level), everything else silent
    spec = ([f"{t}:{a.level or 'V'}" for t in dict.fromkeys(tags)] + ["*:S"] if tags
            else ["*:" + a.level] if a.level else [])
    cmd = ["logcat", "-d", "-v", "threadtime"]
    if not (spec or a.pkg):
        # `-t N` counts the buffer's last N lines BEFORE any filter: with a tag, a
        # level or a pid it returned nothing on a busy phone (a mark 7 s old, Galaxy
        # S25). Filtered reads take the whole buffer and keep the last N here.
        cmd += ["-t", str(max(1, a.max))]
    if a.pkg:
        pid = sess.shell("pidof", a.pkg, check=False).strip().split()
        # its crashes, from the crash buffer: they survive the process and log noise
        crash = crash_blocks(sess.adb("logcat", "-d", "-b", "crash", "-v", "threadtime",
                                      timeout=30), a.pkg)[-a.max:]
        if not pid:
            # not running (maybe it crashed): lines that mention the package, plus its crashes
            cmd += spec or ["*:V"]
            lines = [x for x in sess.adb(*cmd, timeout=30).splitlines() if a.pkg in x]
            lines = lines[-a.max:]
            text = "\n".join(lines + (["-- crash --"] + crash if crash else [])) or "(no lines)"
            return {"ok": True, "lines": lines, "crash": crash, "pid": None, "text": text}
        cmd += ["--pid", pid[0]]
    cmd += spec
    lines = sess.adb(*cmd, timeout=30).splitlines()
    lines = [x for x in lines if x and not x.startswith("--------- beginning")][-a.max:]
    if a.pkg:
        text = "\n".join(lines + (["-- crash --"] + crash if crash else [])) or "(no lines)"
        return {"ok": True, "lines": lines, "crash": crash, "text": text}
    return {"ok": True, "lines": lines, "text": "\n".join(lines) or "(no lines)"}


def cmd_watch(a):
    """Pushed device events, bounded by --max and --timeout (an unbounded tail hangs an agent)."""
    sess = session_for(a)
    _ = sess.client
    port = sess.client.port
    events = []
    types = [t.strip() for t in a.events.split(",")] if a.events else None
    with dev.open_client(sess.serial, port) as c:
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
    if getattr(a, "out_path", None):
        if a.out and a.out != a.out_path:
            raise UserError("give the output path once: PATH or --out", "bad-args")
        a.out = a.out_path
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
        state = sess.save(snap, shown=False)    # an image, not the list
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
