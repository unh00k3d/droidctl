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
    """{serial: FakeAgent}: every serial is 'device' and has a droidctl forward.

    With `shell` (a function: command line -> (stdout bytes, stderr bytes, exit code))
    it also serves what adbutils sends for device work: `host:tport:serial`,
    `features`, `shell,v2:` (stdout/stderr/exit packets) and `sync:` (STAT, SEND).
    Every shell line lands in `shell_lines`, every pushed file in `pushed` {path: bytes}."""

    def __init__(self, agents, shell=None):
        self.agents = agents
        self.shell = shell
        self.shell_lines = []
        self.pushed = {}
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        _serve(self.sock, self._conn)

    @staticmethod
    def _read(conn, n):
        buf = b""
        while len(buf) < n:
            got = conn.recv(n - len(buf))
            if not got:
                raise OSError("closed")
            buf += got
        return buf

    def _conn(self, conn):
        try:
            while True:     # a transport switch keeps the connection for the next service
                service = self._read(conn, int(self._read(conn, 4), 16)).decode()
                if service.startswith("host:tport:serial:") and self.shell:
                    conn.sendall(b"OKAY" + b"\x01" + bytes(7))     # OKAY + the transport id
                    continue
                if service.startswith("host-serial:") and service.endswith(":features") and self.shell:
                    body = "shell_v2,cmd,stat_v2"
                elif service.startswith("shell,v2:") and self.shell:
                    line = service[len("shell,v2:"):]
                    self.shell_lines.append(line)
                    out, err, rc = self.shell(line)
                    conn.sendall(b"OKAY")
                    for pid, data in ((1, out), (2, err)):
                        if data:
                            conn.sendall(bytes([pid]) + len(data).to_bytes(4, "little") + data)
                    conn.sendall(bytes([3]) + (1).to_bytes(4, "little") + bytes([rc]))
                    return
                elif service == "sync:" and self.shell:
                    conn.sendall(b"OKAY")
                    self._sync(conn)
                    return
                elif service == "host:devices":
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
                return
        except (OSError, ValueError):
            pass
        finally:
            conn.close()

    def _sync(self, conn):
        import struct
        cmd = self._read(conn, 4)
        path = self._read(conn, struct.unpack("<I", self._read(conn, 4))[0]).decode()
        if cmd == b"STAT":
            conn.sendall(b"STAT" + bytes(12))                     # does not exist
        elif cmd == b"SEND":
            path, data = path.rsplit(",", 1)[0], b""
            while True:
                kind, n = self._read(conn, 4), struct.unpack("<I", self._read(conn, 4))[0]
                if kind == b"DONE":
                    break
                data += self._read(conn, n)
            self.pushed[path] = data
            conn.sendall(b"OKAY" + bytes(4))

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


def broadcast_reply(code, data=None):
    """What `am broadcast` prints for an ordered broadcast. Like the real one, the data
    is NOT escaped: a JSON value with quotes in it goes between the quotes as is."""
    out = "Broadcasting: Intent { act=com.enesb.proxyapp.CMD flg=0x400000 cmp=com.proxyapp.mobile/... }\n"
    out += f"Broadcast completed: result={code}"
    if data is not None:
        out += f', data="{json.dumps(data, separators=(",", ":"))}"'
    return out + "\n"


