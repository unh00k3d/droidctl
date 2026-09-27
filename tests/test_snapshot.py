"""The snapshot against REAL captured trees (tests/fixtures/trees, from the SM-N950F).

Every expectation here was checked against the raw tree by hand. Golden files
(tests/fixtures/snap) pin the exact text; these tests say *why* it looks that way.
Regenerate the goldens with: .venv/bin/python scripts/make_goldens.py
"""
import copy
import json
import pathlib

import pytest

from droidctl import cli, snapshot as S
from droidctl import spatial as sp

ROOT = pathlib.Path(__file__).resolve().parent
TREES = ROOT / "fixtures" / "trees"
SNAPS = ROOT / "fixtures" / "snap"
REAL = sorted(TREES.glob("real-*.json"))


def load(name):
    return json.loads((TREES / f"{name}.json").read_text())


def build(name, **kw):
    d = load(name)
    return S.build(d["tree"], activity=d["meta"].get("activity"), **kw)


def text(name, **opts):
    return S.render(build(name), S.Opts(**opts))


def el(snap, label):
    return next(e for e in snap.elements if e.label_full == label)


# --- properties of every real screen --------------------------------------
@pytest.mark.parametrize("path", REAL, ids=lambda p: p.stem)
def test_every_real_screen_is_small_and_sane(path):
    d = json.loads(path.read_text())
    snap = S.build(d["tree"], activity=d["meta"].get("activity"))
    out = S.render(snap, S.Opts())
    assert S.est_tokens(out) < 2000, "PLAN: under 2k tokens per screen (estimate)"
    assert S.est_tokens(out) < S.est_tokens(json.dumps(d["tree"])) / 10
    w, h = snap.screen[2], snap.screen[3]
    assert [e.ref for e in snap.elements] == list(range(1, len(snap.elements) + 1))
    for e in snap.elements:
        r = e.rect
        # inverted (clipped-away) and zero-area bounds never become elements
        assert r is not None and r[0] < r[2] and r[1] < r[3], e.label_full
        assert 0 <= r[0] and r[2] <= w and 0 <= r[1] and r[3] <= h
        assert sp.contains(r, e.tap) or r[2] - r[0] <= 1
    assert "Recents" not in out and "Battery charging" not in out, "system bars are dropped by default"


@pytest.mark.parametrize("path", REAL, ids=lambda p: p.stem)
def test_refs_do_not_depend_on_the_layout(path):
    d = json.loads(path.read_text())
    snap = S.build(d["tree"], activity=d["meta"].get("activity"))
    spatial = S.render(snap, S.Opts())
    flat = S.render(snap, S.Opts(layout="flat"))
    for e in snap.elements:
        assert f"[{e.ref}] {e.role}" in spatial
        assert f"[{e.ref}] {e.role}" in flat


@pytest.mark.parametrize("path", REAL, ids=lambda p: p.stem)
def test_signature_is_deterministic(path):
    d = json.loads(path.read_text())
    a = S.build(copy.deepcopy(d["tree"]), activity=d["meta"].get("activity"))
    b = S.build(copy.deepcopy(d["tree"]), activity=d["meta"].get("activity"))
    assert a.sig == b.sig and len(a.sig) == 4
    assert S.flat_lines(a) == S.flat_lines(b)


# --- Settings -------------------------------------------------------------
def test_settings_rows_merge_title_and_summary():
    out = text("real-settings-main")
    assert '[5] row "Connections · Wi-Fi, Bluetooth, Data usage, Flight mode"' in out
    assert "list 8/25 more↓" in out
    # rows scrolled below the navigation bar are invisible (inverted bounds)
    assert "Biometrics and security" not in out
    assert "keyboard=hidden" in out and "dialog=no" in out


def test_system_windows_come_back_with_system():
    snap = build("real-settings-main", system=True)
    labels = {e.label_full for e in snap.elements}
    assert {"Back", "Home", "Recents"} <= labels
    assert all(e.region == "system" for e in snap.elements if e.label_full in ("Back", "Home"))


