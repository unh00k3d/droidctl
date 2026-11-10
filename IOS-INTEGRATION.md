# iOS integration plan (I1 → I4)

_2026-10-21. The plan for putting the iOS backend into droidctl's code. Inputs: `IOS.md` (the spec), `research/ios-decision.md` (the evidence) and a survey of droidctl's seams (file:line references below are from that survey, at commit `82d93f2`). Decisions marked **(proposed)** need the user's OK before I1 starts; everything else follows from the spec._

**Status (2026-10-21): I1 is done** (numbers in PLAN.md "iOS milestones"). As built, it differs from the text below in four ways:
- the Xcode project lives in `droidctl/ios/xcode/` (package data), not `ios/runner/`;
- the guided setup is `droidctl/ios/setup.py`, run in the user's terminal (`client.py` keeps an interactive iPhone setup out of the daemon);
- the runner already implements `gesture`, `launch` and `global home` (explicit points only; the lock guard first);
- the runner's lock-guard refusal is the new agent error code -32010 (`screen-off`).

**I2 is done too** (2026-10-21; numbers in PLAN.md). It differs from §5 below as follows:
- **Touch fallback:** the runner makes the single checked touch itself, so the host never falls back to a positional tap.
- **New error codes:** -32011 (`occluded`) and -32012 (`not-focused`).
- **`ime_enter`** is a typed newline, because the field already has focus. The Return key's AX press is used by `hide-keyboard`.
- **`wait_for`** runs in `IosClient` on the mapped tree, woken by the runner's `wait_change`.
- **The daemon's `PooledClient`** binds the device client's own class (`IosClient`).
- **The iOS test app is not built yet:** the free team's 3-app limit is full until the S1 apps are removed after 2026-10-27. The e2e uses the S1 test app.

**I4 is done too** (2026-10-21): `launch`, `stop-app`, `apps`, `install`, `open-url`, `shot`, `mark`, `logs`. `logs` reads the phone's syslog over lockdown, in `droidctl/ios/syslog.py`. That stream must never be open while Xcode connects to the phone (see PLAN.md).

**I3 is done too** (2026-10-21): `droidctl/ios/manage.py` (`teardown`, `doctor`) and renewal in `setup`. Teardown removes droidctl's product runners only, never the research builds or other apps. Renewal is unverified until a real expiry.

## 1. The shape in one paragraph
The iOS runner speaks **the same NDJSON JSON-RPC framing as `android/agent/PROTOCOL.md`**, so the host reuses `AgentClient`'s transport, retries and notification handling. The host gets a new backend `"ios"` next to `"a11y"` and `"uiautomation"`. Its client, `IosClient`, is an `AgentClient` subclass that:
- connects over usbmux instead of an adb forward;
- **maps the runner's iOS tree into droidctl's raw tree** (`ios/mapping.py`) inside `tree()`;
- **translates Android action names** (`click`, `scroll_forward`, `set_text`, `ime_enter`, …) into the runner's AX/touch policy inside `act()`.

