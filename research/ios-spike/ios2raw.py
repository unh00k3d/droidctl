"""Throwaway S0 mapper: WDA /source JSON -> droidctl raw tree, then run snapshot.build/render.

Usage: python ios2raw.py TREE.json [--flat]
"""
import json
import sys

sys.path.insert(0, "/Users/enesb/Documents/droidctl")
from droidctl import snapshot as S  # noqa: E402

CLASS = {
    "Button": "android.widget.Button", "StaticText": "android.widget.TextView",
    "Switch": "android.widget.Switch", "TextField": "android.widget.EditText",
    "SearchField": "android.widget.EditText", "SecureTextField": "android.widget.EditText",
    "Image": "android.widget.ImageView", "Cell": "android.view.ViewGroup",
    "CollectionView": "androidx.recyclerview.widget.RecyclerView", "Table": "android.widget.ListView",
    "ScrollView": "android.widget.ScrollView", "NavigationBar": "android.view.ViewGroup",
    "Alert": "android.view.ViewGroup", "Link": "android.widget.Button", "TabBar": "android.view.ViewGroup",
}
CLICK = {"Button", "Cell", "Switch", "Link", "TextField", "SearchField", "SecureTextField"}
SCROLL = {"CollectionView", "Table", "ScrollView"}

h = [0]


def node(x):
    h[0] += 1
    t = x.get("type", "Other")
    r = x.get("rect") or {}
    label, name, value = x.get("label"), x.get("name"), x.get("value")
    n = {"handle": h[0], "class": CLASS.get(t, "android.view.View"), "pkg": "ios",
         "bounds": [r.get("x", 0), r.get("y", 0), r.get("x", 0) + r.get("width", 0), r.get("y", 0) + r.get("height", 0)]}
    flags = ["enabled"] if x.get("isEnabled") else []
    acts = []
    if t == "StaticText":
        n["text"] = label or value
    elif t in ("TextField", "SearchField", "SecureTextField"):
        if value and value != label:
            n["text"] = value
        n["hint"] = label
        flags += ["editable", "focusable"]
    elif label:
        n["desc"] = label
    if name and name != label:
        n["id"] = name
    if t in CLICK:
        flags.append("clickable")
        acts.append("click")
    if t == "Switch":
        flags.append("checkable")
        if value == "1":
            flags.append("checked")
    if t in SCROLL:
        flags.append("scrollable")
        acts += ["scroll_forward", "scroll_backward"]
    if x.get("isFocused"):
        flags.append("focused")
    n["flags"] = flags
    n["actions"] = acts
    kids = [node(c) for c in x.get("children") or []]
    if kids:
        n["children"] = kids
    return n


def _content(x):
    if x.get("label") or x.get("name"):
        return True
    return any(_content(c) for c in x.get("children") or [])


def convert(path):
    app = json.load(open(path))["value"]
    r = app["rect"]
    W, H = r["width"], r["height"]
    tree = {"gen": 1, "dump": 1, "screen": {"w": W, "h": H, "density": 160, "rotation": 0}, "windows": []}
    wins = [c for c in app.get("children") or [] if c.get("type") == "Window"] or [app]
    for i, w in enumerate(wins):
        wr = w["rect"]
        if wr["width"] > W * 1.5:  # the 1206x2622 windows in SpringBoard: pixels, not points
            continue
        if not _content(w):  # iOS apps carry empty full-screen overlay windows; they must not occlude
            continue
        tree["windows"].append({"id": i + 1, "type": "application", "layer": i, "pkg": "ios", "title": app.get("label"),
                                "bounds": [0, 0, W, H], "root": node(w)})
    return tree


if __name__ == "__main__":
    t = convert(sys.argv[1])
    snap = S.build(t)
    o = S.Opts.from_env()
    if "--flat" in sys.argv:
        o.spatial = False
    text = S.render(snap, o)
    raw = len(json.dumps(json.load(open(sys.argv[1]))))
    print(text)
    print(f"--- {len(snap.elements)} elements, ~{S.est_tokens(text)} tokens (raw WDA JSON {raw // 1024} KB)")
