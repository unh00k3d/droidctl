"""PURE: the iOS runner's `tree` result -> droidctl's raw tree (the schema snapshot.build reads).

The runner sends XCUIElementType numbers, frames in points, the runner's own handles and
`pid/elementID` uids; this module turns that into windows and nodes with Android class
names, flags and actions, so the unchanged snapshot/spatial/resolve layers work on it
(IOS.md §9). Rules R1-R8 were found on real trees (research/ios-decision.md §13-§14);
handles are passed through untouched so `act` can name the runner's element.
"""
import re

T = {1: "Other", 2: "Application", 3: "Group", 4: "Window", 7: "Alert", 8: "Dialog", 9: "Button", 12: "CheckBox",
     19: "Keyboard", 20: "Key", 21: "NavigationBar", 22: "TabBar", 24: "Toolbar", 25: "StatusBar", 26: "Table",
     32: "CollectionView", 33: "Slider", 34: "PageIndicator", 36: "ActivityIndicator", 37: "SegmentedControl",
     40: "Switch", 41: "Toggle", 42: "Link", 43: "Image", 44: "Icon", 45: "SearchField", 46: "ScrollView",
     47: "ScrollBar", 48: "StaticText", 49: "TextField", 50: "SecureTextField", 52: "TextView", 58: "WebView",
     75: "Cell"}
CLASS = {"Button": "android.widget.Button", "StaticText": "android.widget.TextView", "Switch": "android.widget.Switch",
         "Toggle": "android.widget.Switch", "CheckBox": "android.widget.CheckBox", "TextField": "android.widget.EditText",
         "SearchField": "android.widget.EditText", "SecureTextField": "android.widget.EditText",
         "TextView": "android.widget.EditText", "Image": "android.widget.ImageView", "Icon": "android.widget.ImageView",
         "Cell": "android.view.ViewGroup", "CollectionView": "androidx.recyclerview.widget.RecyclerView",
         "Table": "android.widget.ListView", "ScrollView": "android.widget.ScrollView", "Slider": "android.widget.SeekBar",
         "Link": "android.widget.Button", "Key": "android.widget.Button", "Alert": "android.view.ViewGroup",
         "TabBar": "android.widget.LinearLayout", "NavigationBar": "android.widget.Toolbar",
         "WebView": "android.webkit.WebView", "ActivityIndicator": "android.widget.ProgressBar", "Keyboard": "android.inputmethodservice.KeyboardView"}
CLICK = {"Button", "Cell", "Switch", "Toggle", "CheckBox", "Link", "TextField", "SearchField", "SecureTextField",
         "TextView", "Icon", "Key"}
SCROLL = {"CollectionView", "Table", "ScrollView", "WebView"}
EDIT = {"TextField", "SearchField", "SecureTextField", "TextView"}
# UIAccessibilityTraits bits (public), and UIKit's private back-button bit (navigation bars' back buttons)
TRAIT_BUTTON, TRAIT_LINK, TRAIT_HEADER, TRAIT_BACK = 1, 2, 0x10000, 0x8000000
# static text (public); text entry (private: every editable field and web input on iOS 18.7.8 has it)
TRAIT_STATIC_TEXT, TRAIT_TEXT_ENTRY = 0x40, 0x40000
SWITCHISH = re.compile(r"switch|toggle", re.IGNORECASE)
BACKISH = re.compile(r"\bback\b|arrow.?back|back.?arrow|\bgeri\b|chevron.?(left|backward)|arrow.?left|nav.?back",
                     re.IGNORECASE)
CHEVRON = re.compile(r"^(chevron(\.forward|\.right|\.down|\.up)?|forward|disclosure ?indicator)$", re.I)
SCROLLBAR = re.compile(r"(vertical|horizontal) scroll bar", re.I)
SCROLLBAR_PAGES = re.compile(r"vertical scroll bar, (\d+) pages", re.I)
SPRINGBOARD = "com.apple.springboard"
# a web landmark container's label is its role, optionally after its name: "banner", "Contents, navigation"
LANDMARK = re.compile(r"^(?:.+, )?(banner|navigation|main|complementary|contentinfo|search|form|region|note|article)$")