Everything above the client (`snapshot.build`, `resolve`, `act.py`'s policy, the daemon pool and tree cache, CLI, MCP) stays platform-neutral. It gains only a **capability check** (`caps` from `ping`), so Android-only commands fail with `unsupported` on iOS instead of reaching adb.

## 2. Where the seams are (survey)
| seam | today | iOS change |
|---|---|---|
| device listing | `device.list_devices` (`device.py:158`): adb `host:devices` only | merge usbmuxd `ListDevices` (iPhones by hardware UDID); `devices` shows a `platform` column |
| backend choice | `backend_for` (`device.py:197`): env, then `state.json`, then `"a11y"`; `BACKENDS` at `:30` | add `"ios"`; a UDID found by usbmux is always `"ios"` |
| connect | `_connect` (`device.py:696`) routes to `_connect_remote` / `_connect_ua`; the daemon hooks it with `_pool_connect` (`daemon.py:595, 961`), and `Session.open` calls `_connect` directly (`daemon.py:277`) | a third branch: `ios.runner.connect(udid)` → `(IosClient, ping)` |
| version gate | `act.Session._connect` upgrades when `versionCode < AGENT_VERSION_CODE` (`act.py:73`); `not-installed` triggers `cli.cmd_setup` (`act.py:79`) | skip the APK gate when `ping.backend == "ios-runner"`; the build ID check lives in `ios/runner.py`. Auto-setup on iOS means "start the runner", never a rebuild |
| `sdk` | `act.py:86`, `_enter` at `sdk >= 30` (`act.py:1236`) | iOS ping has no `sdk`; `ime_enter` is honoured by `IosClient.act` (Return key) |
| raw tree | `snapshot.build` reads `screen`, `windows[]` (`id, type, layer, title, pkg, bounds, root, focused`), nodes (`class, bounds, flags, actions, text/desc/hint/state, id, collection, item, visible, handle, uid`) (`snapshot.py:80-100, 164-230, 365-470`) | `ios/mapping.py` produces exactly these; Android class names are emitted so `LIST_CLASSES`/`_class_role` (`snapshot.py:44-51, 667-692`) work unchanged |
| capabilities | nothing reads `caps` today | `ping.caps` (`has_back`, `events`, `node_actions`, `units`, `launch`, `logs`, `install`); `act.py`/`commands.py` check it in one helper |
| adb inside act.py | `Session.adb`/`shell` (`act.py:149-160`) for `press`, `type` fallback (`input text`), `launch`/`stop-app`/`apps`/`install`/`open-url`/`logs`/`mark`, `shot` fallback | per-command platform dispatch; on iOS each one is either an `ios/devices.py` call or `unsupported` (§6) |
| raw `AgentClient(port)` | `cmd_watch` (`act.py:1646`), `cli._wait_for_agent` (`cli.py:75, 90`), daemon side/sub clients (`daemon.py:287, 384, 404`) | replace `AgentClient(port)` by `dev.open_client(serial, info)` that builds the right class; the daemon opens 2–3 connections per device, so the runner accepts several clients |
| errors | `ERROR_KINDS` (`core.py:55-81`), `DEVICE_ERRORS` (`device.py:283`) | add `not-trusted`, `signing`, `profile-expired` (`IOS.md` §16); regenerate AGENTS.md tables (`tests/test_docs.py`) |
| state | `~/.droidctl/state.json` per serial | per UDID: `{backend:"ios", port, build_id, team, runner_bundle, holder_pid, cdid}` |
| fakes | `tests/fakes.py`: `FakeAgent` (NDJSON over TCP), `FakeAdb` | add `FakeUsbmux` (plist framing, `ListDevices`/`Connect` to a `FakeRunner`) and `FakeRunner` (replays real iOS fixtures) |

## 3. Decisions (user, 2026-10-21)
1. **Framing:** the runner adopts PROTOCOL.md's JSON-RPC 2.0 framing, error codes and notification shape (not the spike's ad-hoc NDJSON). One parser and one `AgentClient` for both platforms.
2. **No auth, no token** (user: "no need to complicate").
   - The runner listens on loopback on the phone and is reached only through usbmuxd.
   - **Accepted risk:** any process of the logged-in Mac user can drive the phone while the runner runs. On Android, adb gives that same user the same power.
3. **Mapping and action translation on the host** (`IosClient`), runner kept lean. Fixes never need a re-sign. (The user left the architecture to us.)
4. **Runner language:** Objective-C, as in the spike: every private call is reached by selector and checked with `respondsToSelector:`. One test bundle plus an empty host app if Xcode requires one.
5. **Device IDs:** `-d` accepts an adb serial or an iPhone's hardware UDID. With both an Android device and an iPhone attached and no `-d`, the existing `ambiguous`/`no-device` path asks for one.
6. **Package layout:** `droidctl/ios/` (Python) and `ios/runner/` (the Xcode project, shipped as package data in the wheel; built per team by `setup`, never prebuilt for devices).
   - **Plus a guided setup** that tells the user exactly where they are needed (§4a).

