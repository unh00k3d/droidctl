"""A usbmuxd client: list attached iPhones and open a TCP connection to a port on one.

usbmuxd is the USB multiplexer macOS (and Linux, via the usbmuxd package) already
runs. Its protocol is the plist variant: a 16-byte little-endian header
(length including the header, version 1, message type 8 = plist, tag) and an XML
plist body. After a successful `Connect` the same socket becomes a raw byte pipe
to the device port, so the runner is reached without iproxy (IOS.md §4).

Written from the protocol as libimobiledevice documents it; no code is copied.
"""
import os
import plistlib
import socket
import struct

from droidctl.core import UserError

DEFAULT_SOCKET = "/var/run/usbmuxd"
HEADER = struct.Struct("<IIII")
VERSION = 1
PLIST = 8
# Result.Number values
OK, BAD_COMMAND, BAD_DEVICE, REFUSED, BAD_VERSION = 0, 1, 2, 3, 6
RESULTS = {BAD_COMMAND: "bad command", BAD_DEVICE: "no such device", REFUSED: "connection refused",
           BAD_VERSION: "bad protocol version"}
ID = {"ClientVersionString": "droidctl", "ProgName": "droidctl", "kLibUSBMuxVersion": 3}


def socket_address():
    """$USBMUXD_SOCKET_ADDRESS (libimobiledevice's variable: "UNIX:/path" or "host:port"),
    else the system socket."""
    addr = os.environ.get("USBMUXD_SOCKET_ADDRESS", "")
    if addr.startswith("UNIX:"):
        return addr[5:]
    if addr and ":" in addr:
        host, port = addr.rsplit(":", 1)
        return (host, int(port))
    return DEFAULT_SOCKET


def _open(timeout):
    addr = socket_address()
    try:
        if isinstance(addr, tuple):
            s = socket.create_connection(addr, timeout=timeout)
        else:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect(addr)
    except OSError as e:
        raise UserError(f"cannot reach usbmuxd at {addr}: {e}", "connection",
                        hint="usbmuxd is part of macOS; on Linux install the usbmuxd package")
    return s


def _recv_exact(s, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise UserError("usbmuxd closed the connection", "connection")
        buf += chunk
    return bytes(buf)


class Mux:
    """One connection to usbmuxd. Each request gets a fresh tag."""

    def __init__(self, timeout=5.0):
        self.sock = _open(timeout)
        self.tag = 0

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def send(self, message, **fields):
        self.tag += 1
        body = plistlib.dumps(dict(ID, MessageType=message, **fields))
        self.sock.sendall(HEADER.pack(HEADER.size + len(body), VERSION, PLIST, self.tag) + body)
        return self.tag

    def recv(self):
        length, version, mtype, tag = HEADER.unpack(_recv_exact(self.sock, HEADER.size))
        if mtype != PLIST or length < HEADER.size:
            raise UserError(f"usbmuxd sent an unexpected message (type {mtype})", "connection")
        return plistlib.loads(_recv_exact(self.sock, length - HEADER.size))

    def request(self, message, **fields):
        self.send(message, **fields)
        return self.recv()


def _check(reply, what):
    if reply.get("MessageType") == "Result" and reply.get("Number", OK) != OK:
        n = reply["Number"]
        raise UserError(f"usbmuxd {what}: {RESULTS.get(n, f'error {n}')}",
                        "no-device" if n == BAD_DEVICE else "connection", data={"usbmux_result": n})
    return reply


def list_devices(timeout=5.0):
    """iPhones attached over USB: [{udid, device_id, product_id}], in usbmuxd's order.
    Network (Wi-Fi) entries are left out: droidctl drives iPhones over USB only."""
    with Mux(timeout) as m:
        reply = _check(m.request("ListDevices"), "ListDevices")
    out = []
    for d in reply.get("DeviceList", []):
        p = d.get("Properties", {})
        if p.get("ConnectionType", "USB") != "USB":
            continue
        out.append({"udid": p.get("SerialNumber", ""), "device_id": d.get("DeviceID", p.get("DeviceID")),
                    "product_id": p.get("ProductID")})
    return out


def device_id(udid, timeout=5.0):
    """usbmuxd's numeric DeviceID for a hardware UDID."""
    for d in list_devices(timeout):
        if d["udid"] == udid:
            return d["device_id"]
    raise UserError(f"iPhone {udid} is not attached over USB", "no-device",
                    hint="connect it with a cable and unlock it; run: droidctl devices")


def is_paired(udid, timeout=5.0):
    """True when this Mac holds a pair record for the phone (the user tapped Trust)."""
    with Mux(timeout) as m:
        reply = m.request("ReadPairRecord", PairRecordID=udid)
    return "PairRecordData" in reply


def connect(udid, port, timeout=5.0):
    """A TCP socket to `port` on the phone (the runner listens on its loopback)."""
    did = device_id(udid, timeout)
    m = Mux(timeout)
    try:
        _check(m.request("Connect", DeviceID=did, PortNumber=socket.htons(port)), f"connect to port {port}")
    except UserError:
        m.close()
        raise
    s = m.sock
    s.settimeout(timeout)
    return s
