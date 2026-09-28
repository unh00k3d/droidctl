"""droidctl daemon: the resident broker that keeps everything warm.

One process per user (DROIDCTL_HOME), listening on ~/.droidctl/d.sock (0600).
It runs the same parser and command functions as the in-process CLI, so the
output and error vocabulary are identical; what it adds is state that
outlives a call (see DAEMON.md for the socket API and lifecycle):

- per device, a warm agent connection plus a second one permanently
  subscribed to the agent's events;
- a tree cache that is served without touching the device when it is provably
  still valid (the dirty flag comes from pushed events; `gen` confirms it);
- events that happened between calls (toasts, window changes), reported on
  the next response for that device;
- one thread per client connection, a command lock per device (phone A never
  blocks phone B, reads never wait for a lock), idle exit after 30 min.

Commands run concurrently, so their stdout/stderr/stdin are thread-local: the
process-wide sys.std* are proxies that route to the current request's buffers,
and droidctl's rich consoles are built per request for that client's terminal.
"""
import collections
import contextlib
import io
import json
import logging
import os
import queue
import select
import signal
import socket
import sys
import threading
import time
import traceback

from droidctl import __version__
from droidctl import client as cl

log = logging.getLogger("droidctl.daemon")

# Commands that only read: they never take the device's command lock, so a
# snapshot is served while another agent's tap is in flight. Anything not
# listed here (including commands added later) is treated as mutating.
READ_ONLY = {"snapshot", "snap", "where", "current", "ping", "devices", "doctor", "events",
             "logs", "shot", "screenshot", "wait", "watch", "cheat", "version", "dump-fixture"}
# After these the device's agent may have been restarted or removed.
RESETS_SESSIONS = {"setup", "teardown"}
# Event types worth telling the agent about when they happened between its calls.
BETWEEN_CALLS = {"toast", "window_state", "notification", "announcement"}

TTL_S = 5.0          # never serve a cached tree unverified for longer than this
QUIET_S = 0.3        # the agent compacts bursts within 250 ms (Events.COMPACT_MS)
LOCK_WAIT_S = 120.0  # how long a mutating command waits for another one on the same device


# --------------------------------------------------------------------------
# thread-local stdio: every request gets its own stdout/stderr/stdin
# --------------------------------------------------------------------------
_TL = threading.local()


class _TLStream(io.TextIOBase):
    def __init__(self, name, real):
        self._name, self._real = name, real

    def _target(self):
        return getattr(_TL, self._name, None) or self._real

    def write(self, s):
        return self._target().write(s)

    def flush(self):
        with contextlib.suppress(Exception):
            self._target().flush()

    def isatty(self):
        if getattr(_TL, self._name, None) is not None:
            return bool(getattr(_TL, "tty", False))
        return self._real.isatty()

    def fileno(self):
        if getattr(_TL, self._name, None) is not None:
            raise io.UnsupportedOperation("fileno")
        return self._real.fileno()

    @property
    def encoding(self):
        return "utf-8"

    def writable(self):
        return True


class _TLStdin(io.TextIOBase):
    def __init__(self, real):
        self._real = real

    def _src(self):
        return getattr(_TL, "stdin", None) or self._real

    def read(self, n=-1):
        return self._src().read(n)

    def readline(self, n=-1):
        return self._src().readline(n)

    def __iter__(self):
        return iter(self._src())

    def isatty(self):
        return False

    def readable(self):
        return True


class _TLConsole:
    """Stands in for droidctl.core's lazily built rich consoles."""

    def __init__(self, which):
        self._which = which

    def __getattr__(self, name):
        return getattr(_console(self._which), name)


def _console(which):
    cons = getattr(_TL, "consoles", None)
    if cons is None:
        cons = _TL.consoles = {}
    c = cons.get(which)
    if c is None:
        from rich.console import Console
        tty = bool(getattr(_TL, "tty", False))
        env = getattr(_TL, "env", {}) or {}
        width = getattr(_TL, "width", None) or int(env.get("COLUMNS") or 80)
        kw = dict(force_terminal=tty, width=width, no_color=bool(env.get("NO_COLOR")) or not tty,
                  color_system="standard" if tty else None)
        if which == "err":
            kw["style"] = "red"
        c = Console(file=sys.stderr if which == "err" else sys.stdout, **kw)
        cons[which] = c
    return c


def _install_stdio():
    """Swap in the proxies once, at daemon start."""
    from droidctl import core
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout = _TLStream("out", real_out)
    sys.stderr = _TLStream("err", real_err)
    sys.stdin = _TLStdin(open(os.devnull))
    core.console._real = _TLConsole("out")
    core.err._real = _TLConsole("err")
    return real_err


# --------------------------------------------------------------------------
# cwd and environment. Both are process-wide, so they are the one thing
# concurrent requests can't each have their own of. Kept to the minimum:
# - ANDROID_SERIAL is not applied at all: it becomes the command's -d;
# - path arguments are made absolute against the client's cwd, so only `run`
#   (whose steps may hold relative paths) needs the process cwd;
# - rendering settings (NO_COLOR, COLUMNS, TERM) are per request already.
# What is left (DROIDCTL_*, ANDROID_ADB_SERVER_PORT) is applied under a gate:
# a request needing other values waits until none with different ones runs.
# --------------------------------------------------------------------------
_KEEP_ENV = {"DROIDCTL_HOME", "DROIDCTL_IDLE", "DROIDCTL_NO_DAEMON", "DROIDCTL_AUTOSTART"}
PATH_ARGS = ("file", "out", "apk", "dir", "fixture")
CWD_COMMANDS = {"run"}


