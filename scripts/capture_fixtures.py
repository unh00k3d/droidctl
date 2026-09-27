"""Re-capture the test-app scenario fixtures (`make fixtures`).

Scenario names come from android/testapp/scenarios.json (generated from the
app's own registry), so the list cannot drift from the app. Every screen
scenario is launched fresh with `reset`, the capture waits until the app
window's title is ``s:<scenario>`` (so a stray foreground app is never saved
under the wrong name), waits for the UI to go idle (event-driven, no fixed
sleeps), and saves a non-degraded tree to
tests/fixtures/trees/testapp-<scenario>.json in the same {meta, tree} form as
`droidctl dump-fixture`.

Some scenarios also get a keyboard-shown variant (`testapp-<scenario>-kbd`),
captured after tapping their input.

Skipped: group 0 (peer_uid_probe, not a screen) and group 7 (device-level,
driven by tests). Needs the test app installed and `droidctl setup` done.
Usage:
    .venv/bin/python scripts/capture_fixtures.py [SCENARIO...] [-d SERIAL] [--list]
"""
import argparse
import json
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from capture_resolve_pairs import Cap, TESTAPP   # noqa: E402
from droidctl import device as dev                # noqa: E402
from droidctl import snapshot as S                # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "trees"
SKIP_GROUPS = {0, 7}
# scenarios that are slow to show (TESTAPP huge_tree: ~10 s to lay out 5,000 nodes)
SLOW = {"huge_tree": 60}
# keyboard-shown variants: scenario -> resource-id of the input to tap
KEYBOARD = {"keyboard_toggle": None}   # under_keyboard opens the IME itself
# after-action variants (testapp-<scenario>-<variant>): what TESTAPP expects is
# only on screen after a trigger. Steps: ("tap", text) or ("back",). A variant
# with foreign=PKG is on another app's window (the permission dialog), so the
# s:<scenario> title check is replaced by a foreground-package check.
# Not captured: the notification shade (it shows the phone's real notifications).
VARIANTS = {
    "dialogs": [("alert", [("tap", "Alert")]), ("sheet", [("tap", "Sheet")]),
                ("fullscreen", [("tap", "Full screen")])],
    "snackbar_toast": [("snackbar", [("tap", "Snackbar")])],
    "spinner_dropdown": [("spinner", [("tap", "Apple")]), ("menu", [("tap", "Menu")])],
    "back_confirm": [("exit", [("back",)])],
    "permission": [("request", [("tap", "Request camera")], "foreign")],
}


def scenarios():
    specs = json.loads((ROOT / "android" / "testapp" / "scenarios.json").read_text())
    return [s["name"] for s in specs if s["group"] not in SKIP_GROUPS]


