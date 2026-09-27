"""M5 device-side benchmark: act + settle, gestures, waits, events, IME, toasts.

Runs against the phone with the agent set up and the test app installed.
Ground truth for "how many clicks happened" is the test app's DTA logcat lines,
never the agent's own report.

    .venv/bin/python bench/m5_device.py [-d SERIAL] [--out bench/results/m5-device.json]
"""
import argparse
import datetime
import json
import os
import statistics
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from droidctl import device as dev  # noqa: E402

APP = "dev.droidctl.testapp"


def adb(serial, *args, check=True):
    return subprocess.run([dev.adb_path(), "-s", serial, *args], capture_output=True, text=True,
                          check=check, timeout=30).stdout


def launch(serial, c, scenario):
    adb(serial, "shell", "am", "start", "-n", f"{APP}/.Main", "--es", "s", scenario,
        "--ez", "reset", "true")
    c.wait_for(window=f"s:{scenario}", timeout_ms=5000)
    c.wait_idle(quiet_ms=300, timeout_ms=3000)


def nodes(tree, pkg=APP):
    def walk(n, path=()):
        yield n, path
        for ch in n.get("children", []):
            yield from walk(ch, path + (n,))
    for w in tree["windows"]:
        if w.get("root") and (pkg is None or w.get("pkg") == pkg):
            yield from walk(w["root"])


def find(tree, pred, pkg=APP):
    return next((n for n, _ in nodes(tree, pkg) if pred(n)), None)


def fresh(c, pred, pkg=APP, timeout=5.0):
    """A new tree containing a node matching `pred` (the screen may still be recreating)."""
    deadline = time.monotonic() + timeout
    while True:
        t = c.tree()
        n = find(t, pred, pkg)
        if n is not None or time.monotonic() > deadline:
            return t, n
        time.sleep(0.2)


def dta_mark(serial):
    return adb(serial, "shell", "date '+%m-%d %H:%M:%S.000'").strip()


def dta_since(serial, mark):
    # one shell string, so the timestamp (it contains a space) stays a single argument
    out = adb(serial, "shell", f"logcat -d -v raw -T '{mark}' DTA:I '*:S'", check=False)
    evs = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                evs.append(json.loads(line))
            except ValueError:
                pass
    return evs


def stats(xs):
    xs = sorted(xs)
    if not xs:
        return {}
    return {"n": len(xs), "min": round(xs[0], 1), "median": round(statistics.median(xs), 1),
            "p95": round(xs[min(len(xs) - 1, int(0.95 * len(xs)))], 1), "max": round(xs[-1], 1)}


def first_event_ms(r):
    evs = r.get("events") or []
    return (evs[0]["t"] - r["t0"]) if evs and "t0" in r else None


def bench_counter(serial, c, n=10):
    launch(serial, c, "counter")
    t, _ = fresh(c, lambda x: x.get("text") == "Increment")
    mark = dta_mark(serial)
    host, settle, perform, first, clicked = [], [], [], [], 0
    for _ in range(n):
        btn = find(t, lambda x: x.get("text") == "Increment")
        t0 = time.perf_counter()
        r = c.act(t["dump"], btn["handle"], "click", settle={"quiet_ms": 150, "timeout_ms": 2000})
        host.append((time.perf_counter() - t0) * 1000)
        settle.append(r["settle_ms"])
        perform.append(r["perform_ms"])
        if first_event_ms(r) is not None:
            first.append(first_event_ms(r))
        clicked += bool(r.get("clicked_event"))
        t = r["tree"]
    shown = find(t, lambda x: (x.get("text") or "").startswith("Count:"))
    time.sleep(0.5)
    dta = [e for e in dta_since(serial, mark) if e.get("s") == "counter" and e.get("ev") == "click"]
    return {"host_ms": stats(host), "settle_ms": stats(settle), "perform_ms": stats(perform),
            "first_event_ms": stats(first), "clicked_event": f"{clicked}/{n}",
            "tree_shows": shown and shown.get("text"), "dta_clicks": len(dta),
            "dta_last_n": dta[-1].get("n") if dta else None}


def bench_counter_nosettle(serial, c, n=10):
    launch(serial, c, "counter")
    host = []
    clicked = 0
    for _ in range(n):
        t, btn = fresh(c, lambda x: x.get("text") == "Increment")
        t0 = time.perf_counter()
        r = c.act(t["dump"], btn["handle"], "click")
        host.append((time.perf_counter() - t0) * 1000)
        clicked += bool(r.get("clicked_event"))
    return {"host_ms_incl_click_event_wait": stats(host), "clicked_event": f"{clicked}/{n}"}


def bench_gesture_tap(serial, c, n=5):
    """Gesture taps at a node's bounds centre: device px under the resolution override."""
    launch(serial, c, "counter")
    t, btn = fresh(c, lambda x: x.get("text") == "Increment")
    mark = dta_mark(serial)
    b = btn["bounds"]
    pt = ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
    ms = []
    for _ in range(n):
        r = c.gesture("tap", [pt], settle={"quiet_ms": 150, "timeout_ms": 2000, "tree": False})
        ms.append(r["ms"])
    time.sleep(0.5)
    dta = [e for e in dta_since(serial, mark) if e.get("s") == "counter" and e.get("ev") == "click"]
    return {"point": pt, "bounds": b, "gesture_ms": stats(ms), "dta_clicks": f"{len(dta)}/{n}"}


