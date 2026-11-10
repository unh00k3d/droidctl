"""Settle research round 2 (device): per scenario, AX notifications + idle callbacks + polled screen-signature changes.
python3 settle2.py HOST:PORT OUT.json [rounds]"""
import json, sys, time
from s2 import Client, walk
A, ST = "dev.droidctl.s1.testapp", "com.apple.Preferences"
c = Client(sys.argv[1], timeout=180)
rounds = int(sys.argv[3]) if len(sys.argv) > 3 else 1
out = []
def tree(b=A): return c.call("snapfast", bundle=b)["tree"]
def find(pred, b=A): return next((n for n, _ in walk(tree(b)) if pred(n)), None)
def by_id(i, b=A): return find(lambda n: n.get("id") == i, b)
def ctr(n): x, y, w, h = n["rect"]; return x + w / 2, y + h / 2
def tap(n): x, y = ctr(n); return {"cmd": "fasttap", "x": x, "y": y}
def run(k, bundle, do, watch=2500, idles=("runloop", "anim")):
    r = c.call("watch", do=do, bundle=bundle, watch_ms=watch, poll_ms=1, idles=list(idles))
    sigs = [round(s["t"]) for s in r.get("sigs", []) if not s.get("pre")]
    ev = {}
    for e in r.get("events", []):
        key = e["name"] if not e["name"].startswith("AXNotification") else str(e["n"])
        if e["n"] == 4002:
            p = e["p"]; key = "UT:" + (p.split("event = ")[1].split(";")[0] if "event = " in p else p[:30])
        ev.setdefault(key, []).append(round(e["t"]))
    idl = {i["kind"]: round(i.get("end", -1)) for i in r.get("idles", [])}
    print(f"== {k}: inner={r.get('inner',{}).get('result')} {round(r.get('inner_ms',0))}ms idle={idl} sig_changes={sigs} (snap {[round(s['ms']) for s in r.get('sigs',[])][:3]}ms)")
    for kk, ts in ev.items(): print(f"   {kk:28} {ts[:12]}{' …' if len(ts) > 12 else ''}")
    out.append({"k": k, **r}); return r
c.call("axreg", ids=list(range(1000, 1060)) + [4002])
for rnd in range(rounds):
    c.call("dlaunch", bundle=A); time.sleep(2.5)
    run("noop_press", A, {"cmd": "act", "ax": by_id("dta_custom")["ax"], "action": 2010})
    run("switch_tap", A, tap(by_id("dta_switch")))
    run("slider_inc", A, {"cmd": "act", "ax": by_id("dta_slider")["ax"], "action": 2004})
    run("alert_open", A, tap(by_id("dta_alert")))
    run("alert_ok", A, tap(find(lambda n: n.get("label") == "OK" and n["type"] == 9)))
    tb = by_id("dta_table")
    run("table_page", A, {"cmd": "act", "ax": tb["ax"], "action": 2006})
    x, y, w, h = tb["rect"]
    run("table_fling", A, {"cmd": "swipe", "x1": x + w/2, "y1": y + h*0.9, "x2": x + w/2, "y2": y + h*0.1, "duration": 0.1}, watch=5000)
    run("field_tap_kbd", A, tap(by_id("dta_field")))
    run("type_abc", A, {"cmd": "sendstring", "text": "abc"})
    run("return_kbd_hide", A, {"cmd": "sendstring", "text": "\n"})
    run("switch_to_settings", ST, {"cmd": "dlaunch", "bundle": ST}, watch=3000)
    gen = find(lambda n: n.get("label") == "General" and n["type"] in (75, 9), ST)
    run("settings_push_general", ST, tap(gen), watch=3000)
    back = find(lambda n: n["type"] == 9 and n.get("label") in ("Settings", "Back"), ST)
    run("settings_pop", ST, tap(back), watch=3000)
    run("switch_back_app", A, {"cmd": "dlaunch", "bundle": A}, watch=3000)
json.dump(out, open(sys.argv[2], "w"), indent=1)
