"""Resolution: a saved ref (or a locator) -> a live handle. Pure: no device I/O.

A ref is a *locator*, not a coordinate. `resolve_ref` re-finds the element a
ref named in a fresh tree, in tiers, and each tier must produce a *unique*
hit or it does not count:

  0  fast path   the ref's dump is still the device's latest dump and the
                 content generation has not moved: use the saved handle
  1  identity    `uid` (API 33+ getUniqueId) or `vid` (a per-View id, when
                 the agent reports one), in the same window
  2  exact       role, label, text, desc, hint, id, class and context all
                 equal (no bounds, no volatile flags; Maestro)
  3  locators    id+text, id+path, text+class, desc, in that order (appium-mcp),
                 then class+path for an id-less, label-less control; path
                 locators are never used inside lists (row indices shift)
  4  scoring     Artemis weights: id +0.5 (mismatch -0.5), normalized text
                 +0.4, overlap +0.3, tap-point containment +0.3; >2.5x size
                 mismatch rejected; distance decay max(0.5, 1 - d/800) with d
                 scaled to a 1080x1920 diagonal; accept >= 0.75, or >= 0.55
                 when it is the only candidate that high; heal moves <= 200 px;
                 a candidate needs a positive id/text score (never position
                 alone)

If the screen signature changed, only tiers 1-2 run, so a stale "OK" cannot
hit a look-alike on another screen. Two more guards keep it from guessing by
position: tiers 3-4 only consider candidates whose context (the words
around it, snapshot.context) is the closest to the saved one among elements
of its class (and shares something with it), and tier 4
never picks among candidates that are equally good on identity (id, text,
context) just because one sits where the old one was.

Failures are typed UserErrors, with structured `data`:
  stale-ref   reason gone | shifted | occupied (+ what is there now)
  ambiguous   candidates + the locators tried
  offscreen   the element exists but is scrolled out (+ a scroll-to hint)
  occluded    covered by another window, or (for gestures) the keyboard/a view

An element under the keyboard or behind a view drawn above it is still
*resolved*, with `occluded` set: ACTION_CLICK does not go through touch, so
whether that is fatal depends on the method (see `check_occlusion`).

Locators (`find`): --id/--text/--desc/--class/--role (+ --index) and the
spatial --right-of/--left-of/--above/--below/--near, matched against the
snapshot's elements (merged labels and roles: what the agent saw), then mapped
back to handles. Non-unique matches are `ambiguous`; --index picks one
explicitly.
"""
import math
import re

from droidctl import snapshot as S
from droidctl import spatial as sp
from droidctl.core import UserError

HEAL_PX = 200
ACCEPT = 0.75
ACCEPT_UNIQUE = 0.55
SIZE_RATIO = 2.5
DECAY_PX = 800
REF_DIAG = math.hypot(1080, 1920)     # the distances above are for this screen size
TWIN_EPS = 1e-6
EXACT_KEYS = ("role", "label", "text", "desc", "hint", "id", "class", "ctx")


# --------------------------------------------------------------------------
# normalization and similarity
# --------------------------------------------------------------------------
_BADGE = re.compile(r"\s*[(\[]\s*\d+\+?\s*[)\]]")


def norm(s):
    """Case-, space- and badge-insensitive text: "Inbox (3)" == "inbox"."""
    if not s:
        return ""
    return re.sub(r"\s+", " ", _BADGE.sub("", s)).strip().lower()


def _txt(r):
    """The words a fingerprint is known by: own text, else the merged label."""
    return norm(r.get("text") or r.get("label"))


def ctx_sim(a, b):
    """Jaccard similarity of two context lists (1.0 when both are empty)."""
    A, B = {norm(x) for x in a or ()}, {norm(x) for x in b or ()}
    if not A and not B:
        return 1.0
    return len(A & B) / len(A | B)


def ctx_compatible(a, b):
    """False only when both have context and it shares nothing: a different place."""
    if not a or not b:
        return True
    return ctx_sim(a, b) > 0


