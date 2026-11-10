"""IosClient: the AgentClient API over a usbmux connection to the iOS runner.

The runner speaks the agent protocol (PROTOCOL.md), so everything but the socket and the
tree's shape is inherited: framing, ids, notifications, reconnects. `tree` results are
mapped into droidctl's raw tree here, so callers never see the iOS shape
(`native_tree` gives it to dump-fixture).

Settling differs from Android: the runner has idle callbacks and change notifications
(IOS.md §13.3), so every settle spec becomes the measured iOS rule (100 ms quiet after
both idle replies, 3000 ms cap). `wait_for` runs here, on the mapped tree, woken by the
runner's change counter instead of fixed polling (§13.5).
"""
import time

from droidctl.core import UserError
from droidctl.device import AgentClient
from droidctl.ios import mapping, transport

DEFAULT_PORT = 27100
QUIET_MS = 100          # IOS.md §13.3: quiet window after both idle replies
CAP_MS = 3000           # cold launches settled in 0.42-1.29 s


def ios_settle(settle):
    """An Android settle spec -> the iOS rule (None stays None: no settle)."""
    if settle is None:
        return None
    out = {"quiet_ms": QUIET_MS, "timeout_ms": max(CAP_MS, int(settle.get("timeout_ms") or 0))}
    if settle.get("tree") is False:
        out["tree"] = False
    return out


def _budget(settle, extra=0.0):
    # the settle cap, a tree (a 5 s snapshot timeout plus one retry) and slack
    return (settle["timeout_ms"] / 1000 if settle else 0) + 15 + extra


def _visible(n, scr):
    l, t, r, b = n.get("bounds") or (0, 0, 0, 0)
    return r > l and b > t and r > 0 and b > 0 and l < scr["w"] and t < scr["h"]


def _matches(n, cond):
    exact = cond.get("exact")

    def has(want, *vals):
        want = want.lower()
        return any(v and (v.lower() == want if exact else want in v.lower()) for v in vals)
    # iOS puts a control's label in desc (a button's title), so --text also reads desc, as a person would
    if cond.get("text") is not None and not has(cond["text"], n.get("text"), n.get("desc"), n.get("hint")):
        return False
    if cond.get("desc") is not None and not has(cond["desc"], n.get("desc"), n.get("text")):
        return False
    if cond.get("id") is not None and n.get("id") != cond["id"]:
        return False
    return True


def find(tree, cond):
    """The first visible node matching the node conditions of a wait_for, or None. PURE."""
    scr = tree.get("screen") or {"w": 0, "h": 0}
    for w in tree.get("windows") or []:
        stack = [w.get("root") or {}]
        while stack:
            n = stack.pop()
            if _visible(n, scr) and _matches(n, cond):
                return n
            stack.extend(reversed(n.get("children") or []))
    return None


def check(tree, cond):
    """(matched, detail) for a wait_for condition on a mapped tree. PURE."""
    detail = {}
    if cond.get("pkg") and tree.get("front") != cond["pkg"]:
        return False, detail
    if cond.get("window"):
        want = cond["window"]
        win = next((w for w in tree.get("windows") or [] if w.get("pkg") == want
                    or want.lower() in (w.get("title") or "").lower()), None)
        if win is None:
            return False, detail
        detail["window"] = {"pkg": win.get("pkg"), "title": win.get("title"), "type": win.get("type")}
    if any(cond.get(k) is not None for k in ("text", "id", "desc")):
        n = find(tree, cond)
        if cond.get("gone"):
            return n is None, detail
        if n is None:
            return False, detail
        detail["node"] = {k: n[k] for k in ("text", "desc", "id", "class", "bounds") if n.get(k) is not None}
    return True, detail


