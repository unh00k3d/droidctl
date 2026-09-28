"""Stress the agent's event ring the way a daemon does: a live subscription plus
`events` reads on another connection, while scrolling a long list floods compactable
events (scrolled / window_content). Reports whether the agent process survived.

    .venv/bin/python bench/event_stress.py -d SERIAL [--seconds 60]

Regression check for the ConcurrentModificationException that killed the agent when
compaction mutated an event being serialized (dropbox crashes, agents 0.2.0-0.4.1).
"""
import argparse
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import testapp as T  # noqa: E402
from droidctl import device as dev  # noqa: E402
from droidctl.core import UserError  # noqa: E402


def agent_pid(serial):
    return T.adb(serial, "shell", "pidof", dev.PKG, check=False).strip()


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("-d", "--device", default=os.environ.get("ANDROID_SERIAL"), required=False)
    p.add_argument("--seconds", type=float, default=60)
    p.add_argument("--subscribers", type=int, default=1, help="more subscribers widen the race window")
    a = p.parse_args(argv)
    serial = a.device
    T.launch(serial, "long_list")
    port = dev.ensure_forward(serial)
    pid0 = agent_pid(serial)
    stop = threading.Event()
    counts = {"notes": 0, "reads": 0, "errors": 0}

    def subscriber():
        try:
            c = dev.AgentClient(port)
            c.subscribe()
            while not stop.is_set():
                for _ in c.notifications(timeout=0.5):
                    counts["notes"] += 1
        except UserError:
            counts["errors"] += 1

    def reader():
        c = dev.AgentClient(port)
        while not stop.is_set():
            try:
                c.call("events", {"since": 0})
                counts["reads"] += 1
            except UserError:
                counts["errors"] += 1
                return

    threads = [threading.Thread(target=subscriber, daemon=True) for _ in range(a.subscribers)]
    threads.append(threading.Thread(target=reader, daemon=True))
    for t in threads:
        t.start()
    swipes = 0
    t_end = time.time() + a.seconds
    with dev.AgentClient(port) as c:
        while time.time() < t_end:
            y0, y1 = (1700, 500) if swipes % 2 == 0 else (500, 1700)
            try:
                c.call("gesture", {"type": "swipe", "points": [[540, y0], [540, y1]], "ms": 120}, timeout=5)
            except UserError:
                counts["errors"] += 1
                break
            swipes += 1
    stop.set()
    time.sleep(1)
    pid1 = agent_pid(serial)
    survived = bool(pid0) and pid0 == pid1
    print({"survived": survived, "pid_before": pid0, "pid_after": pid1, "swipes": swipes, **counts})
    return 0 if survived else 1


if __name__ == "__main__":
    sys.exit(main())
