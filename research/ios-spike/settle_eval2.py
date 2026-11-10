"""Score the runner's `settle` command (exp8.py output) against polled truth, with settle_eval.py's criterion:
correct when the settled time is no earlier than the start of the first snapshot that saw the final state;
empty wrapper nodes (cosmetic) are ignored.  python3 settle_eval2.py results/exp8-*.json"""
import json, statistics, sys
def cosmetic(line):
    parts = line.split("|")
    return len(parts) >= 4 and parts[0] in ("1", "3", "4") and not parts[2] and not parts[3]
for f in sys.argv[1:]:
    d = json.load(open(f))
    S = {(r["k"], r["round"]): r for r in d if r["mode"] == "S"}
    P = {(r["k"], r["round"]): r for r in d if r["mode"] == "P"}
    ok, early, waits = 0, [], []
    for k, s in S.items():
        ch = [c for c in P[k]["changes"] if not all(cosmetic(l) for l in c[2] + c[3])]
        t = ch[-1][0] if ch else 0
        waits.append(s["settle_ms"])
        if s["settle_ms"] >= t: ok += 1
        else: early.append((k, s["settle_ms"], t))
    print(f"{f}: correct {ok}/{len(S)}  median {round(statistics.median(waits))} ms  max {max(waits)} ms  early={early}")
