"""lockdown.py against a fake lockdownd (no TLS) and fake plist services, over FakeUsbmux.

The replies' shapes follow libimobiledevice/go-ios; what a real phone says is checked by the
iPhone e2e tests once measured."""
import socket
import struct
import threading

import pytest

from droidctl.core import UserError
from droidctl.ios import lockdown
from tests.fakes import FakeUsbmux

UDID = "00008101-00AAAAAAAAAAAAAA"
VALUES = {"UniqueDeviceID": UDID, "DeviceName": "Test iPhone", "ProductType": "iPhone13,2",
          "ProductVersion": "18.7.8", "BuildVersion": "22H", "DeviceClass": "iPhone"}
APPS = [{"CFBundleIdentifier": "dev.example.mine", "CFBundleName": "Mine", "CFBundleShortVersionString": "1.0",
         "CFBundleVersion": "7", "ApplicationType": "User", "Path": "/private/var/containers/Bundle/Application/X/Mine.app",
         "Entitlements": {"get-task-allow": True}},
        {"CFBundleIdentifier": "com.apple.mobilesafari", "CFBundleDisplayName": "Safari",
         "ApplicationType": "System", "Path": "/Applications/MobileSafari.app"}]


class FakePhone:
    """lockdownd plus named plist services; each StartService gets a fresh listening port."""

    def __init__(self, values=None, services=None, refuse=()):
        self.values = dict(VALUES if values is None else values)
        self.services = services or {}
        self.refuse = set(refuse)
        self.requests, self.ports = [], {}
        self.ld = self._listen(self._lockdown)

    def _listen(self, handler):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(8)

        def loop():
            while True:
                try:
                    conn, _ = s.accept()
                except OSError:
                    return
                threading.Thread(target=self._guard, args=(handler, conn), daemon=True).start()
        threading.Thread(target=loop, daemon=True).start()
        return s

    @staticmethod
    def _guard(handler, conn):
        with conn:
            try:
                handler(conn)
            except (OSError, struct.error, UserError):
                pass

    def _lockdown(self, conn):
        while True:
            msg = lockdown.recv(conn)
            req = msg["Request"]
            self.requests.append(req if req != "StartService" else f"StartService {msg['Service']}")
            if req == "StartSession":
                lockdown.send(conn, {"SessionID": "S1", "EnableSessionSSL": False})
            elif req == "GetValue":
                if msg.get("Domain") == "com.apple.security.mac.amfi":
                    lockdown.send(conn, {"Value": True})
                elif "Key" in msg:
                    v = self.values.get(msg["Key"])
                    lockdown.send(conn, {"Value": v} if v is not None else {"Error": "MissingValue"})
                else:
                    lockdown.send(conn, {"Value": self.values})
            elif req == "StartService":
                name = msg["Service"]
                if name in self.refuse or name not in self.services:
                    lockdown.send(conn, {"Error": "InvalidService"})
                    continue
                svc = self._listen(self.services[name])
                self.ports[svc.getsockname()[1]] = svc
                self.mux.phones[UDID][62000 + len(self.ports)] = svc.getsockname()[1]
                lockdown.send(conn, {"Port": 62000 + len(self.ports), "EnableServiceSSL": False})
            elif req == "StopSession":
                lockdown.send(conn, {})


def instproxy(sent):
    def handle(conn):
        msg = lockdown.recv(conn)
        sent.append(msg)
        if msg["Command"] == "Browse":
            kind = msg["ClientOptions"]["ApplicationType"]
            apps = APPS if kind == "Any" else [a for a in APPS if a["ApplicationType"] == "User"]
            lockdown.send(conn, {"Status": "BrowsingComplete" if not apps else "Browsing", "CurrentList": apps[:1]})
            lockdown.send(conn, {"Status": "Complete", "CurrentList": apps[1:]})
        elif msg["Command"] == "Lookup":
            found = {a["CFBundleIdentifier"]: a for a in APPS if a["CFBundleIdentifier"] in msg["ClientOptions"]["BundleIDs"]}
            lockdown.send(conn, {"LookupResult": found, "Status": "Complete"})
        elif msg["Command"] == "Uninstall":
            if msg["ApplicationIdentifier"] == "dev.example.missing":
                lockdown.send(conn, {"Error": "APIInternalError", "ErrorDescription": "no such app"})
                return
            lockdown.send(conn, {"Status": "RemovingApplication", "PercentComplete": 50})
            lockdown.send(conn, {"Status": "Complete"})
    return handle


