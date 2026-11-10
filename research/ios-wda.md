# appium/WebDriverAgent + appium-xcuitest-driver, read against droidctl

Question: what do WebDriverAgent (WDA) and the Appium XCUITest driver that drives it actually do, mechanism by mechanism, and what should an iOS droidctl take from them, adapt, or avoid?

This goes deeper than `research/ios.md` §5.1 and does not repeat it. Where §5.1 or the rest of `ios.md` is wrong or incomplete, the correction is marked **Correction to ios.md**.

## Sources (read 2026-10-16)

| what | version | path prefix used below | license |
|---|---|---|---|
| appium/WebDriverAgent, shallow clone | `e7e2665`, v16.15.0, committed 2026-10-12 | paths relative to the repo root (`WebDriverAgentLib/…`, `lib/…`) | Objective-C: **BSD-3** (Facebook, `LICENSE`). The npm package declares **Apache-2.0** (`package.json:16`), which I read as covering `lib/*.ts` (inferred). `WebDriverAgentLib/Utilities/LRUCache/*` carries its own Apache-2.0 header (`LRUCache.m:1-12`) |
| appium-xcuitest-driver, installed | 10.16.2 under `~/.appium/node_modules/appium-xcuitest-driver` | `xcuitest/lib/…` | Apache-2.0 (`package.json`) |
| appium-webdriveragent bundled with that driver | **11.1.0** under `xcuitest/node_modules/appium-webdriveragent` | `wda@11/lib/…` | as above |
| Apple frameworks in the local Xcode 26.2 | `…/iPhoneOS.platform/Developer/Library/{Frameworks,PrivateFrameworks}` | `strings` output, cited as `XCUIAutomation (strings)` | Apple |

- I read the clones as data and ran none of their build scripts. Nothing here was measured on a device. Anything not seen in code is marked **(inferred)**.
- **Version skew:** the installed driver bundles WDA **11.1.0**, five majors behind the v16.15.0 clone I read. Any "use the installed WDA to capture fixtures" plan runs 11.1.0 code. Most mechanisms below are old, but line numbers and some fixes (the JSON-source path, the alert fixes, the session-teardown rewrite) are v16-only.

---

## 1. Overview

**Purpose.** WDA is a W3C WebDriver server that runs inside an XCUITest runner on an iOS, tvOS or watchOS device or simulator (`README.md:10-14`). The XCUITest driver is the Appium (Node) side. It builds, signs, installs and launches WDA, forwards a port to it, creates a WebDriver session, and proxies most commands to it (`xcuitest/lib/driver.ts:1067-1240`).

**Process layout.**
```
host (macOS only, xcuitest README:"Only macOS is supported as the host platform")
  appium server ── xcuitest driver ──HTTP──> 127.0.0.1:<wdaLocalPort>   (Node net.Server, forwarder)
                                              │ one usbmux connection per accepted socket
                                              ▼
device  WebDriverAgentRunner-Runner.app (xctrunner, an XCTest UI test bundle host)
          -[UITestingUITests testRunner]  "never ending test"  (WebDriverAgentRunner/UITestingUITests.m:41-49)
            FBWebServer: custom HTTP/1.1 over Network.framework on :8100 (+ MJPEG on :9100)
            every route runs on the main queue, one at a time
            XCTest ─► testmanagerd / XCTRunnerDaemonSession ─► AX of the target app, event synthesis, screenshots
```
- **Runner entry:** `+[UITestingUITests setUp]` configures the process, including the keyboard preferences in §3.6 (`UITestingUITests.m:22-39`). `testRunner` starts the server and spins the main run loop until `/wda/shutdown` (`UITestingUITests.m:44-56`; `FBWebServer.m:71-74,287-298`).
- **Transport, host side:** for a real device, `DEVICE_CONNECTIONS_FACTORY.requestConnection` (`xcuitest/lib/driver.ts:1073-1078`) starts a Node `net.Server`. On every accepted local socket it opens a fresh usbmux connection with `utilities.connectPort(udid, devicePort)` from appium-ios-device (`xcuitest/lib/device/device-connections-factory.ts:33-41`). An `iproxy` fallback exists (`:227-233`). On a simulator, WDA is reached directly on `127.0.0.1`, because simulators share the host's network stack.
- **Protocol:** HTTP + JSON, W3C/JSONWP routes plus about 90 `/wda/*` extensions (`WebDriverAgentLib/Commands/*.m`, `routes` methods). Most routes need a session id. About 40 are registered `.withoutSession` too, among them `/source`, `/screenshot`, `/status`, `/wda/activeAppInfo`, `/wda/homescreen`, `/wda/lock` and `/wda/apps/launchUnattached` (for example `FBDebugCommands.m:26-29`, `FBCustomCommands.m:47-112`).

**Activity.** v16.15.0 was released 2026-10-12, and the changelog shows 7 releases between 2026-09-30 and 2026-10-12 (`CHANGELOG.md:1-38`). It is actively maintained by the Appium team. The shallow clone has one commit, so author statistics are not available.

**Platforms.**
- iOS/tvOS devices and simulators (`README.md:13`), with watchOS branches throughout (for example `XCUIElement+FBTyping.m:137-141`).
- Deployment target **iOS 15.0** (`IPHONEOS_DEPLOYMENT_TARGET = 15.0` ×4 in `WebDriverAgent.xcodeproj/project.pbxproj`).
- Version-gated features: `openURL` with the default app needs iOS 16.4+ (`FBXCTestDaemonsProxy.m:160-167`); simulated location needs 16.4+ (`:188-195`); the accessibility audit needs 17+ (`XCUIApplication+FBHelpers.m:476-482`); VoiceOver control needs an iOS 27+ runtime (`XCUIDevice+FBVoiceOver.m`, error string at the top).

**Requirements.**
- macOS + Xcode, to build and run `xcodebuild build-for-testing test-without-building -project … -scheme WebDriverAgentRunner -destination id=<udid>` (`lib/xcodebuild.ts:413-421,439-446`).
- **Real-device signing:** `DEVELOPMENT_TEAM=<xcodeOrgId> CODE_SIGN_IDENTITY=<xcodeSigningId>` (`:463-466`), an optional `PRODUCT_BUNDLE_IDENTIFIER` override for free teams (`:467-469`), and `-allowProvisioningUpdates -allowProvisioningDeviceRegistration` (`:424-427`).
- **Paths that need no Xcode:** a preinstalled, signed WDA (`usePreinstalledWDA`) or an already-running one (`webDriverAgentUrl`) (`xcuitest/lib/driver.ts:243`).

---

## 2. Feature matrix against droidctl

Verdicts: **Adopt** (take the mechanism as is), **Adapt** (take it with changes), **Ideas only**, **Avoid**, **Gap** (they have nothing usable).

### 2.1 Device API (PROTOCOL.md methods)

