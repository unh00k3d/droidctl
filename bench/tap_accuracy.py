"""Tap accuracy: does a tap land on the element that was meant?

    .venv/bin/python bench/tap_accuracy.py -d SERIAL [--only droidctl|baseline]
        [--targets all|name,...] [--out bench/results/tap-accuracy.json]

~50 targets: test-app rows, icons with child text, duplicates, grids, Compose,
virtual views, unlabeled icons (graded by the app's DTA logcat events: exactly
the intended event, nothing else), plus real Settings rows and launcher icons
(graded by `dumpsys activity top` / the focused window, not by droidctl).

Two methods on the same targets:
  droidctl  `droidctl snapshot`, then `droidctl tap <locator>`, the way an agent
            would (a ref from the snapshot, or a text/desc/id locator with
            --right-of/--below for repeated labels).
  baseline  "mobile-mcp style": a re-implementation of mobile-mcp 1.0.5's
            Android legacy robot (lib/android.js: `uiautomator dump`,
            collectElements(), then `input tap` at the rect centre), with the
            element chosen the way an agent would from that list (exact
            text/content-desc/hint/resource-id; for repeated labels, the match
            nearest to the anchor text). mobile-mcp itself is NOT run: its
            device path now requires the separate `mobilecli` binary, which can
            install an agent on the device; we don't put that on the user's
            phone. This measures the element-list-plus-coordinate-tap approach,
            not mobile-mcp's current release.

`uiautomator dump` is a UiAutomation client and suppresses accessibility
services while it runs, so all droidctl taps run first; afterwards the script
checks that the droidctl agent answers again (recovery) and records it.
"""
import argparse
import datetime
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import testapp as T  # noqa: E402


def tgt(name, scenario, dc, pick, expect, cat):
    return {"name": name, "scenario": scenario, "dc": dc, "pick": pick, "expect": expect, "cat": cat}


def build_targets():
    ts = []
    people = ["Ada Lovelace", "Alan Turing", "Grace Hopper", "Edsger Dijkstra", "Barbara Liskov"]
    for i, who in enumerate(people):
        ts.append(tgt(f"row:{who}", "row_nested", ["tap", "--text", who], {"text": who},
                      {"ev": "click", "id": f"row{i}"}, "row (text inside a clickable row)"))
        ts.append(tgt(f"star:{who}", "row_nested", ["tap", "--desc", "Star", "--right-of", who],
                      {"text": "Star", "anchor": who}, {"ev": "click", "id": f"star{i}"}, "repeated icon in a row"))
    for i, who in enumerate(people[:3]):
        ts.append(tgt(f"crow:{who}", "row_nested_compose", ["tap", "--text", who], {"text": who},
                      {"ev": "click", "id": f"row{i}"}, "compose"))
        ts.append(tgt(f"cstar:{who}", "row_nested_compose", ["tap", "--desc", "Star", "--right-of", who],
                      {"text": "Star", "anchor": who}, {"ev": "click", "id": f"star{i}"}, "compose"))
    for n in (1, 3, 5, 7):
        ts.append(tgt(f"delete:Item {n}", "duplicates", ["tap", "--text", "Delete", "--right-of", f"Item {n}"],
                      {"text": "Delete", "anchor": f"Item {n}"}, {"ev": "delete", "row": n}, "duplicates"))
    for item in ("Wireless Mouse", "USB-C Cable", "Laptop Stand"):
        ts.append(tgt(f"plus:{item}", "cart", ["tap", "--text", "+", "--right-of", item],
                      {"text": "+", "anchor": item}, {"ev": "inc", "id": item}, "duplicates"))
        ts.append(tgt(f"minus:{item}", "cart", ["tap", "--text", "−", "--right-of", item],
                      {"text": "−", "anchor": item}, {"ev": "dec", "id": item}, "duplicates"))
    for plan in ("Basic", "Standard", "Premium", "Enterprise"):
        ts.append(tgt(f"buy:{plan}", "cards", ["tap", "--text", "Buy", "--below", plan],
                      {"text": "Buy", "anchor": plan}, {"ev": "buy", "plan": plan}, "duplicates"))
    for d in (3, 14, 27):
        ts.append(tgt(f"day:{d}", "calendar", ["tap", "--text", str(d)], {"text": str(d)},
                      {"ev": "click", "id": f"day{d}"}, "grid"))
    for d in (5, 20):
        ts.append(tgt(f"vday:{d}", "virtual_views", ["tap", "--desc", f"October {d}"], {"text": f"October {d}"},
                      {"ev": "click", "id": f"day{d}"}, "virtual views"))
    for k in ("1", "5", "9", "0"):
        ts.append(tgt(f"key:{k}", "keypad", ["tap", "--text", k], {"text": k},
                      {"ev": "key", "digit": k}, "grid"))
    ts += [
        tgt("save", "buttons", ["tap", "--text", "Save"], {"text": "Save"}, {"ev": "click", "id": "save"}, "button"),
        tgt("settings-icon", "buttons", ["tap", "--desc", "Settings"], {"text": "Settings"},
            {"ev": "click", "id": "settings"}, "icon"),
        tgt("csave", "buttons_compose", ["tap", "--text", "Save"], {"text": "Save"}, {"ev": "click", "id": "save"}, "compose"),
        tgt("csettings-icon", "buttons_compose", ["tap", "--desc", "Settings"], {"text": "Settings"},
            {"ev": "click", "id": "settings"}, "compose"),
        tgt("cincrement", "counter_compose", ["tap", "--text", "Increment"], {"text": "Increment"},
            {"ev": "click", "id": "inc"}, "compose"),
        tgt("share-icon", "unlabeled_icons", ["tap", "--id", "ic_share"], {"id": "ic_share"},
            {"ev": "click", "id": "ic_share"}, "unlabeled icon"),
        tgt("delete-icon", "unlabeled_icons", ["tap", "--id", "ic_delete"], {"id": "ic_delete"},
            {"ev": "click", "id": "ic_delete"}, "unlabeled icon"),
    ]
    return ts


