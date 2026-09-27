"""The snapshot: a raw device tree in, a compact list of elements with refs out. PURE.

Pruning works on the *tree* (clipping, visibility and row merging need the
hierarchy), in this order:

1. windows: keep the app window plus dialogs/popups above it; the IME only marks
   ``keyboard=shown`` and occludes what is under it; system windows (status and
   navigation bars, the Edge panel) are dropped unless ``system=True``, except a
   system window covering half the screen (the notification shade, the keyguard).
2. clipping: every node is clipped to the screen, its window and its scrolling
   ancestors, then windows above it and fixed app bars (for scrolled content)
   are subtracted. The largest remaining piece is where a tap would land.
3. visibility: ``visible:false``, empty and *inverted* bounds are gone; a node
   under 10% visible is dropped unless something inside it is shown; a clipped
   sliver of 20 px or less is dropped.
4. keep: click / long-click / check / edit / scroll / custom actions, and nodes
   with text, desc, hint, state or error. Everything else collapses.
5. row merge: an actionable node absorbs the text of its passive descendants
   (joined with `` · ``) and stops at actionable descendants. A checkable that
   is not clickable itself (a CheckedTextView in a clickable row) folds into its
   row, which takes its role and state; so does a clickable child with the same
   bounds as its parent (Compose's inner clickables).
6. dedupe, then annotations, regions, reading order and refs.

The layout options only change how the result is *printed*: ref numbers are the
same for every layout, so `tap 7` means the same thing whatever was displayed.
"""
import hashlib
import json
import os
import re
import time

from droidctl import spatial as sp

TRUNC = 80          # display cap for a merged label (the full text stays searchable)
SLIVER = 20         # px: a clipped remainder this thin is not worth listing
MIN_FRAC = 0.10     # below this visible fraction a node is dropped
TINY = 3            # px: a node thinner than this (0x0, 1 px traps) is never listed
KB_COVER = 0.5      # an invisible node this much under the keyboard is listed as covered
TINY_TREE = 3       # an app window with at most this many nodes gets a "tiny tree" warning
MAX_DEFAULT = 150

SCROLL_ACTIONS = {"scroll_forward", "scroll_backward", "scroll_up", "scroll_down",
                  "scroll_left", "scroll_right"}
LIST_CLASSES = {"RecyclerView", "ListView", "GridView", "ScrollView", "NestedScrollView",
                "HorizontalScrollView", "ViewPager", "ViewPager2", "ExpandableListView", "AbsListView"}
CONTAINER_HINTS = ("Layout", "View", "Group", "Card", "Container", "Frame", "Host", "Cell")
TOP_BAR_CLASSES = {"Toolbar", "ActionBarContainer", "ActionBarView", "AppBarLayout", "MaterialToolbar"}
BOTTOM_BAR_CLASSES = {"BottomNavigationView", "NavigationBarView", "BottomAppBar",
                      "BottomNavigationMenuView", "NavigationBarMenuView"}
REGION_ORDER = ["dialog", "popup", "system", "top bar", "content", "fab", "bottom bar",
                "drawer", "sheet"]


def short_class(c):
    return (c or "").rsplit(".", 1)[-1]


def _clean(s):
    return re.sub(r"\s+", " ", s).strip() if s else ""


def est_tokens(text):
    """An ESTIMATE of LLM tokens (~3.5 characters per token); no tokenizer is used."""
    return round(len(text) / 3.5)


# --------------------------------------------------------------------------
# the node model
# --------------------------------------------------------------------------
class Win:
    def __init__(self, raw, screen):
        self.raw = raw
        self.id = raw.get("id")
        self.type = raw.get("type", "")
        self.layer = raw.get("layer", 0)
        self.title = raw.get("title") or ""
        self.pkg = raw.get("pkg") or (raw.get("root") or {}).get("pkg") or ""
        self.rect = sp.inter(sp.rect(raw.get("bounds")), screen)
        if raw.get("bounds") is None and raw.get("root"):
            # a window from rootInActiveWindow (right after a re-bind, or when
            # getWindows() is empty) has no window bounds: its root node's box is it
            self.rect = sp.inter(sp.rect(raw["root"].get("bounds")), screen)
        # Android 9 reports a dialog's/popup's window bounds shifted by its shadow
        # insets (-84,-84 on the SM-N950F: a PopupMenu window at [-42,461,..]
        # whose root node is at [42,545,..]); clipping nodes to that cuts rows
        # off, so trust the root node when it isn't inside the window
        # (only for a pure shift: an IME's root legitimately spans beyond its window)
        root, wr = sp.rect((raw.get("root") or {}).get("bounds")), sp.rect(raw.get("bounds"))
        if (root is not None and wr is not None and root != wr
                and abs(sp.width(root) - sp.width(wr)) <= 8 and abs(sp.height(root) - sp.height(wr)) <= 8):
            self.rect = sp.inter(root, screen) or self.rect
        self.kind = None          # main | dialog | popup | behind | system | ime | skip
        self.occluders = []
        self.bars = {}            # "top"/"bottom" -> Node


class Node:
    __slots__ = ("raw", "win", "parent", "index", "children", "cls", "rect", "clip", "in_scroll",
                 "flags", "actions", "custom", "pieces", "vis", "box", "frac", "shown", "any_shown",
                 "scroll_like", "is_list", "tabstrip", "tab_item", "el", "kb")

    def __init__(self, raw, win, parent, index):
        self.raw, self.win, self.parent, self.index = raw, win, parent, index
        self.children = []
        self.cls = short_class(raw.get("class"))
        self.rect = sp.rect(raw.get("bounds"))
        self.flags = set(raw.get("flags", ()))
        self.actions = {a for a in raw.get("actions", ()) if isinstance(a, str)}
        # a custom action with no label is a standard action the agent could not
        # name; only labelled ones mean something to a reader
        self.custom = [a["label"] for a in raw.get("actions", ())
                       if isinstance(a, dict) and a.get("label")]
        self.pieces, self.vis, self.box, self.frac = [], None, None, 0.0
        self.shown = self.any_shown = False
        self.tabstrip = self.tab_item = False
        self.kb = False           # hidden only by the keyboard (Android reports visible:false)
        self.el = None

    # --- intrinsic properties -------------------------------------------
    def get(self, k):
        v = self.raw.get(k)
        return _clean(v) if isinstance(v, str) else v

    @property
    def clickable(self):
        return "click" in self.actions or "clickable" in self.flags

    @property
    def long_clickable(self):
        return "long_click" in self.actions or "longClickable" in self.flags

    @property
    def checkable(self):
        return "checkable" in self.flags

    @property
    def editable(self):
        return "editable" in self.flags or "set_text" in self.actions

    @property
    def scrollable(self):
        return "scrollable" in self.flags or bool(self.actions & SCROLL_ACTIONS)

    @property
    def actionable(self):
        return (self.clickable or self.long_clickable or self.checkable or self.editable
                or bool(self.custom) or "set_progress" in self.actions)

    def own_label(self):
        """The node's own words: text (not a hint shown as text), else desc."""
        text, desc, hint = self.get("text"), self.get("desc"), self.get("hint")
        if self.editable and ("showingHint" in self.flags or (hint and text == hint)):
            text = ""
        if self.checkable and desc:        # a Switch's text is its "On"/"Off"
            return desc
        if self.cls.endswith("Switch") and text:
            # API 28 android.widget.Switch appends its textOn/textOff: "Wi-Fi OFF"
            for word in (" ON", " OFF"):
                if text.endswith(word) and len(text) > len(word):
                    text = text[:-len(word)]
                    break
        if text and desc and desc.lower() != text.lower():
            if desc.lower().startswith(text.lower()):
                return desc
            if text.lower() in desc.lower():
                return desc
            return f"{text} · {desc}"
        return text or desc or ""

    def has_content(self):
        return any(self.get(k) for k in ("text", "desc", "hint", "state", "error"))

    @property
    def progress(self):
        """A progress indicator (spinner or bar): worth listing even unlabeled."""
        return self.cls.endswith("ProgressBar") or bool(
            self.raw.get("range") and "set_progress" not in self.actions and not self.actionable)

    def ancestors(self):
        n = self.parent
        while n is not None:
            yield n
            n = n.parent

    def is_ancestor_of(self, other):
        return any(a is self for a in other.ancestors())