| droidctl | their equivalent | how it works | quality / limits | verdict |
|---|---|---|---|---|
| `ping` | `GET /status` (`FBSessionCommands.m:173-220`), `GET /health` (`FBWebServer.m:268-270`) | `/status` returns build time, bundle id, version, OS, SDK and testmanagerd version. The driver polls it every 1 s to detect startup (`lib/xcodebuild.ts:557-600`), and reuses a running WDA only if `productBundleIdentifier` and `upgradedAt` match (`lib/webdriveragent.ts:354-410`) | Good. But every route, `/status` included, is queued on the **main queue** (`FBWebServer.m:80`; `FBHTTPServer.m:714-716`), so a hung snapshot also blocks `ping` (inferred) | **Adapt**: version-gated reuse is our `versionCode` check. Serve `ping` off the main thread |
| `tree` | `GET /source?format=json\|xml\|description`, `/wda/accessibleSource` (`FBDebugCommands.m:40-79`) | one `snapshotWithError:` of the active app, then a recursive walk that computes attributes per node (§3.1) | **O(N) extra AX round trips**: `visible`, `accessible` and `customActions` are fetched per element (§3.1) | **Adapt**: one snapshot, standard attributes only |
| `act` | `POST /element/:uuid/click` → `[element tap]` (`FBElementCommands.m:242-255`) | public XCTest tap = a synthesized touch at the hit point XCTest computes | no node action exists anywhere in WDA (§3.2) | **Gap / spike**: see AX actions, §3.2 |
| `gen` | none. `XCElementSnapshot` has a private `generation` property (`WebDriverAgentLib/Routing/FBXCElementSnapshot.h:42`) that WDA never reads | — | its meaning is unknown | **Ideas only**: hash the pruned tree on the host |
| `gesture` | `POST /actions` (W3C) → `FBW3CActionsSynthesizer` → `XCSynthesizedEventRecord` (`FBTouchActionCommands.m:24`; `XCUIApplication+FBTouchAction.m:85-116`); `/wda/tap`, `/wda/touchAndHold`, `/wda/dragfromtoforduration`, `/wda/swipe`, `/wda/pinch`, `/wda/rotate`, `/wda/forceTouch` (`FBElementCommands.m:81-119`) | event records go to `-[XCUIDevice eventSynthesizer] synthesizeEvent:completion:` (`FBXCTestDaemonsProxy.m:106-131`). Drag resolves its points once and validates them (`XCUIApplication+FBTouchAction.m:118-166`) | solid, and it waits `animationCoolOffTimeout` (2 s) for stability afterwards (`:114,160`) | **Adopt** the event-record path; make the post-wait our settle |
| `global` | `/wda/homescreen` (`FBCustomCommands.m:119-127`); `/wda/pressButton` with home, volumeup, volumedown, action, camera (`XCUIDevice+FBHelpers.m:66-79`); `/wda/lock`, `/wda/unlock`, `/wda/locked` (`FBCustomCommands.m:50-55`); `/wda/siri/activate` | `XCUIDevice pressButton:` and private lock calls | there is no back, recents, notifications or quick settings on iOS | **Adopt** home and volume. `back` stays a typed error |
| `wait_idle` | not a route. Internally `fb_waitUntilStableWithTimeout:` (`XCUIElement+FBUtilities.m:138-159`), reachable through `POST /url {idleTimeoutMs}` (`FBSessionCommands.m:83-86`) | the private quiescence wait, with optional animations (§3.4) | blocks the main thread; a never-idle app costs the whole timeout | **Adapt**: expose it as `wait_idle` with our own timeout |
| `wait_for` | none on the device. Appium's implicit wait polls `find` on the host (`xcuitest/lib/commands/find.ts:185`) | polling | — | **Gap**: host polling with a cheap tree diff |
| `subscribe` / `events` | none. The alerts monitor polls every **2 s** (`FBAlertsMonitor.m:16,35-89`). The private headers declare `registerForAXNotificationsForApplicationWithPID:`, `addObserverForAXNotification:handler:` (`PrivateHeaders/XCTest/XCUIAccessibilityInterface-Protocol.h:47-50`) and an `axNotificationHandler` on `XCTRunnerDaemonSession` (`XCTRunnerDaemonSession.h:33`), all unused | — | — | **Spike**: AX notifications may give a real event stream (unverified) |
| `screenshot` | `GET /screenshot` → `_XCT_requestScreenshot:withReply:` on the daemon proxy (`FBScreenshot.m:94-130`); MJPEG stream on :9100 (`FBMjpegServer.m:196-216`) | the daemon captures the screen, then it is rescaled (`FBScreenshot.m:73-92`) | there is an element-screenshot route too (`FBElementCommands.m:71-73`) | **Adopt** the daemon path (crop on the host); **Avoid** the MJPEG listener (§3.7) |
| `current` | `GET /wda/activeAppInfo` → pid, bundleId, name, args (`FBCustomCommands.m:255-267`) | private `activeApplications` + a point hit-test (§3.3) | heuristic when several apps are "active" | **Adapt** |

### 2.2 Raw node schema

| droidctl field | WDA JSON key / source | notes | verdict |
|---|---|---|---|
| `id` | `rawIdentifier` = `identifier` (`XCUIApplication+FBHelpers.m:210`); `name` = identifier, else label (`XCUIElement+FBWebDriverAttributes.m:118-126`) | `name` **merges id and label**, so it is unusable as a fingerprint key | **Adopt** `rawIdentifier`; ignore `name` |
| `uid` | not in the tree. Element UUID = 16 bytes of `(AX elementID, pid)` (`WebDriverAgentLib/Routing/FBElementUtils.m:127-151`); elementID comes from `payload[@"uid.elementID"]` or `_elementID` (`:143-151`) | the closest analogue of `AccessibilityNodeInfo` identity. Stable while the app keeps that AX element (inferred). Unmeasured across reloads and cell reuse | **Adapt** as resolver tier 1, **gated on measurement** |
| `text` / `desc` | `label`, `value` (`XCUIApplication+FBHelpers.m:213-214`) | **trap:** `wdValue` substitutes the **placeholder** when a text field is empty (`XCUIElement+FBWebDriverAttributes.m:107-110`), the **label** for static text (`:99-101`), and "1" for a selected button (`:102-104`) | **Avoid** `wdValue`; read raw `value` |
| `hint` | `placeholderValue`, only for inner-text types (`XCUIApplication+FBHelpers.m:295-299`) | — | **Adopt** |
| `state` / `checked` / `selected` / `focused` / `enabled` | switch `value` coerced to bool (`…WebDriverAttributes.m:104-106`); `isEnabled`, `hasFocus` (`XCUIApplication+FBHelpers.m:272-286`); `hasKeyboardFocus` on the snapshot (`FBXCElementSnapshot.h:19`) | `hasKeyboardFocus` is what typing needs (`XCUIElement+FBTyping.m:67-74`) | **Adopt** (add `hasKeyboardFocus` to the raw schema) |
| `bounds` | `rect` = `CGRectIntegral(frame)`, with `-1,-1,0,0` for infinite frames (`…WebDriverAttributes.m:159-169`); `nativeFrame` raw (`:171-177`) | points, not pixels. Integral rounding loses half-points on @3x screens (inferred) | **Adapt**: send raw frames; scale on the host |
| `visible` | `isVisible`: per-element `attributesForElement:[XC_kAXXCAttributeIsVisible]`, with a shortcut when any descendant is already known visible (`XCUIElement+FBIsVisible.m:47-76`) | expensive (§3.1). The snapshot's own `visibleFrame` (`FBXCElementSnapshot.h:29`; used by `XCUIElement+FBVisibleFrame.m:25-35`) is computed without an extra fetch when it is non-empty | **Avoid** per-node visibility; **Adopt** `visibleFrame` plus host clipping |
| `flags` | `traits` bitmask as a string list (`…WebDriverAttributes.m:189-193`; `FBAccessibilityTraits.m`) | free: traits are part of the standard snapshot (`FBXCElementSnapshot.h:22`) | **Adopt**: role = elementType + traits |
| `actions` (custom) | `customActions`: names only, per-element fetch (`XCUIElement+FBCustomActions.m:39-60`), **unconditional** in the JSON tree (`XCUIApplication+FBHelpers.m:216`) | no route invokes a custom action | **Adapt**: fetch lazily, for one element |
| `collection` / `range` | `minValue` / `maxValue` for slider-like types (`XCUIApplication+FBHelpers.m:302-309`); `getVisibleCells` (`FBFindElementCommands.m:45`) | no total count, so no "4/37" | **Gap** |
| `window` | none. The snapshot has `isMainWindow` (`FBXCElementSnapshot.h:23`); multi-display uses `displayID` (`XCUIApplication+FBTouchAction.m:141-146`) | — | **Ideas only** |

