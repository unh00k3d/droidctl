"""Spatial-layer A/B: real LLM agents doing test-app tasks under layout variants.

    .venv/bin/python bench/spatial.py -d SERIAL [--variants flat,spatial,...]
        [--tasks all|name,...] [--runs 1] [--model claude-sonnet-5] [--budget-usd 1.0]
        [--out bench/results/spatial-ab.json]
    .venv/bin/python bench/spatial.py --dry-run      # scripted fake agent + fake phone

PLAN.md "Spatial layer → Evaluation protocol". Variants:

  flat        DROIDCTL_LAYOUT=flat        (the baseline)
  spatial     DROIDCTL_LAYOUT=spatial     (regions, rows, grids, inferred labels)
  no-regions, no-rows, no-grids, no-infer
              spatial minus one layer (ablation: does that layer earn its place?)
  geo, map    spatial plus --geo / --map on every printed screen
  marks       spatial + `shot --marks` allowed (the agent may look at pixels)

The variant holds for every screen droidctl prints (snapshot, action results,
wait), not just `snapshot`: the shim sets DROIDCTL_LAYOUT, which all of them read.

Each run starts the scenario fresh, then runs `claude -p` headless with only
Bash(droidctl:*) (and Read for `marks`, to view the screenshot). A shim
`droidctl` first on PATH logs every call and pins the variant: it forces
DROIDCTL_LAYOUT, strips --layout/--no-* flags (and --geo/--map where the variant
has them already), and refuses --raw, --geo/--map outside their variant, and
`shot` outside the marks variant. Success and wrong-target taps come only
from the test app's DTA logcat events, never from what the agent says.
Token counts come from the CLI's JSON result (input includes cache reads).
"""
import argparse
import datetime
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import testapp as T  # noqa: E402

VARIANTS = ("flat", "spatial", "no-regions", "no-rows", "no-grids", "no-infer", "geo", "map", "marks")
LAYOUT_ENV = {"flat": "flat", "spatial": "spatial", "marks": "spatial",   # → DROIDCTL_LAYOUT
              **{v: "spatial," + v for v in ("no-regions", "no-rows", "no-grids", "no-infer", "geo", "map")}}
DEFAULT_MODEL = "claude-sonnet-5"
CLAUDE = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")


# --------------------------------------------------------------------------
# tasks: a scenario, an instruction, and a DTA-based grader
# --------------------------------------------------------------------------
def _count(events, ev, id=None, **fields):
    return sum(1 for e in events if e.get("ev") == ev and (id is None or e.get("id") == id)
               and all(e.get(k) == v for k, v in fields.items()))


def _others(events, evs, ok):
    """Interaction events of the given kinds that are not the intended target."""
    return sum(1 for e in events if e.get("ev") in evs and not ok(e))


class Task:
    def __init__(self, name, scenario, prompt, success, wrong, script=None):
        self.name, self.scenario, self.prompt = name, scenario, prompt
        self.success, self.wrong = success, wrong
        self.script = script or []          # dry-run: (droidctl argv, DTA events it "causes")


def _t_cart(ev):
    return _count(ev, "inc", "USB-C Cable") == 1 and _others(ev, {"inc", "dec", "checkout"},
                                                             lambda e: e.get("ev") == "inc" and e.get("id") == "USB-C Cable") == 0


def _t_email(ev):
    texts = [e for e in ev if e.get("ev") == "text" and e.get("id") == "f_email"]
    return bool(texts) and texts[-1].get("value") == "ada@example.com" and _count(ev, "click", "save") >= 1 \
        and not any(e.get("ev") == "text" and e.get("id") != "f_email" and e.get("value") for e in ev)


def _t_pin(ev):
    pins = [e for e in ev if e.get("ev") == "pin"]
    return bool(pins) and pins[-1].get("value") == "2580"