def _gate_env(env):
    return {k: v for k, v in (env or {}).items()
            if (k.startswith("DROIDCTL_") and k not in _KEEP_ENV) or k == "ANDROID_ADB_SERVER_PORT"}


class _Gate:
    def __init__(self):
        self.cond = threading.Condition()
        self.env_key, self.env_active = None, 0
        self.cwd, self.cwd_active = None, 0
        self.managed = {"ANDROID_ADB_SERVER_PORT"}

    def enter(self, env, cwd=None):
        """-> a token for exit(). `cwd` only for requests that need the process cwd."""
        env = _gate_env(env)
        key = tuple(sorted(env.items()))
        with self.cond:
            while ((self.env_active and self.env_key != key)
                   or (cwd and self.cwd_active and self.cwd != cwd)):
                self.cond.wait()
            if self.env_key != key:
                self.managed |= set(env)
                for k in self.managed:
                    if k in env:
                        os.environ[k] = env[k]
                    else:
                        os.environ.pop(k, None)
                self.env_key = key
            self.env_active += 1
            if cwd:
                if self.cwd != cwd:
                    try:
                        os.chdir(cwd)
                    except OSError:
                        os.chdir(os.path.expanduser("~"))
                    self.cwd = cwd
                self.cwd_active += 1
        return bool(cwd)

    def exit(self, used_cwd):
        with self.cond:
            self.env_active -= 1
            if used_cwd:
                self.cwd_active -= 1
            self.cond.notify_all()


_GATE = _Gate()


# --------------------------------------------------------------------------
# per-device sessions: warm connection, event subscription, tree cache
# --------------------------------------------------------------------------
class _Sub(queue.Queue):
    def __init__(self, types=None):
        super().__init__(maxsize=2000)
        self.types = set(types) if types else None

    def offer(self, ev):
        if self.types is None or ev.get("type") in self.types:
            with contextlib.suppress(queue.Full):
                self.put_nowait(ev)


class _Entry:
    """The cached latest dump. Its handles are the only valid ones on the device."""

    def __init__(self, tree, not_important, t_sent):
        self.tree, self.not_important = tree, not_important
        self.gen = tree.get("gen", -1)
        self.t_sent = t_sent               # host time the dump was requested (<= the device's gen read)
        self.t_checked = time.monotonic()  # last time we know it was valid
        self.quiet = False                 # a gen check >= QUIET_S after the dump confirmed no change