# --------------------------------------------------------------------------
# the result
# --------------------------------------------------------------------------
class Resolution:
    """Where a ref or locator landed in the fresh snapshot."""

    def __init__(self, elem, snap, tier, via=None, score=None, moved=None, node=None):
        self.elem = elem                  # snapshot Elem, or None for a hidden node
        self.snap = snap
        self.node = node or elem.node     # the snapshot Node acted on
        self.tier = tier                  # 0..4, or "locator"
        self.via = via
        self.score = score
        self.moved = moved
        self.occluded = None              # None | {"by": "keyboard"} | {"by": "view", "covering": {...}}

    @property
    def handle(self):
        return self.node.raw.get("handle")

    @property
    def click(self):
        """The handle to ACTION_CLICK: the node itself, a folded one, or its clickable ancestor."""
        if self.elem is not None:
            n = S.click_node(self.elem)
        else:
            n = self.node if self.node.clickable else next(
                (a for a in self.node.ancestors() if a.clickable), None)
        return n.raw.get("handle") if n is not None else None

    @property
    def tap(self):
        if self.elem is not None and self.elem.tap:
            return list(self.elem.tap)
        r = self.node.vis or self.node.rect
        return list(sp.center(r)) if r else None

    def to_json(self):
        return {"ref": self.elem.ref if self.elem is not None else None,
                "handle": self.handle, "click": self.click, "dump": self.snap.dump,
                "tier": self.tier, "via": self.via,
                "score": round(self.score, 3) if self.score is not None else None,
                "moved": self.moved, "occluded": self.occluded, "tap": self.tap,
                "element": _describe(self.elem, self.node)}


def _describe(elem, node=None):
    if elem is not None:
        return {"ref": elem.ref, "role": elem.role, "label": elem.label_full or None,
                "id": elem.res_id, "bounds": list(elem.rect) if elem.rect else None,
                "region": getattr(elem, "region", None)}
    n = node
    return {"ref": None, "role": S.short_class(n.raw.get("class")).lower(),
            "label": n.own_label() or None, "id": n.get("id"),
            "bounds": list(n.rect) if n.rect else None}


# --------------------------------------------------------------------------
# refs
# --------------------------------------------------------------------------
def fast_path(state, ref, device_dump, device_gen):
    """Tier 0: the saved handle, if the device has neither re-dumped nor changed.

    ``device_gen`` (the agent's content-generation counter) stands in for "the
    signature is unchanged": the signature needs a fresh tree, the counter is
    free. Returns the saved ref record with ``tier: 0``, or None.
    """
    rec = (state or {}).get("refs", {}).get(str(ref))
    if rec is None or state.get("dump") is None:
        return None
    if state["dump"] == device_dump and state.get("gen") == device_gen and rec.get("dump") == device_dump:
        return {**rec, "tier": 0}
    return None


def _saved(state, ref):
    rec = (state or {}).get("refs", {}).get(str(ref))
    if rec is None:
        # an element that went away on this screen: its number is never reused, and
        # it resolves by its fingerprint (back in view: acts; else stale-ref/offscreen)
        rec = (state or {}).get("retired", {}).get(str(ref))
    if rec is None:
        known = sorted((state or {}).get("refs", {}), key=int)
        raise UserError(f"no ref [{ref}] in the last snapshot"
                        + (f" (refs 1-{known[-1]})" if known else ""), "stale-ref",
                        hint="run: droidctl snapshot",
                        data={"reason": "gone", "ref": ref})
    return rec


def resolve_ref(state, ref, tree, activity=None):
    """Re-find saved ``ref`` (from ``state``, a snapshot.to_state dict) in ``tree``.

    Returns a Resolution; raises UserError stale-ref / ambiguous / offscreen /
    occluded. ``activity`` is ``pkg/.Activity`` when known (it feeds the
    signature, as in the snapshot that made the ref).
    """
    old = dict(_saved(state, ref))
    if old.get("parent") is not None:
        old["_container_role"] = state["refs"].get(str(old["parent"]), {}).get("role")
    snap = S.build(tree, activity=activity if activity is not None else state.get("activity") or None)
    S.carry_refs(state, snap)     # the numbers in results and errors are the agent's numbers
    return resolve_in(old, state, snap, ref)


