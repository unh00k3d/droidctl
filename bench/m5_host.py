"""M5 host-side latency: tap + settle on a static target, in-process and cold CLI.

    .venv/bin/python bench/m5_host.py -d SERIAL [--count 10] [--out bench/results/m5-host.json]

Uses the test app's `counter` scenario (a static screen; each tap changes one
line). In-process = one warm Session (what the daemon will see); cold = a new
`python -m droidctl tap … --json` process per tap (what a shell agent sees).
"""
import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from droidctl import act, device as dev  # noqa: E402


def stats(xs):
    s = sorted(xs)
    return {"n": len(s), "min": round(s[0], 1), "median": round(statistics.median(s), 1),
            "p95": round(s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))], 1), "max": round(s[-1], 1)}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("-d", "--device", default=os.environ.get("ANDROID_SERIAL"))
    p.add_argument("--count", type=int, default=10)
    p.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "results", "m5-host.json"))
    a = p.parse_args()
    serial = dev.resolve_serial(a.device)
    adb = dev.adb_path()
    subprocess.run([adb, "-s", serial, "shell", "am", "start", "-n", "dev.droidctl.testapp/.Main",
                    "--es", "s", "counter", "--ez", "reset", "true"], capture_output=True, check=True)
    time.sleep(2)
    ns = argparse.Namespace
    sess = act.get_session(serial, auto_setup=False)
    base = dict(device=serial, json=True, target=None, ref=None, id=None, desc=None, cls=None, role=None,
                index=None, right_of=None, left_of=None, above=None, below=None, near=None, point=None,
                double=False, method="auto", settle=None, expect_change=False, no_auto_setup=True)
    snap = sess.snap()
    sess.save(snap)
    ref = next(e.ref for e in snap.elements if e.label_full == "Increment")

    warm, tiers, settle = [], [], []
    for _ in range(a.count):
        t0 = time.perf_counter()
        r = act.cmd_tap(ns(**dict(base, target=str(ref), text=None)))
        warm.append((time.perf_counter() - t0) * 1000)
        tiers.append(r["target"]["tier"])
        settle.append(r.get("settle_ms") or 0)
        assert r["method"] == "action" and r["changed"], r
    act.close_sessions()

    cold = []
    for _ in range(a.count):
        t0 = time.perf_counter()
        out = subprocess.run([sys.executable, "-m", "droidctl", "tap", str(ref), "--json", "-d", serial],
                             capture_output=True, text=True)
        cold.append((time.perf_counter() - t0) * 1000)
        assert json.loads(out.stdout)["method"] == "action", out.stdout

    res = {"date": time.strftime("%Y-%m-%d"), "device": "SM-N950F API 28 (USB passthrough into a KVM VM)",
           "host": f"{platform.system()} {platform.machine()} python {platform.python_version()}",
           "target": "testapp counter: tap Increment (static screen, one line changes)",
           "in_process_tap_settle_ms": stats(warm), "device_settle_ms": stats(settle),
           "fast_path_tiers": tiers, "cold_cli_tap_ms": stats(cold),
           "budget": {"tap+settle (static target)": "<500 ms"}}
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w") as f:
        json.dump(res, f, indent=2)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
