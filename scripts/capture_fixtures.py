"""Re-capture the test-app scenario fixtures (`make fixtures`).

Scenario names come from TESTAPP.md itself (groups 3, 4, 5 and 8), so the list
cannot drift from the spec. Each scenario is launched fresh with `reset`, left to
settle, and dumped with `droidctl dump-fixture testapp-<scenario>`. Needs the test
app installed and `droidctl setup` done.
"""
import argparse
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
GROUPS = ("3", "4", "5", "8")
TESTAPP = "dev.droidctl.testapp"


def scenarios(groups=GROUPS):
    """`[(group, name)]` from the TESTAPP.md scenario tables, in document order."""
    out, group = [], None
    for line in (ROOT / "TESTAPP.md").read_text().splitlines():
        m = re.match(r"### (\d+)\.", line)
        if m:
            group = m.group(1)
            continue
        m = re.match(r"\| `([a-z0-9_]+)` \|", line)
        if m and group in groups:
            out.append((group, m.group(1)))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("only", nargs="*", help="capture just these scenarios")
    ap.add_argument("-d", "--device", help="device serial")
    ap.add_argument("--list", action="store_true", help="print the scenario list and exit")
    a = ap.parse_args()
    todo = [s for _, s in scenarios() if not a.only or s in a.only]
    if a.list:
        print("\n".join(todo))
        return 0
    adb = ["adb"] + (["-s", a.device] if a.device else [])
    dev = ["-d", a.device] if a.device else []
    failed = []
    for s in todo:
        subprocess.run(adb + ["shell", "am", "start", "-W", "-S", "-n", f"{TESTAPP}/.Main",
                              "--es", "s", s, "--ez", "reset", "true"],
                       check=False, stdout=subprocess.DEVNULL)
        r = subprocess.run([sys.executable, "-m", "droidctl", "dump-fixture", f"testapp-{s}",
                            "--pkg", TESTAPP] + dev, cwd=ROOT)
        if r.returncode:
            failed.append(s)
    if failed:
        print(f"failed: {' '.join(failed)}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
