"""The toast in the snapshot header (PLAN: `toast="Message archived"`)."""
import json
import pathlib

from droidctl import snapshot as S

TREES = pathlib.Path(__file__).parent / "fixtures" / "trees"


def ev(seq, t, typ="toast", text="Saved!"):
    return {"seq": seq, "t": t, "type": typ, "text": text}


def test_pick_toast_since_the_previous_position():
    events = [ev(3, 100, text="old"), ev(5, 900, typ="window_content", text=None), ev(7, 1000, text="new")]
    assert S.pick_toast(events, prev_seq=3) == "new"
    assert S.pick_toast(events, prev_seq=7) is None


def test_pick_toast_without_a_position_takes_only_fresh_ones():
    events = [ev(1, 1000, text="stale"), ev(2, 9000, text="fresh")]
    assert S.pick_toast(events, None, now_ms=10000) == "fresh"
    assert S.pick_toast([ev(1, 1000)], None, now_ms=10000) is None
    assert S.pick_toast([ev(1, 1000)], None, now_ms=None) is None


def test_header_carries_the_toast_and_state_records_the_position():
    tree = json.loads((TREES / "real-settings-main.json").read_text())["tree"]
    snap = S.build(tree)
    assert "toast=" not in S.header(snap)
    snap.toast, snap.evseq = 'Message "archived"', 42
    assert 'toast="Message \\"archived\\""' in S.header(snap)
    assert S.to_state(snap)["evseq"] == 42


def test_pick_toast_uses_the_daemon_ring_age_when_present():
    events = [dict(ev(1, 0, text="old"), age_ms=9000), dict(ev(2, 0, text="now"), age_ms=300)]
    assert S.pick_toast(events, None, now_ms=None) == "now"
    assert S.pick_toast([dict(ev(1, 0), age_ms=9000)], None) is None