def test_settings_top_bar_is_above_the_list():
    snap = build("real-settings-main")
    assert el(snap, "Search").region == "top bar"
    assert el(snap, "Settings").region == "top bar"
    assert el(snap, "Display · Brightness, Blue light filter, Home screen").region == "content"


def test_switch_rows_keep_both_controls_and_their_state():
    snap = build("real-settings-display")
    row = el(snap, "Adaptive brightness · On")
    sw = next(e for e in snap.elements if e.role == "switch" and e.label_full == "Adaptive brightness")
    assert row.role == "row" and "on" in sw.ann
    night = next(e for e in snap.elements if e.role == "switch" and e.label_full == "Night mode")
    assert "off" in night.ann
    spatial = S.render(snap, S.Opts())
    assert '[6] row "Adaptive brightness · On"   [7] switch on' in spatial   # label shown once per line
    assert '[7] switch  "Adaptive brightness"  on' in S.render(snap, S.Opts(layout="flat"))


def test_a_seekbar_is_a_control_not_a_scroll_container():
    snap = build("real-settings-display")
    sb = next(e for e in snap.elements if e.role == "seekbar")
    assert "range=4509/10000" in sb.ann
    assert sb.context == ("in", "Brightness")
    assert not any(e.role == "scroll" for e in snap.elements)


def test_single_choice_rows_adopt_their_checked_text_view():
    out = text("real-settings-screen-timeout")
    assert "[3] list 6/6" in out
    assert '[9] option "10 minutes" checked' in out
    assert out.count("checked") == 1
    assert "scroll" not in out, "a ScrollView whose content fits is plain layout"


def test_long_lists_count_and_partial_rows():
    snap = build("real-settings-apps-list")
    out = S.render(snap, S.Opts())
    assert "list 9/83 more↓" in out
    assert '[5] dropdown "All"' in out, "a Spinner is a dropdown, not a scroll container"
    assert '"Bixby Voice"' in out                         # partly visible
    assert "40.82 MB" not in out                          # its summary is below the fold
    assert '[13] row "Bixby Home · 11.69 MB"   [14] button "More settings"' in out


def test_keyboard_is_shown_occludes_and_is_not_listed():
    snap = build("real-settings-search-keyboard")
    out = S.render(snap, S.Opts())
    assert "keyboard=shown" in out
    assert "-- keyboard (y 1217-2094, covers 40%)" in out
    assert "Emojis" not in out and "Show predictive text" not in out
    assert '[2] input #search_src_text hint="Search" empty focused' in out
    for e in snap.elements:
        assert not sp.inter(e.rect, snap.keyboard), f"[{e.ref}] is under the keyboard"


def test_nested_icon_buttons_get_context_from_their_row():
    snap = build("real-settings-search-keyboard")
    rm = [e for e in snap.elements if e.res_id and e.res_id.endswith("remove_icon")]
    assert [e.context for e in rm] == [("in", "View security certificates"),
                                       ("in", "Install network certificates")]
    assert '[7] button (unlabeled, in "View security certificates") #remove_icon' in S.render(snap, S.Opts())


# --- Compose apps ---------------------------------------------------------
def test_sahibinden_compose_labels_and_regions():
    snap = build("real-sahibinden-home")
    out = S.render(snap, S.Opts())
    # desc-on-child: the clickable View's label lives on a child View
    assert '[3] button "action_0"' in out
    # unlabeled: guessed from the Compose test tag, plus the nearest text
    assert '[1] button drawer? (unlabeled, left of "sahibinden.com") #buttonDrawer' in out
    assert el(snap, "sahibinden.com").region == "top bar"
    fab = next(e for e in snap.elements if e.res_id == "floatButton")
    assert fab.region == "fab"
    # Compose nests a clickable inside the text field with the same bounds: one control
    inputs = [e for e in snap.elements if e.role == "input"]
    assert len(inputs) == 1 and inputs[0].label_full == "Kelime veya ilan numarası ile ara"
    assert "Emlak · Konut, İş Yeri, Arsa" in out                   # Unicode kept
    assert "Bebek & Çocuk" not in out                             # invisible summary
    assert "! overlap" in out                                     # the FAB sits on a row


