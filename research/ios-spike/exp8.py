"""Round 8 (2026-10-21, iPhone): settle rule v2 (the runner's `settle` command: idles + 100 ms quiet, re-armed by late
change notifications) against polled ground truth (mode P, per-node diffs). Scenarios of settle3.py plus setAttribute.
python3 exp8.py OUT.json ROUNDS"""
import json, sys, time
from s2 import Client, walk
A, ST = "dev.droidctl.s1.testapp", "com.apple.Preferences"
c = Client("127.0.0.1:28100", timeout=180)
rounds = int(sys.argv[2]); out = []
REARM = {"rearm_on": [1018, 1031]} if "--kbd" in sys.argv else {}
def tree(b=A): return c.call("snapfast", bundle=b, timeout=5)["tree"]
def find(pred, b=A): return next((n for n, _ in walk(tree(b)) if pred(n)), None)
def by_id(i, b=A): return find(lambda n: n.get("id") == i, b)
def ctr(n): x, y, w, h = n["rect"]; return x + w / 2, y + h / 2
def tap(n): x, y = ctr(n); return {"cmd": "fasttap", "x": x, "y": y}
def do_tap(n): x, y = ctr(n); c.call("fasttap", x=x, y=y)
def run(k, bundle, mk, watch=2500, pre=None, post=None):
    for mode in ("S", "P"):
        if pre: pre(); time.sleep(1.2)
        do = mk()
        if mode == "S":
            r = c.call("settle", do=do, bundle=bundle, timeout_ms=watch, **REARM)
            rec = {"k": k, "mode": "S", "round": rnd, "settle_ms": round(r["settle_ms"]), "idle": r["idle"], "rearms": r["rearms"],
                   "rounds": r["rounds"], "inner_ms": round(r["inner_ms"]), "inner": r["inner"].get("result"), "events": r["events"]}
            print(f"== {k} [S] settle={rec['settle_ms']} idle={r['idle']} rearms={r['rearms']} rounds={r['rounds']}")
        else:
            r = c.call("watch", do=do, bundle=bundle, watch_ms=watch, poll_ms=1, idles=[])
            sg = [s for s in r.get("sigs", []) if not s.get("pre")]
            rec = {"k": k, "mode": "P", "round": rnd, "changes": [[round(s["t"]), round(s["te"]), s.get("add", [])[:3], s.get("rem", [])[:3]] for s in sg]}
            print(f"   {k} [P] changes at {[(round(s['t']), round(s['te'])) for s in sg]}")
        out.append(rec)
        if post: post()
        time.sleep(1.2)
c.call("axreg", ids=list(range(1000, 1060)) + [3031, 4002])
for rnd in range(rounds):
    print(f"######## round {rnd}")
    c.call("dlaunch", bundle=A); time.sleep(2.5)
    run("noop_press", A, lambda: {"cmd": "act", "ax": by_id("dta_custom")["ax"], "action": 2010})
    run("switch_tap", A, lambda: tap(by_id("dta_switch")))
    run("slider_inc", A, lambda: {"cmd": "act", "ax": by_id("dta_slider")["ax"], "action": 2004})
    ok = lambda: do_tap(find(lambda n: n.get("label") == "OK" and n["type"] == 9))
    run("alert_open", A, lambda: tap(by_id("dta_alert")), post=ok)
    run("alert_ok", A, lambda: tap(find(lambda n: n.get("label") == "OK" and n["type"] == 9)), pre=lambda: do_tap(by_id("dta_alert")))
    run("table_page", A, lambda: {"cmd": "act", "ax": by_id("dta_table")["ax"], "action": 2006}, watch=3500)
    hide = lambda: c.call("sendstring", text="\n")
    focus = lambda: do_tap(by_id("dta_field"))
    run("field_tap_kbd", A, lambda: tap(by_id("dta_field")), watch=3000, post=hide)
    run("setattr_kbd", A, lambda: {"cmd": "setattr", "ax": by_id("dta_field")["ax"], "attr": 2006, "value": f"v{rnd}"}, watch=3000, post=hide)
    run("type_abc", A, lambda: {"cmd": "sendstring", "text": "abc"}, pre=focus, post=hide)
    run("return_kbd_hide", A, lambda: {"cmd": "sendstring", "text": "\n"}, watch=3000, pre=focus)
    run("switch_to_settings", ST, lambda: {"cmd": "dlaunch", "bundle": ST}, watch=3000, post=lambda: c.call("dlaunch", bundle=A))
    c.call("dlaunch", bundle=ST); time.sleep(1.5)
    gen = lambda: find(lambda n: n.get("label") == "General" and n["type"] in (75, 9), ST)
    back = lambda: find(lambda n: n["type"] == 9 and n.get("label") in ("Settings", "Back"), ST)
    run("settings_push_general", ST, lambda: tap(gen()), watch=3000, post=lambda: do_tap(back()))
    run("settings_pop", ST, lambda: tap(back()), watch=3000, pre=lambda: do_tap(gen()))
    run("switch_back_app", A, lambda: {"cmd": "dlaunch", "bundle": A}, watch=3000, pre=lambda: c.call("dlaunch", bundle=ST))
json.dump(out, open(sys.argv[1], "w"), indent=1)
