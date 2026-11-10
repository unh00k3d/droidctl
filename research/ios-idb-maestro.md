# facebook/idb and Maestro's iOS driver: analysis for droidctl

Question: what do idb's simulator accessibility path and Maestro's XCUITest runner teach us about an iOS backend for droidctl, and which of their pieces can we take?

Sources, read 2026-10-16:
- **Shallow clones** under the session scratchpad (`…/scratchpad/ios-src/{idb,maestro}`), read as data. I ran none of their build, test or install scripts.

  | repo | commit | license |
  |---|---|---|
  | facebook/idb | `e8b007e` (2026-10-15, "Observe volume changes") | MIT (`LICENSE:1-3`) |
  | mobile-dev-inc/maestro | `3b068b8` (2026-10-16) | Apache-2.0 (`LICENSE:1-3`). Nine `.m`/`.h` files in the runner carry WDA's "Copyright (c) 2015-present, Facebook, Inc." BSD header (e.g. `Categories/XCAXClient_iOS+FBSnapshotReqParams.m:2`, `Utilities/FBConfiguration.m:1-8`): those are WDA code, BSD-3. |

- Paths are relative to each clone's root. Maestro runner paths are relative to `maestro-ios-xctest-runner/maestro-driver-iosUITests/` unless they start with another top-level directory.
- **Local Xcode, read-only checks:** Xcode 26.2 at `~/Downloads/Xcode.app`, CoreSimulator **1051.17.7**, the one installed runtime iOS 26.2 (23C54). I checked for frameworks and symbols with `ls`, `strings` and `nm`. I compiled nothing, booted no simulator and spawned nothing, so every "works on Xcode 26.2" below means "the symbols exist", not "it ran".
- Anything I did not see in code is marked **(inferred)**. No number here was measured by me. Every latency is quoted from the project's own docs.

---

## TL;DR

- **idb today is not the idb in `research/ios.md`.** The accessibility path was rewritten in Swift as `FBSimulatorAX` plus an in-guest helper, `SimulatorFrameworkBridge`. `FBSimulatorAccessibilityCommands` no longer exists under that name; its successor is `FBSimulatorAX/Commands/SimulatorAccessibilityCommands.swift`. The project is very active (commit dated yesterday) and targets Xcode 26 and 27 (`README.md:80-82`; HID notes for Xcode 27 below).
- **The axbridge path is the closest thing iOS has to droidctl's Backend A**, and it is small enough to rebuild:
  - It is a command-line tool built for `iphonesimulator`, ad-hoc signed, and spawned inside the booted simulator. It binds a Unix socket **on the host filesystem**.
  - It `dlopen`s `XCTAutomationSupport` from the runtime's `/Developer` root and calls `-[XCTAccessibilityFramework userTestingSnapshotForElement:options:error:]`. That is XCTest's own snapshot call, made with **no test runner, no testmanagerd and no signing team**.
  - It does real AX actions: `AXUIElementPerformAction(press | scroll-by-page | scroll-to-visible)` and `AXUIElementSetAttributeValue(value)`. It resolves the target by **hit-testing a point and asserting the element's label** before it acts.
  - It reports settle from **the app's own run-loop-idle and animations-inactive signals**, XCTest's quiescence mechanism, with no screenshots.
- **Where ios.md is wrong:**
  1. The private entitlements it cites (`Privacy.md:27-30`) are for **TCC and HealthKit**, not accessibility. The AX path needs no entitlement in the simulator.
  2. idb is not "simulator-only": only accessibility, HID and REPL screenshots are (`IDBCommandExecutor.swift:136-259`). Install, launch, logs, screenshot, video, xctest and debugserver work on devices.
  3. Maestro's taps are not `XCUICoordinate`. They are private `XCSynthesizedEventRecord`s sent through `XCTRunnerDaemonSession.daemonProxy` (`Routes/XCTest/RunnerDaemonProxy.swift:36-50`), and XCTest's quiescence wait is **swizzled off** (`Categories/XCUIApplicationProcess+FBQuiescence.m:14`, `Utilities/FBConfiguration.m:15`).
- **Maestro's runner is a good *shape* and a poor *template*.** The shape (one XCUITest method that never returns and runs an HTTP server; the host polls `/status`) is the shape everyone uses. But:
  - simulator only; its device driver is a `TODO` spike (`maestro-cli/.../MaestroSessionManager.kt:157-165`);
  - no auth on the server;
  - no element addressing (coordinates only);
  - typing into whatever has focus;
  - settle by hashing two PNG screenshots.
- **Verdicts:**
  - **(a) Build an idb-style simulator backend: yes, as our own small Swift helper with no idb install, gated on one spike that measures it.** Every symbol it binds is present in the local iOS 26.2 runtime, x86_64 slice included. The open costs are swipes, which need host-side HID, and HID changing transport in Xcode 27.
  - **(b) Maestro's runner as a template: take the outline, not the code.** Take its snapshot walk, its recovery ladder, its frame offsets, its foreground-app detection and its event-synthesis calls. Replace its transport, auth, typing and settle.

---

## 1. idb

### 1.1 Architecture

```
idb (Python CLI, idb/cli) ── gRPC (proto/idb.proto, 1824 lines) ──► idb_companion (Swift, one per target)
                                                                      │  CompanionLib → FBSimulatorControl / FBDeviceControl / FBSimulatorAX
                                                                      ├─ simulator: CoreSimulator (in-process), SimulatorKit HID, IOSurface framebuffer,
                                                                      │             SimulatorFrameworkBridge spawned in the guest (Unix socket)
                                                                      └─ device:    MobileDevice.framework, lockdown services, DDI, devicectl, xcodebuild
```

- **Language.** The companion and its frameworks are "nearly a pure Swift codebase", apart from the XCTest DTX layer (`README.md:14`). The build needs macOS 15+, Xcode 26.0+, XcodeGen and protoc (`README.md:80-89`).
- **Distribution.** The companion ships as `Build/Distribution/` with `Resources/SimulatorFrameworkBridge-iOS` beside it (`README.md:96-115`). The axbridge backend fails with a clear error if that helper is not in `Resources` (`FBSimulatorAX/README.md:133-139`).
- **RPC surface.** About 80 RPCs (`proto/idb.proto:15-93`), including `accessibility_info`, `accessibility_action`, `accessibility_quiescence`, a streaming `hid`, `screenshot`, `video_stream` and `xctest_run`.
- **Binding.** The companion listens on TCP `::` (all interfaces), port 10882 by default (`CompanionLib/Configuration/IDBPortsConfiguration.swift:18-22`; `Companion/main.swift:75`). TLS is optional (`main.swift:77`), and **there is no auth** (inferred: nothing else in the bind path). A Unix socket is available with `--grpc-domain-sock` (`main.swift:78`), and the Python client uses it for local companions (`idb/common/companion.py:346,491`).
- **For a tight loop, idb itself recommends linking the framework instead of the CLI:** "Every cli invocation pays client startup and an RPC round trip … long-lived framework objects … keep the boot, the HID connection and the accessibility session alive" (`FBSimulatorControl/README.md:128-132`). That is droidctl's daemon argument, made independently.