TASKS = [
    Task("cart_inc_usb", "cart", 'In the shopping cart, increase the quantity of "USB-C Cable" by exactly one.',
         _t_cart,
         lambda ev: _others(ev, {"inc", "dec", "checkout"}, lambda e: e.get("id") == "USB-C Cable"),
         [(["snapshot"], []), (["tap", "--text", "+", "--right-of", "USB-C Cable"],
                               [{"ev": "inc", "id": "USB-C Cable", "qty": 3}])]),
    Task("calendar_14", "calendar", "In the calendar, select October 14.",
         lambda ev: _count(ev, "click", "day14") == 1 and _others(ev, {"click"}, lambda e: e.get("id") == "day14") == 0,
         lambda ev: _others(ev, {"click"}, lambda e: e.get("id") == "day14"),
         [(["snapshot"], []), (["tap", "--text", "14"], [{"ev": "click", "id": "day14", "day": 14}])]),
    Task("keypad_pin", "keypad", "Enter the PIN 2580 on the keypad and confirm it with OK.",
         _t_pin,
         lambda ev: sum(1 for e in ev if e.get("ev") == "key" and e.get("digit") not in ("2", "5", "8", "0")),
         [(["snapshot"], [])] + [(["tap", "--text", d], [{"ev": "key", "digit": d}]) for d in "2580"]
         + [(["tap", "--text", "OK"], [{"ev": "pin", "value": "2580"}])]),
    Task("photo_r2c1", "photo_grid", "Open the photo in the second row, first column. It has no label.",
         lambda ev: _count(ev, "click", "photo4") == 1 and _others(ev, {"click"}, lambda e: e.get("id") == "photo4") == 0,
         lambda ev: _others(ev, {"click"}, lambda e: e.get("id") == "photo4"),
         [(["snapshot"], []), (["tap", "4"], [{"ev": "click", "id": "photo4", "row": 2, "col": 1}])]),
    Task("share_doc", "unlabeled_icons", "Share the document.",
         lambda ev: _count(ev, "click", "ic_share") == 1 and _others(ev, {"click"}, lambda e: e.get("id") == "ic_share") == 0,
         lambda ev: _others(ev, {"click"}, lambda e: e.get("id") == "ic_share"),
         [(["snapshot"], []), (["tap", "--id", "ic_share"], [{"ev": "click", "id": "ic_share"}])]),
    Task("email_field", "label_left_form", "Type ada@example.com into the Email field, then tap Save.",
         _t_email,
         lambda ev: sum(1 for e in ev if e.get("ev") == "text" and e.get("id") != "f_email" and e.get("value")),
         [(["snapshot"], []), (["type", "2", "ada@example.com"], [{"ev": "text", "id": "f_email", "value": "ada@example.com"}]),
          (["tap", "--text", "Save"], [{"ev": "click", "id": "save"}])]),
    Task("buy_premium", "cards", "Buy the Premium plan.",
         lambda ev: _count(ev, "buy", plan="Premium") == 1 and _others(ev, {"buy"}, lambda e: e.get("plan") == "Premium") == 0,
         lambda ev: _others(ev, {"buy"}, lambda e: e.get("plan") == "Premium"),
         [(["snapshot"], []), (["tap", "--text", "Buy", "--below", "Premium"], [{"ev": "buy", "plan": "Premium"}])]),
    Task("delete_item7", "duplicates", "Delete Item 7 (only that one).",
         lambda ev: _count(ev, "delete", row=7) == 1 and _others(ev, {"delete"}, lambda e: e.get("row") == 7) == 0,
         lambda ev: _others(ev, {"delete"}, lambda e: e.get("row") == 7),
         [(["snapshot"], []), (["tap", "--text", "Delete", "--right-of", "Item 7"], [{"ev": "delete", "id": "delete", "row": 7}])]),
    Task("star_grace", "row_nested", "Star the message from Grace Hopper without opening it.",
         lambda ev: _count(ev, "click", "star2") == 1 and _others(ev, {"click"}, lambda e: e.get("id") == "star2") == 0,
         lambda ev: _others(ev, {"click"}, lambda e: e.get("id") == "star2"),
         [(["snapshot"], []), (["tap", "--desc", "Star", "--right-of", "Grace Hopper"], [{"ev": "click", "id": "star2"}])]),
    Task("drawer_groups", "fab_sheet_drawer", "Open the navigation drawer and select Groups.",
         lambda ev: any(e.get("ev") == "nav" and e.get("item") == "Groups" for e in ev)
         and _others(ev, {"nav", "click"}, lambda e: e.get("item") == "Groups" or e.get("id") == "hamburger") == 0,
         lambda ev: _others(ev, {"nav", "click"}, lambda e: e.get("item") == "Groups" or e.get("id") == "hamburger"),
         [(["snapshot"], []), (["tap", "--desc", "Open navigation drawer"], [{"ev": "click", "id": "hamburger"}]),
          (["tap", "--text", "Groups"], [{"ev": "nav", "id": "drawer", "item": "Groups"}])]),
]


