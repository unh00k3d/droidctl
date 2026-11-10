"""Score settle rules against ground truth.  python3 settle_eval.py settle3b-dev.json
Truth (mode P): time of the last on-screen signature change in the watch window (resolution = one snapshot).
Rules (mode E): computed from the AX notification timeline + idle callback times of the same scenario/round."""
import json, sys, statistics
from collections import defaultdict
d = json.load(open(sys.argv[1]))
CHANGE = {1000, 1001, 1006, 1008, 1009, 1017, 1018, 1020, 1021, 1025, 1031, 3031}
UT_CHANGE = ("ViewDidAppear", "ViewDidDisappear", "KeyEventComplete", "TouchEventsCompleted")
def evs(r):
    out = []
    for e in r.get("events", []):
        if e.get("via") == 2: continue
        n, p = e["n"], e["p"]
        if n == 4002:
            ev = p.split("event = ")[1].split(";")[0].strip() if "event = " in p else ""
            if ev in UT_CHANGE: out.append((e["t"], "UT:" + ev))
        elif n in CHANGE: out.append((e["t"], str(n)))
    return sorted(out)
def rule_quiet(r, first_ms, quiet_ms, cap):
    """Android-style: wait up to first_ms for a change event; then until quiet_ms without one."""
    ts = [t for t, _ in evs(r)]
    start = r["inner_ms"]  # settle starts when the action returns
    after = [t for t in ts if t >= 0]
    if not after or after[0] > max(start, first_ms): return max(start, first_ms) if not after else max(start, first_ms)
    last = after[0]
    for t in after[1:]:
        if t - last > quiet_ms: break
        last = t
    return min(max(start, last + quiet_ms), cap)
def idle_both(r):
    i = {x["kind"]: x.get("end", 1e9) for x in r.get("idles", [])}
    return max(i.get("runloop", 1e9), i.get("anim", 1e9))
truth = {}
for r in d:
    if r["mode"] == "P":
        def cosmetic(line):  # an empty Other/Window/Group wrapper: invisible in droidctl's snapshot
            parts = line.split("|")
            return len(parts) >= 4 and parts[0] in ("1", "3", "4") and not parts[2] and not parts[3]
        sg = [s for s in r["sigs"] if not s.get("pre") and not all(cosmetic(l) for l in s.get("add", []) + s.get("rem", []))]
        # final state first seen by the snapshot that started at sg[-1]["t"] and ended at sg[-1]["te"]
        truth[(r["k"], r["round"])] = (round(sg[-1]["te"]) if sg else 0, round(statistics.mean(s["ms"] for s in r["sigs"])), len(sg), round(sg[-1]["t"]) if sg else 0)
def anim_end(r):
    return {x["kind"]: x.get("end", 1e9) for x in r.get("idles", [])}.get("anim", 1e9)
def runloop_end(r):
    return {x["kind"]: x.get("end", 1e9) for x in r.get("idles", [])}.get("runloop", 1e9)
def idle_then_quiet(r, q):
    t = max(r["inner_ms"], anim_end(r), runloop_end(r))
    for e, _ in evs(r):
        if t < e <= t + q: t = e
    return t + q
rules = {
    "idle+quiet100": lambda r: idle_then_quiet(r, 100),
    "idle+quiet50": lambda r: idle_then_quiet(r, 50),
    "quiet250/first400": lambda r: rule_quiet(r, 400, 250, 5000),
    "max(q150,anim)": lambda r: max(rule_quiet(r, 600, 150, 5000), anim_end(r)),
    "max(q250,anim)": lambda r: max(rule_quiet(r, 400, 250, 5000), anim_end(r)),
    "max(q250,anim,runloop)": lambda r: max(rule_quiet(r, 400, 250, 5000), anim_end(r), runloop_end(r)),
    "quiet150/first600": lambda r: rule_quiet(r, 600, 150, 5000),
    "quiet300/first600": lambda r: rule_quiet(r, 600, 300, 5000),
    "idle(runloop+anim)": idle_both,
    "max(quiet150, idle)": lambda r: max(rule_quiet(r, 600, 150, 5000), idle_both(r)),
}
rows = defaultdict(list)
for r in d:
    if r["mode"] != "E": continue
    t = truth.get((r["k"], r["round"]))
    if not t: continue
    rows[r["k"]].append((t, {k: round(f(r)) for k, f in rules.items()}))
print(f"{'scenario':24} {'truth(last change) [snap ms]':30} " + " ".join(f"{k:>20}" for k in rules))
score = defaultdict(lambda: [0, 0, []])
for k, lst in rows.items():
    for (tl, sm, nch, ts0), res in lst:
        cells = []
        for rk, v in res.items():
            ok = v >= ts0  # strict: the rule's final snapshot starts no earlier than the first snapshot that saw the final state
            score[rk][0] += ok; score[rk][1] += 1; score[rk][2].append(v)
            cells.append(f"{v:>6}{'' if ok else ' EARLY':6}")
        print(f"{k:24} {tl:>6} [{sm:>4}] n={nch:<3}            " + " ".join(f"{c:>20}" for c in cells))
print()
for rk, (ok, n, vs) in score.items():
    print(f"{rk:22} correct {ok}/{n}  median wait {round(statistics.median(vs))} ms  max {max(vs)} ms")
