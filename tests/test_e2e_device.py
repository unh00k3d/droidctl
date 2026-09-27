"""On-device checks for setup/ping/teardown. Run only with DROIDCTL_SERIAL set.

Ground truth here is the phone's own settings (`settings get`), not droidctl's
output. These change the phone's accessibility settings and restore them.
"""
import json
import os
import subprocess
import sys

import pytest

SERIAL = os.environ.get("DROIDCTL_SERIAL")
pytestmark = pytest.mark.skipif(not SERIAL, reason="set DROIDCTL_SERIAL to run on a device")
# the SDK's adb, never Debian's older /usr/bin/adb (mixing the two restarts the server)
_SDK_ADB = os.path.join(os.environ.get("ANDROID_HOME") or os.path.expanduser("~/Android/Sdk"),
                        "platform-tools", "adb")
ADB = _SDK_ADB if os.path.exists(_SDK_ADB) else "adb"


def droidctl(*args):
    out = subprocess.run([sys.executable, "-m", "droidctl", *args, "--json", "-d", SERIAL],
                         capture_output=True, text=True, timeout=180)
    return out.returncode, json.loads(out.stdout)


def setting(key):
    return subprocess.run([ADB, "-s", SERIAL, "shell", "settings", "get", "secure", key],
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture(scope="module")
def original():
    return {k: setting(k) for k in ("enabled_accessibility_services", "accessibility_enabled")}


def test_setup_keeps_existing_services(original):
    code, p = droidctl("setup")
    assert code == 0, p
    after = setting("enabled_accessibility_services").split(":")
    for entry in filter(None, original["enabled_accessibility_services"].replace("null", "").split(":")):
        assert entry in after, f"setup dropped {entry}"
    assert setting("accessibility_enabled") == "1"


def test_ping_comes_from_the_shell_uid():
    code, p = droidctl("ping")
    assert code == 0, p
    assert p["peer_uid"] == 2000 and p["protocol"] == 1


def test_doctor_passes():
    code, p = droidctl("doctor")
    assert code == 0, p


def test_teardown_restores_the_original_settings_exactly(original):
    code, p = droidctl("teardown")
    assert code == 0, p
    assert {k: setting(k) for k in original} == original
