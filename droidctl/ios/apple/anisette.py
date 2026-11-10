"""Anisette: the one-time machine headers (X-Apple-I-MD…) Apple's login and developer services
expect alongside every request.

- **macOS:** the Mac's own, from the private AOSKit/AuthKit frameworks (as AltServer does).
  Measured 2026-11-09 on macOS 15.7.9: an unsigned python3 gets them, no entitlement needed.
- **Linux (or `--anisette URL` anywhere):** an anisette-v3 server: SideStore's public one, offered
  as the default in a terminal (what SideStore and xtool do), or the user's own (`--anisette URL`).
  Since 2026-10-12 Apple refuses (-80045) data from the older anisette-v3-server v2.2.2.
  The server runs Apple's ADI code; the machine identity (a random UID and the `adi.pb` Apple
  provisions) stays with droidctl, in the keyring, so it survives server rebuilds. Ported from
  xtool's OmnisetteADIProvider/ADIDataProvider (MIT); protocol in research/ios-anisette-v3.md.
  The server never sees the Apple ID or the password.
"""
import base64
import hashlib
import json
import os
import plistlib
import socket
import ssl
import urllib.parse
import uuid
import ctypes
import ctypes.util
import datetime
import sys
import time

from droidctl.core import UserError

AUTHKIT_CLIENT = "com.apple.AuthKit/1 (com.apple.akd/1.0)"
RINFO = "17106176"                      # the routing info every client sends (AltServer, xtool)
REFRESH_S = 60.0                        # the OTP is good for about 90 s; take a fresh one after 60


class _ObjC:
    """Just enough of the Objective-C runtime for a few message sends that return objects/strings."""

    def __init__(self):
        self.lib = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Foundation.framework/Foundation")
        for fw in ("AOSKit", "AuthKit"):
            ctypes.cdll.LoadLibrary(f"/System/Library/PrivateFrameworks/{fw}.framework/{fw}")
        self.lib.objc_getClass.restype = ctypes.c_void_p
        self.lib.objc_getClass.argtypes = [ctypes.c_char_p]
        self.lib.sel_registerName.restype = ctypes.c_void_p
        self.lib.sel_registerName.argtypes = [ctypes.c_char_p]

    def cls(self, name):
        c = self.lib.objc_getClass(name.encode())
        if not c:
            raise UserError(f"macOS has no {name} class (AOSKit/AuthKit changed?)", "unsupported")
        return c

    def send(self, obj, sel, *args, restype=ctypes.c_void_p):
        f = self.lib.objc_msgSend
        f.restype = restype
        f.argtypes = [ctypes.c_void_p, ctypes.c_void_p] + [ctypes.c_void_p] * len(args)
        return f(obj, self.lib.sel_registerName(sel.encode()), *args)

    def nsstring(self, s):
        f = self.lib.objc_msgSend
        f.restype, f.argtypes = ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p]
        return f(self.cls("NSString"), self.lib.sel_registerName(b"stringWithUTF8String:"), s.encode())

    def text(self, obj):
        if not obj:
            return None
        raw = self.send(obj, "UTF8String", restype=ctypes.c_char_p)
        return raw.decode() if raw else None


class MacAnisette:
    """The Mac's own anisette, from AOSKit (OTP) and AuthKit's AKDevice (identity)."""

    def __init__(self):
        if sys.platform != "darwin":
            raise UserError("AOSKit anisette exists only on macOS", "unsupported")
        self.o = _ObjC()
        self._cache, self._at = None, 0.0

    def _fetch(self):
        o = self.o
        otp = o.send(o.cls("AOSUtilities"), "retrieveOTPHeadersForDSID:", o.nsstring("-2"))
        if not otp:
            raise UserError("macOS gave no anisette data", "device", hint="sign in to iCloud or retry")

        def otp_value(key):
            return o.text(o.send(otp, "valueForKey:", o.nsstring(key)))
        dev = o.send(o.cls("AKDevice"), "currentDevice")
        locale = o.text(o.send(o.send(dev, "locale"), "localeIdentifier")) or "en_US"
        return {
            "X-Apple-I-MD": otp_value("X-Apple-MD"),
            "X-Apple-I-MD-M": otp_value("X-Apple-MD-M"),
            "X-Apple-I-MD-LU": o.text(o.send(dev, "localUserUUID")),
            "X-Apple-I-MD-RINFO": RINFO,
            "X-Apple-I-SRL-NO": o.text(o.send(o.cls("AOSUtilities"), "machineSerialNumber")) or "0",
            "X-Mme-Device-Id": o.text(o.send(dev, "uniqueDeviceIdentifier")),
            "X-Mme-Client-Info": o.text(o.send(dev, "serverFriendlyDescription")),
            "X-Apple-Locale": locale,
        }

    def base(self):
        if self._cache is None or time.monotonic() - self._at > REFRESH_S:
            self._cache, self._at = self._fetch(), time.monotonic()
        now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        return dict(self._cache, **{"X-Apple-I-Client-Time": now.isoformat().replace("+00:00", "Z"),
                                    "X-Apple-I-TimeZone": time.strftime("%Z") or "UTC"})


