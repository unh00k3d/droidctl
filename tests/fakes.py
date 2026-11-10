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
        self._conns = {}
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

    def drop_subscribers(self):
        """What an unbind does to the event stream: the connection just closes."""
        with self.lock:
            subs = list(self.subscribers)
        for f in subs:
            try:
                self._conns[f].shutdown(socket.SHUT_RDWR)
            except (OSError, KeyError):
                pass

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
        self._conns[f] = conn
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


class FakeUsbmux:
    """usbmuxd's plist protocol over TCP (point droidctl at it with
    USBMUXD_SOCKET_ADDRESS=127.0.0.1:<port>). `phones` is {udid: {device_port: tcp_port}}:
    a Connect to a listed device port is piped to that local TCP port, as usbmuxd
    pipes it to the phone; any other port is refused. `paired` lists the UDIDs with
    a pair record (default: all)."""

    def __init__(self, phones, paired=None):
        import plistlib, struct
        self._plist, self._hdr = plistlib, struct.Struct("<IIII")
        self.phones = phones
        self.paired = set(phones) if paired is None else set(paired)
        self.ids = {u: 100 + i for i, u in enumerate(phones)}
        self.calls = collections.Counter()
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.address = f"127.0.0.1:{self.port}"
        _serve(self.sock, self._conn)

    def _send(self, conn, tag, msg):
        body = self._plist.dumps(msg)
        conn.sendall(self._hdr.pack(self._hdr.size + len(body), 1, 8, tag) + body)

    def _recv(self, conn):
        head = b""
        while len(head) < self._hdr.size:
            chunk = conn.recv(self._hdr.size - len(head))
            if not chunk:
                return None, None
            head += chunk
        length, _, _, tag = self._hdr.unpack(head)
        body = b""
        while len(body) < length - self._hdr.size:
            body += conn.recv(length - self._hdr.size - len(body))
        return tag, self._plist.loads(body)

    def _conn(self, conn):
        try:
            while True:
                tag, msg = self._recv(conn)
                if msg is None:
                    return
                kind = msg.get("MessageType")
                self.calls[kind] += 1
                if kind == "ListDevices":
                    self._send(conn, tag, {"DeviceList": [
                        {"DeviceID": did, "MessageType": "Attached",
                         "Properties": {"ConnectionType": "USB", "DeviceID": did, "SerialNumber": u, "ProductID": 4776}}
                        for u, did in self.ids.items()]})
                elif kind == "ReadPairRecord":
                    if msg.get("PairRecordID") in self.paired:
                        self._send(conn, tag, {"PairRecordData": b"fake"})
                    else:
                        self._send(conn, tag, {"MessageType": "Result", "Number": 2})
                elif kind == "Connect":
                    udid = next((u for u, d in self.ids.items() if d == msg.get("DeviceID")), None)
                    port = socket.ntohs(msg.get("PortNumber", 0))
                    target = self.phones.get(udid, {}).get(port) if udid else None
                    if udid is None:
                        self._send(conn, tag, {"MessageType": "Result", "Number": 2})
                        continue
                    try:
                        up = socket.create_connection(("127.0.0.1", target)) if target else None
                    except OSError:
                        up = None
                    if up is None:
                        self._send(conn, tag, {"MessageType": "Result", "Number": 3})
                        continue
                    self._send(conn, tag, {"MessageType": "Result", "Number": 0})
                    _pipe(conn, up)
                    return
                else:
                    self._send(conn, tag, {"MessageType": "Result", "Number": 1})
        except OSError:
            pass

    def close(self):
        self.sock.close()


def _pipe(a, b):
    """Copy bytes both ways until either side closes."""
    def copy(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
    threading.Thread(target=copy, args=(b, a), daemon=True).start()
    copy(a, b)


IOS_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "ios-runner")


