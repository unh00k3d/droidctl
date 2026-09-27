"""TESTAPP group 5: lists and scrolling. Ground truth: DTA lines."""
import time

from tests.e2e.conftest import dc


def kind(p):
    return (p.get("error") or {}).get("kind")


def until(fn, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.2)
    return fn()


def test_long_list_counts_stays_small_and_scroll_to_reaches_row_734(scenario):
    sc = scenario("long_list")
    s = sc.snap()
    assert "/1000 more↓" in s["text"] and s["tokens_est"] < 2000
    r = dc("scroll-to", "--text", "Row 734", "--max-scrolls", "120", timeout=300)
    assert r["found"]
    dc("tap", r["ref"])
    assert [e["id"] for e in sc.dta("click")] == ["Row 734"]


def test_list_compose_scroll_to_and_tap(scenario):
    sc = scenario("list_compose")
    r = dc("scroll-to", "--text", "Row 40", "--max-scrolls", "30", timeout=180)
    assert r["found"]
    dc("tap", r["ref"])
    assert [e["id"] for e in sc.dta("click")] == ["Row 40"]


def test_list_insert_top_old_ref_heals_by_identity(scenario):
    sc = scenario("list_insert_top", interval_ms=1500)
    ref = sc.ref("Item 3")
    assert until(lambda: sc.dta("insert"), timeout=6)       # the rows shifted under the ref
    dc("tap", ref)
    assert [e["id"] for e in sc.dta("click")] == ["Item 3"]


def test_duplicates_bare_text_is_ambiguous_and_row_context_resolves(scenario):
    sc = scenario("duplicates")
    p = dc("tap", "--text", "Delete", ok=False)
    assert kind(p) == "ambiguous" and p["error"]["data"]["count"] >= 2
    r = dc("tap", "--text", "Delete", "--right-of", "Item 7")
    ev = sc.dta("delete", "delete")
    assert len(ev) == 1 and ev[0]["row"] == 7
    # the row is removed, so the agent can see its delete worked (TESTAPP)
    assert r["changed"]
    labels = [e["label"] or "" for e in sc.snap()["elements"]]
    assert not any(l.startswith("Item 7") for l in labels) and any(l.startswith("Item 8") for l in labels)


def test_duplicates_compose_row_context_resolves(scenario):
    sc = scenario("duplicates_compose")
    assert kind(dc("tap", "--text", "Delete", ok=False)) == "ambiguous"
    r = dc("tap", "--text", "Delete", "--right-of", "Item 2")
    ev = sc.dta("delete", "delete")
    assert len(ev) == 1 and ev[0]["row"] == 2
    assert r["changed"]
    labels = [e["label"] or "" for e in sc.snap()["elements"]]
    assert "Item 2" not in labels and "Item 3" in labels


def test_nested_scroll_moves_only_the_carousel(scenario):
    sc = scenario("nested_scroll")
    s = sc.snap()
    car = next(e for e in s["elements"] if e["role"] == "pager")
    story1 = next(e for e in s["elements"] if e["label"] == "Story 1")
    dc("scroll", "right", car["ref"])
    assert until(lambda: sc.dta("scroll", "carousel"))
    s2 = sc.snap()
    st = next(e for e in s2["elements"] if e["label"] == "Story 1")
    assert st["bounds"] == story1["bounds"]                   # the feed did not move
    assert "Card 1" not in [e["label"] for e in s2["elements"]]


def test_infinite_scroll_to_stops_at_its_cap_with_a_clear_message(scenario):
    sc = scenario("infinite")
    p = dc("scroll-to", "--text", "Post 9999", "--max-scrolls", "6", ok=False, timeout=180)
    assert not p.get("ok")
    assert kind(p) in ("not-found", "timeout"), p
    assert "6" in p["error"]["message"] and "scroll" in p["error"]["message"]
    assert sc.dta("load_more")