# --------------------------------------------------------------------------
# elements
# --------------------------------------------------------------------------
class Elem:
    def __init__(self, node, container):
        self.node = node
        self.nodes = [node]           # node + folded nodes (same control)
        self.segments = []            # label parts, own first
        self.adopted = None           # a checkable folded into this row
        self.container = container    # enclosing list Elem, or None
        self.children = []            # for lists: elements inside
        self.role = ""
        self.ann = []                 # ordered annotation strings
        self.region = "content"
        self.ref = None
        self.covered = False
        self.tap = None
        self.inferred = None          # label guessed from the id
        self.context = None           # (relation, text) for unlabeled controls
        self.list_info = None
        if container is not None:
            container.children.append(self)

    @property
    def rect(self):
        return self.node.box

    def add_segment(self, s):
        s = _clean(s)
        if not s:
            return
        low = s.lower()
        for i, old in enumerate(self.segments):
            ol = old.lower()
            if low == ol or low in ol:
                return
            if ol in low and i > 0:       # a longer version of a later segment
                self.segments[i] = s
                return
        self.segments.append(s)

    @property
    def label_full(self):
        return " · ".join(self.segments)

    @property
    def label(self):
        s = self.label_full
        return s if len(s) <= TRUNC else s[:TRUNC - 1].rstrip() + "…"

    @property
    def res_id(self):
        for n in self.nodes:
            if n.get("id"):
                return n.get("id")
        return None

    def flag(self, f):
        return any(f in n.flags for n in self.nodes)


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------
class Snap:
    """The result of build(): header facts, elements in ref order, warnings."""

    def __init__(self):
        self.pkg = self.activity = self.title = ""
        self.sig = ""
        self.keyboard = None          # IME rect or None
        self.dialog = False
        self.degraded = None          # the agent's reason ("timeout", "truncated", "no-root", ...) or None
        self.incomplete = set()       # ids of windows whose tree was not read completely
        self.unread = 0               # subtrees the agent did not read (degraded dumps)
        self.screen = (0, 0, 1, 1)
        self.elements = []            # in ref order (ref = index + 1)
        self.warnings = []
        self.dump = self.gen = None
        self.windows = []
        self.roots = []               # the Node tree of every kept window (the resolver searches it)
        self.toast = None             # a toast since the previous snapshot (set by the caller)
        self.evseq = None             # the agent's event-ring position this snapshot has seen


def build(tree, activity=None, system=False):
    """Raw `tree` result (plus optional ``pkg/.Activity``) -> Snap."""
    scr = tree.get("screen") or {}
    W, H = scr.get("w") or 1080, scr.get("h") or 1920
    screen = (0, 0, W, H)
    snap = Snap()
    snap.screen = screen
    snap.dump, snap.gen = tree.get("dump"), tree.get("gen")
    if tree.get("degraded"):
        snap.degraded = tree.get("reason") or "yes"

    wins = [Win(w, screen) for w in tree.get("windows", ())]
    for w in wins:
        root = w.raw.get("root")
        cut = sum(1 for n in _iter_raw(root) if n.get("truncated"))
        if cut or (root is None and (w.raw.get("no_root") or w.type == "application")):
            snap.incomplete.add(w.id)
        snap.unread += cut
    _classify(wins, snap, system)
    snap.windows = wins
    for w in wins:
        w.occluders = [o.rect for o in wins
                       if o is not w and o.rect and o.layer > w.layer and o.type != "accessibility_overlay"]

    roots = []
    for w in wins:
        if w.kind in (None, "skip", "ime") or not w.raw.get("root") or w.rect is None:
            continue
        root = _make(w.raw["root"], w, None, 0, w.rect, False)
        if w.kind == "main":
            _find_bars(root, w, wins)
        _visibility(root, snap.keyboard)
        roots.append((w, root))

    snap.roots = [root for _, root in roots]
    elems = []
    for w, root in roots:
        _elements(root, None, None, elems)
    elems = [e for e in elems if e.segments or e.node.actionable or e.node.is_list or e.node.tab_item
             or e.adopted or e.node.progress]
    for e in elems:
        _finish(e, W)
    elems = _dedupe(elems)
    _regions(elems, wins, W, H)
    _covered(elems)
    ordered = _order(elems)
    for i, e in enumerate(ordered, 1):
        e.ref = i
    _infer(ordered)
    _warn_overlaps(ordered, snap)
    snap.elements = ordered

    main = next((w for w in wins if w.kind == "main"), None)
    top = main or max((w for w in wins if w.kind in ("dialog", "popup")), key=lambda w: w.layer, default=None)
    _warn_tree(wins, top, snap)
    snap.pkg = top.pkg if top else (wins[0].pkg if wins else "")
    snap.title = top.title if top else ""
    snap.activity = activity or ""
    snap.sig = signature(snap)
    return snap


def _classify(wins, snap, system):
    area_scr = sp.area(snap.screen)
    for w in wins:
        if w.type == "input_method":
            w.kind = "ime"
            if w.rect and sp.area(w.rect) > 0:
                snap.keyboard = w.rect
    apps = [w for w in wins if w.type == "application" and w.raw.get("root") and w.rect]

    def popup(w):
        return w.title.startswith(("PopupWindow", "Pop-Up Window"))

    def floating(w):
        # Android 9 titles activity windows (the activity label) but not dialogs,
        # and a modal dialog is often the only window it lists at all
        # a window from rootInActiveWindow (`unlisted`, no bounds) has no title to go by
        untitled = not w.title and not w.raw.get("unlisted") and w.raw.get("bounds") is not None
        return popup(w) or untitled or sp.area(w.rect) < 0.7 * area_scr

    main = max((w for w in apps if not floating(w)), key=lambda w: (sp.area(w.rect), -w.layer), default=None)
    for w in apps:
        if w is main:
            w.kind = "main"
        elif floating(w) or w.layer > main.layer:
            w.kind = "popup" if popup(w) else "dialog"
            snap.dialog = True
        else:
            w.kind = "behind"
    for w in wins:
        if w.kind is not None:
            continue
        big = w.rect is not None and sp.area(w.rect) >= 0.5 * area_scr
        if w.type in ("accessibility_overlay", "magnification_overlay"):
            w.kind = "system" if system else "skip"
        elif big and w.raw.get("root") and (main is None or w.layer > main.layer):
            w.kind = "system"        # notification shade, keyguard: they *are* the screen
        else:
            w.kind = "system" if system else "skip"