def rect(x):
    return x.get("frame") or [0, 0, 0, 0]


def overlaps(a, b):
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def walk(n):
    yield n
    for c in n.get("children") or []:
        yield from walk(c)


def tname(x):
    return T.get(x.get("type"), "Other")


def is_decoration(x):
    """R3: disclosure arrows, idle spinners and scroll bars carry nothing to act on."""
    lab = (x.get("label") or "").strip()
    t = tname(x)
    if t in ("Button", "Image") and CHEVRON.match(lab):
        return True
    if t == "ActivityIndicator" and (lab == "Progress halted" or x.get("value") in ("0", None)):
        return True
    if t in ("Other", "ScrollBar") and SCROLLBAR.search(lab):
        return True
    return False


def one_web_view(x, inside=False, web_frame=None):
    """A WKWebView reports itself, its UIScrollView and its content view all as WebView (58):
    only the outermost one is the web view; the inner ones are plain layout. Inside it, the page's
    own root (an unnamed-type node labelled with the page title, as big as the page) is layout
    too: as an element it spanned every row, and the layout put the whole page on one line
    (Safari, 2026-10-22)."""
    web = tname(x) == "WebView"
    y = dict(x)
    if web and inside:
        y["type"] = 1
    if web and not inside:
        web_frame = rect(x)
    elif inside and web_frame and tname(x) == "Other" and x.get("label"):
        fx, fy, fw, fh = rect(x)
        if fw >= 0.9 * web_frame[2] and fh >= 0.9 * web_frame[3]:
            y["label"] = None
        elif LANDMARK.match(x["label"].strip()):
            y["label"] = None              # a landmark is a container, not something to read
    y["children"] = [one_web_view(c, inside or web, web_frame) for c in x.get("children") or []]
    return y


def slider_range(value):
    """A UISlider's value is a localized percentage ("50%", "%50" in Turkish). PURE."""
    m = re.search(r"-?\d+(?:[.,]\d+)?", value or "")
    if not m:
        return None
    return {"min": 0, "max": 100, "cur": float(m.group(0).replace(",", "."))}


def _title_collapsed(b):
    """A button whose only content is its title text, laid out zero wide (an app's hidden Cancel button,
    full-cell frame and all, 2026-10-26)."""
    kids = b.get("children") or []
    # labelled, zero wide but full height: Safari's Page Menu and the keyboard's Emoji key hold an empty
    # 0x0 child, and they are on screen
    return bool(kids) and all(tname(k) == "StaticText" and k.get("label") and k["label"] == b.get("label")
                              and rect(k)[2] == 0 and rect(k)[3] > 0 for k in kids)


def prune(x, cell=None):
    """R1: inside a cell, a descendant entirely outside the cell's own frame was never laid out
    (off-screen rows whose children all report the list's top-left corner). R3: decorations."""
    kids, pages = [], 1
    for c in x.get("children") or []:
        if is_decoration(c):
            m = SCROLLBAR_PAGES.search(c.get("label") or "")
            if m:                              # the bar says how far it scrolls: keep that
                pages = max(pages, int(m.group(1)))
            continue
        if cell is not None and rect(c)[2] > 0 and not overlaps(rect(c), rect(cell)):
            continue
        if tname(c) == "Button" and _title_collapsed(c):
            continue                           # R11: its title was laid out at zero width: not on screen
        kids.append(prune(c, c if tname(c) == "Cell" else cell))
    y = dict(x)
    if tname(x) == "Button" and x.get("value") in ("0", "1") and SWITCHISH.search(x.get("id") or ""):
        y["type"] = 40                     # R12: an app's switch exposed as a button with a 0/1 value
    traits = x.get("traits") or 0
    if tname(x) == "TextView" and traits & TRAIT_STATIC_TEXT and not traits & TRAIT_TEXT_ENTRY:
        y["type"] = 48                     # R13: a read-only text view is text, not an input (2026-10-26)
    y["children"] = kids
    if pages > 1:
        y["pages"] = pages
    return y