def test_gdocs_tabs_bars_and_fab():
    snap = build("real-gdocs-home")
    out = S.render(snap, S.Opts())
    assert '[4] tab "Suggested" selected   [5] tab "Activity"' in out
    assert "-- bottom bar\n[12] tab \"Home\" selected" in out
    assert el(snap, "Toggle menu").region == "fab" and "off" in el(snap, "Toggle menu").ann
    assert "actions=[Expand, Collapse, Toggle]" in out
    assert "pager more→" in out
    # the CoordinatorLayout claims to scroll but spans the window: not a container
    assert not any(e.role in ("list", "scroll") for e in snap.elements)
    assert "user@example.com" in out                               # scrubbed at capture


def test_launcher_offscreen_icons_and_custom_actions():
    snap = build("real-launcher-home")
    out = S.render(snap, S.Opts())
    assert "My EE" not in out and "BT Sport" not in out           # next home page: inverted bounds
    assert '[8] button "Messages" actions=[Move item]   [9] button "Play Store" actions=[Move item]' in out
    assert el(snap, "Search").role == "widget" and "long-press" in el(snap, "Search").ann
    # the weather widget folds its same-size clickable child: no long-press-only claim
    assert "long-press" not in el(snap, "Weather · Tap for weather info").ann


def test_launcher_popup_is_in_the_launcher_window():
    # Samsung draws the icon popup inside the launcher's own window (no separate
    # window, no pane title), so it cannot be told apart as a "popup" region
    snap = build("real-launcher-icon-popup")
    assert not snap.dialog
    assert [e.label_full for e in snap.elements][:4] == [
        "Select items Button", "Remove from Home Button", "Disable Button", "App info"]


def test_camera_seekbar_and_empty_splash():
    assert '"Zoom" range=99/99' in text("real-opencamera-main")
    out = text("real-sahibinden-splash")
    assert "(no elements" in out


# --- options --------------------------------------------------------------
def test_find_in_max_geo_bounds_map():
    snap = build("real-settings-display")
    found = S.render(snap, S.Opts(find="NIGHT"))
    assert found.count("\n") == 2 and "Night mode" in found
    assert "(nothing matches" in S.render(snap, S.Opts(find="zzz"))
    within = S.render(snap, S.Opts(within=3, layout="flat"))
    assert "[3] list" in within and "Navigate up" not in within
    capped = S.render(snap, S.Opts(max=4))
    assert "… 12 more elements" in capped
    assert "@0,3 14x7" in S.render(snap, S.Opts(geo=True))
    assert "[0,63,147,210]" in S.render(snap, S.Opts(bounds=True))
    assert S.render(snap, S.Opts(map=True)).count("\n") > 25


def test_flat_has_no_regions_and_one_element_per_line():
    out = text("real-settings-display", layout="flat")
    assert "-- " not in out
    assert all(line.lstrip().startswith("[") for line in out.splitlines()[1:])


def test_no_rows_and_no_infer():
    snap = build("real-sahibinden-home")
    no_rows = S.render(snap, S.Opts(rows=False))
    assert all(line.count("] ") <= 1 for line in no_rows.splitlines() if line.lstrip().startswith("["))
    assert "drawer?" not in S.render(snap, S.Opts(infer=False))


# --- state, diff, where ---------------------------------------------------
def test_saved_state_carries_what_the_resolver_needs():
    snap = build("real-settings-display")
    st = S.to_state(snap, "SERIAL")
    assert st["sig"] == snap.sig and st["lines"] == S.flat_lines(snap)
    r = st["refs"]["7"]
    assert r["role"] == "switch" and r["handle"] and r["dump"] == snap.dump
    assert r["id"] == "android:id/switch_widget" and r["class"] == "android.widget.Switch"
    assert r["path"][0] is None or isinstance(r["path"][0], str)
    assert r["bounds"] and r["tap"] and r["parent"] == 3
    json.dumps(st)