def client_info_for_authkit(info):
    """Xcode's requests name AuthKit's daemon as the client: swap the third <…> of the
    serverFriendlyDescription ("<model> <os;ver;build> <com.apple.AuthKit/1 (…)>"). PURE."""
    parts = info.split("<")
    if len(parts) < 4:
        return info
    rest = parts[3].split(">", 1)
    return "<".join(parts[:3] + [AUTHKIT_CLIENT + ">" + (rest[1] if len(rest) > 1 else "")])


def headers(provider, cpd=False, client_info=False, app_info=False):
    """The anisette headers in the three flavours the requests need (PlumeImpactor's
    generate_headers): cpd = the login body's extras; client_info = AuthKit's client string;
    app_info = Xcode's app headers. PURE given the provider."""
    h = dict(provider.base())
    info = h.pop("X-Mme-Client-Info", None)
    if client_info and info:
        h["X-Mme-Client-Info"] = client_info_for_authkit(info)
    if app_info:
        h["X-Apple-App-Info"] = "com.apple.gs.xcode.auth"
        h["X-Xcode-Version"] = "11.2 (11B41)"
    if cpd:
        h.update({"bootstrap": "true", "icscrec": "true", "loc": h.get("X-Apple-Locale", "en_US"), "pbe": "false",
                  "prkgen": "true", "svct": "iCloud"})
    return h


def client_info(provider):
    """The raw X-Mme-Client-Info (the login's own header)."""
    return provider.base().get("X-Mme-Client-Info", "")


# --------------------------------------------------------------------------
# anisette v3 (a user-run server)
# --------------------------------------------------------------------------
LOOKUP = "https://gsa.apple.com/grandslam/GsService2/lookup"
# SideStore's public anisette-v3 server: the default offered on Linux, as SideStore and xtool do.
# Since 2026-10-12 Apple refuses the first sign-in step (-80045) with data from the older
# anisette-v3-server (v2.2.2, Android's CoreADI, still at ani.sidestore.io); the "native-c"
# servers below are the ones other sideloaders moved to (checked 2026-11-10: /v3/client_info).
PUBLIC = "https://new.ani.sidestore.io"
ALTERNATE = "https://ani.jkcoxson.com"
RETIRED = {"https://ani.sidestore.io": PUBLIC}
STATE = "anisette-v3"                       # the keyring entry: {server, uid, adi_pb}
SERVER_HINT = (f"use the public one (--anisette {PUBLIC}, or {ALTERNATE}) or run your own anisette-v3 "
               "server and pass --anisette http://localhost:6969")
REJECTED = -80045                           # GrandSlam: this anisette data/client isn't accepted


def current(url):
    """A server URL with retired public servers replaced by their successor (a saved sign-in may
    still name one). PURE."""
    if not url:
        return url
    return RETIRED.get(url.rstrip("/"), url)


def user_agent(provider, default):
    """The User-Agent for GrandSlam: the server's own (it must match its client info), else `default`."""
    info = getattr(provider, "client_info", None)
    return (info().get("user_agent") if callable(info) else None) or default
EXPLAIN = ("Apple's sign-in wants machine data (\"anisette\") that only Apple's own code makes, and Linux has none. "
           f"droidctl gets it from an anisette server: by default SideStore's public one ({PUBLIC}). The server "
           "sees a machine identity droidctl keeps for you and when you sign in; never your Apple ID, password or "
           "token, which go only to Apple.")


def choose_server(ask, say):
    """On Linux, in a terminal: the public server unless the user says no or types their own URL."""
    say(EXPLAIN)
    got = (ask(f"Use the public anisette server? [Y/n, or your own server's URL] ") or "").strip()
    if not got or got.lower() in ("y", "yes"):
        return PUBLIC
    if "://" in got:
        return got
    say(f"Another public one: {ALTERNATE}; or run your own anisette-v3 server.")
    return (ask("your anisette server [http://localhost:6969]: ") or "").strip() or "http://localhost:6969"