class Mapper:
    def __init__(self, pkg):
        self.pkg = pkg

    def node(self, x, parent_cell=None):
        t = tname(x)
        rx, ry, rw, rh = rect(x)
        label, ident, value = x.get("label"), x.get("id"), x.get("value")
        traits = x.get("traits") or 0
        n = {"handle": x["handle"], "class": CLASS.get(t, "android.view.View"), "bounds": [rx, ry, rx + rw, ry + rh]}
        if x.get("uid"):
            n["uid"] = x["uid"]
        flags = [] if x.get("enabled") is False else ["enabled"]
        acts = []
        kids = x.get("children") or []
        if t == "StaticText":
            n["text"] = label or value
        elif t in EDIT:
            if value and value != x.get("placeholder"):
                n["text"] = value
            n["hint"] = x.get("placeholder") or label
            flags += ["editable", "focusable"] + (["password"] if t == "SecureTextField" else [])
            flags += ["focused"] if x.get("focused") else []
            acts += ["set_text"] + ([] if x.get("focused") else ["focus"])
        elif label and t not in ("NavigationBar", "TabBar", "Toolbar"):
            n["desc"] = label.strip()          # bars are containers: "Toolbar" is not something to read
        if ident and ident != label:
            n["id"] = ident
        clickable = t in CLICK or (t == "Other" and traits & (TRAIT_BUTTON | TRAIT_LINK) and (label or ident)) or \
            bool(x.get("_touch_target"))
        if traits & TRAIT_HEADER:
            flags.append("heading")
        if t == "Button" and (traits & TRAIT_BACK or ident == "BackButton" or x.get("_nav_back")):
            n["nav_back"] = True               # `droidctl back` taps it: iOS has no system back
        if t == "NavigationBar":
            # a custom back button (a bar item with an image named "arrowBack", 2026-10-26): the leading button
            # of a navigation bar, named back-like. UIKit's own carries TRAIT_BACK; Safari's has an id.
            lead = [d for d in list(walk(x))[1:] if tname(d) == "Button" and rect(d)[0] < rx + rw / 4]
            first = min(lead, key=lambda d: rect(d)[0], default=None)
            if first is not None and BACKISH.search(f"{first.get('label') or ''} {first.get('id') or ''}"):
                first["_nav_back"] = True
        if t == "Cell":
            desc = list(walk(x))[1:]
            # R6: a cell holding a switch: the switch is the control; the cell is only its row
            # R7: a cell labelled with its selected segment's title is just the segment strip
            sel = [d for d in desc if tname(d) == "Button" and d.get("selected") and d.get("label")]
            if any(tname(d) in ("Switch", "Toggle") for d in desc) or \
                    (sel and (label or "").strip() == sel[0]["label"].strip()):
                clickable = False
                n.pop("desc", None)
            # R9: a cell labelled with its texts joined by ", " (cards side by side, a stock row with buy and
            # sell price boxes, a tab strip) is several tap targets an app put in one element: its centre is
            # one of them by position (2026-10-26: "All Markets" opened "Dividend List"). Each text is a target,
            # touched where it is; the cell is only their container.
            # R10: an unlabelled cell holding cells or controls of its own (an app's section wrappers) is
            # only their container: as a target it is a blank button around real ones
            if clickable and not (label or "").strip() and any(
                    tname(d) in CLICK and d.get("class") != "_UITableCellAccessoryButton" for d in desc):
                clickable = False
            texts = composite_texts(x)
            if clickable and texts:
                clickable = False
                if composite_complete(x, texts):
                    n.pop("desc", None)        # all said by the texts; else the rest stays readable here
                for d in texts:
                    d["_touch_target"] = True
        if t in ("Switch", "Toggle"):
            # R8: a labelled switch wrapping an unlabelled inner switch (SwiftUI Toggle, UIKit Settings rows)
            # is one control. Keep the outer one for reading; a touch must land on the inner one (the outer
            # one's centre is the label and does nothing).
            inner = next((d for d in list(walk(x))[1:] if tname(d) in ("Switch", "Toggle") and not d.get("label")), None)
            if inner is not None:
                kids = [k for k in kids if k is not inner]
                n["touch"] = inner["handle"]
                ir = rect(inner)
                n["touch_rect"] = [ir[0], ir[1], ir[0] + ir[2], ir[1] + ir[3]]
            flags.append("checkable")
            if value == "1":
                flags.append("checked")
            n["desc"] = (label or "").strip()
            if parent_cell is not None:   # R6: named by the row's title label, not the "Title State" composite
                tl = next((d.get("label") for d in walk(parent_cell) if tname(d) == "StaticText" and d.get("label")), None)
                if tl:
                    tl = tl.strip()
                    if label and tl.endswith(" " + label.strip()):
                        tl = tl[:-len(label.strip()) - 1]   # "Debug Off": the title says the state too
                    n["desc"] = tl
        if clickable:
            flags.append("clickable")
            acts.append("click")
        if x.get("selected"):
            flags.append("selected")
        if t == "Slider":
            acts += ["scroll_forward", "scroll_backward"]   # increment / decrement (AX 2004/2005)
            rng = slider_range(value)
            if rng:
                n["range"] = rng
            n["desc"] = (label or "").strip() or None
        mkids = []
        if t in SCROLL:
            flags.append("scrollable")
            cells = [c for c in kids if tname(c) == "Cell"]
            if cells:
                # R2: a table/collection gets rows, and scroll actions only towards rows really hidden
                n["collection"] = {"rows": len(cells), "cols": 1}
                top, bot = ry, ry + rh
                if any(rect(c)[1] + rect(c)[3] <= top + 1 for c in cells):
                    acts.append("scroll_backward")
                if any(rect(c)[1] >= bot - 1 for c in cells):
                    acts.append("scroll_forward")
            elif _overflows(x):
                acts += ["scroll_forward", "scroll_backward"]
                if _pages(x) > 1:
                    n["scrolls"] = True        # proof it scrolls, even filling the screen (snapshot._make)
            else:
                flags.remove("scrollable")         # its content fits: plain layout (alert parts, web views)
            for c in kids:
                m = self.node(c, c if tname(c) == "Cell" else parent_cell)
                if tname(c) == "Cell":
                    m["item"] = {"row": cells.index(c), "col": 0}
                mkids.append(m)
        else:
            sw_names = set()
            if t == "Cell" and any(tname(d) in ("Switch", "Toggle") for d in walk(x)):
                # R6: the title label that names a switch in this row is consumed by the switch
                tl = next((d.get("label") for d in walk(x) if tname(d) == "StaticText" and d.get("label")), None)
                if tl:
                    sw_names.add(tl.strip())
            for c in kids:
                if tname(c) == "StaticText" and (c.get("label") or "").strip() in sw_names:
                    continue
                if t == "Cell" and tname(c) == "Button" and not c.get("label") and \
                        rect(c)[2] * rect(c)[3] >= 0.6 * rw * rh:
                    # R5: an unlabelled button filling most of a labelled cell is the cell's own tap target
                    for cc in c.get("children") or []:
                        mkids.append(self.node(cc, x))
                    continue
                mkids.append(self.node(c, x if t == "Cell" else parent_cell))
        n["flags"], n["actions"] = flags, acts
        if mkids:
            n["children"] = mkids
        return n