### 2.3 CLI commands

| droidctl command | WDA / driver | how | verdict |
|---|---|---|---|
| `snapshot` (prune, compact) | `/wda/accessibleSource` keeps accessible elements and recurses only through inaccessible ones (`XCUIApplication+FBHelpers.m:314-349`); `excluded_attributes` on XML **and** JSON (`FBDebugCommands.m:47-61`). The driver documents it as "Only works if `format` is `xml`" (`xcuitest/lib/commands/source.ts:36`), but the WDA code applies it to JSON too | pruning runs on the device and costs per-node AX calls | **Ideas only**: we prune on the host |
| refs / locators | element UUIDs in a session LRU of **1024** XCUIElements (`FBElementCache.m:24`); locators: XPath (libxml2 over a snapshot, `FBXPath.m:248-345`), class chain, predicate, accessibility id (`FBFindElementCommands.m:41-49`) | an XCUIElement is a lazy query. Re-resolution binds it **by AX element** by default, or **by index** with `boundElementsByIndex` (`FBXCodeCompatibility.m:47-52`) | **Avoid** bound-by-index (it is position-guessing); **Adopt** "bound by AX element" as the identity tier |
| resolution / staleness | `elementForUUID:checkStaleness:YES` re-snapshots and throws `StaleElementReference` when the query no longer resolves (`FBElementCache.m:59-87`; `XCUIElement+FBUtilities.m:161-173`). `useFirstMatch` (default NO) skips uniqueness (`FBConfiguration.m:347`; `FBXCodeCompatibility.m:38-45`) | there is no ambiguity error: `fb_firstMatch` returns `allMatches.firstObject` (`FBXCodeCompatibility.m:44`) | **Avoid**: droidctl's typed `ambiguous` must stay |
| `tap` | `[element tap]` (`FBElementCommands.m:242-255`); coordinates via `/wda/tap {x,y}` (`:502-512,740-756`) | a synthesized touch (§3.2) | **Adapt** |
| `long-press`, `tap --double` | `pressForDuration:`, `doubleTap`, `tapWithNumberOfTaps:numberOfTouches:` (`FBElementCommands.m:290-343`) | touches | **Adopt** |
| `type` | `POST /element/:uuid/value` → tap the field if it has no keyboard focus, then **type into whatever has focus** (§3.2) | key events at 60 keys/s | **Adapt**: verify focus first |
| `type --clear` | IOHID Clear key, then N×(backspace + delete), then a triple tap + delete (`XCUIElement+FBTyping.m:189-223`) | a 3-step ladder that re-reads the value after each step | **Adopt** the ladder |
| `scroll` | `/wda/scroll {direction, distance}` → press-and-drag inside the scroll view (`FBElementCommands.m:403-417`; `XCUIElement+FBScrolling.m:392-460`) | touch drags | **Adopt** |
| `scroll-to` | `/wda/scroll {name\|predicateString\|toVisible}` → its own cell-walk loop, capped at 25 swipes (`XCUIElement+FBScrolling.m:154-300`, cap `:246`); `/wda/element/:uuid/scrollTo` → XCTest's private `-[XCUIElement scrollToVisible]` (`FBElementCommands.m:438-447`; `XCUIElement+FBScrolling.m:102-111`; `PrivateHeaders/XCTest/XCUIElement.h:160`) | `scrollToVisible` is an **AX action** inside XCTest (§3.2) | **Adapt**: `scrollToVisible` first, then a swipe loop |
| `swipe` | `/wda/swipe` → `swipeUp/Down/Left/Right[WithVelocity]` (`FBElementCommands.m:480-500`) | — | **Adopt** |
| `set` (range) | `/element/:uuid/value` on a slider → `adjustToNormalizedSliderPosition:` (0..1); on a picker wheel → `adjustToPickerWheelValue:` (`FBElementCommands.m:211-225`); `/wda/pickerwheel/:uuid/select` | XCTest element-level APIs | **Adopt** |
| `focus` | `/wda/element/:uuid/focuse` (tvOS focus engine, `FBElementCommands.m:78`) | tvOS only | **Gap** on iOS (tap is the focus) |
| `back` / `home` / `press KEY` | no back; home and volume as in §2.1; keys via `/wda/keys` (keyboard focus) and `/wda/element/:uuid/keyboardInput` with `XCUIKeyboardKey*` names (`FBKeyboard.m:60-119`); raw HID via `/wda/performIoHidEvent` | — | **Adopt** keys; `back` = typed `not-found` + hint |
| `wait --text/--id/--gone` | host-side implicit wait (find polling) | — | **Gap** |
| `wait --idle` | quiescence (§3.4) | — | **Adapt** |
| `watch` / `logs` | the driver captures syslog and crash logs on the host (`xcuitest/lib/commands/log.ts:121-140`, `device/log/*`) | appium-ios-device / py-ios-device | **Ideas only** (we would need our own syslog relay) |
| `launch` / `stop-app` / `apps` / `install` / `open-url` | `/wda/apps/launch\|activate\|terminate\|state\|list` (`FBSessionCommands.m:45-49`); `/wda/apps/launchUnattached`; `POST /url` → `openDefaultApplicationForURL:` (16.4+) **or a Siri fallback** "Open {url}" (`XCUIDevice+FBHelpers.m:242-271`); install and remove through the driver (`xcuitest/lib/commands/app-management.ts:28-124`) | `XCUIApplication` launch/activate/terminate | **Adopt** launch/activate/terminate/state; **Avoid** the Siri fallback |
| `shot --marks` | none | — | ours |
| `setup` / `teardown` / `doctor` | the driver's doctor checks the host only: Xcode, CLT, env vars, applesimutils, ffmpeg (`xcuitest/lib/doctor/*.ts`). Teardown = kill xcodebuild or the WDA process (`lib/wda-strategies.ts:298-316`). On a failed start the driver **uninstalls WDA** and retries (`xcuitest/lib/driver.ts:1116-1131`) | — | **Ideas only**; our doctor must check the device (Developer Mode, trust, DDI) |
| `run` batching | none. The W3C `/actions` call batches gestures only | — | ours |
| `mcp` | none | — | ours |

