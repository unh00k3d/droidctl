"""The device link: adb selection, the agent socket, setup state and a11y settings.

Import cost matters here: the ping/act hot path (select the device, find the
forward, talk to the agent) is stdlib only. It speaks adb's tiny host protocol
directly for `host:devices` and `host:list-forward`, so `adbutils` (and the
requests/PIL it drags in, ~150 ms) loads only for setup-type work: shell,
install, creating or removing a forward.
"""
import collections
import json
import os
import re
import socket
import time

from droidctl.core import UserError

PKG = "dev.droidctl.agent"
SERVICE_CLASS = "dev.droidctl.agent.AgentService"
COMPONENT = f"{PKG}/.AgentService"            # the short form we write to settings
REMOTE = "localabstract:droidctl"
PROTOCOL = 3
# The versionCode of the APK bundled in droidctl/assets. It has to match the
# agent's build.gradle.kts (a unit test checks); setup compares it with what
# the phone reports so an unchanged agent is not reinstalled.
AGENT_VERSION_CODE = 5
APK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "droidctl-agent.apk")
DEVICE_TMP_APK = "/data/local/tmp/droidctl-agent.apk"


# --------------------------------------------------------------------------
# host state (~/.droidctl/state.json): per serial {port, version_code}
# --------------------------------------------------------------------------
def home():
    return os.environ.get("DROIDCTL_HOME") or os.path.expanduser("~/.droidctl")


def state_path():
    return os.path.join(home(), "state.json")


def load_state():
    try:
        with open(state_path()) as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        return {"devices": {}}
    data.setdefault("devices", {})
    return data


def save_state(data):
    """Write atomically: a crash mid-write must never leave half a JSON file."""
    os.makedirs(home(), mode=0o700, exist_ok=True)
    tmp = state_path() + f".{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, state_path())


def device_state(serial):
    return load_state()["devices"].get(serial, {})


def update_device_state(serial, **fields):
    data = load_state()
    data["devices"].setdefault(serial, {}).update(fields)
    save_state(data)


def clear_device_state(serial):
    data = load_state()
    if data["devices"].pop(serial, None) is not None:
        save_state(data)


# --------------------------------------------------------------------------
# enabled_accessibility_services (PURE): append ours, remove only ours
# --------------------------------------------------------------------------
def _canonical(entry):
    """`pkg/.Cls` and `pkg/pkg.Cls` name the same service; Android compares case-insensitively."""
    pkg, sep, cls = entry.strip().partition("/")
    if not sep:
        return entry.strip().lower()
    if cls.startswith("."):
        cls = pkg + cls
    return f"{pkg}/{cls}".lower()


def is_ours(entry):
    return _canonical(entry) == _canonical(COMPONENT)


def parse_services(value):
    """The colon-separated setting as a list. `settings get` prints "null" when unset."""
    if value is None or value.strip() in ("", "null"):
        return []
    return [e for e in value.strip().split(":") if e]


def join_services(entries):
    return ":".join(entries)


def add_service(entries):
    """Append ours if absent. Others keep their exact text and order. -> (entries, added)"""
    if any(is_ours(e) for e in entries):
        return list(entries), False
    return list(entries) + [COMPONENT], True


def remove_service(entries):
    """Drop only ours (either spelling). -> (entries, removed)"""
    kept = [e for e in entries if not is_ours(e)]
    return kept, len(kept) != len(entries)


# --------------------------------------------------------------------------
# adb host protocol (stdlib): devices and forwards without importing adbutils
# --------------------------------------------------------------------------
def _adb_host_query(service, timeout=3.0):
    port = int(os.environ.get("ANDROID_ADB_SERVER_PORT", "5037"))
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.sendall(f"{len(service):04x}{service}".encode())
            f = s.makefile("rb")
            status = f.read(4)
            size = f.read(4)
            body = f.read(int(size, 16)).decode() if len(size) == 4 else ""
    except OSError as e:
        raise UserError(f"cannot reach the adb server on port {port}: {e}", "adb",
                        hint="start it with: adb start-server")
    if status != b"OKAY":
        raise UserError(f"adb {service}: {body or status!r}", "adb")
    return body


def list_devices():
    """[(serial, state)] for everything adb sees, including unauthorized/offline."""
    out = []
    for line in _adb_host_query("host:devices").splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            out.append((parts[0], parts[1]))
    return out