def _make(raw, win, parent, index, clip, in_scroll):
    n = Node(raw, win, parent, index)
    n.clip = clip
    n.in_scroll = in_scroll
    coll = raw.get("collection") or {}
    rows, cols = coll.get("rows", 0), coll.get("cols", 0)
    big = n.rect is not None and win.rect is not None and sp.area(n.rect) >= 0.9 * sp.area(win.rect)
    # a SeekBar scrolls too (its value): ranges are never containers
    # ScrollViews that cannot scroll right now (content fits) are plain layout
    scrollish = ((n.scrollable or (n.cls in LIST_CLASSES and "Scroll" not in n.cls))
                 and not raw.get("range") and not n.cls.endswith("Spinner"))   # a Spinner "scrolls" its choices
    # a scrollable filling the window is usually a layout that merely claims to
    # scroll (Compose roots report ScrollView around their bars: Drive), unless
    # it holds a list's worth of content (an edge-to-edge ScrollView of rows)
    n.scroll_like = bool(scrollish and n.rect and (not big or coll or _list_like(raw)))
    n.tabstrip = rows == 1 and cols >= 2 and not n.scroll_like
    n.is_list = n.scroll_like or (bool(coll) and rows * cols >= 2 and not n.tabstrip)
    n.tab_item = bool(parent and raw.get("item") and any(a.tabstrip for a in [parent, *parent.ancestors()][:3]))
    child_clip = sp.inter(clip, n.rect) if (n.scroll_like and n.rect) else clip
    for i, c in enumerate(raw.get("children", ())):
        n.children.append(_make(c, win, n, i, child_clip, in_scroll or n.scroll_like))
    return n


def _list_like(raw):
    """Children that look like list content: 3+ sharing a resource-id, or at
    least 3 (and 30%) clipped out of view (Android reports them inverted)."""
    kids = raw.get("children", ())
    ids = {}
    for c in kids:
        if c.get("id"):
            ids[c["id"]] = ids.get(c["id"], 0) + 1
    if any(v >= 3 for v in ids.values()):
        return True
    gone = sum(1 for c in kids if sp.rect(c.get("bounds")) is None)
    return gone >= 3 and gone >= 0.3 * len(kids)


def _walk(n):
    yield n
    for c in n.children:
        yield from _walk(c)


def _find_bars(root, win, wins):
    """Fixed app bars: full-width, not scrolled, at the top or bottom of the content area."""
    wr = win.rect
    top, bottom = wr[1], wr[3]
    for o in wins:                           # status / navigation bars eat the edges
        if o is win or o.rect is None or o.layer <= win.layer or o.kind == "ime":
            continue
        if sp.width(o.rect) >= 0.9 * sp.width(wr):
            if o.rect[1] <= top < o.rect[3]:
                top = o.rect[3]
            if o.rect[1] < bottom <= o.rect[3]:
                bottom = o.rect[1]
    for n in _walk(root):
        if n.rect is None or n.in_scroll or n.scroll_like or n.raw.get("item") or n.raw.get("visible") is False:
            continue
        w, h = sp.width(n.rect), sp.height(n.rect)
        if h > 0.3 * sp.height(wr):
            continue
        by_class = n.cls in TOP_BAR_CLASSES or n.cls in BOTTOM_BAR_CLASSES
        if w < (0.8 if by_class else 0.95) * sp.width(wr):
            continue
        if not any(d.actionable or d.has_content() for d in _walk(n)):
            continue
        if _repeated(n):
            continue                     # one of many same-id siblings: a list row, not a bar
        if "top" not in win.bars and (n.rect[1] <= top + 8 or n.cls in TOP_BAR_CLASSES) \
                and n.rect[1] < wr[1] + 0.3 * sp.height(wr):
            win.bars["top"] = n
        elif "bottom" not in win.bars and (n.rect[3] >= bottom - 8 or n.cls in BOTTOM_BAR_CLASSES) \
                and n.rect[3] > wr[3] - 0.3 * sp.height(wr):
            win.bars["bottom"] = n


def _edge_sliver(n):
    """A thin node flush with its scroller's edge: Android pre-clips bounds to the
    viewport, so a button 5% scrolled into view arrives as a 13 px strip whose
    real size is unknown (TESTAPP `partial`). Too little of it shows to tap."""
    if not n.in_scroll or n.rect is None or n.clip is None:
        return False
    r, c = n.rect, n.clip
    return ((sp.height(r) <= SLIVER and (r[1] == c[1] or r[3] == c[3]))
            or (sp.width(r) <= SLIVER and (r[0] == c[0] or r[2] == c[2])))


def _under_keyboard(n, keyboard):
    """Android 9 reports a view hidden by the IME as visible:false; it is still
    there (ACTION_CLICK reaches it), so it is listed and flagged covered."""
    if keyboard is None or n.rect is None or n.raw.get("visible") is not False:
        return False
    if sp.inter(n.rect, n.win.rect) != n.rect or not (n.actionable or n.has_content()):
        return False
    return sp.area(sp.inter(n.rect, keyboard)) >= KB_COVER * sp.area(n.rect)


def _repeated(n):
    """Does ``n`` share its resource-id with 3+ siblings (rows of an edge-to-edge list)?"""
    rid = n.get("id")
    if not rid or n.parent is None:
        return False
    return sum(1 for c in n.parent.children if c.get("id") == rid) >= 3


def _visibility(root, keyboard=None):
    bars = list(root.win.bars.values())

    def visit(n):
        vr = sp.inter(n.rect, n.clip) if n.rect else None
        cuts = list(n.win.occluders)
        if n.in_scroll:
            cuts += [b.rect for b in bars if not b.is_ancestor_of(n) and b is not n]
        n.kb = _under_keyboard(n, keyboard)
        n.pieces = ([vr] if n.kb else sp.subtract_all(vr, cuts)) if vr else []
        n.vis = sp.largest(n.pieces)       # where a tap lands
        n.box = sp.bbox(n.pieces)          # what is shown as the element's bounds
        full = sp.area(n.rect)
        size = n.raw.get("size")          # the View's unclipped size (agent protocol 3), when clipped
        if size and len(size) == 2:
            full = max(full, size[0] * size[1])
        n.frac = sum(sp.area(p) for p in n.pieces) / full if full else 0.0
        sliver = (n.box is not None and min(sp.width(n.box), sp.height(n.box)) <= SLIVER
                  and min(sp.width(n.rect), sp.height(n.rect)) > SLIVER) or (not size and _edge_sliver(n))
        tiny = n.rect is not None and min(sp.width(n.rect), sp.height(n.rect)) < TINY
        ok = ((n.raw.get("visible") is not False or n.kb) and n.vis is not None
              and not sliver and not tiny)
        child_shown = False
        for c in n.children:
            visit(c)
            child_shown = child_shown or c.any_shown
        n.shown = ok and (n.frac >= MIN_FRAC or child_shown)
        n.any_shown = n.shown or child_shown

    visit(root)


def _elements(n, container, absorber, out):
    """Build elements depth-first. ``absorber`` is the nearest actionable element."""
    if not n.any_shown:
        return
    win_area = sp.area(n.win.rect) or 1
    if n.shown and n.is_list:
        el = Elem(n, container)
        out.append(el)
        n.el = el
        lab = n.get("desc") or n.get("pane")
        if lab:
            el.add_segment(lab)
        for c in n.children:
            _elements(c, el, None, out)
        return
    if n.shown and (n.actionable or n.tab_item):
        if absorber is not None and (
                (n.checkable and not n.clickable and not n.long_clickable and not n.editable)
                or (absorber.node.rect and n.rect and sp.near(absorber.node.rect, n.rect)
                    and not n.checkable)):
            # the same control seen twice: fold it into the absorbing element
            if n.checkable:
                absorber.adopted = n
            absorber.nodes.append(n)
            absorber.add_segment(n.own_label())
            n.el = absorber
            for c in n.children:
                _elements(c, container, absorber, out)
            return
        pass_through = (not n.own_label() and sp.area(n.rect) > 0.5 * win_area
                        and any(d is not n and (d.actionable or d.has_content()) for d in _walk(n)))
        if not pass_through:
            el = Elem(n, container)
            out.append(el)
            n.el = el
            el.add_segment(n.own_label())
            for c in n.children:
                _elements(c, container, el, out)
            return
        absorber = None
    elif n.shown and (n.has_content() or n.progress):
        if absorber is not None:
            absorber.add_segment(n.own_label())
            n.el = absorber
        else:
            el = Elem(n, container)
            out.append(el)
            n.el = el
            el.add_segment(n.own_label())
    for c in n.children:
        _elements(c, container, absorber, out)


