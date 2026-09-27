# droidctl — agent-first Android CLI (plan v3: own APK from day one)
_Last updated 2026-09-27. Companion specs: `TESTAPP.md` (edge-case test app), `research/` (prior-art reports). Specs to be written during the build: `android/agent/PROTOCOL.md` (M1), `DAEMON.md` (M6)._

## Decisions at a glance
| topic | decision |
|---|---|
| repo | new sibling repo `~/Documents/droidctl`, MIT, same conventions as chromectl |
| platform (v1) | physical Android phones; emulators should work but aren't the focus; iOS is future work |
| device side | **our own accessibility-service APK** (Kotlin, no deps, no INTERNET permission) from day one; no uiautomator2/Appium |
| host side | Python: a resident **daemon** holds all logic and state, plus a thin CLI client that auto-starts it; optional Go thin client later (gated by measurement) |
| interfaces | CLI + skill (default for shell agents), a real **MCP server** (`droidctl mcp`), raw JSON-RPC (`serve --stdio`, unix socket, `Client`) |
| core behaviour | compact snapshot with refs; refs are re-resolved locators; node actions, not coordinates; every action verified with a diff |
| layout | spatial layer (regions, rows, grids, inferred labels, opt-in geo/map/marks) **provisional until benchmarked** |
| testing | purpose-built edge-case test app with logcat ground truth, golden fixtures, coverage gate, benchmarks |


## Context
Appium is slow and noisy for agents. Full UI dumps (30–100k tokens) make agents worse, and screenshot/coordinate tools like mobile-mcp mis-tap and eat context. `droidctl` is a sibling of chromectl with the same conventions: `--json` everywhere, typed `error.kind`, `run` batching, `cheat`/`skill`, and one AGENTS.md as the whole interface.

Core ideas:
1. **Compact snapshot**: actionable or labelled elements only, with refs, at about 1–2k tokens per screen.
2. **Refs are locators, not coordinates**: re-resolved against the live UI and acted on through **accessibility actions on the node itself**. Every action reports what changed.

**Decision (v3):** build our own small accessibility-service APK from the start, instead of uiautomator2. Research (see `research/`) showed that uiautomator2:
- can't click by node;
- kills its server when Python exits, costing 1–3 s per call;
- has no scroll/editable/window info;
- blocks on waits.

No existing tool clicks by element; all of them end in coordinate taps. Owning the device side is what makes the core ideas possible.

Alternative considered: a scrcpy-style `app_process` jar using UiAutomation. It needs no install or settings change, but it conflicts with Appium/u2, dies on reboot, and has no persistent event stream. Rejected, but still possible later as a fallback backend.

## Architecture: thin device, smart host
```
clients: droidctl CLI (thin) | droidctl mcp | serve --stdio | Client()/SDK
                     │  ~/.droidctl/d.sock (unix, 0600, NDJSON JSON-RPC)
                     ▼
droidctl daemon (Python, host)                  droidctl-agent.apk (Kotlin, phone)
 run / --json / errors / registry                AccessibilityService
 snapshot: prune, refs, format, diff  <-- raw -- tree: full-fidelity JSON of all windows
 resolve: fingerprint -> handle        tree      handles: id -> AccessibilityNodeInfo (per generation)
 act: verify, settle, fallback policy  -- act -> actions: ACTION_CLICK/SET_TEXT/SCROLL/custom, gestures
 cache + event ring (per device)     <- events-- events: generation counter, clicks, toasts, window changes
      one persistent socket per device == adb forward tcp:N localabstract:droidctl == LocalServerSocket
      (adb/adbutils only for setup and reconnects, never on the hot path)
```
- **The device side is dumb and fast.** It dumps raw data, performs actions on handles and records events.
- **The logic lives in Python on the host**: pruning, formatting, resolving and verify policy. It is testable offline against captured raw trees, and changing it doesn't require reinstalling the APK.

### Transport and security
- **Socket:** `LocalServerSocket("droidctl")` (abstract unix socket), reached with `adb forward tcp:N localabstract:droidctl`, carrying newline-delimited JSON-RPC 2.0 (requests plus server-pushed notifications). The daemon keeps one persistent connection per device. In `--no-daemon` mode each CLI call opens its own connection, and `run` keeps one open for all steps.
- **Auth is by peer UID.** Accept only `getPeerCredentials().uid` ∈ {2000 (shell), 0 (root: adbd after `adb root` or on rooted/custom-ROM devices; anything already running as root on the phone is unrestricted anyway)}. adbd runs as shell on production builds, so only adb-forwarded connections get through and other apps on the phone are rejected. No token needed. **Verify on the user's phone in M1**; fall back to an adb-broadcast token (Artemis scheme) if needed.
- **The APK declares no `INTERNET` permission** (in fact no `<uses-permission>` at all), so it provably can't send data anywhere. Its power comes from being *enabled as an accessibility service* (which `setup` does and `teardown` undoes), not from a manifest permission; `BIND_ACCESSIBILITY_SERVICE` only restricts who may bind to it (the system). The README must say exactly this, not "no permissions".
- **Versioning:** the host bundles the APK and compares `versionCode` from `ping`. If they differ, it auto-upgrades with `install -r` (same committed debug key).

### Service config (`res/xml/accessibility_service.xml`)
- `typeAllMask`, `flagRetrieveInteractiveWindows`, `flagReportViewIds`, `flagIncludeNotImportantViews` (toggleable per dump).
- `canRetrieveWindowContent`, `canPerformGestures`, `canTakeScreenshot` (API 30+).
- `notificationTimeout=50`, `isAccessibilityTool=true` (so `accessibilityDataSensitive` views stay visible on API 34+).
- minSdk 26, targetSdk 35. Kotlin, **no AndroidX or other dependencies**. Target APK size under 100 KB.

