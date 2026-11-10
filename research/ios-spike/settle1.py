"""Settle research round 1: what does the runner hear after an action?  python3 settle1.py HOST:PORT OUT.json"""
import json, sys, time
from s2 import Client, walk
A = "dev.droidctl.s1.testapp"
c = Client(sys.argv[1], timeout=120)
out = {}
def tree(): return c.call("snapfast", bundle=A)["tree"]
def by_id(i): return next(n for n, _ in walk(tree()) if n.get("id") == i)
def ctr(n): x, y, w, h = n["rect"]; return x + w / 2, y + h / 2
def show(k, r):
    ev = [(round(e["t"]), e["n"], e["name"][:22], e["el"][:18], e["p"][:90]) for e in r.get("events", [])]
    print(f"== {k}: inner={r.get('inner',{}).get('result')} {round(r.get('inner_ms',0))}ms idles={[(i['kind'],round(i.get('end',-1)),i.get('result')) for i in r.get('idles',[])]}")
    print("   sigs", [(round(s['t']), s['nodes'], round(s['ms'])) for s in r.get("sigs", [])])
    for e in ev[:40]: print("  ", e)
    if len(ev) > 40: print("   …", len(ev), "events")
    out[k] = r
c.call("axreg", ids=list(range(1000, 1060)) + [4002])
c.call("dlaunch", bundle=A); time.sleep(2)
W = dict(bundle=A, watch_ms=2500, poll_ms=40, idles=["runloop", "anim"])
show("noop_custom_press", c.call("watch", do={"cmd": "act", "ax": by_id("dta_custom")["ax"], "action": 2010}, **W))
x, y = ctr(by_id("dta_switch")); show("switch_tap", c.call("watch", do={"cmd": "fasttap", "x": x, "y": y}, **W))
x, y = ctr(by_id("dta_alert")); show("alert_open", c.call("watch", do={"cmd": "fasttap", "x": x, "y": y}, **W))
t = tree(); ok = next(n for n, _ in walk(t) if n.get("label") == "OK" and n["type"] == 9)
x, y = ctr(ok); show("alert_ok", c.call("watch", do={"cmd": "fasttap", "x": x, "y": y}, **W))
tb = by_id("dta_table"); show("table_page", c.call("watch", do={"cmd": "act", "ax": tb["ax"], "action": 2006}, **W))
x, y, w, h = tb["rect"]; show("table_fling", c.call("watch", do={"cmd": "swipe", "x1": x + w/2, "y1": y + h*0.9, "x2": x + w/2, "y2": y + h*0.1, "duration": 0.1}, **{**W, "watch_ms": 4000}))
json.dump(out, open(sys.argv[2], "w"), indent=1)
