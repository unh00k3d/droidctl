"""Sweep AX action numbers one at a time on UIKit controls in the test app, with a liveness check after each.
Ground truth = DTA log.  python3 sweep2.py OUT.json"""
import json, os, sys, time, datetime, subprocess
from s2 import Client, walk
A = "dev.droidctl.s1.testapp"; CDID = os.environ["IOS_CDID"]  # CoreDevice id (xcrun devicectl list devices)
c = Client("127.0.0.1:28100", timeout=120)
RANGES = [(0, 120), (1000, 1100), (2000, 2120), (3000, 3030), (4000, 4030), (5000, 5120)]
SKIP = {2003, 2006, 2007, 2014, 2015}
targets = {
    "UIButton": lambda n: n.get("id") == "dta_button",
    "UISwitch": lambda n: n.get("id") == "dta_switch" and n["type"] == 40,
    "UITableViewCell": lambda n: n["type"] == 75 and any(k.get("label") == "Item 3" for k, _ in walk(n)),
    "UITextField": lambda n: n.get("id") == "dta_field",
}
def node(pred):
    for _ in range(3):
        r = c.call("snapfast", bundle=A)
        if r.get("ok"):
            x = next((n for n, _ in walk(r["tree"]) if pred(n)), None)
            if x: return x
        c.call("dlaunch", bundle=A); time.sleep(1.5)
    raise RuntimeError("target not found")
out = {}
for name, pred in targets.items():
    res, n = [], node(pred)
    for a, b in RANGES:
        for k in range(a, b + 1):
            if k in SKIP: continue
            r = c.call("sweep", ax=n["ax"], **{"from": k, "to": k}, gap_ms=40, timeout_ms=300)
            row = list(r["results"][0]) if r.get("results") else [k, time.time(), r.get("error")]
            st = c.call("state", bundle=A).get("state")
            if st != 4:
                row.append(f"app state {st}")
                c.call("dlaunch", bundle=A); time.sleep(1.5); n = node(pred)
            elif row[2] == "ok" or k % 25 == 0:
                n = node(pred)  # refresh the handle now and then (an action may re-create the element)
            res.append(row)
            if name == "UITextField": c.call("sendstring", text="\n") if row[2] == "ok" else None
    out[name] = res
    oks = [x[0] for x in res if x[2] == "ok"]
    print(name, "ok:", oks, "| killed app:", [x[0] for x in res if len(x) > 3], flush=True)
json.dump(out, open(sys.argv[1], "w"))
subprocess.run(["xcrun", "devicectl", "device", "copy", "from", "--device", CDID, "--domain-type", "appDataContainer", "--domain-identifier", A, "--source", "Documents/dta.log", "--destination", "sweep-dta.log"], capture_output=True)
APPLE = 978307200
ev = []
for line in open("sweep-dta.log"):
    ts, _, e = line.partition(" DTA ")
    try: t = (datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() - APPLE) * 1000
    except ValueError: continue
    ev.append((t, e.strip()))
for name, res in out.items():
    print("==", name)
    for i, row in enumerate(res):
        n, t, r = row[:3]
        t2 = res[i + 1][1] if i + 1 < len(res) else t + 500
        hit = [e for te, e in ev if t <= te < t2 and "ax_activate" not in e and "app ready" not in e]
        if hit: print(f"   {n:5} reply={r:9} -> {hit}")
