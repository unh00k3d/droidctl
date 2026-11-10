"""Round 2 on a running S2 server: controls, alerts, typing, hit-test, recycled cells, swipe, home.
python3 exp2.py HOST:PORT OUT.json   (ground truth: the test app's dta.log, pulled afterwards)"""
import json
import statistics
import sys
import time

from s2 import Client, walk

APP = "dev.droidctl.s1.testapp"
SB = "com.apple.springboard"
c = Client(sys.argv[1], timeout=120)
out = {"started": time.strftime("%H:%M:%S", time.gmtime())}


def log(k, v):
    out[k] = v
    print(k, json.dumps(v)[:400], flush=True)


def call(cmd, **kw):
    r = c.call(cmd, **kw)
    if not r.get("ok"):
        raise RuntimeError((cmd, kw, r))
    return r


def tree(bundle=APP):
    return call("tree", bundle=bundle)


def nodes(t):
    return [n for n, _ in walk(t["tree"])]


def find(t, pred, what=""):
    hits = [n for n in nodes(t) if pred(n)]
    if not hits:
        raise KeyError(what)
    return hits[0]


def by_id(t, ident):
    return find(t, lambda n: n.get("id") == ident, ident)


def center(n):
    x, y, w, h = n["rect"]
    return x + w / 2, y + h / 2


def act(n, action=2010):
    return call("act", ax=n["ax"], action=action)["result"]


def ms(xs):
    return {"median": round(statistics.median(xs), 1), "min": round(min(xs), 1), "max": round(max(xs), 1), "n": len(xs)}


call("launch", bundle=APP)
time.sleep(1)
t = tree()
json.dump(t["tree"], open("tree2-testapp.json", "w"))
log("testapp_nodes", t["nodes"])

# --- press on more control kinds -----------------------------------------------------------------
press = {}
for ident in ("dta_sui_button", "dta_sui_toggle", "dta_field", "dta_alert"):
    try:
        n = by_id(t, ident)
        press[ident] = {"type": n["type"], "result": act(n), "t": time.strftime("%H:%M:%S", time.gmtime())}
    except KeyError as e:
        press[ident] = {"missing": str(e)}
    time.sleep(1.0)
    if ident == "dta_alert":
        ta = tree()
        alert_btns = [(n.get("label"), n["type"]) for n in nodes(ta) if n["type"] == 9 and n.get("label") in ("OK", "Cancel")]
        press["alert_buttons_in_app_tree"] = alert_btns
        if press[ident].get("result") == "refused":
            # event-gated fallback: hit-test, then touch
            n = by_id(ta, "dta_alert")
            x, y = center(n)
            press["dta_alert_fallback"] = call("fasttap", x=x, y=y)["result"]
            time.sleep(1.0)
            ta = tree()
            alert_btns = [(n.get("label"), n["type"]) for n in nodes(ta) if n["type"] == 9 and n.get("label") in ("OK", "Cancel")]
            press["alert_buttons_after_fallback"] = alert_btns
        try:
            ok = find(ta, lambda n: n["type"] == 9 and n.get("label") == "OK", "OK")
            press["alert_ok_press"] = act(ok)
            press["alert_tree_nodes"] = ta["nodes"]
            if press["alert_ok_press"] != "ok":
                x, y = center(ok)
                hit = call("hit", x=x, y=y).get("ax")
                press["alert_ok_hit_matches"] = hit == ok["ax"]
                press["alert_ok_fallback"] = call("fasttap", x=x, y=y)["result"]
        except KeyError:
            press["alert_ok_press"] = "no OK button"
        time.sleep(1.0)
log("press", press)

# --- table cells: tree coverage, press, identity across scroll (recycling) -----------------------
time.sleep(1.0)
t = tree()
json.dump(t["tree"], open("tree2-testapp-table.json", "w"))
log("testapp_nodes_after_alert", t["nodes"])
cells = [n for n in nodes(t) if n["type"] == 75]  # XCUIElementTypeCell
items = {n.get("label"): n["ax"] for n in nodes(t) if (n.get("label") or "").startswith("Item ")}
log("table_before", {"cells_in_tree": len(cells), "item_labels": len(items), "first": sorted(items, key=lambda s: int(s.split()[1]))[:3],
                     "last": sorted(items, key=lambda s: int(s.split()[1]))[-3:]})
cell_ax = {}
for cnode in cells:
    lab = next((k.get("label") for k, _ in walk(cnode) if (k.get("label") or "").startswith("Item ")), None)
    cell_ax[cnode["ax"]] = lab
if cells:
    log("cell_press_item", {"label": cell_ax[cells[0]["ax"]], "result": act(cells[0])})
    time.sleep(1.0)
table = by_id(t, "dta_table")
log("table_scroll_page", act(table, 2006))
time.sleep(1.0)
t2 = tree()
cells2 = [n for n in nodes(t2) if n["type"] == 75]
cell_ax2 = {}
for cnode in cells2:
    lab = next((k.get("label") for k, _ in walk(cnode) if (k.get("label") or "").startswith("Item ")), None)
    cell_ax2[cnode["ax"]] = lab
