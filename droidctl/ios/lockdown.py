"""lockdownd and the plain services behind it, over our own usbmux transport: no devicectl, no
Xcode, no libimobiledevice. Used on macOS and Linux alike (`research/ios-linux.md` §4).

lockdownd (port 62078) -> StartSession with the host's pair record -> TLS with its host
certificate -> GetValue / StartService. A service is a second usbmux connection to the port
lockdownd hands out, TLS when it says so. Most services speak length-prefixed XML plists.

Verified on iOS 18.7.8 (2026-10-21): the session, TLS and syslog_relay, the AMFI reveal. The
rest (values, installation_proxy, image mounter) follows the protocol as libimobiledevice and
go-ios implement it and is UNVERIFIED on a phone until measured.
"""
import os
import plistlib
import ssl
import struct
import tempfile
import warnings

from droidctl.core import UserError
from droidctl.ios import transport

LOCKDOWN_PORT = 62078
LABEL = "droidctl"
INSTPROXY = "com.apple.mobile.installation_proxy"
IMAGE_MOUNTER = "com.apple.mobile.mobile_image_mounter"
AMFI = "com.apple.amfi.lockdown"
APP_ATTRS = ["CFBundleIdentifier", "CFBundleDisplayName", "CFBundleName", "CFBundleShortVersionString",
             "CFBundleVersion", "ApplicationType", "Path", "Entitlements", "IsUpgradeable"]


# --------------------------------------------------------------------------
# framing and the handshake
# --------------------------------------------------------------------------
def send(s, obj):
    b = plistlib.dumps(obj)
    s.sendall(struct.pack(">I", len(b)) + b)


def recv(s):
    n = struct.unpack(">I", transport._recv_exact(s, 4))[0]
    return plistlib.loads(transport._recv_exact(s, n))


def pair_record(udid, timeout=5.0):
    with transport.Mux(timeout) as m:
        reply = m.request("ReadPairRecord", PairRecordID=udid)
    if "PairRecordData" not in reply:
        raise UserError("this computer is not paired with the iPhone", "no-device",
                        hint="unlock the iPhone and tap Trust on \"Trust This Computer?\"")
    return plistlib.loads(reply["PairRecordData"])


def tls(sock, rec):
    """TLS as lockdown speaks it: our host certificate, no server verification (the pair record
    is the trust). The key is written to a private temp dir only for the load and removed."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_ciphers("ALL:!aNULL:!eNULL:@SECLEVEL=0")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        ctx.minimum_version = ssl.TLSVersion.TLSv1
    with tempfile.TemporaryDirectory() as d:
        cert, key = os.path.join(d, "cert.pem"), os.path.join(d, "key.pem")
        with open(cert, "wb") as f:
            f.write(rec["HostCertificate"])
        fd = os.open(key, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(rec["HostPrivateKey"])
        ctx.load_cert_chain(cert, key)
    return ctx.wrap_socket(sock)


class Session:
    """One lockdown session: `with Session(udid) as ld: ld.value("ProductVersion")`."""

    def __init__(self, udid, timeout=5.0):
        self.udid, self.timeout = udid, timeout
        self.rec = pair_record(udid, timeout)
        self.s = transport.connect(udid, LOCKDOWN_PORT, timeout=timeout)
        try:
            r = self.request("StartSession", HostID=self.rec["HostID"], SystemBUID=self.rec["SystemBUID"])
            if "Error" in r:
                raise UserError(f"lockdown refused the session: {r['Error']}", "device",
                                hint="unplug and reconnect the iPhone, unlock it and tap Trust if asked")
            self.sid = r.get("SessionID")
            if r.get("EnableSessionSSL"):
                self.s = tls(self.s, self.rec)
        except BaseException:
            self.s.close()
            raise

    def request(self, name, **kw):
        send(self.s, {"Label": LABEL, "Request": name, **kw})
        return recv(self.s)

    def value(self, key=None, domain=None):
        """GetValue: one key, or every value of a domain when key is None. None when absent."""
        kw = {k: v for k, v in (("Key", key), ("Domain", domain)) if v is not None}
        r = self.request("GetValue", **kw)
        return None if "Error" in r else r.get("Value")

    def service(self, name):
        """A connected socket (TLS if the phone asks) to a lockdown service."""
        r = self.request("StartService", Service=name)
        if "Error" in r or "Port" not in r:
            raise UserError(f"lockdown could not start {name}: {r.get('Error')}", "device",
                            hint=_service_hint(r.get("Error")))
        sv = transport.connect(self.udid, r["Port"], timeout=self.timeout)
        return tls(sv, self.rec) if r.get("EnableServiceSSL") else sv

    def close(self):
        try:
            self.request("StopSession", SessionID=self.sid)
        except (OSError, ssl.SSLError, struct.error, UserError):
            pass
        self.s.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _service_hint(err):
    if err == "InvalidService":
        return "this service needs the developer disk image mounted: run droidctl setup"
    if err == "PasswordProtected":
        return "unlock the iPhone"
    return None


def start_service(udid, service, timeout=5.0):
    """A socket to one service; the lockdown session ends at once (the service runs on its own)."""
    with Session(udid, timeout) as ld:
        return ld.service(service)


def call(udid, service, msg, timeout=5.0):
    """One request/reply on a plist service."""
    s = start_service(udid, service, timeout)
    try:
        send(s, msg)
        return recv(s)
    finally:
        s.close()


# --------------------------------------------------------------------------
# the device
# --------------------------------------------------------------------------
def parse_info(values, udid=None):
    """GetValue's root domain -> the fields devices.py reports. PURE."""
    v = values or {}
    return {"udid": v.get("UniqueDeviceID") or udid, "name": v.get("DeviceName"), "product": v.get("ProductType"),
            "os": v.get("ProductVersion"), "build": v.get("BuildVersion"), "class": v.get("DeviceClass")}