# --------------------------------------------------------------------------
# roles and annotations
# --------------------------------------------------------------------------
def _class_role(cls):
    c = cls.split("$")[0]
    if c.endswith("EditText") or c in ("AutoCompleteTextView", "MultiAutoCompleteTextView"):
        return "input"
    if c.endswith("Switch") or c in ("SwitchCompat", "SwitchMaterial", "ToggleButton"):
        return "switch"
    if c.endswith("CheckBox"):
        return "checkbox"
    if c.endswith("RadioButton"):
        return "radio"
    if c == "CheckedTextView":
        return "option"
    if c.endswith("SeekBar") or c in ("Slider", "RatingBar"):
        return "seekbar"
    if c.endswith("ProgressBar"):
        return "progress"
    if c.endswith("Spinner"):
        return "dropdown"
    if c.endswith("WebView"):
        return "web"
    if c.endswith("TabView") or c == "TabWidget":
        return "tab"
    if c == "AppWidgetHostView":
        return "widget"
    return None


def _role(e, win_w):
    n = e.node
    if n.is_list:
        coll = n.raw.get("collection") or {}
        if coll.get("cols", 0) > 1 and coll.get("rows", 0) > 1:
            return "grid"
        if n.cls in ("ViewPager", "ViewPager2") or (coll.get("rows") == 1 and coll.get("cols", 0) > 1):
            return "pager"
        return "list" if (coll or n.cls in LIST_CLASSES - {"ScrollView", "NestedScrollView"}) else "scroll"
    if any(x.editable for x in e.nodes):
        return "input"
    if e.adopted is not None and not n.cls.endswith("Spinner"):   # a Spinner's selected item
        return _class_role(e.adopted.cls) or "toggle"
    r = _class_role(n.cls)
    if r:
        return r
    if n.checkable:
        return "toggle"
    if n.raw.get("range"):
        return "seekbar" if "set_progress" in n.actions else "progress"
    if n.tab_item:
        return "tab"
    if n.actionable:
        if n.cls.endswith("Button") or "Image" in n.cls or "Text" in n.cls:
            return "button"
        wide = n.box is not None and sp.width(n.box) >= 0.6 * win_w
        if (n.cls.endswith(CONTAINER_HINTS) or n.cls == "View") and e.segments and (len(e.segments) >= 2 or wide):
            return "row"
        return "button"
    if "heading" in n.flags:
        return "heading"
    if "Image" in n.cls:
        return "image"
    return "text"


def _fmt_num(x):
    return str(int(x)) if float(x).is_integer() else f"{x:.1f}"


def _finish(e, W):
    n = e.node
    e.role = _role(e, sp.width(n.win.rect) if n.win.rect else W)
    ann = []
    if e.role in ("list", "grid", "pager", "scroll"):
        e.list_info = _list_info(e)
        e.ann = ann
        return
    hint, text = n.get("hint"), n.get("text")
    if e.role == "input":
        edit = next((x for x in e.nodes if x.editable), n)
        hint, text = edit.get("hint"), edit.get("text")
        if hint and hint.lower() != e.label_full.lower():
            ann.append(f'hint="{_q(hint)}"')
        if "showingHint" in edit.flags or not text or (hint and text == hint):
            ann.append("empty")
        if "password" in edit.flags:
            ann.append("password")
        if "focused" in edit.flags:
            ann.append("focused")
    err = next((x.get("error") for x in e.nodes if x.get("error")), None)
    if err:
        ann.append(f'error="{_q(err)}"')
    st = n.get("state")
    if st and st.lower() not in e.label_full.lower():
        ann.append(f'state="{_q(st)}"')
    chk = e.adopted if e.adopted is not None else (n if n.checkable else None)
    if chk is not None:
        on = "checked" in chk.flags
        if e.role in ("switch", "toggle"):
            ann.append("on" if on else "off")
        elif e.role == "checkbox":
            ann.append("checked" if on else "unchecked")
        elif on:
            ann.append("checked")
    if "selected" in n.flags and e.role in ("tab", "button", "row", "option", "image", "text", "toggle"):
        ann.append("selected")
    if n.actionable and "enabled" not in n.flags:
        ann.append("disabled")
    if "heading" in n.flags and e.role != "heading":
        ann.append("heading")
    rng = n.raw.get("range")
    if rng:
        lo, hi, cur = rng.get("min", 0), rng.get("max", 0), rng.get("cur", 0)
        ann.append(f"range={_fmt_num(cur)}/{_fmt_num(hi)}" if lo == 0
                   else f"range={_fmt_num(cur)} ({_fmt_num(lo)}..{_fmt_num(hi)})")
    if n.long_clickable and not any(x.clickable for x in e.nodes) and e.role not in ("input",):
        ann.append("long-press")
    custom = []
    for x in e.nodes:
        for c in x.custom:
            if c not in custom:
                custom.append(c)
    if custom:
        ann.append("actions=[" + ", ".join(custom) + "]")
    e.ann = ann


def _q(s):
    return s.replace('"', '\\"')


def _list_info(e):
    """``8/25 more↓ (12%)`` for a scroll container."""
    n = e.node
    coll = n.raw.get("collection") or {}
    total = (coll.get("rows", 0) or 0) * max(1, coll.get("cols", 0) or 1) if coll.get("rows", 0) > 0 else None
    items = []
    for d in _walk(n):
        if d is n or not d.raw.get("item") or not d.shown:
            continue
        # only items of this collection, not of a nested one
        owner = next((a for a in d.ancestors() if a.raw.get("collection")), None)
        if owner is n:
            items.append(d.raw["item"])
    horizontal = (bool(n.actions & {"scroll_left", "scroll_right"})
                  or n.cls in ("HorizontalScrollView", "ViewPager", "ViewPager2")
                  or (coll.get("rows") == 1 and coll.get("cols", 0) > 1))
    back = bool(n.actions & {"scroll_backward", "scroll_up", "scroll_left"})
    fwd = bool(n.actions & {"scroll_forward", "scroll_down", "scroll_right"})
    arrows = ""
    if back or fwd:
        a, b = ("←", "→") if horizontal else ("↑", "↓")
        arrows = "more" + (a if back else "") + (b if fwd else "")
    parts = []
    if items:                 # no item info: don't pretend 0 are visible
        count = len(items)
        parts.append(f"{count}/{total}" if total else str(count))
        idx = [i.get("col", 0) if horizontal else i.get("row", 0) for i in items]
        first = min(idx)
        if total and first > 0 and (back or fwd):
            arrows += f" ({round(100 * first / total)}%)"
    if arrows:
        parts.append(arrows)
    return " ".join(parts)


# --------------------------------------------------------------------------
# dedupe, regions, cover, order, inference, warnings
# --------------------------------------------------------------------------
def _dedupe(elems):
    """Same text within 8 px: keep the actionable (or outer) one."""
    drop = set()
    for i, a in enumerate(elems):
        if id(a) in drop or not a.rect or not a.segments:
            continue
        for b in elems[i + 1:]:
            if id(b) in drop or not b.rect or b.node.win is not a.node.win:
                continue
            if a.label_full.lower() == b.label_full.lower() and sp.near(a.rect, b.rect):
                loser = b if (a.node.actionable or not b.node.actionable) else a
                drop.add(id(loser))
    return [e for e in elems if id(e) not in drop]


def _in_sheet(n):
    def sheety(a):
        sid = sp.short_id(a.get("id") or "").lower()
        return "BottomSheet" in a.cls or sid == "sheet" or "bottom_sheet" in sid or sid.endswith("_sheet")
    return any(sheety(a) for a in [n, *n.ancestors()])