class Session:
    def __init__(self, serial):
        self.serial = serial
        self.lock = threading.Lock()         # one request at a time on the main connection
        self.open_lock = threading.Lock()
        self.cmd_lock = threading.RLock()    # one mutating command at a time on this device
        self.client = None
        self.info = {}
        self.port = None
        self.alive = False
        self.backend = None
        self.entry = None
        self.max_gen = -1                    # highest `gen` seen on a pushed event
        self.last_seq = 0
        self.ring = collections.deque(maxlen=1000)   # (host time, event)
        self.subs = set()
        self.sub_client = None
        self.sub_alive = False
        self.side_lock = threading.Lock()    # the cache's gen checks: never behind an action
        self.side_client = None
        self.stats = collections.Counter()
        self.last_call_end = time.monotonic()
        self.last_used = time.monotonic()

    # -- connection -------------------------------------------------------
    def open(self):
        from droidctl import device as dev
        self.close()
        self.backend = dev.backend_for(self.serial)
        client, info = dev._connect(self.serial)
        self.client, self.info, self.port = client, info, client.port
        self.entry, self.alive = None, True
        self._start_subscription()
        log.info("device %s: connected on tcp:%s (agent %s)", self.serial, self.port, info.get("version"))

    def _start_subscription(self):
        from droidctl import device as dev
        from droidctl.core import UserError
        try:
            sc = dev.AgentClient(self.port, timeout=5.0)
            res = sc.subscribe()
            self.last_seq = (res or {}).get("next", self.last_seq)
        except UserError as e:
            log.warning("device %s: no event subscription (%s); every cache hit will cost a gen check",
                        self.serial, e)
            self.sub_alive = False
            return
        self.sub_client, self.sub_alive = sc, True
        threading.Thread(target=self._read_events, args=(sc,), daemon=True,
                         name=f"events-{self.serial}").start()

    def _read_events(self, sc):
        from droidctl.core import UserError
        try:
            while self.sub_client is sc:
                for ev in sc.notifications(timeout=1.0):
                    if ev:
                        self._on_event(ev)
        except UserError:
            pass
        finally:
            if self.sub_client is sc:
                self.sub_alive = False
                log.info("device %s: event subscription ended", self.serial)
                # an external UiAutomation client (uiautomator dump, Appium) unbinds
                # the service for ~1-2 s; until we resubscribe, every cache hit pays
                # a gen check (safe, just slower), so try to get the stream back
                threading.Thread(target=self._resubscribe, args=(sc,), daemon=True,
                                 name=f"resub-{self.serial}").start()

    def _resubscribe(self, old, attempts=20, every=1.0):
        for _ in range(attempts):
            time.sleep(every)
            if not self.alive or self.sub_client is not old:
                return                  # closed, or someone already replaced it
            self._start_subscription()
            if self.sub_alive:
                log.info("device %s: event subscription restored", self.serial)
                return

    def _on_event(self, ev):
        self.ring.append((time.monotonic(), ev))
        self.last_seq = max(self.last_seq, ev.get("seq") or 0)
        g = ev.get("gen")
        if isinstance(g, int) and g > self.max_gen:
            self.max_gen = g
        for s in list(self.subs):
            s.offer(ev)

    def close(self):
        self.alive = False
        self.entry = None
        sc, self.sub_client = self.sub_client, None
        self.sub_alive = False
        side, self.side_client = self.side_client, None
        for c in (sc, side, self.client):
            if c is not None:
                with contextlib.suppress(Exception):
                    c.close()
        self.client = None

    # -- calls ------------------------------------------------------------
    def _raw(self, method, params, timeout):
        from droidctl.core import UserError
        with self.lock:
            if not self.alive or self.client is None:
                raise UserError(f"{method}: not connected to {self.serial}", "connection")
            try:
                return self.client.call(method, params, timeout=timeout)
            except UserError as e:
                if e.kind == "connection":
                    self.close()
                raise

    def _read(self, method, params, timeout):
        """A read-only call: reconnect and retry once if the link dropped."""
        from droidctl.core import UserError
        try:
            return self._raw(method, params, timeout)
        except UserError as e:
            if e.kind != "connection":
                raise
            with self.open_lock:
                if not self.alive:
                    self.open()
            return self._raw(method, params, timeout)

    def _side(self, method, params=None, timeout=5.0):
        """A cheap read on a second, persistent connection. The main one is held for
        the whole of an action (a settle can take seconds), and a cache check that
        queued behind it would make a cached snapshot wait for the action."""
        from droidctl import device as dev
        from droidctl.core import UserError
        with self.side_lock:
            for attempt in (0, 1):
                if self.side_client is None:
                    self.side_client = dev.AgentClient(self.port, timeout=timeout)
                try:
                    return self.side_client.call(method, params or {}, timeout=timeout)
                except UserError as e:
                    with contextlib.suppress(Exception):
                        self.side_client.close()
                    self.side_client = None
                    if e.kind != "connection" or attempt:
                        raise

    def call(self, method, params=None, timeout=10.0):
        from droidctl import device as dev
        from droidctl.core import UserError
        self.last_used = time.monotonic()
        params = params or {}
        if method == "tree":
            return self.tree(params, timeout)
        if method in ("wait_for", "wait_idle"):
            # device-side blocking waits get their own connection: they must
            # not hold the main one (and every other caller) for seconds
            with dev.AgentClient(self.port, timeout=5.0) as c:
                return c.call(method, params, timeout=timeout)
        if method in ("ping", "echo", "gen", "current", "events", "screenshot"):
            return self._read(method, params, timeout)
        # everything else may change the screen
        try:
            res = self._raw(method, params, timeout)
        except UserError as e:
            if e.kind == "stale-ref":
                self.entry = None
            raise
        tree = res.get("tree") if isinstance(res, dict) else None
        if tree and not tree.get("degraded"):
            ni = bool((params.get("settle") or {}).get("not_important"))
            self.entry = _Entry(tree, ni, time.monotonic())
            self.stats["primed"] += 1
        else:
            self.entry = None
        return res

    def tree(self, params, timeout):
        """The latest dump, from the cache when it is provably still current."""
        ni = bool(params.get("not_important"))
        if params.get("windows") is False:
            self.entry = None
            return self._read("tree", params, timeout)
        e = self.entry
        if e is not None and e.not_important == ni and self.max_gen <= e.gen:
            now = time.monotonic()
            if e.quiet and self.sub_alive and now - e.t_checked < TTL_S:
                self.stats["hits"] += 1
                return e.tree
            t_send = time.monotonic()
            g = (self._side("gen") or {}).get("gen")
            self.stats["gen_checks"] += 1
            if g == e.gen and self.entry is e:
                # nothing changed between the dump and this check; if they are far
                # enough apart, any later change starts a new (pushed) event burst
                if t_send - e.t_sent >= QUIET_S + 0.1:
                    e.quiet = True
                e.t_checked = time.monotonic()
                self.stats["hits"] += 1
                return e.tree
        t_send = time.monotonic()
        tree = self._read("tree", params, timeout)
        self.stats["misses"] += 1
        self.entry = None if (tree or {}).get("degraded") else _Entry(tree, ni, t_send)
        return tree

    def since(self, t0, t1):
        return [ev for (t, ev) in list(self.ring) if t0 < t <= t1 and ev.get("type") in BETWEEN_CALLS]

    def describe(self):
        st = self.stats
        served = st["hits"] + st["misses"]
        return {"serial": self.serial, "backend": self.backend, "port": self.port, "connected": self.alive,
                "subscribed": self.sub_alive, "agent": self.info.get("version"),
                "cache": {"hits": st["hits"], "misses": st["misses"], "gen_checks": st["gen_checks"],
                          "primed": st["primed"],
                          "hit_rate": round(st["hits"] / served, 3) if served else None},
                "events_seen": self.last_seq, "idle_s": round(time.monotonic() - self.last_used, 1)}


