"""TESTAPP group 1: timing and app state. Ground truth: DTA lines."""
import time

from tests.e2e.conftest import adb, dc


def kind(p):
    return (p.get("error") or {}).get("kind")


def labels(snap):
    return [e["label"] for e in snap["elements"]]


def test_splash_warns_tiny_tree_then_wait_finds_home(scenario):
    sc = scenario("splash", splash_ms=8000)
    s = sc.snap()
    assert any("tiny tree" in w for w in s["warnings"]), s["text"]
    assert dc("wait", "--text", "Home", "--timeout", "15")["matched"]
    assert len(sc.dta("content_shown")) == 1


def test_delayed_short_timeout_then_success(scenario):
    sc = scenario("delayed", delay_ms=3000)
    p = dc("wait", "--text", "Loaded", "--timeout", "0.5", ok=False)
    assert kind(p) == "timeout"
    assert dc("wait", "--text", "Loaded", "--timeout", "10")["matched"]
    assert len(sc.dta("content_shown")) == 1


def test_spinner_forever_settles_at_its_cap_and_the_action_succeeds(scenario):
    sc = scenario("spinner_forever")
    assert any(e["role"] == "progress" for e in sc.snap()["elements"])
    t0 = time.time()
    r = dc("tap", "--text", "Do it")
    assert time.time() - t0 < 8
    assert r["ok"] and r["method"] == "action"
    assert len(sc.dta("click", "do_it")) == 1


def test_skeleton_has_no_placeholder_junk_and_wait_gone_works(scenario):
    sc = scenario("skeleton", delay_ms=6000)
    s = sc.snap()
    assert not any(l and l.startswith("Result ") for l in labels(s))
    assert len(s["elements"]) <= 3, s["text"]      # heading + "Loading", no shimmer boxes
    assert dc("wait", "--desc", "Loading", "--gone", "--timeout", "10")["matched"]
    assert len(sc.dta("loaded")) == 1
    assert sum(1 for l in labels(sc.snap()) if l and l.startswith("Result ")) == 6


def test_ticker_settle_is_bounded_and_the_signature_is_stable(scenario):
    sc = scenario("ticker")
    a = sc.snap()
    t0 = time.time()
    r = dc("tap", "--text", "Tap")
    assert time.time() - t0 < 8 and r["ok"]
    assert len(sc.dta("click", "tap")) == 1
    b = sc.snap()
    assert a["screen"]["sig"] == b["screen"]["sig"]
    d = dc("snapshot", "--diff")
    changed = [x for x in (d["diff"] or []) if not x.lstrip("+-~ ").startswith("[")]
    assert all(" s" in x for x in (d["diff"] or [])), d["diff"]   # only the clock text moves
    del changed


def test_slow_click_short_settle_does_not_retry(scenario):
    sc = scenario("slow_click", delay_ms=2000)
    r = dc("tap", "--text", "Slow", "--role", "button", "--settle", "500")
    assert r["method"] == "action"
    assert dc("wait", "--text", "Done", "--timeout", "6")["matched"]
    assert len(sc.dta("click", "slow")) == 1 and len(sc.dta("done", "slow")) == 1


def test_disabled_button_is_an_error_not_a_silent_noop(scenario):
    sc = scenario("disabled_then_enabled", delay_ms=6000)
    s = sc.snap()
    sub = next(e for e in s["elements"] if e["label"] == "Submit")
    assert "disabled" in sub["annotations"]
    p = dc("tap", "--text", "Submit", ok=False)
    assert kind(p) == "disabled"
    deadline = time.time() + 10
    while not sc.dta("enabled", "submit") and time.time() < deadline:
        time.sleep(0.2)
    r = dc("tap", "--text", "Submit")
    assert r["method"] == "action"
    assert len(sc.dta("click", "submit")) == 1


def test_error_retry_reaches_content(scenario):
    sc = scenario("error_retry")
    s = sc.snap()
    assert "Network error. Please try again." in labels(s) and "Retry" in labels(s)
    dc("tap", "--text", "Retry")
    assert dc("wait", "--text", "Welcome back", "--timeout", "6")["matched"]
    assert len(sc.dta("click", "retry")) == 1 and len(sc.dta("content_shown")) == 1


def test_pull_refresh_via_swipe_down(scenario):
    sc = scenario("pull_refresh")
    dc("swipe", "down")
    deadline = time.time() + 5
    while not sc.dta("refresh") and time.time() < deadline:
        time.sleep(0.2)
    assert len(sc.dta("refresh")) == 1


def test_ui_hang_degrades_the_read_without_hanging_the_service(scenario):
    sc = scenario("ui_hang", hang_ms=4500)      # under the 5 s input-ANR threshold
    dc("tap", "--text", "Hang", "--role", "button", "--method", "gesture", "--settle", "0")
    t0 = time.time()
    s = dc("snapshot", "--full", timeout=30)
    took = time.time() - t0
    assert s["screen"]["degraded"], s["text"]
    assert took < 6, took
    deadline = time.time() + 10
    while not sc.dta("unhung", "hang") and time.time() < deadline:
        time.sleep(0.2)
    assert len(sc.dta("unhung", "hang")) == 1
    assert dc("ping")["ok"]                     # the service survived
    assert not sc.snap()["screen"]["degraded"]


def test_crash_is_reported_and_never_followed_by_a_positional_tap(scenario):
    """TESTAPP expects a "keeps stopping" system dialog; Samsung's Android 9 on the
    SM-N950F just closes the app (no dialog), so the dialog is optional here. The
    crash makes ACTION_CLICK report performed:false; the gesture fallback must not
    then tap the launcher that is now at those coordinates."""
    sc = scenario("crash")
    r = dc("tap", "--text", "Crash", "--role", "button", "--settle", "0", ok=None)
    assert r.get("method") != "gesture-fallback", r
    deadline = time.time() + 10
    cur = {}
    while time.time() < deadline:
        cur = dc("current", ok=None)
        if cur.get("pkg") and cur.get("pkg") != "dev.droidctl.testapp":
            break
        time.sleep(0.3)
    assert cur.get("pkg") != "dev.droidctl.testapp", cur
    # TESTAPP: `logs` shows the stack trace. The whole-device E log can be flooded by
    # other processes, so ask for the app: its crash comes from the crash buffer
    logs = dc("logs", "--pkg", "dev.droidctl.testapp", "--max", "100")
    assert "deliberate crash" in logs["text"] and any("FATAL EXCEPTION" in x for x in logs["crash"])
    s = dc("snapshot", "--full")
    if s["screen"]["dialog"]:
        dc("back", ok=None)                      # dismiss the crash dialog only
    del sc


def test_recreate_an_old_ref_heals_through_the_tiers(scenario):
    sc = scenario("recreate")
    ref = sc.ref(id="keep")
    dc("tap", "--text", "Recreate", "--role", "button")
    assert len(sc.dta("click", "recreate_btn")) == 1
    r = dc("type", ref, "healed")
    assert r["ok"]
    texts = sc.dta("text", "keep")
    assert texts and texts[-1].get("value", texts[-1].get("text")) == "healed", texts