def ids(uid):
    """(identifier for the server, X-Mme-Device-Id, X-Apple-I-MD-LU) from the persisted UID. PURE."""
    u = uuid.UUID(uid)
    device_id = str(u).upper()
    return (base64.b64encode(u.bytes).decode(), device_id,
            hashlib.sha256(device_id.encode()).hexdigest().upper())


def _locale():
    import locale
    loc = (locale.getlocale()[0] or "en_US").split(".")[0]
    return loc if "_" in loc else "en_US"


class V3Anisette:
    def __init__(self, url, store=None):
        self.url = current(url).rstrip("/")
        self.store = store                  # (load, save) for tests; else the keyring
        self._info = None
        self._cache, self._at = None, 0.0

    # -- state
    def _load(self):
        if self.store:
            return self.store[0]()
        from droidctl.ios.apple import secrets
        return secrets.load(STATE)

    def _save(self, st):
        if self.store:
            return self.store[1](st)
        from droidctl.ios.apple import secrets
        secrets.save(STATE, st)

    # -- the server
    def _http(self, path, body=None):
        from droidctl.ios.apple import request
        status, data = request(self.url + path, None if body is None else json.dumps(body).encode(),
                               {"Content-Type": "application/json"} if body is not None else {},
                               method="POST" if body is not None else "GET", timeout=20)
        if status != 200:
            raise UserError(f"the anisette server answered HTTP {status} ({self.url}{path})", "connection",
                            hint=SERVER_HINT)
        try:
            return json.loads(data)
        except ValueError:
            raise UserError(f"the anisette server at {self.url} isn't an anisette-v3 server", "connection",
                            hint=SERVER_HINT) from None

    def client_info(self):
        if self._info is None:
            try:
                self._info = self._http("/v3/client_info")
            except UserError as e:
                raise UserError(f"can't reach the anisette server at {self.url}: {e}", "connection",
                                hint=SERVER_HINT) from None
        return self._info

    # -- Apple's provisioning of our machine identity (once)
    def _apple(self, url, body, uid):
        from droidctl.ios.apple import request
        _, device_id, lu = ids(uid)
        h = {"Content-Type": "text/x-xml-plist", "Accept-Language": "en_US", "Accept": "*/*",
             "X-Apple-I-MD-LU": lu, "X-Mme-Device-Id": device_id, "X-MMe-Country": _locale().split("_")[-1],
             "X-MMe-Client-Info": self.client_info()["client_info"], "User-Agent": self.client_info()["user_agent"]}
        status, data = request(url, None if body is None else plistlib.dumps(body), h,
                               method="POST" if body is not None else "GET", timeout=20)
        try:
            doc = plistlib.loads(data)
        except Exception:
            raise UserError(f"Apple's provisioning answered HTTP {status} with something unreadable", "signing",
                            data={"reason": "anisette-provisioning"}) from None
        st = ((doc.get("Response") or {}).get("Status") or {}) if isinstance(doc, dict) else {}
        if st.get("ec"):
            raise UserError(f"Apple refused the machine provisioning: {st.get('em')} ({st.get('ec')})", "signing",
                            data={"reason": "anisette-provisioning", "code": st.get("ec")})
        return doc

    def provision(self, uid):
        """Provision a machine identity: Apple's start, then the server's ADI over a WebSocket,
        Apple's finish inside the server's 3 s step window. Returns adi.pb (bytes)."""
        urls = self._apple(LOOKUP, None, uid).get("urls") or {}
        start, finish = urls.get("midStartProvisioning"), urls.get("midFinishProvisioning")
        if not (start and finish):
            raise UserError("Apple's lookup gave no provisioning URLs", "signing")
        spim = self._apple(start, {"Header": {}, "Request": {}}, uid)["Response"]["spim"]
        ws = WebSocket(self.url + "/v3/provisioning_session")
        try:
            ws.expect("GiveIdentifier")
            ws.send({"identifier": ids(uid)[0]})
            ws.expect("GiveStartProvisioningData")
            ws.send({"spim": spim})
            cpim = ws.expect("GiveEndProvisioningData")["cpim"]
            r = self._apple(finish, {"Header": {}, "Request": {"cpim": cpim}}, uid)["Response"]
            ws.send({"ptm": r["ptm"], "tk": r["tk"]})
            adi = ws.expect("ProvisioningSuccess")["adi_pb"]
        finally:
            ws.close()
        return adi

    def _fetch(self):
        st = self._load() or {}
        if not st.get("uid") or st.get("server") != self.url:
            # a machine identity belongs to the server that provisioned it: a new server, a new one
            st = {"server": self.url, "uid": str(uuid.uuid4()).upper()}
        if not st.get("adi_pb"):
            st["adi_pb"] = self.provision(st["uid"])
            self._save(st)
        ident, device_id, lu = ids(st["uid"])
        r = self._http("/v3/get_headers", {"identifier": ident, "adi_pb": st["adi_pb"]})
        if r.get("result") != "Headers":
            if "adi" in str(r.get("message", "")).lower() or "provision" in str(r.get("message", "")).lower():
                st.pop("adi_pb", None)          # the server can't use it: provision again next time
                self._save(st)
            raise UserError(f"the anisette server failed: {r.get('message') or r.get('result')}", "signing",
                            hint="retry; if it keeps failing, restart the anisette server",
                            data={"reason": "anisette"})
        return {"X-Apple-I-MD": r["X-Apple-I-MD"], "X-Apple-I-MD-M": r["X-Apple-I-MD-M"],
                "X-Apple-I-MD-RINFO": str(r.get("X-Apple-I-MD-RINFO") or RINFO), "X-Apple-I-MD-LU": lu,
                "X-Apple-I-SRL-NO": "0", "X-Mme-Device-Id": device_id,
                "X-Mme-Client-Info": self.client_info()["client_info"], "X-Apple-Locale": _locale()}

    def base(self):
        if self._cache is None or time.monotonic() - self._at > REFRESH_S:
            self._cache, self._at = self._fetch(), time.monotonic()
        now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        return dict(self._cache, **{"X-Apple-I-Client-Time": now.isoformat().replace("+00:00", "Z"),
                                    "X-Apple-I-TimeZone": time.strftime("%Z") or "UTC"})