## 4. I1: runner + transport + snapshot on the iPhone
**Goal:** `droidctl devices` lists the iPhone; `droidctl setup -d <udid> --team <T>` builds, installs and starts the runner; `droidctl snapshot -d <udid>` prints a snapshot. All three work through the daemon and with `--no-daemon`.

Order, each step with its tests:

1. **`droidctl/ios/transport.py`** (pure Python, no iproxy). A usbmuxd client: `ListDevices`, `Listen` (attach/detach), and `Connect(device_id, port)` returning a plain socket (port big-endian).
   - MIT port of agent-device's `runner-usbmux-protocol.ts`, with NOTICE.
   - **Tests:** `FakeUsbmux` over a unix socket in a temp dir; framing round-trips; `Connect` refusal → `connection`.
2. **`droidctl/ios/devices.py`**: `xcrun devicectl` wrappers (list with UDID ↔ CoreDevice ID, `info details` for Developer Mode and iOS version, `processes`, `install`, `uninstall`, `launch`, `copy from`).
   - Every call uses `--json-output` and `DEVELOPER_DIR` from `xcode-select` or `DROIDCTL_DEVELOPER_DIR`.
   - **Tests:** parse recorded devicectl JSON with the IDs scrubbed. No hand-written JSON: capture it with a script.
3. **`ios/runner/`**, the product runner (replaces `S2Server.m`; built from its proven calls only). Rules R1–R12 of `IOS.md` §6:
   - a per-request `@autoreleasepool`;
   - the `recordIssue:` override plus `continueAfterFailure`;
   - no XCTest waiting APIs;
   - `respondsToSelector:` guards on every private call.
   - **Methods in I1:**
     - `ping` (build ID, Xcode and iOS versions, screen, `caps`, `gen`);
     - `tree`: the daemon snapshot with explicit attributes including `IsVisible`, a 5 s timeout, retry on `kAXErrorServerNotFound` for up to ~1 s, and the SpringBoard merge for alerts (`IOS.md` §9.3);
     - `screen_state`, `current`, `gen`, `quit`.
   - **Several clients at once:** one accept thread, a thread per connection; requests are serialised onto the main queue, as in the spike.
   - **`gen`:** a counter bumped by deduped change notifications (register once per process; 5 ms dedupe). This gives the daemon's tree cache what it needs (`daemon.py:424-451`).
4. **`droidctl/ios/runner.py`**:
   - `build(team, udid)`: `build-for-testing` into `~/.droidctl/ios/dd/<team>`, keyed by the source hash and team;
   - `install`, `start` (a detached `xcodebuild test-without-building -only-testing`, log to `~/.droidctl/ios/<udid>/session.log`);
   - `adopt` (ping the stored port; check the build ID);
   - `stop` (`quit`, then kill the holder);
   - `connect(udid)` → `(IosClient, ping)`.
   - **Typed start errors from the session log:** "Unlock iPhone" → `screen-off` (`data.reason=locked-at-start`); not trusted → `not-trusted`; signing → `signing`; expired → `profile-expired` (exact wording after the 2026-10-27 test).
   - **The build ID check is mandatory:** on a mismatch, uninstall the runner and start again once (`IOS-HANDOFF.md` §8).
   - **Tests:** session-log fixtures (real logs, scrubbed); start/adopt state machine with a fake `xcodebuild` script.
5. **`droidctl/ios/mapping.py`** from `research/ios-spike/ios_map.py` (rules R1–R8, `IOS.md` §9.4/9.4b), plus:
   - `IsVisible` → `visible`;
   - the keyboard window → `type:"input_method"`, so `snap.keyboard` works;
   - SpringBoard alerts → a dialog window;
   - handles = `pid/elementID` strings, with the `dump` id from the runner.
   - **Tests:** `tests/test_ios_mapping.py` on `tests/fixtures/ios-device/*`.
   - **New fixtures, captured with `dump-fixture -d <udid>`:** Settings › General, the test app, a long list, an alert, the keyboard up. Never the Settings root, the Home screen or a third-party app.