## Device API (JSON-RPC methods)
| method | what |
|---|---|
| `ping` | version, sdk, device model, screen size and density, service state, current generation |
| `tree {not_important?, windows?}` | full-fidelity tree of **all** windows (see schema); returns `gen` and handles |
| `act {handle, gen, action, args, settle?}` | `node.refresh()`, check it is still visible and enabled, then `performAction`; returns `{performed, clicked_event}`. With `settle {quiet_ms, timeout_ms}` it also waits for idle and returns the **new tree in the same response** (one round trip for tap-and-see) |
| `gen` | the current content generation counter, a cheap check for cache validity |
| `gesture {type: tap/long/swipe/pinch/path, points, ms}` | `dispatchGesture`; waits for its completion callback |
| `global {name}` | back, home, recents, notifications, quick_settings, power_dialog, lock, screenshot, split |
| `wait_idle {quiet_ms, timeout_ms}` | blocks until no content-change events for `quiet_ms`; returns events seen |
| `wait_for {text\|id\|gone\|activity\|toast\|window, timeout_ms}` | blocks on the device until the condition holds (no host polling) |
| `subscribe {events:[…]}` / `unsubscribe` | push JSON-RPC notifications (toast, window, content, ime, crash, clicked) on this connection |
| `events {since}` | ring buffer: clicked, text-changed, window-state/content-changed, toasts, announcements, IME show/hide |
| `screenshot {scale, quality, crop?}` | API 30+ `takeScreenshot`, downscaled JPEG made on the device; error `secure-window` on FLAG_SECURE; host falls back to `screencap` |
| `current` | foreground package/activity (from window-state events) and whether the keyboard is shown |

**Raw node schema.** For each node:
- `handle`, `window {id, type, layer, title}`, `class`, `pkg`, `id` (viewIdResourceName), `uid` (API 33 `getUniqueId`);
- `text`, `desc`, `hint`, `error`, `state` (stateDescription 30+), `tooltip`, `pane`;
- `bounds` (screen), `visible`, `drawingOrder`;
- `flags`: clickable, longClickable, checkable, checked, focusable, focused, selected, enabled, editable, password, scrollable, heading, showingHint;
- `actions` (standard ids + **custom action labels**);
- `collection {rows, cols}` / `item {row, col}`, `range {min, max, cur}`, `inputType`;
- `children`.

This fills every gap the research found in uiautomator2's XML: scroll hints come from the SCROLL_FORWARD/BACKWARD actions, list size from `collection`, plus editable and window types.

**Handles:** each `tree` call bumps a dump id and maps handle → `AccessibilityNodeInfo` for the latest dump only. `act` with an old dump id fails with `stale` instead of acting on the wrong node.

**Robustness:**
- Each tree read has a 2 s budget. If exceeded, it returns the last good tree with `degraded:true` (Portal idea).
- A null root is retried once.
- Every call on the device is wrapped so the service never crashes.

## Host (Python) layout
```
droidctl/
  pyproject.toml   # deps: adbutils, rich; APK bundled as package data
  AGENTS.md  README.md  NOTICE  PLAN.md  TESTAPP.md  DAEMON.md  research/
  android/         # Gradle project (+ gradlew); `make apk`
  android/agent/   # the accessibility-service APK; PROTOCOL.md (device JSON-RPC contract)
  android/testapp/ # edge-case lab app (~70 scenarios, Views + Compose); spec in TESTAPP.md
  bench/           # latency, token, tap-accuracy and spatial benchmarks; results/*.json
  droidctl/
    cli.py  core.py      # core ported from chromectl (UserError/ERROR_KINDS/emit/die, _surface/cheat/skill, run)
    device.py            # adbutils: device select, forward, install/enable/upgrade, JSON-RPC client
    snapshot.py          # PURE: raw tree -> prune -> refs/lines; signature; diff
    resolve.py           # PURE: fingerprint -> candidate handle (tiers, scoring, failure reasons)
    act.py               # action policy, verify/settle, text ladder
    daemon.py            # resident broker: unix socket, per-device sessions, subscription, tree cache
    client.py            # thin CLI entrypoint (stdlib only): argv -> daemon, auto-start, --no-daemon fallback
    mcp.py               # MCP server (official SDK, optional extra), a client of the daemon
    spatial.py           # PURE: regions, row grouping, grids, label inference, --geo/--map rendering
    assets/droidctl-agent.apk
  tests/
    fixtures/trees/*.json   # REAL raw trees captured via `droidctl dump-fixture`
    test_snapshot.py  test_spatial.py  test_resolve.py  test_unit.py  test_daemon.py  test_mcp.py
    e2e/                    # per TESTAPP.md group, only with DROIDCTL_SERIAL
```

## Performance architecture (latency is a feature)
Appium and mobile-mcp are slow because of *layers and setup per call*, not because of the host language:
- HTTP → WebDriver → driver → device server stacks;
- session creation;
- `waitForIdle`;
- a process spawned per operation (mobilecli);
- a device probe and screenshot on every call (mobile-use);
- a fresh HTTP client each time (droidrun).

Measured on this machine (2026-09-27):

| | per call |
|---|---|
| native binary | 3–5 ms |
| `python3` bare | ~18 ms |
| Python + json/socket/argparse | ~30 ms |
| chromectl cold command | ~45 ms |
| chromectl resident `Client` | ~1.2 ms |

On the phone, an a11y tree read is an estimated 20–150 ms and settle ~150–300 ms (measured in M1). **The device and the settle policy dominate; the host language is second-order.**

**1. A resident broker: `droidctl daemon` holds everything warm.**
```
 agent (bash)  ── droidctl tap … ──┐   thin client: argv → socket → print
 agent (MCP)   ── droidctl mcp ────┤
 raw stdio     ── serve --stdio ───┤
 agent/SDK     ── Client() ────────┼──►  ~/.droidctl/d.sock (0600, NDJSON JSON-RPC)
 watch --max N ── subscribe ───────┘              │
                                                  ▼
                             droidctl daemon (Python, one per user)
                              per device: warm device socket + subscription,
                              event ring, tree cache + dirty flag, refs/fingerprints
                              in memory, per-device lock, parser built once
                                                  │  adb forward (persistent)
                                                  ▼
                              droidctl-agent.apk (device state: handles, events, idle)
```
**Why it beats one-shot calls.** It is more than skipping Python boot:
- **Snapshot cache that is always valid.** The daemon keeps a permanent `subscribe` to content/window events. If nothing changed since the last dump, `snapshot` is served from memory: 0 device round trips, ~1 ms. Every settled `act` response already carries the new tree, so the *next* snapshot is usually a cache hit. A safety TTL (e.g. 5 s) plus a cheap device `gen` check guard against missed events.
- **Event history between calls.** Toasts, crash dialogs and window changes that happened *while the agent was thinking* are buffered and reported ("toast while idle: …"). A one-shot CLI misses them.
- **Nothing re-read or re-built per call.** Refs and fingerprints stay in memory (the state file is written through for crash recovery), the argparse tree is built once (~11 ms in chromectl), and the device socket stays warm.
- **Concurrency.** Several agents or terminals share a phone safely, with one lock per device. Reads served from the cache don't block, and phone A never blocks phone B (fixing chromectl's single-threaded accept loop).