### 1.2 Simulator vs device

The companion's command executor refuses these on a device with `simulatorOnlyOperation` (`CompanionLib/IDBCommandExecutor.swift:136-259`):
- describe accessibility, tap or scroll or set value or drag by accessibility, wait for accessibility, stream quiescence, frame lookup;
- the REPL screenshot (`:257-259`).

HID goes through `target.simulator()`, which throws for a device (`:631-635`).

**Works on a real device** through `FBDeviceControl`:
- install, launch and kill;
- logs, crash logs, files (AFC, house arrest);
- **screenshot** through the `com.apple.mobile.screenshotr` DeviceLink service (`FBDeviceControl/Commands/DeviceScreenshotCommands.swift:46-55`);
- **video** through AVFoundation capture devices (`FBDeviceControl/README.md:124-130`);
- `debugserver`, Instruments, and XCTest runs through `xcodebuild` (`Commands/DeviceXCTestCommands.swift:16-30`).

**What the device path depends on:**
- private `MobileDevice.framework` (`FBDeviceControl/README.md:11-21`);
- the Developer Disk Image for Instruments and debugserver (`:57-62`);
- **`xcrun devicectl` for app launch on iOS 17+** (`Commands/DeviceApplicationCommands.swift:241-247`);
- Xcode for xctest.

**There is no device UI tree and no device input in idb.** On a device, idb is a lifecycle and artifact tool, not a UI driver.

### 1.3 The accessibility path in depth

**Two backends, chosen with `--api`** (`FBSimulatorAX/README.md:15-41`):

| | `ax` (host) | `axbridge` (in-guest) |
|---|---|---|
| Where it runs | `idb_companion` on macOS, through the private `AccessibilityPlatformTranslation` framework that Simulator.app uses | `SimulatorFrameworkBridge`, a process inside the booted simulator |
| How it reaches the app | XPC to `com.apple.CoreSimulator.accessibility`, which CoreSimulatorBridge vends in the guest (`AXTranslationTransport.swift:41-70`; fallback `-[SimDevice sendAccessibilityRequestAsync:…]`) | mach messages to the app's AX server, from inside the guest |
| Tree | the **VoiceOver-flattened** one. An iOS 27 Settings screen gives 14 elements, 1 level deep (`README.md:124`) | the **XCTest-vocabulary** one: 152 elements, 21 levels deep on the same screen (`:124`) |
| Cost | 1 XPC round trip per element. A 159-element Calendar screen took about 99 ms (`:49-51`) | **1 call for the whole tree** ("single-fetch"), about 30 ms on an idle 167-element screen (`:88-92`) |
| Default for | reads (`:19`) | actions (`:19`) |
| Device side effects | none | **asserts `AutomationEnabled` device-wide and never restores it** (`:312-322`) |

**How axbridge reads the tree with no test runner.**
- **Spawning.** The host spawns the guest binary by host path through `SimDevice` spawn. "`SimDevice` resolves `launchPath` against the host filesystem rather than the runtime root" (`FBSimulatorControl/SimulatorFrameworkBridge/SimulatorFrameworkBridgeClient.swift:80-86`).
- **Connecting.** The guest binds a Unix socket in the **host's** per-user temp dir (`SimulatorFrameworkBridgeSocket.swift:20-45`), and the host connects to it (`SimulatorFrameworkBridgeTransport.swift:286-306`).
- **Three scopes:** one-shot, shared (adopt a running guest after a 250 ms ping probe) and exclusive (a private socket) (`:184-246`; `README.md:363-369`).
- **Frameworks.** The guest `dlopen`s five private frameworks plus `libAccessibility.dylib` and links none of them (`project.yml:46-47`): AXRuntime, XCTAutomationSupport, AccessibilityUtilities, AccessibilityPlatformTranslation and RunningBoardServices (`SimulatorFrameworkBridge/Runtime/AccessibilityRuntime.m:627-632`).
  - XCTAutomationSupport is loaded from `/Developer/Library/PrivateFrameworks/…` in the runtime root (`Runtime/Private/XCTAutomationSupportPrivate.h:9-24`).
- **The read itself.** `-[XCTAccessibilityFramework initForRemoteAccess]`, then `userTestingSnapshotForElement:options:error:`. The options **must** start from `XCTDefaultSnapshotParameters()`, with `options[@"attributes"]` set to the numbers that `XCAXAccessibilityAttributesForStringAttributes` returns. A dictionary not built that way "makes the server answer NULL under kAXErrorSuccess" (`AccessibilityRuntime.m:1505-1545`; `XCTAutomationSupportPrivate.h:33-40`).
- **The C entry points** come from `dlsym`: `AXUIElementCopyElementAtPosition`, `AXUIElementPerformAction`, `AXUIElementSetAttributeValue`, `AXUIElementSetMessagingTimeout` and `AXObserverCreate` (`:1215-1236`). Every AX message has a 5 s timeout (`:506`).
- **A guard against API drift.** Every private selector is checked against its recorded `method_getTypeEncoding` (`AccessibilityRuntime.m:575-622`; `AccessibilityRuntime.h:262-304`). A shape change becomes a named error instead of a misread stack. **Port the idea.**
- **Entitlements.** The guest is ad-hoc signed with entitlements embedded in `__TEXT,__entitlements`, but the build file says why: "the bridge's private entitlements (TCC/HealthKit)" (`project.yml:39-43`). The entitlements file lists only TCC, HealthKit, runningboard and bulletinboard keys (`SimulatorFrameworkBridge.entitlements`). **None is for accessibility.** That corrects ios.md §I6.

**Fields.**
- **Default fetch list:** `elementType, elementBaseType, label, value, identifier, frame, automationType, xcTraits, children`, all in the `XC_kAXXCAttribute*` vocabulary (`FBSimulatorBridgeProtocol/BridgeAXWire.swift:72-74`).
- **On request:** `isVisible`, `visiblePoint`, `centerPoint`, `userInteractionEnabled` (`:30-39`). These are expensive: about 18 ms per node against 0.14 ms, a factor of about 130, so 2.1 s against 16 ms on a 118-node tree (`README.md:253-262`). They also force the per-node walk, because a single fetch with them "times out" (`:109`; `AXBridgeUIAutomation.swift:207-215`).
- **Translator-only extras:** `FBIsEnabled`, role, subrole, raw traits, and a **per-element identity "stable while the element lives"**, `FBElementIdentity` (`BridgeAXWire.swift:40-66`). That identity is the nearest iOS analogue of `getUniqueId`.
- **CLI output keys:** `AXLabel, AXFrame, AXValue, AXUniqueId, type, title, frame, help, enabled, custom_actions, role, role_description, subrole, content_required, pid, traits, expanded, placeholder, hidden, focused, is_remote` (`README.md:160`).