def resolve_in(old, state, snap, ref=None):
    """resolve_ref against an already built snapshot (the daemon keeps one)."""
    cands = []
    for e in snap.elements:
        r = S.ref_record(e, snap)
        if e.container is not None:
            r["_container_role"] = e.container.role
        cands.append((e, r))
    same_screen = state.get("sig") == snap.sig
    tried = []

    # A partial (degraded) tree can't prove uniqueness: the real match may sit in
    # a subtree that wasn't read while a look-alike was. Only a window read in
    # full is searched, and only the ref's own window (ids are stable for a
    # window's lifetime); identity (uid) still counts anywhere.
    if snap.degraded:
        win = old.get("window")
        if old.get("uid"):
            hits = [(e, r) for e, r in cands if r.get("uid") == old["uid"]]
            if len(hits) == 1:
                return _finish(hits[0][0], snap, old, 1, "uid")
        if win is None or win in snap.incomplete or not any(w.id == win for w in snap.windows):
            raise UserError(f"the device returned a partial tree ({snap.degraded}), so ref [{ref}] "
                            f"({_what(old)}) can't be matched with certainty", "timeout",
                            hint="wait for the screen to settle, then run: droidctl snapshot",
                            data={"reason": "degraded", "degraded": snap.degraded, "ref": ref})
        cands = [(e, r) for e, r in cands if r.get("window") == win]

    # tier 1: identity
    for key in ("uid", "vid"):
        if not old.get(key):
            continue
        hits = [(e, r) for e, r in cands if r.get(key) == old[key]
                and (key == "uid" or (r.get("window") == old.get("window")
                                      and r.get("class") == old.get("class")))]
        tried.append(key)
        if len(hits) == 1:
            return _finish(hits[0][0], snap, old, 1, key)

    # tier 2: exact attributes
    exact = [(e, r) for e, r in cands if all(r.get(k) == old.get(k) for k in EXACT_KEYS)]
    tried.append("exact")
    if len(exact) == 1:
        return _finish(exact[0][0], snap, old, 2, "exact")
    multi = exact if len(exact) > 1 else None

    if same_screen:
        # tier 3: ordered locators. A text seen once on the old screen is
        # identity enough; a repeated one ("1", "+", "Delete") only counts
        # where its context is the closest
        repeated = not _txt(old) or sum(_txt(r) == _txt(old) for r in state.get("refs", {}).values()) > 1
        compat = _closest_context(old, cands) if repeated else [
            (e, r) for e, r in cands if ctx_compatible(old.get("ctx"), r.get("ctx"))]
        for name, pred in LOCATORS:
            if not pred(old, old):
                continue                  # the saved element has no such attributes
            tried.append(name)
            hits = [(e, r) for e, r in compat if pred(old, r)]
            if len(hits) == 1:
                return _finish(hits[0][0], snap, old, 3, name)
            if len(hits) > 1 and multi is None:
                multi = hits

        # tier 4: scoring
        tried.append("score")
        # (equal-identity twins here are candidates that share an id but not the
        # text: none of them *is* the ref, so they end as stale-ref, not ambiguous)
        res = _score(old, compat, snap)
        if isinstance(res, Resolution):
            return res
        shifted = res[1] if res is not None and res[0] == "shifted" else None
    else:
        shifted = None

    # nothing unique: explain why, most specific first
    hidden = _find_hidden(snap, lambda n: _node_exact(n, old))
    if hidden is not None:
        return _hidden_result(hidden, snap, old, ref)
    if multi:
        raise _ambiguous(f"ref [{ref}] ({_what(old)}) now matches {len(multi)} elements",
                         [e for e, _ in multi], tried)
    if shifted is not None:
        e, moved = shifted
        raise UserError(f"ref [{ref}] ({_what(old)}) moved {moved} px, more than the {HEAL_PX} px a ref may heal",
                        "stale-ref", hint="run: droidctl snapshot",
                        data={"reason": "shifted", "ref": ref, "now": _describe(e), "moved": moved})
    raise _stale(ref, old, snap, same_screen)


def _stale(ref, old, snap, same_screen):
    there = _at(snap, old.get("tap"))
    why = "" if same_screen else " (the screen changed)"
    if there is not None:
        return UserError(f"ref [{ref}] ({_what(old)}) is gone{why}; [{there.ref}] {there.role} "
                         f"{_q(there.label_full)} is there now", "stale-ref", hint="run: droidctl snapshot",
                         data={"reason": "occupied", "ref": ref, "screen_changed": not same_screen,
                               "now": _describe(there)})
    hint = "run: droidctl snapshot"
    if old.get("parent") is not None:
        hint += f"; if it scrolled away: droidctl scroll-to --text {_q(old.get('label') or old.get('text') or '')}"
    return UserError(f"ref [{ref}] ({_what(old)}) is gone{why}", "stale-ref", hint=hint,
                     data={"reason": "gone", "ref": ref, "screen_changed": not same_screen})


