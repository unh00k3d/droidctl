# droidctl daemon: socket API and lifecycle

The daemon is droidctl's resident broker. The `droidctl` command is a thin client
(`droidctl/client.py`, stdlib only) that sends each argv to it and prints the reply.
The daemon keeps the per-device state that one-shot calls can't:

- a warm agent connection per device;
- a permanent event subscription per device;
- a tree cache that is valid by construction;
- the events that happened between calls.

Everything here is language-neutral, so a Go thin client can implement it (PLAN.md,
"Go/Rust: port only the thin client").

Code: `droidctl/daemon.py` (server, sessions, cache) and `droidctl/client.py` (client,
auto-start, `Client` SDK). Tests: `tests/test_daemon.py`, which runs real processes
against `tests/fakes.py`.

## Files
All paths live under `DROIDCTL_HOME` (default `~/.droidctl`).

| file | what |
|---|---|
| `d.sock` | the unix socket, mode 0600 (created under `umask 0177`, so it is never briefly open) |
| `daemon.pid` | the pid. It is removed on exit only if it still names this process |
| `daemon.lock` | `flock`ed around auto-start, so two simultaneous first calls can't start two daemons |
| `daemon.log` | the daemon's stdout/stderr: start, stop, one line per command (`run tap 4 312.0ms code=0`), crashes with tracebacks |

## Framing
- NDJSON: one JSON-RPC 2.0 object per line, UTF-8, in both directions.
- A connection is persistent and carries any number of requests.
- Requests on one connection are answered in order.
- A `subscribe` request turns its connection into a one-way event stream.

## Methods

### `ping`
`{}` → `{ok, pid, version, build}`. It does **not** count as activity, so health checks
don't hold off the idle exit.

### `status`
`{}` → a status object:

| field | meaning |
|---|---|
| `ok`, `running`, `pid`, `version`, `build`, `uptime_s` | identity |
| `socket`, `log`, `pidfile` | paths |
| `idle_exit_s`, `inflight`, `subscribers` | lifecycle |
| `cache` | `{hits, misses, hit_rate}` over all devices |
| `devices` | per device: `serial`, `port`, `connected`, `subscribed`, `agent`, `cache {hits, misses, gen_checks, primed, hit_rate}`, `events_seen`, `idle_s` |

### `shutdown`
`{}` → `{ok, stopping}`. The daemon stops accepting connections, waits up to 30 s for
commands in flight, closes device connections and removes its socket and pidfile.

### `run`
Runs one command line exactly like the in-process CLI: same parser, same command
functions, same output text and error vocabulary.

Params:

| param | meaning |
|---|---|
| `argv` | the command line, e.g. `["tap", "4", "--json"]`. A `--no-daemon` token is ignored |
| `build` | the client's build id (`<version>+<newest source mtime>`); see "Version handshake" |
| `cwd` | the client's working directory; relative paths (`--out`, `--fixture`, `run FILE`, `dump-fixture --dir`) resolve against it |
| `env` | the client's `ANDROID_SERIAL`, `ANDROID_ADB_SERVER_PORT`, `NO_COLOR`, `COLUMNS`, `TERM` and `DROIDCTL_*` (except `HOME`/`IDLE`/`NO_DAEMON`/`AUTOSTART`, which belong to the daemon). Keys that are absent are unset for the command |
| `stdin` | text for commands that read stdin (`type --stdin`, `run` without `--step`/FILE). The client sends it only then |
| `tty`, `width` | render the human text for this terminal: colour only if `tty`, at `width` columns |
| `both` | also return `payload` (the JSON value) and `text` (the uncoloured human rendering). MCP uses this |

Result:

| field | meaning |
|---|---|
| `code` | the exit code: 0 ok, 1 failure (or a payload with `ok:false`), 2 usage error, or the command's own |
| `stdout`, `stderr` | exactly what the in-process CLI would print. With `--json`, stdout is one JSON value that carries `"mode":"daemon"`, and the error envelope `{"ok":false,"error":{kind,message,hint?,data?},"mode":"daemon"}` |
| `json` | whether the command ran with `--json` |
| `mode` | `"daemon"` |
| `payload`, `text` | only with `both` |
| `passthrough: true` | returned instead for `daemon`, `serve` and `mcp`, which the client must run itself |

**Events between calls.** If toasts, window changes, notifications or announcements
arrived for a device after the previous command on that device ended and before this
one started, the payload carries `between_calls: [{type, pkg?, class?, title?, text?}]`
(at most 20). The human output ends with a dim `between calls: toast "Saved!"` line.
Each event is reported once.