**Nested or flat.**
- `--format default` is a **flat** array (`README.md:149-158`).
- `--format nested` adds `children` (`:162-164`).
- `--format complete` is a fixed-key document (`:166-201`): elements, backend, `truncated`, the frontmost `modal`, `automation {enabled, asserted}`, coverage and profile.
- The guest walk emits each element once, because "UIKit lists some elements under more than one parent" (`SimulatorFrameworkBridge/Swift/AccessibilityService.swift:280-283`). droidctl's tree pruning must expect such DAG duplicates.
- **Bounds:** depth 50 and 3000 nodes, reported as `truncated` (`README.md:141-143`).
- **Remote subtrees** (web views, photo picker, autofill) cost one more call per subtree, answered by the process that draws it (`:88-90`).

**Speed (idb's numbers, not mine).**
- About 30 ms for a single-fetch read of an idle 167-element screen (`README.md:92`).
- About 99 ms per host read on the 159-element Calendar screen (`:51`).
- Automation mode costs about 1.8x wall clock for about 15% more elements on a large app (`:322`).
- A cold read costs "one or two orders of magnitude" more than a warm one (`:286`).

**Element actions: real AX actions, addressed by point plus assertion.**
- **Host side.** A marker query (substring of a key, default `AXLabel`) is resolved against a fresh tree read, and the host takes the **frame centre** (`FBSimulatorAX/AXWriteTarget.swift:31-60`). It sends `{x, y, pid, assertion: {key, exact value}}` (`AXBridgeUIAutomation.swift:640-660`).
- **Guest side.** The guest hit-tests that point (`AXUIElementCopyElementAtPosition`). Then:
  - It **refuses if the hit element's asserted key differs**: `assertion_failed`, "the element at (x, y) has … expected …" (`AccessibilityService.swift:1148-1217`).
  - Otherwise it sends the action once (`:1256-1291`).
  - The host re-reads and retries **once** when the element moved, **because nothing was written** (`AXBridgeUIAutomation.swift:583-600`).
- **Actions** (`Runtime/Private/AXRuntimePrivate.h:73-85`):
  - `AXPress` (0x7da), `ScrollUp/Down/Left/RightByPage` (0x7d6–0x7d9) and `ScrollToVisible` (0x7d3);
  - `setValue` through `AXUIElementSetAttributeValue(kAXValue)` (`AccessibilityRuntime.m:1834-1845`);
  - if `AXPress` is refused with `kAXErrorFailure`, the native `-[AXElement press]` (`:1798-1830`), but **never a retry after a timeout** (`AccessibilityRuntime.h:221-222`).
- **Not supported on axbridge:** custom actions are refused with `operationUnsupported` (`AXBridgeUIAutomation.swift:534-542`). The host `ax` backend does them by name and refuses an ambiguous name (`AccessibilityUIAutomation.swift:113-137`; `AccessibilityElement.swift:133-147`). A drag is the only HID gesture (`AXBridgeUIAutomation.swift:561-581`). There is no long-press either: a duration is refused rather than downgraded (`:525-530`).
- **Two traps worth copying the handling of:**
  1. "There is no 'element does not accept this action' outcome … **an ignored action reports as a success**" (`AccessibilityRuntime.h:212-219`). That is droidctl's `performed:true` on a touch-only view, on iOS. Verify by diff, never trust `performed`.
  2. A write whose request left the host but whose answer did not arrive becomes `writeUnconfirmed(idempotent:)`, unless the guest says nothing was sent (`AXBridgeUIAutomation.swift:661-680`). That is droidctl's `maybe_performed`.
- **One rule we must not copy:** the marker resolves to "the first match … whose value equals, otherwise the first that contains it" (`AXTreeWalk.swift:56-57`; `:185-188`). That is a position guess, which our non-negotiables forbid.

**Settle (quiescence) without screenshots.**
- The guest sends the app the AX actions `kAXBeginMonitoringIdleRunLoopAction` (0x7fa) and `kAXDetectAnimationsNonActiveAction` (0x7fb) through `AXUIElementPerformActionWithValue`. It listens with an `AXObserver` for the user-testing notifications "RunLoopIsIdle" and "AnimationsNonActive" (`AXRuntimePrivate.h:86-96`; `AccessibilityRuntime.m:950-972`). These "only [answer] in automation mode".
- `QuiescenceTracker` calls an app **busy** when a request stays unanswered for more than 20 ms, and **quiet** after a 250 ms calm window (`SimulatorFrameworkBridge/Swift/AccessibilityQuiescence.swift:19-80`; `BridgeAXWire.swift:284-289`). The tracker also hears touch-completed and app-state-changed events (`AccessibilityRuntime.h:136-146`).
- This is XCTest's own idle wait, done from outside a test, and it maps directly onto droidctl's quiet-window settle.

### 1.4 HID, screenshots and video (simulator)

- **Touch, keys and buttons.** idb supports two transports (`FBSimulatorControl/README.md:230-238`):
  - the reverse-engineered **Indigo** mach protocol, through SimulatorKit's `IndigoHIDMessageFor*` and `SimDeviceLegacyHIDClient` (`HID/SimulatorIndigoHID.swift:48-64`; `HID/SimulatorIndigoHIDClient.swift:14-60`);
  - the **`dtuhidd` XPC** transport.

  **From Xcode 27 (CoreSimulator 1155.4) the guest drops Indigo button and keyboard events, and Indigo touch is unreliable**, so DTUHID is the default there (`HID/SimulatorHIDTransportSelection.swift:23-53`). `dtuhidd` is demand-launched and can need about 19 s to answer after a slow boot (`HID/SimulatorDTUHIDConnection.swift:13-37`).
  - On this Mac (CoreSimulator 1051.17.7, iOS 26.2) the Indigo path applies. SimulatorKit exports `IndigoHIDMessageForMouseNSEvent`, `…ForButton`, `…ForKeyboardArbitrary` and `SimDeviceLegacyHIDClient`, and the 26.2 runtime has no `dtuhidd` (checked).
- **Text.** `idb ui text` maps each character to US keyboard HID codes and **raises on anything else** ("No keycode found", `idb/common/hid.py:255-262`). Unicode text has to go through AX `set-value` instead.
- **Taps.** `idb ui tap x y` is a HID touch unless `--api ax|axbridge` is given. A marker tap is always an AX press (`idb/cli/commands/tap.py:50-68,96-106`).
- **Screenshots and video.** The simulator's `IOSurface` framebuffer is read in-process: zero-copy, H.264, MJPEG or raw (`FBSimulatorControl/README.md:121,196-204`). **A simulator shown in Simulator.app or DeviceHub has its framebuffer consumed by that app** (`:194`). (Inferred: `simctl io screenshot` does not have this limit, so droidctl's `shot` should use simctl.)

### 1.5 Maintenance and Xcode compatibility