def _in_fab(e):
    n = e.node
    idl = (n.get("id") or "").lower()
    if "FloatingActionButton" in n.cls:
        return True
    for x in [n, *list(n.ancestors())[:3]]:      # Compose FABs sit in an id'd host
        sid = sp.short_id((x.get("id") or "").lower())
        if "fab" in sid.split("_") or sid.startswith(("fab", "float")):
            return True
    return False


def _regions(elems, wins, W, H):
    by_win = {}
    for e in elems:
        by_win.setdefault(e.node.win, []).append(e)
    for w, es in by_win.items():
        if w.kind in ("dialog", "popup"):
            for e in es:
                e.region = "sheet" if _in_sheet(e.node) else w.kind
            continue
        if w.kind == "system":
            for e in es:
                e.region = "system"
            continue
        lists = [e for e in es if e.node.scroll_like and e.rect]
        main_list = max(lists, key=lambda e: sp.area(e.rect), default=None)
        for e in es:
            if e.container is not None:
                continue
            n, r = e.node, e.rect
            bar = next((k for k, b in w.bars.items() if b is n or b.is_ancestor_of(n)), None)
            # the drawer is the DrawerLayout child narrower than the screen. Not "index > 0":
            # an open drawer hides the main content from a11y, leaving the drawer at index 0
            drawer = next((a for a in n.ancestors() if a.parent is not None and a.parent.cls == "DrawerLayout"
                           and a.rect and sp.width(a.rect) < 0.95 * W), None)
            sheet = _in_sheet(n)
            if sheet:                      # a sheet docked at the bottom is not a bar
                e.region = "sheet"
            elif bar:
                e.region = f"{bar} bar"
            elif drawer is not None:
                e.region = "drawer"
            elif not n.in_scroll and _in_fab(e):
                e.region = "fab"
            elif main_list is not None and e is not main_list and not n.in_scroll and r:
                if r[3] <= main_list.rect[1] + 8 and r[1] < H / 2:
                    e.region = "top bar"
                elif r[1] >= main_list.rect[3] - 8 and r[3] > H / 2:
                    e.region = "bottom bar"
                else:
                    e.region = "content"
            else:
                e.region = "content"
        if main_list is None and w.kind == "main" and not w.bars:
            _position_bars([e for e in es if e.container is None and e.region == "content"], w, H)
        # list members follow their list
        for e in es:
            c = e.container
            while c is not None and c.container is not None:
                c = c.container
            if c is not None:
                e.region = c.region


def _position_bars(es, w, H):
    """No list and no bar containers: the first visual row, if it hugs the top
    and holds a control, is the top bar; the last row, if it hugs the bottom,
    holds a control and sits well apart from the content, is the bottom bar."""
    rows = sp.group_rows([e for e in es if e.rect and not e.node.in_scroll], key=lambda e: e.rect)
    if len(rows) < 3:
        return
    rows.sort(key=lambda row: min(e.rect[1] for e in row))
    top, bottom = w.rect[1], w.rect[3]
    for o in w.occluders:          # status / navigation bars
        if sp.width(o) >= 0.9 * sp.width(w.rect):
            if o[1] <= top < o[3]:
                top = o[3]
            if o[1] < bottom <= o[3]:
                bottom = o[1]
    first, last, prev = rows[0], rows[-1], rows[-2]
    if (len(first) >= 2 and min(e.rect[1] for e in first) <= top + 0.05 * H     # a lone widget
            and any(e.node.actionable for e in first)):                          # is not a bar
        for e in first:
            e.region = "top bar"
    gap = min(e.rect[1] for e in last) - max(e.rect[3] for e in prev)
    if (max(e.rect[3] for e in last) >= bottom - 0.05 * H and gap >= 0.15 * H
            and any(e.node.actionable for e in last)):
        for e in last:
            e.region = "bottom bar"


def _draws_above(b, a):
    """Is node ``b`` drawn above node ``a`` (same window, neither contains the other)?"""
    pa = [a, *a.ancestors()]
    pb = [b, *b.ancestors()]
    ida = {id(x) for x in pa}
    lca = next((x for x in pb if id(x) in ida), None)
    if lca is None:
        return False
    ca = pa[[id(x) for x in pa].index(id(lca)) - 1]
    cb = pb[[id(x) for x in pb].index(id(lca)) - 1]
    return (cb.raw.get("drawingOrder", 0), cb.index) > (ca.raw.get("drawingOrder", 0), ca.index)


def _covered(elems):
    """Hit-test with drawingOrder: a tap point under another control drawn above is covered."""
    hitters = [e for e in elems if e.rect and (e.node.clickable or e.node.long_clickable
                                              or e.node.editable or e.node.checkable)]
    for a in elems:
        if not a.rect:
            continue
        pieces = list(a.node.pieces)
        blocked = False
        for b in hitters:
            if b is a or b.node.win is not a.node.win or b.node.is_ancestor_of(a.node) \
                    or a.node.is_ancestor_of(b.node) or not sp.inter(a.rect, b.rect):
                continue
            if _draws_above(b.node, a.node):
                if sp.contains(b.rect, sp.center(a.rect)):
                    blocked = True
                pieces = [p for piece in pieces for p in sp.subtract(piece, b.rect)]
        best = sp.largest(pieces)
        if any(x.kb for x in a.nodes):
            blocked = True               # under the keyboard (ACTION_CLICK still reaches it)
        if blocked:
            a.covered = True
            if "covered" not in a.ann:
                a.ann.append("covered")
        a.tap = sp.center(best) if best else sp.center(a.rect)


def _order(elems):
    """Reading order: regions, then rows (top to bottom, left to right); a list is
    followed by its members."""
    top = [e for e in elems if e.container is None]

    def region_key(e):
        base = REGION_ORDER.index(e.region) if e.region in REGION_ORDER else len(REGION_ORDER)
        # stack several dialogs top-most first
        return (base, -e.node.win.layer if e.region in ("dialog", "popup") else 0)

    out = []

    def emit_level(items):
        lists = [e for e in items if e.children or e.node.is_list]
        plain = [e for e in items if e not in lists]
        rows = sp.group_rows([e for e in plain if e.rect], key=lambda e: e.rect)
        rows += [[e] for e in lists if e.rect]
        rows.sort(key=lambda row: min(e.rect[1] for e in row))
        for row in rows:
            for e in row:
                out.append(e)
                if e.children:
                    emit_level(e.children)

    groups = {}
    for e in top:
        groups.setdefault(region_key(e), []).append(e)
    for k in sorted(groups):
        emit_level(groups[k])
    return out


def _infer(elems):
    """Unlabeled controls: a label guessed from the id, plus the nearest text."""
    texts = [e for e in elems if e.segments and e.rect]
    for e in elems:
        if e.segments or e.role in ("list", "grid", "pager", "scroll", "progress") or not e.rect:
            continue
        if e.role == "input" and any(x.get("hint") for x in e.nodes):
            continue
        holder = next((a.el for a in e.node.ancestors() if a.el is not None and a.el is not e
                       and a.el.segments and not a.el.node.is_list), None)
        if holder is not None:
            e.context = ("in", holder.label)
            continue
        item = next((x.raw.get("item") for x in [e.node, *list(e.node.ancestors())[:2]] if x.raw.get("item")), None)
        if item is not None and e.container is not None and e.container.role == "grid":
            e.context = ("at", f"row {item.get('row', 0) + 1} col {item.get('col', 0) + 1}")
            continue
        e.inferred = sp.label_from_id(e.res_id)
        if e.region == "fab":
            continue                  # it floats over whatever scrolls by: no stable neighbour
        same = [t for t in texts if t.node.win is e.node.win and t is not e]
        for rel in ("left", "right", "above"):
            t = sp.nearest(same, e.rect, rel, key=lambda x: x.rect)
            if t is not None and sp.gap(t.rect, e.rect, rel) <= max(200, 2 * sp.height(e.rect)):
                e.context = ({"left": "right of", "right": "left of", "above": "below"}[rel], t.label)
                if e.role == "input":
                    e.inferred = None     # its visual label beats a guess from the id ("f email")
                break