def composite_texts(cell):
    """The texts (two or more) of a cell whose label joins them by ", ", else []. Some parts may have no
    text of their own (an app's rate cards expose the second row only)."""
    label = (cell.get("label") or "").strip()
    texts = [d for d in list(walk(cell))[1:] if tname(d) == "StaticText" and (d.get("label") or "").strip()
             and rect(d)[2] > 0 and rect(d)[3] > 0]
    if not label:
        # an unlabelled strip of three or more texts side by side in one band (a tab strip); two are a
        # row's title and value ("Server Type · prep"), one target
        if len(texts) < 3:
            return []
        rs = sorted((rect(d) for d in texts), key=lambda r: r[0])
        one_band = max(r[1] for r in rs) < min(r[1] + r[3] for r in rs)
        apart = all(a[0] + a[2] <= b[0] + 1 for a, b in zip(rs, rs[1:]))
        return texts if one_band and apart else []
    rs = sorted((rect(d) for d in texts), key=lambda r: r[0])
    if len(texts) < 2 or not any(a[0] + a[2] <= b[0] + 1 for a, b in zip(rs, rs[1:])):
        return []                      # a title over its subtitle (a card) is one target
    # each text a whole ", "-separated segment (a text may hold ", " itself: "Professional, Scientific and T")
    joined = f", {label}, "
    return texts if all(f", {d['label'].strip()}, " in joined for d in texts) else []


