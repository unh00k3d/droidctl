"""Capture devicectl JSON from the attached phones as test fixtures, scrubbed.

    DEVELOPER_DIR=… python3 tests/fixtures/ios-devicectl/capture.py <CoreDevice id> <bundle id>

Identifiers (UDIDs, CoreDevice ids, ECIDs, serials), device names, hostnames, tunnel
addresses and install paths are replaced by stable fakes; capability lists are dropped.
The shapes and every other value stay as devicectl printed them."""
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
UDID = re.compile(r"\b[0-9A-F]{8}-[0-9A-F]{16}\b")
UUID = re.compile(r"\b[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}\b", re.I)
DROP = {"capabilities", "potentialHostnames", "localHostnames", "ecid", "serialNumber", "tunnelIPAddress",
        "bootedSnapshotName", "url", "lastConnectionDate", "tags", "deviceIdentifier",
        "arguments", "environment", "screenViewingURL"}
fakes = {}


def fake(value, kind):
    if value not in fakes:
        n = sum(1 for k in fakes.values() if k.startswith(kind)) + 1
        fakes[value] = {"udid": f"00008101-{n:016X}", "uuid": f"00000000-0000-4000-8000-{n:012X}",
                        "name": f"Test iPhone {n}"}[kind]
    return fakes[value]


def scrub(x, key=None):
    if isinstance(x, dict):
        return {k: scrub(v, k) for k, v in x.items() if k not in DROP}
    if isinstance(x, list):
        return [scrub(v) for v in x]
    if isinstance(x, str):
        if key == "name" and "iPhone" in x or key == "name" and "’" in x:
            return fake(x, "name")
        x = UDID.sub(lambda m: fake(m.group(0), "udid"), x)
        return UUID.sub(lambda m: fake(m.group(0), "uuid"), x)
    return x


def devicectl(*args):
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "out.json")
        subprocess.run(["xcrun", "devicectl", *args, "-j", out, "-q"], check=False, capture_output=True)
        return json.load(open(out))


def save(name, data):
    with open(os.path.join(HERE, name + ".json"), "w") as f:
        json.dump(scrub(data), f, indent=1, ensure_ascii=False)
        f.write("\n")


if __name__ == "__main__":
    cdid, bundle = sys.argv[1], sys.argv[2]
    save("list_devices", devicectl("list", "devices"))
    save("details", devicectl("device", "info", "details", "--device", cdid))
    save("lock_state_unlocked", devicectl("device", "info", "lockState", "--device", cdid))
    save("apps_one", devicectl("device", "info", "apps", "--device", cdid, "--bundle-id", bundle))
    save("apps_missing", devicectl("device", "info", "apps", "--device", cdid, "--bundle-id", bundle + ".nothere"))
    save("launch_missing", devicectl("device", "process", "launch", "--device", cdid, "com.example.nothere"))