def _warn_tree(wins, main, snap):
    """Say when the app's tree can't be trusted to show what is on screen."""
    if snap.degraded:
        part = (f", {snap.unread} subtree{'s' if snap.unread != 1 else ''} unread") if snap.unread else ""
        snap.warnings.append(f"degraded tree ({snap.degraded}{part}): the device returned what it could "
                             "read in time, so the screen may hold more than is listed. Refs here act on "
                             "exactly these elements; after the screen changes they can't be re-matched "
                             "until a complete read (try: wait, then snapshot)")
    for w in wins:
        if w.type == "application" and not w.raw.get("root") and (main is None or w.layer >= main.layer):
            snap.warnings.append(f"no tree for app window {w.title or w.pkg or w.id} "
                                 "(a slow or broken accessibility provider?)")
    if main is None:
        return
    mine = [e for e in snap.elements if e.node.win is main]
    if not mine:
        snap.warnings.append("opaque view: nothing here is labelled or actionable (still loading, "
                             "or a canvas/game/map?): try wait, shot --marks, or tap --point X,Y")
        return
    count = sum(1 for _ in _iter_raw(main.raw.get("root")))
    if count <= TINY_TREE and not any(e.node.actionable for e in mine):
        snap.warnings.append(f"tiny tree ({count} nodes): a splash or loading screen? try: wait")


def _iter_raw(n):
    if n:
        yield n
        for c in n.get("children", ()):
            yield from _iter_raw(c)


def _warn_overlaps(elems, snap):
    acts = [e for e in elems if e.rect and e.node.actionable and not e.node.is_list]
    for i, a in enumerate(acts):
        for b in acts[i + 1:]:
            if a.node.win is not b.node.win or a.node.is_ancestor_of(b.node) or b.node.is_ancestor_of(a.node):
                continue
            if sp.overlap_ratio(a.rect, b.rect) >= 0.5:
                snap.warnings.append(f"overlap [{a.ref}] [{b.ref}]")


# --------------------------------------------------------------------------
# signature
# --------------------------------------------------------------------------
def signature(snap):
    """Hash of package, activity/title and the app windows' skeleton (roles + ids).

    Excludes texts (a counter ticking is the same screen), system windows and the
    order windows are reported in; the skeleton is a *set*, so scrolling a list of
    same-shaped rows keeps the signature.
    """
    skel = sorted({(e.role, sp.short_id(e.res_id)) for e in snap.elements
                   if e.region not in ("system",)})
    titles = sorted({w.title for w in snap.windows if w.kind in ("main", "dialog", "popup")})
    blob = json.dumps([snap.pkg, snap.activity or snap.title, titles, skel], ensure_ascii=False)
    return hashlib.sha1(blob.encode()).hexdigest()[:4]


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
class Opts:
    """Display options. None of them changes refs."""

    def __init__(self, layout="spatial", regions=True, rows=True, grids=True, infer=True,
                 geo=False, map=False, bounds=False, max=MAX_DEFAULT, find=None, within=None):
        self.layout, self.regions, self.rows, self.grids, self.infer = layout, regions, rows, grids, infer
        self.geo, self.map, self.bounds, self.max, self.find, self.within = geo, map, bounds, max, find, within

    @property
    def spatial(self):
        return self.layout == "spatial"


def short_activity(activity):
    if not activity or "/" not in activity:
        return activity or ""
    pkg, cls = activity.split("/", 1)
    return f"{pkg}/{cls[len(pkg):]}" if cls.startswith(pkg + ".") else activity


def header(snap, opts=None):
    act = short_activity(snap.activity)
    where = act if act else f'{snap.pkg} "{snap.title}"' if snap.title else snap.pkg
    parts = [f"screen {where}", f"sig={snap.sig}",
             f"keyboard={'shown' if snap.keyboard else 'hidden'}",
             f"dialog={'yes' if snap.dialog else 'no'}"]
    if opts is not None and opts.spatial:
        parts.append(f"{snap.screen[2]}x{snap.screen[3]}")
    if snap.degraded:
        parts.append(f"degraded={snap.degraded}")
    if snap.toast:
        parts.append(f'toast="{_q(snap.toast[:80])}"')
    return "  ".join(parts)


def element_line(e, snap, opts, compact=False):
    """One element as text: ``[4] row "Ada · Lunch?"  actions=[Archive]``."""
    bits = [f"[{e.ref}]", e.role]
    if e.list_info:
        bits.append(e.list_info)
    if e.segments:
        bits.append(f'"{_q(e.label)}"')
    elif e.role not in ("list", "grid", "pager", "scroll", "progress"):   # a spinner needs no name
        if (opts is None or opts.infer) and e.inferred:
            bits.append(f"{e.inferred}?")
        if (opts is None or opts.infer) and e.context:
            bits.append(f'(unlabeled, {e.context[0]} "{_q(e.context[1][:40].rstrip(" ,·"))}")')
        elif not e.inferred and not (e.role == "input" and any(x.get("hint") for x in e.nodes)):
            bits.append("(unlabeled)")
    sid = sp.short_id(e.res_id)
    if sid and (e.role == "input" or not e.segments) and e.role not in ("list", "grid", "pager", "scroll"):
        bits.append(f"#{sid}")
    bits.extend(e.ann)
    if opts is not None and opts.geo and e.rect:
        bits.append(sp.geo(e.rect, snap.screen[2], snap.screen[3]))
    if opts is not None and opts.bounds and e.rect:
        bits.append("[{},{},{},{}]".format(*e.rect))
    return " ".join(bits) if compact else "  ".join([" ".join(bits[:2])] + bits[2:])


def _depth(e):
    d, c = 0, e.container
    while c is not None:
        d, c = d + 1, c.container
    return d


def flat_lines(snap, opts=None):
    """One element per line in ref order (the canonical form, used for diffs)."""
    return ["  " * _depth(e) + element_line(e, snap, opts) for e in snap.elements]


def _matches(e, needle):
    hay = [e.label_full, sp.short_id(e.res_id)] + [
        n.get(k) or "" for n in e.nodes for k in ("text", "desc", "hint", "error")]
    return any(needle in (h or "").lower() for h in hay)


def select(snap, opts):
    """The elements a display shows, after --in and --find (before --max)."""
    elems = snap.elements
    if opts.within is not None:
        root = next((e for e in elems if e.ref == opts.within), None)
        keep = set()

        def add(x):
            keep.add(id(x))
            for c in x.children:
                add(c)
        if root is not None:
            add(root)
        elems = [e for e in elems if id(e) in keep]
    if opts.find:
        elems = [e for e in elems if _matches(e, opts.find.lower())]
    return elems


def render(snap, opts):
    """The snapshot as text for the given display options."""
    lines = [header(snap, opts)]
    if opts.map:
        boxes = [(str(e.ref), e.rect) for e in snap.elements if e.rect and e.region != "system"]
        lines += sp.render_map(boxes, snap.screen[2], snap.screen[3])
    elems = select(snap, opts)
    shown = elems[:opts.max]
    if opts.find:
        lines += [element_line(e, snap, opts) for e in shown]
        if not elems:
            lines.append(f"(nothing matches {opts.find!r})")
    elif not opts.spatial:
        lines += ["  " * _depth(e) + element_line(e, snap, opts) for e in shown]
    else:
        lines += _spatial_lines(snap, shown, opts)
    if not snap.elements:
        lines.append("(no elements: nothing on screen is labelled or actionable)")
    if len(elems) > len(shown):
        lines.append(f"… {len(elems) - len(shown)} more elements (use --max N, --find TEXT or --in REF)")
    lines += [f"! {w}" for w in snap.warnings]
    return "\n".join(lines)


