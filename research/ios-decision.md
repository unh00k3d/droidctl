# iOS support: decision (proposal)

> **Implementation spec:** `IOS.md` (repo root, 2026-10-21) turns these results into the device-first design. Start there.

_2026-10-16. Synthesis of six reports in this folder:_
- `ios.md` (overview)
- `ios-wda.md` (WebDriverAgent + appium-xcuitest-driver)
- `ios-transport.md` (pymobiledevice3 + go-ios)
- `ios-mobile-next.md` (mobile-mcp + mobilecli)
- `ios-idb-maestro.md` (idb + Maestro)
- `ios-agent-device.md` (agent-device + droidrun ios-portal)

Citations are in those files. **Nothing here was measured on an iOS device or simulator.** Every latency is a figure from another project, labelled with its source. This is a proposal: the user decides, and the spikes in §5 gate it.

---

## 1. Decision in one paragraph

droidctl gets iOS as a **third implementation of `PROTOCOL.md`**, the way Backend B was the second. The host side (daemon, snapshot, resolve, act, spatial, CLI, MCP) stays platform-neutral.

The device side is **our own small Swift XCUITest runner**:
- size: ~2.5–3.5k lines (estimate, `ios-agent-device.md` §5);
- protocol: NDJSON JSON-RPC, a per-launch token, loopback-only;
- `act` and `settle` return the new tree in one reply;
- seeded with ported pieces from agent-device (MIT), WebDriverAgent (BSD-3) and Maestro (Apache-2.0).

Transport:
- **macOS first:** `xcodebuild` / `devicectl` to build and launch the runner, plus a direct usbmux connection from Python. The usbmux connection is a small MIT port of agent-device's client.
- **go-ios** (MIT, as a subprocess) is the later path for "no Xcode" and Linux.

**pymobiledevice3 is never imported** (GPL-3). Stock WDA is used only as a throwaway fixture-capture tool.

An **idb-style simulator helper** with real AX node actions is an optional second backend, built only if a spike proves it.

---

## 2. What each project taught us (feature overlap with droidctl)

| project | license | closest to droidctl in | what we take | what we reject |
|---|---|---|---|---|
| **agent-device** | MIT | product shape: an agent-first CLI with refs and a daemon on Android + iOS | runner listener + token + peer check, main-thread watchdog, `record(_:)` kAXError mute, bounded `snapshot()` + query-sweep tier, SpringBoard modal probe, quiescence-skip wrapper, synthesized touch/text (~1.5–2k lines). Host side: usbmuxd client (333 lines TS), xctestrun env patch, build/test-without-building split, runner adoption (1.15 s re-adopt vs 13–38 s cold) | positional refs that every action expires, host-side coordinate resolution, the blind top-left `back` tap, the 17k-line multi-OS runner as a whole |
| **WebDriverAgent** | BSD-3 | device API breadth | depth cap swizzle, unresponsive-app deadline (**on** by default for us), AX/XPC timeout save/restore, `activeApplications` + hit-test for `current`, (elementID, pid) identity → our `uid` tier, `_XCT_requestScreenshot`, interruption-handler disable, the 3-step text-clear ladder | as a whole: it rewrites keyboard Auto-Correction/Predictive and never restores them; its HTTP + MJPEG servers have no auth, listen on every interface and allow CORS `*`; `/wda/healthcheck` presses Home; alerts are accepted by position; typing doesn't verify focus |
| **Maestro iOS** | Apache-2.0 (9 files are WDA BSD-3) | runner outline | never-returning test hosting a server; prebuilt simulator bundle; `test-without-building`; snapshot walk + oversized-tree recovery (fix its two bugs); private event synthesis | unauthenticated HTTP, coordinates only, screenshot-hash settle, permission auto-tap inside a read, no real-device story |
| **idb** | MIT | the only **node actions** found on iOS (simulator) | axbridge design: in-guest helper, one-call tree (~30 ms / 167 nodes, their figure), AXPress / ScrollByPage / setValue addressed by point **plus a label assertion**, quiescence via AX idle/animation signals, private-selector signature guard, "ignored action reports success" and `writeUnconfirmed` handling | "first match" marker resolution (a position guess); leaving `AutomationEnabled` on |
| **mobile-mcp / mobilecli** | Apache-2.0 / **FSL-1.1 (ideas only)** | direct competitor; one MCP for both platforms | the same wire verbs on both on-device agents; small optional-capability interfaces; runner launch without Xcode (simctl env / go-ios testmanagerd); pinned-hash runner download; reuse-before-launch; view controller as the iOS "activity"; `--locale` on launch | coordinate refs with no staleness, "visible = x,y ≥ 0", blind typing, prose errors, points mislabelled as pixels, hand-written test trees, default-on telemetry |
| **go-ios** | MIT | "adb of iOS" | the whole adb half without Xcode: list/pair, port forward, **root-free tunnel agent reachable by other processes**, DDI, install/launch/kill/apps, syslog/os_trace, crashes, screenshots, app-state notifications, testmanagerd runner launch, Linux re-signing | DDI and runner downloads from a third-party site (deviceboxhq.com) |
| **pymobiledevice3** | GPL-3 | same as go-ios, best iOS 26/27 coverage, HID touch on iOS 27+ | an optional, user-installed subprocess for edge cases only | importing it; its no-root tunnel is per-process |
| **droidrun ios-portal** | MIT (archived) | — | two small robustness ideas | everything else (open server, `debugDescription` text, hard-coded app list) |

**Our differentiator survives on iOS.** No project in this set combines re-resolved refs, unique-or-fail resolution, typed errors and settle+diff verification on iOS. mobilecli and agent-device both tap positions from a fresh dump (`ios-mobile-next.md` §6.1, `ios-agent-device.md` §2.3).

---

## 3. The one real conflict: can iOS do node actions?

Our core rule is "node actions, not coordinates". The evidence on iOS is mixed:

| evidence | where it runs | result |
|---|---|---|
| agent-device: `XCAXClient_iOS performAction:onElement:value:` from the runner | runner process, probably simulator (inferred) | `kAXErrorCannotComplete` for every action; `setAttribute` succeeds but changes nothing |
| idb axbridge: `AXUIElementPerformAction` on a hit-tested element from an in-guest helper, automation mode | separate process inside the simulator | **works**: AXPress, ScrollByPage, ScrollToVisible, setValue |
| Xcode 26.2 XCUIAutomation strings: "scroll to visible (by AX action)"; `_XCT_performAccessibilityAction:onElement:withValue:reply:` in the runner→testmanagerd protocol | runner → daemon → app | Apple's own framework sends AX actions through the daemon; **nobody has called it from a third-party runner** |

