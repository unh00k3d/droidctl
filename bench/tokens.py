"""Token cost of a screen: raw device JSON vs the flat and spatial snapshots.

    .venv/bin/python bench/tokens.py [--json] [--out bench/results/tokens.json]

Offline, over every captured fixture in tests/fixtures/trees/. Token counts are
an ESTIMATE (~3.5 characters per token, snapshot.est_tokens); no tokenizer is
used, and the ratio between the columns matters more than the absolute value.
The raw column is the device `tree` result as compact JSON, i.e. what an agent
would read if it were handed the tree directly.
"""
import argparse
import datetime
import glob
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from droidctl import snapshot as S  # noqa: E402

FIXTURES = os.path.join(HERE, "..", "tests", "fixtures", "trees")


def measure(path):
    with open(path, encoding="utf-8") as f:
        fx = json.load(f)
    tree, meta = fx["tree"], fx.get("meta", {})
    snap = S.build(tree, activity=meta.get("activity"))
    flat = S.render(snap, S.Opts(layout="flat"))
    spatial = S.render(snap, S.Opts(layout="spatial"))
    raw = json.dumps(tree, separators=(",", ":"), ensure_ascii=False)
    return {"fixture": os.path.basename(path)[:-5], "elements": len(snap.elements),
            "raw": S.est_tokens(raw), "flat": S.est_tokens(flat), "spatial": S.est_tokens(spatial)}


def summarize(rows):
    def col(k):
        xs = [r[k] for r in rows]
        return {"median": statistics.median(xs), "max": max(xs), "sum": sum(xs)}
    out = {k: col(k) for k in ("raw", "flat", "spatial")}
    out["spatial_over_flat_pct"] = round(100.0 * (out["spatial"]["sum"] / out["flat"]["sum"] - 1), 1)
    out["raw_over_spatial_x"] = round(out["raw"]["sum"] / out["spatial"]["sum"], 1)
    out["over_2k"] = [r["fixture"] for r in rows if r["spatial"] > 2000 or r["flat"] > 2000]
    return out


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--json", action="store_true")
    p.add_argument("--out", default=os.path.join(HERE, "results", "tokens.json"))
    a = p.parse_args(argv)
    rows = [measure(f) for f in sorted(glob.glob(os.path.join(FIXTURES, "*.json")))]
    groups = {"real": [r for r in rows if r["fixture"].startswith("real-")],
              "testapp": [r for r in rows if r["fixture"].startswith("testapp-")]}
    result = {"date": datetime.date.today().isoformat(),
              "note": "token counts are an ESTIMATE: len(text)/3.5, no tokenizer",
              "summary": {g: summarize(rs) for g, rs in groups.items() if rs},
              "fixtures": rows}
    if a.out:
        os.makedirs(os.path.dirname(a.out), exist_ok=True)
        with open(a.out, "w") as f:
            json.dump(result, f, indent=2)
    if a.json:
        print(json.dumps(result, indent=2))
    else:
        for g, s in result["summary"].items():
            print(f"{g:8} n={len(groups[g]):3}  raw median {s['raw']['median']:>6}  flat median "
                  f"{s['flat']['median']:>4} max {s['flat']['max']:>4}  spatial median {s['spatial']['median']:>4} "
                  f"max {s['spatial']['max']:>4}  spatial/flat {s['spatial_over_flat_pct']:+}%  "
                  f"raw/spatial {s['raw_over_spatial_x']}x")
    return result


if __name__ == "__main__":
    main()
