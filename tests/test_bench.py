"""The benchmark harnesses' own logic: grading, the variant shim, result parsing,
the decision rule, and a dry run of the A/B with the scripted fake agent."""
import json
import os
import subprocess
import sys

import pytest

BENCH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bench")
sys.path.insert(0, BENCH)

import spatial  # noqa: E402
import tap_accuracy as tap  # noqa: E402
import testapp  # noqa: E402

TASKS = {t.name: t for t in spatial.TASKS}


def ev(**kw):
    return dict(kw)


# --- DTA parsing ------------------------------------------------------------
def test_parse_dta_keeps_one_scenario_and_skips_noise():
    log = "\n".join(['{"s":"cart","ev":"shown"}', "garbage", '{"s":"other","ev":"click"}',
                     '{"s":"cart","ev":"inc","id":"USB-C Cable","n":1}'])
    got = testapp.parse_dta(log, "cart")
    assert [e["ev"] for e in got] == ["shown", "inc"]
    assert [e["ev"] for e in testapp.interactions(got)] == ["inc"]


# --- task grading -----------------------------------------------------------
def test_cart_task_needs_exactly_the_right_increment():
    t = TASKS["cart_inc_usb"]
    assert spatial.grade(t, [ev(ev="inc", id="USB-C Cable")])["success"]
    twice = spatial.grade(t, [ev(ev="inc", id="USB-C Cable"), ev(ev="inc", id="USB-C Cable")])
    assert not twice["success"] and twice["wrong_taps"] == 0            # over-tapping is not a wrong target
    wrong = spatial.grade(t, [ev(ev="inc", id="Wireless Mouse"), ev(ev="inc", id="USB-C Cable")])
    assert not wrong["success"] and wrong["wrong_taps"] == 1


@pytest.mark.parametrize("name,good,bad", [
    ("calendar_14", [ev(ev="click", id="day14")], [ev(ev="click", id="day13")]),
    ("photo_r2c1", [ev(ev="click", id="photo4")], [ev(ev="click", id="photo1")]),
    ("share_doc", [ev(ev="click", id="ic_share")], [ev(ev="click", id="ic_delete")]),
    ("buy_premium", [ev(ev="buy", plan="Premium")], [ev(ev="buy", plan="Basic")]),
    ("delete_item7", [ev(ev="delete", row=7)], [ev(ev="delete", row=6)]),
    ("star_grace", [ev(ev="click", id="star2")], [ev(ev="click", id="row2")]),
    ("drawer_groups", [ev(ev="click", id="hamburger"), ev(ev="nav", item="Groups")],
     [ev(ev="click", id="hamburger"), ev(ev="nav", item="All contacts")]),
])
def test_tasks_grade_the_target_and_count_wrong_taps(name, good, bad):
    t = TASKS[name]
    assert spatial.grade(t, good) == {"success": True, "wrong_taps": 0, "events": len(good)}
    g = spatial.grade(t, bad)
    assert not g["success"] and g["wrong_taps"] >= 1


def test_pin_and_email_tasks():
    pin = TASKS["keypad_pin"]
    keys = [ev(ev="key", digit=d) for d in "2580"]
    assert spatial.grade(pin, keys + [ev(ev="pin", value="2580")])["success"]
    assert spatial.grade(pin, keys + [ev(ev="key", digit="1"), ev(ev="pin", value="25801")])["wrong_taps"] == 1
    mail = TASKS["email_field"]
    ok = [ev(ev="text", id="f_email", value="ada@example.com"), ev(ev="click", id="save")]
    assert spatial.grade(mail, ok)["success"]
    assert not spatial.grade(mail, ok[:1])["success"]                      # never saved
    stray = spatial.grade(mail, [ev(ev="text", id="f_phone", value="ada@example.com")] + ok)
    assert not stray["success"] and stray["wrong_taps"] == 1


# --- the shim pins the variant ---------------------------------------------
def _shim(tmp_path, variant, *argv):
    bindir = tmp_path / "bin"
    spatial.write_shim(str(bindir))
    calls = tmp_path / "calls.jsonl"
    env = dict(os.environ, BENCH_CALLS=str(calls), BENCH_VARIANT=variant, BENCH_REAL_DROIDCTL="DRY")
    r = subprocess.run([str(bindir / "droidctl"), *argv], env=env, capture_output=True, text=True)
    return r, [json.loads(line) for line in calls.read_text().splitlines()]


def test_shim_forces_the_layout_and_logs_every_call(tmp_path):
    r, calls = _shim(tmp_path, "flat", "snapshot", "--layout", "spatial", "--no-rows")
    out = json.loads(r.stdout)
    assert out["layout"] == "flat" and out["argv"] == ["snapshot"]
    assert calls[0]["cmd"] == "snapshot" and calls[0]["blocked"] is None


@pytest.mark.parametrize("variant,argv,blocked", [
    ("spatial", ["shot", "--marks"], True), ("flat", ["shot"], True), ("marks", ["shot", "--marks"], False),
    ("marks", ["snapshot", "--raw"], True), ("spatial", ["snapshot", "--map"], True),
])
def test_shim_refuses_what_the_variant_does_not_allow(tmp_path, variant, argv, blocked):
    r, calls = _shim(tmp_path, variant, *argv)
    assert (r.returncode == 2) == blocked
    assert bool(calls[0]["blocked"]) == blocked