class WebSocket:
    """Just enough RFC 6455 for the provisioning session: masked text frames out, unfragmented
    frames in (the server sends text; it rejects binary and fragmented messages)."""

    def __init__(self, url, timeout=20.0):
        u = urllib.parse.urlsplit(url)
        secure = u.scheme in ("https", "wss")
        port = u.port or (443 if secure else 80)
        sock = socket.create_connection((u.hostname, port), timeout=timeout)
        if secure:
            from droidctl.ios.apple import tls_context
            sock = tls_context().wrap_socket(sock, server_hostname=u.hostname)
        self.sock, self.buf = sock, b""
        key = base64.b64encode(os.urandom(16)).decode()
        host = u.hostname + (f":{u.port}" if u.port else "")
        sock.sendall((f"GET {u.path or '/'} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        while b"\r\n\r\n" not in self.buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise UserError("the anisette server closed the provisioning session", "connection")
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise UserError("the anisette server refused the provisioning session", "connection",
                            hint=SERVER_HINT)

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise UserError("the anisette server closed the provisioning session", "connection")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self):
        while True:
            b0, b1 = self._read(2)
            n = b1 & 0x7F
            if n == 126:
                n = int.from_bytes(self._read(2), "big")
            elif n == 127:
                n = int.from_bytes(self._read(8), "big")
            mask = self._read(4) if b1 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
            op = b0 & 0x0F
            if op == 8:
                raise UserError("the anisette server ended the provisioning session", "connection")
            if op in (1, 2):
                return json.loads(data)
            # ping/pong/continuation: ignore

    def send(self, obj):
        data = json.dumps(obj).encode()
        n = len(data)
        head = bytes([0x81])
        head += bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + n.to_bytes(2, "big") if n < 65536 \
            else bytes([0x80 | 127]) + n.to_bytes(8, "big")
        mask = os.urandom(4)
        self.sock.sendall(head + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(data)))

    def expect(self, result):
        msg = self.recv()
        if msg.get("result") != result:
            raise UserError(f"the anisette server's provisioning failed: {msg.get('result')}"
                            + (f" ({msg['message']})" if msg.get("message") else ""), "signing",
                            hint="retry (each step must finish within the server's 3 s)",
                            data={"reason": "anisette-provisioning"})
        return msg

    def close(self):
        try:
            self.sock.sendall(bytes([0x88, 0x80]) + os.urandom(4))
        except OSError:
            pass
        self.sock.close()


def default_provider(url=None):
    url = current(url or os.environ.get("DROIDCTL_ANISETTE"))
    if url:
        return V3Anisette(url)
    if sys.platform == "darwin":
        return MacAnisette()
    raise UserError("signing in to Apple from Linux needs an anisette server", "signing",
                    hint=SERVER_HINT, data={"reason": "no-anisette"})