def info(udid, timeout=5.0):
    with Session(udid, timeout) as ld:
        out = parse_info(ld.value(), udid)
        dm = ld.value("DeveloperModeStatus", "com.apple.security.mac.amfi")
    out["developer_mode"] = None if dm is None else bool(dm)
    return out


def locked(udid, timeout=5.0):
    """Whether the phone is locked now: lockdown's PasswordProtected (go-ios reads the same key).
    UNVERIFIED that it tracks the lock state rather than "a passcode is set"; measure it."""
    with Session(udid, timeout) as ld:
        return bool(ld.value("PasswordProtected"))


def reveal_developer_mode(udid):
    """Make Settings > Privacy & Security show the Developer Mode switch (AMFI action 0 = reveal;
    it turns nothing on)."""
    call(udid, AMFI, {"action": 0})


# --------------------------------------------------------------------------
# apps: installation_proxy
# --------------------------------------------------------------------------
def parse_app(a):
    """An installation_proxy entry -> the same fields as devices.parse_apps. PURE."""
    path = a.get("Path") or ""
    ents = a.get("Entitlements") or {}
    kind = a.get("ApplicationType")
    return {"bundle": a.get("CFBundleIdentifier"), "name": a.get("CFBundleDisplayName") or a.get("CFBundleName"),
            "version": a.get("CFBundleShortVersionString"), "build": a.get("CFBundleVersion"),
            "developer": bool(ents.get("get-task-allow")), "system": kind in ("System", "Internal"),
            "url": "file://" + path.rstrip("/") + "/" if path else None}


def _replies(s, done=("Complete",)):
    """The replies of one installation_proxy command until it finishes; raises on Error."""
    while True:
        r = recv(s)
        if "Error" in r:
            raise UserError(f"installation_proxy: {r['Error']}: {r.get('ErrorDescription', '')}".rstrip(": "),
                            "device")
        yield r
        if r.get("Status") in done:
            return


def apps(udid, all_apps=False, timeout=10.0):
    """Installed apps: the user's ones, or with all_apps the system ones too."""
    s = start_service(udid, INSTPROXY, timeout)
    try:
        send(s, {"Command": "Browse", "ClientOptions": {"ApplicationType": "Any" if all_apps else "User",
                                                         "ReturnAttributes": APP_ATTRS}})
        out = []
        for r in _replies(s):
            out.extend(parse_app(a) for a in r.get("CurrentList") or [])
        return out
    finally:
        s.close()


def app(udid, bundle, timeout=10.0):
    """The installed app with this bundle id (system apps included), or None."""
    s = start_service(udid, INSTPROXY, timeout)
    try:
        send(s, {"Command": "Lookup", "ClientOptions": {"BundleIDs": [bundle], "ReturnAttributes": APP_ATTRS}})
        for r in _replies(s):
            found = (r.get("LookupResult") or {}).get(bundle)
            if found:
                return parse_app(found)
        return None
    finally:
        s.close()


def uninstall(udid, bundle, timeout=60.0):
    s = start_service(udid, INSTPROXY, timeout)
    try:
        send(s, {"Command": "Uninstall", "ApplicationIdentifier": bundle})
        for _ in _replies(s):
            pass
    finally:
        s.close()


# --------------------------------------------------------------------------
# the developer disk image
# --------------------------------------------------------------------------
def developer_image_mounted(udid, timeout=5.0):
    """Whether a personalized developer disk image (iOS 17+) is mounted."""
    r = call(udid, IMAGE_MOUNTER, {"Command": "LookupImage", "ImageType": "Personalized"}, timeout)
    return bool(r.get("ImageSignature") or r.get("ImagePresent"))


def developer_mode(udid, timeout=5.0):
    """Developer Mode on (True/False), from the image mounter (needs no disk image)."""
    r = call(udid, IMAGE_MOUNTER, {"Command": "QueryDeveloperModeStatus"}, timeout)
    return bool(r.get("DeveloperModeStatus"))
