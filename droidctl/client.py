"""The thin client: argv -> the resident daemon -> print. Stdlib only.

This is the `droidctl` console script. Every call pays Python's boot (~27 ms
measured on the dev machine) and nothing else we can avoid: no argparse, no
rich, no adbutils. It sends the argv to the daemon over ~/.droidctl/d.sock and
prints what comes back. The daemon holds the parser, the warm device
connections and the tree cache (see DAEMON.md).

If no daemon answers, the client starts one (`python -m droidctl daemon start
--foreground`, detached, under a flock so two first calls can't spawn two),
says so once on stderr, and retries. It falls back to running the command in
this process when the daemon can't be used (--no-daemon, DROIDCTL_NO_DAEMON=1,
a sandbox that forbids spawning or unix sockets). DROIDCTL_AUTOSTART=0 turns
auto-start into an error instead.

Also here, because it is the stdlib layer the daemon shares: the paths, the
NDJSON JSON-RPC framing and `Client`, the Python SDK.
"""
import json
import os
import socket
import sys
import time

from droidctl import __version__

# Commands that must run in the calling process: they manage the daemon itself
# or own the process's stdio for their whole life.
LOCAL = {"daemon", "serve", "mcp"}

RESTART = -32010          # the daemon's reply to a client from another build


# --------------------------------------------------------------------------
# paths (DROIDCTL_HOME moves all of them; tests use a tmp dir)
# --------------------------------------------------------------------------
def home():
    return os.environ.get("DROIDCTL_HOME") or os.path.expanduser("~/.droidctl")


def sock_path():
    return os.path.join(home(), "d.sock")


def pid_path():
    return os.path.join(home(), "daemon.pid")


def lock_path():
    return os.path.join(home(), "daemon.lock")


def log_path():
    return os.path.join(home(), "daemon.log")


def build_id():
    """Version plus the newest source mtime: an edited checkout is a new build,
    so a daemon left running from before the edit restarts itself."""
    pkg = os.path.dirname(os.path.abspath(__file__))
    newest = 0
    try:
        with os.scandir(pkg) as it:
            for e in it:
                if e.name.endswith(".py"):
                    newest = max(newest, e.stat().st_mtime_ns)
    except OSError:
        pass
    return f"{__version__}+{newest}"


def idle_seconds():
    """DROIDCTL_IDLE: '1800', '90s', '30m', '2h' (default 30 min; 0 = never)."""
    raw = (os.environ.get("DROIDCTL_IDLE") or "30m").strip().lower()
    mult = {"s": 1, "m": 60, "h": 3600}.get(raw[-1:], None)
    try:
        return float(raw[:-1]) * mult if mult else float(raw)
    except ValueError:
        return 1800.0


# --------------------------------------------------------------------------
# framing: NDJSON JSON-RPC 2.0 over a buffered file (both ends)
# --------------------------------------------------------------------------
def send(f, obj):
    f.write(json.dumps(obj, separators=(",", ":"), default=str).encode() + b"\n")
    f.flush()


def recv(f):
    """One message, or None at EOF (a clean disconnect)."""
    line = f.readline()
    return json.loads(line) if line.strip() else None


class DaemonUnavailable(Exception):
    """No daemon to talk to (and none could be started)."""


class DroidctlError(Exception):
    """A command failed: `kind` is one of droidctl's ERROR_KINDS."""

    def __init__(self, kind, message, hint="", data=None):
        super().__init__(message)
        self.kind, self.hint, self.data = kind, hint, data


class _Conn:
    """One persistent connection to the daemon."""

    def __init__(self, timeout=None):
        path = sock_path()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        try:
            s.connect(path)
        except OSError:
            s.close()
            raise
        s.settimeout(timeout)
        self.sock, self.f, self._id = s, s.makefile("rwb"), 0

    def request(self, method, params=None):
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        send(self.f, msg)
        while True:
            resp = recv(self.f)
            if resp is None:
                raise ConnectionError("the daemon closed the connection")
            if resp.get("id") == self._id:
                return resp

    def close(self):
        for x in (self.f, self.sock):
            try:
                x.close()
            except OSError:
                pass


def ping(timeout=2.0):
    """The daemon's ping result, or None if nothing answers (a stale socket too)."""
    try:
        c = _Conn(timeout=timeout)
    except OSError:
        return None
    try:
        return c.request("ping").get("result")
    except (OSError, ValueError):
        return None
    finally:
        c.close()