def _closest_context(old, cands):
    """Candidates allowed into tiers 3-4: context shares something with the
    saved one, and, among elements of the saved class, is the closest there
    is. Rows that share only their "−"/"+" buttons are other rows."""
    compat = [(e, r) for e, r in cands if ctx_compatible(old.get("ctx"), r.get("ctx"))]
    if not old.get("ctx"):
        return compat
    sims = [ctx_sim(old["ctx"], r.get("ctx")) for _, r in compat if r.get("class") == old.get("class")]
    if not sims:
        return compat
    best = max(sims)
    return [(e, r) for e, r in compat
            if r.get("class") != old.get("class") or ctx_sim(old["ctx"], r.get("ctx")) >= best - TWIN_EPS]


def _what(r):
    lab = r.get("label") or r.get("text") or r.get("desc") or r.get("hint") or ""
    return f"{r.get('role') or 'element'} {_q(lab)}" if lab else (r.get("role") or "element")


def _q(s):
    return '"' + (s or "").replace('"', '\\"')[:60] + '"'


def _at(snap, point):
    """The smallest non-container element whose box contains ``point``."""
    if not point:
        return None
    hits = [e for e in snap.elements if e.rect and not e.node.is_list and sp.contains(e.rect, tuple(point))]
    return min(hits, key=lambda e: sp.area(e.rect)) if hits else None


ADAPTER_ROLES = ("list", "grid", "pager")


def _structural(o):
    """Paths are child indices: inside an adapter list they are row positions,
    which is exactly the "never by index" trap (android_world), so list members
    never use them. A plain ScrollView's children are static, so they may."""
    return o.get("_container_role") not in ADAPTER_ROLES and bool(o.get("path"))


LOCATORS = [
    ("id+text", lambda o, r: bool(o.get("id") and _txt(o)) and r.get("id") == o.get("id") and _txt(r) == _txt(o)),
    ("id+path", lambda o, r: bool(o.get("id") and _structural(o) and o["path"][0])
     and r.get("id") == o.get("id") and r.get("path") == o.get("path")),
    ("text+class", lambda o, r: bool(_txt(o) and o.get("class"))
     and r.get("class") == o.get("class") and _txt(r) == _txt(o)),
    ("desc", lambda o, r: bool(o.get("desc")) and norm(r.get("desc")) == norm(o.get("desc"))),
    # an input is named by its hint; its text is the value (typed into a field with
    # no id, it was "gone" while the same field sat there with the new text)
    ("input+hint", lambda o, r: bool(o.get("role") == "input" and o.get("hint") and o.get("class"))
     and r.get("role") == "input" and r.get("class") == o.get("class") and r.get("hint") == o.get("hint")
     and r.get("window") == o.get("window")),
    # last: an id-less, label-less control (a Compose icon) outside any list is
    # known only by where it sits in the tree; the screen is unchanged (tier 3)
    ("class+path", lambda o, r: bool(not o.get("id") and not _txt(o) and not o.get("desc")
                                     and _structural(o) and o.get("class"))
     and r.get("class") == o.get("class") and r.get("path") == o.get("path")
     and r.get("window") == o.get("window")),
]


