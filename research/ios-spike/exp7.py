"""Round 7 (2026-10-21, iPhone): the IOS-HANDOFF §11 open measurements.
python3 exp7.py SECTION [rounds]   SECTION: names | focus | hang | long | web
Writes results/exp7-SECTION.json (timings and event metadata only; no third-party app trees)."""
import json, os, re, subprocess, sys, time
from s2 import Client, walk
A, ST, SF = "dev.droidctl.s1.testapp", "com.apple.Preferences", "com.apple.mobilesafari"
Q = "com.example.app"
c = Client("127.0.0.1:28100", timeout=180)
CDID = open(os.path.join(os.path.dirname(__file__), ".cdid")).read().strip()
DEV = dict(os.environ, DEVELOPER_DIR="/Users/enesb/Downloads/Xcode.app/Contents/Developer")
out = []
def log(k, v): print(k, json.dumps(v)[:400]); out.append({"k": k, "v": v})
def tree(b=A, **kw): return c.call("snapfast", bundle=b, **kw)
def find(pred, b=A): return next((n for n, _ in walk(tree(b)["tree"]) if pred(n)), None)
def by_id(i, b=A): return find(lambda n: n.get("id") == i, b)
def ctr(n): x, y, w, h = n["rect"]; return x + w / 2, y + h / 2
def evs(r, keep=lambda e: True):
    return [(round(e["t"]), e["n"], e["el"], e["p"][:70]) for e in r.get("events", []) if keep(e)]
def idles(r): return {i["kind"]: round(i.get("end", -1)) for i in r.get("idles", [])}
def watch(do, bundle=A, ms=3000, poll=0):
    return c.call("watch", do=do, bundle=bundle, watch_ms=ms, poll_ms=poll, idles=[] if poll else ["runloop", "anim"])
def pid_of(bundle):
    p = subprocess.run(["xcrun", "devicectl", "device", "info", "processes", "--device", CDID, "--json-output", "/dev/stdout"],
                       capture_output=True, text=True, env=DEV).stdout
    j = json.loads(p[p.index("{"):])
    for x in j["result"]["runningProcesses"]:
        if bundle.split(".")[-1].lower() in x.get("executable", "").lower(): return x["processIdentifier"]
def terminate(bundle):
    pid = pid_of(bundle)
    if pid: subprocess.run(["xcrun", "devicectl", "device", "process", "terminate", "--device", CDID, "--pid", str(pid)], capture_output=True, env=DEV)
    return pid
CHANGE = {1000, 1001, 1006, 1008, 1009, 1017, 1018, 1020, 1021, 1025, 1031, 3031}
def settle_from(r, quiet=100):
    t = max([i.get("end", 1e9) for i in r["idles"]] + [r["inner_ms"]])
    for e in sorted(r["events"], key=lambda e: e["t"]):
        ch = e["n"] in CHANGE or (e["n"] == 4002 and any(k in e["p"] for k in ("ViewDidAppear", "ViewDidDisappear", "KeyEventComplete")))
        if ch and t < e["t"] <= t + quiet: t = e["t"]
    return round(t + quiet)
sec = sys.argv[1]; rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 3
c.call("axreg", ids=list(range(1000, 1060)) + [3031, 4002])

if sec == "names":
    names = ["UIAccessibilityScreenChangedNotification", "UIAccessibilityLayoutChangedNotification", "UIAccessibilityAnnouncementNotification",
             "UIAccessibilityAnnouncementDidFinishNotification", "UIAccessibilityPageScrolledNotification",
             "UIAccessibilityPauseAssistiveTechnologyNotification", "UIAccessibilityResumeAssistiveTechnologyNotification",
             "UIAccessibilityElementFocusedNotification"]
    log("syms", c.call("syms", names=names)["values"])
    log("xct_names", c.call("axnames", **{"from": 0, "to": 5200})["names"])

elif sec == "focus":
    # Focus and keyboard signals around setAttribute value, a tap on the field, and return.
    c.call("dlaunch", bundle=A); time.sleep(2)
    app = c.call("appax", bundle=A)["ax"]
    KB = ["XC_kAXXCAttributeResponderElement", "XC_kAXXCAttributeActiveKeyboard"]
    for rnd in range(rounds):
        f = by_id("dta_field")
        t = time.perf_counter(); v = c.call("fetch", ax=app, attrs=KB); log("fetch_before", {"values": v.get("values"), "err": v.get("error"), "rtt": round((time.perf_counter() - t) * 1000, 1), "field": f["ax"]})
        r = watch({"cmd": "setattr", "ax": f["ax"], "attr": 2006, "value": f"r{rnd}abc"}, ms=2500)
        log("setattr", {"inner": r["inner"].get("result"), "inner_ms": round(r["inner_ms"]), "idles": idles(r), "settle": settle_from(r),
                        "ev": evs(r, lambda e: e["n"] in (1018, 1031, 1008, 1017, 1000, 1053))})
        t = time.perf_counter(); v = c.call("fetch", ax=app, attrs=KB); log("fetch_after_setattr", {"values": v.get("values"), "rtt": round((time.perf_counter() - t) * 1000, 1)})
        kbd = find(lambda n: n["type"] == 19)
        log("keyboard_in_tree", {"present": bool(kbd), "rect": kbd and kbd["rect"]})
        # Dismiss with return (the test app's field resigns on return).
        r = watch({"cmd": "sendstring", "text": "\n"}, ms=2500)
        log("return", {"inner": r["inner"].get("result"), "settle": settle_from(r), "ev": evs(r, lambda e: e["n"] in (1018, 1031, 1053))})
        v = c.call("fetch", ax=app, attrs=KB); log("fetch_after_return", v.get("values"))
        # Focus by touch, for comparison.
        x, y = ctr(by_id("dta_field"))
        r = watch({"cmd": "fasttap", "x": x, "y": y}, ms=2500)
        log("tap_field", {"inner_ms": round(r["inner_ms"]), "settle": settle_from(r), "ev": evs(r, lambda e: e["n"] in (1018, 1031, 1053))})
        r = watch({"cmd": "sendstring", "text": "\n"}, ms=2000)
        log("return2", {"ev": evs(r, lambda e: e["n"] in (1018, 1031))})
        time.sleep(0.5)