### `subscribe`
`{device?, events?:[types], cwd?, env?}` → `{subscribed, serial, next}`, then a stream
of `{"jsonrpc":"2.0","method":"event","params":{serial, seq, t, gen, type, …}}`
notifications until the client disconnects.
- `device` is resolved like `-d`, using the client's `env`.
- Event types are the agent's (PROTOCOL.md "Events"). They come from the daemon's
  permanent subscription, so extra subscribers cost the phone nothing.
- An open subscription holds off the idle exit.

### Errors
| code | meaning |
|---|---|
| `-32010` | restart: the daemon is from another build. After replying, the daemon exits. `data {daemon, client}` |
| `-32601` | unknown method |
| `-32000` | `subscribe` could not resolve the device; the message is `kind: text` |

Command failures are **not** JSON-RPC errors. They are results with `code` 1 and the
usual typed error envelope.

## Lifecycle

**Auto-start.** The thin client handles each call as follows:
1. It connects to `d.sock` and sends `run`. That is one connect and one request in the
   common case.
2. If the socket is missing or refuses the connection (a stale socket from a crash), it
   takes `flock(daemon.lock)` and re-checks with `ping`.
3. If still needed, it spawns `python -m droidctl daemon start --foreground` detached:
   `start_new_session=True`, stdin `/dev/null`, stdout/stderr appended to `daemon.log`,
   cwd `~`.
4. It polls `ping` every 50 ms for up to 5 s, then sends the command.

Only the call that spawned the daemon says so:
- human mode prints one line on stderr: `droidctl: started background daemon (pid N,
  idle-exit 30m) — 'droidctl daemon stop' to stop, DROIDCTL_NO_DAEMON=1 to never start it`;
- `--json` mode puts `"daemon":{"started":true,"pid":N}` in the result instead, and
  stdout stays one JSON value.

**Version handshake.** `run` carries the client's `build`.
- The build id includes the newest mtime of the package sources, so an edited checkout
  counts as a new build.
- On a mismatch the daemon replies `-32010` and exits. The client then starts its own
  build and resends, once.
- Two installs of different builds sharing one `DROIDCTL_HOME` would keep replacing each
  other's daemon. Give each its own home.

**The daemon exits when:**
- `droidctl daemon stop` runs, or the daemon gets SIGTERM/SIGINT/SIGHUP;
- it has run no command and had no subscriber for `DROIDCTL_IDLE` (default `30m`;
  accepts `90`, `90s`, `30m`, `2h`; `0` means never);
- a client of another build connects.

After a crash, the next call finds a dead socket and starts a new daemon.

**Opting out:**
- **`--no-daemon` or `DROIDCTL_NO_DAEMON=1`:** runs the command in-process. No
  background process is started, and nothing is written under `DROIDCTL_HOME` by the
  daemon machinery. There is no cache and no event history. Results carry
  `"mode":"inprocess"`.
- **`DROIDCTL_AUTOSTART=0`:** instead of spawning, the call fails with the typed error
  `no-daemon` and a hint.
- **Sandbox fallback:** if the socket can't be used or the daemon can't be started (a
  sandbox that forbids unix sockets or kills detached children), the client runs the
  command in-process. It says so on stderr in human mode; the result carries
  `"mode":"inprocess"`.

**Control:** `droidctl daemon start|stop|restart|status|logs [--lines N]`, with
`--json` on all of them. These, `serve` and `mcp` always run in the calling process.

## Concurrency
- **One thread per client connection.** Command output is thread-local: the process's
  `sys.stdout`/`sys.stderr`/`sys.stdin` are proxies that route to the running request's
  buffers, and droidctl's rich consoles are built per request for the client's terminal.
- **The working directory and environment are process-wide**, so they are kept out of
  the way:
  - The client's `ANDROID_SERIAL` becomes the command's `-d`; the daemon never sets it
    in its environment.
  - Path arguments (`--out`, `--file`, `--dir`, `--fixture`, `run FILE`, `install APK`)
    are made absolute against the client's `cwd`. Only `run`, whose steps may hold
    relative paths, needs the process cwd.
  - What is left is applied under a gate (`_Gate`): `DROIDCTL_*` settings, the adb
    server port, and `run`'s cwd. A request that needs other values waits until no
    request with different ones is running.
  - So two agents on different phones, or in different directories, never wait for
    each other.