def bench_settings_nav(serial, c, n=5):
    res = []
    for _ in range(n):
        adb(serial, "shell", "am", "start", "-S", "-n", "com.android.settings/.Settings")
        c.wait_for(text="Display", timeout_ms=5000)
        c.wait_idle(quiet_ms=500, timeout_ms=4000)
        t = c.tree()
        row = None
        for node, path in nodes(t, "com.android.settings"):
            if node.get("text") == "Display":
                row = next((x for x in reversed(path + (node,)) if "clickable" in x.get("flags", [])), node)
                break
        t0 = time.perf_counter()
        r = c.act(t["dump"], row["handle"], "click", settle={"quiet_ms": 150, "timeout_ms": 2000})
        host = (time.perf_counter() - t0) * 1000
        arrived = find(r["tree"], lambda x: x.get("text") in ("Brightness", "Screen zoom and font"),
                       pkg="com.android.settings") is not None
        res.append((host, r["settle_ms"], r["perform_ms"], first_event_ms(r), arrived, r["tree"]["ms"]))
    return {"host_ms": stats([x[0] for x in res]), "settle_ms": stats([x[1] for x in res]),
            "perform_ms": stats([x[2] for x in res]),
            "first_event_ms": stats([x[3] for x in res if x[3] is not None]),
            "new_page_in_settled_tree": f"{sum(x[4] for x in res)}/{n}",
            "tree_dump_ms": stats([x[5] for x in res])}


def bench_wait_idle(serial, c, n=10):
    launch(serial, c, "counter")
    host = []
    for _ in range(n):
        t0 = time.perf_counter()
        r = c.wait_idle(quiet_ms=150, timeout_ms=2000)
        host.append((time.perf_counter() - t0) * 1000)
        assert r["idle"]
    return {"host_ms_on_idle_screen": stats(host)}


def bench_toast(serial, c):
    launch(serial, c, "snackbar_toast")
    t = c.tree()
    btn = find(t, lambda x: "toast" in (x.get("text") or "").lower() and "clickable" in x.get("flags", []))
    if not btn:
        return {"skipped": "no toast button found"}
    since = c.events(since=0, limit=1)["next"]
    r = c.act(t["dump"], btn["handle"], "click")
    t0 = time.perf_counter()
    try:
        w = c.wait_for(toast="", since=since, timeout_ms=3000)
    except Exception as e:  # noqa: BLE001 - report, don't crash the bench
        return {"toast_seen": False, "error": str(e)}
    return {"toast_seen": True, "text": w["toast"].get("text"), "wait_ms": round((time.perf_counter() - t0) * 1000),
            "clicked_event": r.get("clicked_event")}


def bench_ime(serial, c):
    launch(serial, c, "keyboard_toggle")
    t = c.tree()
    field = find(t, lambda x: "editable" in x.get("flags", []))
    if not field:
        return {"skipped": "no editable field"}
    since = c.events(since=0, limit=1)["next"]
    c.act(t["dump"], field["handle"], "click", settle={"quiet_ms": 300, "timeout_ms": 3000, "tree": False})
    time.sleep(0.3)
    shown = c.current()["keyboard"]
    evs = [e for e in c.events(since=since)["events"] if e["type"] == "ime"]
    c.global_action("back", settle={"quiet_ms": 300, "timeout_ms": 3000, "tree": False})
    time.sleep(0.3)
    hidden = not c.current()["keyboard"]
    evs = [e for e in c.events(since=since)["events"] if e["type"] == "ime"]
    return {"keyboard_shown_after_click": shown, "keyboard_hidden_after_back": hidden,
            "ime_events": [e["shown"] for e in evs]}


def bench_subscribe(serial, c):
    launch(serial, c, "counter")
    sub = dev.AgentClient(c.port, timeout=10)
    try:
        sub.subscribe(["clicked", "window_content"])
        t = c.tree()
        btn = find(t, lambda x: x.get("text") == "Increment")
        c.act(t["dump"], btn["handle"], "click")
        got = [e["type"] for e in sub.notifications(timeout=1.5)]
    finally:
        sub.close()
    return {"pushed": got[:10], "clicked_pushed": "clicked" in got}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-d", "--device", default=os.environ.get("ANDROID_SERIAL"))
    ap.add_argument("--out", default="bench/results/m5-device.json")
    ap.add_argument("--only", help="comma-separated section names")
    a = ap.parse_args()
    serial = dev.resolve_serial(a.device)
    c = dev.AgentClient(dev.find_forward(serial), timeout=20)
    info = c.ping()
    res = {"date": datetime.date.today().isoformat(), "device": info["model"], "sdk": info["sdk"],
           "agent": info["version"], "transport": "usb-passthrough-vm",
           "note": "settle = wait for the first event after the action (first_ms 600), then quiet 150 ms"}
    for name, fn in [("counter_act_settle", bench_counter), ("counter_act_no_settle", bench_counter_nosettle),
                     ("gesture_tap_center", bench_gesture_tap), ("settings_nav_act_settle", bench_settings_nav),
                     ("wait_idle", bench_wait_idle), ("toast", bench_toast), ("ime", bench_ime),
                     ("subscribe", bench_subscribe)]:
        if a.only and name not in a.only.split(","):
            continue
        try:
            res[name] = fn(serial, c)
        except Exception as e:  # noqa: BLE001
            res[name] = {"error": f"{type(e).__name__}: {e}"}
        print(name, json.dumps(res[name]), flush=True)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w") as f:
        json.dump(res, f, indent=2)


if __name__ == "__main__":
    main()