class Pool:
    def __init__(self):
        self.sessions = {}
        self.lock = threading.Lock()

    def get(self, serial):
        with self.lock:
            s = self.sessions.get(serial)
            if s is None:
                s = self.sessions[serial] = Session(serial)
        from droidctl import device as dev
        with s.open_lock:                     # connecting phone A never blocks phone B
            # a different backend chosen since (setup --backend, DROIDCTL_BACKEND): reconnect
            if not s.alive or s.backend != dev.backend_for(serial):
                s.open()
        return s

    def reset(self):
        with self.lock:
            sessions, self.sessions = list(self.sessions.values()), {}
        for s in sessions:
            s.close()


POOL = Pool()


class PooledClient:
    """What `device.connect()` returns inside the daemon: the AgentClient API over
    the device's shared session. `close()` only drops this caller's subscription."""

    def __init__(self, sess):
        self._sess = sess
        self.port = sess.port
        self._q = None

    def call(self, method, params=None, timeout=10.0):
        if not self._sess.alive:
            # dropped since this client was handed out (setup/teardown, a lost link):
            # nothing was sent yet, so reconnecting is safe even for an action
            self._sess = POOL.get(self._sess.serial)
            self.port = self._sess.port
        ctx = getattr(_TL, "ctx", None)
        if ctx is not None:
            ctx.touch(self._sess)             # per call: callers may cache this client
        if method == "subscribe":
            self.close()
            self._q = _Sub((params or {}).get("events"))
            self._sess.subs.add(self._q)
            return {"subscribed": True, "next": self._sess.last_seq}
        if method == "unsubscribe":
            self.close()
            return {"unsubscribed": True}
        if method == "events" and self._sess.sub_alive:
            # served from the permanent subscription's ring: no device round trip, so a
            # cached snapshot never waits behind a running action. `age_ms` is host-side.
            since = (params or {}).get("since") or 0
            now = time.monotonic()
            evs = [dict(ev, age_ms=int((now - t) * 1000)) for t, ev in list(self._sess.ring)
                   if (ev.get("seq") or 0) > since]
            return {"events": evs, "next": max(self._sess.last_seq, since)}
        return self._sess.call(method, params, timeout)

    def notifications(self, timeout=1.0):
        while self._q is not None:
            try:
                yield self._q.get(timeout=timeout)
            except queue.Empty:
                return

    def close(self):
        if self._q is not None:
            self._sess.subs.discard(self._q)
            self._q = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __getattr__(self, name):
        # the typed wrappers (tree, act, gesture, ...) of AgentClient, bound to us
        from droidctl import device as dev
        fn = getattr(dev.AgentClient, name)
        return fn.__get__(self, PooledClient)


class _Ctx:
    """What one running command has touched: device sessions and their locks."""

    def __init__(self, read_only):
        self.read_only = read_only
        self.sessions = {}                    # serial -> (session, start time)
        self.locked = []

    def touch(self, sess):
        from droidctl.core import UserError
        if sess.serial not in self.sessions:
            self.sessions[sess.serial] = (sess, time.monotonic())
        if not self.read_only and sess not in self.locked:
            if not sess.cmd_lock.acquire(timeout=LOCK_WAIT_S):
                raise UserError(f"{sess.serial} is busy with another command", "timeout",
                                hint="another agent is acting on this device; retry")
            self.locked.append(sess)

    def finish(self):
        between = []
        now = time.monotonic()
        for sess, t_start in self.sessions.values():
            for ev in sess.since(sess.last_call_end, t_start):
                between.append(_brief(sess.serial, ev))
            sess.last_call_end = now
        for sess in self.locked:
            with contextlib.suppress(RuntimeError):
                sess.cmd_lock.release()
        self.locked = []
        return between[-20:]


def _brief(serial, ev):
    out = {"type": ev.get("type")}
    for k in ("pkg", "class", "title", "text"):
        if ev.get(k):
            out[k] = ev[k]
    return out


def _pool_connect(serial, timeout=5.0):
    """Installed as device.connect's hook inside the daemon."""
    sess = POOL.get(serial)
    ctx = getattr(_TL, "ctx", None)
    if ctx is not None:
        ctx.touch(sess)
    return PooledClient(sess), dict(sess.info)


# --------------------------------------------------------------------------
# running one command line
# --------------------------------------------------------------------------
_PARSER = None


def _parser():
    """Built once. serve() removed ANDROID_SERIAL from the daemon's env, so -d
    defaults to None here and execute() applies the client's ANDROID_SERIAL."""
    global _PARSER
    if _PARSER is None:
        from droidctl import cli
        _PARSER = cli.build_parser()
    return _PARSER