def mounter(mounted):
    def handle(conn):
        msg = lockdown.recv(conn)
        if msg["Command"] == "LookupImage":
            lockdown.send(conn, {"ImageSignature": [b"sig"] if mounted else [], "Status": "Complete"})
        else:
            lockdown.send(conn, {"DeveloperModeStatus": True, "Status": "Complete"})
    return handle


@pytest.fixture
def phone(monkeypatch):
    made = []

    def make(**kw):
        p = FakePhone(**kw)
        p.mux = FakeUsbmux({UDID: {lockdown.LOCKDOWN_PORT: p.ld.getsockname()[1]}})
        monkeypatch.setenv("USBMUXD_SOCKET_ADDRESS", p.mux.address)
        monkeypatch.setattr(lockdown, "pair_record", lambda u, timeout=5.0: {"HostID": "H", "SystemBUID": "B"})
        made.append(p)
        return p
    yield make
    for p in made:
        p.mux.close()
        p.ld.close()
        for s in p.ports.values():
            s.close()


def test_info_reads_the_root_domain_and_developer_mode(phone):
    p = phone()
    got = lockdown.info(UDID)
    assert got == {"udid": UDID, "name": "Test iPhone", "product": "iPhone13,2", "os": "18.7.8",
                   "build": "22H", "class": "iPhone", "developer_mode": True}
    assert p.requests == ["StartSession", "GetValue", "GetValue", "StopSession"]


def test_locked_reads_password_protected(phone):
    phone(values={**VALUES, "PasswordProtected": True})
    assert lockdown.locked(UDID) is True
    phone(values=VALUES)
    assert lockdown.locked(UDID) is False


def test_apps_browse_collects_every_batch_in_devices_shape(phone):
    sent = []
    phone(services={lockdown.INSTPROXY: instproxy(sent)})
    mine = lockdown.apps(UDID)
    assert sent[0]["ClientOptions"]["ApplicationType"] == "User"
    assert mine == [{"bundle": "dev.example.mine", "name": "Mine", "version": "1.0", "build": "7", "developer": True,
                     "system": False, "url": "file:///private/var/containers/Bundle/Application/X/Mine.app/"}]
    every = lockdown.apps(UDID, all_apps=True)
    assert [a["bundle"] for a in every] == ["dev.example.mine", "com.apple.mobilesafari"]
    assert every[1]["system"] and every[1]["name"] == "Safari"


def test_app_looks_one_bundle_up_or_says_none(phone):
    phone(services={lockdown.INSTPROXY: instproxy([])})
    assert lockdown.app(UDID, "com.apple.mobilesafari")["name"] == "Safari"
    assert lockdown.app(UDID, "dev.example.absent") is None


def test_uninstall_waits_for_complete_and_types_an_error(phone):
    sent = []
    phone(services={lockdown.INSTPROXY: instproxy(sent)})
    lockdown.uninstall(UDID, "dev.example.mine")
    assert sent[-1] == {"Command": "Uninstall", "ApplicationIdentifier": "dev.example.mine"}
    with pytest.raises(UserError) as e:
        lockdown.uninstall(UDID, "dev.example.missing")
    assert e.value.kind == "device" and "no such app" in str(e.value)


@pytest.mark.parametrize("mounted", [True, False])
def test_the_developer_image_lookup(phone, mounted):
    phone(services={lockdown.IMAGE_MOUNTER: mounter(mounted)})
    assert lockdown.developer_image_mounted(UDID) is mounted
    assert lockdown.developer_mode(UDID) is True


def test_a_refused_service_says_what_to_do(phone):
    phone()
    with pytest.raises(UserError) as e:
        lockdown.start_service(UDID, "com.apple.instruments.remoteserver")
    assert "disk image" in e.value.hint