def _score(old, cands, snap):
    """Tier 4. Returns a Resolution, ("twins", [(e, r)...]), ("shifted", (e, px)) or None."""
    ob = tuple(old["bounds"]) if old.get("bounds") else None
    if ob is None:
        return None
    scale = REF_DIAG / math.hypot(snap.screen[2], snap.screen[3])
    tap = tuple(old["tap"]) if old.get("tap") else sp.center(ob)
    scored = []
    for e, r in cands:
        if not e.rect or (r.get("role") != old.get("role") and r.get("class") != old.get("class")):
            continue
        cb = e.rect
        if max(sp.width(cb) / sp.width(ob), sp.width(ob) / sp.width(cb),
               sp.height(cb) / sp.height(ob), sp.height(ob) / sp.height(cb)) > SIZE_RATIO:
            continue
        ident = 0.0
        if old.get("id") and r.get("id"):
            ident += 0.5 if r["id"] == old["id"] else -0.5
        if _txt(old) and _txt(r) == _txt(old):
            ident += 0.4
        if ident <= 0:
            continue                      # never by position alone: an id or the text must agree
        pos = 0.3 * sp.overlap_ratio(ob, cb) + (0.3 if sp.contains(cb, tap) else 0.0)
        d = math.dist(sp.center(ob), sp.center(cb))
        total = (ident + pos) * max(0.5, 1 - d * scale / DECAY_PX)
        scored.append((total, ident, ctx_sim(old.get("ctx"), r.get("ctx")), round(d * scale), e, r))
    passing = sorted((s for s in scored if s[0] >= ACCEPT_UNIQUE), key=lambda s: -s[0])
    if not passing:
        return None
    best = passing[0]
    if best[0] < ACCEPT and len(passing) > 1:
        return ("twins", [(s[4], s[5]) for s in passing])
    # never choose between equally good identities by where they sit: a twin
    # counts even if it scored low only because it is somewhere else
    twins = [s for s in scored if s is not best and s[1] >= best[1] - TWIN_EPS and s[2] >= best[2] - TWIN_EPS]
    if twins:
        return ("twins", [(s[4], s[5]) for s in [best, *twins]])
    if best[3] > HEAL_PX:
        return ("shifted", (best[4], best[3]))
    return _finish(best[4], snap, old, 4, "score", score=best[0], moved=best[3])


def _finish(elem, snap, old, tier, via, score=None, moved=None):
    res = Resolution(elem, snap, tier, via=via, score=score)
    if moved is None and old.get("bounds") and elem.rect:
        moved = round(math.dist(sp.center(tuple(old["bounds"])), sp.center(elem.rect)))
    res.moved = moved
    res.occluded = _occlusion(elem, snap)
    return res


# --------------------------------------------------------------------------
# occlusion and hidden nodes
# --------------------------------------------------------------------------
def _occlusion(elem, snap):
    """A view drawn above the element's tap point (snapshot marks it `covered`)."""
    if not elem.covered or not elem.rect:
        return None
    if any(n.kb for n in elem.nodes):     # listed only because the IME hides it
        return {"by": "keyboard", "visible": False}
    c = sp.center(elem.rect)
    over = [b for b in snap.elements if b is not elem and b.rect and b.node.win is elem.node.win
            and not b.node.is_ancestor_of(elem.node) and not elem.node.is_ancestor_of(b.node)
            and sp.contains(b.rect, c) and S._draws_above(b.node, elem.node)]
    top = min(over, key=lambda b: sp.area(b.rect)) if over else None
    return {"by": "view", "covering": _describe(top) if top is not None else None}


def _walk_all(snap):
    for root in snap.roots:
        yield from S._walk(root)


def _node_record(n):
    return {"text": n.get("text"), "desc": n.get("desc"), "hint": n.get("hint"),
            "id": n.get("id"), "class": n.raw.get("class")}


def _node_exact(n, old):
    """A raw node that is the saved element's primary node (by attributes and context)."""
    if n.raw.get("class") != old.get("class") or n.get("id") != old.get("id"):
        return False
    if (n.get("text"), n.get("desc"), n.get("hint")) != (old.get("text"), old.get("desc"), old.get("hint")):
        return False
    if not (old.get("text") or old.get("desc") or old.get("hint") or old.get("id")):
        return False                      # nothing identifying: a hidden match would be a guess
    return ctx_compatible(old.get("ctx"), S.context(n, [x for x in [n.own_label()] if x]))


def _find_hidden(snap, pred):
    """The one node matching ``pred`` that the snapshot does not show, or None."""
    hits = [n for n in _walk_all(snap) if not n.shown and pred(n)]
    return hits[0] if len(hits) == 1 else None