class IosClient(AgentClient):
    def __init__(self, udid, port=DEFAULT_PORT, timeout=5.0):
        self.udid = udid
        super().__init__(port, timeout=timeout)

    def _open(self, timeout):
        try:
            self._sock = transport.connect(self.udid, self.port, timeout=timeout)
        except UserError:
            self._sock = None
            raise
        self._buf = bytearray()         # (a unix socket to usbmuxd: no TCP_NODELAY here)

    def clone(self, timeout=5.0):
        return IosClient(self.udid, self.port, timeout=timeout)

    def call(self, method, params=None, timeout=10.0):
        res = super().call(method, params, timeout)
        if isinstance(res, dict) and isinstance(res.get("tree"), dict) and "apps" in res["tree"]:
            res["tree"] = mapping.convert(res["tree"])        # a settled action's tree
        if method == "tree" and isinstance(res, dict) and "apps" in res and not (params or {}).get("native"):
            return mapping.convert(res)
        return res

    # Below, only self.call: inside the daemon these methods are bound to its PooledClient.
    def native_tree(self, params=None, timeout=15.0):
        """The runner's own tree (dump-fixture); `native` also keeps it out of the daemon's cache."""
        return self.call("tree", dict(params or {}, native=True), timeout)

    # -- inputs: the Android settle spec becomes the iOS rule
    def act(self, dump, handle, action=None, custom=None, args=None, settle=None, force=False, event_ms=None):
        p = {"dump": dump, "handle": handle}
        if custom is not None:
            p["custom"] = custom
        else:
            p["action"] = action
        if args:
            p["args"] = args
        s = ios_settle(settle)
        if s is not None:
            p["settle"] = s
        if force:
            p["force"] = True
        # a long press holds 1 s; a click may be a touch (~280 ms)
        return self.call("act", p, timeout=_budget(s, 2.0))

    def gesture(self, type, points, ms=None, settle=None, hold_end_ms=None):
        p = {"type": type, "points": [[float(x), float(y)] for x, y in points]}
        if ms is not None:
            p["ms"] = ms
        if hold_end_ms:
            p["hold_end_ms"] = hold_end_ms
        s = ios_settle(settle)
        if s is not None:
            p["settle"] = s
        return self.call("gesture", p, timeout=_budget(s, (ms or 0) / 1000 + 2))

    def global_action(self, name, settle=None):
        p = {"name": name}
        s = ios_settle(settle)
        if s is not None:
            p["settle"] = s
        return self.call("global", p, timeout=_budget(s))

    def type_text(self, text, settle=None):
        """Keys into the focused input (the runner refuses without one: not-focused)."""
        p = {"text": text}
        s = ios_settle(settle)
        if s is not None:
            p["settle"] = s
        return self.call("type", p, timeout=_budget(s, 5 + len(text) * 0.1))

    def hide_keyboard(self, settle=None):
        s = ios_settle(settle or {})
        return self.call("hide_keyboard", {"settle": s}, timeout=_budget(s, 5))

    def log_history(self):
        """Recent syslog lines: only the daemon keeps them (its PooledClient answers); None here."""
        return None

    def clipboard(self, set=None):
        raise UserError("the clipboard is not available on iOS", "unsupported")

    # -- waits
    def wait_idle(self, quiet_ms=150, timeout_ms=2000):
        timeout_ms = int(timeout_ms)
        return self.call("wait_idle", {"timeout_ms": timeout_ms}, timeout=timeout_ms / 1000 + 5)

    def wait_for(self, timeout_ms=5000, **cond):
        cond = {k: v for k, v in cond.items() if v is not None}
        for k, why in (("toast", "iOS has no toasts"), ("activity", "iOS has no activities: use --window or --text")):
            if k in cond:
                raise UserError(f"wait --{k} is not available on iOS ({why})", "unsupported")
        t0 = time.monotonic()
        deadline = t0 + timeout_ms / 1000
        while True:
            tree = self.call("tree", {}, timeout=15.0)
            ok, detail = check(tree, cond)
            if ok:
                return dict(detail, matched=True, ms=round((time.monotonic() - t0) * 1000))
            left = deadline - time.monotonic()
            if left <= 0:
                raise UserError(f"wait_for timed out after {timeout_ms} ms", "timeout")
            # woken by the runner's change counter, then the settle rule: never fixed polling (§13.5)
            step = int(min(1.0, left) * 1000)
            r = self.call("wait_change", {"gen": tree.get("gen"), "timeout_ms": step}, timeout=step / 1000 + 5)
            if r.get("changed") and deadline - time.monotonic() > 0.1:
                self.wait_idle(timeout_ms=int(min(CAP_MS, (deadline - time.monotonic()) * 1000)))