# --------------------------------------------------------------------------
# starting the daemon
# --------------------------------------------------------------------------
def spawn():
    """Start a detached daemon; return its pid once it answers (else raise)."""
    import subprocess
    os.makedirs(home(), mode=0o700, exist_ok=True)
    with open(log_path(), "ab") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "droidctl", "daemon", "start", "--foreground"],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True,            # survives the calling shell / tool call
            cwd=os.path.expanduser("~"), close_fds=True)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        info = ping(timeout=1.0)
        if info:
            return info.get("pid", proc.pid)
        if proc.poll() is not None:
            break                              # it died (bind refused, crash): see the log
        time.sleep(0.05)
    raise DaemonUnavailable(f"the daemon did not come up (see {log_path()})")


def ensure_daemon(autostart=True):
    """Make sure a daemon of *this* build answers. Returns the pid if we
    started it, None if one was already running."""
    info = ping()
    if info and info.get("build") == build_id():
        return None
    if not autostart:
        raise DaemonUnavailable("no daemon is running and DROIDCTL_AUTOSTART=0")
    import fcntl
    os.makedirs(home(), mode=0o700, exist_ok=True)
    with open(lock_path(), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)        # two first calls must not spawn two daemons
        info = ping()
        if info and info.get("build") == build_id():
            return None                       # someone else started it while we waited
        if info:                              # another build: ask it to go, then wait
            _shutdown_and_wait()
        return spawn()


def _shutdown_and_wait(timeout=5.0):
    try:
        c = _Conn(timeout=2.0)
        try:
            c.request("shutdown")
        finally:
            c.close()
    except (OSError, ValueError):
        pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and ping(timeout=0.5):
        time.sleep(0.05)


# --------------------------------------------------------------------------
# the Python SDK
# --------------------------------------------------------------------------
class Client:
    """A resident connection to the daemon: connect once, call many times.

        from droidctl.client import Client
        with Client() as c:
            snap = c.call("snapshot")            # the --json payload
            c.call(["tap", "4"])
            for ev in c.subscribe(events=["toast"]):
                print(ev)

    `call` raises DroidctlError (with .kind) when the command fails.
    """

    def __init__(self, autostart=True, timeout=None):
        self.started = ensure_daemon(autostart and os.environ.get("DROIDCTL_AUTOSTART") != "0")
        self._timeout = timeout
        self._c = _Conn(timeout=timeout)

    def run(self, argv, stdin=None, both=False):
        """Run one command line; return the daemon's raw result
        {code, stdout, stderr, json, payload?, text?, mode}."""
        if isinstance(argv, str):
            import shlex
            argv = shlex.split(argv)
        params = _run_params(list(argv), stdin)
        if both:
            params["both"] = True
        resp = self._c.request("run", params)
        if "error" in resp:
            err = resp["error"]
            if err.get("code") == RESTART:
                raise DaemonUnavailable("the daemon is from another build; reconnect")
            raise DroidctlError("error", err.get("message", "daemon error"))
        return resp["result"]

    def call(self, argv, stdin=None):
        """Run a command in --json mode; return its payload (raise on failure)."""
        if isinstance(argv, str):
            import shlex
            argv = shlex.split(argv)
        argv = list(argv)
        if "--json" not in argv:
            argv.append("--json")
        res = self.run(argv, stdin=stdin)
        try:
            payload = json.loads(res.get("stdout") or "null")
        except ValueError:
            raise DroidctlError("error", (res.get("stderr") or res.get("stdout") or "").strip())
        if isinstance(payload, dict) and payload.get("ok") is False and "error" in payload:
            e = payload["error"]
            raise DroidctlError(e.get("kind", "error"), e.get("message", ""), e.get("hint", ""), e.get("data"))
        if payload is None and res.get("code"):
            raise DroidctlError("bad-args", (res.get("stderr") or "").strip())
        return payload

    def status(self):
        return self._c.request("status").get("result")

    def subscribe(self, device=None, events=None, timeout=None):
        """Yield pushed device events ({serial, ...event}) on a dedicated connection
        until the iterator is closed, or `timeout` seconds pass with none."""
        c = _Conn(timeout=timeout)
        try:
            base = _run_params([])            # the device is resolved with our env (adb port, serial)
            params = {"cwd": base["cwd"], "env": base["env"]}
            if device:
                params["device"] = device
            if events:
                params["events"] = list(events)
            resp = c.request("subscribe", params)
            if "error" in resp:
                raise DroidctlError("error", resp["error"].get("message", ""))
            while True:
                try:
                    msg = recv(c.f)
                except socket.timeout:
                    return
                if msg is None:
                    return
                if msg.get("method") == "event":
                    yield msg.get("params")
        finally:
            c.close()

    def close(self):
        self._c.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# --------------------------------------------------------------------------
# the CLI entrypoint
# --------------------------------------------------------------------------
def _command(argv):
    for t in argv:
        if t == "--":
            return None
        if not t.startswith("-"):
            return t
    return None