def _spatial_lines(snap, shown, opts):
    keep = {id(e) for e in shown}
    out = []
    region = None
    top = [e for e in shown if e.container is None or id(e.container) not in keep]
    i = 0
    # walk top-level elements in ref order, grouping consecutive ones per region
    while i < len(top):
        e = top[i]
        if opts.regions and e.region != region:
            region = e.region
            pkg = e.node.win.pkg
            extra = f" {pkg}" if region in ("dialog", "popup") and pkg and pkg != snap.pkg else ""
            out.append(f"-- {region}{extra}")
        j = i
        while j < len(top) and top[j].region == e.region:
            j += 1
        out += _level_lines(top[i:j], snap, opts, 0, keep)
        i = j
    if opts.regions and snap.keyboard:
        k = snap.keyboard
        out.append(f"-- keyboard (y {k[1]}-{k[3]}, covers {round(100 * sp.height(k) / snap.screen[3])}%)")
    return out


def _level_lines(items, snap, opts, depth, keep):
    ind = "  " * depth
    out = []
    lists = [e for e in items if e.children or e.node.is_list]
    plain = [e for e in items if e not in lists]
    if opts.rows:
        rows = sp.group_rows([e for e in plain if e.rect], key=lambda e: e.rect)
        rows += [[e] for e in plain if not e.rect]
    else:
        rows = [[e] for e in plain]
    rows += [[e] for e in lists]
    rows.sort(key=lambda row: min(e.ref for e in row))
    # grids: consecutive multi-element rows with aligned columns render as a table
    k = 0
    while k < len(rows):
        run = []
        while k + len(run) < len(rows) and len(rows[k + len(run)]) >= 2 and not any(
                x in lists for x in rows[k + len(run)]):
            run.append(rows[k + len(run)])
        cols = sp.grid_columns(run, key=lambda e: e.rect) if (opts.grids and len(run) >= 2) else None
        if cols:
            out += _grid_lines(run, cols, snap, opts, ind)
            k += len(run)
            continue
        row = rows[k]
        k += 1
        if len(row) == 1:
            e = row[0]
            out.append(ind + element_line(e, snap, opts, compact=True))
            if e.children:
                out += _level_lines([c for c in e.children if id(c) in keep], snap, opts, depth + 1, keep)
        else:
            out.append(ind + "   ".join(_row_part(e, row, snap, opts) for e in row))
    return out


def _row_part(e, row, snap, opts):
    """An element inside a row line; a label repeated from a neighbour is dropped
    (a Settings switch labelled like its row)."""
    line = element_line(e, snap, opts, compact=True)
    if e.segments and e.role in ("switch", "checkbox", "toggle", "radio"):
        others = " ".join(o.label_full.lower() for o in row if o is not e)
        if e.label_full.lower() in others:
            line = line.replace(f' "{_q(e.label)}"', "", 1)
    return line