- **Per device:**
  - A mutating command holds that device's command lock for its whole run, so two
    agents' taps on one phone never interleave. It waits at most 120 s, then fails with
    `timeout`.
  - Read-only commands (`snapshot`, `where`, `current`, `wait`, `watch`, `shot`, `logs`,
    `devices`, `doctor`, `ping`, …; `READ_ONLY` in `daemon.py`) take no lock.
  - Device calls are serialized per connection.
  - Device-side blocking waits (`wait_for`, `wait_idle`) use a connection of their own,
    so a 10 s `wait` never holds up anyone else.
- **Phone A never blocks phone B.** Sessions are created and used under per-device
  locks.

## Device sessions and the tree cache
**Session setup.** `device.connect()` has a hook. Inside the daemon it returns a
`PooledClient` over the device's `Session`. It has the same API as `AgentClient`, and
`close()` only drops that caller's subscription. So `act.get_session()`, `snapshot` and
every other command get the warm connection without knowing about the daemon.

A `Session` holds:
- the main agent connection;
- a second connection subscribed to all agent events;
- a ring of the last 1000 events;
- the cached **latest dump**.

The cache only ever holds the device's latest dump, the only one whose handles are
valid.

**When a cached tree is served:**
- **Dirty flag:** every pushed event carries the agent's content generation `gen`.
  Once an event's `gen` exceeds the cached tree's `gen` (read on the device before that
  dump), the cache is dirty and the next `tree` is a miss.
- **Zero round trips (a "pure hit")** needs all of these:
  - the cache is not dirty;
  - the subscription is alive;
  - the tree is *quiet*;
  - it was last verified less than 5 s ago (TTL).
- **Quiet** means a `gen` check at least 0.4 s after the dump saw no change. Why that
  delay matters: the agent compacts a burst of content events from one package
  within 250 ms into a single event and pushes only the first one. Once 250 ms have
  passed without a change, any later change starts a new burst, and that burst *is*
  pushed. So pushes are only trusted after a verified quiet gap.
- **A `gen` check** (one ~8 ms round trip on the dev phone) serves the tree whenever it
  isn't quiet yet, the TTL expired, or the subscription is down.
- **Anything else is a miss:** a fresh `tree`.

**Invalidation:**
- An action whose reply carries a settled `tree` (`act`, `gesture`, `global` with
  `settle`) replaces the cache with it (`primed`). So a tap followed by a snapshot costs
  no dump.
- An action without a settled tree, a `stale` error from the device, a degraded dump or
  a lost connection drops the cache.
- A lost connection is re-established once for read-only calls, never for actions, so
  an action is never sent twice.
- After `setup`/`teardown`, all sessions are dropped, because the agent may have been
  restarted or removed.
- act.py's per-device `Session` caches the saved snapshot state (refs). The daemon marks
  it for re-reading after every command, so a `snapshot` from another client is never
  shadowed by a stale in-memory copy.

## Other clients
- **`droidctl serve --stdio`:** this API bridged over stdin/stdout, one line each way. A
  `run` missing `cwd`/`env`/`build` gets the bridge process's. This is **not** MCP.
- **`droidctl mcp`:** the MCP server (official SDK, stdio), a client of the daemon.
  Tools are generated from the parser; see `droidctl/mcp.py`. Install it with
  `droidctl mcp --install claude|codex|cursor|all [--dry-run] [--force]`. That keeps
  other servers and backs up any file it changes.
- **Python SDK:**
  ```python
  from droidctl.client import Client, DroidctlError
  with Client() as c:                        # auto-starts the daemon
      snap = c.call("snapshot")              # the --json payload; DroidctlError(.kind) on failure
      c.call(["tap", "4"])
      for ev in c.subscribe(events=["toast"], timeout=30):
          print(ev["text"])
  ```

## Measured (2026-09-27, `bench/m6_daemon.py`)
The host side, with a fake agent that answers instantly. Device time comes on top; see
`bench/results/m1-rtt.json` and `m5-device.json`.

| path | median | budget |
|---|---|---|
| `python -c pass` (the floor of any Python CLI call) | 27.4 ms | |
| `droidctl version --json` through the daemon | 34.3 ms (≈7 ms over bare Python) | CLI → daemon overhead < 35 ms |
| the same in-process (`--no-daemon`) | 61.8 ms | |
| resident `Client.run(version)` | 0.33 ms | daemon overhead < 3 ms |
| resident `Client.run(snapshot)`, cache hit | 3.5 ms | < 5 ms + client boot |
| `droidctl snapshot` through the daemon, cache hit | 38.5 ms | |
| `droidctl snapshot` in-process (connect, ping, tree, build) | 74.7 ms + device time | |