# Real apps: graded by the system's own dumpsys, restored before every tap.
REAL = [
    {"name": "settings:Display", "start": "settings", "dc": ["tap", "--text", "Display"], "pick": {"text": "Display"},
     "top": "DisplaySettings", "cat": "real app"},
    {"name": "settings:Notifications", "start": "settings", "dc": ["tap", "--text", "Notifications"],
     "pick": {"text": "Notifications"}, "top": "Notification", "cat": "real app"},
    {"name": "settings:Sounds and vibration", "start": "settings", "dc": ["tap", "--text", "Sounds and vibration"],
     "pick": {"text": "Sounds and vibration"}, "top": "Sound", "cat": "real app"},
    {"name": "settings:Lock screen", "start": "settings", "dc": ["tap", "--text", "Lock screen"],
     "pick": {"text": "Lock screen"}, "top": "LockScreen", "cat": "real app"},
    {"name": "launcher:Play Store", "start": "home", "dc": ["tap", "--text", "Play Store"],
     "pick": {"text": "Play Store"}, "focus": "com.android.vending", "cat": "real app"},
    {"name": "launcher:Camera", "start": "home", "dc": ["tap", "--text", "Camera"],
     "pick": {"text": "Camera"}, "focus": "camera", "cat": "real app"},
]


# --------------------------------------------------------------------------
# grading
# --------------------------------------------------------------------------
def matches(e, expect):
    return all(e.get(k) == v for k, v in expect.items())


def grade_events(events, expect):
    ev = T.interactions(events)
    hits = [e for e in ev if matches(e, expect)]
    others = [e for e in ev if not matches(e, expect)]
    if len(hits) == 1 and not others:
        return True, "ok"
    if not hits and not others:
        return False, "nothing happened"
    if others:
        return False, "wrong target: " + json.dumps(others[0], separators=(",", ":"))[:120]
    return False, f"fired {len(hits)} times"


# --------------------------------------------------------------------------
# the mobile-mcp-style baseline (lib/android.js collectElements + centre tap)
# --------------------------------------------------------------------------
def parse_bounds(b):
    import re
    m = re.match(r"^\[(\d+),(\d+)\]\[(\d+),(\d+)\]$", b or "")
    if not m:
        return None
    l, t, r, bt = map(int, m.groups())
    return {"x": l, "y": t, "width": r - l, "height": bt - t}