class FakeProxyApp:
    """proxy-app (com.proxyapp.mobile) behind FakeAdb's `shell`: its AdbReceiver's
    commands over `am broadcast` (status, list, add, connect, disconnect, remove, log),
    with the receiver's reply shapes and selection rules (AdbReceiver.kt,
    ProfileRepository.kt: `selected` falls back to the first profile), plus the
    `pm path` / `dumpsys package` / `pm install` that setup uses.

    `profiles` is [{id, name, ...}]; `selected_id` and `state`/`active_id` are the VPN.
    `consent=True` makes the next connect ask for VPN permission (once accepted, the
    test calls `accept()`). `installed` is the versionCode on the phone (None: absent)."""

    def __init__(self, profiles=(), selected_id=None, active_id=None, installed=1, consent=False,
                 apps=("com.android.chrome",)):
        self.profiles = [dict(p) for p in profiles]
        self.selected_id, self.active_id = selected_id, active_id
        self.installed, self.consent, self.apps = installed, consent, set(apps)
        self.broadcasts, self.installs, self.connects = [], 0, []
        self.connect_state = "connected"         # what a connect ends in ("connecting": slow, "error")
        self.log = [{"time": "10:00:01", "proto": "tcp", "app": "com.android.chrome", "target": "example.com:80",
                     "dst": "127.0.0.1:8080", "status": "ok", "up": 120, "down": 1400}]
        self._n = 0

    # -- state as the app computes it
    def selected(self):
        return (next((p for p in self.profiles if p["id"] == self.selected_id), None)
                or (self.profiles[0] if self.profiles else None))

    def _find(self, q):
        hit = [p for p in self.profiles if p["id"] == q] or \
              [p for p in self.profiles if p["name"].lower() == q.lower()]
        if len(hit) != 1:
            raise ValueError(f'no profile matches "{q}"')
        return hit[0]

    def profile_json(self, p):
        return {"id": p["id"], "name": p["name"], "type": p.get("type", "http"), "host": p.get("host", "h"),
                "port": p.get("port", 1), "auth": bool(p.get("user")), "tag": p.get("tag", ""),
                "apps": {"only": "only_selected", "all": "all", "except": "except_selected"}[p.get("mode", "all")],
                "mode": p.get("mode", "all"), "packages": sorted(p.get("packages", [])),
                "dns": p.get("dns", "auto"), "blockQuic": p.get("blockQuic", False)}

    def status(self):
        active = next((p for p in self.profiles if p["id"] == self.active_id), None)
        st = {"state": "off" if active is None else self.connect_state}
        prof = active or self.selected()
        if active is not None:
            st["profile"] = active["name"]
            if self.connect_state == "error":
                st["error"] = 'dial "h:1": refused'
        else:
            st["profile"] = st["selected"] = prof and prof["name"]
        st.update(mode=prof and prof.get("mode", "all"), apps=sorted(prof.get("packages", [])) if prof else [],
                  blockQuic=prof and prof.get("blockQuic", False), dnsPolicy=prof and prof.get("dns", "auto"))
        return st

    # -- the receiver
    def receive(self, extras, flags):
        cmd = extras.get("cmd", "status")
        if cmd == "status":
            return 0, self.status()
        if cmd == "list":
            sel = self.selected()
            return 0, [dict(self.profile_json(p), selected=p is sel, active=p["id"] == self.active_id)
                       for p in self.profiles]
        if cmd == "add":
            import urllib.parse
            u = urllib.parse.urlsplit(extras["url"])
            existing = next((p for p in self.profiles if p["name"].lower() == extras["name"].lower()), None)
            if existing is None:
                self._n += 1
                existing = {"id": f"id-new-{self._n}"}
                self.profiles.append(existing)
                self.selected_id = self.selected_id or existing["id"]
            existing.update(name=extras["name"], type=u.scheme, host=u.hostname, port=u.port, user=u.username,
                            password=u.password, mode=extras.get("mode", "all"),
                            packages=extras["apps"].split(","), dns=extras.get("dns", "auto"),
                            blockQuic=flags.get("blockquic", False))
            # as the receiver's arg(): a blank extra is absent, so an omitted (or blank) tag
            # keeps the profile's old one; `note` is stored but `list` never returns it
            for key in ("tag", "note"):
                if (extras.get(key) or "").strip():
                    existing[key] = extras[key]
            if flags.get("select"):
                self.selected_id = existing["id"]
            return 0, {"saved": self.profile_json(existing), "updated": existing["id"] != f"id-new-{self._n}"}
        if cmd == "connect":
            try:
                p = self._find(extras["profile"]) if "profile" in extras else self.selected()
            except ValueError as e:
                return 1, {"error": str(e)}
            self.selected_id = p["id"]
            self.connects.append(p["name"])
            if self.consent:
                self.pending = p["id"]
                return 1, {"error": "VPN permission not granted yet: accept the consent dialog, then check status",
                           "consent": "requested"}
            self.active_id = p["id"]
            if self.connect_state == "error":
                return 1, {"error": 'dial "h:1": refused'}
            if self.connect_state == "error-later":       # connect answers, then the tunnel fails
                self.connect_state = "error"
                return 0, dict(self.status(), state="connecting")
            return 0, self.status()
        if cmd == "disconnect":
            self.active_id = None
            return 0, self.status()
        if cmd == "remove":
            p = self._find(extras["profile"])
            if self.active_id == p["id"]:
                self.active_id = None
            self.profiles.remove(p)
            if self.selected_id == p["id"]:
                self.selected_id = None
            return 0, {"removed": p["name"]}
        if cmd == "log":
            entries = [e for e in self.log if not flags.get("failed") or e["status"] != "ok"]
            return 0, entries[:int(extras.get("limit", 50))]
        return 1, {"error": f'unknown command "{cmd}"'}

    def accept(self):
        """The user tapped OK: ToggleActivity connects the selected profile."""
        self.consent = False
        self.active_id = self.pending

    def snapshot(self):
        """The user-visible state (what `proxy off` must restore)."""
        return ([dict(p) for p in self.profiles], self.selected()["id"] if self.selected() else None,
                self.active_id)

    # -- FakeAdb's shell callable
    def __call__(self, line):
        import shlex
        words = shlex.split(line)
        if words[:2] == ["am", "broadcast"]:
            self.broadcasts.append(line)
            if self.installed is None:
                return b"Broadcasting: Intent { }\nBroadcast completed: result=0\n", b"", 0
            extras, flags, i = {}, {}, 2
            while i < len(words):
                if words[i] in ("--es", "--ez", "--ei"):
                    key, val = words[i + 1], words[i + 2]
                    if words[i] == "--ez":
                        flags[key] = val == "true"
                    else:
                        extras[key] = val
                    i += 3
                else:
                    i += 2 if words[i] in ("-a", "-n") else 1
            code, data = self.receive(extras, flags)
            return broadcast_reply(code, data).encode(), b"", 0
        if words[:2] == ["pm", "path"]:
            ok = (words[2] == "com.proxyapp.mobile" and self.installed is not None) or words[2] in self.apps
            return (f"package:/data/app/{words[2]}-1/base.apk\n".encode() if ok else b""), b"", 0 if ok else 1
        if words[:2] == ["dumpsys", "package"]:
            return (f"Packages:\n  Package [{words[2]}]\n    versionCode={self.installed} minSdk=26\n".encode()
                    if self.installed is not None else b""), b"", 0
        if words[:2] == ["pm", "install"]:
            self.installs += 1
            self.installed = self.bundled
            self.active_id = None                 # replacing the app kills its running VPN
            return b"Success\n", b"", 0
        if words[0] == "rm":
            return b"", b"", 0
        raise AssertionError(f"unexpected device command: {line}")

    bundled = 1