def execute(params):
    """Run one argv like the CLI would; return {code, stdout, stderr, json, mode, ...}."""
    from droidctl import cli
    from droidctl.core import UserError
    argv = [t for t in params.get("argv") or [] if t != "--no-daemon"]
    out, err = io.StringIO(), io.StringIO()
    _TL.out, _TL.err = out, err
    _TL.stdin = io.StringIO(params.get("stdin") or "")
    _TL.tty = bool(params.get("tty"))
    _TL.width = params.get("width")
    _TL.env = params.get("env") or {}
    _TL.consoles = {}
    result = {"mode": "daemon"}
    ctx, gate = None, None
    try:
        try:
            args = _parser().parse_args(argv)
        except SystemExit as e:               # argparse: --help (0) or a usage error (2)
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 2)
            return dict(result, code=code, stdout=out.getvalue(), stderr=err.getvalue(), json=False)
        if args.cmd in cl.LOCAL:
            return {"passthrough": True, "mode": "daemon"}
        env, cwd = params.get("env") or {}, params.get("cwd")
        if hasattr(args, "device") and args.device is None and env.get("ANDROID_SERIAL"):
            args.device = env["ANDROID_SERIAL"]
        if cwd:
            for attr in PATH_ARGS:
                v = getattr(args, attr, None)
                if isinstance(v, str) and v and v != "-" and not os.path.isabs(os.path.expanduser(v)):
                    setattr(args, attr, os.path.join(cwd, v))
                elif isinstance(v, str) and v.startswith("~"):
                    setattr(args, attr, os.path.expanduser(v))
        gate = _GATE.enter(env, cwd if args.cmd in CWD_COMMANDS else None)
        want_json = bool(getattr(args, "json", False))
        ctx = _TL.ctx = _Ctx(args.cmd in READ_ONLY)
        payload, error, code, exited = None, None, 0, False
        try:
            payload = args.fn(args)
        except UserError as e:
            error = {"kind": e.kind, "message": str(e)}
            if e.hint:
                error["hint"] = e.hint
            if getattr(e, "data", None):
                error["data"] = e.data
        except SystemExit as e:               # a command that exits by itself (die, run)
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
            exited = True
        except Exception as e:                # never let one bad request kill the daemon
            log.error("command %r crashed:\n%s", argv, traceback.format_exc())
            error = {"kind": "error", "message": f"internal error: {type(e).__name__}: {e}"}
        between = ctx.finish()
        _TL.ctx = None
        if args.cmd in RESETS_SESSIONS:
            POOL.reset()
        if error is not None:
            code = 1
            if want_json:
                print(json.dumps({"ok": False, "error": error, "mode": "daemon"}, indent=2, default=str))
            else:
                from droidctl.core import err as err_console
                err_console.print(f"{error['kind']}: {error['message']}"
                                  + (f"\n{error['hint']}" if error.get("hint") else ""), markup=False)
        elif not exited:
            if isinstance(payload, dict):
                payload.setdefault("mode", "daemon")
                if between:
                    payload["between_calls"] = between
                if payload.get("ok") is False:
                    code = 1
            if want_json:
                print(json.dumps(payload, indent=2, default=str))
            else:
                render = getattr(args, "render", None)
                if render is not None and payload is not None:
                    render(payload)
                if between:
                    _console("out").print("[dim]between calls: " + "; ".join(
                        _fmt_between(b) for b in between) + "[/dim]", highlight=False)
        res = dict(result, code=code, stdout=out.getvalue(), stderr=err.getvalue(), json=want_json)
        if params.get("both"):
            res["payload"] = payload if error is None else {"ok": False, "error": error}
            res["text"] = _plain_text(args, payload, error)
        return res
    finally:
        if ctx is not None and ctx.locked:
            ctx.finish()
        _TL.ctx = None
        _forget_act_sessions()
        if gate is not None:
            _GATE.exit(gate)
        _TL.out = _TL.err = _TL.stdin = None
        _TL.consoles = {}


def _forget_act_sessions():
    """act.py caches a Session per device, including the saved snapshot state it
    read from disk. In a long-lived process that copy goes stale as soon as
    another command (`snapshot`, another client) saves a newer one, so mark it
    for re-reading after every command (about a millisecond)."""
    act = sys.modules.get("droidctl.act")
    for sess in list(getattr(act, "_SESSIONS", {}).values()):
        # not close(): other threads may be mid-command on these sessions
        sess._state_loaded = False


def _fmt_between(b):
    what = b.get("text") or b.get("title") or b.get("class") or ""
    return f'{b["type"]} "{what}"' + (f" ({b['pkg']})" if b.get("pkg") else "")


def _plain_text(args, payload, error):
    """The human rendering without colour: what MCP returns as text content."""
    if error is not None:
        return f"{error['kind']}: {error['message']}" + (f"\n{error['hint']}" if error.get("hint") else "")
    render = getattr(args, "render", None)
    if render is None or payload is None:
        return json.dumps(payload, indent=1, default=str)
    saved = (getattr(_TL, "out", None), getattr(_TL, "tty", False), getattr(_TL, "consoles", {}))
    buf = io.StringIO()
    _TL.out, _TL.tty, _TL.consoles = buf, False, {}
    try:
        render(payload)
    finally:
        _TL.out, _TL.tty, _TL.consoles = saved
    return buf.getvalue().rstrip("\n")


