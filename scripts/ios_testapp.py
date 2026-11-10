"""Build droidctl's iOS test app (ios/testapp) and install it on an iPhone, signed with the same
team as the runner. Its bundle id is <runner prefix>.testapp, so it never collides with the
runner and `teardown` (droidctl's runner only) leaves it alone.

    .venv/bin/python scripts/ios_testapp.py -d <hardware UDID> [--team TEAMID] [--sim]

A free team allows 3 apps per iPhone: the runner plus this app take two. `--sim` builds for the
simulator instead (no signing) and prints the .app path.
"""
import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from droidctl import device as dev  # noqa: E402
from droidctl.ios import devices, runner  # noqa: E402

PROJECT = os.path.join(ROOT, "ios", "testapp", "DroidctlTestApp.xcodeproj")


def bundle(team):
    return runner.bundle_prefix(team) + ".testapp"


def build(team=None, sim=False):
    out = os.path.join(runner.ios_home(), "testapp-build")
    if sim:
        args = ["-sdk", "iphonesimulator", "CODE_SIGNING_ALLOWED=NO"]
    else:
        args = ["-sdk", "iphoneos", "-allowProvisioningUpdates", f"DROIDCTL_TEAM={team}",
                f"DROIDCTL_BUNDLE_PREFIX={runner.bundle_prefix(team)}"]
    p = subprocess.run(["xcodebuild", "build", "-project", PROJECT, "-target", "TestApp", f"SYMROOT={out}", *args],
                       env=devices.xcode_env(), capture_output=True, text=True)
    if p.returncode != 0:
        sys.exit("build failed:\n" + "\n".join(ln for ln in p.stdout.splitlines() if "error:" in ln))
    return os.path.join(out, "Debug-iphonesimulator" if sim else "Debug-iphoneos", "TestApp.app")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-d", dest="udid")
    ap.add_argument("--team")
    ap.add_argument("--sim", action="store_true")
    a = ap.parse_args()
    if a.sim:
        print(build(sim=True))
        return
    team = a.team or dev.device_state(a.udid).get("team")
    if not a.udid or not team:
        sys.exit("need -d <UDID> of an iPhone set up with droidctl (or --team)")
    app = build(team)
    from droidctl.ios import syslog
    with syslog.xcode_busy(a.udid):
        devices.install(devices.find(a.udid)["cdid"], app)
    print(bundle(team))


if __name__ == "__main__":
    main()