def _hidden_result(n, snap, old, ref):
    """Why a node exists but is not listed: scrolled out (offscreen), or under
    the keyboard / another window (occluded). Under the keyboard it still
    resolves, since ACTION_CLICK works there; other windows are fatal."""
    what = _what(old) if old else _describe(None, n)["label"] or "element"
    label = (old or {}).get("label") or n.own_label() or n.get("text") or ""
    raw_b = n.raw.get("bounds")
    kb = snap.keyboard
    inside = sp.inter(n.rect, n.clip) if n.rect is not None and n.clip is not None else None
    # under the IME: Android 9 even reports such nodes visible=false, so this
    # is decided by geometry alone
    if inside is not None and kb is not None and n.win.kind != "ime" and sp.inter(inside, kb) is not None \
            and sp.area(sp.inter(inside, kb)) >= 0.5 * sp.area(inside):
        res = Resolution(n.el, snap, "hidden", via="keyboard", node=n)
        # visible=false means the agent refuses a plain `act`; the action layer
        # decides whether to pass force (ACTION_CLICK does not need touch)
        res.occluded = {"by": "keyboard", "visible": n.raw.get("visible") is not False}
        return res
    if inside is not None and n.raw.get("visible") is not False:
        if n.frac < S.MIN_FRAC:
            covering = next((w for w in snap.windows if w.rect and w is not n.win and w.kind != "ime"
                             and w.layer > n.win.layer and sp.inter(w.rect, inside)), None)
            if covering is not None and sp.inter(n.rect, n.clip) is not None and covering.kind in (
                    "dialog", "popup", "system", "skip"):
                raise UserError(f"{what} is covered by another window ({covering.title or covering.type})",
                                "occluded", hint="dismiss it first (droidctl back), or snapshot to see it",
                                data={"by": "window", "window": covering.title or covering.type,
                                      "ref": ref})
    direction = _direction(raw_b, n)
    hint = f"droidctl scroll-to --text {_q(label)}" if label else "scroll the list, then snapshot"
    raise UserError(f"{what} is offscreen" + (f" ({direction})" if direction else ""), "offscreen",
                    hint=hint, data={"ref": ref, "direction": direction, "label": label or None})


def _direction(raw_b, n):
    """Which way the element lies, from its (clipped, often inverted) raw bounds."""
    if not raw_b or len(raw_b) != 4:
        return None
    clip = n.clip or n.win.rect
    if clip is None:
        return None
    l, t, r, b = raw_b
    if t >= clip[3] or (b < t and t >= clip[3] - 1):
        return "down"
    if b <= clip[1] or (b < t and b <= clip[1] + 1):
        return "up"
    if l >= clip[2]:
        return "right"
    if r <= clip[0]:
        return "left"
    if n.rect is not None:
        if n.rect[1] >= clip[3] - S.SLIVER:
            return "down"
        if n.rect[3] <= clip[1] + S.SLIVER:
            return "up"
    return None


def check_occlusion(res, method="auto"):
    """Is acting on ``res`` with ``method`` safe? Raises `occluded` when not.

    - gesture: any occlusion is fatal (the touch would land on the cover);
    - auto:    a view drawn over it is fatal (ACTION_CLICK would bypass what
               the user sees as blocking); the keyboard is not (ACTION_CLICK);
    - action:  never (the node itself receives the action).
    """
    occ = res.occluded
    if not occ or method == "action":
        return res
    if method == "gesture" or occ["by"] != "keyboard":
        by = "the keyboard" if occ["by"] == "keyboard" else "a view drawn above it"
        cov = occ.get("covering") or {}
        if cov.get("label") or cov.get("ref"):
            by += f" ([{cov.get('ref')}] {cov.get('role')} {_q(cov.get('label'))})" if cov.get("ref") \
                else f" ({cov.get('role')} {_q(cov.get('label'))})"
        raise UserError(f"{_describe(res.elem, res.node)['label'] or 'the element'} is covered by {by}",
                        "occluded",
                        hint="--method action acts on the node itself" if method != "action" else "",
                        data={"occluded": occ, "element": _describe(res.elem, res.node)})
    return res


# --------------------------------------------------------------------------
# locators
# --------------------------------------------------------------------------
SPATIAL = {"right_of": "right", "left_of": "left", "above": "above", "below": "below"}