# --------------------------------------------------------------------------
# the server
# --------------------------------------------------------------------------
class Server:
    def __init__(self):
        self.stop = threading.Event()
        self.started = time.time()
        self.build = cl.build_id()
        self.inflight = 0
        self.subscribers = 0
        self.lock = threading.Lock()
        self.last_activity = time.monotonic()
        self.srv = None
        self.idle = cl.idle_seconds()

    # -- lifecycle --------------------------------------------------------
    def bind(self):
        from droidctl.core import UserError
        os.makedirs(cl.home(), mode=0o700, exist_ok=True)
        path = cl.sock_path()
        if cl.ping(timeout=1.0):
            raise UserError(f"a daemon is already running on {path}", "error",
                            hint="droidctl daemon status")
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)                   # a stale socket from a crash
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)                 # born 0600: no window where others can connect
        try:
            srv.bind(path)
        finally:
            os.umask(old)
        os.chmod(path, 0o600)
        srv.listen(64)
        srv.settimeout(0.5)                   # so the accept loop notices stop promptly
        self.srv = srv
        with open(cl.pid_path(), "w") as f:
            f.write(str(os.getpid()))

    def serve_forever(self):
        threading.Thread(target=self._watchdog, daemon=True, name="idle-watchdog").start()
        log.info("daemon %s (pid %d) listening on %s; idle exit %s", self.build, os.getpid(),
                 cl.sock_path(), cl._fmt_idle(self.idle))
        try:
            while not self.stop.is_set():
                try:
                    conn, _ = self.srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
        finally:
            self._cleanup()

    def shutdown(self, why):
        if not self.stop.is_set():
            log.info("shutting down: %s", why)
        self.stop.set()

    def _cleanup(self):
        deadline = time.monotonic() + 30
        while self.inflight and time.monotonic() < deadline:   # let running commands finish
            time.sleep(0.05)
        with contextlib.suppress(Exception):
            self.srv.close()
        POOL.reset()
        mine = False
        with contextlib.suppress(OSError, ValueError):
            mine = open(cl.pid_path()).read().strip() == str(os.getpid())
        if mine:                              # a newer daemon may already own these
            for p in (cl.sock_path(), cl.pid_path()):
                with contextlib.suppress(OSError):
                    os.unlink(p)
        log.info("daemon stopped")

    def _watchdog(self):
        if self.idle <= 0:
            return
        period = min(30.0, max(0.1, self.idle / 4))
        while not self.stop.wait(period):
            idle_for = time.monotonic() - self.last_activity
            if not self.inflight and not self.subscribers and idle_for > self.idle:
                self.shutdown(f"idle for {idle_for:.0f}s")

    # -- connections ------------------------------------------------------
    def _handle(self, conn):
        conn.settimeout(None)
        f = conn.makefile("rwb")
        try:
            while not self.stop.is_set():
                req = cl.recv(f)
                if req is None:
                    return
                rid, method, params = req.get("id"), req.get("method"), req.get("params") or {}
                if method == "subscribe":
                    self._subscribe(conn, f, rid, params)
                    return
                resp = self._dispatch(method, params)
                if rid is not None:
                    cl.send(f, dict(resp, jsonrpc="2.0", id=rid))
                if method == "shutdown" or resp.get("error", {}).get("code") == cl.RESTART:
                    return
        except (OSError, ValueError):
            pass
        finally:
            with contextlib.suppress(Exception):
                f.close()
            with contextlib.suppress(Exception):
                conn.close()

    def _dispatch(self, method, params):
        # only real work keeps the daemon alive: health checks (ping, status)
        # from scripts or `daemon status` must not defeat the idle exit
        if method == "ping":
            return {"result": {"ok": True, "pid": os.getpid(), "version": __version__, "build": self.build}}
        if method == "status":
            return {"result": self.status()}
        if method == "shutdown":
            self.shutdown("requested")
            return {"result": {"ok": True, "stopping": True}}
        if method == "run":
            build = params.get("build")
            if build and build != self.build:
                self.shutdown(f"a client of build {build} connected (this is {self.build})")
                return {"error": {"code": cl.RESTART, "message": "restart: the daemon is from another build",
                                  "data": {"daemon": self.build, "client": build}}}
            with self.lock:
                self.inflight += 1
            self.last_activity = time.monotonic()
            t0 = time.perf_counter()
            try:
                res = execute(params)
            finally:
                with self.lock:
                    self.inflight -= 1
                self.last_activity = time.monotonic()
            argv = params.get("argv") or []
            log.info("run %s %.1fms code=%s", " ".join(argv[:2]), (time.perf_counter() - t0) * 1000,
                     res.get("code"))
            return {"result": res}
        return {"error": {"code": -32601, "message": f"method not found: {method}"}}

    def _subscribe(self, conn, f, rid, params):
        """Turn this connection into a one-way stream of device events."""
        from droidctl import device as dev
        from droidctl.core import UserError
        try:
            env = params.get("env") or {}
            gate = _GATE.enter(env)
            try:
                serial = dev.resolve_serial(params.get("device") or env.get("ANDROID_SERIAL"))
            finally:
                _GATE.exit(gate)
            sess = POOL.get(serial)
        except UserError as e:
            cl.send(f, {"jsonrpc": "2.0", "id": rid,
                        "error": {"code": -32000, "message": f"{e.kind}: {e}"}})
            return
        q = _Sub(params.get("events"))
        sess.subs.add(q)
        with self.lock:
            self.subscribers += 1
        try:
            cl.send(f, {"jsonrpc": "2.0", "id": rid,
                        "result": {"subscribed": True, "serial": serial, "next": sess.last_seq}})
            while not self.stop.is_set():
                r, _, _ = select.select([conn], [], [], 0)
                if r and not conn.recv(1, socket.MSG_PEEK):
                    return                    # the subscriber hung up
                try:
                    ev = q.get(timeout=0.5)
                except queue.Empty:
                    continue
                cl.send(f, {"jsonrpc": "2.0", "method": "event", "params": dict(ev, serial=serial)})
        except OSError:
            pass
        finally:
            sess.subs.discard(q)
            with self.lock:
                self.subscribers -= 1
            self.last_activity = time.monotonic()

    def status(self):
        sessions = list(POOL.sessions.values())
        hits = sum(s.stats["hits"] for s in sessions)
        misses = sum(s.stats["misses"] for s in sessions)
        return {"ok": True, "running": True, "pid": os.getpid(), "version": __version__,
                "build": self.build, "uptime_s": round(time.time() - self.started, 1),
                "socket": cl.sock_path(), "log": cl.log_path(), "pidfile": cl.pid_path(),
                "idle_exit_s": self.idle, "inflight": self.inflight, "subscribers": self.subscribers,
                "devices": [s.describe() for s in sessions],
                "cache": {"hits": hits, "misses": misses,
                          "hit_rate": round(hits / (hits + misses), 3) if hits + misses else None}}


