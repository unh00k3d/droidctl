"""Tiny NDJSON client for the S2 spike server.  python3 s2.py HOST:PORT CMD [key=value ...]"""
import json
import os
import socket
import sys
import time

TOKEN = os.environ.get("S2_TOKEN") or open(os.path.join(os.path.dirname(__file__), ".token")).read().strip()


class Client:
    def __init__(self, addr="127.0.0.1:18100", timeout=60):
        host, port = addr.rsplit(":", 1)
        fam = socket.AF_INET6 if ":" in host else socket.AF_INET
        self.s = socket.socket(fam, socket.SOCK_STREAM)
        self.s.settimeout(timeout)
        self.s.connect((host.strip("[]"), int(port)))
        self.f = self.s.makefile("rb")
        self.n = 0

    def call(self, cmd, **kw):
        self.n += 1
        req = dict(kw, cmd=cmd, token=TOKEN, id=self.n)
        t0 = time.perf_counter()
        self.s.sendall((json.dumps(req) + "\n").encode())
        line = self.f.readline()
        if not line:
            raise ConnectionError("closed")
        r = json.loads(line)
        r["rtt_ms"] = (time.perf_counter() - t0) * 1000
        return r


def walk(n, depth=0):
    yield n, depth
    for c in n.get("children", []):
        yield from walk(c, depth + 1)


if __name__ == "__main__":
    c = Client(sys.argv[1], timeout=10)
    kw = {}
    for a in sys.argv[3:]:
        k, v = a.split("=", 1)
        kw[k] = int(v) if v.lstrip("-").isdigit() else v
    r = c.call(sys.argv[2], **kw)
    if "tree" in r:
        r["tree"] = f"<{r['nodes']} nodes>"
    print(json.dumps(r))
