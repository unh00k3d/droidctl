"""Prototype of droidctl/ios/mapping.py: runner tree (XCUIElementType ints, points) -> droidctl raw tree. PURE.
Rules beyond the throwaway runner2raw.py are marked R1..R7 (see research/ios-decision.md §14)."""
import re

T = {1: "Other", 2: "Application", 3: "Group", 4: "Window", 7: "Alert", 8: "Dialog", 9: "Button", 12: "CheckBox", 19: "Keyboard",
     20: "Key", 21: "NavigationBar", 22: "TabBar", 24: "Toolbar", 25: "StatusBar", 26: "Table", 32: "CollectionView", 33: "Slider",
     34: "PageIndicator", 36: "ActivityIndicator", 37: "SegmentedControl", 40: "Switch", 41: "Toggle", 42: "Link", 43: "Image",
     44: "Icon", 45: "SearchField", 46: "ScrollView", 47: "ScrollBar", 48: "StaticText", 49: "TextField", 50: "SecureTextField",
     52: "TextView", 58: "WebView", 75: "Cell"}
CLASS = {"Button": "android.widget.Button", "StaticText": "android.widget.TextView", "Switch": "android.widget.Switch",
         "Toggle": "android.widget.Switch", "CheckBox": "android.widget.CheckBox", "TextField": "android.widget.EditText",
         "SearchField": "android.widget.EditText", "SecureTextField": "android.widget.EditText", "TextView": "android.widget.EditText",
         "Image": "android.widget.ImageView", "Icon": "android.widget.ImageView", "Cell": "android.view.ViewGroup",
         "CollectionView": "androidx.recyclerview.widget.RecyclerView", "Table": "android.widget.ListView",
         "ScrollView": "android.widget.ScrollView", "Slider": "android.widget.SeekBar", "Link": "android.widget.Button",
         "Key": "android.widget.Button", "Alert": "android.view.ViewGroup", "TabBar": "android.widget.LinearLayout",
         "NavigationBar": "android.widget.Toolbar", "WebView": "android.webkit.WebView"}
CLICK = {"Button", "Cell", "Switch", "Toggle", "CheckBox", "Link", "TextField", "SearchField", "SecureTextField", "TextView", "Icon", "Key"}
SCROLL = {"CollectionView", "Table", "ScrollView", "WebView"}
EDIT = {"TextField", "SearchField", "SecureTextField", "TextView"}
CHEVRON = re.compile(r"^(chevron(\.forward|\.right|\.down|\.up)?|forward|disclosure ?indicator)$", re.I)
SCROLLBAR = re.compile(r"(vertical|horizontal) scroll bar", re.I)

def rect(x): return x["rect"]
def inside(a, b, slack=1):  # a within b
    return a[0] >= b[0] - slack and a[1] >= b[1] - slack and a[0] + a[2] <= b[0] + b[2] + slack and a[1] + a[3] <= b[1] + b[3] + slack
def overlaps(a, b): return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]
def walk(n):
    yield n
    for c in n.get("children") or []: yield from walk(c)
def tname(x): return T.get(x["type"], "Other")

def is_decoration(x):
    lab = (x.get("label") or "").strip()
    t = tname(x)
    if t in ("Button", "Image") and CHEVRON.match(lab): return True                        # R3: disclosure arrows
    if t == "ActivityIndicator" and (lab == "Progress halted" or x.get("value") in ("0", None)): return True  # R3: idle spinners
    if t in ("Other", "ScrollBar") and SCROLLBAR.search(lab): return True                  # R3: scroll bars (hint comes from R2)
    return False

def prune(x, cell=None):
    """R1: inside a cell, a descendant entirely outside the cell's own frame was never laid out (seen: off-screen
    rows whose children all report the list's top-left corner). R3: decorations."""
    kids = []
    for c in x.get("children") or []:
        if is_decoration(c): continue
        if cell is not None and rect(c)[2] > 0 and not overlaps(rect(c), rect(cell)): continue
        kids.append(prune(c, c if tname(c) == "Cell" else cell))
    y = dict(x); y["children"] = kids
    return y

