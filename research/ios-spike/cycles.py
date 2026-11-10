"""Repeat the lock guard N times on the simulator: lock -> state/refusals -> home,home -> clear -> act ok.
Prints the locked windows (UTC) so DTA can be checked for events inside them.  python3 cycles.py HOST:PORT N OUT.json"""
import json
import sys
import time

from s2 import Client, walk

A = "dev.droidctl.s1.testapp"
c = Client(sys.argv[1], timeout=60)
n = int(sys.argv[2])
c.call("dlaunch", bundle=A)
time.sleep(2)
res = []
for i in range(n):
    t = c.call("tree", bundle=A)
    cv = next(x for x, _ in walk(t["tree"]) if x.get("id") == "dta_custom")
    ok_before = c.call("act", ax=cv["ax"], action=2010).get("result")
    c.call("lock")
    time.sleep(2.5)
    w0 = time.time()
    st = c.call("screen_state")["state"]
    refused = [c.call(cmd, **kw).get("error") for cmd, kw in (("act", {"ax": cv["ax"], "action": 2010}), ("fasttap", {"x": 100, "y": 160}), ("dlaunch", {"bundle": A}))]
    w1 = time.time()
    c.call("home", force=True); time.sleep(1.5)
    st2 = c.call("screen_state")["state"]
    c.call("home", force=True); time.sleep(1.5)
    st3 = c.call("screen_state")["state"]
    c.call("dlaunch", bundle=A); time.sleep(1.5)
    ok_after = c.call("act", ax=cv["ax"], action=2010).get("result")
    row = {"i": i, "before": ok_before, "locked_state": st, "refused": refused, "after_home1": st2, "after_home2": st3, "after": ok_after,
           "locked_window": [time.strftime("%H:%M:%S", time.localtime(w0)), time.strftime("%H:%M:%S", time.localtime(w1))]}
    res.append(row)
    print(json.dumps(row), flush=True)
json.dump(res, open(sys.argv[3], "w"), indent=1)
good = sum(r["locked_state"] == "locked" and all(e == "locked" for e in r["refused"]) and r["after"] == "ok" for r in res)
print(f"cycles ok: {good}/{n}")