def _ambiguous(msg, elems, tried=None):
    """Never a pick: which one is meant is the caller's call. Each candidate says
    where it is, so look-alikes (two unlabeled buttons) can be told apart and
    named with a spatial locator or --index without another snapshot."""
    cands = [_describe(e) for e in elems[:12]]
    listing = ", ".join(f"[{c['ref']}] {c['role']} {_q(c['label'] or '')}{_place(c)}" for c in cands[:6])
    return UserError(f"{msg}: {listing}" + (" …" if len(elems) > 6 else ""), "ambiguous",
                     hint="use a ref from `snapshot`, add --role/--id, a spatial locator "
                          "(--right-of/--below … TEXT), or --index N (0-based, in the order listed)",
                     data={"candidates": cands, "count": len(elems), "tried": tried or []})


def _place(c):
    b = c.get("bounds")
    if not b:
        return ""
    where = f"{(b[0] + b[2]) // 2},{(b[1] + b[3]) // 2}"
    return f" ({c['region']} @{where})" if c.get("region") else f" (@{where})"


def _label_nodes(e):
    """Every node whose text went into ``e``'s label: its own nodes plus the
    passive descendants it absorbed (a Compose icon's desc inside a button, the
    texts of a row). Locators must match what the snapshot shows."""
    out, seen, stack = [], set(), list(e.nodes)
    while stack:
        n = stack.pop()
        if id(n) in seen:
            continue
        seen.add(id(n))
        if n.el is e or n in e.nodes:
            out.append(n)
        stack.extend(n.children)
    return out


def _descs(e):
    return {norm(n.get("desc")) for n in _label_nodes(e) if n.get("desc")}


def _text_fields(e):
    out = [e.label_full, *e.segments]
    for n in e.nodes:
        out += [n.get("text"), n.get("desc"), n.get("hint"), n.get("error")]
    return {norm(x) for x in out if x}


def _match_text(elems, needle, fields):
    """Exact (normalized) matches if any, else substring matches."""
    k = norm(needle)
    exact = [e for e in elems if k in fields(e)]
    if exact:
        return exact
    return [e for e in elems if any(k in f for f in fields(e))]


def _match_id(e, want):
    rid = e.res_id or ""
    return rid == want or rid.split(":id/")[-1] == want or sp.short_id(rid) == want


def _match_class(e, want):
    c = e.node.raw.get("class") or ""
    return c == want or S.short_class(c) == want or c.split(".")[-1] == want


def find(snap, id=None, text=None, desc=None, cls=None, role=None, index=None,
         right_of=None, left_of=None, above=None, below=None, near=None):
    """Resolve a locator against ``snap``'s elements. Unique match or a typed error.

    Spatial anchors are texts (resolved the same way, and they must be unique).
    ``index`` (0-based) picks among several matches explicitly: in reference
    order, or by distance to the anchor when a spatial locator is used.
    """
    loc = {k: v for k, v in dict(id=id, text=text, desc=desc, cls=cls, role=role, right_of=right_of,
                                 left_of=left_of, above=above, below=below, near=near).items()
           if v is not None}
    if not loc:
        raise UserError("no locator given (a ref, or --id/--text/--desc/--class/--role)", "bad-args")
    elems = [e for e in snap.elements if e.rect]
    tried = []
    if role is not None:
        elems = [e for e in elems if e.role == role]
        tried.append(f"role={role}")
    if cls is not None:
        elems = [e for e in elems if _match_class(e, cls)]
        tried.append(f"class={cls}")
    if id is not None:
        elems = [e for e in elems if _match_id(e, id)]
        tried.append(f"id={id}")
    if desc is not None:
        elems = _match_text(elems, desc, _descs)
        tried.append(f"desc={desc}")
    if text is not None:
        elems = _match_text(elems, text, _text_fields)
        tried.append(f"text={text}")

    order = None
    for key, direction in SPATIAL.items():
        if loc.get(key) is None:
            continue
        anchor, arect = _anchor(snap, loc[key])
        elems = [e for e in elems if e is not anchor and sp.RELATIONS[direction](e.rect, arect)
                 and not e.node.is_ancestor_of(anchor.node)]
        order = sorted(elems, key=lambda e: (max(0, sp.gap(e.rect, arect, direction)),
                                              math.dist(sp.center(e.rect), sp.center(arect))))
        tried.append(f"{key.replace('_', '-')}={loc[key]}")
    if near is not None:
        anchor, arect = _anchor(snap, near)
        elems = [e for e in elems if e is not anchor and not e.node.is_ancestor_of(anchor.node)
                 and not anchor.node.is_ancestor_of(e.node)]
        order = sp.near_to(elems, arect, key=lambda e: e.rect)
        tried.append(f"near={near}")
        if len(order) >= 2 and index is None:
            d0 = math.dist(sp.center(order[0].rect), sp.center(arect))
            d1 = math.dist(sp.center(order[1].rect), sp.center(arect))
            if d1 - d0 > 8:               # --near means "the nearest": unique only if clearly nearest
                order = order[:1]

    pool = order if order is not None else elems
    if index is not None:
        if not 0 <= index < len(pool):
            raise UserError(f"--index {index}: only {len(pool)} match(es) for {' '.join(tried)}",
                            "not-found", data={"count": len(pool), "tried": tried})
        return _locator_result(pool[index], snap, "index")
    if len(pool) == 1:
        return _locator_result(pool[0], snap, "locator")
    if len(pool) > 1:
        raise _ambiguous(f"{' '.join(tried)} matches {len(pool)} elements", pool, tried)

    # nothing listed: maybe it exists but is scrolled out or under something
    hidden = _find_hidden(snap, lambda n: _node_matches(n, id=id, text=text, desc=desc, cls=cls))
    if hidden is not None and not (right_of or left_of or above or below or near or role):
        return _hidden_result(hidden, snap, None, None)
    raise UserError(f"nothing matches {' '.join(tried)}", "not-found",
                    hint="run: droidctl snapshot (or snapshot --find TEXT)", data={"tried": tried})