6. **`IosClient`** (`droidctl/ios/client.py`): an `AgentClient` subclass. `_open` connects through usbmux; `tree()` maps; `act`/`gesture`/`global_action` raise `unsupported` until I2.
7. **Wiring:**
   - `device.list_devices` merges iPhones, and `backend_for` returns `"ios"` for them;
   - `_connect` routes to `ios.runner.connect`, and `dev.open_client` replaces the raw `AgentClient(port)` sites;
   - the `act.Session._connect` version gate is skipped for iOS;
   - `cmd_devices` prints the platform;
   - `cmd_setup` gets the iOS guided setup (§4a);
   - `cmd_ping` and `dump-fixture` work.
   - **The capability helper** `require(caps, name)` lands now, with every Android-only command guarded. It is unit-tested against a fake iOS ping.
8. **Resolver:** don't treat `id` as unique on iOS (a real app reuses identifiers). That's a flag from the mapping (`uniq_ids:false`) that `resolve.py` honours: fall through to role, container and position tiers.
9. **Done when:**
   - `.venv/bin/pytest` is green, including `test_docs.py` and `test_packaging.py`, which now cover `ios/runner` sources (a source hash, like the APK).
   - The on-device e2e `tests/e2e/ios/test_i1.py` (`DROIDCTL_IOS_UDID`) passes: devices, setup, snapshot of Settings › General, snapshot through the daemon, runner restart and adopt, locked phone → `screen-off`.
   - Numbers go into a new iOS section of PLAN.md: start time, snapshot ms per screen, tokens per screen.

## 4a. Guided setup (`droidctl setup -d <udid>`, or `droidctl setup --ios` with one iPhone attached)
**The problem:** an iPhone needs up to five things that only the user can do, on the Mac or on the phone, and today those surface as raw xcodebuild errors.

**The design:** `setup` is an ordered checklist.
- Each step **checks** the state first and skips when it is already fine, so re-running `setup` resumes where it stopped.
- When the user is needed, it prints **what to do, where, and why**, in numbered lines.
- It then **waits**, polling the condition every second (Ctrl-C aborts; a per-step timeout of 5 min). It continues by itself as soon as the condition holds; the user never has to press a key.
- Every step prints ✓ / → / ✗ as it goes.
- **Non-interactive** (`--json`, MCP, or stdout not a TTY): it doesn't wait. It returns the first unmet step as a typed error carrying:
  - `data.step` and `data.instructions` (the same lines);
  - `data.rerun` (`droidctl setup -d <udid>`).
  
  An agent relays the instructions to its human and re-runs `setup`.

**The steps:**

