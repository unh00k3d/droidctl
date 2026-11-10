# iOS support: can droidctl's design carry over?

Question: can droidctl gain iOS support built the same way as its Android support, and what would that take?

Sources, read 2026-10-16:
- **Shallow clones** under the session scratchpad (`…/scratchpad/ios-src/<repo>`). Paths below are relative to each repo root, and line numbers refer to these checkouts. I read the clones as data and ran none of their build scripts.

  | repo | commit | license |
  |---|---|---|
  | appium/WebDriverAgent | `e7e2665`, v16.15.0 | BSD-3 (Facebook) |
  | mobile-dev-inc/maestro | `3b068b8` | Apache-2.0 |
  | callstackincubator/agent-device | `610de02` | MIT |
  | facebook/idb | `e8b007e` | MIT |
  | mobile-next/mobile-mcp | `d4a83fa` | Apache-2.0 |
  | mobile-next/mobilecli | `9243a76` | **FSL-1.1** with an Apache-2.0 future grant after 2 years. That is a non-compete license: **ideas only.** |
  | doronz88/pymobiledevice3 | `29aadfe` | **GPL-3.0** |
  | droidrun/ios-portal | `c6f1cb7`, archived | **MIT**, unlike the AGPL Android Portal |

- **Apple's own SDK headers** from the Xcode installed on this Mac: `XCUIAutomation.framework/Headers/*.h` under `~/Downloads/Xcode.app/Contents/Developer/Platforms/iPhoneOS.platform/Developer/Library/Frameworks/`. They are cited as `XCUIAutomation/<file>:<line>`. Since Xcode 26, `XCTest.framework/Headers/XCUI*.h` are stubs that forward to this framework.
- **Apple developer pages**, App Review Guidelines and the local `xcrun devicectl` / `xcrun simctl` help, as cited inline.
- Anything I did not see in a source is marked **(inferred)**. Nothing here was measured on an iOS device. Every latency figure is cited from another project's measurement.

---

## TL;DR

**Feasible, but not "the same way".** The host side carries over almost entirely: daemon, CLI, MCP, snapshot pruning and formatting, refs, resolver tiers, diff verification, typed errors and spatial layout. The device side cannot be an accessibility service, because iOS has no third-party equivalent. It must be an **XCUITest runner**: a test bundle that Xcode, or `testmanagerd` reached by pymobiledevice3/go-ios, launches on the device, and that keeps a server open. That is how WebDriverAgent, Maestro, agent-device, droidrun's ios-portal and mobilecli all work (§5).

**Recommended architecture.**
- **Device side:** our own **minimal XCUITest runner ("WDA-lite")** in Swift with no dependencies. It speaks a subset of `PROTOCOL.md` (NDJSON JSON-RPC) on a device-loopback TCP port, with a per-launch token in place of peer-UID auth.
- **Transport:** reached over **usbmux**, the iproxy-style port forward, when cabled; the CoreDevice tunnel address as a fallback; `127.0.0.1` on a simulator.
- **How it reads the tree:** mostly through public `XCUIElement.snapshot()`. The private `XCAXClient_iOS` (the WDA path) is used only for what has no public answer: which app is in the foreground, snapshot depth limits, and (opt-in) traits, custom actions and per-element visibility.
- **Simulator second backend, measurement-gated:** an idb-style in-guest AX reader, which needs no test runner and supports AX press, scroll and set-value on nodes (§6).
- **Stock WDA:** do not reuse it as the backend. It silently turns off the user's keyboard autocorrection and prediction, and it serves an unauthenticated HTTP API on every interface (§5.1). It is fine as a throwaway M0 spike for capturing real fixtures, since Appium's WDA is already installed on this Mac.

