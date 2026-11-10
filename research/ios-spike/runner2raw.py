"""Throwaway: S2-runner tree JSON (XCUIElementType ints) -> droidctl raw tree -> snapshot.build/render.
python3 runner2raw.py FIXTURE.json [--flat] [--quiet]"""
import json
import sys

sys.path.insert(0, "/Users/enesb/Documents/droidctl")
from droidctl import snapshot as S  # noqa: E402

# XCUIElementType raw values (XCUIElementTypes.h)
T = {1: "Other", 2: "Application", 3: "Group", 4: "Window", 7: "Alert", 8: "Dialog", 9: "Button", 19: "Keyboard", 20: "Key",
     21: "NavigationBar", 22: "TabBar", 24: "Toolbar", 25: "StatusBar", 26: "Table", 32: "CollectionView", 33: "Slider",
     36: "ActivityIndicator", 37: "SegmentedControl", 40: "Switch", 41: "Toggle", 42: "Link", 43: "Image", 44: "Icon",
     45: "SearchField", 46: "ScrollView", 47: "ScrollBar", 48: "StaticText", 49: "TextField", 50: "SecureTextField",
     52: "TextView", 58: "WebView", 75: "Cell", 34: "PageIndicator"}
CLASS = {"Button": "android.widget.Button", "StaticText": "android.widget.TextView", "Switch": "android.widget.Switch",
         "Toggle": "android.widget.Switch", "TextField": "android.widget.EditText", "SearchField": "android.widget.EditText",
         "SecureTextField": "android.widget.EditText", "TextView": "android.widget.EditText", "Image": "android.widget.ImageView",
         "Icon": "android.widget.ImageView", "Cell": "android.view.ViewGroup", "CollectionView": "androidx.recyclerview.widget.RecyclerView",
         "Table": "android.widget.ListView", "ScrollView": "android.widget.ScrollView", "Slider": "android.widget.SeekBar",
         "Link": "android.widget.Button", "Key": "android.widget.Button", "Alert": "android.view.ViewGroup"}
CLICK = {"Button", "Cell", "Switch", "Toggle", "Link", "TextField", "SearchField", "SecureTextField", "TextView", "Icon", "Key"}
SCROLL = {"CollectionView", "Table", "ScrollView"}
EDIT = {"TextField", "SearchField", "SecureTextField", "TextView"}
h = [0]


def node(x):
    h[0] += 1
    t = T.get(x["type"], "Other")
    rx, ry, rw, rh = x["rect"]
    label, ident, value = x.get("label"), x.get("id"), x.get("value")
    n = {"handle": h[0], "class": CLASS.get(t, "android.view.View"), "pkg": "ios", "bounds": [rx, ry, rx + rw, ry + rh]}
    flags = [] if x.get("disabled") else ["enabled"]
    acts = []
    if t == "StaticText":
        n["text"] = label or value
    elif t in EDIT:
        if value and value != x.get("ph"):
            n["text"] = value
        n["hint"] = x.get("ph") or label
        flags += ["editable", "focusable"]
    elif label:
        n["desc"] = label
    if ident and ident != label:
        n["id"] = ident
    if t in CLICK:
        flags.append("clickable"); acts.append("click")
    if t in ("Switch", "Toggle"):
        flags.append("checkable")
        if value == "1":
            flags.append("checked")
    if t in SCROLL:
        flags.append("scrollable"); acts += ["scroll_forward", "scroll_backward"]
    if x.get("selected"):
        flags.append("selected")
    if x.get("focused"):
        flags.append("focused")
    n["flags"], n["actions"] = flags, acts
    kids = [node(c) for c in x.get("children") or []]
    if kids:
        n["children"] = kids
    return n


def convert(fx):
    app = fx["tree"]
    W, H = app["rect"][2], app["rect"][3]
    tree = {"gen": 1, "dump": 1, "screen": {"w": W, "h": H, "density": 160, "rotation": 0}, "windows": []}
    wins = [c for c in app.get("children") or [] if c["type"] == 4] or [app]
    for i, w in enumerate(wins):
        if not any(k.get("label") or k.get("id") for k in _walk(w)):
            continue  # empty overlay windows must not occlude
        if w["rect"][2] > W * 1.5:
            continue  # SpringBoard's pixel-space windows (1170x2532 on a 390x844 screen)
        boxes = [k["rect"] for k in _walk(w) if (k.get("label") or k.get("id")) and k["rect"][2] > 0 and k["rect"][3] > 0]
        l = max(0, min(b[0] for b in boxes)); t = max(0, min(b[1] for b in boxes))
        r = min(W, max(b[0] + b[2] for b in boxes)); bt = min(H, max(b[1] + b[3] for b in boxes))
        tree["windows"].append({"id": i + 1, "type": "application", "layer": i, "pkg": "ios", "title": app.get("label"),
                                "bounds": [l, t, r, bt], "root": node(w)})
    return tree


def _walk(n):
    yield n
    for c in n.get("children") or []:
        yield from _walk(c)


if __name__ == "__main__":
    fx = json.load(open(sys.argv[1]))
    snap = S.build(convert(fx))
    o = S.Opts.from_env()
    if "--flat" in sys.argv:
        o.spatial = False
    text = S.render(snap, o)
    if "--quiet" not in sys.argv:
        print(text)
    raw = len(json.dumps(fx["tree"]))
    print(f"--- {fx['meta']['name']}: {fx['meta']['nodes']} nodes, raw {raw // 1024} KB -> {len(snap.elements)} elements, ~{S.est_tokens(text)} tokens")
