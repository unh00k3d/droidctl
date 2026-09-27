#!/usr/bin/env python3
"""Regenerate the snapshot goldens (tests/fixtures/snap) from the real fixtures.

    .venv/bin/python scripts/make_goldens.py

Review `git diff tests/fixtures/snap` before committing: a golden is a claim
that the output is *right* for that raw tree, not just what the code printed.
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from droidctl import snapshot as S  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures"
out_dir = ROOT / "snap"
out_dir.mkdir(exist_ok=True)
for f in sorted((ROOT / "trees").glob("*.json")):
    d = json.loads(f.read_text())
    snap = S.build(d["tree"], activity=d["meta"].get("activity"))
    for kind, layout in (("snap", "spatial"), ("flat", "flat")):
        path = out_dir / f"{f.stem}.{kind}.txt"
        path.write_text(S.render(snap, S.Opts(layout=layout)) + "\n")
        print(path.relative_to(ROOT.parent.parent))