class FakeRunner:
    """The iOS runner's side of the agent protocol over TCP (reach it through FakeUsbmux).
    Serves a REAL captured runner tree (tests/fixtures/ios-runner); `build` is what ping reports."""

    def __init__(self, fixture="settings_general", build="test-build"):
        with open(os.path.join(IOS_FIXTURES, fixture + ".json")) as f:
            self.tree = json.load(f)["tree"]
        self.build, self.gen = build, 3
        self.calls = collections.Counter()
        self.params = {}                    # method -> the last params it got
        self.focused = True                 # what the runner's focus check would find
        self.after_type = None              # a fixture the screen becomes once keys are typed
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        _serve(self.sock, self._conn)

    def _reply(self, method, params):
        if method == "ping":
            return {"protocol": 3, "version": "0.1.0", "build": self.build, "backend": "ios-runner", "platform": "ios",
                    "release": "18.7.8", "model": "iPhone13,2", "gen": self.gen, "screen": self.tree["screen"],
                    "caps": {"units": "pt", "has_back": False, "events": True, "global": ["home"]}}
        if method == "gen":
            return {"gen": self.gen, "dump": self.tree["dump"]}
        if method == "tree":
            return dict(self.tree, gen=self.gen)
        if method == "subscribe":
            return {"subscribed": params.get("events") or [], "next": 0}
        if method in ("unsubscribe", "quit"):
            return {}
        if method == "events":
            return {"events": [], "next": 0}
        if method == "current":
            return {"pkg": self.tree.get("front"), "keyboard": False, "gen": self.gen, "windows": []}
        # inputs: the runner's reply shapes; the screen is the replayed tree (a fake can't change it)
        if method in ("act", "type", "hide_keyboard", "global", "gesture"):
            if method == "act" and params.get("dump") != self.tree["dump"]:
                raise _RpcError(-32002, "stale dump")
            if method in ("type",) or params.get("action") == "ime_enter":
                if not self.focused:
                    raise _RpcError(-32012, "no input has the keyboard focus")
            r = {"performed": True, "method": "ax", "events": []}
            if method == "type" and self.after_type:
                with open(os.path.join(IOS_FIXTURES, self.after_type + ".json")) as f:
                    self.tree = json.load(f)["tree"]
                self.gen += 1
            if method == "act" and params.get("action") in ("click", "long_click"):
                r.update(method="touch", delivered=True, point=[100, 100])
            if params.get("settle") is not None:
                r.update(idle=True, settle_ms=120, tree=dict(self.tree, gen=self.gen))
            if method == "hide_keyboard":
                r.update(hidden=True, method="return_key")
            return r
        if method in ("launch", "open_url"):
            r = {"performed": True, "events": []}
            if params.get("settle") is not None:
                r.update(idle=True, settle_ms=300, tree=dict(self.tree, gen=self.gen))
            return r
        if method == "mark":
            return {"marked": params.get("label")}
        if method == "screenshot":
            # an image of the screen's shape (points x 3 x scale), JPEG like the runner's
            import base64
            import io
            from PIL import Image
            scr, k = self.tree["screen"], 3 * params.get("scale", 0.5)
            buf = io.BytesIO()
            Image.new("RGB", (round(scr["w"] * k), round(scr["h"] * k)), (0, 0, 0)).save(buf, "JPEG")
            return {"format": "jpeg", "w": round(scr["w"] * k), "h": round(scr["h"] * k), "scale": params.get("scale"),
                    "quality": params.get("quality"), "rotation": 0, "data": base64.b64encode(buf.getvalue()).decode()}
        if method == "wait_change":
            return {"gen": self.gen, "changed": False}
        if method == "wait_idle":
            return {"idle": True, "ms": 105, "events": []}
        raise _RpcError(-32006, f"{method} is not implemented by this runner yet")

    def _conn(self, conn):
        f = conn.makefile("rb")
        try:
            for line in f:
                req = json.loads(line)
                method = req.get("method")
                self.calls[method] += 1
                self.params[method] = req.get("params") or {}
                try:
                    msg = {"jsonrpc": "2.0", "id": req.get("id"), "result": self._reply(method, req.get("params") or {})}
                except _RpcError as e:
                    msg = {"jsonrpc": "2.0", "id": req.get("id"), "error": {"code": e.code, "message": str(e)}}
                conn.sendall((json.dumps(msg) + "\n").encode())
        except (OSError, ValueError):
            pass
        finally:
            conn.close()

    def close(self):
        self.sock.close()
