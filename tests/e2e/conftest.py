"""e2e against the test app on a real phone. Run only with DROIDCTL_SERIAL set.

Ground truth is the test app's `DTA` logcat lines (TESTAPP.md), never droidctl's
own output. `scenario(name, **extras)` launches a screen fresh (reset=true),
waits for its window, clears logcat and returns a helper with `dta()` (the
parsed DTA events since the launch) and `dc(...)` (droidctl --json).
"""
import json
import os
import subprocess
import sys
import time

import pytest

SERIAL = os.environ.get("DROIDCTL_SERIAL")
_SDK_ADB = os.path.join(os.environ.get("ANDROID_HOME") or os.path.expanduser("~/Android/Sdk"),
                        "platform-tools", "adb")
ADB = _SDK_ADB if os.path.exists(_SDK_ADB) else "adb"
PKG = "dev.droidctl.testapp"


def pytest_collection_modifyitems(config, items):
    if SERIAL:
        return
    skip = pytest.mark.skip(reason="set DROIDCTL_SERIAL to run e2e tests on a device")
    for it in items:
        if "/e2e/" in str(it.fspath):
            it.add_marker(skip)


def adb(*args, timeout=60, check=True):
    return subprocess.run([ADB, "-s", SERIAL, *args], capture_output=True, text=True,
                          timeout=timeout, check=check).stdout


def dc(*args, stdin=None, ok=True, timeout=120):
    """droidctl <args> --json -d SERIAL -> (exit code, payload)."""
    out = subprocess.run([sys.executable, "-m", "droidctl", *map(str, args), "--json", "-d", SERIAL],
                         capture_output=True, text=True, timeout=timeout, input=stdin)
    try:
        payload = json.loads(out.stdout)
    except ValueError:
        raise AssertionError(f"droidctl {' '.join(map(str, args))}: no JSON\n{out.stdout}\n{out.stderr}")
    if ok is True:
        assert out.returncode == 0, f"droidctl {' '.join(map(str, args))} failed: {payload}"
    return payload


class Scenario:
    def __init__(self, name):
        self.name = name

    def dta(self, ev=None, id=None):
        out = adb("logcat", "-d", "-v", "raw", "DTA:I", "*:S")
        events = []
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("s") != self.name:
                continue
            if ev is not None and e.get("ev") != ev:
                continue
            if id is not None and e.get("id") != id:
                continue
            events.append(e)
        return events

    def snap(self):
        return dc("snapshot", "--full")

    def ref(self, label=None, role=None, id=None):
        """A ref from a fresh snapshot, by exact label / role / id."""
        s = self.snap()
        hits = [e for e in s["elements"]
                if (label is None or e["label"] == label) and (role is None or e["role"] == role)
                and (id is None or (e["id"] or "").endswith(":id/" + id))]
        assert len(hits) == 1, f"{label=} {role=} {id=}: {len(hits)} hits in\n{s['text']}"
        return hits[0]["ref"]


@pytest.fixture
def scenario():
    def launch(name, **extras):
        cmd = ["shell", "am", "start", "-n", f"{PKG}/.Main", "--es", "s", name, "--ez", "reset", "true"]
        for k, v in extras.items():
            flag = "--ei" if isinstance(v, int) and not isinstance(v, bool) else \
                   "--ez" if isinstance(v, bool) else "--es"
            cmd += [flag, k, str(v).lower() if isinstance(v, bool) else str(v)]
        adb("logcat", "-c")
        adb(*cmd)
        dc("wait", "--window", f"s:{name}", "--timeout", "15")
        sc = Scenario(name)
        deadline = time.time() + 15
        while not sc.dta("shown") and time.time() < deadline:
            time.sleep(0.2)
        adb("logcat", "-c")
        return sc
    return launch
