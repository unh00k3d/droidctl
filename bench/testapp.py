"""Shared bench helpers: adb, launching test-app scenarios, and DTA ground truth.

Ground truth is the test app's own `DTA` logcat lines (TESTAPP.md), read with
plain adb, never from droidctl's output, so a benchmark can't grade itself.
"""
import json
import os
import subprocess
import time

PKG = "dev.droidctl.testapp"
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
_SDK_ADB = os.path.join(os.environ.get("ANDROID_HOME") or os.path.expanduser("~/Android/Sdk"),
                        "platform-tools", "adb")
ADB = _SDK_ADB if os.path.exists(_SDK_ADB) else "adb"
VENV_DROIDCTL = os.path.join(ROOT, ".venv", "bin", "droidctl")


def adb(serial, *args, timeout=60, check=True, text=True):
    r = subprocess.run([ADB, "-s", serial, *args], capture_output=True, text=text, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"adb {' '.join(args)} failed: {r.stderr or r.stdout}")
    return r.stdout


def parse_dta(logcat_text, scenario=None):
    """DTA JSON lines from `logcat -v raw DTA:I *:S` output, optionally for one scenario."""
    events = []
    for line in logcat_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if scenario is None or e.get("s") == scenario:
            events.append(e)
    return events


def dta(serial, scenario=None):
    return parse_dta(adb(serial, "logcat", "-d", "-v", "raw", "DTA:I", "*:S"), scenario)


def clear_log(serial):
    adb(serial, "logcat", "-c")


def launch(serial, scenario, timeout=25.0):
    """Start a scenario fresh (reset=true), wait for its `shown` event, clear the log."""
    clear_log(serial)
    # accessibility actions are not user activity: a long run of node taps lets the
    # screen time out (tap-accuracy bench, 2026-09-28), so wake it for every scenario
    adb(serial, "shell", "input", "keyevent", "KEYCODE_WAKEUP")
    adb(serial, "shell", "am", "start", "-n", f"{PKG}/.Main", "--es", "s", scenario, "--ez", "reset", "true")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(e.get("ev") == "shown" for e in dta(serial, scenario)):
            break
        time.sleep(0.25)
    else:
        raise RuntimeError(f"scenario {scenario!r} never logged `shown`")
    time.sleep(0.4)            # let the first frame's layout and a11y events settle
    clear_log(serial)


def interactions(events):
    """DTA events caused by the user (drops the bookkeeping ones)."""
    return [e for e in events if e.get("ev") not in ("shown", "reset", "list", "probe")]


def focus(serial):
    """The focused window line from `dumpsys window` (package/activity ground truth)."""
    out = adb(serial, "shell", "dumpsys", "window", "windows")
    for line in out.splitlines():
        if "mCurrentFocus" in line:
            return line.strip()
    return ""


def top_fragments(serial):
    """`dumpsys activity top` text: names the resumed activity and its fragments."""
    return adb(serial, "shell", "dumpsys", "activity", "top", timeout=30)
