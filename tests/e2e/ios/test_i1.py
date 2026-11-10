"""I1 on a real iPhone: devices, guided setup (non-interactive), snapshot in-process and
through the daemon, runner stop -> auto-start. Run with DROIDCTL_IOS_UDID=<hardware UDID>,
the phone unlocked; `droidctl setup` must have succeeded once (signing team, Trust)."""
import json
import os
import subprocess
import sys
import time

import pytest

UDID = os.environ.get("DROIDCTL_IOS_UDID")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def dc(*args, daemon=True, timeout=300):
    # `python -m droidctl` always runs in-process: only the console script talks to the daemon
    exe = [os.path.join(os.path.dirname(sys.executable), "droidctl")] if daemon else [sys.executable, "-m", "droidctl"]
    cmd = exe + [*args, "--json"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=ROOT)
    return json.loads(p.stdout)


@pytest.fixture(scope="module")
def client():
    from droidctl.ios.client import IosClient
    from droidctl.ios import runner
    runner.connect(UDID)[0].close()
    c = IosClient(UDID)
    yield c
    c.close()


def _walk(n):
    yield n
    for k in n.get("children") or []:
        yield from _walk(k)


def _tap_label(c, label, types=(75, 9)):
    """Test navigation only: an explicit point from the runner's own tree."""
    t = c.native_tree()
    n = next(n for a in t["apps"] for n in _walk(a["root"]) if n.get("label") == label and n["type"] in types)
    x, y, w, h = n["frame"]
    c.call("gesture", {"type": "tap", "points": [[x + w / 2, y + h / 2]]})
    time.sleep(1.5)


def test_devices_lists_the_iphone():
    r = dc("devices", daemon=False)
    row = next(d for d in r["devices"] if d["serial"] == UDID)
    assert row["platform"] == "ios" and row["backend"] == "ios" and row["setup"]


def test_setup_is_idempotent_and_non_interactive():
    r = dc("setup", "-d", UDID, daemon=False)
    assert r["ok"], r
    assert r["steps"] == ["xcode", "attached", "developer-mode", "team", "unlocked", "build", "start"]
    assert r["profile_expires"]


def test_snapshot_of_settings_general(client):
    client.call("launch", {"pkg": "com.apple.Preferences"})
    time.sleep(1.5)
    t = client.native_tree()
    if any(n.get("label") == "General" and n["type"] == 48 and n["frame"][1] < 100
           for a in t["apps"] for n in _walk(a["root"])):
        pass                                   # already on General
    else:
        while any(n.get("label") in ("Settings", "Back") and n["type"] == 9 and n["frame"][1] < 100
                  for a in t["apps"] for n in _walk(a["root"])):
            _tap_label(client, next(n.get("label") for a in t["apps"] for n in _walk(a["root"])
                                    if n.get("label") in ("Settings", "Back") and n["type"] == 9), types=(9,))
            t = client.native_tree()
        _tap_label(client, "General")
    r = dc("snapshot", "-d", UDID, "--full", daemon=False)
    assert r["ok"] and r["screen"]["pkg"] == "com.apple.Preferences"
    labels = {e.get("label") for e in r["elements"]}
    assert {"About", "Software Update · 1"} & labels or "About" in labels
    assert r["tokens_est"] < 400


def test_snapshot_through_the_daemon_is_cached():
    a = dc("snapshot", "-d", UDID, "--full")
    b = dc("snapshot", "-d", UDID)
    assert a["ok"] and b["ok"] and b["unchanged"]
    assert b["elapsed_ms"] < 300


def test_a_stopped_runner_starts_again_on_the_next_command():
    from droidctl.ios import runner
    runner.stop(UDID)
    time.sleep(2)
    t0 = time.monotonic()
    r = dc("snapshot", "-d", UDID, "--full", daemon=False)
    assert r["ok"], r
    assert time.monotonic() - t0 < 60
