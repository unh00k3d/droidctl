"""Settle research round 3 (device). Each scenario runs twice per round:
 mode E: AX notifications + run-loop/animation idle callbacks, no polling (so nothing competes with the daemon);
 mode P: continuous screen-signature polling with per-node diffs, no idle requests (ground truth for "when did it stop").
python3 settle3.py HOST:PORT OUT.json ROUNDS"""
import json, sys, time
from s2 import Client, walk
A, ST = "dev.droidctl.s1.testapp", "com.apple.Preferences"
c = Client(sys.argv[1], timeout=180)
rounds = int(sys.argv[3])
out = []
def tree(b=A): return c.call("snapfast", bundle=b)["tree"]
def find(pred, b=A): return next((n for n, _ in walk(tree(b)) if pred(n)), None)
def by_id(i, b=A): return find(lambda n: n.get("id") == i, b)
def ctr(n): x, y, w, h = n["rect"]; return x + w / 2, y + h / 2
def tap(n): x, y = ctr(n); return {"cmd": "fasttap", "x": x, "y": y}
def summarize(r):
    ev = {}
    for e in r.get("events", []):
        if e.get("via") == 2: continue
        key = str(e["n"]) if e["name"].startswith("AXNotification") else e["name"].replace("Notification", "").replace("kAX", "")
        if e["n"] == 4002:
            p = e["p"]; key = "UT:" + (p.split("event = ")[1].split(";")[0] if "event = " in p else p[:30])
        ev.setdefault(key, []).append(round(e["t"]))
    return ev
def run(k, bundle, mk, watch=2500, pre=None, post=None):
    for mode in ("E", "P"):
        if pre: pre(); time.sleep(1.2)
        do = mk()
        if mode == "E": r = c.call("watch", do=do, bundle=bundle, watch_ms=watch, poll_ms=0, idles=["runloop", "anim"])
        else: r = c.call("watch", do=do, bundle=bundle, watch_ms=watch, poll_ms=1, idles=[])
        rec = {"k": k, "mode": mode, "round": rnd, **r}; out.append(rec)
        if mode == "E":
            idl = {i["kind"]: round(i.get("end", -1)) for i in r.get("idles", [])}
            print(f"== {k} [E] inner={r.get('inner',{}).get('result')} {round(r.get('inner_ms',0))}ms idle={idl}")
            for kk, ts in summarize(r).items(): print(f"     {kk:24} {ts[:10]}{' …%d' % len(ts) if len(ts) > 10 else ''}")
        else:
            sg = [s for s in r.get("sigs", []) if not s.get("pre")]
            print(f"   {k} [P] changes at {[round(s['t']) for s in sg]} snap≈{round(sum(s['ms'] for s in r['sigs'])/len(r['sigs']))}ms")
            for s in sg[-2:]: print(f"      t={round(s['t'])} +{s.get('add',[])[:2]} -{s.get('rem',[])[:2]}")
        if post: post()
        time.sleep(1.2)
def do_tap(n): x, y = ctr(n); c.call("fasttap", x=x, y=y)
c.call("axreg", ids=list(range(1000, 1060)) + [3031, 4002])
for rnd in range(rounds):
    print(f"######## round {rnd}")
    c.call("dlaunch", bundle=A); time.sleep(2.5)
    run("noop_press", A, lambda: {"cmd": "act", "ax": by_id("dta_custom")["ax"], "action": 2010})
    run("switch_tap", A, lambda: tap(by_id("dta_switch")))
    run("slider_inc", A, lambda: {"cmd": "act", "ax": by_id("dta_slider")["ax"], "action": 2004})
    for mode in "EP":  # alert open+ok as a pair per mode
        pass
    ok = lambda: do_tap(find(lambda n: n.get("label") == "OK" and n["type"] == 9))
    run("alert_open", A, lambda: tap(by_id("dta_alert")), post=ok)
    run("alert_ok", A, lambda: tap(find(lambda n: n.get("label") == "OK" and n["type"] == 9)), pre=lambda: do_tap(by_id("dta_alert")))
    run("table_page", A, lambda: {"cmd": "act", "ax": by_id("dta_table")["ax"], "action": 2006}, watch=3500)
    def fling():
        x, y, w, h = by_id("dta_table")["rect"]
        return {"cmd": "swipe", "x1": x + w/2, "y1": y + h*0.9, "x2": x + w/2, "y2": y + h*0.1, "duration": 0.1}
    run("table_fling", A, fling, watch=5000)
    hide = lambda: c.call("sendstring", text="\n")
    focus = lambda: do_tap(by_id("dta_field"))
    run("field_tap_kbd", A, lambda: tap(by_id("dta_field")), watch=3000, post=hide)
    run("type_abc", A, lambda: {"cmd": "sendstring", "text": "abc"}, pre=focus, post=hide)
    run("return_kbd_hide", A, lambda: {"cmd": "sendstring", "text": "\n"}, watch=3000, pre=focus)
    run("switch_to_settings", ST, lambda: {"cmd": "dlaunch", "bundle": ST}, watch=3000, post=lambda: c.call("dlaunch", bundle=A))
    c.call("dlaunch", bundle=ST); time.sleep(1.5)
    gen = lambda: find(lambda n: n.get("label") == "General" and n["type"] in (75, 9), ST)
    back = lambda: find(lambda n: n["type"] == 9 and n.get("label") in ("Settings", "Back"), ST)
    run("settings_push_general", ST, lambda: tap(gen()), watch=3000, post=lambda: do_tap(back()))
    run("settings_pop", ST, lambda: tap(back()), watch=3000, pre=lambda: do_tap(gen()))
    run("switch_back_app", A, lambda: {"cmd": "dlaunch", "bundle": A}, watch=3000, pre=lambda: c.call("dlaunch", bundle=ST))
json.dump(out, open(sys.argv[2], "w"), indent=1)
