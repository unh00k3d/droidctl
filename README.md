# droidctl

An agent-first Android automation CLI. It reads the screen as a **compact list of elements
with refs** (about 40–460 tokens per real screen, estimated, instead of a 2–7k-token raw dump), acts on
**elements through the accessibility API** instead of guessing coordinates, and verifies
every action with a diff. A tiny on-device agent APK plus a resident host daemon keep it
fast: a tap including the wait for the screen to settle takes ~300 ms on a 2017 phone.

```
$ droidctl snapshot
screen dev.droidctl.testapp/.Main  sig=9b86  keyboard=hidden  dialog=no  1080x2220
-- top bar
[1] button "Back"   [2] heading "Cart (4)"   [3] button share? (unlabeled, right of "Cart (4)") #ic_share
-- content
[4] text "Wireless Mouse"   [5] button "−"   [6] text "1"   [7] button "+"
-- bottom bar
[19] text "Total $84.96"   [20] button "Checkout"

$ droidctl tap 7
```

It is a sibling of [chromectl](https://github.com/0xenesbayram/chromectl) and follows its
conventions: `--json` on every command with a typed `error.kind`, `run` batching, `cheat`,
an installable agent skill, and [AGENTS.md](AGENTS.md) as the whole interface.

## Background daemon

The first `droidctl` command starts a **background daemon** and says so once on stderr
(`droidctl: started background daemon (pid …, idle-exit 30m) …`; in `--json` mode the result
carries `"daemon": {"started": true, "pid": …}` instead, so stdout stays clean).

- **What it does:** keeps one warm connection per phone, a screen cache that event
  notifications keep valid, and the events that happened between your calls (toasts, new
  windows), which are reported on the next result under `between_calls`.
- **When it stops:** `droidctl daemon stop`, after 30 minutes idle (`DROIDCTL_IDLE`, e.g.
  `10m`, `0` = never), or when a different droidctl version connects (it restarts as the
  right one). After a crash the next call starts a fresh one.
- **Opting out:** `--no-daemon` or `DROIDCTL_NO_DAEMON=1` runs everything in-process and never
  starts a background process (slower: no cache or event history). `DROIDCTL_AUTOSTART=0`
  makes commands fail with `no-daemon` instead of starting one, for people who want to run
  `droidctl daemon start` themselves. Sandboxes that forbid it fall back to in-process
  automatically. Every `--json` result says `"mode": "daemon"` or `"inprocess"`.
- **Where:** socket `~/.droidctl/d.sock` (0600), log `~/.droidctl/daemon.log`;
  `droidctl daemon status|logs`. The protocol is in [DAEMON.md](DAEMON.md).

## Install

Requires Python 3.10+, `adb` (Android platform-tools) and a phone with USB debugging on
(Android 8 / API 26 or newer).

**Tested on one phone so far:** a Samsung Galaxy Note 8 on Android 9 (API 28). Code paths
that need Android 11+ are implemented but **unverified**: on-device screenshots
(`takeScreenshot`; API 28 uses `adb screencap`), the keyboard action key via `ime_enter`
(API 28 uses `input keyevent 66`), `stateDescription`, stable node ids (`getUniqueId`, API 33),
enabling the service under Android 13+ "restricted settings", and API 34
`accessibilityDataSensitive`. Testing them on an emulator is planned but deferred.

droidctl is not on PyPI yet. Build the wheel (it bundles the agent APK) from a checkout; that
needs the Android SDK (`ANDROID_HOME`) and a JDK 17+ for the APK:

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[test,mcp]'
make apk                        # builds android/agent -> droidctl/assets/droidctl-agent.apk
make dist                       # APK + wheel + sdist in dist/
pip install 'dist/droidctl-0.1.0.dev0-py3-none-any.whl[mcp]'   # anywhere else
```

## Quick start

```bash
droidctl devices                # is the phone visible?
droidctl setup                  # install the agent, enable its accessibility service
droidctl launch com.android.settings
droidctl snapshot
droidctl tap --text "Display"
droidctl type 2 "Merhaba dünya 😀" --enter
droidctl wait --text "Saved" --timeout 5
droidctl run --json --step 'back' --step 'snapshot'
droidctl teardown               # remove everything droidctl put on the phone
```

See [AGENTS.md](AGENTS.md) for every command and the rules agents should follow.

## CLI or MCP?

Both talk to the same daemon, print the same compact text and use the same error kinds.

| | CLI + skill | MCP (`droidctl mcp`) |
|---|---|---|
| context cost | the skill loads on demand | tool definitions on every turn |
| batching and scripting | `run`, pipes, `jq`, loops | one tool per call |
| text with quotes, Unicode, `$` | use `type --stdin` / `--file` | schema-validated arguments |
| screenshots | `shot --out f.jpg`, then read the file | image returned inline |
| clients without a shell | ✗ | ✓ |

- **Shell-capable coding agents** (Claude Code, Codex): use the CLI and
  `droidctl skill install` (writes `~/.claude/skills/droidctl/SKILL.md`).
- **MCP** for clients without a shell, or when inline screenshots matter:
  `droidctl mcp --install claude|codex|cursor|all` (it keeps your other servers and backs up
  the config file; `--dry-run` shows the change).

## What it does to your phone

- `setup` installs `dev.droidctl.agent` and **appends** its service to the secure setting
  `enabled_accessibility_services`, keeping whatever was there (TalkBack, password managers),
  then sets `accessibility_enabled=1` and creates an `adb forward`. It records the original
  values first.
- `teardown` removes only our entry, restores the original `accessibility_enabled` when ours
  was the only service, removes the forward and uninstalls the agent. Verified on a real
  phone: the secure settings end up byte-for-byte identical.
- droidctl never switches or leaves behind a keyboard (IME) and never uninstalls other tools.
  `launch --clear` (which wipes an app's data) is the one destructive option, and it is opt-in.

## Security

- The agent APK **requests no Android permissions (not even INTERNET); its capabilities come
  from being enabled as an accessibility service, which setup does and teardown undoes.**
  While enabled, an accessibility service can read and act on everything on screen, which is
  why droidctl only enables it on request and removes it cleanly. Without `INTERNET` it cannot
  send anything off the phone.
- Its socket is a local abstract unix socket reached only through `adb forward`. It accepts
  peers with uid 2000 (adb shell) or 0 (root) and rejects every other app. On the dev phone
  (Android 9, SELinux enforcing) another app's `connect()` was already refused by SELinux
  (`EACCES`) before the uid check ran, so the check is a second layer.
- Banking and other secure apps: screenshots of `FLAG_SECURE` windows fail with
  `secure-window` (the snapshot still works). Some apps refuse to run on rooted phones whatever
  droidctl does (a banking app crashes at start on the rooted dev phone, with or without
  droidctl's service enabled).

## Benchmarks

Measured on a Samsung Galaxy Note 8 (SM-N950F, Android 9 / API 28) connected over USB,
passed through into a KVM virtual machine; raw data in `bench/results/`. Medians.

| path | median | p95 | file |
|---|---|---|---|
| device socket round trip (`echo`) | 8.3 ms | 11.0 ms | m1-rtt.json |
| same through a raw `adb forward` to `nc`, no agent (transport floor) | 8.0 ms | 11.1 ms | m1-rtt.json |
| `ping` (device info) | 15.4 ms | 18.8 ms | m1-rtt.json |
| cold `droidctl ping --json` from a shell (in-process, before the daemon existed) | 76 ms | 85 ms | m1-rtt.json |
| service bind after `setup` enables it | 194 ms | 224 ms | m1-rtt.json |
| tree dump on the device (Settings page) | 71 ms | 77 ms | m5-device.json |
| `act` click + settle on the device (static target) | 321 ms | 356 ms | m5-device.json |
| `tap` + settle, in-process (budget < 500 ms) | 302 ms | 314 ms | m5-host.json |
| cold `droidctl tap` from a shell | 414 ms | 461 ms | m5-host.json |
| Settings navigation `act` + settle (new page in the result) | 877 ms | 894 ms | m5-device.json |

Host overhead only (fake agent, same machine; m6-daemon.json):

| path | median | budget |
|---|---|---|
| `python -c pass` (the floor for any Python CLI) | 27.4 ms | – |
| `droidctl version` through the daemon | 34.3 ms | CLI → daemon overhead < 35 ms |
| cached `snapshot` through the daemon, from a shell | 38.5 ms | – |
| cached `snapshot`, resident client (SDK/MCP) | 3.5 ms | < 5 ms |
| a resident client call (`version`) | 0.33 ms | < 3 ms |

Snapshot size: 41–463 tokens per real app screen (Settings, launcher, Google Docs,
sahibinden, Open Camera; an estimate at 3.5 characters per token), 12–40× smaller than the raw
accessibility JSON. Every test-app screen is under 260 tokens.

### Tokens per screen

`bench/tokens.py` over every captured fixture (an estimate at 3.5 characters per token):

| fixtures | raw JSON | flat snapshot | spatial snapshot |
|---|---|---|---|
| 11 real app screens | median 3,723 | median 151, max 457 | median 166, max 463 (+3.7%) |
| 91 test-app screens | median 1,793 | median 57, max 407 | median 63, max 399 (+4.9%) |

### Tap accuracy

`bench/tap_accuracy.py`: 52 targets (46 test-app: rows, repeated icons in rows, duplicate
buttons, grids, Compose, virtual views, unlabeled icons; 6 real: Settings rows, launcher icons).
Graded by the test app's own `DTA` events (exactly the intended event, nothing else) and by
`dumpsys` for real apps, never by droidctl's output.

| method | correct | failed safely (typed error, no tap) | wrong target |
|---|---|---|---|
| `snapshot`, then `tap REF` (the recommended path) | 50/52 | 1 | 0 |
| `tap` with locators (`--text/--desc … --right-of/--below`) | 41/52 | 11 | 0 |
| baseline: `uiautomator dump` + `input tap` at the element's centre (mobile-mcp 1.0.5's Android robot, re-implemented) | 52/52 | 0 | 0 |

- **No method hit a wrong target.** The two `tap REF` misses (a dropped socket mid-tap; a snapshot taken before a grid laid out) both passed 3/3 when rerun.
- **Locator misses are refusals, and two are droidctl bugs:**
  - `--desc` doesn't match a description merged from a child (a Compose icon inside a button);
  - `--right-of`/`--left-of` anchors must equal a whole merged row label (`"Ada Lovelace · Lunch tomorrow?"`), not part of it.
  - The two `--below` misses are genuinely ambiguous: two Buy buttons sit below in the same column.
- **Coordinate taps are accurate on static, fully visible targets.** The cases where they go wrong are covered by `tests/e2e` and the resolver's before/after pairs, not by this benchmark: elements occluded by an overlay or the keyboard, moved after a scroll, or on a screen that changed.
- **mobile-mcp itself was not run.** Its device path needs the separate `mobilecli` binary, which can install an agent on the phone.
- **`uiautomator dump` suppresses accessibility services while it runs.** droidctl's agent answered again within 0.1 s.

### Layout A/B (reduced run, provisional)

`bench/spatial.py`: `claude-sonnet-5` headless (`claude -p`, only `Bash(droidctl:*)`), 10 test-app
tasks × 3 layout variants × 1 run. Success comes from the app's `DTA` events. Total cost $3.79.

| variant | success | droidctl calls | input tokens (incl. cache) | wrong taps |
|---|---|---|---|---|
| flat | 9/10 | 47 | 1.62M | 0 |
| spatial (the default) | 9/10 | 45 | 1.51M | 0 |
| spatial + `shot --marks` | 9/10 | 59 | 2.35M | 1 |

- **The one failure is the same task in every variant, and the task is at fault.** In `delete_item7`, the test app's Delete only logs and never removes the row. Every agent saw no change and tapped again, while the task requires exactly one delete.
- **Without that task, flat and spatial tie:** 9/9 each, 32 vs 31 calls, spatial 3% fewer tokens. `--marks` has the same success with +41% tokens and 5 more calls.
- **Reading:** spatial stays the default because it costs nothing extra, but this run doesn't show that it helps. `--marks` stays opt-in.
- **Caveat:** one run per task and one model is not statistically meaningful.

### Pending

Wireless-adb latency, and the full A/B protocol (2 models, 3 runs, per-layer ablations, `--geo`, `--map`).

## How it is tested

- Unit and golden tests run offline on **real trees captured from phones**
  (`tests/fixtures/trees/`, `droidctl dump-fixture`), never hand-written ones.
- A purpose-built test app (`android/testapp`, spec in [TESTAPP.md](TESTAPP.md)) with ~80
  edge-case scenarios logs `DTA` ground-truth events to logcat; the on-device e2e tests
  (`DROIDCTL_SERIAL=… pytest tests/e2e`) check those events, not droidctl's own output.
- The device protocol is specified in [android/agent/PROTOCOL.md](android/agent/PROTOCOL.md);
  the design and its measured decisions are in [PLAN.md](PLAN.md).

## License

MIT (see [LICENSE](LICENSE)). Attributions for adapted designs are in [NOTICE](NOTICE).