def test_diff_reports_a_toggled_switch_and_nothing_else():
    d = load("real-settings-display")
    before = S.build(d["tree"], activity=d["meta"]["activity"])
    tree = copy.deepcopy(d["tree"])

    def find(n, handle):
        if n.get("handle") == handle:
            return n
        for c in n.get("children", ()):
            hit = find(c, handle)
            if hit:
                return hit
    for w in tree["windows"]:
        node = find(w.get("root", {}), 36) if w.get("root") else None   # the Night mode switch
        if node:
            node["flags"] = node["flags"] + ["checked"]
    after = S.build(tree, activity=d["meta"]["activity"])
    assert after.sig == before.sig
    changes = S.diff(S.to_state(before), after)
    assert len(changes) == 1 and changes[0].startswith('~ [11] switch  "Night mode"  on')
    assert S.diff(S.to_state(before), before) == []


def test_where_reports_box_region_and_neighbours():
    snap = build("real-settings-display")
    info = S.where(S.to_state(snap), 7)                   # the Adaptive brightness switch
    assert info["region"] == "content" and info["parent"] == 3
    assert info["inside"] == [6, 3]                       # its row, then its list
    assert "left" not in info["neighbours"]               # the row is around it, not beside it
    assert info["neighbours"]["above"]["ref"] == 4        # the Brightness row (its seekbar is inside it)
    assert info["neighbours"]["below"]["ref"] == 9
    left = S.where(S.to_state(snap), 2)["neighbours"]["left"]
    assert left["ref"] == 1 and left["label"] == "Navigate up"
    assert S.where(S.to_state(snap), 999) is None


# --- CLI ------------------------------------------------------------------
def test_cli_snapshot_from_a_fixture(capsys):
    cli.main(["snapshot", "--fixture", str(TREES / "real-settings-main.json"), "--json"])
    p = json.loads(capsys.readouterr().out)
    assert p["ok"] and p["screen"]["pkg"] == "com.android.settings" and p["total"] == 10
    e = p["elements"][4]
    assert e["ref"] == 5 and e["role"] == "row" and e["parent"] == 4 and e["handle"]


def test_cli_live_path_saves_state_diffs_and_where(monkeypatch, tmp_path, capsys):
    """The live path with the device swapped for a captured tree."""
    monkeypatch.setenv("DROIDCTL_HOME", str(tmp_path))
    trees = [load("real-settings-display")["tree"]]

    class FakeClient:
        def call(self, method, params=None, timeout=None):
            assert method == "tree"
            return copy.deepcopy(trees[-1])

        def close(self):
            pass
    monkeypatch.setattr(cli.dev, "resolve_serial", lambda s: "SERIAL")
    monkeypatch.setattr(cli.dev, "connect", lambda serial: (FakeClient(), {}))
    cli.main(["snapshot"])
    first = capsys.readouterr().out
    assert "[7] switch on" in first
    assert (tmp_path / "snaps" / "SERIAL.json").exists()
    cli.main(["snapshot"])
    assert "unchanged (16 elements" in capsys.readouterr().out
    cli.main(["snapshot", "--diff"])
    assert capsys.readouterr().out.strip().endswith("unchanged")
    cli.main(["where", "7", "--json"])
    w = json.loads(capsys.readouterr().out)
    assert w["inside"] == [6, 3] and w["neighbours"]["below"]["ref"] == 9
    trees.append(load("real-settings-main")["tree"])
    cli.main(["snapshot", "--diff"])
    assert "(new screen: sig was" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["where", "99", "--json"])
    assert json.loads(capsys.readouterr().out)["error"]["kind"] == "not-found"


# --- goldens --------------------------------------------------------------
GOLDEN = sorted(SNAPS.glob("*.snap.txt"))


def test_every_real_fixture_has_goldens():
    names = {p.name for p in SNAPS.glob("*.txt")}
    for f in REAL:
        assert f"{f.stem}.snap.txt" in names and f"{f.stem}.flat.txt" in names, \
            "run: .venv/bin/python scripts/make_goldens.py"


@pytest.mark.parametrize("golden", sorted(SNAPS.glob("*.txt")), ids=lambda p: p.name)
def test_golden(golden):
    stem, kind = golden.name.rsplit(".", 2)[0], golden.name.split(".")[-2]
    got = text(stem, layout="flat" if kind == "flat" else "spatial")
    assert got + "\n" == golden.read_text(), "run: .venv/bin/python scripts/make_goldens.py (and review the diff)"
