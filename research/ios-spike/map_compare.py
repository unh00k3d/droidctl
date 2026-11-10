"""Before/after: the throwaway runner2raw mapper vs the ios_map prototype on every captured iOS tree."""
import glob, json, sys, io, contextlib
sys.path.insert(0, "/Users/enesb/Documents/droidctl")
from droidctl import snapshot as S
import runner2raw as OLD, ios_map as NEW
o = S.Opts.from_env()
def render(conv, fx):
    snap = S.build(conv(fx)); text = S.render(snap, o)
    return snap, text
def load(p):
    d = json.load(open(p))
    return d if "tree" in d and "meta" in d else {"meta": {"name": p}, "tree": d["tree"] if "tree" in d else d}
paths = sys.argv[1:]
print(f"{'tree':34} {'old el':>6} {'old tok':>7} {'warn':>5} | {'new el':>6} {'new tok':>7} {'warn':>5}")
for p in paths:
    fx = load(p)
    OLD.h[0] = 0
    so, to = render(OLD.convert, fx)
    sn, tn = render(lambda f: NEW.convert(f["tree"]), fx)
    wo = sum(1 for l in to.splitlines() if l.startswith("!")); wn = sum(1 for l in tn.splitlines() if l.startswith("!"))
    print(f"{p.split('/')[-1][:34]:34} {len(so.elements):>6} {S.est_tokens(to):>7} {wo:>5} | {len(sn.elements):>6} {S.est_tokens(tn):>7} {wn:>5}")
    if "--show" in sys.argv[0:0] or p.endswith(tuple(a for a in [""] if False)): pass