class FixtureCap(Cap):
    def launch(self, scenario, extras=(), timeout=15):
        self.sh("am", "start", "-S", "-n", f"{TESTAPP}/.Main", "--es", "s", scenario,
                "--ez", "reset", "true", *extras)
        want = f"s:{scenario}"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                tree = self.client.tree(timeout=timeout)
            except Exception:       # a slow first layout can time a dump out; keep waiting
                tree = {}
            if any(w.get("title") == want for w in tree.get("windows", ())):
                self.idle(quiet_ms=400, timeout_ms=4000)
                return
            time.sleep(0.3)
        raise SystemExit(f"{scenario}: window {want!r} never appeared")

    def tree(self, scenario, timeout=20):
        for _ in range(5):
            t = self.client.tree(timeout=timeout)
            if not t.get("degraded") and any(w.get("title") == f"s:{scenario}" for w in t["windows"]):
                return t
            time.sleep(0.5)
        raise SystemExit(f"{scenario}: could not get a clean tree of s:{scenario}")

    def save_fixture(self, name, scenario, tree, note=""):
        import datetime
        apps = [w for w in tree["windows"] if w.get("type") == "application" and w.get("root")]
        top = max(apps, key=lambda w: w.get("layer", 0), default={})
        package = TESTAPP if any(w.get("pkg") == TESTAPP for w in apps) else top.get("pkg", TESTAPP)
        meta = {
            "name": name, "scenario": scenario, "note": note,
            "captured": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "manufacturer": self.info.get("manufacturer"), "model": self.info.get("model"),
            "sdk": self.info.get("sdk"), "release": self.info.get("release"), "screen": self.info.get("screen"),
            "agent": {"version": self.info.get("version"), "versionCode": self.info.get("versionCode")},
            "package": package, "activity": f"{TESTAPP}/.Main" if package == TESTAPP else None,
            "dump_ms": tree.get("ms"), "nodes": tree.get("nodes"), "degraded": tree.get("degraded"),
        }
        path = OUT / f"{name}.json"
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"meta": meta, "tree": tree}, ensure_ascii=False, indent=1) + "\n")
        os.replace(tmp, path)
        print(f"  saved {path.relative_to(ROOT)}  ({tree.get('nodes')} nodes, {tree.get('ms')} ms)")

    def keyboard(self, scenario, input_id):
        """Tap the scenario's input (or its first editable) and wait for the IME."""
        t, s = self.snap(scenario)
        if not s.keyboard:
            loc = {"id": input_id} if input_id else {"role": "input"}
            self.tap(s, **loc)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not self.client.current().get("keyboard"):
                self.idle(quiet_ms=300, timeout_ms=1500)
        self.idle(quiet_ms=500, timeout_ms=4000)
        t, s = self.snap(scenario)
        if not s.keyboard:
            raise SystemExit(f"{scenario}: the keyboard did not open")
        return t

    def variant(self, scenario, steps, foreign=False):
        """Run the trigger steps on a fresh launch; return the settled tree."""
        for step in steps:
            t, s = self.snap(scenario)
            if step[0] == "tap":
                self.click(t, s, text=step[1])       # handles belong to t's dump
            elif step[0] == "back":
                self.client.global_action("back")
            # a quiet spell is not enough: the back_confirm dialog's window came
            # after 500 ms of quiet; wait for the screen itself to change
            # (and the first change can be a transient: the activity's content
            # collapses before the dialog window is listed), so wait for a new
            # signature that holds across two dumps
            deadline, last = time.monotonic() + 8, None
            while time.monotonic() < deadline:
                t2 = self.client.tree(timeout=20)
                sig = None if t2.get("degraded") else S.build(t2).sig
                if sig is not None and sig != s.sig and sig == last:
                    break
                last = sig
                self.idle(quiet_ms=400, timeout_ms=1500)
        # the launch was title-checked before the trigger; after it, a modal
        # dialog may be the only window Android 9 reports (untitled), so check
        # the package instead
        for _ in range(10):
            t = self.client.tree(timeout=20)
            pkgs = {w.get("pkg") for w in t["windows"] if w.get("type") == "application" and w.get("root")}
            if not t.get("degraded") and ((pkgs - {TESTAPP}) if foreign else (TESTAPP in pkgs)):
                return t
            self.idle(quiet_ms=300, timeout_ms=1500)
        raise SystemExit(f"{scenario}: the expected window never appeared")

    def hide_keyboard(self):
        if self.client.current().get("keyboard"):
            self.client.global_action("back")
            self.idle()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("only", nargs="*", help="capture just these scenarios")
    ap.add_argument("-d", "--device", default=os.environ.get("ANDROID_SERIAL"))
    ap.add_argument("--list", action="store_true", help="print the scenario list and exit")
    a = ap.parse_args()
    todo = [s for s in scenarios() if not a.only or s in a.only]
    if a.list:
        print("\n".join(todo))
        return 0
    c = FixtureCap(dev.resolve_serial(a.device))
    failed = []
    try:
        for s in todo:
            print(s)
            try:
                timeout = SLOW.get(s, 15)
                c.launch(s, timeout=timeout)
                c.save_fixture(f"testapp-{s}", s, c.tree(s, timeout=timeout))
                if s in KEYBOARD:
                    c.save_fixture(f"testapp-{s}-kbd", s, c.keyboard(s, KEYBOARD[s]), "keyboard shown")
                    c.hide_keyboard()
                for v in VARIANTS.get(s, ()):
                    name, steps, foreign = v[0], v[1], len(v) > 2
                    c.launch(s, timeout=timeout)
                    t = c.variant(s, steps, foreign)
                    c.save_fixture(f"testapp-{s}-{name}", s, t, "after: " + ", ".join(" ".join(x) for x in steps))
                    if foreign:
                        c.client.global_action("back")      # dismiss (deny) the system dialog
                        c.idle()
            except (SystemExit, Exception) as e:     # one bad scenario must not lose the others
                print(f"  FAILED: {e}", file=sys.stderr)
                failed.append(s)
    finally:
        c.client.close()
    if failed:
        print(f"failed: {' '.join(failed)}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