def _wants_stdin(argv):
    """Only read our stdin when the command will consume it: an agent's tool
    call may hand us an open pipe that never closes."""
    if "--stdin" in argv:
        return True
    if _command(argv) == "run":
        rest = argv[argv.index("run") + 1:]
        if any(t in ("--step", "-s") or t.startswith("--step=") for t in rest):
            return False
        files, skip = [], False
        for t in rest:
            if skip:
                skip = False
            elif t in ("-d", "--device"):
                skip = True                   # its value is not a FILE
            elif t == "-" or not t.startswith("-"):
                files.append(t)
        return not files or files == ["-"]
    return False


_ENV_KEYS = ("ANDROID_SERIAL", "ANDROID_ADB_SERVER_PORT", "NO_COLOR", "COLUMNS", "TERM")


def _run_params(argv, stdin=None):
    env = {k: v for k, v in os.environ.items()
           if k in _ENV_KEYS or (k.startswith("DROIDCTL_") and k != "DROIDCTL_HOME")}
    try:
        cols = os.get_terminal_size(sys.stdout.fileno()).columns
    except (OSError, ValueError, AttributeError):
        cols = None
    p = {"argv": argv, "build": build_id(), "cwd": os.getcwd(), "env": env,
         "tty": sys.stdout.isatty(), "width": cols}
    if stdin is not None:
        p["stdin"] = stdin
    return p


def _inprocess(argv, note=None):
    from droidctl import cli
    if note and "--json" not in argv:
        sys.stderr.write(f"droidctl: {note}; running in-process\n")
    cli.main(argv, mode="inprocess")


def _print_result(res, argv, started):
    out, err = res.get("stdout", ""), res.get("stderr", "")
    if started and res.get("json"):
        try:
            payload = json.loads(out)
            if isinstance(payload, dict):
                payload["daemon"] = {"started": True, "pid": started}
                out = json.dumps(payload, indent=2, default=str) + "\n"
        except ValueError:
            pass
    sys.stdout.write(out)
    sys.stdout.flush()
    if err:
        sys.stderr.write(err)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    no_daemon = "--no-daemon" in argv or os.environ.get("DROIDCTL_NO_DAEMON") == "1"
    argv = [t for t in argv if t != "--no-daemon"]
    cmd = _command(argv)
    if no_daemon or cmd is None or cmd in LOCAL:
        return _inprocess(argv)
    want_json = "--json" in argv
    stdin = sys.stdin.read() if _wants_stdin(argv) else None
    autostart = os.environ.get("DROIDCTL_AUTOSTART") != "0"
    started = None
    for attempt in (0, 1):
        try:
            try:
                conn = _Conn()                # the common case: one connect, one request
            except (FileNotFoundError, ConnectionRefusedError):
                started = ensure_daemon(autostart) or started
                conn = _Conn()
        except DaemonUnavailable as e:
            if not autostart:
                return _no_daemon(want_json, str(e))
            return _inprocess(argv, note=str(e))
        except OSError as e:                  # a sandbox that forbids unix sockets
            return _inprocess(argv, note=f"cannot reach the daemon ({e.strerror or e})")
        try:
            resp = conn.request("run", _run_params(argv, stdin))
        except (OSError, ValueError) as e:
            conn.close()
            if attempt == 0:
                continue                      # it went away (idle exit, restart): once more
            return _inprocess(argv, note=f"the daemon connection failed ({e})")
        conn.close()
        if "error" in resp and resp["error"].get("code") == RESTART and attempt == 0:
            _shutdown_and_wait()
            continue                          # a daemon from another build: replace it
        if "error" in resp:
            return _inprocess(argv, note=resp["error"].get("message", "daemon error"))
        res = resp["result"]
        if res.get("passthrough"):
            return _inprocess(argv)
        if started and not want_json:
            sys.stderr.write(
                f"droidctl: started background daemon (pid {started}, idle-exit "
                f"{_fmt_idle(idle_seconds())}) — 'droidctl daemon stop' to stop, "
                "DROIDCTL_NO_DAEMON=1 to never start it\n")
        _print_result(res, argv, started)
        sys.exit(int(res.get("code", 0)))
    return _inprocess(argv)


def _fmt_idle(s):
    if s <= 0:
        return "never"
    return f"{s / 60:g}m" if s >= 60 else f"{s:g}s"


def _no_daemon(want_json, message):
    hint = "start it with: droidctl daemon start (or use --no-daemon)"
    if want_json:
        print(json.dumps({"ok": False, "mode": "inprocess",
                          "error": {"kind": "no-daemon", "message": message, "hint": hint}}, indent=2))
    else:
        sys.stderr.write(f"no-daemon: {message}\n{hint}\n")
    sys.exit(1)


def entry():
    """The console script: main() with Ctrl-C mapped to exit 130, like the in-process CLI."""
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    entry()
