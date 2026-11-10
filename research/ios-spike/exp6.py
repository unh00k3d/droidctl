"""Round 6: web content timing + all web controls; landscape coordinate spaces for taps and hit-test.
python3 exp6.py HOST:PORT OUT.json"""
import json
import sys
import time

from s2 import Client, walk

APP = "dev.droidctl.s1.testapp"
c = Client(sys.argv[1], timeout=120)
out = {}


def log(k, v):
    out[k] = v
    print(k, json.dumps(v)[:700], flush=True)


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


def web_nodes():
    return [n for n, _ in walk(by_id(tree(), "dta_web"))]


def center(n):
    x, y, w, h = n["rect"]
    return x + w / 2, y + h / 2


call("launch", bundle=APP)
t0 = time.perf_counter()
appear = None
for i in range(40):
    wn = web_nodes()
    if any(n.get("label") == "Web Button" or n["type"] == 49 for n in wn):
        appear = round((time.perf_counter() - t0) * 1000)
        break
    time.sleep(0.25)
log("web_content_appears_ms", appear)
wn = web_nodes()
log("web_nodes", [(n["type"], n.get("label"), n.get("value"), n.get("ph")) for n in wn if n.get("label") or n.get("ph") or n["type"] in (9, 12, 40, 42, 49)])
res = {}
for n in wn:
    if n["type"] in (9, 42, 40, 12) and "scroll bar" not in (n.get("label") or ""):
        r = call("act", ax=n["ax"], action=2010)["result"]
        res[f"{n['type']}:{n.get('label')}"] = r
        time.sleep(0.8)
log("web_press_all", res)

# --- landscape coordinate spaces -------------------------------------------------------------------
call("orientation", set=3)  # device LandscapeLeft -> interface LandscapeRight
time.sleep(2.0)
t = tree()
cv = by_id(t, "dta_custom")
W, H = t["tree"]["rect"][2], t["tree"]["rect"][3]  # landscape points
x, y = center(cv)
cands = {"as_is": (x, y), "portrait_cw": (H - y, x), "portrait_ccw": (y, W - x)}
hits = {}
for k, (px, py) in cands.items():
    h = call("hit", x=px, y=py).get("ax")
    hits[k] = {"pt": [round(px), round(py)], "match": h == cv["ax"]}
log("landscape_hit_spaces", hits)
taps = {}
for k, (px, py) in cands.items():
    for io in (0, 1, 3, 4):
        taps[f"{k}/iorient={io}"] = {"t": time.strftime("%H:%M:%S", time.gmtime()), "r": call("fasttap", x=px, y=py, iorient=io)["result"]}
        time.sleep(1.2)
log("landscape_taps", taps)
call("orientation", set=1)
time.sleep(1.5)
json.dump(out, open(sys.argv[2], "w"), indent=1)