# --- agent result parsing and the decision rule ----------------------------
def test_parse_result_sums_input_tokens_including_cache():
    out = json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 4,
                      "total_cost_usd": 0.05, "result": "DONE",
                      "usage": {"input_tokens": 10, "cache_creation_input_tokens": 200,
                                "cache_read_input_tokens": 3000, "output_tokens": 80}})
    r = spatial.parse_result("log line\n" + out)
    assert r["agent_ok"] and r["tokens_in"] == 3210 and r["tokens_out"] == 80 and r["turns"] == 4
    assert spatial.parse_result("")["tokens_in"] == 0 and not spatial.parse_result("")["agent_ok"]


def _rec(variant, success, steps, tokens, wrong=0):
    return {"variant": variant, "success": success, "steps": steps, "tokens_in": tokens, "tokens_out": 1,
            "wall_s": 1.0, "wrong_taps": wrong, "cost_usd": 0.0}


def test_decision_rule_follows_plan():
    base = [_rec("flat", True, 5, 1000), _rec("flat", False, 9, 1000)]
    win = [_rec("spatial", True, 4, 1100), _rec("spatial", True, 5, 1100)]           # +1 success, +10% tokens
    agg = spatial.aggregate(base + win)
    assert spatial.decide(agg, "flat", "spatial")["verdict"] == "better: earns default"
    costly = spatial.aggregate(base + [_rec("spatial", True, 4, 1300), _rec("spatial", True, 5, 1300)])
    assert spatial.decide(costly, "flat", "spatial")["verdict"] == "better but too many tokens"
    worse = spatial.aggregate(base + [_rec("spatial", False, 5, 900), _rec("spatial", False, 9, 900)])
    assert spatial.decide(worse, "flat", "spatial")["verdict"] == "worse"
    tie = spatial.aggregate(base + [_rec("spatial", True, 5, 1000), _rec("spatial", False, 9, 1000)])
    assert spatial.decide(tie, "flat", "spatial")["verdict"] == "no measurable difference"
    assert spatial.decide(agg, "spatial", "marks")["verdict"] == "not run"


def test_agent_command_restricts_tools():
    cmd = spatial.agent_cmd("spatial", "claude-sonnet-5", 1.0, "sys")
    assert cmd[cmd.index("--allowedTools") + 1] == "Bash(droidctl:*)"
    assert cmd[cmd.index("--tools") + 1] == "Bash" and "dontAsk" in cmd
    assert spatial.agent_cmd("marks", "m", 1.0, "s")[cmd.index("--tools") + 1] == "Bash,Read"
    assert "shot --marks" in spatial.task_prompt(spatial.TASKS[0], "marks")
    assert "shot" not in spatial.task_prompt(spatial.TASKS[0], "flat")


def test_dry_run_exercises_the_whole_harness(tmp_path):
    out = tmp_path / "ab.json"
    res = spatial.main(["--dry-run", "--out", str(out)])
    assert res["summary"]["flat"]["n"] == len(spatial.TASKS)
    assert all(r["success"] for r in res["records"]), [r for r in res["records"] if not r["success"]]
    assert all(r["steps"] >= 2 and r["blocked_calls"] == 0 for r in res["records"])
    assert json.loads(out.read_text())["dry_run"] is True


# --- tap accuracy: grading and the baseline's element choice ---------------
def test_tap_grading_requires_the_one_intended_event():
    exp = {"ev": "delete", "row": 7}
    assert tap.grade_events([ev(ev="delete", row=7)], exp) == (True, "ok")
    assert tap.grade_events([], exp) == (False, "nothing happened")
    assert not tap.grade_events([ev(ev="delete", row=6)], exp)[0]
    assert tap.grade_events([ev(ev="delete", row=7)] * 2, exp) == (False, "fired 2 times")


def test_baseline_picks_the_repeated_label_nearest_its_anchor():
    def el(text, x, y, label=""):
        return {"type": "x", "text": text, "label": label, "rect": {"x": x, "y": y, "width": 100, "height": 50}}
    els = [el("Item 6", 0, 100), el("Delete", 800, 100), el("Item 7", 0, 200), el("Delete", 800, 200)]
    chosen, how = tap.pick_element(els, {"text": "Delete", "anchor": "Item 7"})
    assert chosen["rect"]["y"] == 200 and how == "nearest to anchor"
    assert tap.pick_element(els, {"text": "Nope"})[0] is None
    assert tap.parse_bounds("[0,10][100,60]") == {"x": 0, "y": 10, "width": 100, "height": 50}


def test_every_tap_target_is_well_formed():
    ts = tap.build_targets()
    assert len(ts) + len(tap.REAL) >= 50
    assert len({t["name"] for t in ts}) == len(ts)
    for t in ts:
        assert t["dc"][0] == "tap" and t["expect"] and t["pick"]


def test_ref_picker_uses_merged_row_parts_as_anchors():
    els = [{"ref": 2, "label": "Ada Lovelace · Lunch", "bounds": [0, 100, 800, 200]},
           {"ref": 3, "label": "Star", "bounds": [900, 100, 1000, 200]},
           {"ref": 4, "label": "Alan Turing · Paper", "bounds": [0, 300, 800, 400]},
           {"ref": 5, "label": "Star", "bounds": [900, 300, 1000, 400]}]
    assert tap.pick_ref(els, {"text": "Star", "anchor": "Alan Turing"})[0]["ref"] == 5
    assert tap.pick_ref(els, {"text": "Nope"})[0] is None
    assert tap.pick_ref(els, {"text": "Alan Turing"})[0]["ref"] == 4          # a part of a merged row