**Top 3 blockers:**
1. **No public node actions.** In public XCTest, `tap` "sends a tap event to a hittable point computed for the element" (`XCUIAutomation/XCUIElement.h:136-139`). Every action is a synthesized touch or key event. There is no `ACTION_CLICK`, `SET_TEXT`, `SCROLL_FORWARD` or custom-action equivalent. Private `-[XCAXClient_iOS performAction:onElement:value:error:]` exists (`WebDriverAgent/PrivateHeaders/XCTest/XCAXClient_iOS.h:78`), but no device-side project in this set uses it, and whether it works from a runner on a real device is **unverified**. Until it is proven, "node actions, not coordinates" becomes "element-addressed synthesized touches at an XCTest-computed hit point".
2. **Setup friction and a Mac.** A physical device needs:
   - Developer Mode, which requires a reboot plus the passcode;
   - an Apple-signed runner (a free Personal Team gets 7-day profiles, 3 apps per device and 10 App IDs);
   - Xcode on macOS to build it;
   - a runner that dies with its host-side test session. It costs ~38 s to cold-start and ~1 s to re-adopt on a physical device (agent-device's measurements).

   No part of this is as quiet as `setup` enabling an accessibility service over adb.
3. **No event stream, slower reads, fragile snapshots.**
   - XCTest exposes no `AccessibilityEvent` push, so settle needs XCTest's private quiescence wait or polling.
   - A physical-device snapshot is ~350–450 ms steady-state (agent-device), against droidctl's 147 ms on Android.
   - Recursive snapshots fail on large trees (`kAXErrorIllegalArgument`, main-thread timeouts). Maestro, agent-device and WDA all carry recovery paths for this.

**Xcode on this Mac: present.**
- `xcode-select -p` gives `/Users/enesb/Downloads/Xcode.app/Contents/Developer`, and `xcodebuild -version` gives **Xcode 26.2 (17C52)**.
- The iOS 26.2 simulator runtime is installed, with 12+ simulators, all shut down.
- `xcrun devicectl list devices` shows two previously paired iPhones (iPhone 15, iPhone 12), both `unavailable`.
- `/usr/local/bin/iproxy` (libimobiledevice) is present, and so is Appium with `appium-xcuitest-driver` 10.16.2 (which bundles `appium-webdriveragent`).
- `idb`, `idb_companion`, `pymobiledevice3` and `go-ios` are absent.
- The host is macOS 15.7.9 on an Intel i7-8750H (x86_64). CLAUDE.md still describes the dev machine as a Debian KVM VM, so that section is out of date.

---

## I1. Reading the UI tree

### What public XCTest gives you
`XCUIElementAttributes` (`XCUIAutomation/XCUIElementAttributes.h:35-69`) is the entire public per-element vocabulary:
- `identifier` (the developer's `accessibilityIdentifier`), `frame` (screen points), `value` (id), `title`, `label`;
- `elementType` (an enum: Button, Cell, Switch, TextField, …), `enabled`, `horizontalSizeClass`/`verticalSizeClass`, `placeholderValue`, `selected`, `hasFocus` (not on macOS).

A whole tree comes from `-[XCUIElement snapshotWithError:]` (`XCUIElement.h:353-358`), which returns an `XCUIElementSnapshot` with `children` and `dictionaryRepresentation` (`XCUIElement.h:339-348`). Snapshotting an `XCUIApplication` gives one call per app, and the tree arrives in the runner process.

Against droidctl's raw node schema (PLAN.md "Device API"):

| droidctl raw field | public XCTest | private (WDA / idb) | verdict |
|---|---|---|---|
| `class` | `elementType` enum | — | **equivalent**, coarser (no Compose/View class names) |
| `id` (viewIdResourceName) | `identifier` | — | equivalent; often empty in third-party apps, as on Android |
| `uid` (API 33) | none | AX element token → WDA `fb_uid` (`XCUIElement+FBUID.m:27-31,72-74`) | private only; stability across reads unmeasured |
| `text` / `desc` | `label`, `value`, `title` | — | **adapt**: iOS has one label rather than text+desc; `value` holds field text, switch state "0"/"1", slider "50%" |
| `hint` | `placeholderValue` | — | equivalent |
| `bounds` | `frame` (points) | `visibleFrame` (Maestro reads it by KVC, `ViewHierarchyHandler.swift:405-426`) | **adapt**: points, not px; screenshot = points × scale |
| `visible` | none (only the `hittable` query, `XCUIElement.h:62-63`) | `isVisible` attribute, per element (WDA `XCUIElement+FBIsVisible.m:47-66`) | **gap**: costly (see traps); clip to frames on the host instead |
| flags `clickable/longClickable/checkable/scrollable/editable/password` | none; infer from `elementType` (Button, Switch, ScrollView/Table/CollectionView, TextField/SecureTextField/TextView) | `traits` bitmask (WDA `wdTraits`, `XCUIApplication+FBHelpers.m:287-289`; idb `traits`) | **adapt**: role from type and traits; no "clickable" bit exists |
| `checked` / `selected` / `focused` / `enabled` | `value` (switch), `selected`, `hasFocus`, `enabled` | — | equivalent |
| `heading` | none | header trait (private) | private only |
| `actions` (custom labels) | none | `customActions` attribute: WDA reads names only (`XCUIElement+FBCustomActions.m:39-43`); agent-device caps it at 12 elements per capture, 1 s each (`apple/runner/RUNNER_PROTOCOL.md:88-108`) | **private, read-only so far** |
| `collection {rows, cols}` / `range` | none (only realized cells are in the tree); slider `value` | min/max value (WDA `XCUIApplication+FBHelpers.m:301-309`) | **gap** for list size ("4/37 more↓"); host must infer |
| `window {type, layer}` / `drawingOrder` | the app's `windows` children; `keyboards`, `alerts` queries | `windowContextID`, `displayID` (Maestro KVC, `ViewHierarchyHandler.swift:320,354`) | **adapt**: no z-order. Keyboard and alerts come from queries |

### Which apps can be read: any app, if you know it
- **By bundle id:** `-[XCUIApplication initWithBundleIdentifier:]` returns "a proxy for an application associated with the specified bundle identifier" (`XCUIApplication.h:32-35`). Every runner uses it to read `com.apple.springboard` (home screen, system alerts) and other apps:
  - Maestro: `ViewHierarchyHandler.swift:9`, `RunningApp.swift:27-40`;
  - droidrun portal: `DroidrunPortalTools.swift:105,137-140`.
- **Not by "foreground":** public XCTest has no way to ask which app is in front. The options are:
  - Keep a list of bundle ids and poll `state == .runningForeground`. droidrun's portal hard-codes 19 Apple bundle ids (`DroidrunPortalTools.swift:107-127`), and a third-party app outside that list is "unknown foreground" (`:154-163`, `:173-201`).
  - Use the **private** `-[XCAXClient_iOS activeApplications]` (WDA `FBXCAXClientProxy.m:169-172`; `XCUIApplication+FBHelpers.m:533-621`). Maestro reaches it through `XCUIApplication.activeAppsInfo()` plus the private `_XCT_requestBundleIDForPID:` (`XCUIApplication+Helper.m:40-60`).
  - WDA also hit-tests a screen point (default 20% of the short side from the top-left) through private `_XCT_requestElementAtPoint:` to pick the on-screen app when several are "active" (`FBActiveAppDetectionPoint.m:26-37,50-82`).
- **System alerts** live in SpringBoard's tree. WDA reads them from `fb_systemApplication` (`FBAlert.m:433`), and Maestro auto-handles permission alerts (`ViewHierarchyHandler.swift:57`).
- **Cross-process content** (a WKWebView page, a share sheet, SafariViewService) is a separate process:
  - Maestro snapshots `com.apple.SafariViewService` separately on iOS 26+ (`ViewHierarchyHandler.swift:263-292`) and offsets frames across window contexts (`:360-381`).
  - agent-device refuses a simulator tree whose `AXRemoteElement` leaf covers the viewport (`docs/adr/0004-ios-snapshot-backend-strategy.md:45-57`).
- **Lesson:** the droidctl "all windows" read becomes the foreground app plus SpringBoard (status bar and alerts) plus the keyboard. That is close to `snapshot` / `--system`.

### Is there an accessibility service a third-party app could ship? No.
- **No public API (inferred):** the accessibility clients used here are all private frameworks. WDA uses `XCAXClient_iOS` in XCTest (`PrivateHeaders/XCTest/XCAXClient_iOS.h`). idb uses `AXRuntime`, `AccessibilityPlatformTranslation` and `XCTAutomationSupport` (`SimulatorFrameworkBridge/Runtime/AccessibilityRuntime.m:13-20`). pymobiledevice3 uses the `com.apple.accessibility.axAuditDaemon.remoteserver` developer service (`services/accessibilityaudit.py:277-302`).
- **Store rule:** an App Store app "may only use public APIs" (App Review Guideline 2.5.1).
- **What I found in the public SDK:** UIKit's accessibility API is the provider side, where an app describes itself. I found no public client API that lets an app read another app's tree. This is inferred from the absence of one in the SDK headers and the docs above; Apple does not state the negative in a single document.
- **Consequence:** there is no Backend A on iOS. The XCUITest runner plays the role of droidctl's **Backend B**. Like `app_process` + UiAutomation, it needs a developer trust boundary (a paired host plus Developer Mode) rather than an on-device setting, and it lives only as long as its launcher session.

### Speed and the known traps
- **Depth.** XCTest's default snapshot parameters are unbounded: `maxDepth`/`maxChildren` = INT_MAX (`XCAXClient_iOS+FBSnapshotReqParams.m:20-27`). WDA swizzles `defaultParameters` (`:51-61,161-177`) and sets `snapshotMaxDepth` to **50**: "50 should be enough for the majority of the cases. The performance is acceptable for values up to 100" (`FBConfiguration.m:355-356`). Maestro uses 60 and, past that depth, re-snapshots children one by one (`ViewHierarchyHandler.swift:10,150-160`).
- **Large or changing trees fail.** `kAXErrorIllegalArgument` ("snapshotting a too-large element") and `kAXErrorInvalidUIElement` (an element went stale mid-snapshot) are both treated as recoverable by Maestro, which restarts from the window child (`ViewHierarchyHandler.swift:171-220`). agent-device documents the same with Bluesky as the example, adding main-thread timeouts and the same shape on physical devices (ADR 0004 `:24-31`).
- **Visibility is the expensive key.** idb measured the same tree and the same 118 round trips with and without `isVisible`/`visiblePoint`: **~0.14 ms vs ~18 ms per round trip (16 ms vs 2148 ms total)**, about 130×, because the app has to hit-test every node (`FBSimulatorAX/README.md:253-262`). Asking about one element costs ~18 ms (`:268`). WDA's `visible` attribute is exactly this per-element fetch (`XCUIElement+FBIsVisible.m:62`), and its page source waits for app stability first, because "the snapshot retrieval operation might freeze and time out" (`FBXPath.m:682-688`).
- **Quiescence waits.** XCTest waits for the app to go idle before events and queries. WDA swizzles the private `-[XCUIApplicationProcess waitForQuiescenceIncludingAnimationsIdle:]` (`XCUIApplicationProcess+FBQuiescence.m:19-75`) and defaults to `waitForIdleTimeout` = 10 s and `animationCoolOffTimeout` = 2 s (`FBConfiguration.m:352-353`). Maestro disables XCTest's UI-interruption handling by swizzling `doesNotHandleUIInterruptions` (`XCUIApplication+Helper.m:11-38`). A never-idle screen (an animation, a ticker) stalls every call by the timeout.
- **Unresponsive apps hang snapshots.** WDA added an `accessibilityDeadline` that first pings the run loop through private `notifyWhenEventLoopIsIdleForApplication:` and aborts instead of hanging (`XCAXClient_iOS+FBSnapshotReqParams.m:93-153`; appium/WebDriverAgent#1210). It skips that check for WebKit services, because it crashed WebContent (`:112-118`, #1290).
- **`firstMatch` hides ambiguity.** It "stops evaluation … but also means that multiple matches will not be detected" (`XCUIAutomation/XCUIElementTypeQueryProvider.h:98-105`). WDA's `useFirstMatch` (default NO, `FBConfiguration.m:347`) trades exactly the uniqueness droidctl requires. Since droidctl resolves on the host, the runner never needs queries for resolution.
- **Measured numbers from others:**
  - **idb simulator:** a whole 167-element Settings tree in one call, ~30 ms idle (`FBSimulatorAX/README.md:92`); host-side per-element reads, 159 elements in ~99 ms (`:51`).
  - **agent-device on an iPhone 17 Pro (iOS 26.5.2):** steady-state snapshot **350–450 ms** (usbmux 440 ms); 4.5 s after a 40 s idle over the tunnel route (`docs/usbmux-runner-transport-experiment.md:15-27`).
  - **WDA:** no number in the repo.
- **Lessons for droidctl:**
  - Port the depth cap and the per-subtree recovery (Maestro), plus a run-loop deadline (WDA).
  - Never ask for visibility on the whole tree: clip with frames on the host and hit-test only the element about to be acted on (idb's two-call pattern, README `:270-282`).
  - Keep `firstMatch` out of resolution.
  - Report a sparse tree as degraded, not as "empty screen" (agent-device ADR 0004 `:87-89`). This matches droidctl's `degraded:true` and `diagnose`.

---

## I2. Acting

| droidctl action | iOS equivalent | node or coordinates? |
|---|---|---|
| `tap` (ACTION_CLICK) | `XCUIElement.tap` "sends a tap event to a hittable point computed for the element" (`XCUIElement.h:136-139`); WDA's click is literally `[element tap]` (`FBElementCommands.m:242-247`); runners that resolve on the host send a synthesized touch at x,y through the private `XCSynthesizedEventRecord` + testmanagerd (Maestro `TouchRouteHandler.swift:34-41`; WDA `FBXCTestDaemonsProxy.m:106-120`) | **coordinates**, element-addressed. Private AX `Press` (action id `0x7da`, idb `Runtime/Private/AXRuntimePrivate.h:85`) exists, but only idb's simulator guest uses it |
| long-press, double tap | `pressForDuration:`, `doubleTap` (`XCUIElement.h:144-152`) | coordinates |
| `type` (SET_TEXT → paste → `input text`) | `typeText:` needs keyboard focus on the element or a descendant "otherwise an error is raised" (`XCUIElement.h:103-110`); it types key by key through the on-screen keyboard. droidrun clears by repeating the delete key (`DroidrunPortalTools.swift:513-566`). Pasteboard: WDA `FBPasteboard`; simulator `simctl pbcopy` | **gap**: no set-text on a real device (idb's `set-value` is simulator-only, §6) |
| `--enter` | `typeKey:` / `XCUIKeyboardKey` | key event |
| `scroll` (SCROLL_FORWARD) | `swipeUp/Down/…` (`XCUIElement.h:181-206`); `scrollByDeltaX:deltaY:` is a pointer-device API (`:307`); WDA scrolls by swipes (`XCUIElement+FBScrolling.m`) | coordinates (AX ScrollByPage `0x7d6-0x7d9` exists privately: idb `AXRuntimePrivate.h:75-83`) |
| `scroll-to` | swipe + re-snapshot loop | same as Android's loop |
| `action "Archive"` (custom actions) | none public; names readable privately | **gap** |
| `set` (range) | `adjustToNormalizedSliderPosition:`, `adjustToPickerWheelValue:` (`XCUIElement.h:315-332`) | element-level, but synthesized drag (inferred) |
| `back` | no global back. droidrun taps `navigationBars.buttons[0]`, else edge-swipes (`DroidrunPortalTools.swift:658-677`) | heuristic; droidctl's `back` would be a typed `not-found` or an explicit `swipe --edge` |
| `home`, volume, action | `-[XCUIDevice pressButton:]` (`XCUIDevice.h:70`); Home = 1, volume up/down unavailable in the Simulator (`XCUIDeviceButton.h:25-33`); `hasHardwareButton:` iOS 16+ (`XCUIDevice.h:49`) | system |
| `launch` / stop | `launch` (terminates an existing instance), `activate` (does not), `terminate` (SIGKILL unless a debugger is attached) (`XCUIApplication.h:46-86`); `openURL:` iOS 16.4+ (`:63`, `XCUISystem.h:22`); host side `xcrun devicectl device process launch/terminate`, `simctl launch/terminate` | by bundle id |
| alerts | in SpringBoard's tree; WDA `/alert/accept` | tree + tap |
| `screenshot` | `XCUIScreen.mainScreen screenshot` (WDA, Maestro `ScreenDiffHandler.swift:16-17`); host side `pymobiledevice3 developer dvt screenshot` (`docs/guides/cli-recipes.md:339-340`), `simctl io screenshot`; `devicectl` has no screenshot subcommand (local `xcrun devicectl device --help`) | — |

**What the touch path changes about droidctl's tap policy:**
- **Occlusion:** a synthesized touch goes through hit-testing, like droidctl's `gesture` fallback, so a covering view receives it. XCTest checks `hittable` first. So iOS behaves like droidctl's `--method gesture`, never like `ACTION_CLICK`.
- **Double triggers:** the M5 lesson (`click_no_event` firing twice) does not arise, because there is only one method. The two-step "action, then gesture fallback" ladder collapses into one step.
- **Verification is weaker:** with no `clicked` event and no `performed:true`, verification is the diff alone. `handled:true` cannot be reported on iOS.

**Lessons for droidctl:**
- Map `tap REF` to a host-resolved frame plus a runner-side hit-test of that one element (`hittable`, or private `hitTestElement:withPoint:` at `XCAXClient_iOS.h:56`), then a synthesized touch. Report `method:"touch"`, and fail `occluded` when the hit-test returns another element.
- Make "AX press / AX scroll on device" a spike with a pass/fail result (Open questions Q1), not an assumption.

---

## I3. Transport: host ↔ device

**Simulator.**
- The runner binds `127.0.0.1` on the Mac, because simulators share the host's network stack. Examples: Maestro `127.0.0.1:22087` (`XCTestHTTPServer.swift:33-34`); agent-device binds `127.0.0.1` only (`RUNNER_PROTOCOL.md:13-19`); droidrun 6643–6652 (`DroidrunPortalServer.swift:15-42`).
- There is nothing to forward, and simulators run only on macOS.
- `simctl` launches, terminates, installs, sets the pasteboard, opens URLs, takes screenshots, and grants privacy permissions. It has **no** tree or tap subcommand (local `xcrun simctl help`).
- mobile-mcp starts an already-installed WDA with plain `simctl launch <udid> com.facebook.WebDriverAgentRunner.xctrunner` (`src/iphone-simulator.ts:48-50`).

**Physical device, cabled: usbmux.**
- usbmuxd multiplexes TCP to device-loopback ports over USB; `iproxy LOCAL DEVICE` and `pymobiledevice3 usbmux forward` expose one locally (`docs/guides/cli-recipes.md:19-20`).
- pymobiledevice3 can also open the device port directly through its `LockdownServiceProvider` "without requiring a local forwarder" (`services/wda.py:1-7`).
- agent-device measured usbmux against the CoreDevice tunnel IP (`docs/usbmux-runner-transport-experiment.md`):
  - steady state is a wash (~440 ms snapshots either way);
  - after a 40 s idle the tunnel route paid **4.5 s** to re-probe, while usbmux stayed at 440 ms (`:19-20,31-36`);
  - a Wi-Fi-only CoreDevice device is invisible to usbmuxd (`:40-46`);
  - so they ship usbmux first with a tunnel fallback (`:68-80`).

  This is the closest analogue of droidctl's persistent `adb forward`.

**iOS 17+ CoreDevice / RemoteXPC.**
- Developer services moved to RSD tunnels (`docs/guides/ios17-tunnels.md:3-4`).
- pymobiledevice3 builds a **no-root userspace tunnel in-process** for iOS 17.4+ over USB on macOS, Linux and Windows. iOS 17.0–17.3.1 needs a root `tunneld` on Linux/Windows (`:29-48,66-70`).
- mobile-mcp expects go-ios's tunnel on port 60105 for iOS ≥ 17, plus a separate WDA forward on 8100 (`src/ios.ts:8-9,62-93,105-109`).

**Starting the runner.**

| path | needs | used by |
|---|---|---|
| `xcodebuild test-without-building -xctestrun … -destination id=…` | macOS + Xcode | Maestro `XCRunnerCLIUtils.kt:113-126`, agent-device, droidrun `device.sh` |
| `xcrun devicectl device process launch --terminate-existing <xctrunner>` | macOS + Xcode | Maestro `LocalIOSDeviceController.kt:30-50` (Maestro 2.11.0 then **dropped physical iOS**: "Physical iOS devices are not yet supported", `CHANGELOG.md:11`) |
| testmanagerd over DTX, no Xcode | a prebuilt signed runner; pymobiledevice3 or go-ios | pymobiledevice3 `XCUITestService.run` (`cli/developer/wda.py:47-80`, waits up to 30 s for port 8100 over usbmux); mobilecli via go-ios `testmanagerd.RunTestWithConfig` (`devices/ios.go:663-685`, ideas only) |

**Does it need a Mac?**
- **Building and signing the runner:** yes. That takes Xcode and an Apple signing identity (inferred; Linux re-signers exist but I did not evaluate them).
- **Driving a prebuilt runner on a device from Linux:** possible. pymobiledevice3 runs on Linux (`README.md:12`), mounts the Developer Disk Image (`cli-recipes.md:284-300`), launches the XCTest runner, forwards ports and screenshots.
- **Simulators:** macOS only.
- **Licensing:** pymobiledevice3 is GPL-3, so droidctl (MIT) should call it as a subprocess or speak usbmux itself rather than import it (inferred license reading; confirm before shipping). The usbmuxd wire protocol is a small plist-over-unix-socket protocol, so a stdlib client is plausible (inferred).

**Lessons for droidctl:**
- One persistent usbmux connection per device, owned by the daemon, replaces `adb forward`.
- Tunnel routes need idle-aware health checks.
- macOS is the v1 iOS host. Linux support is a later "prebuilt runner + testmanagerd" path.

---

## I4. Signing and setup friction

| step | iOS | droidctl Android today |
|---|---|---|
| developer gate | **Developer Mode** (iOS/iPadOS 16+): Settings → Privacy & Security, a "reduces the security of your device" warning, a **restart**, then confirm with the passcode; it appears only after pairing with a Mac ([Apple: Enabling Developer Mode](https://developer.apple.com/documentation/xcode/enabling-developer-mode-on-a-device)). pymobiledevice3 can enable it remotely only if **no passcode** is set (`services/amfi.py:50-75`) | USB debugging, once |
| pairing | "Trust this computer" plus the passcode; CoreDevice pairing | adb RSA key |
| code signing | runner (`.xctrunner` + `-Runner.app`) signed with an Apple Development identity; **free Personal Team: 10 App IDs and 3 devices, both expiring after 7 days; 3 apps per device; "Provisioning profiles … will expire 7 days from issuance"** ([Apple: compare memberships](https://developer.apple.com/support/compare-memberships/)); paid program: no 7-day limit (inferred from the page contrasting tiers); free accounts hit "bundle identifier is not available" for generic ids (agent-device `website/docs/docs/installation.md:100-114`) | committed debug-signed APK, `install -r` |
| trust developer | Settings → General → VPN & Device Management → trust (Apple documents it for enterprise apps, [support.apple.com/118254](https://support.apple.com/118254); forum reports show the same gate for Personal Team builds) | none |
| "Enable UI Automation" toggle | under Settings → Developer; no Apple doc explains it, and a user reports automating iOS 15/16 without it ([forum 722521](https://developer.apple.com/forums/thread/722521)); treat as **unverified** | n/a |
| DDI | Developer Disk Image (Personalized DMG on 17–26, Cryptex on 27+) mounted by Xcode/devicectl or `pymobiledevice3 mounter auto-mount` (`cli-recipes.md:294-300`) | n/a |
| start time | physical cold start **~38 s** (36.9 s health check while the tunnel came up), re-adopting a live runner **~1.15 s** (agent-device `docs/evidence/ios-physical-runner-handoff-2026-09-19.md:95-100,150-154`); first open pays ~13 s xctestrun prep and ~6.7 s of `devicectl` listing (`usbmux-…experiment.md:25-27`); simulator cold median **4.3 s**, adopted **29 ms** (`…handoff….md:276-286`) | Backend A ping 16 ms; B launch ~0.2 s |
| persistence | the runner lives only while its test session lives: xcodebuild or the testmanagerd connection on the host (mobilecli clears its state when `RunTestWithConfig` returns, `devices/ios.go:663-685`); a locked phone wedges capture and relaunch (`usbmux-…experiment.md:54-56`); Auto-Lock must be Never during measurement (`:100`) | A: permanent, rebinds after reboot |
| failure taxonomy | agent-device's typed reasons: `signing_no_development_team`, `signing_provisioning_profile_missing`, `bundle_identifier_already_registered`, `devtools_security_developer_mode_disabled`, `device_developer_mode_disabled`, `device_developer_disk_image_unavailable`, `build_failed_unclassified` (`installation.md:115-116`) | `not-installed`, `suppressed`, … |

**Lessons for droidctl:**
- `setup` on iOS is a guided **doctor** more than an installer. It can check, explain and build/install the runner, but the user flips Developer Mode and trusts the profile.
- Port agent-device's reason taxonomy into `ERROR_KINDS` as `data.reason` under one kind, for example `not-installed` / `signing`.
- `teardown` = uninstall the runner app (`devicectl device uninstall`) and touch no setting.

---

## I5. Prior art in code

### 5.1 appium/WebDriverAgent (BSD-3)
**Source endpoint (`FBDebugCommands.m:25-79`).**
- `GET /source?format=xml|json|description` and `/wda/accessibleSource` both work with or without a session. The JSON form is `fb_tree`: one `fb_standardSnapshot`, then a recursive dictionary of `type, rawIdentifier, name, value, label, rect, customActions`, plus frame, enabled, visible, accessible, focused, traits, placeholder and min/max (`XCUIApplication+FBHelpers.m:185-312`).
- `accessibleSource` keeps only accessible elements and recurses only through inaccessible containers (`:314-349`). That is a ready-made "prune to labelled leaves".

**Snapshot.**
- `fb_takeSnapshot:` uses public `snapshotWithError:` or a "custom" unique query snapshot (`XCUIElement+FBUtilities.m:46-70`).
- XPath runs on that snapshot (`FBXPath.m:672-679`).
- v9.0.0 removed custom snapshotting and the `snapshotTimeout`/`customSnapshotTimeout` settings (`CHANGELOG.md:1113-1121`), so `customSnapshotTimeout` no longer exists. The live settings list is `FBSettings.m:11-45`.

**Private AX.**
- `XCUIDevice.sharedDevice.accessibilityInterface` → `XCAXClient_iOS`, which serves `requestSnapshotForElement:attributes:parameters:`, `activeApplications`, `systemApplication`, the idle/animation notifications and `attributesForElement:` (`FBXCAXClientProxy.m:63-210`).
- The headers also list `performAction:onElement:value:`, `setAttribute:value:element:`, `hitTestElement:withPoint:` and `addObserverForAXNotification:handler:` (`PrivateHeaders/XCTest/XCAXClient_iOS.h:56,76-83`). WDA uses none of them.

**Two "never break the device" problems.**
1. **Keyboard preferences.** At runner start, `+[UITestingUITests setUp]` calls `configureDefaultKeyboardPreferences` (`WebDriverAgentRunner/UITestingUITests.m:22-27`). That writes **Auto-Correction = NO and Predictive = NO** into the device's `TIPreferencesController` (`FBConfiguration.m:198-220`), and nothing restores them.
2. **Open, unauthenticated server.** The HTTP server binds the interface from `USE_IP`, else none set, so all interfaces (`FBWebServer.m:93-98`; `FBConfiguration.m:137-146`). It advertises the Wi-Fi IP (`FBWebServer.m:125`) and has no auth. Anyone on the LAN who can reach port 8100 could drive the phone (inferred from the code).

| WDA piece | verdict |
|---|---|
| private `activeApplications` + detection point | **Adapt** (the only way to name the foreground app) |
| depth cap via swizzled `defaultParameters` (50) | **Port** the idea |
| `accessibilityDeadline` run-loop ping | **Port** the idea |
| `accessibleSource` pruning | **Skip**: droidctl prunes on the host |
| W3C actions / WebDriver sessions / XPath / class chain | **Skip**: per-session overhead, host already resolves |
| keyboard prefs at startup, all-interface no-auth server | **Avoid** (non-negotiable) |
| as an M0 fixture-capture tool | **Use** (`/source?format=json`, already installed here via Appium) |

### 5.2 Maestro iOS driver (Apache-2.0)
- **Runner:** `maestro-ios-xctest-runner`, a FlyingFox HTTP server inside an XCTest test case (`maestro_driver_iosUITests.swift:26-30`) on `127.0.0.1:$PORT` or `22087` (`XCTestHTTPServer.swift:31-42`).
- **Hierarchy (`ViewHierarchyHandler.swift`):**
  - the foreground app comes from `activeAppsInfo()` filtered to `.runningForeground` (`RunningApp.swift:27-40`);
  - public `snapshot()` is walked directly rather than through `dictionaryRepresentation`, "which is O(subtree) per call" (`:306-358`);
  - it adds SpringBoard status bars and SafariViewService;
  - it offsets frames for landscape and cross-process windows (`:47-141,360-426`);
  - it recovers from `kAXErrorIllegalArgument` by re-rooting at the window (`:143-220`).
- **Taps:** synthesized touches at x,y (`TouchRouteHandler.swift`).
- **Settle:** "is the screen static" = two screenshots with equal SHA-256 (`ScreenDiffHandler.swift:16-21`).
- **Scope:** simulators only as of 2.11.0 (`CHANGELOG.md:11`).
- **Verdicts:**
  - **Port** the snapshot walk, the recovery ladder and the frame offsets (Apache, with NOTICE attribution).
  - **Skip** the screenshot-hash settle as the default: it costs two full screenshots per poll. It may be worth keeping as a fallback where quiescence is unavailable.

### 5.3 agent-device (MIT): the closest analogue
- **Runner:** its own XCUITest runner speaking a small `POST /command` HTTP+JSON protocol (`apple/runner/README.md:61-67`).
- **Auth and binding:**
  - a **per-launch 24-byte token** passed in the xctestrun environment, "which the app under test does not inherit", checked in constant time before routing; replies carry an auth header, and the host refuses runners without it (`RUNNER_PROTOCOL.md:25-38`);
  - simulators bind 127.0.0.1; devices accept only loopback (usbmux) or CoreDevice `utun` peers (`:15-23`, `RunnerListenerPeer.swift`).
- **Snapshots:**
  - XCTest recursive snapshots for physical devices;
  - a private host AX bridge for simulators, derived from idb's guest reader (`apple/snapshot-bridge/README.md`, `LICENSE.idb`);
  - one typed fallback per app generation (ADR 0004 `:37-43`);
  - "Physical devices should use an equivalent non-XCTest semantic backend only if Apple exposes a supported channel" (`:84-85`).
- **Recovery semantics:** status-before-invalidate and "no command is replayed after an observed mutating accepted/started/…" (`docs/ios-runner-protocol-optimizations.md:9-27,104-116`). This is droidctl's "`connection` + `maybe_performed`, never resend" rule, independently arrived at.

| piece | verdict |
|---|---|
| token auth + interface classification | **Port** (replaces peer UID) |
| usbmux-first, tunnel-fallback route resolver with non-retryable "not attached" | **Port** |
| degraded-snapshot verdicts, generation-scoped fallback circuit | **Adapt** |
| `customActions` read cap (12 elements, 1 s each, one in flight) | **Adapt** if we read custom actions |
| TypeScript daemon | **Skip** (we have ours) |

### 5.4 facebook/idb: FBSimulatorAX (MIT)
See §6. idb is simulator-only, and its axbridge is the only code here that performs **AX actions on nodes** (`FBSimulatorAX/AXBridgeUIAutomation.swift:519-556`).

### 5.5 mobile-mcp (Apache-2.0) and mobilecli (FSL-1.1: ideas only)
- **mobile-mcp iOS path:**
  - go-ios tunnel + WDA forward on 8100 + `GET /source?format=json` (`src/webdriver-agent.ts:274-284`);
  - keeps only seven element types and treats "visible" as `isVisible=="1" && x>=0 && y>=0` (`:236-263`);
  - coordinate taps through W3C actions.

  These are the failure modes droidctl already designs against (PLAN "Prior art").
- **mobilecli:** starts its own runner (`com.mobilenext.devicekit-iosUITests.xctrunner`, `devices/ios.go:41`) through go-ios testmanagerd without Xcode. It also injects an HTTP agent into the foreground app over **LLDB/debugserver** (`devices/ios_device_agent.go:22-37,138-179`). That works only for debuggable (`get-task-allow`) apps (inferred) and is far outside droidctl's model.
- **Verdicts:** **Skip** both as backends; **note** the no-Xcode testmanagerd launch as a proof that Linux driving is possible.

### 5.6 pymobiledevice3 (GPL-3)
Capabilities (`docs/guides/cli-recipes.md`):
- install and launch (`developer core-device launch-application`, `developer dvt launch`);
- screenshot (`developer dvt screenshot`);
- process control, syslog, DDI mount, Developer Mode toggle;
- XCUITest launch (`XCUITestService`, `services/dvt/testmanaged/xcuitest.py:94-237`);
- a WDA client that routes through lockdown with no local forwarder (`services/wda.py:1-7`).

**Accessibility audit service** (`services/accessibilityaudit.py`):
- `iter_elements` walks the foreground app by **moving the Accessibility Inspector focus** one element at a time, waiting for a `hostInspectorCurrentElementChanged:` event per element, and stops on a repeat (`:627-680`). It yields VoiceOver-style captions, not a tree with frames and types.
- `perform_press` sends `AXAction-2010` "Activate", which "can only be used for processes that carry the `task_for_pid-allow` entitlement" (`:508-565`).
- It is slow, flattened and visibly moves an inspector cursor (`set_show_visuals`, `:470`).

**Verdicts:**
- **Skip** DVT-only as a backend (option d).
- **Use as a subprocess** (GPL) for Linux runner launch, DDI mount and screenshots, if we go there.

### 5.7 droidrun ios-portal (MIT, archived, superseded by the closed mobilerun-ios)
- **Runner:** an XCUITest runner with FlyingFox HTTP on 6643–6652 (`DroidrunPortalServer.swift:15-42`).
- **Tree:** `XCUIApplication.debugDescription` with memory addresses regex-stripped and empty "Other" lines dropped (`AccessibilityTree.swift:10-42`). That is a text dump, not structured data.
- **Foreground app:** a hard-coded bundle list (above).
- **Taps:** coordinates relative to the app's origin (`DroidrunPortalTools.swift:471-496`).
- **Verdict:** **Skip**. It is the minimal-viable shape of option (a), and a good illustration of what *not* to parse.

---

## I6. Simulator-only shortcuts

**idb FBSimulatorAX reads and drives the foreground app's tree "without a test bundle"** (`FBSimulatorAX/README.md:3`), along two paths:
- **`--api ax` (host).** The private `AccessibilityPlatformTranslation` framework that Simulator.app uses, through the `com.apple.CoreSimulator.accessibility` service, at one XPC round trip per element (`:43-51`). It returns the VoiceOver-flattened tree: on a Settings screen, 14 elements one level deep against 152 elements 21 levels deep for the XCTest vocabulary (`:124`).
- **`--api axbridge` (guest).** idb's `SimulatorFrameworkBridge` is spawned *inside* the simulator and fetches the whole XCTest-vocabulary tree in **one** call (`:68-92`). It also performs **press / scroll / scroll-to-visible / set-value** as AX actions on elements (`AXBridgeUIAutomation.swift:519-556`; `Runtime/AccessibilityRuntime.h:164-172`). A drag is the only HID gesture (`:559-575`). Only the simulator makes this possible: the guest embeds private entitlements in `__TEXT,__entitlements` (`SimulatorFrameworkBridge/Privacy.md:27-30`; `SimulatorFrameworkBridge.entitlements`), which a device would not honour (inferred).

**Device-wide side effects.**
- axbridge **asserts `AutomationEnabled` in `com.apple.Accessibility` on every read and action and never restores it**. It is "a device-wide simulator setting and affects every accessibility consumer" (`:312-322`).
- Empty reads can mean `ApplicationAccessibilityEnabled` is off, which needs a relaunch (`:294-301`).
- Both are acceptable on a throwaway simulator but must be disclosed (droidctl `doctor`).

**Bounds and the requesting client.**
- Read bounds: depth 50, 3000 nodes, `truncated` reported (`:141-143`).
- Which tree an app returns depends on the *requesting client*: `xctest` gets the automation tree and `noClient` gets the VoiceOver tree. The automation tree "is not a superset" of the VoiceOver one (`:328-350`).

**Others.**
- **agent-device:** routes simulator snapshots through a derived bridge and falls back to XCTest (§5.3).
- **simctl:** gives no tree.
- **macOS AX on the Simulator window:** possible in principle, through Simulator.app's own translation (idb's `ax` path is that translation). It would need Accessibility permission for the host process on macOS (inferred; not tested).

**Lessons for droidctl:**
- A simulator backend with true node actions and ~30 ms whole-tree reads exists. It is the nearest thing to Backend A on iOS.
- It is still **private-API and per-runtime fragile**. idb verifies the bindings on specific runtimes, iOS 26.4 and 27.0 (`Privacy.md:31-35`).
- Gate it behind measurement and fixture parity with the XCTest runner.

---

## Mapping: droidctl concept → iOS

| droidctl concept | iOS equivalent | gap / risk |
|---|---|---|
| Backend A (a11y service APK) | **none** (no third-party AT API) | structural |
| Backend B (UiAutomation via `app_process`) | XCUITest runner launched by xcodebuild/devicectl/testmanagerd | the closest match; needs signing + Developer Mode; dies with the host session |
| peer-UID auth | per-launch token in the runner env + loopback/utun peer check (agent-device) | no peer credentials on a TCP loopback reached via usbmux (inferred) |
| `adb forward` + persistent socket | usbmux connection to a device port; tunnel IP fallback; 127.0.0.1 on simulator | idle tax on the tunnel route (4.5 s) |
| `ping` | runner `status`: iOS version, model, screen points + scale, foreground app | — |
| `tree` (all windows) | `snapshot()` of the foreground app (private `activeApplications`) + SpringBoard status bar/alerts + keyboard | foreground detection is private; cross-process content needs extra snapshots |
| handles + dump id | snapshot node path or AX element token (`fb_uid`) per dump | public API has no handle; re-query by path is fragile (inferred) |
| `act {handle, action}` | synthesized touch at a hittable point / `typeText` / swipe | **no node actions** in public API (Q1) |
| `gesture` | `XCSynthesizedEventRecord` via testmanagerd (private, used by all) | — |
| `global {back, home, …}` | Home/volume/action buttons; no back, recents or notifications global | `back` becomes a heuristic or an error |
| `wait_idle` / settle | private quiescence wait (WDA), or poll snapshot/screenshot equality (Maestro) | **no event stream**; settle costs polls |
| `events` / `subscribe` (toasts, window changes) | none public; private `addObserverForAXNotification:` (unverified) | event history between calls is lost, unless Q4 says otherwise |
| `wait_for` | runner-side polling of snapshots | slower than event-driven |
| `screenshot` | `XCUIScreen.screenshot` in the runner; `simctl io` / pymobiledevice3 on the host | coordinates: points vs pixels × scale |
| `current` | private active app + bundle id via `_XCT_requestBundleIDForPID:` | — |
| snapshot keep-rules (clickable/editable/scrollable flags) | `elementType` + private traits | role inference changes; fixtures needed |
| `list 4/37 more↓` (collection info) | none; only realized cells | scroll hints from frames only |
| resolver tier 1 (`uid`) | none public; AX token (private) | tier 1 likely absent |
| tiers 2–4 (attributes, locators, Artemis scoring) | identifier, label, value, type, frame | carry over unchanged (pure host code) |
| typed errors | same set; `occluded` from a hit-test; new `signing`/`developer-mode` reasons; `secure-window` semantics unverified | — |
| diff verification | same (snapshot before vs after) | the only signal (no clicked event) |
| `type` ladder (set_text → paste → `input text`) | `typeText` with focus; pasteboard + paste key; simulator AX set-value | no set-text on device |
| setup / teardown | doctor + build/sign/install runner; uninstall runner; never touch keyboard or AX settings | Developer Mode and trust are user actions |
| `screen-off` / locked | locked phone wedges capture/relaunch (agent-device) | detect and report |

---

## Device-side options

| option | pros | cons | latency (cited) |
|---|---|---|---|
| **(a) own minimal XCUITest runner, "WDA-lite", NDJSON JSON-RPC** | full control of the wire contract (`PROTOCOL.md` subset), payload shape and auth; public snapshot API plus a small, audited set of private calls; no device-setting side effects; works on simulators and devices | we own signing UX, launch lifecycle, snapshot recovery and per-iOS private-API breakage; a Swift test bundle to build per Xcode (inferred: rebuild needed when Xcode/XCTest changes) | device snapshot ~350–450 ms (agent-device, same architecture); sim adopted command 29 ms, cold ~4.3 s; device cold ~38 s / adopt ~1.15 s |
| **(b) reuse WebDriverAgent** | mature, many iOS versions, already installed here via Appium; JSON source in one call | WebDriver session overhead per call; changes keyboard prefs at start; open no-auth server on all interfaces; XML/XPath machinery we don't need; settings like `useFirstMatch` work against uniqueness | no number in repo; per-call session creation in mobile-mcp `withinSession` (`webdriver-agent.ts:71-76`) |
| **(c) idb / simulator-only AX** | no test runner; whole tree in one call; **AX press/scroll/set-value on nodes**; ~30 ms reads | simulator only; private API pinned per runtime; leaves `AutomationEnabled` on; not our code (MIT, could port with attribution) | 167 elements ~30 ms (axbridge), 159 elements ~99 ms (ax) |
| **(d) pymobiledevice3 DVT only** | no runner, no signing; works from Linux | tree = focus-walk captions (one event round trip per element), no types/frames; press only for `task_for_pid-allow` apps; GPL-3 | unmeasured; ≥1 s timeout per stall (`accessibilityaudit.py:650-656`) |

**Recommendation: (a), with (c) as a later simulator backend.**
1. **M0 (spike, no code shipped):** drive the installed WDA (`/source?format=json`) on a simulator and on one paired iPhone to capture real raw trees (fixtures from real devices). Then run droidctl's snapshot pruning on them offline to see what the Android-tuned keep-rules lose.
2. **M1:** WDA-lite with `ping/tree/act(touch|type|swipe)/global(home)/screenshot/current/wait_idle(poll)`, a token and usbmux. Answer Q1 (AX actions on device) and Q4 (AX notifications) in this milestone, because they decide whether `act` can be node-level and whether `events` exists.
3. **Later, if measured useful:** an idb-style simulator backend behind the same contract.

---

## Host: what is platform-neutral, what is Android-specific

Grounded in `droidctl/` (line counts from `wc -l`):

| module | lines | status |
|---|---|---|
| `core.py`, `client.py`, `commands.py` (registry), `cli.py` (surface) | 126 / 461 / 226 / 1200 | **neutral** framework. Android words live in help strings and a few commands: `press` "via adb", `install` "an APK" (`commands.py:147,214`) |
| `daemon.py` | 1165 | **mostly neutral**: per-device sessions, event subscription, cache. Device keyed by `serial`, env gate for `ANDROID_SERIAL`/`ANDROID_ADB_SERVER_PORT` (`daemon.py:161-183,249-325`) |
| `mcp.py` | 454 | **neutral** transport; Android in tool descriptions (`mcp.py:31,44,313`) |
| `spatial.py` | 295 | **neutral** geometry; resource-id name inference (`spatial.py:186-193`) is Android-flavoured but harmless |
| `resolve.py` | 748 | **algorithm neutral** (tiers, scoring, `carry_refs`); occlusion and "keyboard does not block" rules assume `ACTION_CLICK` bypasses touch (`resolve.py:39,468-528`): on iOS every tap is a touch, so the `occluded` policy flips to "anything over it is fatal" |
| `snapshot.py` | 1837 | **pipeline neutral**, inputs Android-shaped: `LIST_CLASSES` names (`:46`), InputType password detection (`:106-130`), Android 9 window and IME quirks (`:92,175,446,562`), resource-id list detection (`:498,572`), Switch textOn/off (`:216`). Needs a per-platform normalizer from iOS raw → droidctl raw (role, flags, password = SecureTextField, list = Table/CollectionView) |
| `act.py` | 1825 | **mixed**: action policy, settle, diff, readback are neutral; adb shell (`act.py:148-161`), text ladder's `input text` (`:984-1206`), `keyevent` (`:1254,1346`), logcat marks/logs (`:1447-1452,1608-1625`), `am`/`pm` (`:1526-1536`), APK install (`:1579`) are Android |
| `device.py` | 862 | **Android**: adb, forward, `enabled_accessibility_services`, Backend B launcher, screencap. `AgentClient` (`device.py:296-527`) is a generic NDJSON JSON-RPC client and could talk to WDA-lite unchanged |

**Shape of the change (inferred).**
- Introduce a platform seam at `device.py`: `android.py` (today's code) and `ios.py` (usbmux/simctl/devicectl, runner launch, token).
- Add a raw-tree normalizer, so that `snapshot.py`/`resolve.py` keep consuming one schema.
- Split `act.py` into neutral policy and per-platform primitives (`type`, `press`, `launch`, `logs`).
- The PLAN's "the contract is the seam" claim (Device backends) holds for the read path. The act path needs the iOS capability flags (`node_actions: false`, `events: false`) so the policy can choose touch-only and polling.

---

## "Never break the user's device" on iOS

- **Leave preferences alone.** Never write keyboard preferences (WDA does, §5.1), AX settings (`AutomationEnabled`, `ApplicationAccessibilityEnabled`, idb does on simulators, §6), Auto-Lock or Reduce Motion (WDA has `reduceMotion`, `FBSettings.m:25`). On a simulator, any such change is disclosed and reversible.
- **Leave Developer Mode alone.** Don't toggle it remotely: it reboots the phone, and pymobiledevice3 refuses when a passcode is set anyway. `doctor` explains it, and teardown leaves it as the user set it.
- **Don't expose the phone.** Bind device loopback (plus utun, for Wi-Fi) only, require a token on every request, and never serve on Wi-Fi interfaces (agent-device `RUNNER_PROTOCOL.md:15-38`).
- **Teardown removes only our runner app:** `devicectl device uninstall app` / `simctl uninstall`. It never removes other runners (WDA, Maestro), mirroring "never uninstall other tools".
- **Don't silently take over XCTest.** One test session at a time per device is likely (inferred; it parallels UiAutomation exclusivity). `doctor` must show an Appium/Maestro runner holding the device instead of killing it.
- **Don't repeat ambiguous mutations.** Never replay a mutation after an ambiguous transport failure (agent-device and droidctl agree).

---

## Open questions and the measurements that decide them

| # | question | decided by |
|---|---|---|
| Q1 | Can a runner on a **physical** device perform AX actions (`Press` 0x7da, `ScrollByPage` 0x7d6–9, custom actions) via `-[XCAXClient_iOS performAction:onElement:value:error:]` on third-party apps, or does it need `task_for_pid-allow` like the audit daemon? | spike in WDA-lite on one paired iPhone + the simulator; TESTAPP-style iOS test app with logged handlers (ground truth from the app's own log, not our output) |
| Q2 | Snapshot cost on device: public `snapshot()` of a typical screen, cold vs warm, at depth 50 vs unbounded; how often `kAXErrorIllegalArgument` fires on real apps | bench on device, 20+ screens across system and third-party apps |
| Q3 | Settle: private quiescence vs snapshot-equality polling vs screenshot hash, latency and false-settle rate | bench with an iOS port of the test app's animation/ticker scenarios |
| Q4 | Is there a usable event stream (`addObserverForAXNotification:` / `registerForAXNotificationsForApplicationWithPID:`) for toasts/window changes, or does `events` stay empty on iOS? | spike |
| Q5 | Handle stability: does the AX element token (`fb_uid`) stay the same across snapshots on one screen (resolver tier 1, fast path)? | repeated snapshots + scroll, compare tokens |
| Q6 | Points vs pixels: do frames, synthesized touch coordinates, screenshots and `--marks` agree (scale 2×/3×, landscape, iPad split view)? | on-device check, the iOS twin of the M1 resolution-override check |
| Q7 | Runner lifetime: how long does a runner live under xcodebuild vs testmanagerd when the host is idle, and after lock/unlock; can the daemon re-adopt it (agent-device: ~1.15 s)? | lifecycle bench |
| Q8 | Free Personal Team in practice: does the 7-day expiry break a user's flow silently, and can `doctor` read the profile's expiry? | test with a free Apple ID |
| Q9 | Linux host: prebuilt runner (signed on a Mac) launched by pymobiledevice3 subprocess or our own testmanagerd client, at what cost? Is subprocess use of GPL-3 pymobiledevice3 acceptable for MIT droidctl? | spike; license review |
| Q10 | Does the snapshot keep-rule set (tuned on Android fixtures) produce a 1–2k token snapshot on iOS trees (WDA JSON of Settings is ~150 nodes, idb `:124`)? | offline run on M0 fixtures |
| Q11 | Simulator AX backend (c): tree parity with the XCTest runner (automation vs VoiceOver tree, idb `:345-350`) and stability across iOS runtimes | fixture diff, two runtimes |
| Q12 | Is "Enable UI Automation" ever required on iOS 17+? | toggle off, run the runner on one device |

---

## Sources
- Apple SDK headers (Xcode 26.2, local): `XCUIAutomation.framework/Headers/XCUIElement.h`, `XCUIElementAttributes.h`, `XCUIApplication.h`, `XCUIDevice.h`, `XCUIDeviceButton.h`, `XCUISystem.h`, `XCUIScreen.h`, `XCUIElementTypeQueryProvider.h`, `XCUIElementQuery.h`.
- Apple: [Enabling Developer Mode on a device](https://developer.apple.com/documentation/xcode/enabling-developer-mode-on-a-device); [Compare memberships](https://developer.apple.com/support/compare-memberships/); [App Review Guidelines 2.5.1](https://developer.apple.com/app-store/review/guidelines/); [Install custom enterprise apps (trust flow)](https://support.apple.com/118254); [Developer Forums 722521: "Enable UI Automation"](https://developer.apple.com/forums/thread/722521).
- Local tools: `xcrun devicectl device --help`, `xcrun simctl help`, `xcodebuild -version`, `xcrun simctl list devices`, `xcrun devicectl list devices`.
- [appium/WebDriverAgent](https://github.com/appium/WebDriverAgent) `e7e2665` (BSD-3).
- [mobile-dev-inc/maestro](https://github.com/mobile-dev-inc/maestro) `3b068b8` (Apache-2.0).
- [callstackincubator/agent-device](https://github.com/callstackincubator/agent-device) `610de02` (MIT).
- [facebook/idb](https://github.com/facebook/idb) `e8b007e` (MIT): `FBSimulatorAX/README.md`, `SimulatorFrameworkBridge/`.
- [mobile-next/mobile-mcp](https://github.com/mobile-next/mobile-mcp) `d4a83fa` (Apache-2.0).
- [mobile-next/mobilecli](https://github.com/mobile-next/mobilecli) `9243a76` (FSL-1.1, ideas only).
- [doronz88/pymobiledevice3](https://github.com/doronz88/pymobiledevice3) `29aadfe` (GPL-3.0).
- [droidrun/ios-portal](https://github.com/droidrun/ios-portal) `c6f1cb7` (MIT, archived).
- droidctl: `PLAN.md` (Decisions, Architecture, Device API, Device backends, Performance, Snapshot, Resolution, Actions, Setup, Future work), `droidctl/*.py`.
