"""Round 3: tree latency (public vs daemon snapshot, depth), attributes (frame/parent/valid/visible),
scroll-to-visible, Return key, stale-element liveness.   python3 exp3.py HOST:PORT OUT.json"""
import json
import re
import statistics
import sys
import time

from s2 import Client, walk

APP = "dev.droidctl.s1.testapp"
SETTINGS = "com.apple.Preferences"
c = Client(sys.argv[1], timeout=120)
out = {}
A = "XC_kAXXCAttribute"


def log(k, v):
    out[k] = v
    print(k, json.dumps(v)[:500], flush=True)


def call(cmd, **kw):
    r = c.call(cmd, **kw)
    if not r.get("ok"):
        raise RuntimeError((cmd, r))
    return r


def ms(xs):
    return {"median": round(statistics.median(xs), 1), "min": round(min(xs), 1), "max": round(max(xs), 1), "n": len(xs)}


def nodes(t):
    return [n for n, _ in walk(t["tree"])]


def by_id(t, ident):
    return next(n for n in nodes(t) if n.get("id") == ident)


def frame(s):
    nums = [float(x) for x in re.findall(r"-?[\d.]+", s or "")]
    return nums[:4] if len(nums) >= 4 else None


call("launch", bundle=APP)
time.sleep(1)

# --- tree latency: public snapshot vs daemon snapshot, and depth / attribute effects ---------------
r = call("snapfast", bundle=APP, show_params=True)
log("default_params", r.get("default_params"))
for bundle in (APP, SETTINGS):
    call("activate", bundle=bundle) if bundle == APP else call("launch", bundle=bundle)
    time.sleep(1.0)
    pub = [call("tree", bundle=bundle) for _ in range(5)]
    fast = [call("snapfast", bundle=bundle) for _ in range(5)]
    shallow = [call("snapfast", bundle=bundle, params={"maxDepth": 12}) for _ in range(3)]
    minimal = [call("snapfast", bundle=bundle, attrs=[A + "ElementType", A + "Frame", A + "Label"]) for _ in range(3)]
    log(f"latency_{bundle}", {
        "public": {"nodes": pub[-1]["nodes"], "rtt": ms([x["rtt_ms"] for x in pub])},
        "daemon": {"nodes": fast[-1]["nodes"], "rtt": ms([x["rtt_ms"] for x in fast])},
        "daemon_depth12": {"nodes": shallow[-1]["nodes"], "rtt": ms([x["rtt_ms"] for x in shallow])},
        "daemon_3attrs": {"nodes": minimal[-1]["nodes"], "rtt": ms([x["rtt_ms"] for x in minimal])},
    })
    json.dump(fast[-1]["tree"], open(f"tree3-daemon-{bundle}.json", "w"))

# --- attributes on elements ----------------------------------------------------------------------
call("activate", bundle=APP)
time.sleep(1.0)
t = call("tree", bundle=APP)
attrs = [A + "Frame", A + "Parent", A + "IsValid", A + "IsVisible", A + "Label", A + "Identifier", A + "ElementType", A + "IsUserInteractionEnabled"]
sw = by_id(t, "dta_switch")
x, y, w, h = sw["rect"]
hit = call("hit", x=x + w / 2, y=y + h / 2)["ax"]
hv = call("fetch", ax=hit, attrs=attrs)
log("switch_hit_attrs", hv.get("values") or hv.get("error"))
pv = hv.get("values", {}).get(A + "Parent")
log("switch_hit_parent_is_switch", pv == sw["ax"])
cells = [n for n in nodes(t) if n["type"] == 75]
off = next(n for n in cells if n["rect"][1] > 2000)
on = next(n for n in cells if 0 < n["rect"][1] < 800)
log("cell_visible_attr", {"onscreen": call("fetch", ax=on["ax"], attrs=[A + "IsVisible", A + "Frame"]).get("values"),
                          "offscreen": call("fetch", ax=off["ax"], attrs=[A + "IsVisible", A + "Frame"]).get("values")})
f = call("fetch", ax=sw["ax"], attrs=attrs)
log("fetch_latency_ms", round(f["rtt_ms"], 1))

# --- scroll to visible (2003) on an offscreen cell ------------------------------------------------
lab = next((k.get("label") for k, _ in walk(off) if k.get("label")), None)
r = call("act", ax=off["ax"], action=2003)
time.sleep(1.0)
t2 = call("tree", bundle=APP)
after = next(n for n in nodes(t2) if n.get("ax") == off["ax"])
log("scroll_to_visible", {"label": lab, "result": r["result"], "rect_before": off["rect"], "rect_after": after["rect"]})

# --- Return key through sendString ------------------------------------------------------------------
fld = by_id(t2, "dta_field")
x, y, w, h = fld["rect"]
call("fasttap", x=x + w / 2, y=y + h / 2)
time.sleep(1.0)
log("sendstring_return", call("sendstring", text="ok\n")["result"])
time.sleep(1.0)
t3 = call("tree", bundle=APP)
log("keyboard_after_return", any(n["type"] == 20 for n in nodes(t3)))

# --- stale elements: liveness check after relaunch --------------------------------------------------
old = by_id(t3, "dta_custom")["ax"]
call("launch", bundle=APP)
time.sleep(1.0)
v = call("fetch", ax=old, attrs=[A + "IsValid"])
log("stale_isvalid", {"values": v.get("values"), "error": v.get("error"), "ms": round(v["rtt_ms"], 1)})
json.dump(out, open(sys.argv[2], "w"), indent=1)
