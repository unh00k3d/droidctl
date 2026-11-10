"""Round 9: the AX page scroll (2006 on the table) animates, but both idle replies come back in ~10 ms.
Ground truth: the test app's DTA 'scroll end' (ISO time, same device clock as CFAbsoluteTime).
For each delay D: act 2006, sleep D, request animations idle (and run-loop idle); record when each replied."""
import datetime, json, os, subprocess, sys, time
from s2 import Client, walk
A = "dev.droidctl.s1.testapp"
c = Client("127.0.0.1:28100", timeout=120)
CDID = open(".cdid").read().strip(); DEV = dict(os.environ, DEVELOPER_DIR="/Users/enesb/Downloads/Xcode.app/Contents/Developer")
EPOCH = datetime.datetime(2001, 1, 1, tzinfo=datetime.timezone.utc)
c.call("dlaunch", bundle=A); time.sleep(2)
tbl = next(n for n, _ in walk(c.call("snapfast", bundle=A)["tree"]) if n.get("id") == "dta_table")
recs = []
for rnd in range(int(sys.argv[1])):
    for d in (0, 30, 60, 120):
        for act in (2006, 2007):
            t0 = c.call("stats")  # warm
            r = c.call("watch", do={"cmd": "act", "ax": tbl["ax"], "action": act}, bundle=A, watch_ms=1, poll_ms=0, idles=[])
            t0abs = r["t0abs"]
            time.sleep(d / 1000)
            # The standalone `idle` command never gets its reply (unexplained); `settle` around a no-op does.
            s = c.call("settle", do={"cmd": "stats"}, bundle=A, timeout_ms=3000, rearm=0)
            recs.append({"act": act, "delay": d, "t0abs": t0abs, "idle_reply_ms": s["rounds"][0][1], "settled_ms": round(s["settle_ms"]),
                         "settle_t0_offset": None})
            time.sleep(1.2)
subprocess.run(["xcrun", "devicectl", "device", "copy", "from", "--device", CDID, "--domain-type", "appDataContainer", "--domain-identifier", A,
                "--source", "Documents/dta.log", "--destination", "exp9-dta.log"], capture_output=True, env=DEV)
ends = []
for line in open("exp9-dta.log"):
    if "scroll end" in line:
        ts = datetime.datetime.fromisoformat(line.split()[0].replace("Z", "+00:00"))
        ends.append((ts - EPOCH).total_seconds() * 1000)
for r in recs:
    nxt = [e for e in ends if e >= r["t0abs"]]
    r["scroll_end_ms"] = round(nxt[0] - r["t0abs"]) if nxt else None
    print(r["act"], r["delay"], "scroll_end", r["scroll_end_ms"], "idle replies after", r["idle_reply_ms"], "ms of the settle call")
json.dump(recs, open("results/exp9-pagescroll.json", "w"), indent=1)