def grade(task, events):
    ev = T.interactions(events)
    return {"success": bool(task.success(ev)), "wrong_taps": int(task.wrong(ev)), "events": len(ev)}


# --------------------------------------------------------------------------
# the droidctl shim and the agent command
# --------------------------------------------------------------------------
SHIM = r'''#!/usr/bin/env python3
"""bench shim: log every droidctl call and pin the layout variant."""
import json, os, sys, time
LOG, VARIANT, REAL = os.environ["BENCH_CALLS"], os.environ["BENCH_VARIANT"], os.environ["BENCH_REAL_DROIDCTL"]
LAYOUT = os.environ["BENCH_LAYOUT"]
argv = sys.argv[1:]
cmd = next((a for a in argv if not a.startswith("-")), "")
blocked = None
given = [f for f in ("geo", "map") if f in LAYOUT.split(",")]
if "--raw" in argv or any(a in ("--geo", "--map") and a[2:] not in given for a in argv):
    blocked = "--raw" + "".join(", --" + f for f in ("geo", "map") if f not in given) + " not available in this run"
elif cmd == "shot" and VARIANT != "marks":
    blocked = "shot is not available in this run"
clean, skip = [], False
for a in argv:
    if skip:
        skip = False
        continue
    if a == "--layout":
        skip = True
        continue
    if a.startswith("--layout=") or a in ("--no-regions", "--no-rows", "--no-grids", "--no-infer", "--geo", "--map"):
        continue
    clean.append(a)
with open(LOG, "a") as f:
    f.write(json.dumps({"t": time.time(), "argv": argv, "cmd": cmd, "blocked": blocked}) + "\n")
if blocked:
    print("droidctl: " + blocked, file=sys.stderr)
    sys.exit(2)
env = dict(os.environ, DROIDCTL_LAYOUT=LAYOUT)
if REAL == "DRY":
    print(json.dumps({"ok": True, "dry": True, "argv": clean, "layout": env["DROIDCTL_LAYOUT"]}))
    sys.exit(0)
os.execve(REAL, [REAL] + clean, env)
'''

SYSTEM = """You are operating a real Android phone through the `droidctl` command-line tool, run with Bash.
Use only droidctl commands. The droidctl skill below is its complete documentation.
Refs come from `droidctl snapshot`; act on elements with refs or locators, never guess coordinates.
When the task is done, reply with the single word DONE. If it cannot be done, reply FAILED: <reason>.

"""

MARKS_HINT = ("\nYou may also look at the screen: `droidctl shot --marks --out shot.jpg`, then Read shot.jpg. "
              "The numbered boxes are the snapshot's refs.")


def write_shim(bindir):
    os.makedirs(bindir, exist_ok=True)
    path = os.path.join(bindir, "droidctl")
    with open(path, "w") as f:
        f.write(SHIM.replace("#!/usr/bin/env python3", "#!" + sys.executable, 1))
    os.chmod(path, 0o755)
    return path


def agent_cmd(variant, model, budget_usd, system):
    tools = "Bash,Read" if variant == "marks" else "Bash"
    allowed = "Bash(droidctl:*),Read" if variant == "marks" else "Bash(droidctl:*)"
    return [CLAUDE, "-p", "--model", model, "--output-format", "json",
            "--tools", tools, "--allowedTools", allowed, "--permission-mode", "dontAsk",
            "--no-session-persistence", "--setting-sources", "project",
            "--max-budget-usd", str(budget_usd), "--append-system-prompt", system]


def task_prompt(task, variant):
    return f"Task: {task.prompt}\nThe app is already open on the phone." + (MARKS_HINT if variant == "marks" else "")


def parse_result(stdout):
    """The CLI's --output-format json result → the numbers we keep."""
    try:
        r = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else {}
    except (ValueError, IndexError):
        r = {}
    u = r.get("usage") or {}
    tin = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    return {"agent_ok": r.get("subtype") == "success" and not r.get("is_error"),
            "subtype": r.get("subtype"), "turns": r.get("num_turns"), "cost_usd": r.get("total_cost_usd"),
            "tokens_in": tin, "tokens_out": int(u.get("output_tokens") or 0),
            "answer": (r.get("result") or "")[:300]}