reused = {ax: (cell_ax[ax], cell_ax2[ax]) for ax in set(cell_ax) & set(cell_ax2)}
log("table_after_scroll", {"cells_in_tree": len(cells2), "same_ax_same_label": sum(a == b for a, b in reused.values()),
                           "same_ax_different_label": sum(a != b for a, b in reused.values()),
                           "examples": list(reused.values())[:5]})
# act on a cell element captured before the scroll (it may now be recycled for another row)
if cells:
    log("act_on_pre_scroll_cell", {"was": cell_ax[cells[0]["ax"]], "now": cell_ax2.get(cells[0]["ax"]), "result": act(cells[0])})
    time.sleep(1.0)
# swipe-scroll fallback on the table
x, y, w, h = by_id(t2, "dta_table")["rect"]
log("swipe_up", call("swipe", x1=x + w / 2, y1=y + h * 0.8, x2=x + w / 2, y2=y + h * 0.2, duration=0.3)["result"])
time.sleep(1.5)

# --- hit-test + fast tap ---------------------------------------------------------------------------
t = tree()
hits = {}
for ident in ("dta_button", "dta_custom", "dta_switch", "dta_field", "dta_sui_button"):
    n = by_id(t, ident)
    x, y = center(n)
    h = call("hit", x=x, y=y)
    hits[ident] = {"expected": n["ax"], "hit": h.get("ax"), "match": h.get("ax") == n["ax"], "ms": round(h["rtt_ms"], 1), "err": h.get("error")}
log("hit_test", hits)
n = by_id(t, "dta_custom")
x, y = center(n)
log("fasttap_custom", ms([call("fasttap", x=x, y=y)["rtt_ms"] for _ in range(5)]))
time.sleep(1.0)

# --- typing --------------------------------------------------------------------------------------
t = tree()
f = by_id(t, "dta_field")
x, y = center(f)
call("fasttap", x=x, y=y)
time.sleep(1.0)
t = tree()
kb = [n for n in nodes(t) if n["type"] == 20]  # XCUIElementTypeKeyboard
f = by_id(t, "dta_field")
log("field_focus", {"focused": f.get("focused"), "keyboard_in_app_tree": bool(kb)})
r = call("sendstring", text="hello")
log("sendstring", {"result": r["result"], "rtt_ms": round(r["rtt_ms"], 1)})
time.sleep(0.5)
r = call("type", bundle=APP, text=" world")
log("typeText", {"rtt_ms": round(r["rtt_ms"], 1)})
time.sleep(0.5)
f = by_id(tree(), "dta_field")
log("field_value_after_typing", f.get("value"))
r = call("setattr", ax=f["ax"], attr=2006, value="set by ax")
log("setattr_value", {"result": r["result"], "rtt_ms": round(r["rtt_ms"], 1)})
time.sleep(0.5)
log("field_value_after_setattr", by_id(tree(), "dta_field").get("value"))
log("fetch_attrs", call("fetch", ax=f["ax"], attrs=[2006, 2001, 2003]).get("values"))

# --- system permission alert (SpringBoard) ------------------------------------------------------
t = tree()
p = by_id(t, "dta_perm")
x, y = center(p)
call("fasttap", x=x, y=y)
time.sleep(2.0)
sb = tree(SB)
json.dump(sb["tree"], open("tree2-springboard-alert.json", "w"))
alert_buttons = [(n.get("label"), n["ax"]) for n in nodes(sb) if n["type"] == 9 and n.get("label")]
log("springboard_alert_buttons", [a for a, _ in alert_buttons][:12])
deny = next((n for n in nodes(sb) if n["type"] == 9 and (n.get("label") or "").startswith("Don")), None)
if deny:
    log("deny_press", act(deny))
    time.sleep(1.5)
    if "ok" != out["deny_press"]:
        x, y = center(deny)
        log("deny_fallback_tap", call("fasttap", x=x, y=y)["result"])
        time.sleep(1.5)

# --- home + frontmost-app detection ---------------------------------------------------------------
log("home", call("home")["rtt_ms"])
time.sleep(1.5)
h = call("hit", x=195, y=400)
pid = int(h["ax"].split("/")[0]) if h.get("ax") else None
log("front_after_home", {"pid": pid, "bundle": call("bundle_for_pid", pid=pid).get("bundle") if pid else None})
call("activate", bundle=APP)
time.sleep(1.5)
h = call("hit", x=195, y=400)
pid = int(h["ax"].split("/")[0])
log("front_after_activate", {"pid": pid, "bundle": call("bundle_for_pid", pid=pid).get("bundle"), "hit_ms": round(h["rtt_ms"], 1)})
sbt = tree(SB)
log("springboard_tree_while_app_front", sbt["nodes"])
json.dump(out, open(sys.argv[2], "w"), indent=1)
