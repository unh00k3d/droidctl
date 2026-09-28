"""Stable refs (snapshot.carry_refs) on REAL before/after pairs.

The spatial A/B (2026-09-28) found agents deleting the wrong row: after a `scroll`,
refs were renumbered, and an agent reusing the number from its earlier snapshot hit
another row's Delete. On one screen a number now keeps meaning one element.
"""
import pytest

from droidctl import resolve as R
from droidctl import snapshot as S
from droidctl.core import UserError
from tests.test_resolve import build, load


def saved(snap, prev=None):
    """What the agent saw: carry against `prev`, then the saved state."""
    S.carry_refs(prev, snap)
    return S.to_state(snap)


def ref_of(state, label, role=None, ctx_has=None):
    hits = [int(k) for k, r in state["refs"].items() if r["label"] == label
            and (role is None or r["role"] == role) and (ctx_has is None or ctx_has in (r.get("ctx") or []))]
    assert len(hits) == 1, (label, ctx_has, hits)
    return hits[0]


def pair(name):
    a, b = load(f"{name}-a"), load(f"{name}-b")
    sa = saved(build(a))
    snap_b = build(b)
    sb = saved(snap_b, sa)
    return a, b, sa, snap_b, sb


def test_a_scroll_keeps_every_row_its_number():
    a, b, sa, snap_b, sb = pair("duplicates_scroll")
    for item in ("Item 7", "Item 12"):
        assert ref_of(sb, "Delete", ctx_has=item) == ref_of(sa, "Delete", ctx_has=item)
        assert ref_of(sb, item) == ref_of(sa, item)
    assert len({e.ref for e in snap_b.elements}) == len(snap_b.elements)       # no number twice
    # rows that scrolled in got numbers never used on this screen
    fresh = [e.ref for e in snap_b.elements if str(e.ref) not in sa["refs"]]
    assert fresh and min(fresh) > max(map(int, sa["refs"]))


def test_a_number_that_scrolled_away_is_not_reused_and_fails_typed():
    a, b, sa, snap_b, sb = pair("duplicates_scroll")
    gone = ref_of(sa, "Item 1")
    assert str(gone) not in sb["refs"] and sb["retired"][str(gone)]["label"] == "Item 1"
    with pytest.raises(UserError) as e:
        R.resolve_ref(sb, gone, b["tree"])
    assert e.value.kind == "offscreen"          # not "the element that has its number now"


def test_the_old_delete_of_a_gone_row_never_hits_another_row():
    """The A/B failure itself: `tap N` with N from before the scroll."""
    a, b, sa, snap_b, sb = pair("duplicates_scroll")
    old_item1_delete = ref_of(sa, "Delete", ctx_has="Item 1")
    with pytest.raises(UserError) as e:
        R.resolve_ref(sb, old_item1_delete, b["tree"])
    assert e.value.kind in ("offscreen", "stale-ref")


def test_a_row_inserted_at_the_top_shifts_nothing():
    a, b, sa, snap_b, sb = pair("list_insert_top")
    for item in ("Item 1", "Item 5", "Item 10"):
        assert ref_of(sb, item) == ref_of(sa, item)


def test_a_changed_quantity_keeps_its_row_number():
    a, b, sa, snap_b, sb = pair("cart_inc")
    assert ref_of(sb, "2", ctx_has="Wireless Mouse") == ref_of(sa, "1", ctx_has="Wireless Mouse")
    assert ref_of(sb, "+", ctx_has="USB-C Cable") == ref_of(sa, "+", ctx_has="USB-C Cable")


def test_a_reorder_keeps_numbers_with_their_elements():
    a, b, sa, snap_b, sb = pair("drag_reorder")
    for t in ("Task 1", "Task 2", "Task 5"):
        assert ref_of(sb, t) == ref_of(sa, t)


def test_the_same_screen_twice_is_numbered_the_same():
    a, b, sa, snap_b, sb = pair("static_twice")
    assert {k: r["label"] for k, r in sa["refs"].items()} == {k: r["label"] for k, r in sb["refs"].items()}
    assert not sb["retired"]


@pytest.mark.parametrize("a_name,b_name", [("lookalike_ok-a", "lookalike_ok-b"),
                                           ("real-settings-main", "real-settings-display")])
def test_a_different_screen_starts_at_one(a_name, b_name):
    sa = saved(build(load(a_name)))
    snap_b = build(load(b_name))
    sb = saved(snap_b, sa)
    assert sorted(e.ref for e in snap_b.elements) == list(range(1, len(snap_b.elements) + 1))
    assert not sb["retired"]


def test_layout_stays_in_reading_order_whatever_the_numbers():
    a, b, sa, snap_b, sb = pair("duplicates_scroll")
    text = S.render(snap_b, S.Opts())
    rows = [line for line in text.splitlines() if '"Item ' in line]
    items = [int(line.split('"Item ')[1].split('"')[0]) for line in rows]
    assert items == sorted(items), text


def test_carry_is_idempotent_and_state_round_trips():
    a, b, sa, snap_b, sb = pair("duplicates_scroll")
    before = [e.ref for e in snap_b.elements]
    S.carry_refs(sa, snap_b)
    assert [e.ref for e in snap_b.elements] == before
    assert sb["next_ref"] > max(before)


def test_numbers_restart_once_they_grow_large():
    a, b = load("duplicates_scroll-a"), load("duplicates_scroll-b")
    sa = saved(build(a))
    sa["next_ref"] = S.REF_RESET + 1
    snap_b = build(b)
    S.carry_refs(sa, snap_b)
    assert sorted(e.ref for e in snap_b.elements) == list(range(1, len(snap_b.elements) + 1))
