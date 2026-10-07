"""The `diagnose` command's classifier: why a screen is unreadable, and what to do.

The status logic is pure (`cli._diag_status`), so it is tested directly, plus drift
guards that every status it can return has a cause line and a fix. See PLAN.md
"Device backends" for the hardened-app behaviour this diagnoses."""
import inspect

from droidctl import cli


def st(**kw):
    """_diag_status with sensible defaults; override only the signals a case cares about."""
    base = dict(has_app_window=True, has_blocked=False, has_readable=True, leaving=False,
                degraded=False, n_elements=10, backend="a11y", a11y_on=False,
                has_other_services=False, has_ua_procs=False)
    base.update(kw)
    return cli._diag_status(**base)


# --- the readable / degenerate screens --------------------------------------
def test_a_readable_app_window_with_elements_is_ok():
    assert st() == "ok"


def test_no_application_window_at_all():
    assert st(has_app_window=False, has_readable=False, n_elements=0) == "no-app-window"


def test_mid_transition_beats_ok():
    assert st(leaving=True) == "transition"


def test_readable_but_empty_is_opaque():
    assert st(n_elements=0) == "opaque"


def test_degraded_without_a_blocked_window_says_degraded():
    # a readable window that is only partial (truncated), not a hardened block
    assert st(has_readable=False, degraded=True, n_elements=0) == "degraded"


# --- the blocked app window: cause depends on what is enabled ----------------
def test_hardened_app_with_our_service_on_backend_a():
    assert st(has_blocked=True, has_readable=False, n_elements=0, a11y_on=True) == "hardened-a11y"


def test_backend_b_but_our_service_still_on_is_the_footgun():
    assert st(has_blocked=True, has_readable=False, n_elements=0, a11y_on=True,
              backend="uiautomation") == "hardened-ua-service-on"


def test_a_non_droidctl_service_we_cannot_touch():
    assert st(has_blocked=True, has_readable=False, n_elements=0,
              has_other_services=True) == "hardened-other-service"


def test_a_uiautomator_client_outranks_an_enabled_service():
    # if Appium/uiautomator holds UiAutomation, that is the cause to name first
    assert st(has_blocked=True, has_readable=False, n_elements=0, a11y_on=True,
              has_ua_procs=True) == "suppressed-ua"


def test_blocked_with_nothing_enabled_is_latched_or_slow():
    assert st(has_blocked=True, has_readable=False, n_elements=0) == "latched-or-slow"


def test_a_blocked_window_is_never_reported_ok_even_with_elements_elsewhere():
    # a blocked app window is the signal; sibling readable windows don't excuse it
    assert st(has_blocked=True, n_elements=10, a11y_on=True) == "hardened-a11y"


# --- drift guards: every status is explained and has a remedy ----------------
def _all_statuses():
    """Every status _diag_status can return, read from its source (so a new branch
    that forgets a cause/fix fails here)."""
    src = inspect.getsource(cli._diag_status)
    import re
    return set(re.findall(r'return "([a-z0-9-]+)"', src))


def test_every_status_has_a_cause_line():
    assert _all_statuses() <= set(cli._DIAG), \
        f"missing _DIAG entries: {_all_statuses() - set(cli._DIAG)}"


def test_every_cause_line_is_a_reachable_status():
    assert set(cli._DIAG) <= _all_statuses(), \
        f"_DIAG has statuses _diag_status never returns: {set(cli._DIAG) - _all_statuses()}"


def test_every_status_has_a_fix():
    for status in cli._DIAG:
        fix = cli._diag_fix(status, others=["com.example/.Svc"])
        assert isinstance(fix, list)
        assert (fix == []) == (status == "ok"), f"{status}: only 'ok' has an empty fix"
