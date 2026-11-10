"""Fling precision: run-loop idle vs the app's own 'scroll end' DTA timestamp (same device clock).  python3 fling.py HOST:PORT N"""
import json, sys, time
from s2 import Client, walk
A = "dev.droidctl.s1.testapp"
c = Client(sys.argv[1], timeout=120)
c.call("axreg", ids=list(range(1000, 1060)) + [4002])
c.call("dlaunch", bundle=A); time.sleep(2)
res = []
for i in range(int(sys.argv[2])):
    t = c.call("snapfast", bundle=A)["tree"]
    x, y, w, h = next(n for n, _ in walk(t) if n.get("id") == "dta_table")["rect"]
    up = i % 2 == 0
    y1, y2 = (y + h * 0.9, y + h * 0.1) if up else (y + h * 0.1, y + h * 0.9)
    r = c.call("watch", do={"cmd": "swipe", "x1": x + w/2, "y1": y1, "x2": x + w/2, "y2": y2, "duration": 0.1}, bundle=A, watch_ms=4000, poll_ms=0, idles=["runloop", "anim"])
    ids = {k["kind"]: k.get("end") for k in r["idles"]}
    res.append({"t0abs": r["t0abs"], "inner": r["inner_ms"], **ids, "ev": [(round(e["t"]), e["n"]) for e in r["events"] if e.get("via") != 2 and e["n"] != 4002]})
    time.sleep(1)
json.dump(res, open("fling.json", "w"))
print("done", len(res))
