"""Stand-ins for adb and the on-device agent, so the real daemon can be tested
without a phone.

- FakeAdb answers the two adb host-protocol services droidctl's hot path uses
  (`host:devices`, `host:list-forward`); point droidctl at it with
  ANDROID_ADB_SERVER_PORT. Its forwards lead to the FakeAgents' TCP ports.
- FakeAgent speaks the agent's NDJSON JSON-RPC (PROTOCOL.md) over TCP and
  serves a REAL captured tree (tests/fixtures/trees), never a hand-written one.
  Tests drive it: `change()` bumps `gen` and pushes an event like a screen
  change would, `toast()` pushes a toast; counters record what was asked.
"""
import collections
import json
import os
import socket
import threading
import time

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "trees")


def _serve(sock, handler):
    def loop():
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            threading.Thread(target=handler, args=(conn,), daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()


class FakeAgent:
    def __init__(self, fixture="real-settings-main", tree_delay=0.0, act_delay=0.0):
        with open(os.path.join(FIXTURES, fixture + ".json")) as f:
            self.base = json.load(f)["tree"]
        self.tree_delay, self.act_delay = tree_delay, act_delay
        self.gen, self.dump, self.seq = 5, 0, 0
        self.calls = collections.Counter()
        self.lock = threading.Lock()
        self.subscribers = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        _serve(self.sock, self._conn)

    # -- test controls
    def change(self, type="window_content"):
        with self.lock:
            self.gen += 1
        self._push(type, pkg="com.android.settings")

    def toast(self, text):
        self._push("toast", text=text, pkg="com.android.settings")

    def close(self):
        self.sock.close()

    # -- protocol
    def _push(self, type, **fields):
        with self.lock:
            self.seq += 1
            ev = dict(fields, seq=self.seq, t=int(time.monotonic() * 1000), gen=self.gen, type=type)
            subs = list(self.subscribers)
        for f in subs:
            try:
                f.write((json.dumps({"jsonrpc": "2.0", "method": "event", "params": ev}) + "\n").encode())
                f.flush()
            except OSError:
                pass

    def _tree(self):
        if self.tree_delay:
            time.sleep(self.tree_delay)
        with self.lock:
            self.dump += 1
            return dict(self.base, gen=self.gen, dump=self.dump, degraded=False)

    def _call(self, method, p, f):
        self.calls[method] += 1
        if method == "ping":
            from droidctl import device as dev
            return {"protocol": dev.PROTOCOL, "version": "fake", "versionCode": dev.AGENT_VERSION_CODE,
                    "sdk": 28, "release": "9",
                    "manufacturer": "fake", "model": "FAKE", "device": "fake",
                    "screen": {"w": 1080, "h": 2220, "density": 420, "rotation": 0},
                    "service": {"connected": True}, "gen": self.gen, "peer_uid": 2000, "uptime_ms": 1}
        if method == "gen":
            return {"gen": self.gen}
        if method == "tree":
            return self._tree()
        if method == "subscribe":
            with self.lock:
                self.subscribers.append(f)
            return {"subscribed": True, "next": self.seq}
        if method in ("act", "gesture", "global"):
            if self.act_delay:
                time.sleep(self.act_delay)
            with self.lock:
                self.gen += 1
            out = {"performed": True, "events": [], "idle": True, "settle_ms": 1}
            if p.get("settle") and p["settle"].get("tree", True):
                out["tree"] = self._tree()
            return out
        if method == "wait_idle":
            return {"idle": True, "ms": 1, "events": []}
        if method == "wait_for":
            time.sleep(p.get("timeout_ms", 0) / 1000)
            raise _RpcError(-32007, "timed out")
        if method == "events":
            return {"events": [], "next": self.seq}
        if method == "current":
            return {"pkg": "com.android.settings", "activity": ".Settings", "keyboard": False,
                    "windows": [], "gen": self.gen}
        raise _RpcError(-32601, f"method not found: {method}")

    def _conn(self, conn):
        f = conn.makefile("rwb")
        try:
            for line in f:
                req = json.loads(line)
                try:
                    resp = {"result": self._call(req["method"], req.get("params") or {}, f)}
                except _RpcError as e:
                    resp = {"error": {"code": e.code, "message": str(e)}}
                if "id" in req:
                    f.write((json.dumps(dict(resp, jsonrpc="2.0", id=req["id"])) + "\n").encode())
                    f.flush()
        except (OSError, ValueError):
            pass
        finally:
            with self.lock:
                if f in self.subscribers:
                    self.subscribers.remove(f)
            conn.close()


class _RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class FakeAdb:
    """{serial: FakeAgent}: every serial is 'device' and has a droidctl forward."""

    def __init__(self, agents):
        self.agents = agents
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        _serve(self.sock, self._conn)

    def _conn(self, conn):
        try:
            n = int(conn.recv(4), 16)
            service = conn.recv(n).decode()
            if service == "host:devices":
                body = "".join(f"{s}\tdevice\n" for s in self.agents)
            elif service == "host:list-forward":
                body = "".join(f"{s} tcp:{a.port} localabstract:droidctl\n" for s, a in self.agents.items())
            elif service == "host:version":
                body = "0029"
            else:
                conn.sendall(b"FAIL" + b"0007unknown")
                return
            data = body.encode()
            conn.sendall(b"OKAY" + f"{len(data):04x}".encode() + data)
        except (OSError, ValueError):
            pass
        finally:
            conn.close()

    def close(self):
        self.sock.close()
