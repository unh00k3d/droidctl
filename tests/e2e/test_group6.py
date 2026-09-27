"""TESTAPP group 6: windows, dialogs, system UI. Ground truth: DTA lines."""
import json
import os
import subprocess
import sys
import time

from tests.e2e.conftest import adb, dc, Scenario, SERIAL


def until(fn, timeout=6):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.2)
    return fn()


def labels(s):
    return [e["label"] for e in s["elements"]]


def test_dialogs_are_flagged_and_dismissed_with_back(scenario):
    sc = scenario("dialogs")
    for opener, marker, key in (("Alert", "Delete file?", "alert"), ("Full screen", "Full screen dialog", "full"),
                                ("Sheet", "Share via", "sheet")):
        dc("tap", "--text", opener, "--role", "button")
        s = until(lambda: (lambda x: x if marker in labels(x) else None)(sc.snap()))
        assert s and s["screen"]["dialog"], (opener, s and s["text"])
        assert "Alert" not in labels(s)                   # only the dialog, not the screen behind it
        dc("back")
        assert until(lambda: sc.dta("dismiss", key)), key
        assert "Alert" in labels(sc.snap())


def test_toast_in_the_header_when_it_fired_outside_droidctl(scenario):
    sc = scenario("snackbar_toast")
    s = sc.snap()                                          # records the event-ring position
    x, y = next(e for e in s["elements"] if e["label"] == "Toast")["tap"]
    adb("shell", "input", "tap", str(x), str(y))          # not through droidctl
    snap = until(lambda: (lambda p: p if p.get("toast") else None)(dc("snapshot", "--full")))
    assert snap and snap["toast"] == "Saved!" and 'toast="Saved!"' in snap["text"]
    assert not dc("snapshot", "--full").get("toast")     # reported once, not again
    del sc


def test_toast_in_the_header_through_the_daemon(scenario, tmp_path):
    sc = scenario("snackbar_toast")
    env = {k: v for k, v in os.environ.items() if k not in ("DROIDCTL_NO_DAEMON", "DROIDCTL_HOME")}
    env.update(DROIDCTL_HOME=str(tmp_path), ANDROID_SERIAL=SERIAL, DROIDCTL_IDLE="120s")

    def client(*argv):
        out = subprocess.run([sys.executable, "-m", "droidctl.client", *argv, "--json"], env=env,
                             capture_output=True, text=True, timeout=60)
        return json.loads(out.stdout)
    try:
        s = client("snapshot", "--full")
        assert s["mode"] == "daemon"
        x, y = next(e for e in s["elements"] if e["label"] == "Toast")["tap"]
        adb("shell", "input", "tap", str(x), str(y))
        time.sleep(1.0)
        p = client("snapshot", "--full")
        assert p.get("toast") == "Saved!", p.get("text")
        assert any(e.get("type") == "toast" for e in (p.get("between_calls") or [])), p.get("between_calls")
    finally:
        subprocess.run([sys.executable, "-m", "droidctl", "daemon", "stop"], env=env,
                       capture_output=True, timeout=30)
    del sc


def test_snackbar_action_is_tappable(scenario):
    sc = scenario("snackbar_toast")
    dc("tap", "--text", "Snackbar")
    until(lambda: "UNDO" in labels(sc.snap()) or "Undo" in labels(sc.snap()))
    lbl = "UNDO" if "UNDO" in labels(sc.snap()) else "Undo"
    dc("tap", "--text", lbl)
    assert until(lambda: sc.dta("undo", "snackbar"))


def test_permission_dialog_is_a_foreign_package_and_allow_works(scenario):
    adb("shell", "pm", "revoke", "dev.droidctl.testapp", "android.permission.CAMERA", check=False)
    try:
        sc = scenario("permission")
        dc("tap", "--text", "Request camera")
        s = until(lambda: (lambda x: x if "Allow" in labels(x) else None)(sc.snap()))
        assert s and s["screen"]["pkg"] != "dev.droidctl.testapp", s and s["text"]
        assert s["screen"]["pkg"] in s["text"]              # the header names the foreign package
        dc("tap", "--text", "Allow")
        ev = until(lambda: sc.dta("permission", "camera"))
        assert ev and ev[-1]["granted"] is True
    finally:
        adb("shell", "pm", "revoke", "dev.droidctl.testapp", "android.permission.CAMERA", check=False)


def test_notification_shade_and_its_action(scenario):
    """Read-only on the shade: only our own notification's action is tapped; the
    user's other notifications are never touched or saved."""
    sc = scenario("notification")
    dc("tap", "--text", "Notify")
    assert sc.dta("posted", "notification")
    try:
        dc("notifications")
        s = until(lambda: (lambda x: x if any("droidctl test notification" in (l or "") for l in labels(x))
                           else None)(dc("snapshot", "--full", "--system")))
        assert s, "our notification is not in the shade"
        if not any((l or "") == "Mark read" for l in labels(s)):
            ours = next(e for e in s["elements"] if "droidctl test notification" in (e["label"] or ""))
            dc("expand", ours["ref"], ok=None)
        # SystemUI's notification stack reports stale bounds for rows it has moved
        # (the next row's a11y box still overlaps "Mark read" after the expand; a
        # screenshot shows it fully visible), so a plain tap is refused as occluded.
        # The error's own hint, --method action, acts on the node and works.
        p = dc("tap", "--text", "Mark read", ok=None)
        if not p.get("ok"):
            assert p["error"]["kind"] == "occluded" and "--method action" in p["error"]["hint"]
            dc("tap", "--text", "Mark read", "--method", "action")
        assert until(lambda: sc.dta("action", "mark_read"))
    finally:
        dc("back", ok=None)
        if until(lambda: dc("current")["pkg"] == "com.android.systemui", timeout=1):
            dc("back", ok=None)


def test_deep_link_opens_the_form(scenario):
    scenario("deep_link")
    form = Scenario("form")
    r = dc("open-url", "droidctl-test://s/form")
    assert r["ok"]
    assert until(lambda: form.dta("shown"))
    assert "Form" in labels(dc("snapshot", "--full"))


def ime_shown():
    """Ground truth from the IME service itself. The test app's own `ime` DTA line
    uses WindowInsetsCompat.isVisible(ime()), which never fires on API 28."""
    return "mInputShown=true" in adb("shell", "dumpsys", "input_method")


def test_keyboard_is_tracked_and_its_nodes_never_listed(scenario):
    sc = scenario("keyboard_toggle")
    dc("tap", "--id", "input")
    s = until(lambda: (lambda x: x if x["screen"]["keyboard"] else None)(sc.snap()))
    assert s and s["screen"]["keyboard"] and ime_shown()
    raw = dc("snapshot", "--raw")["raw"]
    ime = {w["id"] for w in raw["windows"] if w.get("type") in ("input_method", "ime")}
    assert ime and not [e for e in s["elements"] if e["window"] in ime]
    dc("tap", "--text", "Hide keyboard")
    assert until(lambda: not sc.snap()["screen"]["keyboard"])
    assert until(lambda: not ime_shown())
