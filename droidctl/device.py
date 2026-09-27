"""The device link: adb selection, the agent socket, setup state and a11y settings.

Import cost matters here: the ping/act hot path (select the device, find the
forward, talk to the agent) is stdlib only. It speaks adb's tiny host protocol
directly for `host:devices` and `host:list-forward`, so `adbutils` (and the
requests/PIL it drags in, ~150 ms) loads only for setup-type work: shell,
install, creating or removing a forward.
"""
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
PROTOCOL = 1
# The versionCode of the APK bundled in droidctl/assets. It has to match the
# agent's build.gradle.kts (a unit test checks); setup compares it with what
# the phone reports so an unchanged agent is not reinstalled.
AGENT_VERSION_CODE = 1
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
class AgentClient:
    """One persistent connection to the on-device agent, good for many calls."""

    def __init__(self, port, host="127.0.0.1", timeout=5.0):
        self.port = port
        self._id = 0
        try:
            self._sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as e:
            raise UserError(f"nothing listening on tcp:{port}: {e}", "connection")
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._r = self._sock.makefile("rb")

    def call(self, method, params=None, timeout=10.0):
        self._id += 1
        req = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            req["params"] = params
        try:
            self._sock.settimeout(timeout)
            self._sock.sendall(json.dumps(req, separators=(",", ":")).encode() + b"\n")
            while True:
                line = self._r.readline()
                if not line:
                    # adb accepts the forwarded connect even when nothing listens
                    # on the device, then closes it: EOF here means no agent
                    raise UserError("the agent closed the connection before replying "
                                    "(service not running?)", "connection")
                try:
                    msg = json.loads(line)
                except ValueError:
                    raise UserError(f"the agent sent invalid JSON: {line[:200]!r}", "device")
                if "id" not in msg:
                    continue                      # a notification; not ours to answer
                if msg["id"] is not None and msg["id"] != self._id:
                    continue                      # a late reply to an older request
                if "error" in msg:
                    e = msg["error"] or {}
                    code = e.get("code")
                    text = e.get("message", "unknown error")
                    if code == -32001:
                        raise UserError(f"the agent refused this connection: {text}", "device")
                    if code == -32002:
                        raise UserError(f"{method}: {text}", "stale-ref")
                    raise UserError(f"{method}: {text} (code {code})", "device")
                return msg.get("result")
        except socket.timeout:
            raise UserError(f"{method}: no reply within {timeout:g}s", "timeout")
        except (ConnectionError, OSError) as e:
            raise UserError(f"{method}: connection lost: {e}", "connection")

    def close(self):
        try:
            self._r.close()
            self._sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


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


def connect(serial, timeout=5.0):
    """Open an AgentClient and ping it: -> (client, ping_result).

    Uses the existing forward; on failure re-creates the forward once, then
    diagnoses (not installed / not enabled / not bound) with a typed error.
    """
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