def _grid_lines(run, cols, snap, opts, ind):
    roles = {e.role for row in run for e in row}
    shared = roles.pop() if len(roles) == 1 else None
    cells = []
    for row in run:
        line = ["."] * len(cols)
        for e in row:
            c = sp.column_of(e.rect, cols)
            if e.segments:
                body = f'"{_q(e.label[:24])}"'
            elif e.context and e.context[0] == "at":
                body = f"?({e.context[1]})"            # unlabeled: say where it is
            else:
                body = e.role if not shared else "?"
            txt = f"[{e.ref}]" + body
            extra = " ".join(a for a in e.ann if not a.startswith("actions="))
            if extra:
                txt += " " + extra
            if shared is None:
                txt = f"[{e.ref}]{e.role}" + txt[len(f"[{e.ref}]"):]
            line[c] = txt if line[c] == "." else line[c] + " " + txt
        cells.append(line)
    widths = [max(len(r[i]) for r in cells) for i in range(len(cols))]
    out = [ind + f"grid {len(run)}x{len(cols)}" + (f" of {shared}" if shared else "")]
    for r in cells:
        out.append(ind + "  " + "  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    return out


# --------------------------------------------------------------------------
# JSON, saved state, diff
# --------------------------------------------------------------------------
def element_json(e):
    return {
        "ref": e.ref, "role": e.role, "label": e.label_full or None,
        "inferred": e.inferred, "context": list(e.context) if e.context else None,
        "id": e.res_id, "class": e.node.raw.get("class"), "region": e.region,
        "bounds": list(e.rect) if e.rect else None, "tap": list(e.tap) if e.tap else None,
        "parent": e.container.ref if e.container else None,
        "annotations": list(e.ann), "list": e.list_info,
        "handle": e.node.raw.get("handle"), "window": e.node.win.id,
    }


def _path(n):
    """(nearest ancestor id, child-index steps from it) - a locator for the resolver."""
    steps = []
    x = n
    while x.parent is not None and not x.parent.get("id"):
        steps.append(x.index)
        x = x.parent
    if x.parent is None:
        return None, list(reversed(steps))
    steps.append(x.index)
    return x.parent.get("id"), list(reversed(steps))


CTX_MAX = 4          # context segments kept per ref
CTX_CHARS = 60


def _texts(n, out):
    """Labels in ``n``'s subtree (document order); list and scroll contents are
    skipped, since they change with every scroll."""
    if len(out) >= CTX_MAX or n.is_list or n.scroll_like:
        return
    lab = n.own_label() or n.get("hint")
    if lab and lab[:CTX_CHARS] not in out:
        out.append(lab[:CTX_CHARS])
    for c in n.children:
        _texts(c, out)


def _row_mates(node):
    """Labels of the node's visual row: siblings (at the first level that has
    any) whose vertical span overlaps its own. Android's accessibility tree
    drops unimportant layouts, so a "row" is often not a node at all; the
    geometry is what still says which "Delete" goes with which "Item 7"."""
    if node.rect is None:
        return []
    h = sp.height(node.rect)
    if node.win.rect is not None and h > 0.2 * sp.height(node.win.rect):
        return []
    child, anc = node, node.parent
    while anc is not None and len(anc.children) == 1 and not (anc.is_list or anc.scroll_like):
        child, anc = anc, anc.parent
    if anc is None:
        return []
    # inside a list, siblings are usually other rows (they don't overlap this
    # one vertically, so they're skipped below); when a flattened list holds a
    # row's pieces side by side ("Item 7" + its "Delete"), the overlap finds them
    out = []
    for c in anc.children:
        if c is child or c.rect is None or sp.height(c.rect) > 3 * h:
            continue
        if min(c.rect[3], node.rect[3]) - max(c.rect[1], node.rect[1]) > 0:
            _texts(c, out)
    return out


def context(node, own=()):
    """The words around a node, which the resolver uses to tell identical
    controls apart ("Delete" in the row "Item 7", the "+" of "Wireless Mouse")
    and to refuse a look-alike on a different screen.

    First its row-mates (see _row_mates). Failing that, the labels in its
    nearest ancestor that has any, other than its own subtree; that walk stops
    at a list or scroll container, so a list item never borrows its
    neighbours'."""
    own = {s.lower() for s in own}
    mates = [t for t in _row_mates(node) if t.lower() not in own]
    if mates:
        return mates[:CTX_MAX]
    child = node
    for anc in node.ancestors():
        # a list, a scroll view, or a ScrollView's single content holder: the
        # siblings from here on are other rows
        if anc.is_list or anc.scroll_like or (anc.parent is not None and anc.parent.scroll_like
                                               and len(anc.parent.children) == 1):
            return []
        # two or more clickable siblings shaped like us: a collection (Compose
        # lists are often plain containers), so the siblings are other rows
        if child.clickable and sum(1 for c in anc.children if c is not child and c.clickable
                                   and c.cls == child.cls) >= 2:
            return []
        out = []
        for c in anc.children:
            if c is not child:
                _texts(c, out)
        out = [t for t in out if t.lower() not in own]
        if out:
            return out[:CTX_MAX]
        child = anc
    return []


def click_node(e):
    """The node a tap should ACTION_CLICK: the element's own clickable node, a
    folded one, else its nearest clickable ancestor (a text inside a row)."""
    for n in e.nodes:
        if n.clickable:
            return n
    return next((a for a in e.node.ancestors() if a.clickable), None)


def ref_record(e, snap):
    """One ref's saved fingerprint (see to_state)."""
    n = e.node
    anchor, steps = _path(n)
    cn = click_node(e)
    return {
        "handle": n.raw.get("handle"), "dump": snap.dump, "window": n.win.id,
        "click": cn.raw.get("handle") if cn is not None else None,
        "role": e.role, "label": e.label_full, "text": n.get("text"), "desc": n.get("desc"),
        "hint": n.get("hint"), "id": e.res_id, "uid": n.raw.get("uid"), "vid": n.raw.get("vid"),
        "class": n.raw.get("class"), "path": [anchor, steps],
        "ctx": context(n, e.segments),
        "bounds": list(e.rect) if e.rect else None,
        "tap": list(e.tap) if e.tap else None, "region": e.region,
        "parent": e.container.ref if e.container else None,
    }


def to_state(snap, serial=None):
    """The saved form (``~/.droidctl/snaps/<serial>.json``), read by the resolver.

    ::

        {"version": 1, "serial", "created" (unix s), "dump", "gen", "sig",
         "pkg", "activity", "title", "screen": [0, 0, w, h], "keyboard": rect|null,
         "lines": [canonical flat lines],               # for --diff and `unchanged`
         "refs": {"<ref>": {
             "handle", "dump", "window",                # the fast path: act on the handle
             "click",                                   # handle to ACTION_CLICK (self/folded/ancestor)
             "role", "label", "text", "desc", "hint",   # the fingerprint
             "id", "uid", "vid", "class", "path": [anchor_id, [child indices]],
             "ctx": [labels around it],                 # see context()
             "bounds", "tap", "region", "parent"}},
         "evseq": last agent event seq already reported (toast header), or null}
    """
    refs = {str(e.ref): ref_record(e, snap) for e in snap.elements}
    return {"version": 1, "serial": serial, "created": round(time.time(), 3), "dump": snap.dump,
            "gen": snap.gen, "sig": snap.sig, "pkg": snap.pkg, "activity": snap.activity,
            "title": snap.title, "screen": list(snap.screen),
            "keyboard": list(snap.keyboard) if snap.keyboard else None,
            "lines": flat_lines(snap), "refs": refs, "evseq": snap.evseq}


TOAST_FRESH_MS = 3500   # Toast.LENGTH_LONG: a toast this recent may still be on screen


def pick_toast(events, prev_seq=None, now_ms=None):
    """The newest toast text among agent `events` that the caller hasn't reported yet:
    seq > prev_seq when a previous position is known, else fired within the last
    TOAST_FRESH_MS (device uptime `now_ms`). Pure."""
    best = None
    for e in events or ():
        if e.get("type") != "toast" or not e.get("text"):
            continue
        if prev_seq is not None:
            if (e.get("seq") or 0) <= prev_seq:
                continue
        elif "age_ms" in e:                      # the daemon's ring knows each event's age
            if e["age_ms"] > TOAST_FRESH_MS:
                continue
        elif now_ms is None or now_ms - (e.get("t") or 0) > TOAST_FRESH_MS:
            continue
        if best is None or (e.get("seq") or 0) > (best.get("seq") or 0):
            best = e
    return best["text"] if best else None


def snap_path(serial):
    from droidctl.device import home
    return os.path.join(home(), "snaps", f"{serial}.json")


def save_state(serial, state):
    path = snap_path(serial)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(path + ".tmp", path)
    return path


def load_state(serial):
    try:
        with open(snap_path(serial), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


_REF = re.compile(r"^(\s*)\[\d+\] ?")


def _body(line):
    return _REF.sub(r"\1", line).strip()


def diff(prev, snap):
    """Lines describing what changed since ``prev`` (a saved state): ``+`` added,
    ``-`` removed, ``~`` changed in place. Empty list: unchanged."""
    new = flat_lines(snap)
    old = prev.get("lines", [])
    old_b = [_body(x) for x in old]
    new_b = [_body(x) for x in new]
    pool = {}
    for i, b in enumerate(old_b):
        pool.setdefault(b, []).append(i)
    matched_old, unmatched_new = set(), []
    for j, b in enumerate(new_b):
        if pool.get(b):
            matched_old.add(pool[b].pop(0))
        else:
            unmatched_new.append(j)
    gone = [i for i in range(len(old)) if i not in matched_old]
    out = []
    # an element whose role and label survive but whose annotations moved is "changed"
    key = re.compile(r'^(\S+)(?:\s+("(?:[^"\\]|\\.)*"))?')
    used = set()
    for j in unmatched_new:
        kn = key.match(new_b[j])
        partner = None
        for i in gone:
            if i in used:
                continue
            ko = key.match(old_b[i])
            if kn and ko and kn.group(0) == ko.group(0):
                partner = i
                break
        if partner is not None:
            used.add(partner)
            out.append(f"~ {new[j].strip()}   (was: {old_b[partner]})")
        else:
            out.append(f"+ {new[j].strip()}")
    for i in gone:
        if i not in used:
            out.append(f"- {old_b[i]}")
    return out


# --------------------------------------------------------------------------
# where
# --------------------------------------------------------------------------
def where(state, ref):
    """Box, region and nearest neighbours of a saved ref (pure; reads a state dict)."""
    refs = state.get("refs", {})
    me = refs.get(str(ref))
    if me is None:
        return None
    w, h = state["screen"][2], state["screen"][3]
    box = tuple(me["bounds"]) if me.get("bounds") else None
    out = {"ref": ref, "role": me["role"], "label": me.get("label") or None, "region": me.get("region"),
           "parent": me.get("parent"), "bounds": list(box) if box else None, "tap": me.get("tap"),
           "neighbours": {}}
    if box is None:
        return out
    out["size"] = [sp.width(box), sp.height(box)]
    out["geo"] = sp.geo(box, w, h)
    others, inside = [], []
    for k, r in refs.items():
        if k == str(ref) or not r.get("bounds") or r.get("window") != me.get("window"):
            continue
        rb = tuple(r["bounds"])
        # what contains me (my row, my list) and what I contain are not neighbours
        i = sp.inter(rb, box)
        if i and sp.area(i) >= 0.9 * min(sp.area(rb), sp.area(box)):
            if sp.area(rb) > sp.area(box):
                inside.append((sp.area(rb), int(k)))
            continue
        others.append((int(k), rb, r))
    out["inside"] = [k for _, k in sorted(inside)]
    for d in ("left", "right", "above", "below"):
        hit = sp.nearest(others, box, d, key=lambda o: o[1])
        if hit is not None:
            out["neighbours"][d] = {"ref": hit[0], "role": hit[2]["role"], "label": hit[2].get("label") or None,
                                    "gap": sp.gap(hit[1], box, d)}
    return out
