"""Round 5: WKWebView content + actions, multi-touch pinch, rotation (frames, taps, screenshot, hit-test).
python3 exp5.py HOST:PORT OUT.json"""
import base64
import json
import struct
import sys
import time

from s2 import Client, walk

APP = "dev.droidctl.s1.testapp"
c = Client(sys.argv[1], timeout=120)
out = {}


def log(k, v):
    out[k] = v
    print(k, json.dumps(v)[:600], flush=True)


def call(cmd, **kw):
    r = c.call(cmd, **kw)
    if not r.get("ok"):
        raise RuntimeError((cmd, r))
    return r


def tree():
    return call("tree", bundle=APP)


def nodes(t):
    return [n for n, _ in walk(t["tree"])]


def by_id(t, ident):
    return next(n for n in nodes(t) if n.get("id") == ident)


def center(n):
    x, y, w, h = n["rect"]
    return x + w / 2, y + h / 2


def subtree(t, ident):
    return [n for n, _ in walk(by_id(t, ident))]


call("launch", bundle=APP)
time.sleep(3)  # let the web view load
t = tree()
json.dump(t["tree"], open("tree5-testapp.json", "w"))

# --- WKWebView -------------------------------------------------------------------------------------
web = subtree(t, "dta_web")
log("web_subtree", [(n["type"], n.get("label"), n.get("value"), n.get("ph"), [round(v) for v in n["rect"]]) for n in web if n.get("label") or n.get("value") or n.get("ph")])


def web_node(pred):
    return next((n for n in subtree(tree(), "dta_web") if pred(n)), None)


res = {}
for name, pred in (("button", lambda n: n["type"] == 9 and n.get("label") == "Web Button"),
                   ("link", lambda n: n["type"] == 42 or (n.get("label") == "Web Link" and n["type"] != 48)),
                   ("checkbox", lambda n: n["type"] in (12, 40, 41) or (n.get("label") or "").startswith("Web Checkbox") and n["type"] != 48)):
    n = web_node(pred)
    if not n:
        res[name] = "not found"
        continue
    r = call("act", ax=n["ax"], action=2010)["result"]
    res[name] = {"type": n["type"], "press": r}
    if r != "ok":
        x, y = center(n)
        h = call("hit", x=x, y=y).get("ax")
        res[name]["fallback_tap"] = call("fasttap", x=x, y=y)["result"]
        res[name]["hit_matches"] = h == n["ax"]
    time.sleep(1.0)
log("web_press", res)
inp = web_node(lambda n: n["type"] == 49)
if inp:
    x, y = center(inp)
    call("fasttap", x=x, y=y)
    time.sleep(1.0)
    log("web_sendstring", call("sendstring", text="abc")["result"])
    time.sleep(0.5)
    inp2 = web_node(lambda n: n["type"] == 49)
    log("web_input_value", inp2.get("value"))
    log("web_setattr", call("setattr", ax=inp2["ax"], attr=2006, value="xyz")["result"])
    time.sleep(0.5)
    log("web_input_value_after_setattr", web_node(lambda n: n["type"] == 49).get("value"))
    call("sendstring", text="\n")
    time.sleep(0.5)

# --- pinch -----------------------------------------------------------------------------------------
t = tree()
p = by_id(t, "dta_pinch")
x, y = center(p)
w = p["rect"][2]
log("pinch_out", call("pinch", x=x, y=y, **{"from": w * 0.1, "to": w * 0.4, "duration": 0.6})["result"])
time.sleep(1.0)
log("pinch_in", call("pinch", x=x, y=y, **{"from": w * 0.4, "to": w * 0.1, "duration": 0.6})["result"])
time.sleep(1.0)

# --- rotation --------------------------------------------------------------------------------------
log("orientation_before", call("orientation")["orientation"])
log("rotate_landscape", call("orientation", set=3)["orientation"])  # UIDeviceOrientationLandscapeLeft
time.sleep(2.0)
t = tree()
root = t["tree"]["rect"]
cv = by_id(t, "dta_custom")
r = call("screenshot", data=True)
png = base64.b64decode(r["png"])
log("landscape", {"app_rect": root, "custom_rect": cv["rect"], "screenshot_px": list(struct.unpack(">II", png[16:24]))})
open("landscape.png", "wb").write(png)
x, y = center(cv)
h = call("hit", x=x, y=y)
log("landscape_hit_matches", h.get("ax") == cv["ax"])
log("landscape_fasttap", call("fasttap", x=x, y=y)["result"])
time.sleep(1.0)
log("landscape_press", call("act", ax=cv["ax"], action=2010)["result"])
time.sleep(0.5)
log("rotate_back", call("orientation", set=1)["orientation"])
time.sleep(2.0)
log("portrait_app_rect", tree()["tree"]["rect"])
json.dump(out, open(sys.argv[2], "w"), indent=1)