| # | check (automatic) | when the user is needed: what we print | error kind |
|---|---|---|---|
| 1 | Xcode found: `DEVELOPER_DIR`, `xcode-select -p`, then `/Applications/Xcode*.app` and `~/Downloads/Xcode*.app`; `xcodebuild -version` runs | install Xcode from the App Store; or run `sudo xcodebuild -license accept` / `xcodebuild -runFirstLaunch` when those are what fails | `missing-dep` |
| 2 | the iPhone shows in usbmuxd `ListDevices` and is paired | connect the iPhone with a USB cable, unlock it, tap **Trust** on "Trust This Computer?" and enter the passcode | `no-device` |
| 3 | Developer Mode on (`devicectl device info details`) | on the iPhone: Settings › Privacy & Security › **Developer Mode** › On, then restart the phone and confirm "Turn On" after the restart. The switch appears only after Xcode has seen the phone once, which step 2 does | `device` (`data.reason=developer-mode`) |
| 4 | a signing team: Apple Development identities in the keychain (`security find-identity -p codesigning`, team ID from the certificate) | open Xcode › Settings › Accounts › **+** › Apple ID and sign in (a free Apple ID works). With several teams, pass `--team <ID>`; setup lists them | `signing` |
| 5 | the phone is unlocked (`devicectl device info lockState`) | unlock the iPhone and keep it unlocked until setup finishes (xcodebuild otherwise waits forever on "Unlock iPhone") | `screen-off` |
| 6 | build and install (automatic; progress shown; the first build takes about a minute) | — (signing errors map to `signing` with xcodebuild's message) | `signing` |
| 7 | the developer is trusted: a launch attempt doesn't fail with the "not explicitly trusted" error | on the iPhone: Settings › General › **VPN & Device Management** › *Apple Development: your Apple ID* › **Trust** › Trust | `not-trusted` |
| 8 | start the runner, `ping` (build ID check), one snapshot as a smoke test | — | `connection` |

**At the end** it prints a summary:
- the team;
- the profile's expiry, with "re-run `droidctl setup` before <date>" for a free team;
- the runner state;
- the next command to try (`droidctl snapshot -d <udid>`).

**Shared with `doctor`:** the same checklist runs read-only (no waiting, no building).

**Tests:** each step's check is a pure function over captured tool output (devicectl JSON, `security` output, usbmux replies). The step runner is tested with scripted states: a step fails, the "user" fixes it on a later poll, and setup continues.

**I1 scope:** all eight steps. I3 adds expiry renewal, `teardown`'s disclosure (removing the last app removes the Trust) and the full `doctor`.

## 5. I2: actions (summary; details in `IOS.md` §11–§13 and `research/ios-decision.md` §15)
- **Runner `act`:**
  - `IsValid` → lock guard → an AX action from the allowlist only (2003, 2004/2005, 2006/2007, 2010, 2014/2015, set value).
  - On refusal: `VisiblePoint` → enforced hit-test → one touch. The paired-switch rule (R8) touches the recorded inner switch.
- **Runner `settle`:** the §13.3 rule with re-arming on 1018 / 1031 `visible = 1`, the 200 ms floor after AX scroll actions, and the 3000 ms cap. It returns the tree and the deduped events, the same reply shape as the Android agent's settled actions.
- **`IosClient.act` translation table:**

  | droidctl action | runner call |
  |---|---|
  | `click` | AX press, else checked touch |
  | `long_click` | a held touch |
  | `scroll_forward`/`_backward` | 2006/2007 |
  | `set_text` | setAttribute, then read-back compared on normalised digits/text (input masks) |
  | `ime_enter` | AX press on the `Return` key, else `sendString "\n"` |
  | `focus` | a checked touch on the field (setAttribute would change its value) |
  | `expand`/`collapse`/`dismiss` | `unsupported` |
- **Host-side changes in act.py:**
  - `cmd_tap`'s "handled" signal: there's no `clicked` event on iOS, so `performed` from the runner (AX ok, or a hit-test-checked touch) counts as handled. `--expect-change` uses the diff.
  - The `type` fallback chain skips `input text`.
  - `press` maps only `enter`; other keys are `unsupported`.
  - `back` resolves to the navigation bar's back button by role, else `unsupported`.
  - A new `hide-keyboard` command for both platforms (Android: back while the IME shows).
- **`wait_for`, `wait_idle`, `events`/`subscribe`, `current`:** on the runner, notification-driven. AGENTS.md notes that web content needs `wait_for`.
- **The iOS test app** (`ios/testapp`, TESTAPP.md scenarios plus a number-pad field with and without a Done accessory, a secure field and a masked field). e2e uses DTA pulled with `devicectl copy from`.

## 6. Command coverage on iOS
| command | I1 | later |
|---|---|---|
| `devices`, `ping`, `snapshot`/`snap`, `where`, `dump-fixture`, `setup` (build/install/start) | ✓ | — |
| `tap`, `long-press`, `type`, `set`, `focus`, `scroll`, `scroll-to`, `swipe`, `gesture`, `action`, `wait`, `current`, `watch`, `home`, `hide-keyboard` | `unsupported` | I2 |
| `teardown` (uninstall runner and test app; disclose Trust removal), `doctor` (the setup checklist read-only, plus expiry and host sleep) | minimal stop; `doctor` = the read-only checklist | I3 |
| `launch`, `stop-app`, `apps`, `open-url` (`devicectl`), `shot` (`XCUIScreen` JPEG, `--marks` in points), `install` (`.ipa`/`.app` via devicectl), `logs`/`mark` (os_log; design open) | `unsupported` | I4 |
| `back`, `recents`, `notifications`, `quick-settings`, `press <key>` other than enter, `expand`/`collapse`/`dismiss` | `unsupported` with a hint | stays (`back` by role in I2) |

## 7. Risks and how the plan contains them
- **Private API drift with a new Xcode/iOS:** selector guards, plus the Xcode version in `ping`. `doctor` warns when the Xcode/iOS pair is untested.
- **A stale runner binary:** the build ID check on every start (seen 1 in 11 starts).
- **Free-team churn:** `setup` re-signs when `embedded.mobileprovision` expires within 24 h; `profile-expired` names the fix.
- **The daemon's assumptions about Android:**
  - the side client's `gen` round trip, which the runner's `gen` covers;
  - the subscription ring's event types (`daemon.py:48`), with iOS events mapped to `window_state` / `announcement` where they mean the same;
  - the `between_calls` toast logic (no toasts on iOS).
- **Test fakes drifting from reality:** `FakeRunner` replays only real captured trees and real session logs.

## 8. Work order and size (estimates)
| step | estimate |
|---|---|
| transport + FakeUsbmux + tests | 0.5 day |
| devices.py + captured JSON | 0.5 day |
| product runner (I1 methods) | 1.5 days |
| runner.py lifecycle + typed errors | 1.5 days |
| mapping.py + fixtures + tests | 1 day |
| IosClient + wiring + caps + docs regen | 1.5 days |
| e2e I1 + PLAN.md numbers | 0.5 day |

I2 is roughly another 5–7 days, including the iOS test app. These are estimates, not measurements.

## 9. I6–I7: setup without Xcode, and Linux (planned 2026-11-09)
**Why:** every renewal went through Xcode's Apple ID session. On 2026-10-22 its command line lost the account ("No Accounts"), every profile expired by 2026-10-28, and nothing could be signed. Linux has no Xcode at all. Evidence: `research/ios-signing.md` (building, signing, provisioning) and `research/ios-linux.md` (the device side with go-ios).

**Decisions (user, 2026-11-09):**
- **Both account types:** a paid team through an App Store Connect *Team* key (no password, unattended renewal), and a free Apple ID through `droidctl login` (Xcode's private GrandSlam + developer-services protocol; the password goes only into SRP, 2FA once per machine, Apple's token kept in the OS keyring, never the password by default). Today's Xcode path stays as a macOS fallback.
- **A prebuilt runner everywhere:** built unsigned, XCTest frameworks stripped (the Xcode licence forbids redistributing them; the phone loads them from the developer disk image). *Revised 2026-11-10 (user):* committed inside the package like the agent APK (`make runner` → `droidctl/assets/droidctl-runner.zip`, 73 KB, byte-reproducible with the same Xcode; no build-machine paths), not downloaded. `tests/test_packaging.py` fails when the runner sources change without it; GitHub Actions (free on the public repo) rebuilds it on macOS and keeps the fresh zip as an artifact, so a runner change made on Linux can be committed from there.
- **go-ios on Linux:** `setup` asks whether iPhone support is wanted and fetches a pinned go-ios (sha256-checked); `DROIDCTL_GO_IOS` overrides it.
- **Linux verification:** build it, then the user tests it on another Linux host.

**Shape:**
- `ios/lockdown.py` (split out of `syslog.py`): pair record, TLS session, services, `GetValue`, installation_proxy (browse/uninstall), image mounter lookup, Developer Mode. Pure Python, used by both backends; it also takes most of the load off devicectl and CoreDeviceService on macOS.
- `ios/apple/`: `anisette.py` (AOSKit on macOS; an anisette-v3 server on Linux, self-hosted first, a public one only as an opt-in), `gsa.py` (GrandSlam SRP login, 2FA, the Xcode app token), `devservices.py` (teams, devices, App IDs, certs, team profile), `asc.py` (App Store Connect key, ES256 JWT), `secrets.py` (macOS Keychain via `security`, Linux Secret Service via `secret-tool`, a 0600 file fallback).
- `ios/sign.py`: one `sign(app, identity, profile, prefix)`; `/usr/bin/codesign` on macOS, go-ios `sign` on Linux.
- `ios/backend.py` + `ios/goios.py`: the device side (tunnel agent, DDI, install, `runtest`, ps/kill) behind the same functions `devices.py`/`runner.py` call; Xcode stays the default on a Mac with Xcode.
- Certificates: reuse before create; never revoke a certificate droidctl didn't create; error 7460 (cert limit) is a typed error that lists them.

**Work order:**
1. *(no phone, no password)* `lockdown.py`; the stripped unsigned runner built locally; `sign.py` on macOS; the runner artifact manifest (build id from the artifact, not from source + team); the runner zip in the package and the CI workflow (2026-11-10).
2. *(the user's password, once)* `droidctl login` on macOS with AOSKit anisette → team profile for the runner bundle → sign the stripped runner → install → ping (`ios-signing.md` M1, M4). This also unblocks the phone.
3. Renewal through the new provisioner (M8); the token-lifetime log (M5).
4. `asc.py` (unit tests now; M3 when a paid team exists).
5. go-ios backend (`goios.py`, `backend.py`), the Linux setup checklist, the setup question; first tried on this Mac without Xcode (V11), then the user's Linux host (`ios-linux.md` §6.4 V1–V13).
6. Docs: AGENTS.md / SKILL.md / IOS-HANDOFF.md, the command tables regenerated.

**Step 2 measured (2026-11-09, iPhone 12, iOS 18.7.8, free team, Intel Mac):** `droidctl setup --ios` signed in as part of setup (no 2FA prompt: this Mac is already a trusted device for the Apple ID, so AOSKit's anisette carries it), registered the phone, ensured the App ID, downloaded the team profile and signed the prebuilt runner; xcodebuild started it with no Xcode account. **M1 holds:** the stripped runner (372 KB, no `Frameworks/`) runs on the phone; XCTest comes from the developer disk image (runner build `ba1c4a594098` answered on 27100). **M4 holds:** profile "iOS Team Provisioning Profile: dev.droidctl.<prefix>.runner.xctrunner", 7 days. Apple's token is good for a year (`token_duration_s` 31536000; M5 still to be confirmed by use). Found on the way: developer-services replies are gzipped unasked; the spd/app-token plists can come without an XML header; developerservices2 chains to DigiCert, gsa to the Apple Root CA; repeated sign-ins get HTTP 429 (so the token is saved before anything else can fail); a free team has `xcodeFreeOnly` False and says "Xcode Free Provisioning Program" in its membership instead.


### 9.1 Built 2026-11-10: the iPhone without Xcode (step 5); `setup --ios` verified end to end on the user's Linux host 2026-11-10
- Found on that run: Apple refuses (-80045, "This action cannot be completed at this time", at the first SRP step) anisette from anisette-v3-server v2.2.2 (ani.sidestore.io) since 2026-10-12: the default became new.ani.sidestore.io (native-c 0.2.0), with ani.jkcoxson.com tried on -80045; the identity is re-provisioned per server and GrandSlam gets the server's own User-Agent. The free team was at its certificate limit (2 AltStore + 2 Xcode): a free team's Xcode has no Revoke, so `devservices.revoke_cert` revoked them on the user's explicit choice ("iOS Development" via `revokeDevelopmentCert`; Xcode's "Apple Development" ones only via the v1 API, `X-HTTP-Method-Override: DELETE` on `certificates/{id}`, HTTP 204).
- `ios/goios.py`: the pinned go-ios 1.3.2 (sha256 of both release zips; the real download and `ios version` checked on this Mac), droidctl's own userspace tunnel agent per phone (port 28150+, `--pair-record-path` and cwd under `~/.droidctl/ios/go-ios`), `ensure_ddi` (an image already mounted is kept; `image auto` checked through lockdown, not its exit code), install, ps/kill, the `runtest` holder, go-ios's failure wording.
- `devices.backend()`: `xcode` when Xcode is found, else `goios` (`DROIDCTL_IOS_BACKEND` overrides). Every `devices.*` call dispatches: lockdown for device info, lock state, apps and uninstall; go-ios for install, processes and kill. `runner._start` installs the signed runner itself when its build or profile changed, then starts `ios runtest`; a refused launch is told apart (expired profile; lockdown says unlocked → untrusted; else "locked or untrusted").
- `ios/identity.py`: a keychain identity the team lists (macOS), else droidctl's own key in the keyring, else a new certificate (openssl CSR → `submitDevelopmentCSR`). Never revokes; `cert-limit` lists the certificates. `sign.py` signs with go-ios `sign app` (go-codesign; it takes the certificate from the profile, so only the key is kept).
- `apple/anisette.py` `V3Anisette`: the anisette-v3 client ported from xtool (MIT), with a stdlib WebSocket; the identity (UID + adi.pb) in the keyring. `setup` and `login` ask on Linux: Enter = SideStore's public `https://ani.sidestore.io` (v3, checked 2026-11-10; user decision 2026-11-10, as SideStore/xtool do), or the user's own URL; checked before use. `--anisette`, `DROIDCTL_ANISETTE`.
- `setup`: plain `droidctl setup` with no phone asks whether to set up an iPhone; the first step is Xcode, or usbmuxd + go-ios (asks before downloading it); iOS ≥ 17.4.1 without Xcode; it doesn't wait on lockdown's `PasswordProtected` (unverified as "locked now").
- Unit tests: `tests/test_ios_goios.py` (29; the WebSocket exchange against a local server). Not run against a phone yet.
- **Measured 2026-11-10 (this Mac, no Apple ID):** `V3Anisette` against the public server: Apple's lookup + midStart/midFinishProvisioning + the WebSocket session provisioned a fresh machine identity and returned headers in 4.3 s (adi.pb 836 base64 chars); a second run with the kept identity took 1.0 s. A sign-in through it is still to be tried (Linux host).

**To verify on hardware** (the user's Linux host first, then this Mac with `DROIDCTL_IOS_BACKEND=goios`):
1. One command, `droidctl setup --ios` (user decision 2026-11-10: Linux is one command too): it offers `sudo apt-get install -y usbmuxd` + a udev trigger (no replug), waits for Trust; `droidctl devices` lists the UDID (V1); `doctor` reaches lockdown (V2, OpenSSL 3 TLS).
2. Same run: go-ios download (y), anisette (Enter = the public server), anisette provisioning, Apple sign-in (expect a 2FA code: a new machine identity), a new droidctl certificate (watch for `cert-limit` on the free team), profile, go-ios signing.
3. Tunnel, DDI (V3: go-ios's 17E5179g image with our Xcode-26.2 runner; else `DROIDCTL_IOS_DDI`), install, `runtest`, first ping; timings (V4).
4. Untrust / lock at start to see the refused-launch wording (V8); `PasswordProtected` locked vs unlocked (decides `step_unlocked`); `droidctl stop`/`teardown` leaves no runner and no agent (V6, V13).
