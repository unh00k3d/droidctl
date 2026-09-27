"""M6 daemon overheads: thin client -> daemon, resident Client, cache-hit snapshot.

    .venv/bin/python bench/m6_daemon.py [--n 30] [--serial SERIAL] [--json]

Runs a throwaway daemon (tmp DROIDCTL_HOME). Without --serial the device is a
FakeAgent behind a FakeAdb (tests/fakes.py): that measures the host side only
(the fake answers instantly). With --serial it uses the real phone through the
real adb server. Results go to bench/results/m6-daemon[-SERIAL].json.
"""
import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
PY = sys.executable


def stats(xs):
    xs = sorted(xs)
    return {"n": len(xs), "min": round(xs[0], 2), "median": round(statistics.median(xs), 2),
            "p95": round(xs[min(len(xs) - 1, int(len(xs) * 0.95))], 2), "max": round(xs[-1], 2)}


def wall(argv, env, n):
    out = []
    for _ in range(n):
        t0 = time.perf_counter()
        r = subprocess.run(argv, env=env, capture_output=True, text=True)
        out.append((time.perf_counter() - t0) * 1000)
        if r.returncode not in (0,):
            raise SystemExit(f"{argv} failed: {r.stderr or r.stdout}")
    return stats(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--serial")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    home = tempfile.mkdtemp(prefix="droidctl-bench-")
    env = {k: v for k, v in os.environ.items() if not k.startswith("DROIDCTL_")}
    env.update(DROIDCTL_HOME=home, DROIDCTL_IDLE="10m")
    fakes = None
    if a.serial:
        serial = a.serial
    else:
        from tests.fakes import FakeAdb, FakeAgent
        agent = FakeAgent()
        adb = FakeAdb({"FAKE": agent})
        env["ANDROID_ADB_SERVER_PORT"] = str(adb.port)
        serial, fakes = "FAKE", (agent, adb)
    os.environ.update({k: env[k] for k in ("DROIDCTL_HOME", "ANDROID_ADB_SERVER_PORT") if k in env})

    fixture = os.path.join(ROOT, "tests", "fixtures", "trees", "real-settings-main.json")
    client = [PY, "-m", "droidctl.client"]
    res = {"date": time.strftime("%Y-%m-%d"), "host": platform.node(), "python": platform.python_version(),
           "device": "fake agent (host overhead only)" if fakes else serial, "n": a.n}
    try:
        subprocess.run(client + ["version", "--json"], env=env, capture_output=True, check=True)
        res["python_bare"] = wall([PY, "-c", "pass"], env, a.n)
        res["inprocess_version"] = wall([PY, "-m", "droidctl", "version", "--json"], env, a.n)
        res["client_version"] = wall(client + ["version", "--json"], env, a.n)
        res["inprocess_snapshot_fixture"] = wall([PY, "-m", "droidctl", "snapshot", "--fixture", fixture, "--json"], env, a.n)
        res["client_snapshot_fixture"] = wall(client + ["snapshot", "--fixture", fixture, "--json"], env, a.n)
        subprocess.run(client + ["snapshot", "-d", serial, "--json"], env=env, capture_output=True, check=True)
        time.sleep(0.5)
        subprocess.run(client + ["snapshot", "-d", serial, "--json"], env=env, capture_output=True, check=True)
        res["client_snapshot_cache_hit"] = wall(client + ["snapshot", "-d", serial, "--json"], env, a.n)
        res["inprocess_snapshot_device"] = wall([PY, "-m", "droidctl", "snapshot", "-d", serial, "--json"], env, a.n)

        from droidctl.client import Client
        with Client() as c:
            for label, argv in (("resident_version", ["version", "--json"]),
                                ("resident_snapshot_cache_hit", ["snapshot", "-d", serial, "--json"])):
                c.run(argv)
                xs = []
                for _ in range(max(a.n, 100)):
                    t0 = time.perf_counter()
                    c.run(argv)
                    xs.append((time.perf_counter() - t0) * 1000)
                res[label] = stats(xs)
            st = c.status()
        dev = next((d for d in st["devices"] if d["serial"] == serial), {})
        res["daemon_cache"] = dev.get("cache")
        res["budgets_ms"] = {"cli_to_daemon_overhead": 35, "daemon_overhead_resident": 3,
                             "snapshot_cache_hit": "5 + client boot"}
        res["cli_to_daemon_overhead_est_ms"] = round(res["client_version"]["median"] - res["python_bare"]["median"], 2)
    finally:
        subprocess.run([PY, "-m", "droidctl", "daemon", "stop"], env=env, capture_output=True)
        if fakes:
            for f in fakes:
                f.close()

    name = "m6-daemon" + (f"-{serial}" if a.serial else "") + ".json"
    out = os.path.join(ROOT, "bench", "results", name)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(res, f, indent=2)
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        for k, v in res.items():
            if isinstance(v, dict) and "median" in v:
                print(f"{k:32s} median {v['median']:7.2f} ms  p95 {v['p95']:7.2f}  min {v['min']:7.2f}")
        print(f"cli->daemon overhead (client version - python bare): {res['cli_to_daemon_overhead_est_ms']} ms")
        print(f"cache: {res['daemon_cache']}")
        print(f"-> {out}")


if __name__ == "__main__":
    main()