### 2.4 Features they have that we don't, and that matter on iOS

| feature | where | how | verdict |
|---|---|---|---|
| **System alerts** (permission prompts, SpringBoard, Safari web alerts, the iOS 18+ limited-access prompt) | `FBAlert.m:427-452` checks SpringBoard, then the app, then `FB_LIMITED_ACCESS_PROMPT_BUNDLE_ID` (`XCUIApplication+FBAlert.m:26-30`) through targeted Alert/Sheet/ScrollView queries (`:99-120`); `/alert/text\|accept\|dismiss`, `/wda/alert/buttons` (`FBAlertViewCommands.m:24-31`) | **accept = the last button, dismiss = the first** for an Alert (reversed for a Sheet) (`FBAlert.m:257-284,319-330`). They already hit a wrong-control bug on iOS 17.5 location alerts (`:258-264`) | **Adapt**: alerts appear in the snapshot as a `dialog` region and are acted on by label; **Avoid** positional accept/dismiss |
| **Auto-accept/dismiss alerts** | `defaultAlertAction` session capability → `FBAlertsMonitor` (2 s poll) (`FBSession.m:256-275`) | polling | **Ideas only**: an opt-in `--auto-alert accept` that names the button it pressed |
| **Alert precedence over the app** | `activeApplication` returns SpringBoard when it shows an Alert, `SBTransientOverlayWindow` or `NotificationShortLookView` (`FBSession.m:360-375`) | — | **Adopt**: include the SpringBoard alert in `snapshot` by default |
| **App crash detection** | the tested app not running → `FBApplicationCrashedException` (`FBSession.m:377-380`) | — | **Adopt** as a `crash` event or error kind |
| Keyboard dismiss / is shown | `fb_dismissKeyboardWithKeyNames:` taps a named key, or the last key on iPad (`XCUIApplication+FBHelpers.m:389-455`); visibility = the first labelled key exists and is hittable (`FBKeyboard.m:25-56`) | heuristic | **Adapt**: `keyboard=shown` from `app.keyboards` in the snapshot |
| App state | `/wda/apps/state` → `XCUIApplicationState` | public | **Adopt** |
| Permissions | reset on real devices via `resetAuthorizationStatusForResource:` (`/wda/resetAppAuth`, `FBCustomCommands.m:84`; `xcuitest/lib/commands/permissions.ts:14-31`); get/set on a **simulator only** via applesimutils (`permissions.ts:33-75`) | — | **Adopt** reset (it changes app state, so the user asks for it explicitly) |
| Location | iOS 16.4+ `setSimulatedLocation:` through the XCTest daemon session (`FBXCTestDaemonsProxy.m:188-218`); older devices through a lockdown service (`xcuitest/lib/commands/location.ts:72-108`) | — | **Adopt**, opt-in, and clear it on teardown |
| Pasteboard | `/wda/setPasteboard`, `/wda/getPasteboard` (`FBCustomCommands.m:63-66`) | the runner's `UIPasteboard` | **Adapt**: Unicode or long text via paste, restoring the previous content (droidctl's Android rule) |
| Lock/unlock, orientation, rotation, appearance, battery, device info, multi-display, accessibility audit (17+), VoiceOver control (27+), notification expectations, Touch ID (simulator), video recording | `FBCustomCommands.m:47-112`, `FBOrientationCommands.m:42-51`, `FBVideoCommands.m:27-33` | — | case by case. **lock-state** (`/wda/locked`) maps to our `screen-off`/`locked` header and is worth taking |

---

## 3. The mechanisms that matter most

### 3.1 Fetching the hierarchy

**Path.**
- `/source?format=json` → `fb_tree:` → `fb_standardSnapshot` → **public** `-[XCUIElement snapshotWithError:]` (`XCUIApplication+FBHelpers.m:190-196`; `XCUIElement+FBUtilities.m:46-60`).
- The walk runs over `snapshot.children` (`XCUIApplication+FBHelpers.m:244-253`). **WDA never calls `dictionaryRepresentation`** (no occurrence in `WebDriverAgentLib/`), so it avoids the per-call O(subtree) copy that Maestro also avoids (ios.md §5.2).
- The snapshot request itself goes through private `-[XCAXClient_iOS requestSnapshotForElement:attributes:parameters:error:]`, which WDA swizzles (`XCAXClient_iOS+FBSnapshotReqParams.m:174-176`).

**Depth and child caps (`snapshotMaxDepth`).**
- XCTest's default parameters are unbounded: `maxDepth` and `maxChildren` = INT_MAX, with `traverseFromParentsToChildren` and `snapshotKeyHonorModalViews` (`XCAXClient_iOS+FBSnapshotReqParams.m:20-27`).
- WDA swizzles `-[XCAXClient_iOS defaultParameters]` to merge its own values (`:51-61,166-168`), and captures `-[XCTElementQuery snapshotParameters]` so query snapshots use the same (`:63-68,170-172`).
- Defaults: `maxDepth` **50**, `maxChildren` INT_MAX. The comment reads "performance is acceptable for values up to 100" (`FBConfiguration.m:355-357`). Both are runtime settings (`snapshotMaxDepth`, `snapshotMaxChildren`; `FBConfiguration.m:272-290`).
- A capped tree is silently truncated: nothing in WDA marks the cut. A droidctl runner must return `truncated:true` (inferred requirement).

**Per-node cost: the hidden O(N).** After the one snapshot, `dictionaryForElement:` computes, for **every** node:
- `visible` → `fb_isVisible` → `attributesForElement:[XC_kAXXCAttributeIsVisible]`, unless a descendant is already known visible (`XCUIElement+FBIsVisible.m:47-76`; fetch at `XCUIElement+FBWebDriverAttributes.m:195-198` via `FBXCElementSnapshotWrapper+Helpers.m:70-82`, which logs any fetch over 50 ms, `:24,78`);
- `accessible` → `fb_isAccessibilityElement` on the node and **each ancestor** (cached per snapshot after the first fetch) (`…WebDriverAttributes.m:210-240`; `XCUIElement+FBAccessibility.m:28-48`);
- `nativeAccessibilityElement`, which reads the same attribute (`XCUIApplication+FBHelpers.m:281-283`);
- `customActions` → `attributesForElement:[XC_kAXXCAttributeCustomActions]`, **always**, because this key is not in the excludable block map (`XCUIApplication+FBHelpers.m:216`; `XCUIElement+FBCustomActions.m:45-60`).

So the JSON source costs 1 snapshot + up to ~3N AX round trips. With `excluded_attributes=visible,accessible,nativeAccessibilityElement` it still costs N for `customActions`. The XML source excludes hittable, nativeFrame, min/max and customActions by default, but keeps `visible` and `accessible` (`FBXPath.m:418-450`).

idb's measurement (ios.md I1) of ~18 ms per visibility round trip applies per node here. That is my estimate for the source of the "WDA page source is slow" reputation (inferred; no number in either repo).

**Correction to ios.md §5.1:** it describes `/source?format=json` as "one `fb_standardSnapshot`, then a recursive dictionary". That is true, but the dictionary is built from per-element AX fetches, so it is not a cheap tree read and not a model for ours.

