"""The resolver against REAL trees: real-app captures (tests/fixtures/trees) and
test-app before/after pairs (tests/fixtures/resolve, captured with
scripts/capture_resolve_pairs.py). The expectations live in
tests/fixtures/resolve/cases.json so another implementation can reuse them.
"""
import argparse
import json
import pathlib

import pytest

from droidctl import cli, core
from droidctl import resolve as R
from droidctl import snapshot as S

ROOT = pathlib.Path(__file__).resolve().parent
TREES = ROOT / "fixtures" / "trees"
PAIRS = ROOT / "fixtures" / "resolve"
CASES = json.loads((PAIRS / "cases.json").read_text())
ALL_TREES = sorted(TREES.glob("real-*.json")) + sorted(PAIRS.glob("*-a.json"))


def load(name):
    for d in (PAIRS, TREES):
        p = d / f"{name}.json"
        if p.exists():
            return json.loads(p.read_text())
    pytest.skip(f"fixture {name} not captured (scripts/capture_resolve_pairs.py)")


def build(d):
    return S.build(d["tree"], activity=d["meta"].get("activity"))


def pick(state, sel):
    """The one ref in ``state`` matching a cases.json selector."""
    hits = [k for k, r in state["refs"].items()
            if ("role" not in sel or r["role"] == sel["role"])
            and ("label" not in sel or r["label"] == sel["label"])
            and ("ctx_has" not in sel or sel["ctx_has"] in r["ctx"])]
    assert len(hits) == 1, f"selector {sel} matches refs {hits}"
    return hits[0]


def check_target(res, want):
    rec = S.ref_record(res.elem, res.snap) if res.elem is not None else {
        "label": res.node.own_label(), "role": None, "ctx": S.context(res.node, [res.node.own_label()])}
    for k in ("label", "role"):
        if k in want:
            assert rec[k] == want[k], f"resolved to {rec['role']} {rec['label']!r}, wanted {want}"
    if "ctx_has" in want:
        assert want["ctx_has"] in rec["ctx"], f"resolved to one with ctx {rec['ctx']}, wanted {want}"


def check_outcome(run, exp):
    if exp["result"] == "ok":
        res = run()
        if "to" in exp:
            check_target(res, exp["to"])
        if "tier" in exp:
            assert res.tier == exp["tier"], f"tier {res.tier} ({res.via}), wanted {exp['tier']}"
        if "moved_min" in exp:
            assert res.moved >= exp["moved_min"]
        if "moved_max" in exp:
            assert res.moved <= exp["moved_max"]
        if "occluded" in exp:
            assert res.occluded and res.occluded["by"] == exp["occluded"]
        else:
            assert res.occluded is None
        for method, want in exp.get("methods", {}).items():
            if want == "ok":
                R.check_occlusion(res, method)
            else:
                with pytest.raises(core.UserError) as ei:
                    R.check_occlusion(res, method)
                assert ei.value.kind == want
        return res
    with pytest.raises(core.UserError) as ei:
        run()
    e = ei.value
    assert e.kind == exp["result"], f"{e.kind}: {e}"
    for k in ("reason", "direction"):
        if k in exp:
            assert (e.data or {}).get(k) == exp[k], f"{e.kind} {e.data}"
    assert e.kind in core.ERROR_KINDS
    return e


# --- cases.json -----------------------------------------------------------
@pytest.mark.parametrize("case", CASES["pairs"], ids=lambda c: c["name"])
def test_pair(case):
    da, db = load(case["a"]), load(case["b"])
    sa = build(da)
    state = S.to_state(sa)
    act_b = db["meta"].get("activity")
    if case.get("all") == "self":
        for ref, r in state["refs"].items():
            res = R.resolve_ref(state, ref, db["tree"], act_b)
            assert res.elem is not None and res.elem.label_full == r["label"] and res.tier == 2, ref
    elif case.get("all") == "stale-ref":
        for ref in state["refs"]:
            with pytest.raises(core.UserError) as ei:
                R.resolve_ref(state, ref, db["tree"], act_b)
            assert ei.value.kind == "stale-ref", f"[{ref}] {state['refs'][ref]['label']!r}: {ei.value}"
    for exp in case.get("expect", ()):
        ref = pick(state, exp["ref"])
        check_outcome(lambda: R.resolve_ref(state, ref, db["tree"], act_b), exp)


@pytest.mark.parametrize("case", CASES["locators"], ids=lambda c: c["name"])
def test_locator(case):
    snap = build(load(case["tree"]))
    check_outcome(lambda: R.find(snap, **case["find"]), case)


def test_every_capture_has_a_case():
    """A captured pair nobody checks is dead weight; a case without a capture is skipped."""
    named = {c[k] for c in CASES["pairs"] for k in ("a", "b")} | {c["tree"] for c in CASES["locators"]}
    captured = {p.stem for p in PAIRS.glob("*.json") if p.name != "cases.json"}
    assert captured <= named, f"uncovered captures: {sorted(captured - named)}"


