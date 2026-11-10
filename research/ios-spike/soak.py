"""Soak: run a mixed workload for MINUTES, report per-minute latency and runner memory.
python3 soak.py HOST:PORT MINUTES OUT.json"""
import json
import statistics
import sys
import time

from s2 import Client, walk

APP = "dev.droidctl.s1.testapp"
c = Client(sys.argv[1], timeout=120)
minutes = float(sys.argv[2])
c.call("dlaunch", bundle=APP)
time.sleep(4)
rows, errors = [], []
end = time.time() + minutes * 60
minute = {"snap": [], "act": [], "tap": [], "tree": []}
next_report = time.time() + 60
i = 0
while time.time() < end:
    i += 1
    try:
        t = c.call("snapfast", bundle=APP)
        if not t.get("ok"):
            raise RuntimeError(t)
        minute["snap"].append(t["rtt_ms"])
        cv = next(n for n, _ in walk(t["tree"]) if n.get("id") == "dta_custom")
        r = c.call("act", ax=cv["ax"], action=2010)
        minute["act"].append(r["rtt_ms"])
        if r.get("result") != "ok":
            errors.append({"i": i, "act": r.get("result")})
        if i % 5 == 0:
            x, y, w, h = cv["rect"]
            minute["tap"].append(c.call("fasttap", x=x + w / 2, y=y + h / 2)["rtt_ms"])
        if i % 10 == 0:
            minute["tree"].append(c.call("tree", bundle=APP)["rtt_ms"])
            c.call("forget")  # drop cached AX elements so the cache does not grow without bound
    except Exception as e:  # keep going; record what failed
        errors.append({"i": i, "t": time.strftime("%H:%M:%S"), "error": repr(e)[:200]})
        time.sleep(2)
        try:
            c = Client(sys.argv[1], timeout=120)
        except Exception as e2:
            errors.append({"i": i, "reconnect": repr(e2)[:200]})
    if time.time() >= next_report:
        st = c.call("stats")
        row = {"t": time.strftime("%H:%M:%S"), "iters": i, "rss_mb": round(st.get("rss_mb", -1), 1), "errors": len(errors)}
        for k, xs in minute.items():
            if xs:
                row[k + "_p50"] = round(statistics.median(xs), 1)
                row[k + "_max"] = round(max(xs), 1)
        rows.append(row)
        print(json.dumps(row), flush=True)
        minute = {k: [] for k in minute}
        next_report += 60
    time.sleep(0.5)
json.dump({"rows": rows, "errors": errors[:50], "n_errors": len(errors), "iterations": i}, open(sys.argv[3], "w"), indent=1)
print("done iterations", i, "errors", len(errors))