def read_calls(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


# --------------------------------------------------------------------------
# running
# --------------------------------------------------------------------------
class Phone:
    def __init__(self, serial):
        self.serial = serial

    def start(self, scenario):
        T.launch(self.serial, scenario)

    def events(self, scenario):
        return T.dta(self.serial, scenario)


class FakePhone:
    """Dry run: the fake agent appends the DTA events its commands 'cause'."""

    def __init__(self):
        self.log = []

    def start(self, scenario):
        self.log = [{"s": scenario, "ev": "shown"}]

    def events(self, scenario):
        return [dict(e, s=scenario) for e in self.log]


def fake_agent(task, env, phone):
    """Scripted stand-in for `claude -p`: runs the task's script through the shim."""
    t0 = time.time()
    for argv, caused in task.script:
        r = subprocess.run([os.path.join(env["BENCH_BIN"], "droidctl"), *argv], env=env,
                           capture_output=True, text=True)
        if r.returncode == 0:
            phone.log += caused
    usage = {"input_tokens": 1000, "cache_read_input_tokens": 500 * len(task.script), "output_tokens": 50}
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": len(task.script) + 1,
                       "total_cost_usd": 0.0, "usage": usage, "result": "DONE",
                       "duration_ms": int((time.time() - t0) * 1000)})


def run_one(task, variant, phone, *, model, budget_usd, system, dry, serial, home, timeout):
    work = tempfile.mkdtemp(prefix=f"dcbench-{task.name}-{variant}-")
    bindir = os.path.join(work, "bin")
    write_shim(bindir)
    calls = os.path.join(work, "calls.jsonl")
    env = dict(os.environ, BENCH_CALLS=calls, BENCH_VARIANT=variant, BENCH_BIN=bindir, BENCH_LAYOUT=LAYOUT_ENV[variant],
               BENCH_REAL_DROIDCTL="DRY" if dry else T.VENV_DROIDCTL,
               PATH=bindir + os.pathsep + os.environ.get("PATH", ""), DROIDCTL_HOME=home)
    env.pop("DROIDCTL_LAYOUT", None)
    if serial:
        env["ANDROID_SERIAL"] = serial
    phone.start(task.scenario)
    t0 = time.monotonic()
    if dry:
        out, err, rc = fake_agent(task, env, phone), "", 0
    else:
        try:
            p = subprocess.run(agent_cmd(variant, model, budget_usd, system), input=task_prompt(task, variant),
                               cwd=work, env=env, capture_output=True, text=True, timeout=timeout)
            out, err, rc = p.stdout, p.stderr, p.returncode
        except subprocess.TimeoutExpired as e:
            out, err, rc = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or ""), "timeout", -1
    wall = time.monotonic() - t0
    time.sleep(0 if dry else 1.0)                       # late DTA lines from the last action
    g = grade(task, phone.events(task.scenario))
    cl = read_calls(calls)
    rec = {"task": task.name, "scenario": task.scenario, "variant": variant, **g, **parse_result(out),
           "steps": len(cl), "blocked_calls": sum(1 for c in cl if c.get("blocked")),
           "commands": [" ".join(c["argv"])[:160] for c in cl], "wall_s": round(wall, 1), "exit": rc}
    if rc != 0 and not dry:
        rec["stderr"] = (err or "")[-500:]
    shutil.rmtree(work, ignore_errors=True)
    return rec


def _median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def aggregate(records):
    out = {}
    for v in VARIANTS:
        rs = [r for r in records if r["variant"] == v]
        if not rs:
            continue
        out[v] = {"n": len(rs), "success": sum(r["success"] for r in rs),
                  "success_rate": round(sum(r["success"] for r in rs) / len(rs), 3),
                  "steps_median": _median([r["steps"] for r in rs]),
                  "steps_total": sum(r["steps"] for r in rs),
                  "tokens_in_median": _median([r["tokens_in"] for r in rs]),
                  "tokens_in_total": sum(r["tokens_in"] for r in rs),
                  "tokens_out_total": sum(r["tokens_out"] for r in rs),
                  "wall_median_s": _median([r["wall_s"] for r in rs]),
                  "wrong_taps": sum(r["wrong_taps"] for r in rs),
                  "cost_usd": round(sum(r["cost_usd"] or 0 for r in rs), 3)}
    return out


