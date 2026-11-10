"""droidctl's own iOS test app, Forms tab: the keyboard and field cases the S1 app lacked
(IOS-HANDOFF.md §7.2), judged by the app's DTA log.

Needs the app installed (scripts/ios_testapp.py -d <UDID>; a free team's slot frees up after the
S1 expiry experiment) and DROIDCTL_IOS_TESTAPP=<its bundle id>. Skipped with the S1 app."""
import json

import pytest

from tests.e2e.ios.test_i2 import APP, dc, dta  # noqa: F401  (dta is the module fixture)

pytestmark = pytest.mark.skipif(".s1." in APP, reason="the S1 app has no Forms tab: install ios/testapp")


@pytest.fixture(scope="module")
def forms(dta):  # noqa: F811
    dc("tap", "--text", "Forms")
    assert dc("wait", "--id", "dta_forms_title", "--timeout", "5")["matched"]
    dta.since()
    return dta


def test_a_number_pad_with_a_done_bar(forms):
    r = dc("type", "--id", "dta_num_done", "123")
    assert r["verified"] and r["screen"]["keyboard"]
    assert forms.since()[-1] == "dta_num_done text=123"
    dc("tap", "--id", "dta_num_done_button")
    got = forms.since()
    assert "dta_num_done done" in got and "dta_num_done editing_ended" in got


def test_a_number_pad_without_done_cannot_hide_by_return_and_says_so(forms):
    dc("type", "--id", "dta_num_plain", "42")
    assert forms.since()[-1] == "dta_num_plain text=42"
    h = dc("hide-keyboard", ok=False)
    if h.get("ok"):                                   # however it got the keyboard down, it must be down
        assert not h["screen"]["keyboard"]
    else:
        assert h["error"]["kind"] in ("unsupported", "not-found"), h
    dc("tap", "--id", "dta_forms_title")


def test_a_secure_field_takes_text_and_never_shows_it(forms):
    r = dc("type", "--id", "dta_secure", "hunter22")
    assert forms.since()[-1] == "dta_secure length=8"
    assert "hunter22" not in json.dumps(r) and "hunter22" not in json.dumps(dc("snapshot", "--full"))


def test_a_masked_field_ends_formatted(forms):
    dc("type", "--id", "dta_masked", "5551234567")
    assert forms.since()[-1] == "dta_masked text=(555) 123-4567"
    snap = dc("snapshot", "--full")
    assert any(e.get("id") == "dta_masked" and "(555) 123-4567" in json.dumps(e) for e in snap["elements"])


def test_a_splash_then_home_is_waited_for(forms):
    dc("tap", "--id", "dta_splash")
    assert dc("wait", "--text", "Welcome home", "--timeout", "5")["matched"]
    got = forms.since()
    assert got.index("splash shown") < got.index("home shown")
    dc("tap", "--text", "Close")
    assert forms.since()[-1] == "home closed"
