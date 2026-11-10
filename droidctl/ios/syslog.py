"""The iPhone's system log, live, over our own usbmux transport: no libimobiledevice.

lockdown (`lockdown.py`) -> StartService com.apple.syslog_relay -> a TLS stream of text lines. Verified on
iOS 18.7.8 (2026-10-21). The stream is live only (no history), so the daemon keeps a ring of
recent lines per iPhone (`droidctl logs` reads it); without the daemon `logs` listens briefly.

**While a syslog_relay stream is open, Xcode cannot connect to the iPhone** (measured
2026-10-21: xcodebuild waited 60 s on "Device is busy (Connecting to iPhone)", 2/2; ending the
lockdown session didn't help; after repeated such failures CoreDeviceService sat at 95% CPU
until restarted). With the stream closed for the start: 5.3 s, CoreDeviceService idle. So the
ring reads only while droidctl is in use (the daemon's `active` test), and it lets go whenever
droidctl itself needs Xcode (`xcode_busy`, a flag file any process can see: the runner's build
and start raise it). A single short read (2 s) left no trace (start 5.1 s after it).
"""
import contextlib
import os
import re
import socket
import ssl
import threading
import time

from droidctl.core import UserError
from droidctl.ios import lockdown

# "Oct 21 22:12:26 iPhone TestApp(UIKitCore)[1986] <Notice>: text"
LINE = re.compile(r"^(\w{3} [ \d]\d \d\d:\d\d:\d\d) (\S+) ([^\[\(]+?)(?:\(([^)]*)\))?\[(\d+)\] <(\w+)>: ?(.*)$")
# droidctl's levels (logcat's) -> the lowest iOS level they keep
LEVELS = ["Debug", "Info", "Notice", "Warning", "Error", "Fault"]
FROM_LOGCAT = {"V": "Debug", "D": "Debug", "I": "Info", "W": "Warning", "E": "Error", "F": "Fault"}


def parse(line):
    """A syslog line -> {time, proc, image, pid, level, msg, line}, or None. PURE."""
    m = LINE.match(line)
    if not m:
        return None
    return {"time": m.group(1), "proc": m.group(3).strip(), "image": m.group(4) or "", "pid": int(m.group(5)),
            "level": m.group(6), "msg": m.group(7), "line": line}


def keep(entry, proc=None, pids=(), level=None, tags=()):
    """Whether a parsed line passes `logs`' filters. PURE.
    proc/pids: the app's process name and pids; tags: a process or image name, or the mark text."""
    if entry is None:
        return False
    if proc is not None and entry["proc"] != proc and entry["pid"] not in pids:
        return False
    if level:
        want = LEVELS.index(FROM_LOGCAT.get(level, "Debug"))
        have = LEVELS.index(entry["level"]) if entry["level"] in LEVELS else 0
        if have < want:
            return False
    if tags:
        hit = any(t == entry["proc"] or t == entry["image"] or (t == "droidctl-mark" and
                                                              entry["msg"].startswith("droidctl-mark ")) for t in tags)
        if not hit:
            return False
    return True


def split(buf):
    """Complete lines out of the relay's bytes (NUL-separated records, newlines inside). PURE.
    Returns (lines, rest)."""
    buf = buf.replace(b"\x00", b"")
    *done, rest = buf.split(b"\n")
    return [d.decode("utf-8", "replace") for d in done if d], rest


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------
def listen(udid, seconds, stop=None):
    """The lines that arrive within `seconds` (a live stream has no history)."""
    s = lockdown.start_service(udid, "com.apple.syslog_relay")
    s.settimeout(0.25)
    out, rest = [], b""
    end = time.monotonic() + seconds
    try:
        while time.monotonic() < end and not (stop and stop()):
            try:
                chunk = s.recv(65536)
            except (socket.timeout, ssl.SSLWantReadError):
                continue
            if not chunk:
                break
            lines, rest = split(rest + chunk)
            out.extend(lines)
    finally:
        s.close()
    return out


# --------------------------------------------------------------------------
# sharing the iPhone with Xcode: flag files under ~/.droidctl/ios/<udid>/
# --------------------------------------------------------------------------
BUSY_STALE_S = 600.0        # a flag older than this was left by a crashed process


def _flag(udid, name):
    from droidctl import device as dev
    return os.path.join(dev.home(), "ios", udid, name)


def _fresh(path, age):
    try:
        return time.time() - os.path.getmtime(path) < age
    except OSError:
        return False


@contextlib.contextmanager
def xcode_busy(udid, wait=5.0):
    """Around anything that needs Xcode's connection to the iPhone (the runner's build, start):
    every log stream on this Mac lets go first, and stays off until the block ends."""
    path = _flag(udid, "xcode-busy")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(str(os.getpid()))
    try:
        reading = _flag(udid, "syslog-reading")
        end = time.monotonic() + wait
        while _fresh(reading, 3.0) and time.monotonic() < end:
            time.sleep(0.2)               # a ring in this or another process is still connected
        yield
    finally:
        with contextlib.suppress(OSError):
            os.remove(path)


def xcode_is_busy(udid):
    return _fresh(_flag(udid, "xcode-busy"), BUSY_STALE_S)


class Ring:
    """A background reader keeping the last `size` lines (the daemon's per-iPhone log history).
    It reads only while `active()` says droidctl is in use and nobody needs Xcode (see above)."""

    def __init__(self, udid, size=50000, active=lambda: True):   # bursts reach ~1,500 lines/s
        import collections
        self.udid, self.lines = udid, collections.deque(maxlen=size)
        self.active = active
        self.started = time.time()
        self.alive, self.error, self.reading = True, None, False
        self._stop = False
        threading.Thread(target=self._run, daemon=True, name=f"syslog-{udid}").start()

    def _may_read(self):
        return not self._stop and self.active() and not xcode_is_busy(self.udid)

    def _beat(self, on):
        path = _flag(self.udid, "syslog-reading")
        if on:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(str(os.getpid()))
        else:
            with contextlib.suppress(OSError):
                os.remove(path)
        self.reading = on

    def _run(self):
        backoff = 1.0
        while not self._stop:
            if not self._may_read():
                time.sleep(0.5)
                continue
            try:
                s = lockdown.start_service(self.udid, "com.apple.syslog_relay")
                s.settimeout(0.5)
                rest, backoff = b"", 1.0
                self.alive, self.error = True, None
                self._beat(True)
                last_beat = time.monotonic()
                try:
                    while self._may_read():
                        if time.monotonic() - last_beat > 1.0:
                            self._beat(True)
                            last_beat = time.monotonic()
                        try:
                            chunk = s.recv(65536)
                        except (socket.timeout, ssl.SSLWantReadError):
                            continue
                        if not chunk:
                            break
                        lines, rest = split(rest + chunk)
                        self.lines.extend(lines)
                finally:
                    s.close()
                    self._beat(False)
            except (UserError, OSError, ssl.SSLError) as e:
                self.alive, self.error = False, str(e)
                if not self._stop:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)

    def snapshot(self):
        return list(self.lines)

    def close(self):
        self._stop = True