def composite_complete(cell, texts):
    """The texts say all of the cell's label."""
    if not (cell.get("label") or "").strip():
        return True
    rest = f", {(cell.get('label') or '').strip()}, "
    for d in sorted(texts, key=lambda d: -len(d["label"])):
        rest = rest.replace(f", {d['label'].strip()}, ", ", ", 1)
    return rest.strip(", ") == ""


def _overflows(x):
    """iOS doesn't say whether a ScrollView can scroll: it can when some content lies outside it, or
    when its scroll bar (dropped as decoration) said "2 pages" or more: a web page lists only part
    of its content (Safari on Wikipedia, 2026-10-22)."""
    if _pages(x) > 1:
        return True
    fx, fy, fw, fh = rect(x)
    for k in list(walk(x))[1:]:
        kx, ky, kw, kh = rect(k)
        if kw <= 0 or kh <= 0:
            continue
        if kx < fx - 1 or ky < fy - 1 or kx + kw > fx + fw + 1 or ky + kh > fy + fh + 1:
            return True
    return False


def _pages(x):
    """The page count a scroll bar gave for this scroller: its own, or a plain-layout descendant's
    (a WKWebView's inner scroll view), never a nested scroller's. PURE."""
    best, stack = x.get("pages", 1), list(x.get("children") or [])
    while stack:
        d = stack.pop()
        if tname(d) in SCROLL:
            continue
        best = max(best, d.get("pages", 1))
        stack.extend(d.get("children") or [])
    return best


def _alert_box(w):
    """The frame of an alert (UIAlertController, SpringBoard alerts) in this window, or None."""
    a = next((k for k in walk(w) if tname(k) == "Alert"), None)
    if a is None:
        return None
    x, y, ww, hh = rect(a)
    return [x, y, x + ww, y + hh]


