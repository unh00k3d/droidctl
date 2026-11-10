"""S3 latency + S4 identity on a running S2 server.  python3 bench.py HOST:PORT OUT.json"""
import json
import statistics
import sys
import time

from s2 import Client, walk

APP = "dev.droidctl.s1.testapp"
SETTINGS = "com.apple.Preferences"
c = Client(sys.argv[1], timeout=120)
out = {}


def stats(xs):
    xs = sorted(xs)
    return {"n": len(xs), "min": round(xs[0], 1), "median": round(statistics.median(xs), 1), "max": round(xs[-1], 1)}


def must(r):
    if not r.get("ok"):
        raise RuntimeError(r)
    return r


def tree(bundle):
    return must(c.call("tree", bundle=bundle))


def by_label(t):
    """label/id -> ax key, for leaves with a unique label."""
    m, dup = {}, set()
    for n, _ in walk(t["tree"]):
        k = n.get("id") or n.get("label")
        if not k or "ax" not in n:
            continue
        if k in m:
            dup.add(k)
        m[k] = n["ax"]
    return {k: v for k, v in m.items() if k not in dup}


def find(t, ident):
    for n, _ in walk(t["tree"]):
        if n.get("id") == ident:
            return n
    raise KeyError(ident)


def compare(a, b):
    ma, mb = by_label(a), by_label(b)
    common = set(ma) & set(mb)
    same = sum(ma[k] == mb[k] for k in common)
    return {"common": len(common), "same_ax": same, "changed_ax": len(common) - same}


# --- S3: cold-ish launch, tree latency --------------------------------------------------------
t0 = time.perf_counter()
must(c.call("launch", bundle=APP))
out["launch_testapp_ms"] = round((time.perf_counter() - t0) * 1000)
for bundle in (APP, SETTINGS):
    if bundle == SETTINGS:
        t0 = time.perf_counter()
        must(c.call("launch", bundle=SETTINGS))
        out["launch_settings_ms"] = round((time.perf_counter() - t0) * 1000)
    rs = [tree(bundle) for _ in range(10)]
    out[f"tree_{bundle}"] = {"nodes": rs[-1]["nodes"], "snapshot_ms": stats([r["snapshot_ms"] for r in rs]),
                             "rtt_ms": stats([r["rtt_ms"] for r in rs]),
                             "bytes": len(json.dumps(rs[-1]["tree"]))}
    json.dump(rs[-1]["tree"], open(f"tree-{bundle}.json", "w"))
    print(bundle, out[f"tree_{bundle}"], flush=True)

ss = [must(c.call("screenshot")) for _ in range(5)]
out["screenshot"] = {"rtt_ms": stats([r["rtt_ms"] for r in ss]), "bytes": ss[-1]["bytes"]}
print("screenshot", out["screenshot"], flush=True)

# --- act latency + S4 identity in the test app -------------------------------------------------
must(c.call("activate", bundle=APP))
t1 = tree(APP)
custom = find(t1, "dta_custom")["ax"]
acts = [must(c.call("act", ax=custom, action=2010)) for _ in range(10)]
out["act_press_custom"] = {"rtt_ms": stats([r["rtt_ms"] for r in acts]), "results": sorted({r["result"] for r in acts})}
print("act", out["act_press_custom"], flush=True)

taps = []
for _ in range(5):
    n = find(t1, "dta_custom")
    x, y, w, h = n["rect"]
    taps.append(must(c.call("tap", bundle=APP, x=x + w / 2, y=y + h / 2))["rtt_ms"])
out["tap_coordinate"] = {"rtt_ms": stats(taps)}
print("tap", out["tap_coordinate"], flush=True)

ident = {}
t_again = tree(APP)
ident["same_screen_reread"] = compare(t1, t_again)
scroll = find(t1, "dta_scroll")["ax"]
old_rows = {n["label"]: n["ax"] for n, _ in walk(t1["tree"]) if n.get("label", "").startswith("Row ")}
r = must(c.call("act", ax=scroll, action=2006))
time.sleep(1)
t2 = tree(APP)
ident["after_scroll_page"] = compare(t1, t2)
ident["scroll_result"] = r["result"]
new_rows = {n["label"]: n["ax"] for n, _ in walk(t2["tree"]) if n.get("label", "").startswith("Row ")}
ident["rows_before"] = len(old_rows)
ident["rows_after"] = len(new_rows)
ident["rows_same_ax"] = sum(old_rows.get(k) == v for k, v in new_rows.items())
# act on an element from before the scroll (Row 1 is now scrolled away, if it was visible before)
if "Row 1" in old_rows:
    ident["act_on_scrolled_away_row1"] = c.call("act", ax=old_rows["Row 1"], action=2010)
# value change (switch through a tap) then compare
sw = find(t2, "dta_switch")
x, y, w, h = sw["rect"]
must(c.call("tap", bundle=APP, x=x + w / 2, y=y + h / 2))
time.sleep(0.5)
t3 = tree(APP)
ident["after_switch_toggle"] = compare(t2, t3)
ident["switch_ax_before_after"] = [sw["ax"], find(t3, "dta_switch")["ax"]]
# app relaunch
must(c.call("launch", bundle=APP))
t4 = tree(APP)
ident["after_relaunch"] = compare(t3, t4)
ident["act_on_dead_pid_element"] = c.call("act", ax=custom, action=2010)
# Settings: into General and back
must(c.call("activate", bundle=SETTINGS))
s1 = tree(SETTINGS)
gen = [n for n, _ in walk(s1["tree"]) if n.get("label") == "General" and n.get("type") in (9, 75)]
ident["settings_general_candidates"] = [(n["type"], n["ax"]) for n in gen]
if gen:
    x, y, w, h = gen[0]["rect"]
    must(c.call("tap", bundle=SETTINGS, x=x + w / 2, y=y + h / 2))
    time.sleep(1.5)
    s_gen = tree(SETTINGS)
    json.dump(s_gen["tree"], open("tree-settings-general.json", "w"))
    back = [n for n, _ in walk(s_gen["tree"]) if n.get("type") == 9 and n.get("label") in ("Settings", "Back")]
    if back:
        x, y, w, h = back[0]["rect"]
        must(c.call("tap", bundle=SETTINGS, x=x + w / 2, y=y + h / 2))
        time.sleep(1.5)
    s2 = tree(SETTINGS)
    ident["settings_after_push_pop"] = compare(s1, s2)
out["identity"] = ident
print(json.dumps(ident, indent=1), flush=True)
json.dump(out, open(sys.argv[2], "w"), indent=1)