- **Very active.** The newest commit is dated 2026-10-15. The docs describe iOS 26.4 and 27.0 runtimes (`FBSimulatorAX/README.md:343`) and DeviceHub.app, Xcode 27's replacement for Simulator.app (`FBSimulatorControl/README.md:190-194`).
- **Checked on the local Xcode 26.2 / iOS 26.2 runtime.** Each of these exists, by `ls` plus `strings`/`nm`:
  - in the runtime root, `Developer/Library/PrivateFrameworks/XCTAutomationSupport.framework`, a universal **x86_64 + arm64** binary that contains `userTestingSnapshotForElement:options:error:`, `initForRemoteAccess`, `attributesForElement:attributes:error:`, `_XCTDefaultSnapshotParameters`, `_XCAXAccessibilityAttributesForStringAttributes` and `XCTElementTypeFromAXAutomationType`;
  - `AXRuntime` (`_AXUIElementCopyElementAtPosition`, `_AXUIElementPerformAction`, `_AXUIElementSetAttributeValue`, `_AXObserverCreate`);
  - `AccessibilityPlatformTranslation` (`frontmostApplicationWithDisplayId:bridgeDelegateToken:`, `objectAtPoint:displayId:bridgeDelegateToken:`);
  - `AccessibilityUtilities` (`setAutomationEnabled:`);
  - `usr/lib/libAccessibility.dylib` (`__AXSAutomationEnabled`);
  - on the host, CoreSimulator's `sendAccessibilityRequestAsync:completionQueue:completionHandler:` and the `com.apple.CoreSimulator.accessibility` service string.

  **So the binding surface is present on Xcode 26.2.** Whether it *behaves* on 26.2 is unverified. idb's documented verification is arm64 26.4 and 27.0, and the x86_64 simulator (this Intel Mac) is not mentioned.

---

## 2. Maestro's iOS driver

### 2.1 Architecture

```
maestro CLI (Kotlin) ──HTTP JSON, OkHttp──► 127.0.0.1:22087 ──► maestro-driver-iosUITests-Runner.app (XCUITest bundle in the simulator)
   LocalXCTestInstaller                                          testHttpServer(): FlyingFox HTTPServer, never returns
   xcodebuild test-without-building -xctestrun <prebuilt>        Routes: viewHierarchy, touch, swipe(V2), inputText, eraseText,
   (or: simctl launch / devicectl process launch …xctrunner)            screenshot, isScreenStatic, pressKey/Button, launchApp, …
```

