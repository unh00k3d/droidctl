"""TESTAPP group 4: text input. Ground truth: DTA lines."""
import time

from tests.e2e.conftest import dc, SERIAL


def until(fn, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.2)
    return fn()


def last_value(sc, id):
    ev = sc.dta("text", id)
    return ev[-1]["value"] if ev else None


def test_form_annotations_multiline_and_max_length(scenario):
    sc = scenario("form")
    s = sc.snap()
    assert 'hint="Name"' in s["text"] and 'error="Invalid email"' in s["text"] and "empty" in s["text"]
    dc("type", "--id", "notes", "line one\nline two")
    assert last_value(sc, "notes") == "line one\nline two"          # the newline survives
    r = dc("type", "--id", "code", "1234567")
    assert r["value"] == "12345" and r.get("warning")               # maxLength reported, not hidden
    dc("type", "--id", "email", "ada@example.org")
    assert 'error="Invalid email"' not in sc.snap()["text"]
    dc("type", "--id", "age", "42")
    dc("tap", "--text", "Submit")
    sub = sc.dta("submit", "submit")
    assert len(sub) == 1 and sub[0]["code"] == "12345" and sub[0]["age"] == "42"
    assert sub[0]["email"] == "ada@example.org"


def test_form_compose(scenario):
    sc = scenario("form_compose")
    dc("type", "--id", "name", "Ada")
    dc("type", "--id", "email", "ada@example.org")
    r = dc("type", "--id", "password", "s3cret!")
    assert r.get("value") is None and r["verified"]
    dc("tap", "--text", "Submit")
    sub = sc.dta("submit", "submit")
    assert len(sub) == 1 and sub[0]["name"] == "Ada" and sub[0]["email"] == "ada@example.org"
    assert sc.dta("text", "password")[-1]["len"] == 7


def test_formatter_value_is_the_formatted_text(scenario):
    sc = scenario("formatter")
    r = dc("type", "--id", "phone", "2125550100")
    assert r["ok"] and r["value"] != "2125550100"          # the app reformatted it
    assert "".join(ch for ch in r["value"] if ch.isdigit()) == "2125550100"
    assert last_value(sc, "phone") == r["value"]


def test_reject_set_text_falls_back_to_paste_and_restores_the_clipboard(scenario):
    from droidctl import device as dev
    sc = scenario("reject_set_text")
    c, _ = dev.connect(SERIAL)
    try:
        c.clipboard(set="droidctl-e2e-sentinel")
        r = dc("type", "--id", "message", "pasted text")
        assert r["method"] == "paste", r
        assert c.clipboard().get("previous") == "droidctl-e2e-sentinel"
    finally:
        c.clipboard(set="")
        c.close()
    assert last_value(sc, "message") == "pasted text"


def test_otp_fills_all_six_boxes(scenario):
    sc = scenario("otp")
    dc("type", "--id", "otp1", "123456", ok=None)
    ev = until(lambda: sc.dta("otp", "otp"))
    assert ev and ev[-1]["value"] == "123456"


def test_search_enter(scenario):
    sc = scenario("search_enter")
    dc("type", "--id", "search_src_text", "kittens", "--enter")
    ev = until(lambda: sc.dta("search", "search"))
    assert ev and ev[-1]["value"] == "kittens"


def test_webview_form(scenario):
    sc = scenario("webview_form")
    until(lambda: "City" in sc.snap()["text"], timeout=8)
    dc("type", "--id", "city", "Istanbul")
    assert until(lambda: [e for e in sc.dta("text", "web") if e["value"] == "Istanbul"])
    dc("tap", "--text", "Send")
    ev = until(lambda: sc.dta("submit", "web"))
    assert ev and ev[-1]["value"] == "Istanbul"