def decide(agg, base, cand, token_budget=0.15):
    """PLAN's rule: `cand` earns default status over `base` only if it raises success
    or cuts steps, without costing more than ~15% extra input tokens."""
    if base not in agg or cand not in agg:
        return {"compare": f"{cand} vs {base}", "verdict": "not run"}
    b, c = agg[base], agg[cand]
    better = c["success_rate"] > b["success_rate"] or (
        c["success_rate"] == b["success_rate"] and c["steps_total"] < b["steps_total"])
    worse = c["success_rate"] < b["success_rate"]
    tok = (c["tokens_in_total"] / b["tokens_in_total"] - 1) if b["tokens_in_total"] else 0.0
    verdict = ("better: earns default" if better and tok <= token_budget else
               "better but too many tokens" if better else
               "worse" if worse else "no measurable difference")
    return {"compare": f"{cand} vs {base}", "success": [b["success_rate"], c["success_rate"]],
            "steps_total": [b["steps_total"], c["steps_total"]], "tokens_in_change_pct": round(100 * tok, 1),
            "wrong_taps": [b["wrong_taps"], c["wrong_taps"]], "verdict": verdict}


def decisions(agg):
    """flat → spatial; each layer: spatial vs spatial-without-it (the layer earns
    its place only if spatial beats the ablation); opt-ins: spatial → +geo/+map/+marks."""
    return ([decide(agg, "flat", "spatial")]
            + [decide(agg, v, "spatial") for v in ("no-regions", "no-rows", "no-grids", "no-infer")]
            + [decide(agg, "spatial", v) for v in ("geo", "map", "marks")])


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("-d", "--device", default=os.environ.get("ANDROID_SERIAL"))
    p.add_argument("--variants", default=",".join(VARIANTS))
    p.add_argument("--tasks", default="all")
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--budget-usd", type=float, default=1.0, help="per-run spend cap passed to claude")
    p.add_argument("--timeout", type=int, default=480, help="per-run wall clock cap (s)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--out", default=None)
    a = p.parse_args(argv)
    variants = [v for v in a.variants.split(",") if v]
    tasks = TASKS if a.tasks == "all" else [t for t in TASKS if t.name in a.tasks.split(",")]
    if not a.dry_run and not a.device:
        p.error("-d SERIAL is required (or --dry-run)")
    phone = FakePhone() if a.dry_run else Phone(a.device)
    system = SYSTEM + (subprocess.run([T.VENV_DROIDCTL, "skill", "print"], capture_output=True, text=True,
                                      env=dict(os.environ, DROIDCTL_NO_DAEMON="1")).stdout if not a.dry_run else "(skill)")
    home = tempfile.mkdtemp(prefix="dcbench-home-")
    records = []
    try:
        for run in range(a.runs):
            for t in tasks:                       # interleave variants per task so drift hits all equally
                for v in variants:
                    r = run_one(t, v, phone, model=a.model, budget_usd=a.budget_usd, system=system,
                                dry=a.dry_run, serial=a.device, home=home, timeout=a.timeout)
                    r["run"] = run
                    records.append(r)
                    print(f"{t.name:14} {v:8} success={r['success']!s:5} steps={r['steps']:2} wrong={r['wrong_taps']} "
                          f"tokens_in={r['tokens_in']} wall={r['wall_s']}s", file=sys.stderr, flush=True)
    finally:
        if not a.dry_run:
            subprocess.run([T.VENV_DROIDCTL, "daemon", "stop"], env=dict(os.environ, DROIDCTL_HOME=home),
                           capture_output=True)
        shutil.rmtree(home, ignore_errors=True)
    agg = aggregate(records)
    result = {"date": datetime.datetime.now().isoformat(timespec="seconds"), "dry_run": a.dry_run,
              "model": None if a.dry_run else a.model, "runs": a.runs, "variants": variants,
              "tasks": [t.name for t in tasks], "device": a.device and "SM-N950F (API 28)",
              "host": platform.platform(), "summary": agg,
              "decisions": decisions(agg),
              "note": f"{a.runs} run(s) per task/variant, 1 model ({a.model}): "
                      + ("provisional, not statistically meaningful" if a.runs < 3 else "the protocol's run count, one model"),
              "records": records}
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w") as f:
            json.dump(result, f, indent=2)
    print(json.dumps({"summary": agg, "decisions": result["decisions"]}, indent=2))
    return result


if __name__ == "__main__":
    main()