def _sheet_box(w, W, H):
    """The frame of a view presented over a backdrop, when it doesn't fill the screen, else None: UIKit's
    dimming view (a page or form sheet, a popover), or an app's empty full-screen view under a floating card.
    The screen behind it is out of the tree, as behind an alert."""
    def backdrop(c):                          # UIKit's own, or an app's: an empty view over the whole screen
        if c.get("class") == "UIDimmingView":
            return "uikit"
        r = rect(c)
        if tname(c) == "Other" and not c.get("children") and not c.get("label") and not c.get("id") and \
                r[0] <= 0 and r[1] <= 0 and r[0] + r[2] >= W and r[1] + r[3] >= H:
            return "app"
        return None

    for k in walk(w):
        kids = k.get("children") or []
        i = next((i for i, c in enumerate(kids) if backdrop(c)), None)
        if i is None:
            continue
        kind = backdrop(kids[i])
        boxes = [rect(c) for c in kids[i + 1:] if rect(c)[2] > 0 and rect(c)[3] > 0]
        if not boxes:
            return None
        box = [min(b[0] for b in boxes), min(b[1] for b in boxes),
               max(b[0] + b[2] for b in boxes), max(b[1] + b[3] for b in boxes)]
        box = [max(0, box[0]), max(0, box[1]), min(W, box[2]), min(H, box[3])]
        if box == [0, 0, W, H]:
            return None
        # an app's own pop-up card (2026-10-26, a real app) floats clear of the top and the bottom; a screen
        # laid over a plain background view reaches one of them
        if kind == "app" and not (box[1] > 0.1 * H and box[3] < 0.9 * H):
            return None
        return box
    return None


def _has(w, *types):
    return any(tname(k) in types for k in walk(w))


def _content_box(w, W, H):
    boxes = [rect(k) for k in walk(w) if (k.get("label") or k.get("id")) and rect(k)[2] > 0 and rect(k)[3] > 0
             and overlaps(rect(k), [0, 0, W, H])]
    if not boxes:
        return None
    return [max(0, min(b[0] for b in boxes)), max(0, min(b[1] for b in boxes)),
            min(W, max(b[0] + b[2] for b in boxes)), min(H, max(b[1] + b[3] for b in boxes))]


def convert(result):
    """The runner's `tree` result -> {screen, dump, gen, windows, ...} as the Android agent sends it."""
    scr = result.get("screen") or {}
    W, H = scr.get("w") or 390, scr.get("h") or 844
    out = {"dump": result.get("dump"), "gen": result.get("gen"), "ms": result.get("ms"),
           "nodes": result.get("nodes"), "degraded": False,
           "screen": {"w": W, "h": H, "density": 160 * (scr.get("scale") or 1), "rotation": scr.get("rotation", 0),
                      "on": scr.get("on", True), "locked": scr.get("locked", False)},
           "windows": []}
    if result.get("front"):
        out["front"] = result["front"]
    layer = 0
    apps = result.get("apps") or []
    for app in apps:
        bundle = app.get("bundle")
        root = app.get("root") or {}
        m = Mapper(bundle)
        wins = [c for c in root.get("children") or [] if tname(c) == "Window"] or [root]
        springboard = bundle == SPRINGBOARD
        main_done = False
        for w in wins:
            native = w                         # before pruning: a dimming view is empty, so pruned
            w = prune(one_web_view(w))
            if not any(k.get("label") or k.get("id") or k.get("value") for k in walk(w)):
                continue                       # empty overlay windows
            if rect(w)[2] > W * 1.5:
                continue                       # SpringBoard windows in pixel units
            box = _content_box(w, W, H)
            if box is None:
                continue
            kind = "application"
            if _has(w, "Keyboard"):
                kind = "input_method"
            elif springboard and not _has(w, "Alert"):
                # behind an app; or, on the Home screen, every window after the main one (the status bar)
                kind = "system" if main_done or any(a.get("bundle") != SPRINGBOARD for a in apps) else "application"
            sheet = None if _has(w, "Alert") or kind != "application" else _sheet_box(native, W, H)
            alert = _alert_box(w) or sheet
            if alert:
                bounds = alert                 # an alert is a dialog: its own frame, never the screen
            elif kind == "application" and not main_done:
                bounds = [0, 0, W, H]          # R4: the main window is the screen; later ones get a content box
                main_done = True
            else:
                bounds = box
            layer += 1
            out["windows"].append({"id": layer, "type": kind, "layer": layer, "pkg": bundle,
                                   "title": root.get("label"), "bounds": bounds, "root": m.node(w)})
            if sheet:
                out["windows"][-1]["modal"] = True
    out["windows"].reverse()                   # top of the z-order first, as getWindows() lists them
    return out