**Working position:**
- iOS actions are **element-addressed synthesized touches**. `act` re-finds the element on the device by fingerprint, uniquely or `stale-ref`/`ambiguous`. Then it hit-tests the target point and asserts that the hit element is the one we meant (idb's guard), so a moved element fails instead of tapping whatever is now there. Then it synthesizes the touch.
- This keeps "never guess by position": the coordinate is derived from a just-verified element, never from the host's old snapshot. Results report `method:"touch"`.
- If spike S1 shows that the daemon AX-action route works on a device, `act` uses it first, and our double-trigger policy carries over unchanged.

---

## 4. Architecture

```
host (Python, unchanged above device layer)          iPhone / simulator
┌───────────────────────────────────────────┐       ┌──────────────────────────────┐
│ CLI / MCP → daemon → snapshot/resolve/act │       │ droidctl-ios-runner          │
│                 │                         │ usbmux│  (XCUITest, never-returning) │
│        device.py (PROTOCOL.md client)     │──────▶│  NDJSON JSON-RPC, token auth │
│                 │                         │ 127.0 │  tree / act+settle / gesture │
│        platform/ios.py                    │ .0.1  │  global / wait_* / screenshot│
│         ├ devices, setup, teardown        │       │  current                     │
│         ├ apps: devicectl|simctl|go-ios   │       └──────────────────────────────┘
│         ├ logs: os_trace (go-ios|log)     │
│         └ runner lifecycle + adoption     │
└───────────────────────────────────────────┘
```

**Runner API.** It implements the `PROTOCOL.md` subset that iOS can honour: `ping`, `tree`, `act {handle, gen, action, args, settle}`, `gen`, `gesture`, `global {home, lock}`, `wait_idle`, `wait_for`, `screenshot`, `current`.

**Not supported on iOS:** `subscribe`/`events` (there is no UI event stream), `global back`, `recents`, `notifications`, `quick_settings`, and custom actions unless S1 succeeds. These are **capability flags** in `ping` (`has_back:false`, `units:"pt"`, `node_actions:false|true`). Commands that need a missing capability return `unsupported` with a hint, never a silent no-op.

**Node schema mapping:**

| our field | iOS source |
|---|---|
| `class` | XCUIElementType |
| `id` | accessibilityIdentifier |
| `text` / `desc` | value / label |
| `hint` | placeholderValue |
| `bounds` | frame, in **points** |
| `flags` | enabled, selected, hasFocus, hasKeyboardFocus, traits |
| `window` | app, SpringBoard alert or keyboard |

Expensive attributes (`isVisible`, custom actions) are fetched lazily, per element, only for the element we act on (WDA's biggest speed trap, idb's 130× number).

**Settle.** XCTest quiescence with a short, measured timeout. Report `idle:false` instead of WDA's silent 10 s stall. Then a host tree diff, the same as on Android.

**Typing.** Focus the field, then verify `hasKeyboardFocus` in the snapshot before any keystroke (the wrong-field bug WDA still has and we fixed on Android). Type with synthesized text, then read the value back: `no-change`, or `unconfirmed` with before/after.

**Alerts.** The SpringBoard modal is an extra window in `tree`, rendered as a `dialog` region and acted on by label. Interruption auto-handling is disabled.

**Transport:**
- **Talking to the runner:** usbmux connect to the runner's on-device port works on every iOS version, needs no tunnel and no `iproxy` (agent-device does this).
- **Launching the runner:**
  - macOS: `xcodebuild test-without-building`, or Appium's iOS 17+ `devicectl device process launch` of a preinstalled `.xctrunner` (S2);
  - Linux / no Xcode: go-ios `runtest` over its root-free tunnel agent, owned by the daemon and stopped on teardown;
  - simulator: `simctl launch` with environment variables (mobilecli's approach) or `xcodebuild`.

**Signing:**
- A free Apple ID gives 7-day profiles and 3 apps per device; a paid account removes those limits.
- We ship a prebuilt simulator runner (committed with a source hash, like the APK).
- For devices we build or re-sign it with the user's team at `setup`.

**"Never break the user's device" on iOS (teardown contract):**
- **Never:**
  - write keyboard preferences;
  - call the accessibility-settings reset;
  - add "health checks" that press Home.
- **Always:**
  - uninstall our runner on `teardown` (agent-device leaves it installed);
  - stop the tunnel agent we started;
  - restore anything we changed (location, and `AutomationEnabled` on simulators).
- **Disclose in `doctor` what we can't undo:**
  - Developer Mode stays on, because the host can't turn it off;
  - the developer disk image stays mounted until reboot.

---

## 5. Spikes, in order (each one decides something)

| # | spike | pass → | fail → |
|---|---|---|---|
| **S0** | Capture real fixtures on the iOS 26.2 simulator with the installed Appium WDA 11.1.0: `/source?format=json&excluded_attributes=visible,accessible,nativeAccessibilityElement` on Settings + a list-heavy app | gives the snapshot code real trees to test against (fixtures from real devices, never hand-written) | — (always do it). Simulator only, so WDA's keyboard tampering is harmless there |
| **S1** | Minimal runner calls `_XCT_performAccessibilityAction` (Press, ScrollByPage, Increment) through testmanagerd; simulator first, then a real iPhone; checked against test-app `DTA` events | `node_actions:true`: our action model carries over intact | element-addressed touch with a hit-test assertion (§3) |
| **S2** | Launch a preinstalled `.xctrunner` with `devicectl device process launch`, end the host session, ping the runner again | a resident runner, like Backend B: launch once, re-adopt in ~1 s | the daemon holds the test session; adoption after daemon restart only |
| **S3** | Latency on this Intel Mac's simulator and on one device: `tree`, `act` + settle in one round trip, cold start | sets settle defaults and budgets (agent-device: 350–450 ms tree on an iPhone 17 Pro) | if `tree` is over ~1 s, look at the depth cap / query-sweep tier before anything else |
| **S4** | Element identity stability: (AX elementID, pid) across scrolls, re-renders, app restarts | becomes resolver tier 1 (`uid`) | tiers 2–4 only, as on API 28 |
| **S5** | idb-style simulator helper: read + AXPress, cold/warm latency, `AutomationEnabled` before/after, `simctl spawn` on x86_64 | optional simulator backend with real node actions and a ~30 ms read | simulators use the device runner |

**Ground truth for S1/S3/S5:** an iOS port of the test app (TESTAPP.md scenarios) that logs `DTA` events with `os_log`, read through `log stream` (simulator) or os_trace (device). Never droidctl's own output.

---

## 6. Proposed milestones (after the spikes)

- **I1:** runner skeleton (`ping`, `tree`) on the simulator, `platform/ios.py`, `devices`/`setup` for the simulator, `snapshot` working end to end from S0 fixtures, `units=pt`.
- **I2:** `act` (tap, long-press, type with focus check, clear, scroll by swipe, scroll-to), settle + diff, alerts as a dialog window, `current`, `wait`.
- **I3:** real device: signing at `setup`, usbmux transport, runner adoption, `teardown` uninstalls, `doctor` disclosures.
- **I4:** apps / launch / logs / screenshot (`--marks` in points), MCP and AGENTS.md capability notes, e2e on simulator + device, iOS test app.
- **I5 (optional):** go-ios path for Linux / no Xcode; S5 simulator backend if it passed.

---

## 7. Decisions needed from the user

1. **Adopt this direction** (own runner, element-addressed touches, macOS first, go-ios later)?
2. **Apple account:** do we target free-account signing (7-day re-sign) as supported, or require a paid team for devices?
3. **Test hardware:** the paired iPhone 15 / iPhone 12 show as unavailable in `devicectl`. Which one becomes the iOS dev device? S1 and S2 need a real device.
4. **Scope of v1 iOS:** simulator + device together, or simulator first?
5. **CLAUDE.md's Environment section** still describes a Debian KVM VM. The current machine is an Intel macOS 15.7.9 host with Xcode 26.2 at `~/Downloads/Xcode.app`. Update it?

---

## 8. S0 results (2026-10-19, iOS 26.2 simulator)

**Setup:**
- WebDriverAgent 11.1.0, from the installed appium-xcuitest-driver 10.16.2, built with `xcodebuild build-for-testing`. The simulator needs no signing.
- Run with `TEST_RUNNER_USE_IP=127.0.0.1`. WDA was ready 37 s after `test-without-building`.
- Device: iPhone 17 simulator, iOS 26.2, Xcode 26.2, on this Intel Mac.
- Fixtures: `tests/fixtures/ios-wda/` (6 screens; WDA JSON, points, with a `meta` block).
- The mapper used below was a throwaway script, not committed: WDA JSON → our raw tree → the unchanged `snapshot.build`/`render`.

**Confirmed from the reports (seen, not just read in code):**
- WDA's MJPEG server listened on `*:9100` (IPv4 and IPv6) although the HTTP server honoured `USE_IP` (`lsof`).
- WDA had switched **Auto-Correction and Predictive Text off**: `value=0` on Settings › General › Keyboard, where both default to on. I switched both back on before stopping WDA.
- XCTest's idle wait is slow: "Waiting up to 10s until … is in idle state" before actions, and on Maps "App event loop idle notification not received, will attempt to continue" after the full 10 s.
- A system alert (Maps' location prompt) lives in **SpringBoard's** tree, not the app's. Reading only the foreground app misses it, so `tree` must merge SpringBoard modals (as Maestro and agent-device do).

**Our pruning on iOS trees.** The tree size counts every node, including the many `Other` wrappers.

| screen | tree nodes / depth | raw WDA JSON | our snapshot |
|---|---|---|---|
| Settings root | 168 / 20 | 54 KB | 16 elements, ~166 tokens |
| Settings › General | 125 / 20 | 40 KB | 20 elements, ~193 tokens |
| Settings › Keyboard (18 switches) | 161 / 20 | 53 KB | 37 elements, ~587 tokens |
| Contacts list | 131 / 18 | 39 KB | 26 elements, ~242 tokens |
| Reminders welcome | 94 / 18 | 29 KB | 17 elements, ~253 tokens |
| Location alert (SpringBoard) | — | 50 KB | 11 elements, ~185 tokens, `dialog=yes` |

The host pipeline works on iOS trees with only a mapping layer: clipping, row merge, regions, list hints and the alert as a dialog. These are the iOS-specific things that layer has to handle, all seen in these trees:
1. **Empty overlay windows.** Apps carry extra full-screen windows with no content. Contacts came out as 0 elements and `dialog=yes` until those windows were skipped.
2. **Scroll bars are elements:** `Other "Vertical scroll bar, 2 pages"`, usually twice each. Turn them into the list's `more↓` hint (they even give a page count) and drop them as elements.
3. **Switches come in pairs.** There is a labelled outer `Switch` spanning the whole row and an unlabelled inner `Switch`, which is the actual toggle.
   - **Tapping the outer one's centre did nothing.** It lands on the label text, and WDA's click still returned a normal response. This is the "ignored action looks like success" problem: settle+diff verification is required, and `act` must target the inner control.
4. **Duplicated subtrees** (DAG duplicates, as idb said): `Dictate` twice, and scroll bars twice. `_dedupe` must expect them.
5. **Decorations:** `Image "chevron.forward"` disclosure arrows came out as stray `button "chevron"` elements. Drop them, or absorb them into the row.
6. **Units are mixed in SpringBoard.** Some SpringBoard windows report 1206×2622 (pixels) while everything else is in points (402×874).
7. **`label`, `name` and `value` repeat the same string** on StaticText, which is a large part of the 30–54 KB raw size. `name` is a real identifier only when it differs from `label` (e.g. `com.apple.settings.general`, `KeyboardAutocorrection`).
8. **Element handles go stale after a value change.** WDA's element id for a switch returned 404 right after it toggled. That's an early, unmeasured hint for S4: identity may not survive a state change.

**Operational notes:**
- **Screenshots froze.** Both `simctl io screenshot` and WDA `/screenshot` kept returning the same frame (clock stuck at 12:04) while the accessibility tree kept updating (12:09 → 12:11). On this host a screenshot can be stale; the tree was right.
- **Taps sent without a JSON body did nothing.** I discarded the responses, so whether WDA returned an error is unknown. With `-d '{}'` they worked.
- **The Mac was overloaded** during S0: load average 100–280, a first-boot `BackgroundShortcutRunner` at 100% CPU, plus other user processes. Tree reads took 0.9–2.9 s. **These are not representative timings.** S3 must run on an idle machine with a warmed-up simulator.

**Next:** S1, a minimal runner that tries `_XCT_performAccessibilityAction` (it needs Xcode signed in with an Apple ID only for the device part), and the iOS mapping layer prototyped against these fixtures.

---

## 9. S1 results (2026-10-20): real AX actions work from our own runner

**Setup.**
- A throwaway project (scratchpad, not in the repo) with two targets:
  - a UIKit test app that logs DTA events to os_log and `Documents/dta.log`;
  - an ObjC XCUITest runner.
- Runs:
  - iOS 26.2 iPhone 17 simulator on an idle Mac (load ~3);
  - **iPhone 12, iOS 18.7.8**, signed with a free Apple ID (personal team).
- Ground truth:
  - the simulator app's os_log, via `simctl spawn log stream`;
  - the device app's `dta.log`, pulled with `devicectl device copy from --domain-type appDataContainer`.

**Three routes were tried; all three end in testmanagerd:**
- A: `XCTRunnerDaemonSession.sharedSession.daemonProxy` `_XCT_performAccessibilityAction:onElement:withValue:reply:` (int action);
- B: `XCUIDevice.accessibilityInterface` (`XCAXClient_iOS`) `performAction:onElement:value:error:` with an `XCUIAccessibilityAction`;
- C: that client's `remoteAccessibilityInterface` `performAccessibilityAction:onElement:value:reply:`.

Errors come back as `XCTDaemonErrorDomain` from all three. A and B agreed on every call. C differed once, and only because the list was already at the bottom: it reached the app, but the scroll was refused (CannotComplete).

- The element comes from `[[el snapshotWithError:] valueForKey:@"accessibilityElement"]`, which gives `AX element pid: N, elementID: X.Y`.
- `XCUIAccessibilityAction` has `initWithAXAction:` and `actionNumber`, plus factories: `incrementActionForPlatform:` (2004), `decrementActionForPlatform:` (2005), `scrollToVisibleAction`, `beginMonitoringIdleRunLoopAction`, `detectAnimationsNonActiveAction`. Platform 2 and above is iOS; platform 1 is macOS and throws.

**Results** (identical on simulator and device; DTA = what the app logged):

| action (number) | target | reply | DTA | meaning |
|---|---|---|---|---|
| Press (2010) | custom view with `accessibilityActivate` | ok | `custom ax_activate`, **no touch** | real node action |
| Press (2010) | standard `UIButton` | `kAXErrorCannotComplete` | `button ax_activate` only, **no action fired** | refused cleanly, so the event-gated touch fallback is safe |
| Press (2010) | `UISwitch` | `kAXErrorCannotComplete` | nothing, value unchanged | same |
| ScrollDownByPage (2006) | `UIScrollView` | ok | `ax_scroll 4`, one page (y 0→479 on device, →493 on sim) | real node scroll |
| ScrollDownByPage (2006) | same, at the bottom | `kAXErrorCannotComplete` | `ax_scroll 4`, no movement | like Android `ACTION_SCROLL_FORWARD` returning false |
| Increment (2004) / Decrement (2005) | `UISlider` | ok | 0.50→0.60→0.50 | real |
| 2014 / 2015 | slider | ok | `ax_scroll 5` / `6` (next/previous page) | — |
| 2001, 2002, 2016, 2018 | slider | **ok** | **nothing** | **"ok" does not prove an effect**: keep the diff/DTA check |
| synthesized `tap` (baseline) | custom view | — | `custom touch_ended`, no `ax_activate` | touch path, for contrast |

**Latency.**
- AX action round trip: 1–5 ms, on both the simulator and the device.
- `XCUIElement.tap` (baseline): about 560 ms on the device, because XCTest waits for idle around it.

**Conclusions.**
1. **`node_actions` is true on iOS.**
   - Press, scroll-by-page, increment and decrement all go through testmanagerd from a third-party runner, on a real device with a free account.
   - agent-device's "kAXErrorCannotComplete for every action" was most likely Press on standard controls, which refuse it the same way here.
2. **The action model carries over.**
   - Try the AX action first.
   - If it's refused with CannotComplete, nothing ran: we saw no side effects, but this is not guaranteed for arbitrary apps.
   - Then make one element-addressed touch with the hit-test assertion. This is our existing "event-gated single fallback".
   - Standard UIKit controls (UIButton, UISwitch, and probably most UIControls) will normally take the touch path. Results report `method`.
3. **Replies are not proof of an effect.** Some unknown action numbers return ok and do nothing, so settle + diff stays mandatory.
4. **Element identity:** the same elementID was reported for the same element across the whole run (input to S4).

**Operational notes.**
- `xcodebuild -destination id=` needs the hardware UDID (`00008101-…`), not the CoreDevice identifier.
- Free-account signing needed:
  - the team chosen once in Xcode (the team ID isn't stored where scripts can read it);
  - `-allowProvisioningUpdates`;
  - the user tapping **Trust** under Settings → General → VPN & Device Management.
- The phone must be unlocked. `xcodebuild` waited forever on "Unlock iPhone to Continue" and didn't notice the unlock; it had to be restarted.
- Installed on the iPhone (to remove at the end): `dev.droidctl.s1.testapp` and `dev.droidctl.s1.runner.xctrunner`. Free profiles expire after 7 days.

**Next:**
- S2 (resident runner launched with `devicectl` without an `xcodebuild` session);
- S3 tree latency on the device;
- S4 identity across scroll and re-render.

---

## 10. S2–S5 and extras (2026-10-20, iPhone 12 / iOS 18.7.8 and the iOS 26.2 simulator)

**Spike runner.**
- An XCUITest method (`S2Server.testServe`) that serves NDJSON over TCP. Each request carries a per-launch token passed as `TEST_RUNNER_S2_TOKEN`.
- Commands:
  - reads: `tree` (public snapshot), `snapfast` (daemon snapshot), `hit` (`_XCT_requestElementAtPoint`), `fetch` (`_XCT_fetchAttributes`);
  - actions: `act` (`_XCT_performAccessibilityAction`), `fasttap`/`swipe` (XCPointerEventPath via `XCUIDevice.eventSynthesizer`), `tap` (XCUICoordinate);
  - text: `sendstring` (`_XCT_sendString`), `type` (`typeText`), `setattr` (`_XCT_setAttribute`);
  - other: `screenshot`, `home`, `launch`/`activate`, `bundle_for_pid`.
- Ground truth: the test app's `dta.log`, pulled from the device.
- The Mac was idle (load ~3–4).

### S2: resident runner
| question | result |
|---|---|
| Does the runner survive `xcodebuild` dying (SIGTERM or SIGKILL)? | **No.** testmanagerd ends the runner within seconds in both cases (simulator). |
| Can the `.xctrunner` start without Xcode (`simctl launch`)? | **The app starts, but no test runs.** `XCTestConfigurationFilePath` is empty; the configuration comes over the IDE session (`XCTestSessionIdentifier`), so without an IDE-side peer there is nothing to run. |
| Time from start command to first ping | 5–6 s, warm, including reinstalling a changed build (device and simulator). |

- **Consequence:** the runner lives exactly as long as a host-side session holder.
  - On macOS that is a detached `xcodebuild test-without-building`, started with `nohup`, owned by neither the CLI nor the daemon. A restarted daemon re-adopts the runner through its port.
  - Without Xcode it would be go-ios, which implements the IDE side of the protocol.
- **Free accounts:** removing every app signed by a free personal team drops the trust, so the user must tap Trust again on the next install.

### Transport
| path | loopback-bound listener (127.0.0.1) | IPv4 any | IPv6 any (`::`) |
|---|---|---|---|
| usbmux (`iproxy`, libimobiledevice) | **works, ~3 ms RTT** | works | — |
| CoreDevice tunnel (`tunnelIPAddress` `fd13:56:e7ef::1`, the device's address) | refused | refused | works, 2.8 ms |

- **Loopback-only binding works on a real device over usbmux.** So unlike WDA and agent-device, which bind every interface, our runner never has to be reachable from Wi-Fi.
- The tunnel needs an IPv6 listener. A later option is to bind it to the tunnel address only.

### S3: latency (iPhone 12, medians)
| operation | ms |
|---|---|
| `tree`, public snapshot: test app 62 nodes / Settings 162 / test app with 200-row table 1030 | 84 / 137–147 / 658 |
| `snapfast`, daemon snapshot, same screens | — / **87** / **432** |
| daemon snapshot with `maxDepth` 12 (Settings, truncates to 18 nodes) | 17 |
| AX action (`act`) | **1.0** |
| `hit` (element at point) | 3–7 (34 when it also resolves the pid) |
| `fetch` (attributes of one element) | 3.4 |
| `IsValid` on an element of a dead pid | 4.7 (fails fast with ServerNotFound) |
| AX action on an element of a dead pid | **6200** (30 retries): always check `IsValid` first |
| `fasttap` (synthesized event) vs `XCUICoordinate.tap` | **290** vs 460 |
| `sendstring` "hello" vs `typeText` " world" | 277 vs 1076 |
| `setattr` value | 37 |
| screenshot without / with the PNG sent back (1170×2532 px, 3 MB) | 66 / 372 |
| launch app (`XCUIApplication.launch`) | 2400–2730 |
| activate a running app | 85–800 |
| `home` | 477 |

- Simulator, same Intel Mac: public snapshot 1246 ms and daemon snapshot 743 ms for 1102 nodes.
- Snapshot cost scales with the node count, and a `UITableView` exposes every row (see S4).
- Depth caps cut the work, but they drop content, so they're only for an incremental or partial read.

### S4: element identity (`pid/elementID` from `XCAccessibilityElement`)
| change | same ID |
|---|---|
| re-read, same screen | 48/48 |
| switch value change | yes (same element) |
| AX page scroll and swipe on a 200-row `UITableView` | 200/200, labels unchanged (no recycling seen through AX) |
| app to background and back (another app, or Home) | 216/218 |
| relaunch (new pid) | 0/48 |
| Settings: push General, pop back | 2/30 (Settings rebuilds its cells) |

- **Becomes resolver tier 1 within a pid and screen**, with the usual fingerprint check, because navigation re-creates elements.
- A stale ID after relaunch fails fast through `IsValid`.

### Actions on more control kinds (device; DTA confirms each)
| target | AX Press (2010) | fallback |
|---|---|---|
| SwiftUI `Button` / `Toggle` | **ok**: action fired / toggled, no touch | — |
| UIKit `UIButton`, `UISwitch`, `UITextField`, `UITableViewCell`, `UIAlertController` button, SpringBoard permission-alert button | refused, no side effect | `fasttap` after the hit-test check: fired exactly once (`ax_activate` then `touch_began` then `action`) |
| ScrollToVisible (2003) on an off-screen cell ("Item 40", y 2020) | ok, the row moved to y 304 | — |
| ScrollDownByPage on the table | ok | swipe also works |

- **Text:**
  - tapping the field focuses it, and `sendstring` types character by character (`editingChanged` per character);
  - `"\n"` fires `textFieldShouldReturn`;
  - `setattr` 2006 replaces the value, and `editingChanged` fires once.
- **Long press:** `fasttap` with a 1.0 s hold returns after 1.26 s.

### Hit-test assertion
- `_XCT_requestElementAtPoint` returns the deepest element.
- For a `UIButton` it returns the button's StaticText child, which is a descendant in the snapshot. That's fine.
- For a `UISwitch` it returns an internal `UIView` that isn't in the snapshot, and its `Parent` isn't the switch either.
- **The assertion is therefore: same pid, and the hit element is the target, a descendant of it, or has a frame inside the target's frame** (`fetch XC_kAXXCAttributeFrame`).
- `XC_kAXXCAttributeIsVisible` is 0 for off-screen rows and 1 for on-screen ones.

### Alerts and the current app
- An in-app `UIAlertController` replaces the app's tree with the alert (47 nodes).
- A system permission alert appears in SpringBoard's tree (`Don’t Allow` / `Allow`).
- To find the current app: `hit` at the screen centre, then `bundle_for_pid` (SpringBoard pid 34 on the Home screen).

### S5: in-simulator helper (idb's design, our own code; `simctl spawn`, no XCTest)
- **Automation mode:** it needs `AXSettings.setAutomationEnabled:`; with automation off, apps answer `kAXErrorServerNotFound` (-25215).
  - An XCTest session switches automation mode off when it ends; we saw 1 → 0. So the helper and the runner must not both own it.
  - It was 0 before the experiment and is 0 now.
- **Hit-test:** 13–16 ms. It returns the real control (the `UISwitch` itself, not an inner view).
- **Press:** the same semantics as the daemon route. The custom view and Increment work; UIButton, UISwitch and the alert button refuse (-25200).
- **Set value:** works (1.5 s).
- **Full tree:** `XCTAccessibilityFramework userTestingSnapshotForElement:options:error:` gives 1102 nodes in **320–430 ms**. That's about twice as fast as the runner on the same simulator (743 ms daemon, 1246 ms public).
- **Verdict:** a valid optional simulator backend (no build or signing, faster reads), but not needed: the runner covers the simulator too.

### Snapshot pipeline on real-device trees (`tests/fixtures/ios-device/`)
| fixture | nodes | raw | snapshot |
|---|---|---|---|
| Settings › General | 149 | 16 KB | 35 elements, ~340 tokens |
| Settings root (scratchpad only: holds the Apple Account name and Wi-Fi SSID) | 162 | 17 KB | 17 elements, ~189 tokens |
| test app with 200-row table | 1030 | 98 KB | 214 elements, ~1034 tokens: off-screen rows leak in (below) |

**New mapping issues** (in addition to the eight from S0):
1. **Off-screen rows:** a `UITableView` exposes every row with real off-screen frames. Drop nodes with `IsVisible`=0, or outside their scroll container, and turn them into `more↓`. ScrollToVisible makes `scroll-to` cheap.
2. **Window frames:** every iOS window has a full-screen frame, including the status bar. Window bounds must come from the content, otherwise the status bar occludes everything and the screen reads as a dialog. The status bar should be a system window.
3. **SwiftUI Toggle duplicates:** a labelled Switch plus an unlabelled inner Switch (the same pattern as UIKit's Settings switches in S0).
4. **SpringBoard pixel windows:** windows with 1170×2532 frames, which must be skipped.

### Not tested
- Rotation and landscape coordinates.
- WKWebView content (Safari would expose the user's tabs).
- Multi-touch.
- Keyboard-preference side effects of our runner: we never write any.
- Running for hours or days.
- Free-profile expiry after 7 days.
- A device on iOS 26.

### Cleanup done
- The runner was stopped, `iproxy` killed, and both S1 apps uninstalled from the iPhone; no `dev.droidctl.*` apps are left.
- The simulator is shut down, and its automation mode is restored to 0.
- On the phone, the test app's notification permission was denied during the test; it was removed together with the app.
- The phone's Developer Mode and the pairing are unchanged (both were on before).

### Decisions this settles
- **`node_actions:true`:** AX actions first; on CannotComplete, one checked `fasttap`. Each result reports `method`.
- **Runner lifetime:** a detached `xcodebuild` session holder on macOS. The daemon re-adopts the runner through its port; a restart costs ~6 s.
- **Transport:** usbmux to a loopback-bound port (port the usbmux client, MIT). The tunnel is an IPv6 fallback.
- **Tree:** the daemon snapshot with explicit attributes, including `IsVisible`.
- **Identity:** `pid/elementID` as tier 1, plus a fingerprint check, plus an `IsValid` pre-check.
- **Typing:** `_XCT_sendString` after a focus check. `setattr` serves as `clear`/`set_text`.

---

## 11. Remaining experiments (2026-10-20): web views, multi-touch, rotation, side effects, profile, soak

The test app gained:
- a `WKWebView` holding a local HTML page (button, input, link, checkbox) that reports through a script message handler into DTA;
- a `UIPinchGestureRecognizer` view;
- landscape support;
- logging from `viewWillTransition`.

Every result below was checked against DTA, on both the iPhone 12 (iOS 18.7.8) and the iOS 26.2 simulator, unless noted.

### WKWebView
| | device | simulator |
|---|---|---|
| web content in the tree after launch | 0.74 s | 3.3 s (before that, only the WKWebView and its scroll bars) |
| AX Press on web Button / Link / Checkbox | ok, `click` fired / ok / `checkbox true` | same |
| tap the input, then `sendstring "abc"` | `input value=a…abc` | same (the tree read straight after showed "a": a value lag) |
| `setattr` 2006 = "xyz" | ok, `input value=xyz` | same |

- Web content maps cleanly: Button 9, TextField 49 with the placeholder as its value, Link 42, Switch 40 for the checkbox. Each comes with a duplicate StaticText label.
- The settle logic must wait until the web view stops being an empty shell.

### Multi-touch
- A two-finger pinch: two `XCPointerEventPath`s in one `XCSynthesizedEventRecord`. Scale 3.26 out and 0.27 in, with `touches=2`, on device and simulator.

### Rotation
- Rotating with `XCUIDevice.orientation = LandscapeLeft` gives `rotate size=844x390`.
- **The tree switches to landscape interface points:** the app rect becomes 844×390, and the custom view moves to (16, 75, 812, 44).
- **Hit-testing and synthesized events stay in portrait device points.** For device orientation LandscapeLeft, the tree point (x, y) maps to (H − y, x), where H is the landscape height.
  - All four `interfaceOrientation` values of the event record hit with the converted point, and none hit with the raw tree point. So the record's orientation doesn't matter; the conversion does.
- **Screenshots stay portrait pixels:** 1170×2532 in landscape. `--marks` must rotate.
- AX Press doesn't care about orientation.
- **Device LandscapeRight: (x, y) maps to (y, W − x)** (W = landscape width). Confirmed on both simulator and device by a hit-test match plus DTA `custom touch_ended`.
- **PortraitUpsideDown:** not reachable on either. Setting it left the app in landscape-right (844×390), and the same mapping applied. iPhones with Face ID don't rotate apps upside-down.

### Side effects of our runner (simulator preferences diffed before and after a full session)
- **`com.apple.keyboard.preferences`:** autocorrection and prediction are unchanged. Unlike WDA's S0 behaviour, our runner writes no keyboard preferences. The only new keys are written by the keyboard itself when it first appears: `DidShowContinuousPathIntroduction`, `KeyboardsCurrentAndNext`, and `CPDidDiscover` in `com.apple.keyboard`.
- **`com.apple.Accessibility`:** unchanged, including `AutomationEnabled` 0.
- Other changed plists are background daemons (CloudKit, routined, healthd, …) running during the boot, and unrelated.

### Free provisioning profile
- `embedded.mobileprovision` of both apps: `CreationDate` 2026-10-20T10:28Z, `ExpirationDate` 2026-10-27T10:28Z, **exactly 7 days**.
- `ProvisionedDevices` holds this iPhone only, and `get-task-allow` is 1.
- `doctor` can read the expiry and warn.
- Uninstalling every app from the personal team removes the trust: the reinstall today failed with "Developer App Certificate is not trusted" until the user tapped Trust again.

### Soak (long runs)
**Simulator, 20 min, 866 iterations.** Each iteration was a daemon snapshot (1100 nodes) plus an AX press; every 5th iteration added a `fasttap`, every 10th a public tree read and an AX-cache flush.
- **0 errors.** Latency was flat: snapshot p50 ~638 ms, act 1.1 ms, tap 288 ms.
- **Runner memory grew ~11 MB/min (140 → 311 MB).** The cause was autoreleased snapshot objects piling up, because requests ran inside long-lived run-loop turns.
- **Fix:** `@autoreleasepool` around each request, and around each socket line. A re-run of 8 min, 321 iterations, 0 errors, grew **150 → 154 MB** (~0.4 MB/min).
- **The real runner must drain a pool per request.**

**iPhone, 30 min, 1648 iterations (build without the fix).**
- **0 errors.** Latency was flat: daemon snapshot of 1030 nodes p50 460 ms, act 2.7 ms, `fasttap` 272 ms, public tree 702 ms.
- RSS went 93 → 357 MB, then iOS trimmed it to ~300 MB. The runner was never killed.
- **With the fix:** 7 min, 384 iterations, 0 errors, **RSS flat at 82.0 MB**.

### Lock screen
`pressLockButton`, on the device:
- **The runner survives the lock:** it answered ping during more than 9 min locked.
- **The app's tree collapses to 1 node.** Its state reads 3 (background) or 2 (suspended). SpringBoard shows the cover sheet: window `SBCoverSheetWindow`, `lockscreen-date-view`, `proud-lock` labelled **"Locked"**, plus the flashlight and camera buttons.
- **AX actions still reach the app behind the lock screen:** `act` Press returned ok, and DTA shows `custom ax_activate` at +2 s, +10 s and +40 s while locked.
  - droidctl must detect the lock (SpringBoard `Locked` / app state ≠ 4) and refuse to act, with a typed `locked` error.
- **`XCUIApplication activate` while locked records an XCTest failure ("Failed to activate application"), and that failure ended the test method.** The runner died. Two fixes were tested:
  - **Override `-recordIssue:`** (plus `continueAfterFailure = YES`) so failures become error replies.
    - Verified on the simulator: `typeText` with no focus now returns `ok:false, issues:[…keyboard focus…]` after XCTest's own ~41 s of retries, and the runner keeps serving.
    - But `XCUIApplication activate` of a missing app still blocks for more than 2 min even with the issue swallowed.
  - **Launch and activate through the daemon instead:** `_XCT_launchApplicationWithBundleID:arguments:environment:completion:`.
    - A missing app fails in ~0 ms with "The request to open … failed".
    - An installed app opens in 60 ms. A running app is brought to the front with the same pid, so no relaunch (simulator).
    - Locked device: it fails in 10–60 ms, and the runner stays alive.
  - **Rule for the real runner:** no waiting XCUIElement or XCUIApplication API on the request path (`launch`, `activate`, `typeText`, `tap`, `waitForExistence`). Use daemon calls with completions and our own timeouts, and still override `recordIssue:` as a backstop.

### Stale runner code on the simulator
- After rebuilding, `test-without-building` on the simulator once kept running a **previous** build of the test bundle: a command added in the new build answered "unknown cmd", although the installed file contained it. Uninstalling the runner app fixed it.
- **The runner's `ping` must report a build ID**, and the host must reinstall when it doesn't match.
- **After unlock, the runner was alive.** Unlocked with the cover sheet still up, `dlaunch` failed ("request to open … failed") although the app's tree was back. With the cover sheet dismissed, `dlaunch` was ok and the app state 4.
- Lock states droidctl must distinguish:
  - **locked:** SpringBoard `Locked`, so refuse;
  - **cover sheet up, unlocked:** the window `SBCoverSheetWindow` without `Locked`; launches fail, so report it or dismiss it with `home`;
  - **clear.**

### What remains untested, and why
- **A device on iOS 26:** the only available iPhone runs iOS 18.7.8, and the other paired iPhone (iPhone 15) is unavailable. The iOS 26.2 **simulator** ran S0, S1, S5, the web/pinch/rotation rounds and the soak with the same results. A device on iOS 26 needs that hardware.
- **Runs of days, and free-profile expiry in practice:** the expiry date is readable (7 days). Waiting it out wasn't done.

### Cleanup (end of §11)
- Both runners were stopped and `iproxy` killed.
- Both S1 apps were uninstalled from the iPhone; no `dev.droidctl.*` apps are left. The next install needs Trust again.
- The simulator is shut down, with automation mode 0.
- Spike sources were refreshed in `research/ios-spike/`.

### Build-ID check and lock guard (follow-up)
**Build ID.** The runner is compiled with `GCC_PREPROCESSOR_DEFINITIONS=… S2_BUILD_ID="<id>"`, `ping` returns it, and the start script reinstalls on a mismatch (`research/ios-spike/sim-build-start.sh`).
- Four builds on the simulator: A ok, B ok, **C came up as B (stale)**, which the script detected and repaired by uninstalling and restarting, after which it reported C. D ok.
- The stale case followed a clean `quit` of the previous runner, not a force-kill. So it can't be predicted, and the check is required.

**Lock guard.** Guarded commands: `act`, `fasttap`/`tap`/`swipe`/`pinch`, `type`/`sendstring`/`setattr`, `dlaunch`/`launch`/`activate`.
- The guard calls `screenState` first: a hit-test at the screen centre, then `_XCT_requestBundleIDForPID`.
  - If that bundle isn't SpringBoard, the state is `clear`.
  - Otherwise, or with no hit, it takes a SpringBoard snapshot. `Locked` gives `locked`; `SBCoverSheetWindow` gives `cover-sheet`; no hit and nothing recognisable gives `screen-unknown`.
- `locked`, `cover-sheet` and `screen-unknown` are refused with that error.

| | simulator | iPhone |
|---|---|---|
| cost when clear (median, per guarded command) | 6.2 ms | 5.6 ms |
| locked: act, fasttap, dlaunch, sendstring (+ activate, launch, setattr on the device) | all refused `locked` in 0.2–0.6 s | all refused `locked` in 0.10–0.19 s |
| DTA during the locked window | no events | **no events** (12:52:02–12:52:40) |
| recovery | `home`: `cover-sheet`; `home` again: `clear`; act ok | user unlock: `clear` after 25 s; dlaunch ok, act ok (DTA 12:52:40) |

- **The first device version failed open.** On a locked iPhone the centre hit-test returns **no element** (screen off), so the state read `unknown` and nothing was blocked:
  - the press reached the suspended app (ServerNotFound after 30 retries);
  - a tap went to the lock screen;
  - `activate` hung the runner.
- **The guard must fail closed**: no hit means look at SpringBoard, and when nothing is recognisable, refuse.

### 2-hour soak, simulator (runner with the lock guard and per-request pools)
- **Result:** 3290 iterations, **1 error**. Runner RSS stayed **134–154 MB** throughout, with no growth.
- **Speed while awake:** snapshot p50 640–880 ms, act (including the guard) 7–8 ms, `fasttap` ~295 ms.
- **Host sleep:** the Mac went into lid-closed sleep at 17:10:43 ("Clamshell Sleep") and DarkWake-d at 17:40:46. **The runner and its `xcodebuild` session survived 30 min of host sleep.**
  - The one error was a `snapfast` timeout in the first second after wake.
  - In DarkWake, with the lid closed and the CPU thermally throttled, snapshots took 4.7–9 s.
  - At 17:46:45 the Mac slept again ("Dark Wake Thermal Emergency").
- **Consequence:** droidctl must treat timeouts right after a host wake as retryable, and `doctor` can report the host's sleep and thermal state.

### End-of-session state
- iPhone: no `dev.droidctl.*` apps; the runner stopped; `iproxy` killed.
- Simulator: shut down, automation mode 0.
- Spike sources are in `research/ios-spike/`.

### Follow-up runs (2026-10-21, Mac awake, load ~7)
- **Build-ID check, 5 more rebuild cycles (simulator):** all 5 came up with the expected ID. Across all runs, **1 stale start in 11.** It's rare and unpredictable, and the check repairs it, so it stays mandatory.
- **Lock guard, 10 automated cycles (simulator):** lock, then `screen_state`, then act / fasttap / dlaunch, then `home` ×2, then act.
  - **10/10 cycles passed.** Each went `locked` → `cover-sheet` → `clear`, and every action was refused `locked`.
  - DTA logged exactly the 20 expected `ax_activate` events (one before and one after each lock), and **0 inside a locked window**.
- **30-min soak, simulator, with `caffeinate` (Mac kept awake):** 1231 iterations, **0 errors**, RSS 134–141 MB, snapshot p50 645–889 ms. This confirms the single error in the 2-hour run came from the host sleep and wake.
- **The iPhone soak with `launch`** (XCTest `XCUIApplication.launch`) sat in "Wait for dev.droidctl.s1.testapp to idle" for over 20 minutes.
  - Meanwhile the runner kept answering other requests from the nested run loop, at ~300 ms.
  - That's the third XCTest wait seen hanging (after `activate` on a missing app or a locked phone, and `typeText` without focus). **No XCTest waiting call on the request path.**
- **Rotation, all four orientations (iPad (A16) simulator, iOS 26.2, the test app built with all iPad orientations).** Each conversion was confirmed by a hit-test match and DTA `custom touch_ended`:

| device orientation | app frame | tree point (x, y) → touch/hit point |
|---|---|---|
| Portrait | 820×1180 | (x, y) |
| PortraitUpsideDown | 820×1180 | (W − x, H − y) |
| LandscapeLeft | 1180×820 | (H − y, x) |
| LandscapeRight | 1180×820 | (y, W − x) |

W and H are the app frame's width and height in the current orientation. The iPhone results for LandscapeLeft/Right match this table.

- **`test-without-building -only-testing` installs only the runner, not the target app.** The test app had to be installed separately (`devicectl device install app` / `simctl install`). The real setup must install the runner itself, without relying on the target-app install.
- **Final iPhone soak** (runner `devfinal`: per-request pools, lock guard, `dlaunch`), 30 min: 1647 iterations, **0 errors**.
  - **RSS flat at 79–82 MB.**
  - Snapshot of 1030 nodes p50 451–457 ms; act including the guard 7.7–8.2 ms; `fasttap` 276–283 ms.
- **Cleanup:** the runner was stopped, `iproxy` killed, and both apps uninstalled from the iPhone (0 `dev.droidctl.*` left). All simulators are shut down (0 booted), the simulator's automation mode is 0, and the spike sources are refreshed in `research/ios-spike/`.
- **Still not obtainable here:**
  - an iOS 26 *device*, because only the iOS 18.7.8 iPhone 12 is available;
  - a free profile actually expiring, which takes 7 days (the expiry date is readable from `embedded.mobileprovision`).

### Pending: free-profile expiry in practice (user decision 2026-10-21)
- **Not available:** an iOS 26 device. The user has none, so the iOS 26.2 simulator results are the iOS 26 coverage (known limitation).
- **Expiry test:** both S1 apps were reinstalled on the iPhone 12 and are left there on purpose.
  - Profiles: runner 357232e6… and test app e52a9f1c…, created 2026-10-20T10:28Z, **expiring 2026-10-27T10:28Z**.
  - **Untrusted** launch error recorded today: `devicectl device process launch` gives "Unable to launch … because it has an invalid code signature, inadequate entitlements or its profile has not been explicitly trusted by the user" (BSErrorCodeDescription = Security).
  - **Trusted baseline (2026-10-21):** once the user tapped Trust, the same `devicectl device process launch` printed "Launched application with dev.droidctl.s1.testapp bundle identifier."
  - **After the expiry date:** re-run the launch and an `xcodebuild test-without-building`, record the exact errors here, then uninstall both apps.

---

## 12. Settle on iOS (2026-10-21, iPhone 12 / iOS 18.7.8; probe runner builds `ax1`–`ax5`)

**Question:** with no known UI event stream, how does the runner know an action has finished changing the screen? The design that came out of it is in `IOS.md` §13; this section is the evidence.

**Method:**
- Found the APIs in Xcode 26.2's `XCUIAutomation`/`XCTestCore`: strings, runtime introspection on the simulator, and the Ghidra decompile of the x86_64 simulator slice via rea.
- The probe runner gained these commands:
  - `methods`/`protos` (introspection);
  - `axnames`, `axreg` (subscribe), `axevents`;
  - `idle` (the two idle callbacks);
  - `sig` (FNV-1a of type|frame|label|value of on-screen nodes from a 4-attribute daemon snapshot);
  - `watch`: run an action, then record notifications, idle replies and polled signature changes with a per-node diff.
- **Mode E** (notifications + idle replies, no polling) and **mode P** (continuous polling, no idle requests) ran separately, because polling delays the idle replies.
- Scripts: `research/ios-spike/settle1.py` (sim), `settle2.py`, `settle3.py` (3 rounds × 14 scenarios × 2 modes), `settle_eval.py` (rule scoring), `fling.py` (DTA cross-check).

**Findings:**
1. **There is an event stream.** `_registerForAXNotification:error:` plus a wrapper on the daemon session's `_XCT_receivedAccessibilityNotification:fromElement:payload:` deliver screen-changed (1000), layout-changed (1001), alert (1006), page-scrolled (1009, "rows 7 to 14 of 200"), per-key text changes (1017), first responder (1018), pid status (1021), value/frame changes (1025), and XCTest user-testing events (4002):
   - `TouchEventsCompleted`, `KeyEventComplete`;
   - `ViewDidAppear`/`ViewDidDisappear` with `controllerClass`/`controllerTitle`;
   - `RunLoopIsIdle`, `AnimationsNonActive`.
   
   Most arrive twice.
2. **The idle callbacks work from our runner** and cost 3–13 ms when the app is idle.
   - Run-loop idle fired **7–8 ms after the app's own DTA "scroll end" in 8/8 flings** (deceleration 0.9–2.8 s).
   - Animations idle fired at ~370 ms in those flings, so it does not cover deceleration. It does cover alert and navigation animations (+250–430 ms), which run-loop idle doesn't.
3. **The rule "both idle replies, then 100 ms without a change notification, then one snapshot" saw the final screen in 42/42** scenario runs (flings judged by DTA).
   - A 150 ms quiet window alone was early in 8/42.
   - Waits are in `IOS.md` §13.3.
4. **Three ground-truth traps, each fixed before scoring:**
   - `NSString.hash` samples ~96 characters (the first signature missed most changes);
   - off-screen recycled `UITableView` cells come back with shuffled labels on every snapshot;
   - empty full-screen wrapper nodes come and go for ~400 ms after transitions.
   
   Diffs must use mapped, on-screen nodes.
5. **A snapshot just after `launch` brought the test app to the front hung for the full 30 s** in 2/3 rounds. Snapshots need a short timeout and a retry.

**Profile note:** the probe runner was rebuilt and reinstalled five times today (same team). Xcode kept the same profile (`357232e6…`, expiring 2026-10-27T10:28:09Z, checked after the last build), so the expiry test is unaffected; the test app was not reinstalled.

---

## 13. First real app: a third-party UIKit app's test build (2026-10-21, iPhone 12; user request)

**Scope:**
- **Target:** a sanctioned test build of a third-party UIKit app (not named here).
- **Screens:** pre-login only, its test-configuration screen and two of its pickers.
- **Not touched:** no login, no credentials, nothing that changes server-side state. The debug and log switches, the language button and "Default Login" were not pressed.
- **State left as found:** every setting as it was.
- **Data:** trees are kept in the scratchpad only (internal environment names); no fixture is committed.

| check | result |
|---|---|
| visible to accessibility | **yes, fully**: 90–92 nodes. Unlike the hardened production app on Android, nothing is hidden from our runner |
| daemon snapshot | 58–76 ms (median 76 over 10) |
| identifiers | 31 of the elements carry `accessibilityIdentifier`s (component-style names such as `SegmentedControl_1`): good resolver input |
| launch from the Home screen to idle | `dlaunch` 85 ms; both idle replies at ~1.1 s; value/frame notifications (1025) kept arriving until ~5.4 s (unidentified, probably an animated component) |
| AX Press on cells, segments, the back button | **refused** (UIKit). The checked touch landed every time: the hit element's frame was inside the target (a 331 pt-wide child of a 358 pt cell) |
| tap → settled screen (§12 rule) | push a picker 574 ms; back 567–572 ms; segment change 423–536 ms (the action itself ~310–320 ms) |
| segment state | `selected` on the chosen `SegmentedControl_N` button; it flipped and restored correctly |
| identity | 31/31 same IDs on re-read; **7/31 after a picker push and pop** (cells rebuilt, as in Settings), so the fingerprint check is needed |
| through the snapshot pipeline (throwaway mapper) | 17 elements, ~178 tokens from 10 KB |

**Mapping gaps it showed** (to fix in `ios/mapping.py`):
1. A switch row renders as a plain row: "Debug Off" loses the Switch and its `value 0`. Render it as a switch with its state.
2. The selected segment's button is absorbed into its container cell (the cell's label is the selected segment's title): "row Default" plus two buttons.
3. Disclosure `chevron` buttons render as `button "chevron" disabled` (S0 rule 4).
4. Idle `ActivityIndicator`s labelled "Progress halted" become noise suffixes on rows ("Download Log · Progress halted"). Drop them, or show them only while animating.
5. "Horizontal scroll bar, 1 page" renders as text (S0 rule 2).

### 13b. Logged in (2026-10-21; test credentials supplied by the user, not recorded here)

**Can a standard UIKit control be activated without a touch? No.**
- Every AX action number in 0–120, 1000–1100, 2000–2120, 3000–3030, 4000–4030 and 5000–5120 was sent, one at a time with a liveness check after each (`research/ios-spike/sweep2.py`), to a `UIButton`, a `UISwitch`, a `UITableViewCell` and a `UITextField` in the test app.
- The app's DTA log shows **no** button action, switch toggle, cell selection or field focus from any of them. 2069 scrolled the table, as the known scroll actions do.
- 24 numbers reply `ok` and do nothing: 2001, 2002, 2004, 2005, 2016, 2018, 2022, 2024, 2027–2031, 2037–2039, 2043, 2051, 2052, 2055–2057, 2065–2067.
- **2025, 2042 and 2043 terminated the app** (state 1 afterwards; 2025 replies "refused"). A runner must send only a fixed allowlist of action numbers, never arbitrary ones.
- This matches Apple's documented VoiceOver behaviour: when `accessibilityActivate` isn't implemented, VoiceOver taps the element's activation point. So for UIKit apps the touch fallback is the platform's own mechanism, and that app is all UIKit.

**What *is* coordinate-free, measured on that app:**
- **Text entry:** `_XCT_setAttribute` value (2006) on the field.
  - It logged in with every login field set this way, and no tap on any field (the password read back as bullets).
  - The app's input mask still formatted the value.
  - Setting a value also brought the keyboard up (the field became first responder).
- **Scrolling:** ScrollToVisible (2003) moved the covered login button above the number pad.
- **Focus check:** `XC_kAXXCAttributeResponderElement`, fetched on the application element, returns the focused field's row (a cell that contains the field). No snapshot attribute exposes keyboard focus: `HasFocus`/`HasKeyboardFocus` are not real names. The real attribute list (Xcode 26.2) is in the session notes; it includes `VisiblePoint`, `CenterPoint`, `ResponderElement`, `ActiveKeyboard`, `SelectedTextRange`, `HasNativeFocus`, `UserTestingActions` and `CustomActions`.

**How the touch is addressed (the rule for the real runner):**
1. Read the element's own `XC_kAXXCAttributeVisiblePoint`. iOS computes it on the visible part of the element: for the login button under the number pad it gave (17, 551), the 3 pt strip above the keys, while the centre lay on the "2" key.
2. Hit-test that point. Touch only if the hit element is the target or lies inside its frame; otherwise refuse with `occluded`.
3. One touch, then settle (§12) and verify.

**My error in the probe:** a helper computed the hit-test without enforcing it, and one tap at the login button's centre landed on the number pad ("2" key; the events show `KeyboardKeyTouchDown`/`KeyEventComplete`). The field values were unchanged afterwards (the mask dropped the extra digit). The helper was fixed to the rule above before anything else was pressed.

**Tour (read-only):**
- **Covered:** the five tabs (a money-moving tab opened only to look), two list screens and back.
- **Settle and size:** tab switches settled 417–427 ms; pushes 590–617 ms. Snapshots took 115–236 ms for 95–225 nodes.
- **AX Press:** never accepted; every press took the touch at the visible point, and every hit-test passed.
- **Data:** everything is test data; trees stay in the scratchpad.

**New mapping and resolver issues:**
1. **The menu tab: 76 elements, ~1175 tokens.** A stacked off-screen section is reported at overlapping frames, which produced 60+ `overlap` warnings and "covered" rows. Nodes outside the visible area or stacked behind others need the off-screen rule plus a check for collapsed sections.
2. **Every menu row has an unlabelled icon button** ("button (unlabeled, in …)"). Absorb it into the row.
3. **The same identifier appears on different elements:**
   - a tab bar button and a menu row for the same feature carry the same identifier;
   - two different number fields on the login form carry the same identifier.
   
   The resolver must not treat `id` as unique on iOS. Disambiguate by role, container and position (the existing tiers).
4. **The session ends when the app spends minutes in the background** (the sweep) and returns to the welcome screen. Agents must expect a re-login.

---

## 14. iOS mapping prototype (2026-10-21; `research/ios-spike/ios_map.py`, compared by `map_compare.py`)

**Cause of the menu blow-up:** the rows below the list's visible area report correct frames for the cell itself, but every child (label, icon button, chevron) reports the list's top-left corner (0, 91). So they rendered as a pile on top of the visible rows. droidctl's own clipping can't catch it, because (0, 91) is inside the list.

**Rules added in front of the unchanged `snapshot.build`/`render`** (the spatial layer does the rest):

| rule | what |
|---|---|
| R1 | inside a cell, drop descendants entirely outside the cell's own frame (never laid out) |
| R2 | tables/collections get `collection {rows}`, cells get `item {row}`, and scroll actions only towards rows that are really hidden, so the normal "15/26 more↓" hint works |
| R3 | drop decorations: chevron buttons/images, idle `ActivityIndicator`s ("Progress halted"), scroll-bar nodes |
| R4 | the app's main window is the full screen; only later windows (alerts, sheets, keyboard) get a content box (otherwise a screen without a bottom element reads as a dialog) |
| R5 | an unlabelled button that fills ≥60% of a labelled cell is folded into the cell |
| R6 | a cell holding a switch: the switch is named by the row's title label, which is consumed; the cell is not a tap target |
| R7 | a cell labelled with its selected segment's title is only the segment strip (the selected button keeps `selected`) |
| R8 | a labelled switch wrapping an unlabelled inner switch is one element; the inner one is recorded as the touch target (`touch`, `touch_rect`) |

**Result on every real-device iOS tree captured** (elements / estimated tokens / warnings; that app's trees stay in the scratchpad):

| tree | throwaway mapper | prototype |
|---|---|---|
| app: menu tab | 76 / 1175 / 65 | **25 / 211 / 0** |
| app: a tab | 48 / 537 / 0 | 21 / 208 / 0 |
| app: a list screen | 38 / 433 / 1 | 24 / 313 / 1 (a real overlap: a floating button over the last row) |
| app: test settings | 17 / 178 / 0 | 14 / 140 / 0 (switches now `switch "Debug" off`) |
| app: other tabs ×6 | 16–29 / 181–333 | 15–28 / 168–320 |
| test app (200-row table) | 214 / 1034 / 0 | **23 / 178 / 0** ("12/200 more↓") |
| Settings › General | 35 / 340 / 0 | 13 / 141 / 0 ("10/21 more↓") |

**Still open:** the runner snapshot doesn't request `IsVisible` yet (R1 works from frames alone); the paired-switch touch target must be honoured by `act`; and duplicate identifiers (§13b) are for the resolver, not the mapper.

---

## 15. The open items from the handoff (2026-10-21, iPhone 12 / iOS 18.7.8; probe runner builds `p6`–`p8`)

**Scope:** the measurable items of `IOS-HANDOFF.md` §11. Scripts: `research/ios-spike/exp7.py` (sections `names`, `focus`, `hang`, `long`, `web`), `exp8.py` + `settle_eval2.py` (settle re-validation), `exp9.py` (AX page scroll against DTA). Raw data: `research/ios-spike/results/exp7-*.json`, `exp8-*.json`, `exp9-*.json`, `exp10-*.json` (timings and event metadata only; Safari's page title was removed, no QA tree stored).

**Runner additions** (probe only): `settle` (the §13.3 rule on the runner, with optional re-arming), `syms` (read `uint32` globals by name), `appax` (register the application element for `fetch`), and a `timeout` for `snapfast`.

**Profiles:** the runner was rebuilt three times. `embedded.mobileprovision` in the new builds is still `357232e6…` (runner) and `e52a9f1c…` (test app), both expiring 2026-10-27T10:2xZ, so the expiry test is unaffected. The test app was not reinstalled on purpose.

### 15.1 Notification names
- **Confirmed at runtime** (`dlsym` of UIKit's public constants on the phone): 1000 `UIAccessibilityScreenChangedNotification`, 1001 `LayoutChanged`, 1008 `Announcement`, 1009 `PageScrolled`, 1033/1034 `Pause`/`ResumeAssistiveTechnology`.
- **Named by their payloads:**
  - 1031 carries `{visible = 1}` / `{visible = 0}`: **keyboard visibility**. It's posted by SpringBoard (pid 34) and by the app.
  - 3031 carries `code = kAXAppAccessibilityReadyNotification` and the bundle ID: the app's accessibility is ready, 0.74–1.19 s after a cold launch.
  - 1008 during keyboard changes carries `AXAnnouncementTypeKeyboardAppearSound` / `…DisappearSound`.
  - 1018 carries the newly focused element itself (the text field, `pid/elementID`).
  - 1017 arrives once per typed character, on the field element (text value changed: inferred).
- **Still unnamed:** 1007 (56–64 times during a cold Safari launch, never in our apps), 1020, 1025 (value/frame motion, inferred).
- **UIKit exports no other `UIAccessibility*Notification` constants** (`nm` over the iOS 26.2 simulator's UIKitCore); the private numbers aren't exported by the AX frameworks either.

### 15.2 Duplicates and the dedupe window
- From the 1688 notifications of the §12 runs, paired as identical (number, element, payload):
  - **every true duplicate arrived within 3 ms** (619 pairs, max 2.96 ms);
  - between 3 and 20 ms only genuine repeats appear (1025 and 1001 bursts).
- **Not "most arrive twice":** 4002, 1021 and 1006 come in pairs; 1000, 1018, 1009 and 1020 come once; 1001 and 1025 mostly once.
- **Each `_registerForAXNotification:` call adds a delivery.** After four registrations in one runner process, every event arrived five times. The runner must register once per process.
- **Decision:** dedupe identical (number, element, payload) within **5 ms**.

### 15.3 Focus and keyboard signals (the test app's field; 3 rounds)
| | setAttribute value | tap on the field |
|---|---|---|
| 1031 `visible = 1` (keyboard starts to appear) | 51–107 ms | 166–172 ms |
| 1018 with the field as element | 107–123 ms | 191–195 ms |
| 1031 `visible = 1` again (keyboard in place) | 561–609 ms | 688–696 ms |

- **`fetch` `ResponderElement` + `ActiveKeyboard` on the application element:**
  - 7–14 ms when a field is focused (it returned the field itself);
  - **~106 ms when nothing is focused** (an empty reply).
- **Decision:** take focus from the 1018 that the settle already collected. Fetch `ResponderElement` only when no 1018 arrived. `ActiveKeyboard` (or a node of type 19 in the tree) says whether the keyboard is up.

### 15.4 Settle: two misses in the §13.3 rule, and the fixes
1. **setAttribute on a field (keyboard comes up), 2/3 runs early.**
   - Both idle replies came back at ~28 ms, before the keyboard animation began (1031 at ~51 ms). So the rule settled at 224–231 ms while the keyboard moved until ~565 ms.
   - The 3rd run had requested the idles later, and its animations-idle reply covered the keyboard (610 ms).
   - **Fix:** if 1018 or 1031 `visible = 1` arrives after the idle replies, request both idles again.
     - Re-arming on *any* late change notification also fixed it, but cost Settings › General ~500 ms extra: a late screen-changed (1000) re-armed, and the second animations-idle took 500 ms with no visible change.
     - With re-arming on 1018/1031 only, 3/3 setAttribute runs re-armed and settled at 704–713 ms; every other scenario kept its old timing (no re-arm).
2. **The AX page scroll (2006/2007 on a `UITableView`) animates, but nothing reports it.**
   - The app's own "scroll end" came **162–183 ms after the action (16/16)**.
   - Both idle replies came back in 4–11 ms, even when requested 30, 60 or 120 ms into the animation, and no notification arrived. So the rule settled at ~115 ms (3/3 early against polled truth).
   - **Fix (proposal, not yet run on the runner):** after an AX scroll action (2003, 2006, 2007, 2014, 2015), the quiet window starts no earlier than 200 ms after the action.

**Re-validation of the 14 scenarios (×3) with the runner's `settle` command**, scored like §12:
- re-arm on 1018/1031: **39/42**;
- the 3 misses are the AX page scroll above;
- median wait 562 ms, max 947 ms.

### 15.5 Cold launches and long transitions (2 rounds; settle timeout question)
| app (cold) | both idles | §13.3 rule settled | last notification in 9 s | content truth |
|---|---|---|---|---|
| Settings | 267–283 ms | 440–459 ms | 1.08–1.10 s (1021, 1001, 1025) | — |
| test app | 321–517 ms | 423–821 ms | 1.18–1.20 s | **web view content at 1.73–1.75 s** (3/3) |
| Safari (restores a page) | 374–659 ms | 1128–1289 ms | 3.4–4.1 s (1025) | page layout changes until 1.62–1.69 s |
| the real app | 326–336 ms | 489–501 ms | 4.25 s (1021) | — |

- **The rule never got near 3000 ms.** Keep `timeout_ms` = 3000 as the default.
- **Content that loads later (web views, network) is not UI motion:** no notification marks it, so the settle can't see it, as on Android. `wait_for` on content is the answer (§15.7).
- **A pure quiet window would fail here:** SpringBoard and Safari keep posting 1021/1025 for seconds.

### 15.6 The snapshot "hang" after activation
- **The 30 s hang did not reproduce: 0 in 33 snapshots taken straight after `dlaunch`** (test app, Settings, the real app).
- **Instead, a snapshot sent straight after `dlaunch` failed fast** with `Error getting main window kAXErrorServerNotFound` in 11/12 runs (2–35 ms). One or two immediate retries succeeded.
- **With a settle first (the product path, since `launch` is an action):** 9/9 succeeded first time. The settle took 111–384 ms and the whole sequence 0.26–0.68 s.
- **Decision:**
  - settle before every snapshot that follows an activation;
  - treat `kAXErrorServerNotFound` as retryable (short backoff, up to ~1 s);
  - keep the 5 s snapshot timeout plus one retry for the hang seen once in §12.
- **Spike bug:** the standalone `idle` command never received its reply (5 s timeouts), while the same calls inside `watch`/`settle` work. Cause unknown; the product runner only requests idles inside the settle loop.

### 15.7 Web content
- After a cold launch the WKWebView's content (the "Web Button") first appeared in a snapshot at **1.73–1.75 s**.
- The last notifications in the app came at ~0.94 s (3031 accessibility ready), then SpringBoard's 4002 at ~0.99 s. **Nothing arrives when the web content appears**, from the app or from a WebContent pid.
- **Decision:** no settle hook for web content. Agents use `wait_for` (text, ref or role) with a timeout, re-checked after each quiet window. AGENTS.md says so for iOS web views.

### 15.8 Keyboard dismissal (test app, default keyboard)
| candidate | keyboard gone? |
|---|---|
| **AX Press (2010) on the keyboard's return key** (`type 9`, identifier **`Return`**, label "return") | **yes**, settled 730 ms; DTA `field return` |
| `sendString "\n"` | yes, 714 ms |
| tap outside the field (the status label) | no |
| swipe down on the table (default `keyboardDismissMode`) | no |
| swipe down on the keyboard | no |

- **The keyboard's keys are in the app's tree:** a `Keyboard` (type 19) with `Key`s (type 20) and buttons. Their identifiers (`Return`, `shift`, `delete`, `more`, `space`) don't depend on the label language.
- **Decision for `hide-keyboard`** (no `back` on iOS):
  1. AX Press on the key with identifier `Return`, or on a key or accessory button labelled Done/Go/Search/Send/Next (localized labels need a list);
  2. else, if the field has a return action, `sendString "\n"`;
  3. else (a number pad with no accessory), fail with a typed error and keep relying on ScrollToVisible for covered targets.
- **Not measured:** the number pad itself. Contacts would have needed its first-run screen dismissed, and the real app's login field needs a login screen. The I2 test app gets a number-pad field with and without a Done accessory.

### 15.9 Not done (needs the user)
- **Full Keyboard Access:** it needs a Settings toggle on the phone, so it waits for the user's OK, and expectations are low (§11 of the handoff).