elif sec == "hang":
    # Does a snapshot taken right after bringing an app to the front hang, and does waiting for run-loop idle first avoid it?
    targets = [A, ST] + ([Q] if "--qa" in sys.argv else [])
    for rnd in range(rounds):
        for b in targets:
            for variant in ("direct", "idle_first"):
                other = ST if b != ST else A
                c.call("dlaunch", bundle=other); time.sleep(1.5)
                t0 = time.perf_counter()
                lr = c.call("dlaunch", bundle=b)
                rec = {"b": b.split(".")[-1], "variant": variant, "rnd": rnd, "dlaunch_ms": round((time.perf_counter() - t0) * 1000)}
                if variant == "idle_first":
                    # The standalone `idle` command never gets its reply (unexplained); `settle` around a no-op does.
                    ir = c.call("settle", do={"cmd": "stats"}, bundle=b, timeout_ms=5000, rearm=0)
                    rec["idle"] = ir["idle"]; rec["idle_ms"] = round(ir["settle_ms"])
                tries = []
                for attempt in range(3):
                    s = tree(b, timeout=5)
                    tries.append({"ok": s["ok"], "ms": round(s.get("snapshot_ms", 0)), "nodes": s.get("nodes"), "err": s.get("error")})
                    if s["ok"]: break
                rec["tries"] = tries; rec["total_ms"] = round((time.perf_counter() - t0) * 1000)
                log("hang", rec)

elif sec == "long":
    # Long transitions: cold launches and network-bound screens. Mode E (events + idles) and mode P (polled diffs) separately.
    def cold(b): return lambda: (terminate(b), time.sleep(1.5))
    scen = [("cold_settings", ST, cold(ST)), ("cold_testapp", A, cold(A)), ("cold_safari", SF, cold(SF))]
    if "--qa" in sys.argv: scen.append(("cold_qa", Q, cold(Q)))
    for rnd in range(rounds):
        for k, b, pre in scen:
            for mode in ("E", "P"):
                pre()
                r = watch({"cmd": "dlaunch", "bundle": b}, bundle=b, ms=9000, poll=0 if mode == "E" else 1)
                if mode == "E":
                    last = max([e["t"] for e in r["events"] if e["n"] in CHANGE or (e["n"] == 4002 and "View" in e["p"])] or [0])
                    by_n = {}
                    for e in r["events"]: by_n.setdefault(e["n"], []).append(round(e["t"]))
                    log("long", {"k": k, "mode": "E", "rnd": rnd, "inner_ms": round(r["inner_ms"]), "idles": idles(r), "settle_rule": settle_from(r),
                                 "last_change_ev": round(last), "count_by_n": {n: len(v) for n, v in by_n.items()}, "last_by_n": {n: v[-1] for n, v in by_n.items()}})
                else:
                    sg = [s for s in r.get("sigs", []) if not s.get("pre")]
                    log("long", {"k": k, "mode": "P", "rnd": rnd, "changes": [round(s["t"]) for s in sg], "snap_ms": [round(s["ms"]) for s in sg][:12],
                                 "last_diff": (sg[-1].get("add", [])[:2] + sg[-1].get("rem", [])[:2]) if sg and b != Q else None})
                time.sleep(1)

elif sec == "web":
    # When does WKWebView content appear after a cold launch, and does any notification mark it?
    for rnd in range(rounds):
        terminate(A); time.sleep(1.5)
        r = watch({"cmd": "dlaunch", "bundle": A}, ms=4000)
        webpids = sorted({e["el"].split("/")[0] for e in r["events"]} - {"34"})
        log("web_E", {"rnd": rnd, "idles": idles(r), "settle": settle_from(r),
                      "ev": [x for x in evs(r) if x[1] != 4002 or "View" in x[3]][:60], "pids": webpids})
        terminate(A); time.sleep(1.5)
        # Host-side polling truth: time until the web Button appears.
        t0 = time.perf_counter(); c.call("dlaunch", bundle=A); seen = None; polls = []
        while time.perf_counter() - t0 < 6:
            s = tree(A, timeout=5)
            el = round((time.perf_counter() - t0) * 1000)
            hit = s["ok"] and any(n.get("label") == "Web Button" for n, _ in walk(s["tree"]))
            polls.append((el, s.get("snapshot_ms") and round(s["snapshot_ms"]), hit))
            if hit: seen = el; break
        log("web_P", {"rnd": rnd, "web_button_seen_ms": seen, "polls": polls})

json.dump(out, open(os.path.join(os.path.dirname(__file__), f"results/exp7-{sec}.json"), "w"), indent=1)