def serve():
    """`droidctl daemon start --foreground`: run until stopped. Blocks."""
    from droidctl import device as dev
    real_err = sys.stderr
    handler = logging.StreamHandler(real_err)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.handlers[:] = [handler]
    log.setLevel(logging.INFO)
    log.propagate = False
    server = Server()
    server.bind()
    os.environ.pop("ANDROID_SERIAL", None)    # per request (the client's), never the spawner's
    _install_stdio()
    dev._connect_hook = _pool_connect
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        with contextlib.suppress(ValueError):  # only possible on the main thread
            signal.signal(sig, lambda *_: server.shutdown("signal"))
    try:
        server.serve_forever()
    finally:
        dev._connect_hook = None
    return server


# --------------------------------------------------------------------------
# control (the `daemon` command, in-process)
# --------------------------------------------------------------------------
def status():
    info = cl.ping()
    if not info:
        return {"ok": True, "running": False, "socket": cl.sock_path(), "log": cl.log_path()}
    c = cl._Conn(timeout=5)
    try:
        return c.request("status").get("result")
    finally:
        c.close()


def start():
    running = cl.ping()
    pid = cl.ensure_daemon(autostart=True)
    if pid is None:
        return {"ok": True, "started": False, "pid": (running or cl.ping() or {}).get("pid"),
                "socket": cl.sock_path()}
    return {"ok": True, "started": True, "pid": pid, "socket": cl.sock_path(), "log": cl.log_path()}


def stop():
    info = cl.ping()
    if info:
        cl._shutdown_and_wait()
        return {"ok": True, "stopped": True, "pid": info.get("pid")}
    pid = None
    with contextlib.suppress(OSError, ValueError):
        pid = int(open(cl.pid_path()).read().strip())
        os.kill(pid, signal.SIGTERM)
        return {"ok": True, "stopped": True, "pid": pid, "note": "signalled via the pidfile"}
    return {"ok": True, "stopped": False, "note": "no daemon was running"}