- **The runner** is one test method, `testHttpServer()`, that starts a FlyingFox server and awaits forever (`maestro_driver_iosUITests.swift:26-30`).
  - It sets `continueAfterFailure = true`, so XCTest-internal assertion failures do not kill the server (`:14-20`).
  - It binds `127.0.0.1:$PORT`, default 22087, with a 100 s timeout (`Routes/XCTestHTTPServer.swift:31-42`). There are 20 routes (`:4-24`; `RouteHandlerFactory.swift:5-48`).
  - **No auth.** (Inferred: a simulator shares the host's network stack, so any local process on the Mac can drive the simulator through 22087.)
- **How the host builds it.** The runner is **prebuilt for the simulator** (x86_64 + arm64, no team) and shipped as zips inside the driver jar (`build-maestro-ios-runner.sh:26-31,43-68`; `maestro-ios-driver/.../IOSBuildProductsExtractor.kt:37-60`). The user's machine never compiles Swift.
- **How the host launches it** (`maestro-ios-driver/.../installer/LocalXCTestInstaller.kt:194-241`). The default is `prebuiltRunner = false` (`maestro-cli/.../MaestroSessionManager.kt:376,384`), which means `xcodebuild test-without-building -xctestrun … -destination id=<udid>` with `TEST_RUNNER_PORT` (`util/XCRunnerCLIUtils.kt:104-133`). The other mode installs the bundle and launches the `.xctrunner` app directly:
  - `simctl launch --terminate-running-process … dev.mobile.maestro-driver-iosUITests.xctrunner` with `SIMCTL_CHILD_PORT` (`util/LocalSimulatorUtils.kt:347-372`);
  - `devicectl device process launch --terminate-existing …` for a real device (`util/LocalIOSDeviceController.kt:29-51`).
- **Readiness.** The host polls `GET /status` every 500 ms for up to **120 s** (`MAESTRO_DRIVER_STARTUP_TIMEOUT`) (`LocalXCTestInstaller.kt:125-142,268`).
- **Recovery.**
  - The first transport `IOException` latches the runner as dead, and every later call fails fast (`maestro-ios-driver/.../XCTestDriverClient.kt:32-80`).
  - Only `swipeV2` is sent one-shot, so OkHttp never re-sends it after a timeout (`:148-166,363-375`). **`touch` and `inputText` are not one-shot** (`:170-188`). (Inferred from OkHttp's default retry-on-connection-failure and 408 behaviour: a tap could be delivered twice. droidctl's "never resend an action" rule exists for exactly this.)
  - `close()` only kills the runner process. The FIXME says uninstall would suffice, but nothing uninstalls (`LocalXCTestInstaller.kt:64-99,244-256`).
- **Real devices: not supported.** The CLI refuses a physical iPhone because the device driver "is an unfinished spike: almost every method is `TODO("Not yet implemented")`" (`maestro-cli/.../MaestroSessionManager.kt:157-165`; `CHANGELOG.md:11`). Even the device launch path polls `127.0.0.1` with no port forward anywhere in the iOS driver (`LocalXCTestInstaller.kt:160-173`; grep for `iproxy|usbmux|forward` finds nothing). (Inferred: it could never reach a device runner as written.)

### 2.2 How it fetches the hierarchy (`Routes/Handlers/ViewHierarchyHandler.swift`)

- **Foreground app.**
  - Private `XCAXClient_iOS.activeApplications` gives the apps. Each pid's bundle id comes from `_XCT_requestBundleIDForPID:reply:` on the test-runner daemon proxy, with a 1 s wait per pid (`Categories/XCUIApplication+Helper.m:40-69`).
  - The first app whose `XCUIApplication.state == .runningForeground` wins. Otherwise the handler returns SpringBoard (`Routes/XCTest/RunningApp.swift:27-40`; handler `:23-30`).
  - `DockFolderViewService` on the iOS 26 iPad home screen is replaced with SpringBoard (`:48-55`).
- **The walk.** `xcuiElement.snapshot()` is walked recursively (`:306-358`), not `dictionaryRepresentation`, "which is O(subtree) per call" (`:311-313`). Fields:
  - `identifier, frame, value (String only), title, label, elementType, enabled, horizontal/verticalSizeClass, placeholderValue, selected, hasFocus`;
  - `displayID` and `windowContextID` through KVC (`:341-357`).

  **No traits, no hittable or visible flag, no custom actions.** The host maps these to Android-style attributes, and `checked` = (checkable type && value == "1") (`maestro-client/.../drivers/IOSDriver.kt:212-241`).
- **Other apps and windows are merged in:**
  - every SpringBoard status bar (`:248-258`);
  - on iOS 26+, `com.apple.SafariViewService` when it is running and has web views (`:260-292`);
  - when the app element is skipped, the app's keyboard, the first alert, and lone custom window children (`:184-201,222-246`).
- **Coordinate fixes.**
  - If the app's frame differs from SpringBoard's frame, every frame is shifted by the size delta, unless it is the same area in swapped orientation (`:68-112`).
  - At a cross-process window boundary (windowContextID changes, the subtree is remote, and `visibleFrame` is finite), the snapshot's `visibleFrame - frame` offset accumulates into descendants (`:360-403`).
- **The recovery ladder** (`:143-220`):
  1. Snapshot the app.
  2. On a "recoverable" error, swizzle `-[XCAXClient_iOS defaultParameters]` to `maxDepth = 60`. The recoverable errors are `kAXErrorIllegalArgument getting snapshot for element` and `kAXErrorInvalidUIElement` (stale mid-snapshot) (`:216-220`). The swizzle is in `Routes/XCTest/AXClientSwizzler.swift:8-50`. Then re-root at the window: the first descendant with more than one child (`:294-304`).
  3. If the tree is 60 or more deep, re-fetch each child separately.

  Two weaknesses:
  - The re-fetch uses `element.descendants(matching: .other).element(boundBy: i)`, i.e. the i-th *descendant* of type Other, not the i-th child (`:153-158`). (Inferred: it can fetch the wrong subtree.)
  - The `maxDepth` override is a process-global that is never reset (`AXClientSwizzler.swift:6,14-17`). (Inferred: after one failure every later snapshot is capped.)
  - **Port the ladder, fix both.**
- **Optional `snapshotKeyHonorModalViews = false`** exposes elements behind modals. It is env-driven and WDA-derived (`Categories/XCAXClient_iOS+FBSnapshotReqParams.m:66-80`).
- **A hidden side effect.** If the host stored a notifications permission, each `viewHierarchy` call on SpringBoard **auto-taps the matching permission button** (`Routes/Helpers/SystemPermissionHelper.swift:9-38`). The handler that stores it is `SetPermissionsHandler.swift:19`. A read that also acts is the opposite of droidctl's model.
- **Interruption monitors** are disabled by swizzling `doesNotHandleUIInterruptions` to YES (`Categories/XCUIApplication+Helper.m:11-38`).

### 2.3 How it taps, types and swipes, and how it settles

- **Tap and long-press.** The route builds a private `XCSynthesizedEventRecord` with one `XCPointerEventPath` (`initForTouchAtPoint:offset:`, `liftUpAtOffset:` after 0.1 s or the hold duration) (`Routes/XCTest/EventRecord.swift:14-29`; `PointerEventPath.swift:5-39`). It sends the record with `-[XCTRunnerDaemonSession.sharedSession.daemonProxy _XCT_synthesizeEvent:completion:]` (`RunnerDaemonProxy.swift:7-18,36-50`; `TouchRouteHandler.swift:20-48`).
  - **Not `XCUICoordinate`:** there is no `XCUICoordinate` and no public `typeText` anywhere in the runner (grep).
  - It is a coordinate in points, made orientation-aware (`Routes/Helpers/ScreenSizeHelper.swift`).
- **Element taps happen on the host.** Settle, re-find the element in the settled tree, tap the **bounds centre** (`maestro-client/.../Maestro.kt:221-260`).
  - An opt-in `retryIfNoChange` taps a second time when the hierarchy did not change (`:379-381,404-440`). It defaults to false in the orchestra (`maestro-orchestra/.../Orchestra.kt:435,441`). That is exactly the double-trigger droidctl's tap policy rules out.
- **Swipe.**
  - v1 adds `moveToPoint:atOffset:` to the same event record (`EventRecord.swift:31-38`; `SwipeRouteHandler.swift:36-43`).
  - v2 dispatches through the foreground window's private `eventTarget` with `dispatchEventWithDescription:eventBuilder:error:` (`Routes/XCTest/EventTarget.swift:9-38`; `SwipeRouteHandlerV2.swift:40-65`). It maps XCTest's "Timed out while evaluating UI query" and "main thread busy for 5.0s" to HTTP 408, non-retryable (`:29-38`; tests at `maestro_driver_iosUITests.swift:40-115`).
- **Typing.**
  - It waits **at most 1 s** for *any* keyboard, then types anyway (`Routes/Helpers/TextInputHelper.swift:17-24`).
  - The first character goes slowly, then a 500 ms sleep, then the rest at 30 keys/s, all through `XCPointerEventPath initForTextInput` / `typeText:atOffset:typingSpeed:shouldRedact:` (`:26-49`; `PointerEventPath.swift:15-23,49-55`). The reason given: "characters after the first one are often skipped" (`:27-28`).
  - There is no target, no read-back and no secure-field handling.
  - `eraseText` types N delete characters (`EraseTextHandler.swift:22-26`).

  This is the "types into whatever has focus" failure droidctl fixed on Android (PLAN "Bug RCA 2026-10-07").
- **XCTest's quiescence is switched off.** `waitForQuiescenceIncludingAnimationsIdle:` is swizzled. It returns immediately when `FBConfiguration.waitForIdleTimeout < DBL_EPSILON` (`Categories/XCUIApplicationProcess+FBQuiescence.m:14,35,59-…`). That timeout is a static that defaults to 0 and is never set (`Utilities/FBConfiguration.m:15,22-30`; grep finds no caller of `setWaitForIdleTimeout`). (Inferred: this is why synthesized events return fast. The cost is that the runner never knows when the app settled.)
- **Settle.**
  - **Device side:** `isScreenStatic` takes two `XCUIScreen.main.screenshot()` PNGs and compares their SHA-256 (`Routes/Handlers/ScreenDiffHandler.swift:14-33`).
  - **Host side:** that check repeats until true for up to 3 s. If it never passes, the host falls back to polling the **whole hierarchy until two fetches are equal**: 10 tries 200 ms apart, or until a timeout (`maestro-client/.../drivers/IOSDriver.kt:494-510,706`; `utils/ScreenshotUtils.kt:38-74`).
  - The screen-static check "can pass while a scroll view is still slowly decelerating", so after a scroll Maestro re-fetches until the element's bounds are stable twice (`Maestro.kt:233-247,281-296`).
- **Private API surface on Xcode 26.2.** `XCUIAutomation.framework` (simulator platform) contains every selector the runner uses: `_XCT_synthesizeEvent:completion:`, `_XCT_sendString:maximumFrequency:completion:`, `XCSynthesizedEventRecord`, `initForTouchAtPoint:offset:`, `typeText:atOffset:typingSpeed:shouldRedact:`, `dispatchEventWithDescription:eventBuilder:error:`, `waitForQuiescenceIncludingAnimationsIdle:`, `XCAXClient_iOS`, `defaultParameters`, `activeApplications`, `doesNotHandleUIInterruptions` (checked with `strings`). Since Xcode 26 these live in XCUIAutomation, not XCTest.framework, as ios.md says.

### 2.4 Startup time and side effects

- **Startup.** Neither the code nor the docs give a number. The only figure is the 120 s budget (`LocalXCTestInstaller.kt:268`).
  - (Estimate, inferred: `xcodebuild test-without-building` plus the runner's own launch is several seconds on a simulator. ios.md's ~38 s cold start is agent-device's *physical-device* figure and does not transfer.)
  - **Measure it** in the spike.
- **Side effects on the simulator:**
  - two apps installed and left installed (`maestro-driver-ios.app`, the runner);
  - a long-lived `xcodebuild` process;
  - a listener on host loopback;
  - the auto-tap of permission dialogs above;
  - `applesimutils` or `simctl privacy` for permissions (`util/LocalSimulatorUtils.kt:459-486,536`).

  Unlike WDA, it **does not** touch keyboard autocorrection preferences (grep finds no `TIPreferences` or keyboard configuration).

---

## 3. Feature matrix against droidctl

Legend: ✔ real, ◐ partial or different, ✘ none. "Sim" = simulator only.

### 3.1 Device API (PROTOCOL.md methods)

| droidctl method | idb (axbridge unless noted) | Maestro runner | droidctl Android | Verdict for an iOS backend |
|---|---|---|---|---|
| `ping` | ◐ guest `ping` frame (`SimulatorFrameworkBridgeTransport.swift:267-280`) | ◐ `GET /status` | ✔ | trivial |
| `tree` (all windows) | ◐ frontmost app plus remote subtrees, modal detected (`README.md:88-90,194`); no status bar | ◐ foreground app + status bars + Safari service + keyboard/alerts (`ViewHierarchyHandler.swift:47-113`) | ✔ all windows | **Merge both**: idb's single-fetch read, Maestro's extra windows |
| `act` node actions | ✔ press, scroll ×4, scroll-to-visible, set value, by point + label assertion (`AccessibilityService.swift:1148-1291`); ✘ custom actions on axbridge | ✘ coordinates only | ✔ `performAction` | **idb model on the simulator**; on a device, synthesized taps at XCTest's hit point |
| `act` + settle in one round trip | ✘ separate `quiescence` stream | ✘ | ✔ | build: guest-side `act` → quiescence → `tree` |
| `gesture` tap/swipe/path | ◐ HID on the host (Indigo, or DTUHID from Xcode 27); a drag only, through axbridge | ✔ `XCSynthesizedEventRecord` (tap, long, swipe); multi-finger style exists (`EventRecord.swift:9-12`) | ✔ | device: Maestro's event record; simulator: HID or a runner |
| `global` back/home/… | ◐ HID buttons (home, lock, Siri, side) | ◐ `XCUIDevice.press(.home)`, `pressLockButton` (`PressButtonHandler.swift:21-23`) | ✔ | no "back" on iOS: report `unsupported` |
| `wait_idle` | ✔ run-loop idle + animations inactive, 20 ms busy, 250 ms quiet (`AccessibilityQuiescence.swift`) | ◐ two-screenshot hash | ✔ event quiet window | **port idb's tracker** |
| `wait_for` | ◐ marker polling (`AXBridgeUIAutomation.swift:308-354`) | ✘ host polling | ✔ on device | host-side polling over a cheap read |
| `subscribe` / `events` | ◐ quiescence stream only: touches completed, app state changed (`AccessibilityRuntime.h:136-146`) | ✘ | ✔ | no toast/window events on iOS; build what AXObserver gives |
| `screenshot` | ✔ IOSurface (Sim); device screenshotr | ✔ `XCUIScreen` PNG/JPEG (`ScreenshotHandler.swift:12-23`) | ✔ | Sim: `simctl io`; device: from the runner |
| `current` | ✔ frontmost by centre hit-test / window server / RunningBoard (`AXBridgeUIAutomation.swift:216-218`; `AccessibilityService.swift:845-890`) | ✔ `activeApplications` + state | ✔ | either |
| `type` (set text, Unicode) | ✔ AX `setValue` (Unicode); HID text is US-ASCII only (`hid.py:255-262`) | ◐ keystrokes to the focused field, no verify | ✔ set_text + read-back | **AX setValue first, then verified keystrokes** |

### 3.2 Node fields

| droidctl field | idb axbridge | Maestro | Note |
|---|---|---|---|
| class / role | `elementType`, `elementBaseType`, `automationType` | `elementType` (int) | map XCUIElementType to droidctl roles |
| id | `identifier` | `identifier` | accessibilityIdentifier, often empty in production apps |
| uid | `FBElementIdentity` (translator vocabulary only) | ✘ | the only stable per-element identity seen |
| text / desc / hint | `label`, `value`, `title`, `placeholder` | `label`, `value`, `title`, `placeholderValue` | iOS has no separate text vs desc |
| bounds | `frame` (points) | `frame` (points) + offsets | droidctl prints device px: multiply by the screen scale |
| visible / covered | `isVisible`, `visiblePoint` (expensive, per element) | ✘ | ask per action target only (`README.md:268-283`) |
| flags (enabled, selected, focused, checked) | traits bitmask, `FBIsEnabled` | `enabled`, `selected`, `hasFocus`, checked = value "1" | traits carry button, header, selected, … |
| actions / custom actions | `custom_actions` (`ax` backend) | ✘ | WDA-style read cost (ios.md §5.3) |
| collection / range | ✘ | ✘ | gap: `list 4/37` hints cannot come from iOS AX |
| window | `windowDisplayID`, pid, `is_remote` | `displayID`, `windowContextID` | |

### 3.3 CLI commands and settle/verify

| droidctl | idb | Maestro | Verdict |
|---|---|---|---|
| `snapshot` (pruned, refs) | `ui describe-all --format complete` (raw) | `hierarchy` (raw) | host pruning carries over unchanged |
| `tap REF` (node action, typed errors) | `ui tap MARKER` (first match, position guess) | `tapOn` selector → centre coordinate | **keep droidctl's resolver**; send the point and the label assertion to the guest the way idb does |
| `type` | `ui set-value`, `ui text` | `inputText` | AX setValue + read-back |
| `scroll`, `scroll-to` | `ui scroll up/down/left/right/visible` | swipe | AX scroll-by-page is a real node action |
| `swipe`, `gesture` | `ui swipe`, `ui drag-and-drop` (HID) | `swipe` | HID (Sim) or the runner |
| `shot --marks` | `screenshot` | `takeScreenshot` | scale points to px |
| settle on every action | ✘ (caller polls) | screenshot hash, then hierarchy equality, up to 3 s | **idb quiescence + droidctl diff** |
| `--expect-change`, no double tap | `writeUnconfirmed(idempotent:)` | opt-in `retryIfNoChange` (double tap) | keep droidctl's policy |
| `doctor` / side-effect disclosure | `automation {enabled, asserted}` in `complete` | ✘ | report `AutomationEnabled` in `doctor` |

---

## 4. Reusable pieces

| piece | file | license | verdict |
|---|---|---|---|
| In-guest AX runtime: dlopen list, `userTestingSnapshotForElement` call, hit-test, perform/setValue, write outcomes | `idb/SimulatorFrameworkBridge/Runtime/AccessibilityRuntime.{h,m}`, `Runtime/Private/*.h` | MIT | **Port** (attribution). The core of a simulator backend |
| Private-selector signature check | `AccessibilityRuntime.m:503-622` | MIT | **Port** the idea: name drift instead of crashing |
| Write = point + assertion, re-read once on `elementMoved`, `writeUnconfirmed` | `AccessibilityService.swift:1148-1291`; `AXBridgeUIAutomation.swift:583-680` | MIT | **Port**. Matches `stale-ref` / `maybe_performed` |
| Quiescence tracker (busy 20 ms, quiet 250 ms) and the two AX action ids | `Swift/AccessibilityQuiescence.swift`; `AXRuntimePrivate.h:86-96` | MIT | **Port**. It becomes `wait_idle` and `act … settle` |
| Guest spawn by host path, socket in the host temp dir, shared/exclusive adoption | `FBSimulatorControl/SimulatorFrameworkBridge/*` | MIT | **Adapt**: `xcrun simctl spawn` + our socket (inferred equivalent of `SimDevice` spawn) |
| Indigo HID message building | `FBSimulatorControl/HID/SimulatorIndigoHID*.swift`, `PrivateHeaders/SimulatorApp/Indigo.h` | MIT | **Adapt only if needed** for swipes on Xcode ≤26; DTUHID for 27 |
| `--format complete` document shape (`truncated`, `automation`, `modal`, `profile`) | `FBSimulatorAX/README.md:166-201` | MIT | **Adapt** into our raw `tree` schema |
| Snapshot walk (no `dictionaryRepresentation`), cross-process offsets, orientation guard | `maestro/.../ViewHierarchyHandler.swift:306-403,68-112` | Apache-2.0 | **Port** with NOTICE |
| Recovery ladder (`kAXErrorIllegalArgument`/`InvalidUIElement`, window re-root, depth 60) | `ViewHierarchyHandler.swift:143-220,294-304`; `AXClientSwizzler.swift` | Apache-2.0 | **Port and fix** (child index, sticky global) |
| Foreground app via `activeApplications` + `_XCT_requestBundleIDForPID` | `Categories/XCUIApplication+Helper.m:40-69`; `RunningApp.swift` | Apache-2.0 (the file is Maestro's, with no Facebook header) | **Port** |
| Event synthesis: `XCSynthesizedEventRecord`/`XCPointerEventPath`/`_XCT_synthesizeEvent` | `Routes/XCTest/{EventRecord,PointerEventPath,RunnerDaemonProxy,EventTarget}.swift` | Apache-2.0 | **Port** for the device runner |
| XCTest-timeout classification → non-retryable | `SwipeRouteHandlerV2.swift:29-38` + tests | Apache-2.0 | **Adapt** into `ERROR_KINDS` (`timeout`, `busy`) |
| Quiescence-off swizzle, `snapshotKeyHonorModalViews` swizzle | `Categories/*FBQuiescence.m`, `*FBSnapshotReqParams.m` | **BSD-3 (WDA)** inside an Apache repo | **Ideas, or port with WDA's notice**; do not relabel as Apache |
| Screenshot-hash settle, keystroke typing without focus check, permission auto-tap, `retryIfNoChange` | `ScreenDiffHandler.swift`, `TextInputHelper.swift`, `SystemPermissionHelper.swift`, `Maestro.kt:404-440` | Apache-2.0 | **Avoid** |
| HTTP on loopback with no auth; idb's TCP `::` with no auth | `XCTestHTTPServer.swift:34`; `IDBPortsConfiguration.swift:22` | — | **Avoid**: token auth (ios.md §5.3) |

---

## 5. Verdicts

### (a) An idb-style simulator accessibility backend for droidctl, with no idb install: **worth building, behind one measuring spike.**

Why it is worth it:
- **It is the only path in this research to *node actions* on iOS:** AXPress, scroll-by-page and setValue. Everything else is synthesized touches.
- It needs **no test runner**: no `xcodebuild`, no runner app to install, no signing team, no 120 s startup budget.
- It has the cheapest read: about 30 ms for a whole tree (idb's figure).
- It has a real idle signal.

That is Backend A's design on iOS, for the simulator.

Why our own helper is feasible:
- **The helper is a single command-line binary**, not a framework that idb's build produces.
  - It links only Foundation, CoreGraphics and UIKit, and `dlopen`s the rest (`project.yml:29-52`).
  - It is ad-hoc signed. The entitlements idb embeds are for TCC and HealthKit, which we do not need (`project.yml:39-43`).
  - It reaches the host over a Unix socket on the shared filesystem.
- **The binding surface exists in the local iOS 26.2 runtime** (§1.5), x86_64 included, which this Intel Mac needs.
- **We would ship it like the APK:** prebuilt (for the simulator, x86_64 + arm64), committed with a source hash, and spawned with `xcrun simctl spawn <udid> <host path> serve --socket …` (inferred to be equivalent to idb's `SimDevice` spawn, `SimulatorFrameworkBridgeClient.swift:80-86`). The protocol would be droidctl's own NDJSON JSON-RPC `PROTOCOL.md` subset.
- **Size:** idb's whole AX path (the runtime `.m` plus the service `.swift`) is about 4,300 lines (`wc`), including features we would not take (displays, client types, settings, coverage grid). (Estimate: a droidctl subset of read, hit-test, perform, setValue and quiescence is about 1,000–1,500 lines.)

What it costs, and the risks:
1. **Private API, per runtime.** Mitigate with idb's signature check and a `doctor` line naming the drift.
2. **A device-wide side effect.** `AutomationEnabled` is asserted and left on (`README.md:316-322`), and the quiescence signals only answer in automation mode. On a throwaway simulator that is acceptable. `doctor` must disclose it, and `teardown` should restore the value we found (idb deliberately does not, `:318`; for us a teardown-time restore is cheap).
3. **No swipes or drags without host HID.** Scrolling is covered by AX scroll-by-page, and taps by AXPress. Free-form gestures need Indigo, which Xcode 27 drops for keys and buttons and makes unreliable for touch, or DTUHID. A droidctl fallback could use the device-style runner on the simulator for gestures only. **Decide by measurement.**
4. **Simulator only.** On a device, an unsigned binary cannot be spawned (inferred), and idb notes that the sandbox refuses the `AutomationEnabled` preference write there (`AccessibilityRuntime.m:1334-1336`, a comment). (Inferred: the device needs the XCUITest runner regardless.) So this is a second backend, not the iOS backend.
5. **Fidelity is not a superset.** The automation tree omits some elements that the VoiceOver tree has (`README.md:346-348`). Fixture parity against the runner's `snapshot()` tree decides which one we normalise to.

**Gate (spike, measured):**
- build a minimal helper (read + press);
- spawn it on the iOS 26.2 simulator;
- record cold and warm read latency on the test app;
- AXPress success on the test app's touch-only and silent-click scenarios, against `DTA` events;
- the `AutomationEnabled` before and after values;
- whether `simctl spawn` really is enough on x86_64.

**Fallback:** if any of these fails, simulators use the same runner as devices.

### (b) Maestro's runner design as a template for ours: **the outline yes, the code selectively, the contract no.**

**Keep:**
- one never-returning test method hosting a server, with `continueAfterFailure = true`;
- a prebuilt simulator bundle shipped in the package, so users never compile;
- `xcodebuild test-without-building` for launch, or a direct `.xctrunner` launch;
- the snapshot walk, the recovery ladder and the coordinate offsets;
- `activeApplications` for the foreground app;
- private event synthesis for taps and swipes, which is fast because it skips XCTest's quiescence wait;
- XCTest timeouts classified as non-retryable.

**Replace:**
- **HTTP on 22087 with no auth** → NDJSON JSON-RPC with a per-launch token and loopback/usbmux-only peers (agent-device's design, ios.md §5.3).
- **Coordinates only** → element-addressed actions: re-resolve on the device by identity, label and frame, then hit-test and assert at the point before the synthesized tap. Mirror idb's assertion, so a moved element fails with `stale-ref` instead of tapping whatever is now there.
- **Typing** → focus the field and verify it holds focus (`hasFocus` in the snapshot) before any keystrokes, then read the value back. Prefer AX setValue where the backend has it.
- **Settle by screenshot hash** → quiescence. Inside a runner this is XCTest's own `waitForQuiescenceIncludingAnimationsIdle:` with a short, measured timeout, the very method Maestro swizzles off. Then a tree diff on the host.
- **Permission auto-tap inside a read** → never: reads stay pure.
- **Retryable taps** (OkHttp re-send) → one attempt, then `connection` + `maybe_performed`.

**Do not inherit:** its device story. It has none: a `TODO` spike, and no port forward.

---

## 6. Lessons for droidctl

1. **On the simulator, iOS has a Backend A after all.** An unsigned helper inside the guest can call XCTest's own snapshot and real AX actions with no runner. Plan for **two iOS backends** (simulator AX helper, device XCUITest runner) behind one `PROTOCOL.md`, exactly like A and B on Android.
2. **Actions on iOS are "point + identity assertion", not handles.** Neither project keeps element handles across calls: idb hit-tests a point and asserts the label, and Maestro re-finds and taps a centre. Our resolver already re-resolves by fingerprint. The device side should **re-check the fingerprint at the point it is about to act on** and fail with `stale-ref` on a mismatch.
3. **`performed` is meaningless on iOS too:** "an ignored action reports as a success" (`AccessibilityRuntime.h:216-218`). Keep droidctl's rule: verify by diff, never tap again automatically, and report `handled`/`changed` honestly.
4. **Settle comes from the app, not from pixels.** Run-loop-idle plus animations-inactive, with a 20 ms busy threshold and a 250 ms quiet window, is both cheaper and more truthful than two PNG hashes. It is the same idea as droidctl's 150 ms event quiet window. Measure the iOS default the way M1/M8 did.
5. **Reachability is priced per node.** Never ask for `isVisible`/`visiblePoint` on a whole tree, which is 130x slower. Ask for the one element before acting, the way droidctl's `occluded` check runs only on the target.
6. **iOS trees are DAGs, not trees.** UIKit lists an element under two parents (`AccessibilityService.swift:280-283`). Our tree code must de-duplicate by identity before pruning, or refs will double.
7. **Disclose and restore device-wide state.** `AutomationEnabled` (idb) is the iOS counterpart of `enabled_accessibility_services`: `doctor` shows it, and `teardown` restores what we found. No permission auto-taps, no interruption-monitor surprises, no keyboard preferences.
8. **Typing without a focus check is the most common real-world bug on both platforms.** Maestro types after waiting at most 1 s for *any* keyboard. Port droidctl's "verified focus or no keys" rule to iOS from day one.
9. **Pin Xcode behaviour, not just versions.** Xcode 27 changes the HID transport (Indigo → `dtuhidd`) and the host app (Simulator.app → DeviceHub). Since Xcode 26, XCUITest symbols live in XCUIAutomation. Each private binding gets a startup self-check, as in idb's signature table, and a typed `unsupported-runtime` error.
10. **Neither project solves the physical device:** idb has no device UI at all, and Maestro's device driver is unfinished. The device runner (ios.md's "WDA-lite") remains our own work. These two repos supply its parts: Maestro's snapshot ladder and event synthesis, and idb's write semantics and quiescence logic. They do not supply its transport or its signing story.

---

## Corrections to `research/ios.md`

| ios.md says | the code says |
|---|---|
| §I6: axbridge is simulator-only because "the guest embeds private entitlements in `__TEXT,__entitlements` (`Privacy.md:27-30`)" | Those entitlements are TCC/HealthKit (`project.yml:39-43`; `SimulatorFrameworkBridge.entitlements`). The AX path uses none. Simulator-only follows from spawning an unsigned binary in the guest (inferred); idb notes that the sandbox refuses the `AutomationEnabled` write on a device (`AccessibilityRuntime.m:1334-1336`, a comment) |
| §I6: "idb verifies the bindings on iOS 26.4 and 27.0 (`Privacy.md:31-35`)" | That sentence covers the **TCC** bindings. The AX docs cite 26.4–27.0 for client-type values (`FBSimulatorAX/README.md:343`). Locally, the AX symbols exist on 26.2 (checked here) |
| §5.4: "idb is simulator-only" | Only AX, HID and REPL screenshots are (`IDBCommandExecutor.swift:136-259,631-635`). Device install, launch, screenshot (screenshotr), video, xctest and debugserver exist |
| §I6: press/scroll/set-value; "a drag is the only HID gesture" | True for axbridge, and custom actions are **refused** there (`AXBridgeUIAutomation.swift:534-542`). The host `ax` backend does custom actions (`AccessibilityUIAutomation.swift:113-137`). Actions target a **point plus label assertion**, not an element handle |
| §5.2: Maestro taps are "synthesized touches at x,y (`TouchRouteHandler.swift`)" | More precisely private `XCSynthesizedEventRecord` through `_XCT_synthesizeEvent:completion:` (`RunnerDaemonProxy.swift:36-50`), with XCTest's quiescence swizzled off (`XCUIApplicationProcess+FBQuiescence.m:14`; `FBConfiguration.m:15`) |
| §5.2: the recovery ladder handles `kAXErrorIllegalArgument` | Also `kAXErrorInvalidUIElement` (`ViewHierarchyHandler.swift:216-220`). It sets a **sticky** `maxDepth = 60` and has a descendant-for-child indexing slip (`:153-158`) |
| §5.2: settle is two screenshots with equal SHA-256 | That is the device half. The host repeats it for up to 3 s, then falls back to whole-hierarchy equality polling (`IOSDriver.kt:494-510,706`; `ScreenshotUtils.kt:38-74`) |
| TL;DR / §I3: the ~38 s cold start | agent-device's physical-device figure. There is no Maestro or idb simulator figure in either codebase. Measure it |