class Mapper:
    def __init__(self, screen):
        self.h = 0; self.W, self.H = screen
    def node(self, x, parent_cell=None):
        self.h += 1
        t = tname(x)
        rx, ry, rw, rh = rect(x)
        label, ident, value = x.get("label"), x.get("id"), x.get("value")
        n = {"handle": self.h, "class": CLASS.get(t, "android.view.View"), "pkg": "ios", "bounds": [rx, ry, rx + rw, ry + rh]}
        if x.get("ax"): n["uid"] = x["ax"]
        flags = [] if x.get("disabled") else ["enabled"]
        acts = []
        kids = x.get("children") or []
        if t == "StaticText":
            n["text"] = label or value
        elif t in EDIT:
            if value and value != x.get("ph"): n["text"] = value
            n["hint"] = x.get("ph") or label
            flags += ["editable", "focusable"] + (["password"] if t == "SecureTextField" else [])
        elif label:
            n["desc"] = label.strip()
        if ident and ident != label: n["id"] = ident
        clickable = t in CLICK
        if t == "Cell":
            desc = list(walk(x))[1:]
            # R5: an unlabelled button that fills most of a labelled cell is the cell's own tap target
            # R6: a cell holding a switch: the switch is the control; the cell is only its row
            # R7: a cell labelled with its selected segment's title is just the segment strip
            sel = [d for d in desc if tname(d) == "Button" and d.get("selected") and d.get("label")]
            if any(tname(d) in ("Switch", "Toggle") for d in desc) or (sel and (label or "").strip() == sel[0]["label"].strip()):
                clickable = False; n.pop("desc", None)
        if t in ("Switch", "Toggle"):
            # R8: a labelled switch wrapping an unlabelled inner switch (SwiftUI Toggle, UIKit Settings rows) is one
            # control. Keep the outer one for reading; the inner one is where a touch must land (tapping the outer
            # centre hits the label and does nothing, S0).
            inner = next((d for d in list(walk(x))[1:] if tname(d) in ("Switch", "Toggle") and not d.get("label")), None)
            if inner is not None:
                kids = [k for k in kids if k is not inner]
                n["touch"] = inner.get("ax"); ir = rect(inner); n["touch_rect"] = [ir[0], ir[1], ir[0] + ir[2], ir[1] + ir[3]]
            flags.append("checkable")
            if value == "1": flags.append("checked")
            n["desc"] = (label or "").strip()
            title = next((d.get("label") for d in kids if tname(d) == "StaticText"), None)
            if parent_cell is not None:   # R6: name it by the row's title label, not the "Title State" composite
                tl = next((d.get("label") for d in walk(parent_cell) if tname(d) == "StaticText" and d.get("label")), None)
                if tl: n["desc"] = tl.strip()
        if clickable: flags.append("clickable"); acts.append("click")
        if x.get("selected"): flags.append("selected")
        if x.get("focused"): flags.append("focused")
        mkids = []
        if t in SCROLL:
            flags.append("scrollable")
            cells = [c for c in kids if tname(c) == "Cell"]
            if cells:
                n["collection"] = {"rows": len(cells), "cols": 1}
                top, bot = ry, ry + rh
                if any(rect(c)[1] + rect(c)[3] <= top + 1 for c in cells): acts.append("scroll_backward")
                if any(rect(c)[1] >= bot - 1 for c in cells): acts.append("scroll_forward")
            else:
                acts += ["scroll_forward", "scroll_backward"]
            for i, c in enumerate(kids):
                m = self.node(c, c if tname(c) == "Cell" else parent_cell)
                if tname(c) == "Cell": m["item"] = {"row": cells.index(c), "col": 0}
                mkids.append(m)
        else:
            sw_names = set()
            if t == "Cell":   # R6: the title label that names a switch in this row is consumed by the switch
                if any(tname(d) in ("Switch", "Toggle") for d in walk(x)):
                    tl = next((d.get("label") for d in walk(x) if tname(d) == "StaticText" and d.get("label")), None)
                    if tl: sw_names.add(tl.strip())
            for c in kids:
                if tname(c) == "StaticText" and (c.get("label") or "").strip() in sw_names: continue
                if t == "Cell" and tname(c) == "Button" and not c.get("label") and rect(c)[2] * rect(c)[3] >= 0.6 * rw * rh:
                    # R5: absorb the button's children (if any) but not the button itself
                    for cc in c.get("children") or []: mkids.append(self.node(cc, x))
                    continue
                mkids.append(self.node(c, x if t == "Cell" else parent_cell))
        n["flags"], n["actions"] = flags, acts
        if mkids: n["children"] = mkids
        return n

def convert(tree, screen=None):
    """tree: the runner's app node. Returns the droidctl raw tree (one window per real app window)."""
    W, H = screen or (tree["rect"][2], tree["rect"][3])
    out = {"gen": 1, "dump": 1, "screen": {"w": W, "h": H, "density": 160, "rotation": 0}, "windows": []}
    m = Mapper((W, H))
    wins = [c for c in tree.get("children") or [] if c["type"] == 4] or [tree]
    main = None
    for i, w in enumerate(wins):
        w = prune(w)
        if not any(k.get("label") or k.get("id") or k.get("value") for k in walk(w)): continue   # empty overlay windows
        if rect(w)[2] > W * 1.5: continue                                                      # pixel-space windows
        boxes = [rect(k) for k in walk(w) if (k.get("label") or k.get("id")) and rect(k)[2] > 0 and rect(k)[3] > 0
                 and overlaps(rect(k), [0, 0, W, H])]
        if not boxes: continue
        if main is None:      # the app's main window is the screen (its frame includes the status bar area, but
            main = i          # the status bar itself is SpringBoard's; a content box would read it as a dialog)
            l, t, r, bt = 0, 0, W, H
        else:                 # alerts, sheets, the keyboard: their content box
            l = max(0, min(b[0] for b in boxes)); t = max(0, min(b[1] for b in boxes))
            r = min(W, max(b[0] + b[2] for b in boxes)); bt = min(H, max(b[1] + b[3] for b in boxes))
        out["windows"].append({"id": i + 1, "type": "application", "layer": i, "pkg": "ios", "title": tree.get("label"),
                               "bounds": [l, t, r, bt], "root": m.node(w)})
    return out