**Stability before reading.**
- The XML source and XPath lookups first call `waitUntilStableWithElement:`, i.e. quiescence with `animationCoolOffTimeout` (2 s). The comment reads "the snapshot retrieval operation might freeze and time out" (`FBXPath.m:207,271,682-689`).
- The **JSON source does not** call it (`XCUIApplication+FBHelpers.m:190-196`). **Correction to ios.md I1**, which says WDA's page source waits for stability: only the XML/XPath paths do. Whether public `snapshotWithError:` waits internally is not visible in the source (unknown).

**`kAXErrorIllegalArgument` recovery: none in WDA.**
- `fb_takeSnapshot:` turns any snapshot error into a `StaleElementReference` exception (`XCUIElement+FBUtilities.m:54-56,161-173`). There is no `IllegalArgument` or `-25xxx` handling anywhere in `WebDriverAgentLib/`. The only nod to it is a header comment about "Error copying attributes -25202" (`XCUIElement+FBUtilities.h:91`).
- Its two defences are the depth cap and the **`accessibilityDeadline`** guard. Before each snapshot, the guard sends the app the private idle-run-loop check (`notifyWhenEventLoopIsIdleForApplication:`) and aborts if the app does not answer within the deadline. It remembers unresponsive pids so XCTest's own retries fail fast (`XCAXClient_iOS+FBSnapshotReqParams.m:72-153`), and skips `com.apple.WebKit.*` because the check crashed WebContent (`:112-118`).
- **The deadline defaults to 0, i.e. off** (`FBConfiguration.m:354`; check at `XCAXClient_iOS+FBSnapshotReqParams.m:99-102`). It is enabled only by the session capability or setting (`FBSessionCommands.m:314-315`).
- **Correction to ios.md I1:** it lists WDA among the projects that "carry recovery paths" for too-large trees and implies the deadline is active. Both are wrong for stock WDA. Maestro's re-root-at-window recovery is the one to port.

**Timeouts.**
- `withAXTimeout:`, `withXPCRequestTimeout:` and `withApplicationStateTimeout:` save, set and restore XCTest's **process-global** timeouts under recursive locks (`FBXCAXClientProxy.m:21-149`). For example, the private `_XCTSetXPCRequestTimeout` (`:114-123`).
- These are the knobs for bounding a hung read without the deadline swizzle. Restoring them in `@finally` is the right shape: **Adopt**.

**Speed.** There are no latency numbers in either repo. The driver polls `/status` with a 1 s per-request timeout during startup only (`lib/xcodebuild.ts:572-585`).

### 3.2 How element actions are performed

**No private `performAction:` is used.**
- WDA's private headers declare `-[XCAXClient_iOS performAction:onElement:value:error:]`, `setAttribute:value:element:outError:` and `hitTestElement:withPoint:error:` (`PrivateHeaders/XCTest/XCAXClient_iOS.h:56,76,78`; `XCUIAccessibilityInterface-Protocol.h:31-36`).
- The runner→daemon protocol declares `_XCT_performAccessibilityAction:(int)action onElement:withValue:reply:` and `_XCT_setAttribute:value:element:reply:` (`PrivateHeaders/XCTest/XCTMessagingRole_CapabilityExchange-Protocol.h:43,47`).
- `grep` finds **no caller** of any of them in `WebDriverAgentLib/`. The only private AX "action-like" call WDA makes is the point hit-test `_XCT_requestElementAtPoint:` for active-app detection (`FBActiveAppDetectionPoint.m:71`).

**But Apple's own XCUIAutomation performs AX actions from the runner.** This is new evidence for ios.md's Q1. The local Xcode 26.2 binaries contain:
- `Failed to scroll to visible (by AX action) %@, error: %@`, `AXScrollToVisible_ErrorReported` / `_Recovered`, and both `performAction:onElement:value:error:` and `_XCT_performAccessibilityAction:onElement:withValue:reply:` (`XCUIAutomation (strings)`);
- `kAXScrollToVisibleAction`, `kAXBeginMonitoringIdleRunLoopAction`, `kAXDetectAnimationsNonActiveAction`, and parameterized attributes such as `XC_kAXXCParameterizedAttributeKeyboardKeyForString` (`XCTAutomationSupport (strings)`).

So XCTest's own scroll-to-visible and its idle and animation checks travel as AX actions from the runner through the daemon to the app. That is also why WDA's idle check could crash WebContent (`XCAXClient_iOS+FBSnapshotReqParams.m:112-114`).

This makes "AX Press / Increment / ScrollByPage from our runner on a real device" much more plausible than ios.md assumed. It remains **unverified**. The action ids for Press etc. are known only from idb's simulator headers (ios.md I2).

**Tap.**
- Element: `[element tap]`, public XCTest (`FBElementCommands.m:247`). XCTest resolves the query, waits for quiescence before the event (the swizzled `…isPreEvent:` variant, `XCUIApplicationProcess+FBQuiescence.m:39-54`), scrolls the element into view by AX action when needed (the XCUIAutomation strings above; inferred link), computes a hit point, and synthesizes a touch.
- Coordinates: `XCUICoordinate` offsets from an element or the app, then `tap`/`pressForDuration` (`FBElementCommands.m:740-756`).
- All roads end in `-[XCUIDevice eventSynthesizer] synthesizeEvent:` (`FBXCTestDaemonsProxy.m:106-131`).
- Verification: none. The route returns OK when the event was delivered (`FBElementCommands.m:254`).

**Typing.**
1. `fb_prepareForTextInputWithSnapshot:`: if neither the element nor a descendant has `hasKeyboardFocus`, **tap it**, then re-snapshot (`XCUIElement+FBTyping.m:81-99`). **It never checks that focus actually arrived.**
2. `FBTypeText` builds an `XCPointerEventPath initForTextInput`, calls `typeText:atOffset:typingSpeed:shouldRedact:`, and synthesizes it (`XCUIElement+FBTyping.m:27-40`). It is **not** `-[XCUIElement typeText:]`, except on watchOS (`:137-141`). The key events go to **whatever holds keyboard focus**, the same failure droidctl fixed on Android in its 2026-10-07 RCA ("`type REF` filled the wrong field", PLAN.md "Actions").
3. Speed: XCTest's `iOSMaximumTypingFrequency`, default **60 keys/s** (`FBConfiguration.m:73-78`). 100 characters take about 1.7 s (arithmetic).
4. Characters the current keyboard can't produce (emoji, other scripts): XCTest maps strings to keys through `XC_kAXXCParameterizedAttributeKeyboardKeyForString` (strings above, inferred). WDA has no paste fallback for typing.
5. Clear: IOHID `kHIDUsage_KeyboardClear` first (`XCUIElement+FBTyping.m:198-204`), then backspace+delete × the visual length (`:179-187,209`), and on the last retry a triple tap + delete (`:205-208`). It re-reads the value after each step and stops when only the placeholder is left (`:214-218`).

**Sliders and pickers:** public `adjustToNormalizedSliderPosition:` and `adjustToPickerWheelValue:` (`FBElementCommands.m:213-225`). These are element-level calls; underneath they are synthesized drags (inferred).

### 3.3 Finding the foreground app

