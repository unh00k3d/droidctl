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

## Two backends

droidctl's agent reaches the screen one of two ways, and everything else (snapshots, refs,
actions, the daemon, MCP) is the same for both:

| | `a11y` (default) | `uiautomation` |
|---|---|---|
| how | our accessibility service, installed and enabled by `setup` | the same agent code run by `app_process` from a pushed (not installed) APK, holding a UiAutomation |
| changes on the phone | installs `dev.droidctl.agent`, appends it to `enabled_accessibility_services` | none: a file in `/data/local/tmp`, removed by `teardown` |
| lifetime | permanent; rebinds after reboot | until 10 min idle, reboot or `teardown`; the next command restarts it |
| gestures | `dispatchGesture` | injected touch events |
| screenshots | `takeScreenshot` (API 30+), else `screencap` | `screencap` |
| conflicts | Appium/uiautomator2 suppress it (`suppressed`) | only one UiAutomation client at a time: with Appium/uiautomator2 attached it can't start (`suppressed`) |

`droidctl setup --backend uiautomation` selects it for a phone (`DROIDCTL_BACKEND` per call),
and `doctor` shows which one answers. Use it where an accessibility service can't be enabled,
or for apps that hide their UI while an unknown accessibility service is on (measured on a
production banking app: with droidctl's service enabled its screens came back empty; with the
service off, a UiAutomation client saw them). It needs exactly the trust droidctl already
has (USB debugging and an authorized host): a UiAutomation is only available to the adb shell
user, never to an installed app. droidctl never hides a service or spoofs anything to get past
an app's checks, and automating a production app should be cleared with its owner.
Measured on the SM-N950F (API 28): both backends pass the same e2e checks; `uiautomation` is a
little slower (ping 16.8 vs 15.6 ms, snapshot 200 vs 147 ms, tap+settle 532 vs 493 ms, cold
in-process CLI). It connects with `FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES`, so TalkBack and
droidctl's own service keep running beside it (verified on API 28).

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
| `snapshot`, then `tap REF` (the recommended path) | 52/52 | 0 | 0 |
| `tap` with locators (`--text/--desc … --right-of/--below`) | 50/52 | 2 | 0 |
| baseline: `uiautomator dump` + `input tap` at the element's centre (mobile-mcp 1.0.5's Android robot, re-implemented) | 52/52 | 0 | 0 |

Measured 2026-09-28 on agent 0.4.2 (`bench/results/tap-accuracy.json`; the 2026-09-27 run had
`tap REF` 50/52 and locators 41/52).
- **No method hit a wrong target.** The two locator refusals are genuinely ambiguous: two Buy
  buttons sit below the plan name in the same column.
- **Fixed since the first run:** `--desc` matches a description merged from a child (a Compose
  icon inside a button); `--right-of`/`--left-of` anchors may be one part of a merged row label
  (`"Ada Lovelace"` of `"Ada Lovelace · Lunch tomorrow?"`); and an agent crash (an event
  race, see PLAN.md) that had closed a connection mid-tap.
- **Time per tap (median, warm daemon):** ~750 ms for droidctl when the tap changes nothing on
  screen (it waits up to 600 ms for a first change, so a slow screen isn't reported as done),
  ~390 ms when it does; the baseline takes ~2.6 s (`uiautomator dump` plus the tap, with no
  check of what happened).
- An external UiAutomation client (`uiautomator dump`, Appium) makes Android unbind
  accessibility services while it runs, closing every agent connection for ~1–2 s. droidctl
  waits and retries **read-only** requests; an action cut off this way returns `connection`
  with `maybe_performed: true` and is never resent.
- **Coordinate taps are accurate on static, fully visible targets.** The cases where they go wrong are covered by `tests/e2e` and the resolver's before/after pairs, not by this benchmark: elements occluded by an overlay or the keyboard, moved after a scroll, or on a screen that changed.
- **mobile-mcp itself was not run.** Its device path needs the separate `mobilecli` binary, which can install an agent on the phone.
- **`uiautomator dump` suppresses accessibility services while it runs** (see the dropped socket above). droidctl's agent answered again within 0.1 s after the dump ended.

### Layout A/B (full run, one model)

`bench/spatial.py`: `claude-sonnet-5` headless (`claude -p`, only `Bash(droidctl:*)`), 10 test-app
tasks × 9 layout variants × 3 runs = 270 runs (2026-09-28, agent 0.4.2,
`bench/results/spatial-ab-full-2.json`). Success and wrong taps come from the app's `DTA`
events. Total cost $23.92. Each variant holds for every screen droidctl prints
(`DROIDCTL_LAYOUT`), including action results.

| variant | success | droidctl calls | input tokens vs flat | wrong taps |
|---|---|---|---|---|
| flat | 30/30 | 78 | — | 0 |
| **spatial (the default)** | 30/30 | 80 | +2.6% | 0 |
| spatial without regions / rows / grids / inferred labels | 30/30 each | 81 / 85 / 84 / 84 | +2.8% to +7.9% | 0 |
| spatial + `--geo` / `--map` | 30/30 each | 81 / 86 | +3.8% / +9.7% | 0 |
| spatial + `shot --marks` | 30/30 | 82 | +14.2% | 0 |

- **No layout measurably helps on these tasks.** Every variant succeeded every time; the
  differences in calls are a handful out of ~80 and come from one or two tasks, which is
  run-to-run noise. The tasks are at a ceiling for this model.
- **Spatial stays the default, provisionally** (user decision 2026-09-28): it costs ~3%
  more than flat and carries structure flat can't show (regions, grids, rows). `--geo`,
  `--map` and `--marks` stay opt-in. Harder tasks or a second, weaker model are what could
  separate the layouts.
- **The first full run (`spatial-ab-full.json`) found two droidctl defects instead**, both
  fixed before this run: 12 of its 13 failures were a correct tap reported as a bare
  "unchanged" and then tapped again (results now say the click was handled), and 3 runs
  deleted the wrong row with a ref number from before a scroll (refs are now stable on one
  screen). The earlier 1-run pass is in `spatial-ab.json`.

### Pending

Wireless-adb latency, a second model for the A/B, and harder A/B tasks.

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