# --- properties over every real tree ----------------------------------------
@pytest.mark.parametrize("path", ALL_TREES, ids=lambda p: p.stem)
def test_every_ref_resolves_to_itself_on_the_same_tree(path):
    """With the device's content moved on but the screen the same, a ref must come
    back to its own node: exact attributes, or (label-less twins) the tree path."""
    d = json.loads(path.read_text())
    snap = build(d)
    state = S.to_state(snap)
    for ref, r in state["refs"].items():
        res = R.resolve_ref(state, ref, d["tree"], d["meta"].get("activity"))
        assert res.handle == r["handle"], f"[{ref}] {r['label']!r} -> {res.to_json()['element']}"
        assert res.tier in (2, 3)


def test_fast_path_needs_the_same_dump_and_generation():
    d = load("cart_inc-a")
    state = S.to_state(build(d))
    dump, gen = state["dump"], state["gen"]
    hit = R.fast_path(state, 7, dump, gen)
    assert hit["tier"] == 0 and hit["handle"] == state["refs"]["7"]["handle"]
    assert R.fast_path(state, 7, dump, (gen or 0) + 1) is None      # content changed: re-resolve
    assert R.fast_path(state, 7, dump + 1, gen) is None             # a newer dump: handles moved on
    assert R.fast_path(state, 999, dump, gen) is None


def test_an_unknown_ref_is_stale():
    d = load("cart_inc-a")
    state = S.to_state(build(d))
    with pytest.raises(core.UserError) as ei:
        R.resolve_ref(state, 999, d["tree"])
    assert ei.value.kind == "stale-ref" and ei.value.data["reason"] == "gone"


def test_a_text_inside_a_row_clicks_its_clickable_ancestor():
    """Settings rows: the ref is the row, and the click handle is the clickable
    node, which for a merged row is the row itself."""
    d = load("real-settings-main")
    snap = build(d)
    res = R.find(snap, text="Display")
    assert res.click is not None and res.click == res.handle
    # the switch in a Settings row folds into the row: its click is the row's clickable node
    d2 = load("real-settings-display")
    res2 = R.find(build(d2), role="switch", text="Night mode")
    assert res2.click is not None


def test_ambiguous_errors_carry_candidates_through_the_json_envelope(capsys):
    d = load("duplicates_scroll-b")
    snap = build(d)

    def boom(a):
        R.find(snap, text="Delete")
    with pytest.raises(SystemExit):
        cli.dispatch(argparse.Namespace(json=True, fn=boom))
    err = json.loads(capsys.readouterr().out)["error"]
    assert err["kind"] == "ambiguous" and err["data"]["count"] > 10
    assert all(c["label"] == "Delete" for c in err["data"]["candidates"])


# --- small pure pieces ---------------------------------------------------------
@pytest.mark.parametrize("a,b", [("Inbox (3)", "inbox"), ("  Row  7 ", "row 7"), ("Cart [12]", "cart")])
def test_norm_drops_badges_case_and_spacing(a, b):
    assert R.norm(a) == R.norm(b)


def test_context_similarity():
    assert R.ctx_sim([], []) == 1.0
    assert R.ctx_sim(["Item 7"], ["item 7"]) == 1.0
    assert R.ctx_sim(["Screen A", "OK"], ["Screen B", "OK"]) == pytest.approx(1 / 3)
    assert R.ctx_compatible([], ["x"]) and not R.ctx_compatible(["Item 7"], ["Item 8"])


# --- degraded trees ---------------------------------------------------------
def _ref_labelled(state, label):
    return next(k for k, r in state["refs"].items() if r["label"] == label)


def test_fast_path_still_acts_on_a_partial_dump():
    """The handle from a partial dump is the exact node the agent saw: acting on
    it needs no uniqueness proof, so huge_tree stays actionable."""
    d = json.loads((TREES / "testapp-huge_tree.json").read_text())
    state = S.to_state(build(d))
    ref = _ref_labelled(state, "L1.3")
    hit = R.fast_path(state, ref, state["dump"], state["gen"])
    assert hit["tier"] == 0 and hit["handle"] == state["refs"][ref]["handle"]


def test_a_partial_tree_never_rematches_in_an_incomplete_window():
    """After a change, a partial read can't prove a match is unique (the real one
    may be in the unread part), so re-resolution refuses with a typed error."""
    d = json.loads((TREES / "testapp-huge_tree.json").read_text())
    state = S.to_state(build(d))
    ref = _ref_labelled(state, "L1.3")
    tree = dict(d["tree"], dump=d["tree"]["dump"] + 1)
    with pytest.raises(core.UserError) as e:
        R.resolve_ref(state, ref, tree, d["meta"].get("activity"))
    assert e.value.kind == "timeout" and e.value.data["reason"] == "degraded"


def test_a_degraded_tree_rematches_only_inside_the_refs_complete_window():
    """Degraded elsewhere (the flag alone), but the ref's window was read in full:
    matching proceeds, restricted to that window."""
    d = json.loads((TREES / "real-settings-main.json").read_text())
    state = S.to_state(build(d))
    ref = _ref_labelled(state, "Display · Brightness, Blue light filter, Home screen")
    tree = dict(d["tree"], dump=d["tree"]["dump"] + 1, degraded=True, reason="no-root")
    res = R.resolve_ref(state, ref, tree, d["meta"].get("activity"))
    assert res.handle == state["refs"][ref]["handle"] and res.tier == 2
