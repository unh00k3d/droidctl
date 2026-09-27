"""TESTAPP group 8: spatial layout. Ground truth: DTA lines."""
import re
import time

import pytest

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


@pytest.mark.parametrize("name", ["cart", "cart_compose"])
def test_cart_regions_and_plus_right_of_the_item(scenario, name):
    sc = scenario(name)
    s = sc.snap()
    assert "-- top bar" in s["text"] and "-- bottom bar" in s["text"]
    dc("tap", "--text", "+", "--right-of", "USB-C Cable")
    ev = sc.dta("inc")
    assert len(ev) == 1 and ev[0]["id"] == "USB-C Cable"


def test_rtl_right_of_follows_the_screen_not_the_locale(scenario):
    sc = scenario("rtl")
    dc("tap", "--text", "+", "--left-of", "Wireless Mouse")     # on screen, "+" is left of it in RTL
    ev = sc.dta("inc")
    assert len(ev) == 1 and ev[0]["id"] == "Wireless Mouse"
    p = dc("tap", "--text", "+", "--right-of", "Laptop Stand", ok=False)
    assert not p.get("ok") and kind(p) in ("not-found", "ambiguous")
    assert len(sc.dta("inc")) == 1                                # nothing else was tapped


def test_calendar_grid_and_the_14th(scenario):
    sc = scenario("calendar")
    s = sc.snap()
    assert "grid 6x7" in s["text"]
    dc("tap", "--text", "14", "--role", "button")
    assert [e["day"] for e in sc.dta("click", "day14")] == [14]


def test_calendar_compose_the_14th(scenario):
    sc = scenario("calendar_compose")
    assert "grid 5x7" in sc.snap()["text"]
    dc("tap", "--text", "14")
    assert [e["day"] for e in sc.dta("click", "day14")] == [14]


def test_keypad_grid_and_pin(scenario):
    sc = scenario("keypad")
    assert "grid 4x3" in sc.snap()["text"]
    steps = sum((["--step", f"tap --text {d} --role button"] for d in "4821"), []) + ["--step", "tap --text OK"]
    r = dc("run", *steps)
    assert r["failed"] == 0
    assert [e["value"] for e in sc.dta("pin")] == ["4821"]


def test_photo_grid_unlabeled_tiles_are_named_by_position(scenario):
    sc = scenario("photo_grid")
    s = sc.snap()
    m = re.search(r"\[(\d+)\]\?\(row 2 col 1\)", s["text"])
    assert m, s["text"]
    dc("tap", m.group(1))
    ev = sc.dta("click", "photo4")
    assert len(ev) == 1 and (ev[0]["row"], ev[0]["col"]) == (2, 1)


def test_unlabeled_icons_are_inferred_from_ids(scenario):
    sc = scenario("unlabeled_icons")
    s = sc.snap()
    assert "share?" in s["text"] and "delete?" in s["text"]
    ref = next(e["ref"] for e in s["elements"] if e.get("inferred") == "delete")
    dc("tap", ref)
    assert len(sc.dta("click", "ic_delete")) == 1


def test_label_left_form_inputs_by_their_visual_label(scenario):
    sc = scenario("label_left_form")
    s = sc.snap()
    assert 'right of "Email"' in s["text"]
    dc("type", "--role", "input", "--right-of", "Email", "ada@example.org")
    # two inputs are below "Password" (ambiguous); the agent picks the ref whose
    # context names it, as the snapshot shows: input (unlabeled, below "Password")
    pw = next(e["ref"] for e in s["elements"] if e["role"] == "input" and e["context"] == ["below", "Password"])
    dc("type", pw, "hunter22")
    assert sc.dta("text", "f_email")[-1]["value"] == "ada@example.org"
    assert sc.dta("text", "f_password")[-1]["value"] == "hunter22"   # this app logs it plainly


def test_cards_below_disambiguates(scenario):
    sc = scenario("cards")
    assert kind(dc("tap", "--text", "Buy", ok=False)) == "ambiguous"
    dc("tap", "--text", "Buy", "--below", "Premium")
    assert [e["plan"] for e in sc.dta("buy")] == ["Premium"]


def test_fab_sheet_and_drawer_regions(scenario):
    sc = scenario("fab_sheet_drawer")
    s = sc.snap()
    assert "-- fab" in s["text"] and "-- sheet" in s["text"]
    fab = next(e for e in s["elements"] if e["label"] == "Add")
    assert fab["region"] == "fab" and fab["parent"] is None        # not merged into a list row
    dc("tap", fab["ref"])
    assert len(sc.dta("click", "fab")) == 1
    dc("tap", "--desc", "Open navigation drawer")
    s = until(lambda: (lambda x: x if "-- drawer" in x["text"] else None)(sc.snap()))
    assert s, sc.snap()["text"]
    dc("back")


def test_layout_bugs_elements_are_listed_and_tappable(scenario):
    sc = scenario("layout_bugs")
    s = sc.snap()
    for lbl in ("Left", "Right", "x"):
        assert lbl in [e["label"] for e in s["elements"]]
    dc("tap", "--text", "x")
    assert len(sc.dta("click", "tiny")) == 1


@pytest.mark.xfail(strict=True, reason="`layout-check` is post-v1 (PLAN.md Command inventory: Dev); "
                                       "not implemented, so TESTAPP's layout_bugs report can't be checked")
def test_layout_check_reports_overlap_ellipsis_and_small_targets(scenario):
    scenario("layout_bugs")
    r = dc("layout-check")
    assert {"overlap", "ellipsized", "small-target"} <= {f["kind"] for f in r["findings"]}
