"""Round 4: screenshot scale, long-press, launch/activate timings, device fixture capture.
python3 exp4.py HOST:PORT OUTDIR"""
import base64
import json
import os
import struct
import sys
import time

from s2 import Client, walk

APP = "dev.droidctl.s1.testapp"
SETTINGS = "com.apple.Preferences"
SB = "com.apple.springboard"
c = Client(sys.argv[1], timeout=120)
outdir = sys.argv[2]
os.makedirs(outdir, exist_ok=True)
out = {}
A = "XC_kAXXCAttribute"
ATTRS = [A + x for x in ("ElementType", "Identifier", "Label", "Value", "Frame", "IsEnabled", "IsSelected", "HasFocus",
                         "PlaceholderValue", "IsVisible")]


def log(k, v):
    out[k] = v
    print(k, json.dumps(v)[:300], flush=True)


def call(cmd, **kw):
    r = c.call(cmd, **kw)
    if not r.get("ok"):
        raise RuntimeError((cmd, r))
    return r


def capture(name, bundle, note):
    r = call("tree", bundle=bundle)
    fx = {"meta": {"name": name, "captured": time.strftime("%Y-%m-%d"), "source": "droidctl S2 spike runner, public XCUIElement snapshot",
                   "device": "iPhone 12 (iPhone13,2)", "os": "iOS 18.7.8", "xcode": "26.2 (17C52)", "units": "pt", "bundle": bundle,
                   "note": note, "nodes": r["nodes"]},
          "tree": r["tree"]}
    json.dump(fx, open(os.path.join(outdir, name + ".json"), "w"), separators=(",", ":"))
    return r


# screenshot pixel size vs point size
r = call("screenshot", data=True)
png = base64.b64decode(r["png"])
w, h = struct.unpack(">II", png[16:24])
root = call("tree", bundle=SB)["tree"]["rect"]
log("screenshot", {"px": [w, h], "pt": root[2:], "scale": round(w / root[2], 3), "bytes": len(png), "rtt_ms": round(r["rtt_ms"])})

# launch / activate timings
t0 = time.perf_counter(); call("launch", bundle=APP); log("launch_testapp_ms", round((time.perf_counter() - t0) * 1000))
t0 = time.perf_counter(); call("launch", bundle=SETTINGS); log("launch_settings_ms", round((time.perf_counter() - t0) * 1000))
t0 = time.perf_counter(); call("activate", bundle=APP); log("activate_testapp_ms", round((time.perf_counter() - t0) * 1000))
t0 = time.perf_counter(); call("activate", bundle=SETTINGS); log("activate_settings_ms", round((time.perf_counter() - t0) * 1000))

# fixtures: Settings root, General, test app, home screen
time.sleep(1)
s = capture("settings_root", SETTINGS, "Settings top level")
gen = next(n for n, _ in walk(s["tree"]) if n.get("label") == "General" and n["type"] == 9)
x, y, ww, hh = gen["rect"]
call("fasttap", x=x + ww / 2, y=y + hh / 2)
time.sleep(1.5)
g = capture("settings_general", SETTINGS, "Settings > General")
back = next((n for n, _ in walk(g["tree"]) if n["type"] == 9 and n.get("label") in ("Settings", "Back")), None)
if back:
    x, y, ww, hh = back["rect"]
    call("fasttap", x=x + ww / 2, y=y + hh / 2)
    time.sleep(1.0)
call("activate", bundle=APP)
time.sleep(1.0)
capture("testapp_main", APP, "droidctl S1 test app: controls + 200-row UITableView (all rows exposed, most offscreen)")

# long press via synthesized hold on the custom view (touch_ended after ~1 s hold)
t = call("tree", bundle=APP)["tree"]
cv = next(n for n, _ in walk(t) if n.get("id") == "dta_custom")
x, y, ww, hh = cv["rect"]
t0 = time.perf_counter()
log("long_press", {"result": call("fasttap", x=x + ww / 2, y=y + hh / 2, hold=1.0)["result"], "ms": round((time.perf_counter() - t0) * 1000)})

call("home")
time.sleep(1.5)
capture("springboard_home", SB, "Home screen (user's own icons; labels are app names)")
call("activate", bundle=APP)
json.dump(out, open(os.path.join(outdir, "exp4.json"), "w"), indent=1)