`+fb_activeApplicationWithDefaultBundleId:` (`XCUIApplication+FBHelpers.m:548-621`):
1. Private `-[XCAXClient_iOS activeApplications]` returns AX elements, one per active app (`FBXCAXClientProxy.m:169-172`).
2. If several are active, hit-test a **detection point**: by default 20 % of the screen's short side from the top-left corner, via the private daemon call `_XCT_requestElementAtPoint:`, with a **0.3 s** reply timeout (`FBActiveAppDetectionPoint.m:26-37,50-82`). Pick the active app whose pid matches. The point is configurable (`setCoordinatesWithString:`, `:89-108`).
3. Bundle ids come from the daemon's `_XCT_requestBundleIDForPID:`, waiting **1 s** per pid (`XCUIApplication+FBHelpers.m:144-168`).
4. Fallbacks: the first active app that maps to an `XCUIApplication`, then SpringBoard (`:609-620`).

pid → `XCUIApplication` goes through `-[applicationProcessTracker monitoredApplicationWithProcessIdentifier:]`, cached and pruned when processes stop (`FBXCAXClientProxy.m:212-238`).

A session-level override (`defaultActiveApplication`) and alert precedence sit on top (`FBSession.m:351-384`). On multi-display devices the point lookup only works on the main display and returns nil otherwise (`FBActiveAppDetectionPoint.m:52-67`).

- **Quality:** good for the common case. The point heuristic can name the wrong app when a split view, Slide Over or a picture-in-picture window covers that point (inferred).
- **Lesson:** report the method used in `current` (`via: hit-test|recent|springboard`), as droidctl reports `method` for taps.

### 3.4 Quiescence and idle waits