def _locator_result(e, snap, via):
    res = Resolution(e, snap, "locator", via=via)
    res.occluded = _occlusion(e, snap)
    return res


def _anchor(snap, spec):
    """A spatial anchor -> (element, rect): a ref number (int or "[7]"/"7") or a
    unique text. A text may be one segment of a merged row label ("Ada Lovelace"
    of "Ada Lovelace · Lunch tomorrow?"); the rect is then that text's own box,
    so the star button inside the same row is still "right of" the name."""
    m = re.fullmatch(r"\[?(\d+)\]?", str(spec).strip())
    if m:
        n = int(m.group(1))
        e = next((x for x in snap.elements if x.ref == n and x.rect), None)
        if e is None:
            raise UserError(f"anchor [{n}] is not on the screen", "stale-ref", data={"reason": "gone", "ref": n})
        return e, e.rect
    hits = _match_text([e for e in snap.elements if e.rect], spec, _text_fields)
    if not hits:
        raise UserError(f"anchor {_q(spec)}: nothing matches", "not-found", data={"anchor": spec})
    if len(hits) > 1:
        raise _ambiguous(f"anchor {_q(spec)} matches {len(hits)} elements", hits, [f"anchor={spec}"])
    return hits[0], _anchor_rect(hits[0], spec)


def _anchor_rect(e, spec):
    """The box of the text in ``e`` that matched ``spec`` (exact before substring,
    smallest first), or the element's box when the whole label matched."""
    k = norm(spec)
    exact, sub = [], []
    for n in _label_nodes(e):
        box = n.box or n.rect
        if not box or not sp.area(box):
            continue
        vals = {norm(n.get(f)) for f in ("text", "desc", "hint") if n.get(f)}
        if k in vals:
            exact.append((sp.area(box), box))
        elif any(k in v for v in vals):
            sub.append((sp.area(box), box))
    best = sorted(exact) or sorted(sub)
    if not best or norm(e.label_full) == k:
        return e.rect
    return best[0][1]


def _node_matches(n, id=None, text=None, desc=None, cls=None):
    if id is None and text is None and desc is None:
        return False
    if id is not None and not (n.get("id") or "").split(":id/")[-1] == id and n.get("id") != id:
        return False
    if cls is not None and S.short_class(n.raw.get("class")) != cls and n.raw.get("class") != cls:
        return False
    if desc is not None and norm(n.get("desc")) != norm(desc):
        return False
    if text is not None and norm(text) not in {norm(n.get("text")), norm(n.get("desc")), norm(n.get("hint"))}:
        return False
    return True
