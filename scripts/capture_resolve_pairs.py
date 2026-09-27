"""Capture the resolver's real before/after tree pairs from the test app.

Each case launches one scenario fresh, verifies the window title is
``s:<scenario>`` (so a stray foreground app is never saved under the wrong
name), dumps tree A, performs the change the case is about (a scroll, a tap, a
drag, time passing, the keyboard opening), waits for the UI to settle and dumps
tree B. Files land in tests/fixtures/resolve/<case>-a.json / -b.json, in the
same {meta, tree} form as dump-fixture. What each pair must resolve to lives in
tests/fixtures/resolve/cases.json (language-neutral), checked by
tests/test_resolve.py.

Needs the test app installed and `droidctl setup` done. Usage:
    .venv/bin/python scripts/capture_resolve_pairs.py [CASE...] [-d SERIAL]
"""
import argparse
import datetime
import json
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from droidctl import device as dev          # noqa: E402
from droidctl import resolve as R           # noqa: E402
from droidctl import snapshot as S          # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "resolve"
TESTAPP = "dev.droidctl.testapp"
SETTLE = {"quiet_ms": 300, "timeout_ms": 3000}


class Cap:
    def __init__(self, serial):
        self.serial = serial
        self.client, self.info = dev.connect(serial)
        self.adb = [dev.adb_path(), "-s", serial]

    def sh(self, *args):
        return subprocess.run(self.adb + ["shell", *args], capture_output=True, text=True, timeout=30).stdout

    def launch(self, scenario, extras=()):
        self.sh("am", "start", "-S", "-n", f"{TESTAPP}/.Main", "--es", "s", scenario,
                "--ez", "reset", "true", *extras)
        want = f"s:{scenario}"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            tree = self.client.tree()
            if any(w.get("title") == want for w in tree.get("windows", ())):
                self.idle()
                return
            time.sleep(0.3)
        raise SystemExit(f"{scenario}: window {want!r} never appeared")

    def idle(self, quiet_ms=400, timeout_ms=4000):
        return self.client.wait_idle(quiet_ms=quiet_ms, timeout_ms=timeout_ms)

    def tree(self, scenario):
        """A settled, non-degraded tree whose app window is the scenario's."""
        for _ in range(5):
            t = self.client.tree(timeout=20)
            if not t.get("degraded") and any(w.get("title") == f"s:{scenario}" for w in t["windows"]):
                return t
            time.sleep(0.5)
        raise SystemExit(f"{scenario}: could not get a clean tree of s:{scenario}")

    def snap(self, scenario):
        t = self.tree(scenario)
        return t, S.build(t, activity=f"{TESTAPP}/.Main")

    def save(self, case, part, scenario, tree, note=""):
        meta = {
            "name": f"{case}-{part}", "scenario": scenario, "note": note,
            "captured": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "manufacturer": self.info.get("manufacturer"), "model": self.info.get("model"),
            "sdk": self.info.get("sdk"), "release": self.info.get("release"), "screen": self.info.get("screen"),
            "agent": {"version": self.info.get("version"), "versionCode": self.info.get("versionCode")},
            "package": TESTAPP, "activity": f"{TESTAPP}/.Main",
            "dump_ms": tree.get("ms"), "nodes": tree.get("nodes"), "degraded": tree.get("degraded"),
        }
        OUT.mkdir(parents=True, exist_ok=True)
        path = OUT / f"{case}-{part}.json"
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"meta": meta, "tree": tree}, ensure_ascii=False, indent=1) + "\n")
        os.replace(tmp, path)
        print(f"  saved {path.relative_to(ROOT)}  ({tree.get('nodes')} nodes)")

    def click(self, tree, snap, **loc):
        res = R.find(snap, **loc)
        return self.client.act(tree["dump"], res.click, action="click", settle=SETTLE)

    def tap(self, snap, **loc):
        res = R.find(snap, **loc)
        return self.client.gesture("tap", [res.tap], settle=SETTLE)

    def swipe(self, x, y0, y1, ms=600):
        self.client.gesture("swipe", [[x, y0], [x, y1]], ms=ms, settle=SETTLE)
        self.idle()


# --------------------------------------------------------------------------
# the cases
# --------------------------------------------------------------------------
def c_static_twice(c):
    """The same untouched screen dumped twice: every ref resolves to itself."""
    c.launch("buttons")
    c.save("static_twice", "a", "buttons", c.tree("buttons"))
    time.sleep(1.0)
    c.save("static_twice", "b", "buttons", c.tree("buttons"), "no change, a second dump")