def select_serial(requested, devices):
    """-d / ANDROID_SERIAL (passed in as `requested`), else the only ready device. PURE."""
    if requested:
        for serial, state in devices:
            if serial == requested:
                if state != "device":
                    raise UserError(f"device {serial} is {state}", "no-device",
                                    hint="accept the USB debugging prompt on the phone"
                                    if state == "unauthorized" else "")
                return serial
        raise UserError(f"device {requested} is not attached", "no-device",
                        hint="see: droidctl devices")
    ready = [s for s, st in devices if st == "device"]
    if len(ready) == 1:
        return ready[0]
    if not ready:
        others = ", ".join(f"{s} ({st})" for s, st in devices)
        raise UserError("no device attached" + (f"; not ready: {others}" if others else ""),
                        "no-device",
                        hint="accept the USB debugging prompt on the phone" if devices
                        else "connect a phone with USB debugging on")
    raise UserError(f"{len(ready)} devices attached: {', '.join(ready)}", "no-device",
                    hint="pick one with -d SERIAL or ANDROID_SERIAL")


def resolve_serial(requested):
    return select_serial(requested, list_devices())


def find_forward(serial, forwards_text=None):
    """The local tcp port of an existing `tcp:N localabstract:droidctl` forward, or None."""
    text = _adb_host_query("host:list-forward") if forwards_text is None else forwards_text
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == serial and parts[2] == REMOTE \
                and parts[1].startswith("tcp:"):
            return int(parts[1][4:])
    return None


# --------------------------------------------------------------------------
# adb via adbutils (lazy): shell, install, forward
# --------------------------------------------------------------------------
def adb_device(serial):
    import adbutils
    return adbutils.adb.device(serial)


def sh(d, cmd, timeout=30):
    """Run `adb shell` and return stripped stdout; adb failures become kind `adb`."""
    import adbutils
    try:
        return d.shell2(cmd, timeout=timeout).output.strip()
    except (adbutils.AdbError, adbutils.AdbTimeout, OSError) as e:
        raise UserError(f"adb shell {cmd!r}: {e}", "adb")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ensure_forward(serial):
    port = find_forward(serial)
    if port:
        return port
    import adbutils
    port = _free_port()
    try:
        adb_device(serial).forward(f"tcp:{port}", REMOTE)
    except (adbutils.AdbError, OSError) as e:
        raise UserError(f"adb forward failed: {e}", "adb")
    return port


def remove_forward(serial):
    port = find_forward(serial)
    if port:
        import adbutils
        try:
            adb_device(serial).forward_remove(f"tcp:{port}")
        except (adbutils.AdbError, OSError) as e:
            raise UserError(f"adb forward --remove failed: {e}", "adb")
    return port


def installed_version(d):
    """versionCode of the installed agent, or None if it is not installed."""
    if not sh(d, ["pm", "path", PKG]).startswith("package:"):
        return None
    m = re.search(r"versionCode=(\d+)", sh(d, ["dumpsys", "package", PKG]))
    return int(m.group(1)) if m else None


def get_services(d):
    return parse_services(sh(d, ["settings", "get", "secure", "enabled_accessibility_services"]))


def a11y_enabled(d):
    return sh(d, ["settings", "get", "secure", "accessibility_enabled"]) == "1"


# --------------------------------------------------------------------------
# the agent socket: NDJSON JSON-RPC 2.0 (stdlib only)
# --------------------------------------------------------------------------
# Device JSON-RPC error codes (PROTOCOL.md "Errors") -> (error.kind, message prefix)
DEVICE_ERRORS = {
    -32001: ("device", "the agent refused this connection"),
    -32002: ("stale-ref", "stale dump"),
    -32003: ("stale-ref", "gone"),
    -32004: ("disabled", "disabled"),
    -32005: ("offscreen", "not visible"),
    -32006: ("unsupported", "unsupported"),
    -32007: ("timeout", "timed out"),
    -32008: ("secure-window", "secure window"),
    -32009: ("device", "gesture cancelled"),
}


