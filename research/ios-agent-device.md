# agent-device (iOS path) and droidrun ios-portal: analysis for droidctl

Question: can agent-device's XCUITest runner be the starting point for droidctl's iOS device side, and what do agent-device and droidrun's ios-portal teach us that `research/ios.md` §5.3/§5.7 did not?

Sources (read 2026-10-16), shallow clones under the session scratchpad (`…/scratchpad/ios-src/<repo>`). Paths are relative to each repo root; line numbers refer to these checkouts. I read the clones as data and ran none of their build scripts.

| repo | commit | license (verified in `LICENSE`) |
|---|---|---|
| callstackincubator/agent-device | `610de02` (2026-10-16) | MIT, "Copyright (c) 2026 Callstack" (`LICENSE:1-3`) |
| droidrun/ios-portal | `c6f1cb7` (2026-08-19, archived) | MIT, "Copyright (c) 2025 Niels Schmidt" (`LICENSE:1-3`). Its one dependency, FlyingFox 0.22.0, is pinned in `Package.resolved` (its license I did not check). |

Nothing here was measured on an iOS device by me. Every number is cited from agent-device's own evidence files, with the device and build they name. Anything I did not see in code is marked **(inferred)**.

---

## TL;DR

- **agent-device's iOS runner is mature, defensive and large.** The XCUITest runner is ~17,000 lines of Swift/Objective-C (production files in `apple/runner/AgentDeviceRunner/AgentDeviceRunnerUITests/`, unit tests excluded, which add ~14,000 more), and the host side for Apple is ~40,600 lines of TypeScript (`packages/platform-apple/src`, tests excluded). It supports iOS, iPadOS, tvOS and macOS from one target.
- **Everything is a coordinate.** On iOS a ref resolves on the host to a rect, and the runner gets `{"command":"tap","x":…,"y":…}` (`RUNNER_PROTOCOL.md:74`). Even its "selector tap" queries the element and then taps its frame centre (`RunnerTests+CommandExecution.swift:25-110`). The guarantee registry says so outright: "apple/android backends never define tapTarget/fillTarget/hoverTarget" (`packages/contracts/src/interaction-guarantees.ts:400-408`).
- **The biggest new fact for us:** agent-device tried private AX node actions and they failed. `XCAXClient_iOS performAction:onElement:value:` "answers kAXErrorCannotComplete for every AX action number", `setAttribute:` "reports success and does nothing", and the raw `AXUIElement` C API cannot be aimed at app elements (`RunnerAXSnapshotBridge.h:73-80`). That closes, negatively, the open question in `ios.md` blocker 1, at least for the environment they tested (the bridge is simulator-gated, so probably the simulator: **inferred**).
- **Physical devices get exactly two snapshot tiers:** the public recursive `XCUIElement.snapshot()` and an `XCUIElementQuery` sweep. The private-AX tier and the idb-derived host bridge are simulator-only (`RunnerTests+SnapshotBackendCapabilities.swift:80-86`; `website/docs/docs/snapshots.md:246-271`).
- **Measured on an iPhone 17 Pro (agent-device's runs):** a steady-state snapshot is 350–450 ms over usbmux. The cold start was 38.2 s *with a cold CoreDevice tunnel* (the worst case), and re-adopting a live runner after a daemon restart took 1.15 s (`docs/usbmux-runner-transport-experiment.md:17-27`; `docs/evidence/ios-physical-runner-handoff-2026-09-19.md:97-100`).
- **Its ref model is the opposite of ours.** Refs are positional (`@e12`), and every mutation expires all of them (ADR 0014). There is no fingerprint re-resolution. `back` falls back to a blind top-leading coordinate tap verified by screenshot (`RunnerTests+Navigation.swift:159-200`), which our "never guess by position" rule forbids.
- **Verdict:** **don't fork the runner wholesale.** Its protocol, ref model, flat node schema and multi-OS conditionals would all have to change, and most of its 17k lines defend agent-device's own host contract. **Do port about 1,200 lines of specific, well-tested pieces** with MIT attribution: listener token + peer classification, HTTP-free framing ideas, the bounded main-thread watchdog, the `record(_:)` kAXError mute, the bounded `snapshot()` + query-sweep fallback, the XCTest quiescence-skip / `waitForIdleTimeout` wrapper, and the `XCSynthesizedEventRecord` gesture synthesis. Write our own ~2–3k-line runner around them that speaks `PROTOCOL.md` and our node schema. See §5.
- **droidrun ios-portal:** a 1,000-line, unauthenticated FlyingFox HTTP server that returns `debugDescription` text and taps rect centres. It is archived and "superseded by mobilerun-ios" with no other reason given (`README.md:12-20`). Two ideas are worth keeping (§4); its code is not.

---

## 1. agent-device overview

### 1.1 Architecture
- **Language and shape:** a TypeScript CLI with a resident daemon (`src/daemon.ts`, `packages/daemon/`), an MCP server generated from the command metadata (`src/mcp/command-tools.ts:1-80`), a Node client (`src/agent-device-client.ts`), and per-platform packages (`packages/platform-android`, `platform-apple`, `platform-harmonyos`, `platform-linux`, `platform-web`, plus cloud providers). It is the same "agent CLI + daemon + MCP" shape as droidctl.
- **Daemon RPC is versioned** (ADR 0006), and commands come from one descriptor registry (ADRs 0003, 0008; `packages/command-registry/src/catalog.ts:9-42`), the equivalent of our parser-generated docs.
- **Android side:** an instrumentation APK (`android/snapshot-helper/`), already covered in `research/repos-b.md` §4.1.
- **Apple side:**

  | component | path | role |
  |---|---|---|
  | XCUITest runner | `apple/runner/AgentDeviceRunner/` | long-lived test that serves `POST /command` (iOS, tvOS, macOS) |
  | shared Swift presentation | `apple/snapshot-presentation/` (1,556 source lines) | visibility fold, coordinate space, invariants; mirrored by a TS twin |
  | simulator host AX bridge | `apple/snapshot-bridge/` (1,088 lines, ObjC, idb-derived, `LICENSE.idb`) | reads a simulator's tree without XCTest |
  | macOS helper | `apple/macos-helper/` | native macOS app backend |
  | host package | `packages/platform-apple/src/runner/*` (~50 files) | build, cache, sign, launch, lease, adopt, transport, recovery |

### 1.2 The runner as a process
- **One XCTest method that never returns.** `testCommand()` reads its port and token from the environment, opens an `NWListener`, and parks on `XCTWaiter.wait(for:timeout: 24*60*60)` (`RunnerTests.swift:290-346`). A 60 s keepalive log line marks liveness (`:323-331`). The host app is an empty shell (`AgentDeviceRunner/AgentDeviceRunnerApp.m`).
- **Listener binding:** loopback on simulator/macOS; on a device, every interface (`RunnerTests.swift:348-358`). Each connection is then classified once it is ready (`RunnerListenerPeer.swift:22-31`): a loopback peer (usbmux) is served, a peer whose *available* interfaces are all `.other` (the CoreDevice `utun`) is served as a tunnel, and anything else (Wi-Fi, cellular, hotspot) is closed before a byte is read (`RunnerTests+Transport.swift:32-45`).
  - A weakness of this check (**inferred**): a VPN on the phone is also a `utun`/`.other` interface, so a peer arriving over a phone VPN would pass as `tunnel`. The token still guards it.
- **Token auth:** the host mints 24 random bytes as base64url (`packages/platform-apple/src/runner/runner-contract.ts:430-438`) and writes it, with the port, into each test target's `EnvironmentVariables` of a per-session copy of the `.xctestrun` (`runner-artifact-env.ts:89-115`). The app under test does not inherit it (`RUNNER_PROTOCOL.md:27-30`). The runner refuses to listen without both (`RunnerTests+Environment.swift:13-24`) and requires at least 32 characters (`RunnerListenerToken.swift:6-13`). It compares in constant time (`:15-24`) before routing (`RUNNER_PROTOCOL.md:32-35`). Every reply carries `X-Agent-Device-Runner-Auth: required`, and the host refuses a runner that doesn't send it (`runner-contract.ts:451-461`).
- **Framing:** HTTP/1.1, one request per connection, `Connection: close` (`RUNNER_PROTOCOL.md:7-11`). A hand-written request-head parser enforces the head size, token, method, path and length limits in a fixed order (table at `RUNNER_PROTOCOL.md:42-54`; `RunnerHttpRequestFraming.swift`). A request must arrive within 30 s (`:56-59`).
- **Envelope:** `{ok, data?, error{code,message,hint}}` (`RUNNER_PROTOCOL.md:133-154`). It is not JSON-RPC.
- **Commands:** 36 (`RunnerTests+Models.swift:5-42`): `tap, mouseClick, longPress, drag, remotePress, type, swipe, scroll, desktopScroll, findText, querySelector, readText, snapshot, screenshot, backInApp, backSystem, home, rotate, appSwitcher, actionButton, keyboardDismiss, keyboardReturn, alert, sequence, gesture, gestureViewport, recordStart, recordStop, status, uptime, appState, pasteboardWrite, activate, terminate, targetReset, shutdown`. Each command declares "traits" (interaction? replayable after session loss? may it launch the app? does an XCTest-recorded failure turn success into failure?), exhaustively, in one switch (`:94-305`).
- **Reliability machinery** (this is where most of the 17k lines go):
  - a **command journal** of the last 64 commands by `commandId` (`RunnerTests+CommandJournal.swift:24`), so a host that lost a response asks `status(commandId)` instead of resending: "No command is replayed after an observed mutating `accepted`, `started`, `completed`, or `failed` state" (`docs/ios-runner-protocol-optimizations.md:9-27,104-116`);
  - duplicate sends of an in-flight `commandId` coalesce onto the one execution (`RunnerTests.swift:97-101`);
  - a **main-thread watchdog**: XCTest work runs on main under a 30 s cap (`:68`). Abandoned work keeps the runner "busy" (`RUNNER_BUSY`); after 120 s it reports `RUNNER_WEDGED` so the host recycles it (`:102-114`);
  - an **`XCTestCase.record(_:)` override** that mutes `kAXError…` "Failed to get matching snapshot / Failed to resolve query" issues. Without it, "XCTest ends the test case as soon as the main-thread block that recorded them returns, killing the long-lived runner" (`RunnerTests.swift:241-288`). Timeouts and "not running" issues still record.

### 1.3 How the host builds, signs, installs and launches it
1. **Build once per cache key:** `xcodebuild build-for-testing -project … -scheme AgentDeviceRunner -destination … -derivedDataPath <cache>`, plus `-allowProvisioningUpdates` for a device (`runner-artifact.ts:863-912`). The cache key covers the build recipe, and old keys are trimmed (`runner-cache-metadata.ts`; `website/docs/docs/configuration.md:130`).
2. **Signing:** `CODE_SIGN_STYLE=Automatic`, or `Manual` when `AGENT_DEVICE_IOS_PROVISIONING_PROFILE` is set, with `DEVELOPMENT_TEAM=$AGENT_DEVICE_IOS_TEAM_ID` and an optional `CODE_SIGN_IDENTITY` (`runner-cache-metadata.ts:258-285`). The default bundle id is `com.callstack.agentdevice.runner` with tests at `<id>.uitests` (`:21`; `AgentDeviceRunner.xcodeproj/project.pbxproj:239-240`).
   - Personal Teams fail with "bundle identifier is not available" on that generic id, so users must set `AGENT_DEVICE_IOS_BUNDLE_ID` to something unique (`website/docs/docs/installation.md:107-114`).
   - Startup failures are classified into typed reasons: `signing_no_development_team`, `bundle_identifier_already_registered`, `device_developer_mode_disabled`, `device_developer_disk_image_unavailable`, … (`installation.md:115-116`). The two `device_*` reasons come from `xcrun devicectl device info details`, checked before building.
3. **Launch:** `xcodebuild test-without-building -only-testing AgentDeviceRunnerUITests/RunnerTests/testCommand -test-timeouts-enabled NO -collect-test-diagnostics never -xctestrun <session copy>` (`runner-process-launch.ts:62-86`). Install is implicit in `xcodebuild`. stdout/stderr go to a log *file*, not pipes, so a detached runner never gets SIGPIPE when the daemon that started it dies (`:40-48`). Readiness is a log marker (`AGENT_DEVICE_RUNNER_LISTENER_READY`, `RunnerTests.swift:305-311`).
4. **Transport:** physical devices use **usbmux first**. The host speaks the usbmuxd plist protocol itself on `/var/run/usbmuxd` (`ListDevices`, `Connect`; `runner-usbmux-protocol.ts:49-88`; `runner-usbmux.ts:16`), with no `iproxy`. It falls back to the CoreDevice tunnel only when usbmuxd says the device is not attached (`docs/usbmux-runner-transport-experiment.md:62-79`).

### 1.4 Re-adoption of a live runner
- On a graceful daemon stop, a request-proven runner is **detached**: its lease token is rewritten to `detached-owner-…`, the daemon closes its side of the log, and the next daemon **adopts** it after one `uptime` probe (500 ms cap; 5,000 ms on a physical device because the probe may first resolve the tunnel address) (`runner-adoption.ts:47-80`).
- There is a kill switch, `AGENT_DEVICE_IOS_RUNNER_DETACH=0`, and orphans self-expire through the 24 h `XCTWaiter` (`:59-75`).
- **Measured** (iPhone 17 Pro, iOS 27.0, Xcode 26.2): re-`prepare` after a graceful stop returned in **1,151 ms** (health check 6 ms) against a cold start of **38,247 ms**, of which the health check took 36,856 ms "the tunnel had gone cold, so this is the worst case for the comparison"; the same PID survived both daemons (`docs/evidence/ios-physical-runner-handoff-2026-09-19.md:93-100`).

### 1.5 Startup and steady-state numbers, with their sources
All from agent-device's runs on an iPhone 17 Pro, iOS 26.5.2 / 27.0, Xcode 26.2, not reproduced by me:

| what | number | source |
|---|---|---|
| steady-state snapshot, usbmux | 440 ms (350–450 over the tunnel) | `usbmux-runner-transport-experiment.md:19` |
| snapshot after 40 s idle | 440 ms usbmux vs 4.5–4.6 s tunnel (re-probe) | `:20,31-36` |
| runner killed → next snapshot self-heals | 4.0–4.7 s | `:21` |
| cold xctestrun prep (build) | ~13 s | `:25-27` |
| fresh device resolution (`devicectl` listing) | ~6.7 s | `:26-27` |
| cold start, cold tunnel (worst case) | 38.2 s | handoff evidence `:97-99` |
| adoption | 1.15 s (122 ms adopt phase in an earlier run) | `:93-99` |

**Inferred:** a typical warm-tunnel, cached-build cold start is the ~13 s prep (skipped when cached) plus the `devicectl` listing plus `xcodebuild` launch to listener ready. The evidence files don't isolate the last of these.

---

## 2. agent-device's iOS path in depth

### 2.1 Hierarchy fetch
- **Capture plan.** A regular snapshot runs the tiers `[recursiveTree, querySweep, privateAX]` (`RunnerTests+SnapshotCapturePlan.swift:129`) under a **20 s plan budget**, kept below the 30 s watchdog (`:115-119`). The first usable result wins, and every payload carries a verdict `snapshotQuality {state: healthy|recovered|sparse, backend, reason, reasonCode, effectiveDepth, …}` (`:11-42`).
  - On a device, `privateAX` is unavailable (`RunnerTests+SnapshotBackendCapabilities.swift:80-86`), so a device has the **tree** and the **query sweep** only.
  - Before any tier, a bounded (4 s) SpringBoard probe checks for a **blocking system modal**; if one is up, the modal *is* the snapshot (`RunnerTests+Snapshot.swift:164-179,306-390`; `RunnerTests.swift:144-146`).
- **Tier 1, tree:** public `element.snapshot()` on the app (`RunnerTests+SnapshotAcquisition.swift:115-134`), inside an Objective-C exception catcher, on the main thread under an **8 s slice** (`RunnerTests.swift:140-143`; `SnapshotAcquisition.swift:85-97`).
  - **No depth limit at acquisition.** The public API has none, and agent-device sets no private `snapshotMaxDepth`. A regular capture walks the whole materialized tree and cuts depth in presentation (`RunnerTests+Snapshot.swift:49-57`). A runaway guard stops at 50,000 nodes (regular) or 5,000 (raw) (`:12-20`).
  - The walk is iterative and **dedupes** nodes whose (type, label, identifier, frame) repeat, re-parenting the duplicate's children onto its parent (`:221-262`).
  - Attributes read per node: label (with an aggregated label for merged containers), identifier, value, placeholder, `hasFocus`, selected, enabled, frame, element type (`SnapshotAcquisition.swift:184-223`). That is all.
- **Tier 2, query sweep:** a flat `descendants(matching:)` enumeration of interactive types within a 1 s slice, where no query may start with <100 ms left "because XCTest cannot cancel a query" (`RunnerTests+Snapshot.swift:136-150,444-515`). Results are sorted top-to-bottom, left-to-right, under a synthetic root.
- **Tier 3, private AX (simulator only):** `XCAXClient_iOS requestSnapshotForElement:attributes:parameters:error:` with `maxDepth`/`maxChildren`, attributes mapped through `XCElementSnapshot axAttributesForElementSnapshotKeyPaths:isMacOS:` and trimmed to the 9 it uses ("the mapper expands keypaths with extra attributes … disproportionately expensive … on large React Native trees") (`RunnerAXSnapshotBridge.m:325-400`).
  - It retries down a **depth ladder** `[56, 40, 24, 12]` because deep RN trees get `kAXErrorIllegalArgument` "between depth 56 and 64 on the Bluesky Home feed" (`RunnerTests+AXSnapshotFallback.swift:34-41`). The accepted depth is remembered per bundle and pid for 120 s (`RunnerTests.swift:124-136`).
  - Frontier extension re-roots capped subtrees, up to 8 calls (`AXSnapshotFallback.swift:7-11`).
- **Error recovery across captures:** after an XCTest tier grinds past 3 s or is abandoned, the bundle's "XCTest channel" is **penalized for 120 s**. Later plans then use only non-XCTest tiers (simulator) or a 1 s bounded XCTest probe (device) (`RunnerTests.swift:115-139`; `SnapshotCapturePlan.swift:120,247-296`). A warm-up exemption spares a freshly launched process.
- **Speed:** 440 ms steady state on an iPhone 17 Pro, end to end through the daemon (§1.5). Each poll of `wait`/settle pays that.
- **Sparse handling:** a sparse result sets `runnerFatal` and invalidates the cached target (`RunnerTests+Snapshot.swift:517-539`). The host then takes a screenshot itself and returns `fallbackScreenshotPath` (`snapshots.md:256-262`).

### 2.2 Snapshot format and compaction
- **Wire node** (flat array with parent indices): `index, type, label, identifier, value, placeholder, rect{x,y,width,height}, enabled, focused?, selected?, hittable?, depth, parentIndex, hiddenContentAbove?, hiddenContentBelow?, actions?` (`apple/snapshot-presentation/Sources/AgentDeviceSnapshotPresentation/SnapshotModels.swift:288-334`).
  - `hittable` is **geometric** ("geometric-actionability", `SnapshotBackendCapabilities.swift:35-37`), recomputed after coordinate normalization, not `XCUIElement.isHittable`.
  - There are no traits, no checked state (switches carry it in `value`; RN packs "checkbox, unchecked" into `value`, which the host parses as `iosValueSemantics`, `snapshots.md:297-309`), no custom actions on a device (`--actions` is simulator-only, `snapshots.md:34`), and no window list.
- **Runner-side fold:** `SnapshotVisibilityFold` drops nodes outside the viewport and outside the nearest scroll container, and sets `hiddenContentAbove/Below` instead of emitting them (`SnapshotPresentation.swift:21-43`; ADR 0004 "Regular visible strategy"). This deliberately has **no node-count cutoff**, so a bottom tab bar after a long list survives.
- **Host-side text** (`snapshots.md:96-110`):
  ```
  Snapshot: 9 visible nodes (14 total)
  @e1 [application] "Contacts"
    @e4 [other] "Lists"
      @e6 [button] "Lists"
  [off-screen below] 2 interactive items: "All Contacts", "New List"
  ```
  - Off-screen interactive content becomes summaries of a few labels per direction (`snapshots.md:84-86`). `kind` is a platform-neutral role next to the raw `type` (`:124-137`).
  - Compared with droidctl: indented tree vs our flat/spatial lines. They have no row merge, regions, grids or inferred labels, and no `sig=`/`unchanged` on iOS (the unchanged acknowledgement is Android-only, `snapshots.md:81`).

### 2.3 Refs, resolution and staleness
- **Refs are positional names in a "ref frame"** (ADR 0014 `docs/adr/0014-session-ref-frame-lifetime.md:16-34`):
  - a full snapshot activates a frame;
  - **every mutating command expires it** "immediately before the device op — with no success-only rollback";
  - a mutation with a ref from an expired frame fails as `ref_frame_expired` → `ref_generation_mismatch` → `plain_ref_requires_complete_frame` → `ref_not_issued`;
  - a `@e12~s42` suffix pins a generation.
  - Motivation: "`snapshot -> press @e1 -> press @e2` can dispatch both presses … A false successful tap on the new screen is worse than a clear stale-ref failure" (`:54-58`).
- **There is no re-resolution.** A ref is never matched again by fingerprint; after any action, the agent needs a new `snapshot` or a `--settle` diff, which re-issues refs. `wait @ref` "resolves the ref to its label or text, then waits for that text, so a duplicate label can match a different element" (`commands.md:375`).
- **Before dispatch:**
  - the guarantee matrix (`interaction-guarantees.ts:30-88`): disambiguation, occlusion, keyboard occlusion, parent-owned touch point, offscreen, non-hittable promotion, plus `targetStability`, which re-observes a target whose screen just animated and refuses with `target_still_moving` (`commands.md:428`);
  - runner side, a selector tap refuses `ELEMENT_OFFSCREEN` when the frame centre is outside the main window ("XCTest reports closed-drawer/off-viewport items as hittable, then 'taps' … as a silent no-op", `RunnerTests+CommandExecution.swift:47-67`).

### 2.4 Actions
- **Tap / long-press / double-tap / drag:** coordinates in app space, anchored at `app.coordinate(withNormalizedOffset: (0,0)).withOffset(x,y)`, not the window, because a SpringBoard alert button can sit in a different window from the first one that qualifies (`RunnerTests+Interaction.swift:583-600`).
  - Optionally there is a **"synthesized" fast path**: private `XCSynthesizedEventRecord` + `XCPointerEventPath` (`initForTouchAtPoint:offset:`, `moveToPoint:atOffset:`, `liftUpAtOffset:`, `synthesizeWithError:`) resolved by reflection (`RunnerXCTestEventBridge.h:5-27`; `RunnerSynthesizedGesture.m:285-309`).
  - Policy decides per gesture kind whether the XCUICoordinate fallback is allowed when AX is unhealthy (`RunnerTests+SynthesizedGesturePolicy.swift:9-80`).
- **Bounded XCTest waits around an event:** `setWaitForIdleTimeout:` to 1 s, plus private `_performWithInteractionOptions:block:` with skip-pre/post-quiescence bits (1, 2), "Some apps never report post-gesture quiescence" (`RunnerTests+Lifecycle.swift:419-483`). Alert verification uses `_waitForQuiescenceAsPreEvent:` (`RunnerTests+Alert.swift:146-161`).
- **Typing:**
  - focus by a coordinate tap at the field (`RunnerTests+TypeExecution.swift:21-62`), then `typeTextReliably` with private text synthesis (`XCPointerEventPath initForTextInput`, `typeText:atOffset:typingSpeed:shouldRedact:`, select-all via `typeKey:"a" modifiers:command`) (`RunnerXCTestEventBridge.h:13-17`; `RunnerSynthesizedTextEntry.m:95-113`);
  - read-back: `TEXT_ENTRY_MISMATCH` when the settled value doesn't match, or `verification:"unconfirmed"` with `requested/before/after` when the app formatted it (`TypeExecution.swift:98-150`; `commands.md:441-454`);
  - typed failures: `TEXT_INPUT_NOT_FOCUSED`, `…_COMMIT_NOT_OBSERVED`, `…_CLEAR_NOT_OBSERVED`, `…_SYNTHESIS_BUDGET_EXCEEDED` (`commands.md:439-444`);
  - **no keyboard-preference changes.** A grep for autocorrect/prediction/`TIPreferences` finds nothing, unlike WDA (`ios.md` §5.1).
- **Scroll:** a host-planned swipe kept above the keyboard (`keyboardAvoided`, refuses `scroll_keyboard_occludes_surface`). `--until <selector>` is their scroll-to, and the result reports `movement: moved|at-edge|unchanged|unobserved`, refusing `scroll_no_progress` (`commands.md:467-473`). There is no node scroll action on iOS.
- **Back:**
  - `backInApp` looks for a top-band control whose label or id contains "back"/"close"/"cancel" or whose id is `BackButton` (`RunnerTests+Navigation.swift:5-8,118-157`);
  - failing that, it **taps a computed top-leading point** and compares screenshots before and after (`:159-200`);
  - `backSystem` is an edge-swipe gesture (`RunnerTests+Interaction.swift:69-95`).
- **Home:** `XCUIDevice.shared.press(.home)` (`Interaction.swift:115-125`). App switcher is a bottom-edge drag (`:97-112`).
- **Alerts:** first the SpringBoard `alerts`/`sheets` (blocking system modals), then `app.alerts`, then "dismiss popup" windows (`RunnerTests+Alert.swift:24-55`; `RunnerTests+SystemModal.swift:85-114`). `accept`/`dismiss` are sent once and never replayed (`commands.md:389`).
  - The docs say alert support is for the "iOS simulator", macOS and Android (`commands.md:387`), but the SpringBoard probe is compiled for every iOS target, devices included (`RunnerTests.swift:41-50`). Device behaviour is unverified by them, as far as the docs say.
- **Batching:** a runner `sequence` command fuses allowlisted coordinate steps (`RunnerTests+Models.swift:344-367`), and the daemon has a `batch` command (`website/docs/docs/batching.md`).

### 2.5 Settle and change verification
- **The runner has no event stream and no settle command.** The host polls interactive-only captures every ≤300 ms until the tree has been unchanged for **500 ms** (1.5 s for broad transitions), with a **10 s** cap (`src/commands/interaction/runtime/stable-capture.ts:37-84`). It returns a diff capped at **80 lines** plus a 20-entry unchanged tail (`settle.ts:68-73`). A settled tree of ≤5 nodes warns of a splash screen (`stable-capture.ts:84`).
- **Inferred cost on a device:** each poll is a ~440 ms capture, so "unchanged for 500 ms" needs at least two or three captures after the action. That is ~1–1.5 s per settled action, versus our measured tap + settle of ~0.3–0.5 s on Android.
- **Opt-in by default:** "A successful `click` … does not confirm that the element received it or that the screen changed. … add `--verify` … add `--settle`" (`commands.md:426`). droidctl settles and diffs on every action by default.
- **Runner-side fixed delays:** 200 ms after a snapshot before an interaction, and 250 ms "minimum gap since the activation" before the first interaction (counted from the activation, so an agent's own 190–260 ms round trip usually covers it) (`RunnerTests.swift:70-73,90-94`).

### 2.6 Foreground-app detection
- **There is none in general.** A session is *bound* to the app the agent `open`ed (`activateTarget`, `RunnerTests+Lifecycle.swift:365-412`). Reads of a not-running session app refuse with `APP_NOT_RUNNING` rather than relaunching it (`:341-363`). `appState` reads a *named* bundle's `XCUIApplication.state` (`RUNNER_PROTOCOL.md:118-125`).
- The one private signal is the AX client's `activeApplications` pids, and the runner states explicitly that it is "NOT a foreground owner … the private AX client exposes no ordering … resolves no bundle id for an arbitrary app" (`RunnerTests+Models.swift:417-427`; `RunnerTests+Lifecycle.swift:324-339`).
- **System surfaces** that must not be activated (`com.apple.SafariViewService` for web auth, `PassbookUIService` for payment) are observed in place through a fixed registry (`RunnerSystemSurfaceHostPolicy.swift:17-22`).
- **Implication for droidctl:** our `current` (any foreground app) has no public iOS answer, which confirms `ios.md`'s mapping that this needs private API or a bound target.

### 2.7 Real device vs simulator

| aspect | simulator | physical device |
|---|---|---|
| snapshot source | host AX bridge (idb-derived, no runner) first, XCTest fallback per app generation (ADR 0004 "Decision") | runner XCTest tree + query sweep only |
| private AX tier, `--actions` | yes | no (`SnapshotBackendCapabilities.swift:80-86`) |
| listener | 127.0.0.1 only | all interfaces + peer classification |
| transport | loopback | usbmux; CoreDevice tunnel only when the device is not attached |
| signing | none | team/profile env vars, `-allowProvisioningUpdates` |
| locked device | n/a | "wedges AX capture/runner relaunch identically on both routes" (`usbmux-runner-transport-experiment.md:54-56`) |
| SpringBoard as a target | verified | "physical iPhones are unverified" (`commands.md:1095`) |

### 2.8 Teardown and side effects on the device
- **Graceful shutdown:** a `shutdown` command, then escalation to killing the `xcodebuild` process tree; temp `.xctestrun` copies are deleted (`runner-disposal.ts:154-237`). Simulator runner apps are `simctl terminate`d (`:259,288-301`).
- **On a physical device the runner app stays installed**: no device uninstall appears in the runner disposal path (**inferred** from its absence; `devicectl device uninstall app` exists only in the user-facing app deployment code, `packages/platform-apple/src/deployment/runtime.ts:179`). The same holds for droidctl's Backend A until `teardown`, so a droidctl `teardown --ios` must uninstall it explicitly.
- **No keyboard or settings changes** (see §2.4). `pasteboardWrite` writes the device pasteboard on request.
- **Grants pasteboard access silently:** apps launched through the runner never show iOS's "Allow Paste" prompt (`website/docs/docs/known-limitations.md:9-11`). That is a test-context artefact to document, not a setting we change.
- **The "unlock to continue" and Developer Mode prerequisites** are surfaced as typed reasons, not worked around (`installation.md:115-116`).

---

## 3. Feature matrix against droidctl

Legend: **Adopt** = port the code or technique with attribution; **Adapt** = take the idea and change it; **Ideas** = note only; **Avoid** = conflicts with our rules. "A-D Android" draws on `research/repos-b.md` §4.1 plus the helper's serializer attributes (`android/snapshot-helper/…/AccessibilityTreeXml.java:30-120`).

### 3.1 Device API (`PROTOCOL.md`)

| droidctl method | A-D Android | A-D iOS runner | droidctl (Android) | verdict for our iOS runner |
|---|---|---|---|---|
| `ping` | helper session | `uptime` / `status` (`RunnerTests+CommandDispatch.swift:26-53`) | version, sdk, screen, gen | **Adapt**: one `ping` answering version + screen + the bound app's state |
| `tree` | XML via instrumentation | `snapshot {interactiveOnly, depth, scope, raw}` | full raw tree, all windows | **Adopt** the tiered capture (tree → query sweep), bounded slices and quality verdict; return *raw* nodes and prune on the host |
| `act` (node action) | none (`input tap`) | none; private `performAction` fails (`RunnerAXSnapshotBridge.h:73-80`) | ACTION_CLICK etc. on the node | **Adapt**: `act` = resolve the element by our locator on the device, then a synthesized touch at its hit point; report `method:"synthesized-touch"` honestly |
| `act … settle` (tree in the same reply) | no | no | yes | **Ideas/ours**: do it runner-side: event, bounded quiescence, then one `snapshot()` in the reply. That saves a full HTTP + capture round trip per action |
| `gen` | no | no | content generation | **Adapt**: a cheap tree digest or a counter bumped per snapshot; no events exist to drive it |
| `gesture` | `input` swipe | `gesture` plan, `drag`, `swipe`, `sequence` | dispatchGesture | **Adopt** `XCSynthesizedEventRecord` synthesis (`RunnerSynthesizedGesture.m`) with the XCUICoordinate fallback |
| `global` | keyevents | `home`, `backSystem`, `appSwitcher`, `actionButton` | back, home, recents, … | **Adapt**: `home` via XCUIDevice; `back` only as an explicit control match (**Avoid** their blind top-leading tap) |
| `wait_idle` | waitForIdle(100/500) | none (host polls) | event-driven quiet window | **Adapt**: XCTest quiescence with a bounded `waitForIdleTimeout`, then a digest-stability check (§6) |
| `wait_for` | host polls | host polls (`commands.md:361-386`) | device-side, event-driven | **Adapt**: a runner-side poll loop (no XPC round trip to the host per poll) |
| `subscribe`/`events` | no | no | push events | **n/a**: no iOS source; make it an explicit capability `events:false` |
| `screenshot` | screencap | `screenshot` with display metadata (`RunnerTests+Models.swift:407-415`) | takeScreenshot / screencap | **Adopt**: `XCUIScreen` of the app's window, plus pixel/point scale in the reply |
| `current` | dumpsys | none; session-bound app + `appState` | foreground pkg/activity | **Adapt**: report the bound app and its state; never claim the foreground owner |
| `clipboard` | broadcast | `pasteboardWrite` | set/get | **Adopt** (write only) |
| `shutdown` | quit | `shutdown` | yes | **Adopt** |
| `status(commandId)` journal | – | yes | none (we return `connection` + `maybe_performed`) | **Adopt**: lost responses are likelier over usbmux/tunnels than over adb forward |

### 3.2 Node fields

| droidctl field | A-D Android | A-D iOS | iOS source we would use | verdict |
|---|---|---|---|---|
| `handle` | – | `index` (per capture) | per-snapshot index | **Adapt**: an index into the latest snapshot, invalid after the next one (as our dump ids) |
| `window {id,type,layer,title}` | yes (`window-*` attrs) | no | none public; SpringBoard modal is a separate query | **Adapt**: synthesize `window` = app / springboard-modal / keyboard |
| `class` | `class` | `type` (ElementType name) | `elementType` | **Adopt** their raw-value table incl. hidden 19/20 Keyboard/Key (`RunnerTests+Snapshot.swift:76-109`) |
| `pkg` | `package` | no | bound bundle id | **Adapt** |
| `id` | `resource-id` | `identifier` | `identifier` | **Adopt** |
| `uid` | – | – | none | **n/a** (resolver tier 1 never applies on iOS) |
| `text` / `desc` | text / content-desc | `label`, `value` | `label`, `value` | **Adapt**: map `label`→`desc`-like, `value`→`text`; document the semantics |
| `hint` | `hint` | `placeholder` | `placeholderValue` | **Adopt** |
| `error`, `tooltip`, `pane` | no | no | none | **n/a** |
| `state` | no | in `value` (RN), `iosValueSemantics` on host | `value` of switches etc. | **Ideas**: their RN value heuristic |
| `bounds` | px | points, normalized to the app's orientation (`SnapshotCoordinateSpace.swift`) | `frame` (points) | **Adopt** the orientation normalization; print points and record the scale |
| `visible` | `visible-to-user` | via fold + `hiddenContentAbove/Below` | frame ∩ viewport ∩ scroll clip | **Adopt** the fold rules (port `SnapshotVisibilityFold.swift`, 249 lines) |
| `drawingOrder` | yes | no | none | **n/a**: occlusion becomes "covered by a later sibling's frame" (inferred, weaker) |
| flags: clickable/longClickable/checkable | clickable | none; `hittable` geometric | none public | **Adapt**: derive "actionable" from type (button, cell, link, switch…, `RunnerTests.swift:225-233`) |
| checked / selected / focused / enabled | yes | selected, focused, enabled | `isSelected`, `hasFocus`, `isEnabled` | **Adopt** |
| editable / password | yes | via type | TextField/SecureTextField/TextView/SearchField | **Adopt** |
| scrollable | can-scroll-* | type ∈ ScrollView/Table/CollectionView | type | **Adapt**: no direction hints; use `hiddenContentAbove/Below` |
| heading | yes | no | traits only via private AX | **Ideas** |
| `actions` (custom) | no | `actions` (simulator, bounded read, `RUNNER_PROTOCOL.md:88-108`) | private, simulator-only, not invocable | **Avoid** on device; **Ideas** for the simulator |
| `collection`/`item`, `range` | no | no | none (slider `value` string) | **n/a** |
| `children` | XML tree | flat + `parentIndex` | `XCUIElementSnapshot.children` | **Ours**: keep our nested schema; flatten on the host if needed |

### 3.3 CLI commands

| droidctl command | A-D equivalent | iOS feasibility (from A-D's code) | verdict |
|---|---|---|---|
| `devices`, `ping`, `doctor` | `devices`, `device status`, startup reasons | yes | **Adopt** typed startup reasons (`installation.md:115`) into `doctor` |
| `setup` / `teardown` | `prepare` (Apple runner), lease cleanup | yes, but slow and needs signing | **Adapt**: setup = build + sign + install + launch; teardown must uninstall (they don't) |
| `diagnose` | sparse verdict + fallback screenshot | yes | **Adopt** the `healthy/recovered/sparse` verdict as `diagnose`'s input |
| `snapshot` (`--diff --find --in --raw --bounds --layout --geo --map --system`) | `snapshot -i -d -s --raw --diff --force-full` | yes | ours; **Adopt** off-screen summaries as a candidate for `list … more↓` on iOS |
| `where` | – | yes (host) | ours |
| `tap`, `long-press`, `tap --double` | `click/press`, `longpress` | coordinates only | **Adapt** (synthesized touch, honest `method`) |
| `type` (`--clear --append --enter --stdin`) | `fill`, `type`, `--text-stdin`, `--delay-ms` | yes, read-back verified | **Adopt** their typed text errors and `unconfirmed` evidence |
| `scroll`, `swipe`, `gesture --path` | `scroll`, `swipe`, `gesture pan/fling/pinch` | gestures only | **Adapt**: scroll = swipe inside the target container's frame |
| `scroll-to` | `scroll --until` | yes | **Adopt** `movement`/`scroll_no_progress` reporting |
| `action`, `set`, `expand`, `collapse`, `dismiss`, `focus` | – | no public API | **unsupported** on iOS (typed error); `set` on sliders via `adjust(toNormalizedSliderPosition:)` (**inferred**, public XCUIElement API, not used by A-D) |
| `back` | `back` | no system back | **Adapt**: explicit back-control match or edge swipe; never a blind point |
| `home` | `home` | yes | **Adopt** |
| `recents`, `notifications`, `quick-settings`, `press KEY` | `app-switcher`; hardware buttons | partial (gestures) | **Ideas** |
| `wait --text/--id/--gone/--idle` | `wait text/selector/absent/stable` | polling | **Adopt** their strict `absent` semantics ("passes only when a complete, readable capture has zero matches", `commands.md:374`) |
| `mark`, `logs`, `watch` | logs, traces | `log stream` exists on host (inferred) | **Ideas** |
| `current` | `appState` | partial | **Adapt** (§2.6) |
| `shot --marks --crop` | `screenshot --overlay-refs` | yes | ours |
| `launch`, `stop-app`, `apps`, `install`, `open-url` | `open`, `close`, `apps`, `install`, `reinstall`, deep links | via `devicectl` | **Adopt** `devicectl device process launch` etc. on the host |
| `run` | `batch`, runner `sequence` | yes | ours; **Ideas**: a runner-side fused sequence for coordinate gestures |
| `mcp`, `serve`, `cheat`, `skill` | MCP, client API | host-only | ours |

### 3.4 MCP tools
Ours are 12 (`droidctl/mcp.py:22-37`): `snapshot, tap, type, scroll, swipe, action, key, wait, launch, shot, logs, devices`. agent-device generates its tools from the full command metadata, with output schemas and "ref pins" (`src/mcp/command-tools.ts:1-80`, `tool-ref-pins.ts`). Verdict: **ours**. Their ref pinning per tool call is an **Idea** if we ever expire refs on mutation; we don't (we re-resolve).

### 3.5 Cross-cutting

| concern | agent-device | droidctl | verdict |
|---|---|---|---|
| snapshot compaction | runner fold + host compaction, off-screen summaries, indented tree | prune/merge/dedupe, spatial rows/regions/grids, `sig`, `unchanged` | ours; **Adopt** the fold's scroll-clip rule and off-screen summaries for iOS |
| refs | positional, expire on every mutation (ADR 0014) | fingerprint re-resolution, numbers carried on one screen | **Avoid** theirs; ours is stricter on look-alikes and friendlier to agents |
| resolver tiers | none; selector queries (`id`, `label`, `value`, `text`) with ambiguity refusal (`RunnerTests+Interaction.swift:150-200`) | uid → exact → locators → scoring | ours (tier 1 drops out on iOS); **Adopt** their candidate-fact classification for the device-side locator |
| typed errors | 100s of codes + `details.reason` | one `ERROR_KINDS` | ours; **Adopt** the *runner* codes that map onto ours: `RUNNER_BUSY`/`WEDGED` → `connection`, `APP_NOT_RUNNING` → `not-found`, `ELEMENT_OFFSCREEN` → `offscreen`, `TEXT_ENTRY_MISMATCH` → `no-change`; add `not-signed`/`locked` reasons under `device` |
| change verification | opt-in `--verify` / `--settle` | always on, `handled:true` | ours |
| `run` batching | `batch` + runner `sequence` | `run` over one connection | ours |
| lost-response recovery | `commandId` journal + `status` | `connection` + `maybe_performed` | **Adopt** the journal on iOS |
| recorded XCTest issues | muted by class (`RunnerTests.swift:241-288`) | n/a | **Adopt**: mandatory for a long-lived runner |
| main-thread watchdog | 30 s, busy/wedged | 2 s tree budget + `degraded` | **Adopt** |

### 3.6 Each side's gaps
**agent-device has, we lack (for iOS):** usbmux and CoreDevice transport; build cache + signing env + typed startup reasons; runner adoption across daemon restarts; XCTest-channel penalty and quality verdicts; a commandId journal; keyboard-band facts beside the tree (`DataPayload.keyboard`, `RunnerTests+Models.swift:445-449`); orientation and foldable coordinate normalization; system-surface registry; RN `value` semantics; `target_still_moving` re-observation; screen recording.

**We have, they lack:** node actions (Android); settle and the diff on every action, in the same round trip; fingerprint re-resolution and stable numbers; an event stream (toasts, windows, clicked events); `handled:true` (click received, nothing changed); spatial layout; `--expect-change`; strict "never guess by position" (their `back` fallback and Maestro non-hittable coordinate fallback, `RunnerTests+TypeExecution.swift:14-19`, both guess); a tiny, dependency-free device side.

---

## 4. droidrun ios-portal

- **Status:** archived. The README says only "Deprecated — superseded by mobilerun-ios … kept for reference only" (`README.md:12-20`). mobilerun-ios is published as a releases-only repo. No technical reason for the archive is given.
- **Shape:** a SwiftUI host app (`droidrun-ios-portal/DroidrunPortalApp.swift`) plus a UI-test target. The "test" starts a FlyingFox `HTTPServer` on the first free port in 6643–6652 inside `setUpWithError` and then runs `RunLoop.main.run()` forever. The test method itself is empty (`droidrun-ios-portalUITests/DroidrunPortalServer.swift:15-42,81-83`).
  - It is launched with `xcodebuild test` (build and run each time) (`device.sh:3-7`) and reached through `iproxy` on a device (`README.md:104-117`).
  - `DEVELOPMENT_TEAM = 5KTKV6FRNZ` is committed in the project (`project.pbxproj:271`), so every user must edit signing.
- **No authentication at all:** no token, no peer check. FlyingFox `HTTPServer(port:)` is constructed with a port only (`DroidrunPortalServer.swift:27`), so it listens on every interface (**inferred** from FlyingFox's default). On a device, anyone on the same Wi-Fi can drive it.
- **Routes** (`DroidrunPortalHandler.swift:94-189`): `GET /state`, `GET /vision/screenshot`, `POST /inputs/launch|type|key`, `POST /gestures/tap|swipe|back`, `GET /device/date`.
- **Tree:** `XCUIApplication.debugDescription`, with memory addresses removed by regex and "Other" lines without identifier/label/placeholder dropped (`AccessibilityTree.swift:10-42`). It is text, not structure. `/state` adds `keyboardVisible` (`app.keyboards.element.exists && isHittable`), the focused element (`hasKeyboardFocus == true` predicate) and screen bounds (`DroidrunPortalTools.swift:356-389,392-406`).
- **Foreground app:** a hard-coded list of 19 bundle ids polled for `.runningForeground`, falling back to SpringBoard or "Unknown" (`DroidrunPortalTools.swift:107-127,172-201`). A third-party app is therefore always "Unknown" unless launched through `/inputs/launch`.
- **Actions:**
  - `tap` takes an `NSCoder` rect string and taps its midpoint relative to the app origin (`:471-495`);
  - `type` taps the rect, finds the `hasKeyboardFocus` element and calls `typeText` in 100-character chunks (`:584-613`);
  - `clear` taps near the bottom-right of the field and types `delete × n`, checking progress per pass (`:511-582`);
  - `back` taps `navigationBars.buttons.element(boundBy: 0)`, the first nav-bar button, whatever it is, or else edge-swipes (`:657-677`).
- **Side effect at start:** `reset()` activates SpringBoard, which sends the phone to the home screen when the server starts (`:203-207`; called from `DroidrunPortalServer.swift:21`).
- **What is good:**
  1. `/state` returns a degraded but well-formed response on any error instead of dropping the connection, because "the test runner avoids accumulating unhandled errors that cause xcodebuild to kill it" (`DroidrunPortalHandler.swift:94-118`). It is the same failure agent-device fixes with its `record(_:)` override.
  2. `fetchAccessibilityTree` caps the window wait at 2 s, "shorter than clients' /state HTTP timeout", so a stale request can't overlap the next command (`DroidrunPortalTools.swift:455-465`).
  3. `openApp` uses `activate()` instead of `launch()` for SpringBoard, because relaunching SpringBoard "locks the phone" (`:438-441`).
- **Verdict:** **Avoid** the code: no auth, a text tree, positional `back`, a hard-coded foreground list. **Ideas only:** items 1–3 above. The MIT license would allow porting, but nothing here is worth it.

---

## 5. Verdict: fork agent-device's runner, or write our own?

**The license allows a fork.** MIT, with the copyright line kept in our NOTICE and in each ported file's header. The idb-derived simulator bridge carries its own `LICENSE.idb` (MIT) if we ever take it.

**Why not fork the runner as a whole:**
1. **Size and scope.** 17k production lines, much of it serving tvOS (remote presses), macOS (mouse, desktop scroll, menubar), recording, foldables, Maestro compatibility and their trait/journal contract. Forking means owning all of it or a long cut, and upstream changes daily (the clone's head commit is from today).
2. **The wire contract is theirs.** Their HTTP framing, `{ok,data,error}` envelope, 36-command vocabulary, `CommandTraits`, `commandId` journal and `runnerFatal`/`runnerMainThreadBusy` stamps are coupled to their TypeScript recovery logic (`RUNNER_PROTOCOL.md:164-193`). Switching to our NDJSON JSON-RPC (`PROTOCOL.md`) touches the transport, dispatch, models and every response builder.
3. **The node schema is flat and thin.** It is `PresentedNode` with `parentIndex`, already folded and presented on the device, with no handle stability and no raw/visible split we would want. Our design is "device dumps raw, host prunes", so their runner-side presentation is in the wrong place for us.
4. **The ref and act model differs.** They resolve on the host and send coordinates; we want `act {handle, gen, action}` and `tree`/`act`/`settle` in one round trip. Their runner has no notion of a handle into the last snapshot.
5. **Two behaviours violate our rules:** the blind top-leading `back` tap and the non-hittable coordinate fallbacks.

**What to port (Adopt, MIT attribution), with sizes from this checkout:**

| piece | files | lines | change for droidctl |
|---|---|---|---|
| listener + token + peer classification | `RunnerTests.swift:290-358` (subset), `RunnerTests+Environment.swift`, `RunnerListenerToken.swift`, `RunnerListenerPeer.swift` | ~170 | NDJSON over one persistent connection instead of HTTP one-shot; keep the token in the xctestrun env |
| main-thread watchdog | `RunnerTests+MainThreadWork.swift` | 197 | keep; map busy/wedged onto our `connection` + a `busy` data flag |
| issue muting + ObjC exception catcher | `RunnerTests.swift:241-288`, `RunnerObjCExceptionCatcher.{h,m}` | ~75 | keep verbatim |
| bounded tree capture + query-sweep tier | `RunnerTests+SnapshotAcquisition.swift` (subset), `RunnerTests+Snapshot.swift` (subset) | ~600 of 1,181 | emit our raw node schema (nested), drop presentation; keep the slices, caps and AX-failure classification |
| SpringBoard blocking-modal probe | `RunnerTests+SystemModal.swift` (subset) | ~150 | return it as an extra window in `tree` |
| quiescence-skip / bounded idle wrapper | `RunnerTests+Lifecycle.swift:419-483` | 65 | keep; it is the core of our `settle` |
| synthesized touch and text | `RunnerXCTestEventBridge.{h,m}`, `RunnerSynthesizedGesture.{h,m}`, `RunnerSynthesizedTextEntry.{h,m}` | ~1,160 | keep the reflection core; drop foldable/display routing initially |
| element-type table | `RunnerTests+Snapshot.swift:76-113` | 40 | keep |
| visibility fold (host side, in Python) | `SnapshotVisibilityFold.swift` | 249 | **reimplement in `snapshot.py`** (it is pure geometry), with attribution |

That totals ~2,700 lines of Swift/ObjC to read and ~1,500–2,000 to keep. **Recommendation: write our own runner ("WDA-lite", as `ios.md` proposed) of roughly 2,500–3,500 lines**, seeded with those pieces, and speaking:
- `ping`, `tree` (raw nested nodes + per-tree `gen` + handles = indices valid for that tree only), `act {gen, handle, action:"tap"|"long"|"double"|"type"|"clear", args, settle}`, `gesture`, `global {home}`, `wait_idle`, `wait_for` (runner-side polling), `screenshot`, `current` (bound app + state), `clipboard`, `shutdown`, plus `status {id}` from their journal idea;
- `act` resolves the handle by **re-querying the element** (identifier/label/type/frame match from the last snapshot) on the device and taps its hit point via synthesized events, then folds in quiescence and one `snapshot()` in the same reply. On iOS that is the only way to get one round trip per action, since every host round trip also costs a usbmux connect (`usbmux-runner-transport-experiment.md:37-39`).

Then measure (estimates until measured): `tree` ≈ 300–450 ms on a recent iPhone (their 440 ms includes the daemon), `act` + settle ≈ 0.6–1.0 s.

**Host-side pieces worth porting into Python (Adapt, MIT):** the usbmuxd plist client (`runner-usbmux-protocol.ts`, 333 lines), the xctestrun env patch (`runner-artifact-env.ts:89-115`), the `build-for-testing` / `test-without-building` split and signing env (`runner-artifact.ts:863-912`, `runner-cache-metadata.ts:258-285`), and the startup-failure classifier's reasons (`installation.md:115-116`). Their lease/adoption design (`runner-adoption.ts`) is worth copying in spirit: our daemon is the natural owner, and adoption turns a ~13–38 s restart into ~1 s.

---

## 6. Lessons for droidctl

1. **Plan for no node actions on iOS, and say so in output.** agent-device's negative result on `performAction`/`setAttribute` (`RunnerAXSnapshotBridge.h:73-80`) means `ios.md` blocker 1 is real until someone shows otherwise on a device. Results must report `method:"synthesized-touch"`; `action`, `expand`, `dismiss` and `set` are `unsupported` on iOS (except perhaps `set` on sliders, **inferred**).
2. **Fold `act` + quiescence + tree into one runner reply.** agent-device pays a host-polled settle at ~440 ms per capture with a 500 ms quiet window (`stable-capture.ts:37-84`). Our one-round-trip `act … settle` matters even more on iOS than on Android.
3. **A long-lived XCTest runner must mute AX-error issues, or XCTest kills it** (`RunnerTests.swift:263-288`; droidrun's degraded `/state` for the same reason). Make this a day-one test.
4. **Bound every XCTest call and assume it cannot be cancelled.** Use slices (8 s tree, 1 s sweep, 4 s modal probe, 20 s plan, 30 s main-thread cap), track abandoned main-thread work, and refuse fast while it drains (`RunnerTests.swift:102-146`). This is our "2 s budget + `degraded:true`" rule, with the twist that an abandoned call keeps the main thread busy.
5. **Remember hostile screens.** A per-bundle penalty (120 s) after a slow XCTest grind stops every later capture from paying it again (`SnapshotCapturePlan.swift:143-296`). Our daemon could hold the equivalent per-app hint.
6. **usbmux first, CoreDevice tunnel only as the fallback,** and treat "device not attached" as non-retryable (`usbmux-runner-transport-experiment.md:44-79`). The tunnel's 30 s cache TTL cost ~4.5 s per idle gap, exactly the agent workload.
7. **Per-launch token in the xctestrun environment + peer-interface check** replaces our peer-UID auth on iOS (`RUNNER_PROTOCOL.md:15-38`). Require the "auth: required" reply header so an old runner is refused.
8. **Adopt live runners across daemon restarts.** 1.15 s vs a 13–38 s cold start (§1.4), logging to a file so the runner survives its parent.
9. **Bind a session to an app; don't pretend to know the foreground app** (`RunnerTests+Models.swift:417-427`). Our `current` on iOS = bound app + `XCUIApplication.state`, labelled as such.
10. **Keep our ref model.** Their "every mutation expires refs" rule was a response to the same bug class (#1239) that we solved by re-resolution + `carry_refs`. On iOS, where every capture costs ~0.4 s, forcing a fresh snapshot after every action is expensive, which is a further argument for ours.
11. **Prefer `hiddenContentAbove/Below` + off-screen summaries over emitting offscreen nodes** on iOS, where scroll direction hints don't exist (`SnapshotModels.swift:302-303`; `snapshots.md:84-86`).
12. **Typed text-entry outcomes:** port `TEXT_ENTRY_MISMATCH` vs `verification:"unconfirmed"` with `requested/before/after` (`RunnerTests+TypeExecution.swift:98-150`). It is the iOS form of our read-back `value` and `no-change`.
13. **Never change keyboard settings.** agent-device shows a reliable iOS typing path without WDA's autocorrect/prediction changes. This keeps "never break the user's device" achievable on iOS.
14. **Teardown must uninstall the runner app,** which agent-device leaves on physical devices (§2.8). With a Personal Team it also uses one of the 3 app slots (`ios.md` I4).
15. **Fixtures:** their Swift/TS parity tables (`contracts/fixtures/*.json`) are the same idea as our "fixtures come from real devices". iOS raw trees must be captured from a real device with our runner's `tree`, not from a simulator bridge, because the two backends return different vocabularies (ADR 0004).

---

## 7. Corrections to `research/ios.md`

- **TL;DR / blocker 2, "~38 s to cold-start and ~1 s to re-adopt":** the 38.2 s was explicitly a worst case with a cold CoreDevice tunnel (health check 36.9 s of it). The cached-build prep is ~13 s plus ~6.7 s of `devicectl` listing (`usbmux-runner-transport-experiment.md:25-27`; handoff evidence `:97-99`). "~1 s" is right (1,151 ms).
- **Blocker 1, "no device-side project in this set uses [`XCAXClient_iOS performAction`]":** true, but agent-device *tried* it and recorded that it fails with `kAXErrorCannotComplete` for every action (`RunnerAXSnapshotBridge.h:73-80`). Treat that as negative evidence (probably simulator, **inferred**), not as an open path.
- **§5.3 "Snapshots: XCTest recursive snapshots for physical devices; a private host AX bridge for simulators":** incomplete. The runner also has an in-runner **private-AX tier** (`XCAXClient_iOS requestSnapshotForElement…` with a depth ladder, `RunnerAXSnapshotBridge.m:325-400`; `RunnerTests+AXSnapshotFallback.swift:34-41`) that is policy-gated to the simulator, and a **query-sweep tier** that *does* run on devices (`SnapshotBackendCapabilities.swift:80-86`; `RunnerTests+Snapshot.swift:444-515`).
- **§5.3 "devices accept only loopback (usbmux) or CoreDevice `utun` peers":** the device listener binds every interface and *closes* other peers after classification (`RunnerTests.swift:348-358`; `RunnerTests+Transport.swift:32-45`). A phone-side VPN `utun` would also classify as `tunnel` (**inferred**).
- **I1 "Speed": "Maestro, agent-device and WDA all carry recovery paths":** correct, but agent-device's device-side recovery is only the query sweep, plus a 1 s bounded XCTest probe once penalized. Its strongest recovery (private AX, host bridge) does not exist on devices (`snapshots.md:271`).
- **§5.7 droidrun:** accurate, and two omissions matter: the server has **no authentication** and listens on all interfaces (**inferred**), and it sends the phone to the home screen on start (`DroidrunPortalTools.swift:203-207`). It is archived without a stated technical reason (`README.md:12-20`).