def collect_elements(xml_text):
    """mobile-mcp's collectElements(): nodes with text, content-desc, hint,
    resource-id or checkable, with a positive-area rect, in document order."""
    root = ET.fromstring(xml_text[xml_text.index("<?xml"):] if "<?xml" in xml_text else xml_text)
    out = []

    def walk(n):
        for c in n.findall("node"):
            walk(c)
        a = n.attrib
        if a.get("text") or a.get("content-desc") or a.get("hint") or a.get("resource-id") or a.get("checkable") == "true":
            rect = parse_bounds(a.get("bounds"))
            if rect and rect["width"] > 0 and rect["height"] > 0:
                el = {"type": a.get("class") or "text", "text": a.get("text", ""),
                      "label": a.get("content-desc") or a.get("hint") or "", "rect": rect}
                if a.get("resource-id"):
                    el["identifier"] = a["resource-id"]
                out.append(el)
    walk(root)
    return out


def centre(el):
    r = el["rect"]
    return (r["x"] + r["width"] // 2, r["y"] + r["height"] // 2)


def pick_element(elements, pick):
    """Choose like an agent reading mobile-mcp's list: exact text/label/id; for a
    repeated label, the match nearest to the anchor text."""
    if "id" in pick:
        cands = [e for e in elements if (e.get("identifier") or "").endswith(":id/" + pick["id"])]
    else:
        cands = [e for e in elements if e["text"] == pick["text"] or e["label"] == pick["text"]]
    if not cands:
        return None, "no element with that text/label"
    if len(cands) == 1 or "anchor" not in pick:
        return cands[0], "first match" if len(cands) > 1 else "unique"
    anchors = [e for e in elements if e["text"] == pick["anchor"] or e["label"] == pick["anchor"]]
    if not anchors:
        return cands[0], "anchor not found: first match"
    ax, ay = centre(anchors[0])
    return min(cands, key=lambda e: math.dist(centre(e), (ax, ay))), "nearest to anchor"


# --------------------------------------------------------------------------
# running
# --------------------------------------------------------------------------
def start_real(serial, how):
    if how == "settings":
        T.adb(serial, "shell", "am", "force-stop", "com.android.settings")
        T.adb(serial, "shell", "am", "start", "-n", "com.android.settings/.Settings")
    else:
        T.adb(serial, "shell", "input", "keyevent", "KEYCODE_HOME")
        time.sleep(0.6)
        T.adb(serial, "shell", "input", "keyevent", "KEYCODE_HOME")
    time.sleep(2.0)


def grade_real(serial, t):
    time.sleep(1.5)
    if "top" in t:
        top = T.top_fragments(serial)
        return (t["top"].lower() in top.lower()), f"top has {t['top']!r}" if t["top"].lower() in top.lower() else "not on the expected page"
    f = T.focus(serial)
    return (t["focus"] in f), f


def dc_json(env, *argv, timeout=120):
    r = subprocess.run([T.VENV_DROIDCTL, *argv, "--json"], env=env, capture_output=True, text=True, timeout=timeout)
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {"ok": False, "error": {"kind": "no-json", "message": (r.stdout + r.stderr)[-300:]}}


def run_droidctl(serial, t, env):
    real = "start" in t
    if real:
        start_real(serial, t["start"])
    else:
        T.launch(serial, t["scenario"])
    dc_json(env, "snapshot")
    t0 = time.monotonic()
    res = dc_json(env, *t["dc"])
    ms = (time.monotonic() - t0) * 1000
    if not res.get("ok", True) and "error" in res:
        ok, why = False, f"{res['error'].get('kind')}: {res['error'].get('message', '')[:100]}"
        if real:
            ok2, _ = grade_real(serial, t)
            why += " (and the page " + ("did" if ok2 else "did not") + " change)"
    elif real:
        ok, why = grade_real(serial, t)
    else:
        time.sleep(0.3)
        ok, why = grade_events(T.dta(serial, t["scenario"]), t["expect"])
    return {"target": t["name"], "cat": t["cat"], "method": "droidctl", "ok": ok, "why": why,
            "tap_method": res.get("method"), "ms": round(ms)}


def run_baseline(serial, t):
    real = "start" in t
    if real:
        start_real(serial, t["start"])
    else:
        T.launch(serial, t["scenario"])
    t0 = time.monotonic()
    xml_text = ""
    for _ in range(10):                                   # mobile-mcp retries a null root 10 times
        xml_text = T.adb(serial, "exec-out", "uiautomator", "dump", "/dev/tty", check=False)
        if "null root node" not in xml_text and "<?xml" in xml_text:
            break
    try:
        els = collect_elements(xml_text[:xml_text.rindex(">") + 1])
    except (ValueError, ET.ParseError) as e:
        return {"target": t["name"], "cat": t["cat"], "method": "baseline", "ok": False, "why": f"dump failed: {e}"}
    el, how = pick_element(els, t["pick"])
    if el is None:
        return {"target": t["name"], "cat": t["cat"], "method": "baseline", "ok": False, "why": how}
    x, y = centre(el)
    T.adb(serial, "shell", "input", "tap", str(x), str(y))
    ms = (time.monotonic() - t0) * 1000
    if real:
        ok, why = grade_real(serial, t)
    else:
        time.sleep(1.0)                                   # mobile-mcp has no settle; an agent's next turn is later
        ok, why = grade_events(T.dta(serial, t["scenario"]), t["expect"])
    return {"target": t["name"], "cat": t["cat"], "method": "baseline", "ok": ok, "why": why,
            "pick": how, "at": [x, y], "ms": round(ms)}


def summarize(rows):
    out = {}
    for m in ("droidctl", "baseline"):
        rs = [r for r in rows if r["method"] == m]
        if not rs:
            continue
        cats = {}
        for r in rs:
            c = cats.setdefault(r["cat"], [0, 0])
            c[0] += r["ok"]
            c[1] += 1
        out[m] = {"correct": sum(r["ok"] for r in rs), "total": len(rs),
                  "accuracy": round(sum(r["ok"] for r in rs) / len(rs), 3),
                  "by_category": {k: f"{a}/{b}" for k, (a, b) in sorted(cats.items())},
                  "failures": [f"{r['target']}: {r['why']}" for r in rs if not r["ok"]]}
    return out


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("-d", "--device", default=os.environ.get("ANDROID_SERIAL"), required=False)
    p.add_argument("--only", choices=["droidctl", "baseline"])
    p.add_argument("--targets", default="all")
    p.add_argument("--no-real", action="store_true")
    p.add_argument("--out", default=os.path.join(HERE, "results", "tap-accuracy.json"))
    a = p.parse_args(argv)
    if not a.device:
        p.error("-d SERIAL is required")
    targets = build_targets() + ([] if a.no_real else REAL)
    if a.targets != "all":
        want = set(a.targets.split(","))
        targets = [t for t in targets if t["name"] in want]
    home = tempfile.mkdtemp(prefix="dcbench-tap-")
    env = dict(os.environ, DROIDCTL_HOME=home, ANDROID_SERIAL=a.device)
    rows = []
    try:
        if a.only in (None, "droidctl"):
            for t in targets:
                r = run_droidctl(a.device, t, env)
                rows.append(r)
                print(f"droidctl {t['name']:28} {'ok ' if r['ok'] else 'MISS'} {r['why'][:70]}", file=sys.stderr, flush=True)
            subprocess.run([T.VENV_DROIDCTL, "daemon", "stop"], env=env, capture_output=True)
        recovery = None
        if a.only in (None, "baseline"):
            for t in targets:
                r = run_baseline(a.device, t)
                rows.append(r)
                print(f"baseline {t['name']:28} {'ok ' if r['ok'] else 'MISS'} {r['why'][:70]}", file=sys.stderr, flush=True)
            # uiautomator suppressed our service while it ran: does it come back?
            t0 = time.monotonic()
            while time.monotonic() - t0 < 30:
                r = dc_json(dict(env, DROIDCTL_NO_DAEMON="1"), "ping")
                if r.get("ok"):
                    recovery = {"ok": True, "after_s": round(time.monotonic() - t0, 1)}
                    break
                time.sleep(1)
            else:
                recovery = {"ok": False, "last": r}
    finally:
        subprocess.run([T.VENV_DROIDCTL, "daemon", "stop"], env=env, capture_output=True)
        T.adb(a.device, "shell", "input", "keyevent", "KEYCODE_HOME", check=False)
    result = {"date": datetime.datetime.now().isoformat(timespec="seconds"), "device": "SM-N950F (API 28)",
              "baseline": "mobile-mcp-style re-implementation (mobile-mcp 1.0.5 lib/android.js), not mobile-mcp itself",
              "summary": summarize(rows), "agent_recovery_after_uiautomator": recovery, "rows": rows}
    if a.out:
        with open(a.out, "w") as f:
            json.dump(result, f, indent=2)
    print(json.dumps({"summary": result["summary"], "recovery": recovery}, indent=2))
    return result


if __name__ == "__main__":
    main()