class AgentClient:
    """One persistent connection to the on-device agent, good for many calls.

    Pushed notifications (after `subscribe`) are queued while waiting for a reply
    and handed out by `notifications()`.
    """

    # Methods that only read device state, so repeating one is harmless. When the
    # service is unbound mid-request (an external UiAutomation client such as
    # `uiautomator dump` or Appium suppresses accessibility services; measured on
    # the SM-N950F: the socket closes ~0.7 s into a dump and the service is back
    # ~1.2 s later), these wait for the agent and are retried once. Actions never
    # are: they may already have been performed.
    IDEMPOTENT = frozenset({"ping", "echo", "gen", "tree", "events", "current",
                            "screenshot", "wait_idle", "wait_for"})
    REBIND_WAIT = 4.0

    def __init__(self, port, host="127.0.0.1", timeout=5.0):
        self.port, self.host = port, host
        self._id = 0
        self._notes = collections.deque(maxlen=5000)
        self._sock = None
        self._worked = False          # reconnect only after a working link was lost, so a
        self._open(timeout)           # phone without the agent still fails fast

    def _open(self, timeout):
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=timeout)
        except OSError as e:
            self._sock = None
            raise UserError(f"nothing listening on tcp:{self.port}: {e}", "connection")
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # our own line buffer, not makefile(): a buffered file is unusable after
        # one read timeout ("cannot read from timed out object"), and bounded
        # waits for pushed events time out by design
        self._buf = bytearray()

    def _drop(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    def _reconnect(self):
        """Wait for the agent to answer again on a fresh connection (adb accepts
        the connect even while nothing listens, so only a ping reply counts)."""
        self._drop()
        deadline = time.monotonic() + self.REBIND_WAIT
        last = None
        while time.monotonic() < deadline:
            try:
                self._open(1.0)
                self._request("ping", None, 1.0)
                return
            except UserError as e:
                last = e
                self._drop()
                time.sleep(0.1)
        raise UserError("the agent's service did not come back within "
                        f"{self.REBIND_WAIT:g}s ({last})", "connection",
                        hint="run: droidctl doctor (is Appium/uiautomator attached?)")

    def _readline(self):
        while True:
            i = self._buf.find(b"\n")
            if i >= 0:
                line = bytes(self._buf[:i + 1])
                del self._buf[:i + 1]
                return line
            chunk = self._sock.recv(65536)      # socket.timeout leaves _buf intact
            if not chunk:
                line = bytes(self._buf)
                self._buf.clear()
                return line
            self._buf += chunk

    def _read(self, method, timeout):
        self._sock.settimeout(timeout)
        line = self._readline()
        if not line:
            # adb accepts the forwarded connect even when nothing listens
            # on the device, then closes it: EOF here means no agent
            raise UserError("the agent closed the connection before replying "
                            "(service not running?)", "connection")
        try:
            return json.loads(line)
        except ValueError:
            raise UserError(f"the agent sent invalid JSON: {line[:200]!r}", "device")

    def call(self, method, params=None, timeout=10.0):
        if self._sock is None:                    # dropped by an earlier unbind
            self._reconnect()
        try:
            return self._request(method, params, timeout)
        except UserError as e:
            if e.kind != "connection":
                raise
            if not self._worked:
                raise
            if method not in self.IDEMPOTENT:
                self._drop()                      # the next call reconnects
                raise UserError(
                    f"{method}: the agent's service went away mid-request (an external UiAutomation "
                    "client such as uiautomator/Appium, or the service being disabled); "
                    "the action may or may not have been performed", "connection",
                    hint="run: droidctl snapshot to see the current state before repeating it",
                    data={"maybe_performed": True, "method": method})
        self._reconnect()
        return self._request(method, params, timeout)

    def _request(self, method, params, timeout):
        self._id += 1
        req = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            req["params"] = params
        try:
            self._sock.settimeout(timeout)
            self._sock.sendall(json.dumps(req, separators=(",", ":")).encode() + b"\n")
            while True:
                msg = self._read(method, timeout)
                if "id" not in msg:
                    self._notes.append(msg)       # a pushed event; keep it for notifications()
                    continue
                if msg["id"] is not None and msg["id"] != self._id:
                    continue                      # a late reply to an older request
                self._worked = True
                if "error" in msg:
                    raise error_from(method, msg["error"] or {})
                return msg.get("result")
        except socket.timeout:
            raise UserError(f"{method}: no reply within {timeout:g}s", "timeout")
        except (ConnectionError, OSError) as e:
            raise UserError(f"{method}: connection lost: {e}", "connection")

    def notifications(self, timeout=1.0):
        """Yield pushed notification params until `timeout` passes with none."""
        if self._sock is None:
            raise UserError("connection lost (the agent's service was unbound)", "connection")
        while True:
            while self._notes:
                yield self._notes.popleft().get("params")
            try:
                msg = self._read("notification", timeout)
            except socket.timeout:
                return
            except (ConnectionError, OSError) as e:
                raise UserError(f"connection lost: {e}", "connection")
            if "id" not in msg:
                yield msg.get("params")

    # -- typed wrappers (PROTOCOL.md). Timeouts cover the device-side wait plus slack.
    def ping(self):
        return self.call("ping")

    def tree(self, not_important=False, timeout=None, budget_ms=None, max_nodes=None):
        params = {"not_important": not_important}
        if budget_ms:
            params["budget_ms"] = int(budget_ms)
        if max_nodes:
            params["max_nodes"] = int(max_nodes)
        if timeout is None:
            timeout = max(10.0, (budget_ms or 2000) / 1000 + 8)
        return self.call("tree", params, timeout=timeout)

    def act(self, dump, handle, action=None, custom=None, args=None, settle=None,
            force=False, event_ms=None):
        p = {"dump": dump, "handle": handle}
        if custom is not None:
            p["custom"] = custom
        else:
            p["action"] = action
        if args:
            p["args"] = args
        if settle is not None:
            p["settle"] = settle
        if force:
            p["force"] = True
        if event_ms is not None:
            p["event_ms"] = event_ms
        return self.call("act", p, timeout=_settle_budget(settle))

    def gesture(self, type, points, ms=None, settle=None):
        p = {"type": type, "points": [list(map(int, pt)) for pt in points]}
        if ms is not None:
            p["ms"] = ms
        if settle is not None:
            p["settle"] = settle
        return self.call("gesture", p, timeout=_settle_budget(settle) + (ms or 0) / 1000)

    def global_action(self, name, settle=None):
        p = {"name": name}
        if settle is not None:
            p["settle"] = settle
        return self.call("global", p, timeout=_settle_budget(settle))

    def events(self, since=0, limit=None):
        p = {"since": since}
        if limit:
            p["limit"] = limit
        return self.call("events", p)

    def wait_idle(self, quiet_ms=150, timeout_ms=2000):
        return self.call("wait_idle", {"quiet_ms": quiet_ms, "timeout_ms": timeout_ms},
                         timeout=timeout_ms / 1000 + 5)

    def wait_for(self, timeout_ms=5000, **cond):
        cond = {k: v for k, v in cond.items() if v is not None}
        return self.call("wait_for", dict(cond, timeout_ms=timeout_ms), timeout=timeout_ms / 1000 + 5)

    def current(self):
        return self.call("current")

    def clipboard(self, set=None):
        return self.call("clipboard", {} if set is None else {"set": set})

    def subscribe(self, events=None):
        return self.call("subscribe", {"events": list(events)} if events else {})

    def unsubscribe(self):
        return self.call("unsubscribe")

    def close(self):
        self._drop()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def error_from(method, err):
    """A device JSON-RPC error object -> UserError with the right error.kind."""
    code = err.get("code")
    text = err.get("message", "unknown error")
    kind, _ = DEVICE_ERRORS.get(code, ("device", ""))
    if code == -32001:
        return UserError(f"the agent refused this connection: {text}", kind)
    if code in DEVICE_ERRORS:
        return UserError(f"{method}: {text}", kind)
    return UserError(f"{method}: {text} (code {code})", kind)


def _settle_budget(settle):
    """Seconds to wait for a reply that may include a device-side settle and a tree dump."""
    t = 5.0
    if settle:
        t += settle.get("timeout_ms", 2000) / 1000 + 3.0   # + the tree dump's 2 s budget
    return t


def screenshot(client, serial, scale=0.5, quality=70, crop=None):
    """A downscaled JPEG: the agent's takeScreenshot (API 30+), else `adb exec-out screencap`.

    -> {format, w, h, scale, data (base64), source}. FLAG_SECURE raises secure-window
    from the agent; screencap of a secure window comes back black (not detectable here).
    """
    p = {"scale": scale, "quality": quality}
    if crop:
        p["crop"] = list(crop)
    try:
        r = client.call("screenshot", p, timeout=10.0)
        r["source"] = "agent"
        return r
    except UserError as e:
        if e.kind != "unsupported":
            raise
    return screencap_jpeg(serial, scale, quality, crop)


def secure_windows(dumpsys_text):
    """Names of on-screen FLAG_SECURE windows in `dumpsys window windows` output.

    The API < 30 screenshot path is `screencap`, which renders a secure window
    black instead of failing; this is how we refuse to pass that off as a real
    screenshot. Pure (tested on real captures)."""
    import re
    out = []
    for block in re.split(r"\n(?=  Window #\d+ )", dumpsys_text):
        m = re.match(r"\s*Window #\d+ Window\{\S+ \S+ ([^}]*)\}", block)
        if not m:
            continue
        flags = re.search(r"\bfl=([^\n]*)", block)
        if (flags and re.search(r"\bSECURE\b", flags.group(1))
                and "isVisible=true" in block and "mHasSurface=true" in block):
            out.append(m.group(1))
    return out


def screencap_jpeg(serial, scale=0.5, quality=70, crop=None):
    import base64
    import io
    import subprocess
    try:
        dump = subprocess.run([adb_path(), "-s", serial, "shell", "dumpsys", "window", "windows"],
                              capture_output=True, text=True, timeout=15).stdout
    except (subprocess.SubprocessError, OSError):
        dump = ""
    secure = secure_windows(dump)
    if secure:
        raise UserError(f"a FLAG_SECURE window is on screen ({secure[0]}); screencap would return "
                        "a black image, not the screen", "secure-window",
                        hint="snapshot still works on secure windows")
    try:
        png = subprocess.run([adb_path(), "-s", serial, "exec-out", "screencap", "-p"],
                             capture_output=True, timeout=15, check=True).stdout
    except (subprocess.SubprocessError, OSError) as e:
        raise UserError(f"screencap failed: {e}", "adb")
    try:
        from PIL import Image
    except ImportError:
        raise UserError("Pillow is needed for screenshots on API < 30", "missing-dep",
                        hint="pip install pillow")
    img = Image.open(io.BytesIO(png)).convert("RGB")
    if crop:
        img = img.crop(tuple(crop))
    if scale != 1:
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return {"format": "jpeg", "w": img.width, "h": img.height, "scale": scale,
            "data": base64.b64encode(buf.getvalue()).decode(), "source": "screencap"}


def adb_path():
    """The SDK's adb when ANDROID_HOME points at one, else whatever is on PATH."""
    home = os.environ.get("ANDROID_HOME") or os.path.expanduser("~/Android/Sdk")
    cand = os.path.join(home, "platform-tools", "adb")
    return cand if os.path.exists(cand) else "adb"


def diagnose(serial):
    """Why is the agent unreachable? Raises the most precise UserError it can."""
    d = adb_device(serial)
    if installed_version(d) is None:
        raise UserError(f"the droidctl agent is not installed on {serial}", "not-installed",
                        hint="run: droidctl setup")
    if not any(is_ours(e) for e in get_services(d)):
        raise UserError(f"the agent is installed but its accessibility service is not enabled on {serial}",
                        "not-installed", hint="run: droidctl setup")
    if not a11y_enabled(d):
        raise UserError("the agent's service is listed but accessibility_enabled is 0",
                        "not-installed", hint="run: droidctl setup")
    raise UserError("the agent's service is enabled but its socket does not answer "
                    "(Android may not have bound it yet, or Appium/uiautomator2 is suppressing it)",
                    "connection", hint="run: droidctl doctor")


# Set by the daemon: connect() then hands out the device's warm, shared session
# (same AgentClient API, a tree cache behind `tree`) instead of a new socket.
_connect_hook = None


def connect(serial, timeout=5.0):
    """Open an AgentClient and ping it: -> (client, ping_result).

    Uses the existing forward; on failure re-creates the forward once, then
    diagnoses (not installed / not enabled / not bound) with a typed error.
    Inside the daemon the client is the device's pooled session; closing it is
    cheap either way, so callers always close.
    """
    if _connect_hook is not None:
        return _connect_hook(serial, timeout)
    return _connect(serial, timeout)


def _connect(serial, timeout=5.0):
    for attempt in (0, 1):
        port = find_forward(serial) if attempt == 0 else None
        if port is None:
            if attempt == 0:
                continue
            remove_forward(serial)
            port = ensure_forward(serial)
        try:
            c = AgentClient(port, timeout=timeout)
            try:
                return c, c.call("ping", timeout=timeout)
            except UserError:
                c.close()
                raise
        except UserError as e:
            if e.kind != "connection":
                raise
    # the forward we just made leads nowhere; don't leave it behind
    remove_forward(serial)
    diagnose(serial)


def median(xs):
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def rtt_stats(samples_ms):
    s = sorted(samples_ms)
    p95 = s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))]
    return {"n": len(s), "min": round(s[0], 3), "median": round(median(s), 3),
            "p95": round(p95, 3), "max": round(s[-1], 3)}


def time_pings(client, count):
    samples = []
    for _ in range(count):
        t0 = time.perf_counter()
        client.call("ping")
        samples.append((time.perf_counter() - t0) * 1000)
    return samples