**Clients**, all speaking the same JSON-RPC on the unix socket:
- **CLI (`droidctl <cmd>`):** a *thin* entrypoint that imports only `sys/os/socket/json`. If the daemon is running, it sends `{"argv": [...]}` (or structured params) and prints the result. If not, it **auto-starts** the daemon (`spawn_background`, from chromectl) and retries, or with `--no-daemon` runs in-process using the same code. It is still correct without the daemon, just slower.
- **Resident agents and the SDK:** connect once and call many times (chromectl's `Client` pattern). They can also `subscribe` and get pushed events.
- **Raw stdio:** `droidctl serve --stdio` bridges our JSON-RPC over stdio to the daemon. This is **not MCP**; MCP is `droidctl mcp` (see the MCP section).

**Reused from chromectl `daemon.py`:**
- NDJSON framing over a buffered file (`_send`/`_recv`);
- persistent connections;
- ping/status/shutdown ops;
- the 0600 socket with stale-socket cleanup;
- the pidfile;
- detached spawn;
- `NON_ROUTABLE`-style rules.

**Changed from chromectl:**
- **Commands return payload dicts,** and `emit` happens only at the edge. The daemon returns structured results, not captured stdout.
- **A thread per connection with per-device locks,** instead of a serial accept loop.
- **Version handshake:** the client sends its version, and a stale daemon from before an upgrade restarts itself.
- **Idle exit** after 30 min (configurable). Logs go to `~/.droidctl/daemon.log`.

**Daemon lifecycle (exact):**
- **Auto-start.**
  1. Any device command (`launch`, `snapshot`, `tap`, …) runs the thin client, which connects to `~/.droidctl/d.sock`.
  2. If the socket file is missing, the connect is refused (a stale socket from a crash), or the ping fails, the client takes `flock(~/.droidctl/daemon.lock)` so two simultaneous first calls can't spawn two daemons. chromectl's check-then-bind is racy.
  3. It spawns `python -m droidctl daemon start --foreground` **detached**: `start_new_session=True` (its own session, so it survives the calling shell or agent tool call ending and ignores the terminal's Ctrl-C), stdin=/dev/null, stdout/stderr → `~/.droidctl/daemon.log`.
  4. It polls ping every 50 ms, up to 5 s, then sends the original command.
  - Only this first call pays the daemon boot (Python imports plus the device connect, estimated ~200–400 ms). Every later call reuses it.
- **Lazy per device.** The daemon connects to a phone on the first request for that serial: it reuses the cached forward port, subscribes to events and starts the cache. If a phone is unplugged, it marks that device offline; the next request re-forwards and reconnects, or returns E `no-device`.
- **The daemon exits when:**
  - `droidctl daemon stop` is run;
  - it has been idle for 30 min with no requests and no subscribers (`DROIDCTL_IDLE=…`);
  - a client of a different version connects (it replies "restart", exits, and the client respawns the right version);
  - the laptop reboots.
  - After a crash, the next call finds a dead socket and respawns it (the log is kept).
- **Opting out:**
  - `--no-daemon` / `DROIDCTL_NO_DAEMON=1` runs everything in-process and never starts a background process (slower, no cache or event history);
  - `DROIDCTL_AUTOSTART=0` returns E `no-daemon` with a hint instead of spawning, for users who want to start it explicitly.
- **Sandboxes:** if spawning or binding the socket fails (a sandboxed agent that kills detached children or forbids unix sockets), the client **falls back to in-process mode automatically** and says so in the result (`"mode":"inprocess"`).
- **Control:** `droidctl daemon start|stop|restart|status|logs`. `status` shows pid, uptime, version, connected devices, cache hit rate and subscriber count.

**Making the daemon visible (required, not optional).** A background process that starts itself must never be a surprise:
- **First-start notice.** When a call spawns the daemon, it prints one line on stderr: `droidctl: started background daemon (pid 4242, idle-exit 30m) — 'droidctl daemon stop' to stop, DROIDCTL_NO_DAEMON=1 to never start it`. In `--json` mode the result carries `"daemon":{"started":true,"pid":4242}` instead, and stdout stays clean.
- **Every `--json` result** includes `"mode":"daemon"|"inprocess"`.
- **`droidctl doctor` and `droidctl daemon status`** show it: pid, uptime, version, socket path, log path, devices, cache hit rate.
- **Docs:**
  - README: a "Background daemon" section near the top (what it is, when it starts and stops, how to opt out, where the logs are).
  - AGENTS.md / SKILL.md: a short "Daemon" block. It explains that the first call is slower, that the next response reports events that happened between calls, and how to use `--no-daemon`.
  - `droidctl --help` epilog: one line.
  - `DAEMON.md`: the full socket API and lifecycle.
- **Tests:**
  - a drift test asserts that the README/AGENTS.md daemon sections exist and mention the opt-out env vars;
  - an e2e test asserts the first-start notice appears exactly once, and that `DROIDCTL_NO_DAEMON=1` leaves no process behind (`pgrep`).

**2. Device link = one persistent socket per device, owned by the daemon.**
- `setup` creates `adb forward tcp:N localabstract:droidctl` once and caches `N` and the APK version.
- The daemon keeps one socket open per device, carrying both requests and the event subscription.
- adb is invoked only on setup or reconnect (device replugged, forward lost); the daemon reconnects automatically and retries once.
- In `--no-daemon` mode, the CLI connects to the cached port directly.

**3. Thin, lean client:**
- The CLI entrypoint is stdlib only (sys/os/socket/json; ~20–30 ms floor). Anything else (argparse surface, rich, PIL for `--marks`, adbutils for setup) loads only inside the daemon or in `--no-daemon` mode.
- No urllib (it drags in ssl+email).
- A `-X importtime` budget test in CI.

**4. Fewer round trips:**
- The tap fast path (ref fresh + signature unchanged) is one `act` with `settle` folded in: the device performs the action, waits for idle and returns the new tree **in the same response**.
- So a tap-and-see is **one round trip**, and it primes the daemon's cache for the next snapshot.

**5. Settle policy is the real latency knob:**
- event-driven quiet window (default 150 ms, cap 2 s, `--settle MS|0`);
- return immediately when the clicked event plus a window-state change arrive and then go quiet;
- no fixed sleeps anywhere (droidrun and Artemis both sleep).

**6. Resident mode** is item 1: `serve --stdio`, `Client` and subscriptions all go through the daemon. Target: ~1 ms daemon overhead per command, and ~0 device cost for cache-hit snapshots.

**Streaming (push, not polling).** The device link and the daemon socket are both persistent full-duplex NDJSON streams. WebSocket and SSE would only wrap them in HTTP framing and a handshake, so they add overhead and no speed. They're reserved for a future browser viewer. Expected cost split (estimates, to be measured in M1): the round trip is a few ms, a tree dump is 20–150 ms, settle is 150–300 ms. So we cut round trips and polling, not protocol bytes.
- **v1: device-side blocking waits.** `wait_idle` and `wait_for {text|id|gone|activity|toast|window, timeout}` are one request, and the device replies from its event stream the instant the condition holds.
- **v1: JSON-RPC notifications (no `id`).** `subscribe {events:[toast, window, content, ime, crash]}` on the device socket (held by the daemon) and on the daemon socket (for clients and `watch --max N`).
- **v1: dirty-flag tree cache in the daemon** (item 1). The cheap 80% of a live mirror.
- **v1.x, gated by measurement: incremental tree mirror.** The device pushes debounced changed subtrees, so even *after* changes a snapshot needs no full dump. Costs are device CPU and battery, event storms and consistency (a periodic full-dump checksum). Built only if M1/M8 show full dumps after changes dominate.
- **Later: frame stream** (scrcpy-style H.264) for an instant `shot` and a live viewer.
- Not planned: msgpack/CBOR. Optional gzip for wireless adb if measured useful.

**7. Budgets enforced by tests** (recorded in `bench.json` by the e2e run):

| path | budget |
|---|---|
| CLI → daemon overhead (Python thin client) | <35 ms |
| daemon overhead (resident client / `serve`) | <3 ms |
| `snapshot`, cache hit | <5 ms + client boot |
| `snapshot`, cache miss (typical screen) | <250 ms |
| `tap` + settle (static target screen) | <500 ms |
| cold CLI, `--no-daemon` | <60 ms host overhead |

**Go/Rust: port only the thin client, if anything.** The daemon keeps all the logic (snapshot, resolver, policy, cache), so the per-call client is ~150 lines: argv → unix socket → print. That is the one place a native binary pays off. It cuts the ~20–30 ms Python boot to ~3 ms per bash call, turning a cache-hit snapshot into ~5 ms end to end. A Go client (Go is installed here) is a small, low-risk job.

**Triggers to port:**
- M1/M8 measurements show the client boot is a meaningful share of per-step time;
- or we want **single-binary distribution.** A Go binary that embeds the APK and can also run the daemon logic would need a full port, so that decision is deferred to v1.0 packaging.

The contracts are kept language-neutral so either port stays verifiable:
- `android/agent/PROTOCOL.md` (device JSON-RPC);
- `DAEMON.md` (host socket API);
- golden fixtures (raw tree → `.snap.txt`) and resolver cases as JSON.

## MCP server (`droidctl mcp`)
`serve --stdio` is our own JSON-RPC, **not** MCP. MCP layers its own protocol on JSON-RPC 2.0:
- an `initialize` handshake with protocol-version and capability negotiation, then `notifications/initialized`;
- `tools/list` with a JSON-Schema `inputSchema` per tool;
- `tools/call` returning `content` blocks (text/image) plus `isError`;
- `ping`;
- optionally `resources/*`, `logging`, progress and cancellation.

So we ship a real MCP server:
- **`droidctl mcp`**: the stdio transport, which is what Claude Code, Codex, Cursor and Antigravity launch. It is built on the **official `mcp` Python SDK** (optional extra `droidctl[mcp]`) for spec compliance and version negotiation. It is a resident process, so the SDK's import cost is paid once per session, not per call. Optional `--http` (streamable HTTP) for remote setups.
- **A client of the daemon:** it auto-starts the daemon and connects to it. The MCP session, bash CLI calls and other agents therefore share the same warm connection, tree cache, refs and per-device locks.
- **Tools generated from the command registry,** the same source as `cheat` and the AGENTS.md table, so the MCP schema can never drift from the CLI.
  - The set stays **small and token-lean** (every tool definition costs context on every turn): `snapshot`, `tap`, `type`, `scroll`, `swipe`, `action`, `key` (back/home/enter/…), `wait`, `launch`, `shot`, `logs`, `devices`.
  - Each tool takes an optional `device`.
  - Descriptions are one or two lines. Details live in a single `droidctl://guide` resource / prompt instead of in every tool.
- **Results in the same compact text as the CLI:** the snapshot lines, the diff after actions, typed errors. `isError:true` carries `error.kind` in the text, for example `stale-ref (gone): re-run snapshot`.
  - `shot` returns an MCP `image` block (a downscaled JPEG).
  - `structuredContent` plus an `outputSchema` provide the `--json` shape for clients that use it.
- **Streaming:** the progress notifications carry settle/wait progress, and cancellation aborts a `wait`. Optionally the `droidctl://screen` resource with `resources/subscribe` produces a push update when the screen changes, for the clients that support it.
- **Install helper:** `droidctl mcp --install claude|codex|cursor|all` writes the client config (the Artemis convenience). `droidctl skill install` remains the bash-first alternative.
- **Validation:**
  - run the MCP Inspector (`npx @modelcontextprotocol/inspector droidctl mcp`) in CI or as a manual check;
  - an e2e test drives `droidctl mcp` over stdio (initialize → tools/list → tools/call snapshot/tap on the test app);
  - a test asserts the tool list matches the command registry.

## CLI vs MCP: which to recommend
With the daemon, both are equally fast on the device side. Transport differences are milliseconds, while a model turn takes seconds. So the choice depends on the client:

| | CLI + skill | MCP |
|---|---|---|
| context cost | skill loads on demand (~1–2k tokens when used) | tool definitions every turn (~1.5–2.5k tokens; less where the client defers tool schemas) |
| batching and scripting | `run`, pipes, `jq`, loops, shell scripts the agent writes | one tool per call |
| text with quotes, Unicode, `$` | shell-quoting risk → `type --stdin` / `--file` | schema-validated args, no quoting issues |
| screenshots | `shot --out f.jpg`, then the agent reads the file (an extra step) | image returned inline |
| clients without a shell (Claude Desktop, chat UIs) | ✗ | ✓ |
| permissions | allowlist `Bash(droidctl:*)` | per-tool approval in the client |
| sandboxed agents | the daemon may be blocked → in-process fallback | the server is launched by the client |

**Recommendation in the docs:**
- **Shell-capable coding agents** (Claude Code, Codex): the **CLI + skill** is the default. It is lean on context, and batching and scripting are its big advantages.
- **MCP** for clients without a shell, when inline screenshots matter, or when a team wants a standard tool integration.
- Both are first-class. Same daemon, same output text, same error kinds.
- Add `type --stdin` to remove the CLI's main weakness.

## Setup and teardown (never break the user's phone)
- `droidctl setup`:
  1. `install -r -g`;
  2. **append** our service to `enabled_accessibility_services`, keeping the existing entries (droidrun overwrites them and kills TalkBack and password managers);
  3. set `accessibility_enabled 1`, re-checking up to 5 times;
  4. `ping`;
  5. optionally `dumpsys deviceidle whitelist +pkg` against OEM battery killers.
  - Android 13+ "restricted settings" only blocks the settings UI; the adb path is expected to work. **Verify in M1.**
- `droidctl teardown`: remove only our entry, then uninstall. Never touches other tools, the IME or other settings.
- `droidctl doctor`: adb, device, APK version, service bound, socket and peer-UID check, round-trip latency. It also warns if Appium/uiautomator2 is attached, since their UiAutomation suppresses a11y services; that case surfaces as error kind `suppressed` with a hint.
- Commands auto-run setup if the service is missing (`--no-auto-setup` to disable).

## Snapshot (host `snapshot.py`, pure)
Raw tree in, compact list out. Pruning works on the **tree**: Artemis's dedupe/merge never ran because its data was flattened first. Every test goes through real captured trees.

1. **Windows:** keep the app windows plus dialogs and popups. Mark the IME window and use its bounds for occlusion (`keyboard=shown`). Drop systemui unless `--system`.
2. **Clip** to the screen, the window and scrollable ancestors. Detect fixed bars (≥95% of width, at the edge, not scrollable), clamp nodes out of them and drop slivers ≤20 px.
3. **Visibility:**
   - drop nodes with `visible=false` or zero area;
   - drop nodes less than 10% visible unless they have a visible child;
   - hit-test with `drawingOrder` to flag `covered`.
4. **Keep:**
   - nodes with click, long-click, check, edit or scroll;
   - nodes with custom actions;
   - nodes with text, desc, hint or state.
   - Everything else collapses.
5. **Row merge:** an actionable node absorbs passive descendant text and descs (` · `, ~80 chars) but stops at actionable descendants. Covers Compose desc-on-child.
6. **Dedupe:** drop a child whose text equals its parent's, and same text within 8 px. Warn when actionable elements overlap by ≥50% outside normal nesting.
7. **Rich but terse annotations:**
   - `hint=`, `error=`;
   - `empty` (text == hint or showingHint);
   - `password`, `checked`, `off`, `disabled`, `focused`, `selected`, `heading`;
   - `range=3/10`;
   - `actions=[Delete, Archive]` for custom actions;
   - scroll containers: `list 4/37 more↓` from collection + scroll actions.
8. Cap at `--max 150`.

Format: one line per element, non-default states only:
```
screen com.foo/.InboxActivity  sig=3f9a  keyboard=hidden  dialog=no  toast="Message archived"
[1] button   "Back"
[2] input    "Search" hint="Search mail"  #search  empty
[3] list     4/37 more↓
  [4] row    "Ada Lovelace · Lunch tomorrow? · 10:42"  actions=[Archive, Delete]
[9] switch   "Dark mode"  off
```
- Options: `--diff`, `--find` (matches text, hint, desc, error), `--in REF`, `--raw` (raw JSON tree), `--bounds`, `--json`.
- A repeated identical signature prints `unchanged`.
- **Signature:** hash of the package, activity and pruned app-window skeleton (roles + ids). Excludes systemui and window order.
- **Fingerprint per ref** (saved in `~/.droidctl/snaps/<serial>.json` with the signature and the dump's handle map):
  - `uid` (API 33+);
  - id, class, text, desc, hint;
  - the path to the nearest ancestor with an id;
  - the merged label;
  - bounds.

### Spatial layer (layout without screenshots)
A flat list loses layout: which bar a control is in, what sits next to what, grids, and unlabeled icons next to their context. The layers below go from cheap and always on to heavier opt-ins. Every layer uses the **same refs**, so the agent can look at the layout and then act with `tap 7`.

**Status: provisional.** Nothing in this layer becomes a default until the spatial benchmark (TESTAPP.md group 8) shows it helps. To make that A/B-testable, every layer is independently switchable: `--layout flat|spatial` plus `--no-regions`, `--no-rows`, `--no-grids`, `--no-infer`, or in config and `DROIDCTL_LAYOUT`. The flat list stays available permanently as the baseline.

**Evaluation protocol:**
- **Agents:** at least 2 models (e.g. Claude Sonnet and Opus via Claude Code; optionally one non-Claude).
- **Runs:** each task × each variant × 3 runs.
- **Metrics:** task success (checked by the test app's logcat events, not by the agent's claim), steps taken, tokens in and out, wall time, and wrong-target taps.
- **Variants:** flat, spatial default, spatial minus each layer (ablation), `+--geo`, `+--map`, `+shot --marks`.
- **Decision rule:** a layer is on by default only if it raises success or cuts steps without adding more than ~15% tokens. Otherwise it stays opt-in or is dropped.
- **Where it runs:** a scripted harness (`bench/spatial.py`), re-run on every change to `snapshot.py`, with results in `bench/results/*.json`.

**Candidate defaults (budget: at most +15% tokens over the flat list):**
1. **Regions.** Elements are grouped under region headers: `top bar`, `content`, `bottom bar`/`nav`, `fab`, `dialog`, `sheet`, `drawer`, `keyboard`. Detected from window types, pane titles, known classes (Toolbar, BottomNavigationView, NavigationBarView, Compose semantics roles), the fixed-bar detection above, and position.
2. **Row grouping in reading order.** Elements whose vertical spans overlap by at least 50% render **on one line**, left to right and separated by spaces. This conveys horizontal layout almost for free, and is often shorter than one element per line.
3. **Grids as tables.** A collection with `collection.cols > 1`, or children aligned on both axes (calendar, keypad, photo grid, keyboard-like layouts), is rendered as a compact table with refs in the cells.
4. **Context for unlabeled controls.** An unlabeled icon gets a label from its resource-id name (`#ic_share` → `share?`, marked as inferred) and/or its nearest text on the same row or directly above: `button (unlabeled, right of "Wireless Mouse")`. An input with no hint gets its label from the text to its left or above it (a label-for heuristic).
5. **Scroll position:** `list 6/50 more↓ (12%)`.

Example (default output):
```
screen com.shop/.CartActivity  sig=c09e  1080x2400
-- top bar
[1] button "Back"   [2] heading "Cart (3)"   [3] button share? #ic_share
-- content  list 3/3
[4] row "Wireless Mouse · $24.99"   [5] button "−"   [6] text "1"   [7] button "+"
[8] row "USB-C Cable · $9.99"       [9] button "−"   [10] text "2"  [11] button "+"
-- bottom bar
[12] text "Total $44.97"            [13] button "Checkout"
```
```
[20] grid 7x5 "October 2026"
      S     M     T     W     T     F     S
      .     .     .     [21]1 [22]2 [23]3 [24]4
      [25]5 [26]6 [27]7 ...
```

**Opt-in:**
- **`--geo`:** a compact box per element as screen percentages, `@x,y wxh` (e.g. `@82,4 8x3`). About 5 tokens per element; answers "bottom-right" and "how big" questions precisely.
- **`--map`:** a coarse ASCII wireframe (about 48×24 characters) with ref numbers drawn in their boxes, as a whole-screen overview. Costs ~300 tokens. **Measure** whether agents benefit (LLMs read ASCII layouts unevenly); keep it opt-in.
- **`shot --marks`:** a screenshot with ref-numbered boxes (set-of-marks). This is the only layer that shows pixels: colours, images, icons, visual bugs. The default is a downscaled JPEG. `--crop REF` returns just one element's region, which is cheap.
- **`where REF`:** that element's box, region and neighbours (left, right, above, below).

**Spatial locators:** `tap --text "+" --right-of "Wireless Mouse"`, and likewise `--below`, `--above`, `--left-of` and `--near` (Maestro-style relative selectors). They compose with role/text filters, and ambiguity is still an error rather than a guess.

**Layout checks (QA, later):** `layout-check` reports overlapping controls, clipped or ellipsized text (a11y text longer than what fits), elements offscreen, and touch targets under 48dp. Pure analysis of the tree plus `--geo` data.

## Resolution (host `resolve.py`, pure)
1. **Fast path:** the ref's snapshot is still the device's latest dump *and* the signature is unchanged. Use the handle directly: no extra dump, one round trip.
2. **Otherwise:** fetch a fresh tree and match in tiers, requiring a **unique** hit at each tier:
   1. `uid` when available;
   2. exact attributes (not bounds or volatile flags; Maestro);
   3. ordered locators: id+text, id+path, text+class, desc (appium-mcp);
   4. Artemis scoring:

      | Signal | Weight |
      |---|---|
      | id | +0.5 (mismatch −0.5) |
      | normalized text | +0.4 (strip "(3)" badges) |
      | overlap | +0.3 |
      | tap-point containment | +0.3 |

      - Reject a >2.5× size mismatch.
      - Decay by distance: `max(0.5, 1−d/800)`, scaled by the screen diagonal.
      - Accept at ≥0.75, or ≥0.55 if unique.
      - Heal moves ≤200 px.
3. **Signature changed** (a different screen): only tiers 1–2 are allowed, so a stale "OK" can't hit a look-alike.
4. **Never guess by position.** Typed failures:
   - `stale-ref` with reason `gone`, `shifted` or `occupied` (plus what is there now);
   - `ambiguous` (candidates plus the locators tried);
   - `occluded` (covered or under the keyboard);
   - `offscreen` (with a `scroll-to` hint).

## Actions (host `act.py`)
- **Locators** (shared by every action): a positional ref (`tap 4`, equivalent to `--ref 4`); `--id`, `--text`, `--desc`, `--class`, `--role` (+ `--index`); spatial `--right-of`, `--left-of`, `--above`, `--below`, `--near`; `--point X,Y` in device px only as an explicit escape hatch. Non-unique matches → `ambiguous`, never a guess.
- **tap:**
  1. Resolve the ref. If the node isn't clickable, walk up to its clickable ancestor.
  2. `act ACTION_CLICK`.
  3. **Fallback policy (clicks that report success but do nothing):** if the device reports `performed:true` *and* a `TYPE_VIEW_CLICKED` event for that node arrives, the click was handled, so there's no fallback. If `performed:false`, or there's no clicked event and the tree is unchanged within 300 ms, do **one** `gesture tap` at the center of the largest uncovered area (droidrun geometry) and report `method:"gesture-fallback"`.
  - The event check is what makes the fallback safe: we never double-tap something that already handled the click, which would undo a toggle.
  - `--method auto|action|gesture` overrides the policy.
- **Other node actions:**
  - `long-press`, `tap --double`;
  - `scroll --ref N up|down` (ACTION_SCROLL_*), `scroll-to --text X` (repeat scroll + find, with a cap);
  - `action --ref N "Archive"` (custom actions such as swipe-to-delete without a gesture);
  - `set --ref N 7` (range/slider);
  - `focus`, `expand`/`collapse`, `dismiss`.
- **type** (`type N "text" [--append] [--clear] [--enter]`, or `--stdin` / `--file F` to avoid shell quoting):
  1. `ACTION_SET_TEXT`: Unicode, no tap first.
  2. If that fails: focus, then clipboard paste (the service sets the clipboard, then ACTION_PASTE, then restores it).
  3. Last resort: `adb input text` for ASCII.
  - `--enter` uses `ACTION_IME_ENTER` (API 30+), else `input keyevent 66`.
  - Read the value back until it is stable twice (150 ms) and return `value`. **Never switch the IME.**
- **Global and gestures:** `back`, `home`, `recents`, `notifications`, `quick-settings`; `swipe up|down|left|right` (default x=0.6W, y 0.7→0.3H, 800 ms); `gesture --path` as the escape hatch; `press KEY` through `adb input keyevent` for keys with no a11y equivalent.
- **Settle and verify (every action, on by default):**
  - device-side settle folded into `act` (quiet window 150 ms by default, cap 2 s, `--settle MS|0`; the default is tuned by M1/M8 measurements), **event-driven** rather than polling hashes;
  - then a fresh pruned snapshot, diffed against the old one.
  - Result: `{changed, method, diff (≤80 lines), refs, toast, events, warning?}`.
  - Settling never fails the action. `--expect-change` makes no change an error (`no-change`). No automatic retry beyond the event-gated fallback above.
- **wait:** `--text/--id/--gone/--activity/--toast [--timeout]`, driven by events with a snapshot check.
- **Error kinds:** chromectl's generic set plus `no-device`, `not-installed`, `suppressed`, `stale-ref`, `ambiguous`, `occluded`, `offscreen`, `disabled`, `no-change`, `secure-window`, `screen-off`, `adb`, `no-daemon`. Kept in one `ERROR_KINDS` registry, drift-tested as in chromectl.

## Command inventory (v1)
Every command takes `--json`, `-d SERIAL|NAME` (falls back to `ANDROID_SERIAL`, then the single attached device), and `--no-daemon`.
- **Setup / device:** `devices`, `setup`, `teardown`, `doctor`, `ping`.
- **Daemon:** `daemon start|stop|restart|status|logs`.
- **Look:**
  - `snapshot [--diff] [--find T] [--in REF] [--raw] [--bounds] [--layout flat|spatial] [--geo] [--map] [--system] [--max N]`;
  - `where REF`;
  - `shot [--scale 0.5] [--full] [--marks] [--crop REF] [--out F]`;
  - `current`.
- **Act:**
  - `tap`, `long-press`, `type`, `scroll`, `scroll-to`, `swipe`, `action`, `set`, `focus`, `expand`, `collapse`, `dismiss`, `gesture --path`;
  - `back`, `home`, `recents`, `notifications`, `quick-settings`, `press KEY`.
  - Options: `--method auto|action|gesture`, `--settle MS|0`, `--expect-change`.
- **Wait / observe:** `wait --text/--id/--gone/--activity/--toast [--timeout]`, `watch --max N` (pushed events), `logs --max N [--pkg] [--level]`.
- **Apps:** `launch <pkg> [--clear] [--stop]` (via `adb shell`, then device `wait_for window` + settle), `stop-app`, `apps`, `install`, `open-url`.
- **Integration:** `run` (steps, one connection), `serve --stdio`, `mcp [--install claude|codex|cursor|all] [--http]`, `cheat`, `skill print|install`, `version`.
- **Dev:** `dump-fixture NAME`, `layout-check` (post-v1).

Any coordinates we print are always in device pixels.

## Milestones
0. **Toolchain: done 2026-09-27.** Dev phone: Galaxy Note 8 SM-N950F, Android 9 (API 28), stock plus Magisk (details in CLAUDE.md). Add an API 35 emulator as a second target for API 30+ paths. JDK 21, Go, Android SDK at `~/Android/Sdk` (platform 35, build-tools 35.0.0, platform-tools/adb 37; `ANDROID_HOME` in `~/.zshrc`). Still to do at the start of M1: create the `.venv` with `adbutils`, and connect and authorize the phone (`adb devices`).
1. **Walking skeleton, measured: done 2026-09-27.** Results (SM-N950F, API 28, USB passthrough into a KVM VM; `bench/results/m1-rtt.json`): socket `echo` median 8.3 ms / p95 11.0 (equal to a raw adb-forward echo through toybox `nc`, so this is the transport floor here; it jitters 4–8 ms between runs); `ping` 15.4 / 18.8 ms; a fresh connection plus echo 13.5 ms; a 64 KB echo 48 ms; service bind (settings put → first answer) ~194 ms. Cold `droidctl ping --json` 76 ms end to end (≈48 ms host overhead, an estimate); `python -c pass` 27 ms; `import droidctl.cli` 7 ms cumulative (no adbutils/rich). **Found and fixed:** `LocalSocket` `flush()` polls the send queue with ~10 ms sleeps, adding 10.6 ms to every reply; the agent no longer flushes. APK: 12.7 KB, no `<uses-permission>` at all. Peer UID over `adb forward` = 2000; setup preserves other services; teardown restores the secure settings byte-for-byte. Not measured: wireless adb (needs `adb tcpip` on the user's network). Not yet verified: that a non-shell app uid is rejected (M2, via the debuggable test app's `run-as`).
   Original scope:
   - Gradle project;
   - a service with a socket, `ping` and the peer-UID check;
   - Python: chromectl core port, `device.py`, `setup`/`teardown`/`doctor`/`ping`.
   - **Measure the round trip**: raw socket RTT over USB and over wireless adb, a cold `droidctl ping` end-to-end, and `-X importtime`. These numbers decide the Go/Rust gate and the settle defaults.
   - Write `android/agent/PROTOCOL.md` (the language-neutral device contract).
   - Verify on the phone: adb enabling on Android 13+, peer UID = 2000, TalkBack/other services preserved.
2. **Raw tree:**
   - device `tree` plus handles;
   - `dump-fixture` to capture real trees from your apps and from the test app;
   - build the test app skeleton, scenario registry and DTA logging, plus scenario groups 3–5 from TESTAPP.md;
   - `make fixtures` for golden trees and snapshots.
3. **Snapshot, test first**, against fixtures: tokens <2k per screen, expected lines (row merge, hint/empty, bars clipped, collection counts, custom actions).
   - Spatial layer: regions, row grouping, grid tables, inferred labels. Golden `.snap.txt` files cover the TESTAPP group 8 scenarios.
4. **Resolver, test first** on fixture pairs: scrolled, reordered, a different screen with a look-alike OK, a moved element.
5. **Actions:**
   - device `act`, `gesture`, `global`, events, `wait_idle`;
   - host tap policy, type ladder, scroll, custom actions, settle/diff.
6. **Daemon:** `droidctl daemon` (ported from chromectl `daemon.py`, with structured results, per-device locks, version handshake and idle exit), a permanent subscription, the dirty-flag tree cache, the thin `client.py` with auto-start, `serve --stdio`, `Client`, `watch`, and `DAEMON.md`. Measure cache-hit vs miss, then decide on the Go thin client.
   - **MCP:** `droidctl mcp` (official SDK, tools generated from the registry, image `shot`, `--install` helper), validated with MCP Inspector plus a stdio e2e test.
7. **Extras:** `screenshot` (+ `--marks`, `--crop`), spatial opt-ins (`--geo`, `--map`, `where`, spatial locators), toasts in the header, `wait`, `run`, `logs`, apps commands, `type --stdin`; test app groups 1, 2 and 6 (timing, security/occlusion, windows).
8. **Docs and release:** README (including the Background daemon section and CLI vs MCP guidance), AGENTS.md, SKILL.md, DAEMON.md, benchmarks (latency, tokens, tap accuracy vs mobile-mcp, the spatial A/B that decides layout defaults), packaging (APK bundled in the wheel); test app group 7 (device-level) and the scenario coverage gate.

## Open questions and decision gates
| question | decided by | when |
|---|---|---|
| ~~Does peer-UID auth work (adbd = uid 2000) on the user's phone?~~ **Yes** (peer_uid 2000 on the SM-N950F, 2026-09-27); rejection of an app uid still to be shown in M2 | on-device test | M1 |
| Can adb enable our service on Android 13+ despite restricted settings? | on-device test (**not possible on the Android 9 dev phone**; use the API 35 emulator or another device) | M1 |
| Do a11y bounds, gesture coordinates, `screencap` and `--marks` agree under a display-resolution override (dev phone: 1080x2220 over 1440x2960)? | on-device test | M1/M7 |
| Do the M1 on-device checks hold on a **stock, unrooted** phone? (dev starts on a rooted phone, where custom ROMs and Magisk/LSPosed can change settings, SELinux and a11y behaviour) | re-run the M1 checks on a stock device | before v1.0 |
| Real socket RTT (USB and wireless), tree-dump time, settle time → settle defaults | `bench` | M1, M5 |
| Port the thin client to Go? | client boot share of per-step time; distribution needs | M6 / v1.0 |
| Which spatial layers are on by default? `--map` useful at all? | spatial A/B | M8 |
| Incremental tree mirror worth it? | cache-miss cost after changes | after M8 |
| `click_no_event` views: acceptable double-trigger risk of the fallback? | TESTAPP `click_no_event` results | M5 |
| Accessibility-tool flag vs Play policy (only matters if we ever publish to Play) | policy check | before any Play release |

## Future work (out of scope for v1)
- **iOS** behind the same host contracts: simulators via `idb`/`simctl`, devices via WebDriverAgent.
- **WebView → chromectl handoff:** forward `webview_devtools_remote_<pid>` and drive debuggable WebViews with chromectl's CDP commands.
- **Incremental tree mirror** and a **frame stream** (scrcpy-style) for instant `shot` and a live browser viewer (the only place WebSocket makes sense).
- **`layout-check`** QA audits (overlap, clipping, offscreen, touch target < 48dp).
- **Fallback backend:** a scrcpy-style `app_process` + UiAutomation jar for devices where the a11y service can't be enabled.
- **Single-binary distribution** (Go, APK embedded) if the gate says so.

## Licensing
- **Artemis (Apache-2.0):** port with attribution in NOTICE. This covers the scoring thresholds and the helper design; our APK is written fresh in Kotlin, with Artemis's Java as reference.
- **droidrun Portal APK (AGPL-3) and mobilecli (unclear license):** ideas only, no code.
- **Our license:** MIT.

## Verification
- **`pytest`:** snapshot and resolver tests offline on golden fixtures (test app + real apps), plus the drift tests. e2e against the test app's scenario matrix (TESTAPP.md) when `DROIDCTL_SERIAL` is set; ground truth comes from the app's `DTA` logcat events, not from droidctl output. A coverage gate fails if any scenario lacks an e2e test or fixture.
- **e2e on the phone:**
  - `setup` leaves existing accessibility services on;
  - `snapshot` on Settings is short;
  - `tap` on a row gives `changed:true`, `method:"action"`;
  - a toggle is tapped exactly once;
  - swipe-to-delete works via `action "Delete"`;
  - Unicode `type` reads back correctly;
  - a stale ref after navigating gives `stale-ref`;
  - `teardown` restores the original settings.
- **Daemon:** auto-start notice exactly once; `--no-daemon` leaves no process; version-mismatch restart; sandbox fallback to in-process; two devices don't block each other.
- **MCP:** MCP Inspector passes; stdio e2e (initialize → tools/list → tools/call) against the test app; tool list == command registry.
- **Budgets** (see Performance) are asserted from `bench.json`.
- **Benchmark** (in the README):
  - raw dump vs snapshot tokens;
  - per-call latency;
  - tap accuracy over ~50 taps across testapp and real apps (rows, icon-with-child-text, Compose, custom views), compared against mobile-mcp;
  - the spatial A/B (see Spatial layer), which decides the layout defaults.

## Prior art (details in research/)
- **Artemis:** clipping and bar detection, `precondition_xml` scoring, helper APK install/enable flow.
- **droidrun/mobilerun:** 10% visibility rule, tap-blocker geometry, text-input ladder, degraded snapshots. Also its pitfalls: overwriting a11y settings, leaking the IME, fixed sleeps.
- **mobile-mcp:** line format. Its failure modes are what we design against: dp vs px scaling, top-left coordinates, positional refs, no verification.
- **Maestro:** exact-match resolution, `isVisible` hit-test, settle.
- **agent-device:** ref expiry, settle timing, Compose desc-on-child.
- **appium-mcp:** ordered unique locators.
- **android_world:** never guess by index; a11y proto schema.