def c_long_list_scroll(c):
    c.launch("long_list")
    t, s = c.snap("long_list")
    c.save("long_list_scroll", "a", "long_list", t)
    W, H = s.screen[2], s.screen[3]
    c.swipe(W // 2, int(H * 0.75), int(H * 0.45))
    c.save("long_list_scroll", "b", "long_list", c.tree("long_list"), "list scrolled up about 30% of the screen")


def c_list_insert_top(c):
    c.launch("list_insert_top")
    c.save("list_insert_top", "a", "list_insert_top", c.tree("list_insert_top"))
    time.sleep(3.6)                  # the scenario inserts a row at the top every 3 s
    c.idle()
    c.save("list_insert_top", "b", "list_insert_top", c.tree("list_insert_top"), "a row inserted at the top")


def c_duplicates_scroll(c):
    c.launch("duplicates")
    t, s = c.snap("duplicates")
    c.save("duplicates_scroll", "a", "duplicates", t)
    W, H = s.screen[2], s.screen[3]
    c.swipe(W // 2, int(H * 0.7), int(H * 0.55))
    c.save("duplicates_scroll", "b", "duplicates", c.tree("duplicates"), "scrolled a little")


def c_lookalike_ok(c):
    c.launch("lookalike_ok")
    t, s = c.snap("lookalike_ok")
    c.save("lookalike_ok", "a", "lookalike_ok", t)
    c.click(t, s, text="OK")
    c.idle()
    c.save("lookalike_ok", "b", "lookalike_ok", c.tree("lookalike_ok"), "OK on screen A tapped: screen B")


def c_cart_inc(c):
    c.launch("cart")
    t, s = c.snap("cart")
    c.save("cart_inc", "a", "cart", t)
    c.click(t, s, text="+", right_of="Wireless Mouse")
    c.idle()
    c.save("cart_inc", "b", "cart", c.tree("cart"), "+ tapped on Wireless Mouse (qty 1 -> 2)")


def c_drag_reorder(c):
    c.launch("drag_reorder")
    t, s = c.snap("drag_reorder")
    c.save("drag_reorder", "a", "drag_reorder", t)
    h = R.find(s, desc="Drag handle", right_of="Task 1").tap
    target = R.find(s, text="Task 3")
    y1 = target.elem.rect[3] - 10
    pts = [[h[0], h[1] + int((y1 - h[1]) * k / 10)] for k in range(11)]
    c.client.gesture("path", [h, h] + pts, ms=1500, settle=SETTLE)
    c.idle()
    c.save("drag_reorder", "b", "drag_reorder", c.tree("drag_reorder"), "Task 1 dragged below Task 3")


def c_under_keyboard(c):
    c.launch("under_keyboard")
    time.sleep(1.0)
    cur = c.client.current()
    if cur.get("keyboard"):
        c.client.global_action("back", settle=SETTLE)
        c.idle()
    t, s = c.snap("under_keyboard")
    if s.keyboard:
        raise SystemExit("under_keyboard: the keyboard would not close")
    c.save("under_keyboard", "a", "under_keyboard", t, "keyboard hidden: Submit visible")
    c.tap(s, id="message")
    time.sleep(1.0)
    c.idle()
    t2, s2 = c.snap("under_keyboard")
    if not s2.keyboard:
        raise SystemExit("under_keyboard: the keyboard did not open")
    c.save("under_keyboard", "b", "under_keyboard", t2, "input tapped: keyboard over Submit")


def c_overlay_blocker(c):
    c.launch("overlay_blocker")
    c.save("overlay_blocker", "a", "overlay_blocker", c.tree("overlay_blocker"))


def c_partial(c):
    c.launch("partial")
    time.sleep(0.5)
    c.idle()
    c.save("partial", "a", "partial", c.tree("partial"))


CASES = {
    "static_twice": c_static_twice,
    "long_list_scroll": c_long_list_scroll,
    "list_insert_top": c_list_insert_top,
    "duplicates_scroll": c_duplicates_scroll,
    "lookalike_ok": c_lookalike_ok,
    "cart_inc": c_cart_inc,
    "drag_reorder": c_drag_reorder,
    "under_keyboard": c_under_keyboard,
    "overlay_blocker": c_overlay_blocker,
    "partial": c_partial,
}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("only", nargs="*", help="capture just these cases")
    ap.add_argument("-d", "--device", default=os.environ.get("ANDROID_SERIAL"))
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.list:
        print("\n".join(CASES))
        return 0
    todo = a.only or list(CASES)
    unknown = [x for x in todo if x not in CASES]
    if unknown:
        raise SystemExit(f"unknown case(s): {' '.join(unknown)}")
    c = Cap(dev.resolve_serial(a.device))
    failed = []
    try:
        for name in todo:
            print(name)
            try:
                CASES[name](c)
            except (SystemExit, Exception) as e:     # one bad case must not lose the others
                print(f"  FAILED: {e}", file=sys.stderr)
                failed.append(name)
    finally:
        c.client.close()
    if failed:
        print(f"failed: {' '.join(failed)}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