- **Mechanism:** XCTest's private `-[XCUIApplicationProcess waitForQuiescenceIncludingAnimationsIdle:]`, or `…:isPreEvent:` on newer Xcode, is swizzled (`XCUIApplicationProcess+FBQuiescence.m:62-75`). The swizzle returns immediately ("Making it to believe it is idling") when the per-process flag `fb_shouldWaitForQuiescence` is NO or `waitForIdleTimeout` is 0. Otherwise it runs the original with XCTest's application-state timeout temporarily set to `waitForIdleTimeout` (`:22-54`).
- **Defaults:** `waitForIdleTimeout` = **10 s**; `animationCoolOffTimeout` = **2 s** (`FBConfiguration.m:352-353`). The per-process flag defaults to **YES** for every app, including ones the session did not launch (`XCUIApplicationProcess+FBQuiescence.m:83-90`). The session capability `shouldWaitForQuiescence` sets it at launch (`FBSessionCommands.m:365-366`).
- **Where it runs:** before every synthesized event (XCTest's pre-event wait); after W3C actions, drags and scrolls with the 2 s timeout (`XCUIApplication+FBTouchAction.m:114,160`; `XCUIElement+FBScrolling.m:265`); before the XML source and XPath (`FBXPath.m:682-689`).
- **Explicit waits:** `fb_waitUntilStableWithTimeout:` temporarily forces the flag on and the timeout to the given value, under `@synchronized(FBConfiguration)` (`XCUIElement+FBUtilities.m:138-159`).
- **Disabling it:** set the `waitForIdleTimeout` setting to 0, or the capability `shouldWaitForQuiescence:false`. XCTest's interruption monitor is disabled once per process by swizzling `doesNotHandleUIInterruptions` to YES (`XCUIApplication+FBUIInterruptions.m:16-29`, called from `UITestingUITests.m:27`).
- **Extra knob:** `eventLoopIdleDelaySec` sleeps inside `-[XCUIApplicationProcess setEventLoopHasIdled:]`, so XCTest sees "idle" later; the swizzle is installed on demand (`XCUIApplicationProcessDelay.m`; `FBSessionCommands.m:305-310`).
- **What it is underneath:** the idle and animation checks are AX actions sent to the app (`kAXBeginMonitoringIdleRunLoopAction`, `kAXDetectAnimationsNonActiveAction` in `XCTAutomationSupport (strings)`; `notifyWhenEventLoopIsIdleForApplication:` / `notifyWhenNoAnimationsAreActiveForApplication:` in `FBXCAXClientProxy.m:191-201`).
- **Lesson:** this is the closest thing iOS has to droidctl's event-driven settle. A ticker or Lottie screen never goes idle and costs the full timeout on every call. Our default must be a short cap (for example 150 ms quiet / 2 s max, mirroring Android) with "not idle" reported as data, never XCTest's 10 s silent stall.

### 3.5 Launching, keeping alive, reconnecting; startup time

**Launch paths (`lib/wda-strategies.ts:59-168`):**

| strategy | command | lifetime tied to |
|---|---|---|
| `real-device-xcodebuild` / `simulator` | `xcodebuild [build-for-testing] test-without-building -project WebDriverAgent.xcodeproj -scheme WebDriverAgentRunner -destination id=<udid> …`, with `USE_PORT`, `USE_IP`, `MJPEG_SERVER_PORT` and `WDA_PRODUCT_BUNDLE_IDENTIFIER` in the environment, a marker `APPIUM_XCODEBUILD_WDA_MARKER=1` on the command line, spawned detached (`lib/xcodebuild.ts:408-525`; marker `lib/constants.ts:22-28`) | the xcodebuild process (`quit` = SIGTERM, `lib/xcodebuild.ts:312`) |
| `real-device-preinstalled`, iOS ≥ 17 | `xcrun devicectl device process launch --terminate-existing --environment-variables {…} <bundle>.xctrunner` (`wda@11/lib/webdriveragent.ts:657-663,681-686`; `node-devicectl/lib/mixins/process.ts:32-59`) | **nothing on the host**: devicectl returns, and the driver then polls `/status` (`wda@11/lib/webdriveragent.ts:704-712`). Inferred: the runner survives the devicectl exit and lives until it is killed or the device reboots |
| `real-device-preinstalled`, iOS < 17 | appium-ios-device `Xctest` over the DTX testmanagerd protocol (`wda@11/lib/webdriveragent.ts:688-689`) | that client (`:342-346`) |
| `simulator` + preinstalled | `simctl launch --terminate-running-process <udid> <bundle>` with the environment (`wda@11/lib/webdriveragent.ts:692-701`) | the simulator |
| `existing-url` | none: use `webDriverAgentUrl` (`lib/wda-strategies.ts:59-76`) | not managed |

**Correction to ios.md I4** ("the runner lives only while its test session lives"): true for xcodebuild and the testmanagerd paths. Appium's iOS 17+ preinstalled path launches the `.xctrunner` as a plain process and holds nothing. It is the only path that gives a resident runner like droidctl's Backend B, and it is worth a spike. The code comment warns that it "has limitation about the WDA preinstalled package" (`wda@11/lib/webdriveragent.ts:682-684`, pointing at appium/appium#19206). I did not read that issue.

**Keep-alive.**
- `FBFailureProofTestCase` sets `continueAfterFailure = YES`, stops halting on received control, and swallows every recorded issue, so an XCTest assertion never ends the never-ending test (`FBFailureProofTestCase.m:17-73`).
- If no port in the range binds, the server calls `abort()` (`FBWebServer.m:115-123`).
- Sessions are killed and replaced under a generation counter. Pending requests of a killed session get an immediate "invalid session" (`FBSession.m:181-330`; `FBWebServer.m:177-192`).

**Reconnect and reuse.**
- `setupCaching` reuses a WDA that answers `/status` when its bundle id and `UPGRADE_TIMESTAMP` match the bundled version (`lib/webdriveragent.ts:354-410`).
- `cleanupObsoleteProcesses` kills other xcodebuild processes holding the port for a different udid (`lib/wda-strategies.ts:220-230`).
- The preinstalled path **kills WDA at every session start**, to start fresh (`xcuitest/lib/driver.ts:1692-1696`).
- `useNewWDA` uninstalls it first (`:1109-1112`). A failed xcodebuild start **uninstalls WDA** and throws, so the retry loop can rebuild it (`:1116-1131`).

**Startup time.**
- No measurement exists in either repo. Defaults: `wdaLaunchTimeout` **60 s** (`lib/webdriveragent.ts:29`); real devices try **1** time, simulators 2, with a 10 s interval (`xcuitest/lib/driver.ts:148-152`).
- Bug-shaped detail: `waitForStart` computes `retries = timeout / 500` but polls every **1000 ms**, so it can wait about 2× `wdaLaunchTimeout` (`lib/xcodebuild.ts:562-566`; inferred from the arithmetic).
- agent-device's numbers in ios.md I4 (~38 s cold, ~1.15 s re-adopt) remain the only measured ones.

### 3.6 Side effects on the user's device

| side effect | where | restored? |
|---|---|---|
| **Keyboard Auto-Correction = NO, Predictive = NO**, written through `TIPreferencesController` (`setValue:forPreferenceKey:` + `synchronizePreferences`), "Works for Simulator and Real devices" | `FBConfiguration.m:197-232`, at every runner start (`UITestingUITests.m:26`) | **No.** Nothing in WDA or the driver writes them back (no occurrence of either key in `xcuitest/lib`). Sessions can flip them via the `keyboardAutocorrection`/`keyboardPrediction` settings (`FBSettingsHandler.m:79-90`), which also persist |
| `DidShowGestureKeyboardIntroduction` = YES, and on iOS 13+ `DidShowContinuousPathIntroduction` = YES: permanently suppresses the keyboard tutorials | `FBConfiguration.m:222-229` | No (harmless, but it is still a write to the user's preferences) |
| Runner-local `NSUserDefaults`: `XCTDisableRemoteQueryEvaluation`, `DisableScreenshots`, `DisableDiagnosticScreenRecordings`, `XCTEmitOSLogs`, … | `FBConfiguration.m:82-121` | they live in the runner's own domain (inferred: `standardUserDefaults` of the runner app), so the device is unaffected |
| **`/wda/healthcheck` presses Home** | `XCUIDevice+FBHealthCheck.m:40-45`; route `FBSessionCommands.m:55,231-237` | it is an action, not a check: never call it as a probe |
| `openURL` without iOS 16.4 → **activates Siri** with "Open {url}" | `XCUIDevice+FBHelpers.m:262-265,285-299` | — |
| Reduce Motion | `setReduceMotionEnabled:`, which the code comment says does not work on real devices (`FBConfiguration.m:366-377`) | no |
| VoiceOver enable/disable (iOS 27+ runtime) | `FBCustomCommands.m:102-111,741-749` | no automatic restore |
| Simulated location | `FBXCTestDaemonsProxy.m:188-218`; the driver resets the location service only if `resetLocationService` is set (`xcuitest/lib/driver.ts:472-479`) | only on request |
| Simulator software keyboard forced on (`UIKeyboardImpl setAutomaticMinimizationEnabled:NO`) | `FBConfiguration.m:234-248`, applied unless the capability is false (`FBSessionCommands.m:317-320`) | simulator only, not restored |
| WDA app uninstalled and reinstalled on a failed start | `xcuitest/lib/driver.ts:1116-1131` | it touches only WDA |

**Conclusion (non-negotiable):** droidctl's runner must not call anything in `FBConfiguration`'s keyboard path. If keyboard state ever needs to change, `setup` records it and `teardown` restores it byte for byte, as with `enabled_accessibility_services`.

**Practical note for an M0 fixture-capture spike with Appium's WDA:** record Settings → General → Keyboard → Auto-Correction / Predictive on the test phone before the first run, and switch them back afterwards.

### 3.7 Security: auth and bind address

1. **No authentication** on any route. Session ids are UUIDs, but about 40 routes work `.withoutSession`, among them `/source`, `/screenshot`, `/wda/homescreen`, `/wda/lock`, `/wda/unlock`, `/wda/apps/launchUnattached`, `/wda/setPasteboard`, `/wda/getPasteboard` and `/wda/simulatedLocation` (`FBCustomCommands.m:47-111`; `FBDebugCommands.m:27-29`; `FBScreenshotCommands.m:21`). Creating a session needs no secret either (`FBSessionCommands.m:44`).
2. **The HTTP server binds all interfaces** unless `USE_IP` is set (`FBConfiguration.m:137-146`; `FBWebServer.m:93-98`; `FBTCPSocket.m:38-49`: `nw_listener_create_with_port` without a local endpoint). It advertises the Wi-Fi IP (`FBWebServer.m:125-126`). On a phone on a LAN, this is remote control of the phone for anyone who can reach port 8100 (inferred from the code).
3. **The MJPEG screen stream ignores `USE_IP` entirely.** `FBTCPSocket` for port 9100 is created without an interface (`FBWebServer.m:134-135`), so it always listens on all interfaces. Any client that sends a request line gets a live multipart JPEG of the screen (`FBMjpegServer.m:196-216`). This is new relative to ios.md.
4. **CORS is wide open:** `Access-Control-Allow-Origin: *` on every response (`FBWebServer.m:82-83`), an `OPTIONS /*` route that answers every preflight (`FBCustomCommands.m:112`), and request bodies parsed as JSON regardless of Content-Type (`FBWebServer.m:241`). A web page open in a browser on the developer's Mac could drive the phone through the Appium forward on `127.0.0.1:8100`. That is a drive-by attack from the browser (inferred from the code; not tested).
5. **The host-side forwarder also binds all interfaces:** `this.localServer.listen(this.localport)` with no host argument (`xcuitest/lib/device/device-connections-factory.ts:72`). A USB-only phone thereby becomes reachable from the Mac's LAN (inferred from Node's default bind semantics).
6. The request body limit is **1 GiB** by default (`FBConfiguration.m:29,163-174`).

**Lesson:** our runner binds device loopback only. It requires a per-launch token, generated by the host and passed in the launch environment, on every request, including the stream. It sends no CORS headers. The host forwarder binds `127.0.0.1` only.

---

## 4. Reusability verdict

- **Use as is: no.** It breaks two non-negotiables: it rewrites keyboard preferences without restoring them (§3.6), and it serves an unauthenticated, all-interface, CORS-open API (§3.7). The WebDriver session, element-cache and XPath model also duplicates what droidctl already does on the host. It is acceptable only as a throwaway M0 tool to capture real fixtures, with the keyboard settings recorded and restored by hand and the phone off untrusted Wi-Fi.
- **Fork: no.** About 300 Objective-C files, most of them WebDriver plumbing (routing, W3C actions, XPath/libxml2, class-chain parsing, tvOS/watchOS branches). We would need under 10 % of it and would inherit the side effects to strip out.
- **Borrow pieces: yes, under BSD-3.** Keep the copyright notice and license text in a NOTICE or a third-party file, and per file. Do not use the Facebook name for endorsement (`LICENSE`). Port to Swift where practical. Each piece below is small and isolated:

| piece | file:lines | use |
|---|---|---|
| depth/children cap by swizzling `defaultParameters` + `snapshotParameters` | `XCAXClient_iOS+FBSnapshotReqParams.m:20-68,161-177` | **Adapt**: default 50–60, report truncation |
| unresponsive-app deadline (idle-loop ping first, pid memo, WebKit skip) | `XCAXClient_iOS+FBSnapshotReqParams.m:72-153` | **Adapt**: **on by default** in ours (it is off in WDA) |
| save/set/restore of the global AX, XPC and app-state timeouts | `FBXCAXClientProxy.m:21-149` | **Adopt** |
| `activeApplications` + point hit-test + bundle id via `_XCT_requestBundleIDForPID:` | `FBXCAXClientProxy.m:169-172`; `FBActiveAppDetectionPoint.m:50-82`; `XCUIApplication+FBHelpers.m:144-168,548-621` | **Adapt** (`current`) |
| element identity = (AX elementID, pid) | `WebDriverAgentLib/Routing/FBElementUtils.m:127-151` | **Adapt** as `uid`, after measuring its stability |
| quiescence swizzle with a kill switch | `XCUIApplicationProcess+FBQuiescence.m:19-110` | **Adapt**: off by default, explicit `wait_idle` |
| disable XCTest's UI-interruption handling | `XCUIApplication+FBUIInterruptions.m:16-29` | **Adopt** (alerts must stay visible, not be auto-handled) |
| key-event typing and the 3-step clear ladder | `XCUIElement+FBTyping.m:27-40,163-231` | **Adapt**: add a focus check before sending keys |
| event synthesis via `eventSynthesizer`; `openURL:usingApplication:`; simulated location | `FBXCTestDaemonsProxy.m:106-218` | **Adopt** |
| screenshot via `_XCT_requestScreenshot:` | `FBScreenshot.m:94-130` | **Adopt** |
| never-ending test + failure-proof test case | `UITestingUITests.m:41-49`; `FBFailureProofTestCase.m` | **Adopt** the pattern |
| private headers | `PrivateHeaders/XCTest/*.h` (class-dump output) | **Adapt**: declare only the selectors we call, in our own header; avoid vendoring the class-dumped tree (its licensing is murky, inferred) |
| host launcher patterns: process marker, `/status` polling, version-gated reuse, kill obsolete port holders | `lib/xcodebuild.ts:481-525,557-600`; `lib/constants.ts:22-28`; `lib/webdriveragent.ts:354-410`; `lib/wda-strategies.ts:215-230` | **Ideas only** (Apache-2.0 TS; we write Python) |

**Avoid entirely:**
- `configureDefaultKeyboardPreferences` and the keyboard settings;
- the all-interface HTTP and MJPEG listeners, CORS and the unauthenticated routes;
- `/wda/healthcheck` (it presses Home);
- positional alert accept/dismiss;
- `boundElementsByIndex` and `useFirstMatch`;
- the Siri `openURL` fallback;
- `wdValue` placeholder substitution;
- per-node `visible`, `accessible` and `customActions` fetches in the tree;
- uninstalling the runner on a failed start without telling the user.

---

## 5. Lessons for droidctl

1. **Tree read = one public snapshot, walked by us, standard attributes only.**
   - Keep identifier, label, value, placeholder, frame, elementType, traits, enabled, selected, hasFocus, hasKeyboardFocus, visibleFrame and children. Visibility comes from host clipping plus `visibleFrame`.
   - Custom actions, the precise `isVisible` and min/max are lazy, per element, and only for the element about to be acted on.
   - This is the single biggest speed lever WDA leaves on the table (§3.1).
2. **Truncation and hangs are data, not silence.** Cap the depth at 50–60 and return `truncated:true`. Enable the idle-loop deadline by default. Port Maestro's re-root recovery, because WDA has none. Map failures to `degraded:true`.
3. **Put `ping` and the health check on their own thread.** WDA serializes every route on the main queue (§2.1), so a hung snapshot blocks liveness. The daemon must be able to tell "runner busy" from "runner dead".
4. **Spike AX actions before accepting touches.**
   - XCUIAutomation itself performs AX actions from the runner (scroll-to-visible, idle and animation monitors; §3.2). `_XCT_performAccessibilityAction:onElement:withValue:reply:` is in the runner→daemon protocol.
   - Test Press, Increment/Decrement, ScrollByPage and custom actions on a real device. If they work, droidctl's "node actions, not coordinates" survives on iOS, together with its double-trigger policy.
5. **Typing must verify focus before it sends keys.** WDA taps and types without checking that the tap gave the field focus (§3.2), the bug droidctl already fixed on Android. Re-read `hasKeyboardFocus` after the tap. If focus did not arrive, return `no-change` with the "tap it, then type" hint. Keep WDA's clear ladder.
6. **Quiescence is opt-in and short.** XCTest's 10 s pre-event wait is the iOS "settle" that never fails loudly. Make the default about 2 s with "not idle" reported, and expose `wait --idle`.
7. **Resident runner path.** Spike Appium's iOS 17+ `devicectl device process launch --terminate-existing` of a preinstalled `.xctrunner` (§3.5). If the runner survives without a host session, it is our Backend B equivalent: launch once, keep warm, relaunch on a failed ping. Gate reuse on a version in `ping`, like `setupCaching`.
8. **Security baseline:** loopback-only bind on the device, a per-launch token, no CORS, a `127.0.0.1`-only host forward, a body-size cap in MB rather than GB, and no unauthenticated screen stream (§3.7).
9. **Never touch user settings.** No keyboard-preference writes and no "health checks" that press Home. Location, VoiceOver and permission resets are explicit, opt-in commands that `teardown` undoes (§3.6).
10. **Alerts are first-class in the snapshot.** Read SpringBoard and the limited-access prompt app alongside the foreground app (`FBAlert.m:427-452`), render them as a `dialog` region, and act on them by label. Never by "last button" (§2.4).
11. **Take the iOS-only commands that agents need:** app state, crash detection, lock state, pasteboard (with restore), simulated location, permission reset, and keys by `XCUIKeyboardKey` name. Skip the rest of the WebDriver surface.
12. **Fixture capture now:** the installed Appium WDA is 11.1.0, not the v16 code read here. I checked the three facts that matter in its copy: JSON honours `excluded_attributes` (`wda@11/WebDriverAgentLib/Commands/FBDebugCommands.m`, the `handleGetSourceCommand:` body), `customActions` is unconditional (`wda@11/…/XCUIApplication+FBHelpers.m:207`), and the runner still sets the keyboard preferences (`wda@11/WebDriverAgentRunner/UITestingUITests.m:26`). So `/source?format=json&excluded_attributes=visible,accessible,nativeAccessibilityElement` is the cheapest full-fidelity dump it offers. Record and restore the keyboard settings around the run.