def tail_log(lines=50):
    try:
        with open(cl.log_path(), "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 256 * 1024))
            text = f.read().decode("utf-8", "replace")
    except FileNotFoundError:
        return {"ok": True, "path": cl.log_path(), "lines": []}
    return {"ok": True, "path": cl.log_path(), "lines": text.splitlines()[-lines:]}


def serve_stdio():
    """`droidctl serve --stdio`: our daemon JSON-RPC, bridged over stdin/stdout.

    Not MCP (that is `droidctl mcp`). Requests are forwarded as they are, except
    that a `run` without cwd/env/build gets this process's."""
    cl.ensure_daemon(os.environ.get("DROIDCTL_AUTOSTART") != "0")
    c = cl._Conn()
    out = sys.stdout.buffer

    def pump():
        with contextlib.suppress(OSError, ValueError):
            for line in c.f:
                out.write(line)
                out.flush()

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    for line in sys.stdin.buffer:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            out.write(json.dumps({"jsonrpc": "2.0", "id": None,
                                  "error": {"code": -32700, "message": "parse error"}}).encode() + b"\n")
            out.flush()
            continue
        if msg.get("method") == "run":
            p = msg.setdefault("params", {})
            defaults = cl._run_params(p.get("argv") or [])
            defaults["tty"] = False
            for k, v in defaults.items():
                p.setdefault(k, v)
        cl.send(c.f, msg)
    with contextlib.suppress(OSError):
        c.sock.shutdown(socket.SHUT_WR)
    t.join(timeout=30)
    c.close()


# --------------------------------------------------------------------------
# commands: daemon, serve, mcp (always in-process: see client.LOCAL)
# --------------------------------------------------------------------------
def cmd_daemon(a):
    from droidctl.core import UserError
    if a.action == "start":
        if a.foreground:
            serve()
            return {"ok": True, "stopped": True}
        return start()
    if a.action == "stop":
        return stop()
    if a.action == "restart":
        stopped = stop()
        return dict(start(), restarted=bool(stopped.get("stopped")))
    if a.action == "status":
        return status()
    if a.action == "logs":
        return tail_log(a.lines)
    raise UserError(f"unknown daemon action {a.action!r}", "bad-args")


def render_daemon(p):
    from droidctl.core import console
    if "lines" in p:
        for line in p["lines"]:
            print(line)
        if not p["lines"]:
            console.print(f"[dim]no log yet ({p['path']})[/dim]")
        return
    if p.get("running") is False:
        console.print(f"daemon: [yellow]not running[/yellow]  [dim]socket {p['socket']}  log {p['log']}[/dim]")
        return
    if "uptime_s" in p:
        cache = p.get("cache") or {}
        rate = cache.get("hit_rate")
        console.print(f"daemon: [green]running[/green] pid {p['pid']}  up {p['uptime_s']}s  "
                      f"version {p['version']}  idle-exit {cl._fmt_idle(p['idle_exit_s'])}", highlight=False)
        console.print(f"  socket {p['socket']}\n  log    {p['log']}", highlight=False)
        console.print(f"  cache  {cache.get('hits', 0)} hits / {cache.get('misses', 0)} misses"
                      + (f" ({rate:.0%} hit rate)" if rate is not None else "")
                      + f"   subscribers {p['subscribers']}   in flight {p['inflight']}", highlight=False)
        for d in p.get("devices") or []:
            state = "connected" if d["connected"] else "offline"
            console.print(f"  device {d['serial']}  {state}  tcp:{d['port']}  "
                          f"events {'subscribed' if d['subscribed'] else 'NOT subscribed'}  "
                          f"hits {d['cache']['hits']} misses {d['cache']['misses']}", highlight=False)
        return
    if p.get("started"):
        console.print(f"daemon: [green]started[/green] pid {p['pid']}  [dim]log {p.get('log')}[/dim]", highlight=False)
    elif "started" in p:
        console.print(f"daemon: already running (pid {p.get('pid')})", highlight=False)
    elif p.get("stopped"):
        console.print(f"daemon: stopped (pid {p.get('pid')})", highlight=False)
    else:
        console.print(f"daemon: {p.get('note', 'not running')}")


def cmd_serve(a):
    from droidctl.core import UserError
    if not a.stdio:
        raise UserError("serve needs --stdio (for MCP use: droidctl mcp)", "bad-args")
    serve_stdio()
    return None


def cmd_mcp(a):
    from droidctl import mcp
    if a.install:
        return mcp.install(a.install, dry_run=a.dry_run, force=a.force)
    mcp.run_stdio()
    return None


def render_mcp(p):
    from droidctl.core import console
    if not p:
        return
    for r in p.get("clients", []):
        console.print(f"{r['client']}: {r['action']}  [dim]{r.get('path', '')}[/dim]", highlight=False)
        if r.get("backup"):
            console.print(f"  [dim]backup: {r['backup']}[/dim]")
        if r.get("detail"):
            console.print(f"  {r['detail']}", highlight=False)


def add_parsers(sub, jsonopt):
    sp = sub.add_parser("daemon", parents=[jsonopt],
                        help="the background daemon: start, stop, restart, status, logs")
    sp.add_argument("action", choices=["start", "stop", "restart", "status", "logs"])
    sp.add_argument("--foreground", action="store_true", help="start: run in this process (what auto-start spawns)")
    sp.add_argument("--lines", type=int, default=50, metavar="N", help="logs: how many lines (default 50)")
    sp.set_defaults(fn=cmd_daemon, render=render_daemon)

    sp = sub.add_parser("serve", parents=[jsonopt],
                        help="bridge the daemon's JSON-RPC over stdin/stdout (not MCP: see `mcp`)")
    sp.add_argument("--stdio", action="store_true", help="speak NDJSON JSON-RPC on stdin/stdout")
    sp.set_defaults(fn=cmd_serve)

    sp = sub.add_parser("mcp", parents=[jsonopt],
                        help="run the MCP server on stdio, or --install it into an agent's config")
    sp.add_argument("--install", choices=["claude", "codex", "cursor", "all"],
                    help="add droidctl to that client's MCP config (keeps the other servers)")
    sp.add_argument("--dry-run", action="store_true", help="--install: only print what would change")
    sp.add_argument("--force", action="store_true", help="--install: replace an existing droidctl entry")
    sp.set_defaults(fn=cmd_mcp, render=render_mcp)
