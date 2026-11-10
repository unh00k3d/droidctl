# droidctl on iOS: implementation spec (real iPhone first)

_2026-10-21. Everything here comes from the spikes in `research/ios-decision.md` §8–§11 (code in `research/ios-spike/`) and the prior-art reports in `research/ios*.md`._
- **Measured** means measured by us, on the iPhone 12 (iOS 18.7.8) unless the iOS 26.2 simulator is named.
- **Estimate**, **inferred** and **unverified** are labelled as such.
- This document is the input for planning the integration. It does not replace `PLAN.md`: once milestones start, PLAN.md gets an iOS section with the decisions and the measured numbers, as it did for Android.

**Priority (user decision 2026-10-21): the real iPhone comes first.** The simulator is a secondary target: it's useful for CI and fast iteration, but no design decision may rest on simulator-only behaviour.

---

## 0. The answer in five lines
1. droidctl drives an iPhone through **our own XCUITest runner**, a small ObjC/Swift test bundle that never returns and serves droidctl's protocol over a loopback TCP port. The host reaches it over **usbmux**, the USB channel macOS already runs.
2. **Node actions exist on iOS.** AX actions (Press, scroll by page, increment, decrement, scroll-to-visible, set value) go from the runner through testmanagerd and take about 1 ms.
   - Standard UIKit controls refuse Press cleanly, with nothing run.
   - Those get exactly one synthesized touch, after a hit-test proves the touch lands on the intended element.
3. **Reading the screen:** the daemon snapshot takes 87 ms for Settings and 450 ms for a 1030-node screen. Element identity (`pid/elementID`) is stable while the app stays on one screen, so it can become resolver tier 1.
4. **Device rules:**
   - The runner needs a host-side `xcodebuild` session to live.
   - A free Apple ID works, but profiles last 7 days and need a Trust tap on the phone.
   - The runner must refuse to act while the phone is locked: AX actions otherwise reach apps behind the lock screen.
5. **Measured stability:** 0 errors over 30 minutes on the iPhone, with memory flat at 80 MB. Everything above `device.py` (snapshot, resolve, act policy, daemon, CLI, MCP) is reused, plus an iOS mapping layer.

---

## 1. Hardware and environment used

| item | value |
|---|---|
| host | Intel Mac, macOS 15.7.9 (Darwin 24.6) |
| Xcode | 26.2 at `~/Downloads/Xcode.app`. It isn't in `/Applications`, so set `DEVELOPER_DIR=~/Downloads/Xcode.app/Contents/Developer` for every `xcrun`/`xcodebuild`. `xcode-select -p` must not be trusted |
| phone | iPhone 12, iOS 18.7.8, 390×844 pt, scale 3 (1170×2532 px), Face ID, Developer Mode on, paired |
| phone IDs | two different IDs, both needed (values kept in the user's memory notes, not in the repo): the **hardware UDID** `00008101-…` (for `xcodebuild -destination id=` and usbmux) and the **CoreDevice identifier**, a UUID (for `devicectl --device`) |
| Apple account | free Apple ID (personal team). Team ID kept out of the repo (§17) |
| simulator | iPhone 17 and iPad (A16), iOS 26.2 |
| libimobiledevice | `iproxy` installed. The spikes used it; the product must not need it (§4) |
| other paired phone | an iPhone 15, shown as unavailable in `devicectl` |
| **not available** | an iOS 26 *device*. iOS 26 coverage is the simulator only (known limitation) |

---

## 2. Architecture

```
host (Python)                                                  iPhone
┌──────────────────────────────────────────────┐   USB        ┌───────────────────────────────────────┐
│ CLI / MCP → daemon → snapshot / resolve / act │  usbmuxd     │ dev.<prefix>.droidctl.runner.xctrunner│
│                │  (unchanged, platform-neutral)│ ───────────▶ │  XCUITest bundle, one test that never │
│        device.py: backend = "ios"             │  Connect to  │  returns; listens on 127.0.0.1:PORT   │
│        ios/transport.py  usbmuxd client       │  device port │  JSON-RPC (PROTOCOL.md), no auth      │
│        ios/runner.py     build/sign/install/  │              │  → testmanagerd (AX actions, snapshot,│
│                          start/adopt/stop     │              │    hit-test, sendString, launch)      │
│        ios/mapping.py    iOS tree → raw tree  │              │  → XCUIDevice.eventSynthesizer        │
│        ios/devices.py    devicectl / xcrun    │              │    (touches, swipes, pinch)           │
└──────────────────────────────────────────────┘              └───────────────────────────────────────┘
          │ xcodebuild test-without-building (detached session holder, owned by nobody)
```

**The seam is the same one Backend B used.** The host talks to a device that answers the `PROTOCOL.md` methods, and the iOS runner implements the subset iOS can honour (§8). That leaves two choices:
- **Where the iOS→raw mapping runs.** **Proposed: on the host** (`ios/mapping.py`), like the rest of droidctl's logic.
  - It can then be tested offline against captured fixtures.
  - Fixing it never needs a re-sign or reinstall, which is expensive on iOS (§5).
  - The runner sends a lean iOS tree; the host converts it into the raw schema.
- **What the runner still has to do itself:** it keeps the element map (handle → `XCAccessibilityElement`), the lock guard, the hit-test assertion and the fallback. Those need device-side objects or must happen in one round trip.

**Proposed host layout** (new files only; estimates of size are estimates):
```
droidctl/ios/__init__.py
droidctl/ios/transport.py   # usbmuxd plist client: ListDevices, Listen, Connect (port of agent-device's MIT TS client, ~300 lines)
droidctl/ios/devices.py     # devicectl/xcrun wrappers: list, install, uninstall, launch, copy-from, info; UDID <-> CoreDevice id
droidctl/ios/runner.py      # build-for-testing, signing, install, start detached session, ping/build-id check, adopt, stop
droidctl/ios/mapping.py     # PURE: runner tree -> droidctl raw tree (windows, classes, flags, actions, offscreen, dedupe)
droidctl/ios/profile.py     # read embedded.mobileprovision: team, expiry, devices (for doctor)
ios/runner/                 # Xcode project: DroidctlRunner UI-test target (+ an empty host app if Xcode needs one)
ios/testapp/                # iOS port of TESTAPP.md, logs DTA events (os_log + Documents/dta.log)
tests/fixtures/ios-device/  # REAL trees from the iPhone (two exist already)
tests/e2e/ios/              # e2e against the iPhone with DROIDCTL_IOS_UDID
```

---

## 3. Device prerequisites (what the user must have, in order)

**Supported (user decisions 2026-10-21):**
- **iOS 17 or later.** Tested on a device: 18.7.8 only; on the simulator: 26.2.
- **A free or a paid Apple team.** The free team is the documented path: 7-day profiles, a Trust tap, re-signing weekly. Paid-team behaviour is inferred, unverified.

| step | how we check it | measured failure when missing |
|---|---|---|
| 1. macOS with Xcode (any path) | `xcrun --find xcodebuild` with `DEVELOPER_DIR` | — |
| 2. iPhone paired and trusted with this Mac | usbmuxd `ListDevices` shows it; `devicectl list devices` state | the phone appears as "unavailable" (seen with the iPhone 15) |
| 3. Developer Mode on | `devicectl device info details` (`developerModeStatus`) | — (it was already on) |
| 4. Apple ID added in Xcode, team chosen once | `xcodebuild … -allowProvisioningUpdates` signs | without a team: signing errors (needed one GUI step in Xcode) |
| 5. Phone **unlocked** during install and start | — | `xcodebuild` sat on "Unlock iPhone to Continue" forever and didn't notice the unlock; it had to be killed and restarted |
| 6. Developer **trusted** on the phone (free team) | a launch attempt (§5.6) | "Unable to launch … its profile has not been explicitly trusted by the user" (BSErrorCodeDescription = Security); before the install: "Developer App Certificate is not trusted" |

- **`setup` must guide each of these with a typed error and the exact fix**, for example: Settings → General → VPN & Device Management → *the Apple ID* → Trust.
- It must not retry blindly.

---

## 4. Transport

| path | listener on the phone | measured |
|---|---|---|
| **usbmux** (`iproxy` in the spike; our own client in the product) | `127.0.0.1` | **works, ~3 ms RTT** |
| usbmux | `0.0.0.0` | works |
| CoreDevice tunnel (`fd13:56:e7ef::1`) | `127.0.0.1` or `0.0.0.0` | **refused** |
| CoreDevice tunnel | `::` (IPv6 any) | works, 2.8 ms |

**Decisions:**
- **The runner binds loopback only**, so it is never reachable over Wi-Fi, unlike WDA and agent-device, which bind every interface. This is the iOS form of "no INTERNET permission".
- **The host speaks usbmuxd itself:** plist messages over `/var/run/usbmuxd`, with `ListDevices`, `Listen` and `Connect(DeviceID, port)`.
  - No `iproxy` and no libimobiledevice dependency.
  - Port agent-device's `runner-usbmux-protocol.ts` (MIT, 333 lines) to Python stdlib, with NOTICE attribution.
  - The port number in `Connect` is big-endian.
- **One persistent connection per device in the daemon**, as on Android. agent-device found that every host round trip also costs a usbmux connect, so the connection must not be per call.
- **The CoreDevice tunnel is a fallback** only, and only if the runner also listens on IPv6 bound to the tunnel address rather than `::`. Not needed for v1.
- **Auth: none** (user decision 2026-10-21).
  - The runner listens on loopback only, reached through usbmuxd.
  - Peer-UID auth doesn't exist here (usbmux peers arrive as loopback connections), and the spike's per-launch token was dropped as unneeded complexity.
  - Accepted: any process of the logged-in Mac user can drive the phone while the runner runs, as with adb on Android.
  - Environment variables still reach the runner through the `TEST_RUNNER_` prefix, which xcodebuild strips (the port, for example).
- **Framing:** NDJSON, one request per line, `id` echoed. Move to JSON-RPC 2.0 framing to match `PROTOCOL.md`, so `AgentClient` can be reused as-is.

---

## 5. Runner lifecycle on a real iPhone

### 5.1 Facts that shape it (measured)
- **The runner lives exactly as long as its `xcodebuild test-without-building` session.** SIGTERM or SIGKILL of xcodebuild ends the runner within seconds, through testmanagerd.
- **`simctl launch` / `devicectl process launch` of the `.xctrunner` starts the app but runs no test.** The test configuration comes over the IDE session (`XCTestSessionIdentifier`, an empty `XCTestConfigurationFilePath`). Without go-ios or Xcode acting as the IDE peer, nothing runs.
- **A detached xcodebuild** (started with `nohup`, owned by neither the CLI nor the daemon) keeps the runner alive. It survived:
  - 30 min of host lid-closed sleep;
  - a locked phone for 9+ min;
  - daemon restarts (it re-adopts through the port).
- **Start to first ping: 5–6 s warm**, including the reinstall of a changed build. agent-device reports 13–38 s cold with a cold CoreDevice tunnel (their figure).
- **`-only-testing:` installs only the runner,** not a target app. Setup must not depend on Xcode installing anything else.
- **Stale binary:** in 1 of 11 starts (simulator), `test-without-building` ran the *previous* build of the test bundle, after a clean quit. So `ping` must return a build ID compiled in (`GCC_PREPROCESSOR_DEFINITIONS=… BUILD_ID="<id>"`), and the host uninstalls and restarts on a mismatch. This repaired it every time. Not seen on the device, but the check costs nothing.

### 5.2 Signing
- **Bundle IDs must be unique per team:** a generic ID fails on personal teams ("bundle identifier is not available", agent-device). Derive them, e.g. `dev.droidctl.<hash of team>.runner`. The UI-test target produces `<id>.xctrunner`.
- **Build:**
  ```
  xcodebuild build-for-testing -project … -scheme Runner \
    -destination 'platform=iOS,id=<hardware UDID>' -derivedDataPath ~/.droidctl/ios/dd \
    -allowProvisioningUpdates DEVELOPMENT_TEAM=<team> \
    "GCC_PREPROCESSOR_DEFINITIONS=\$(inherited) DROIDCTL_BUILD_ID=\\\"<id>\\\""
  ```
  - `-destination id=` needs the **hardware UDID**; the CoreDevice ID fails.
  - The team ID can't be read from Xcode's settings by a script, so ask the user for it, or read it from an existing profile.
- **Free personal team (measured):**
  - The profile is valid for exactly 7 days: `CreationDate` 2026-10-20T10:28Z, `ExpirationDate` 2026-10-27T10:28Z.
  - `ProvisionedDevices` lists this phone only; `get-task-allow` = 1.
  - Read it with `security cms -D -i <app>/embedded.mobileprovision` from the derived data.
  - **Removing every app signed by the team removes the trust**, so the next install needs the Trust tap again. Measured several times. `teardown` must say so.
  - **Measured 2026-10-21: a free team can install only 3 apps on one device.** The 4th install fails with "This device has reached the maximum number of installed apps using a free developer profile" and the list of the 3. The product runner needs no host app, so it uses 1 slot; the e2e test app uses a second. `setup` removes earlier droidctl runners itself and otherwise names the apps to remove.
- **Paid team:** supported (user decision 2026-10-21), with no 7-day expiry and no Trust tap per install (inferred, not tested here).
- **Pending:** what an expired profile looks like in practice. Both spike apps are left on the phone until after 2026-10-27T10:28Z, to record the exact launch and xcodebuild errors. The trusted baseline launch works (2026-10-21).
- **Ship:** no prebuilt device runner (it must be signed by the user's team). The Xcode project ships in the wheel; `setup` builds it once per team and source hash, then caches it under `~/.droidctl/ios/`. A prebuilt *simulator* runner could ship like the APK (with a source hash).

### 5.2b Found while building I1 (2026-10-21)
- **No host app is needed.** A UI-test bundle without `TEST_TARGET_NAME` builds and runs on the device (`DroidctlRunner-Runner.app` only), so droidctl uses one of the free team's 3 app slots.
- **A passcode prompt blocks the start:** with the phone waiting for its passcode, `devicectl device info lockState` still said `passcodeRequired: false`, and the start failed after ~60 s with "Timed out while enabling automation mode" (the spike runner too). `setup` maps it to `screen-off` with "unlock the iPhone and enter the passcode if it asks".
- **The runner app runs in a 320×480 compatibility mode** (no launch screen in the generated xctrunner), so `UIScreen` lies; the screen size comes from the front app element's frame.
- **`XCUIApplication(bundleIdentifier:)` for an app XCTest didn't launch** is fine for the snapshot root once `IsVisible` is gone. The runner still prefers `XCAccessibilityElement elementWithProcessIdentifier:` with the pid from the hit-test.

### 5.3 Start
```
TEST_RUNNER_DROIDCTL_PORT=<port> \
nohup xcodebuild test-without-building -xctestrun <cached .xctestrun> \
  -destination 'platform=iOS,id=<UDID>' -only-testing:Runner/DroidctlRunner/testServe \
  > ~/.droidctl/ios/<udid>/session.log 2>&1 &
```
- Using `-xctestrun` (the `build-for-testing` output) instead of `-project` avoids rebuilding. agent-device also patches the env into the `.xctestrun` (idea, MIT).
- **Then:**
  1. Open usbmux `Connect` to the port and retry for up to ~60 s.
  2. `ping` must return the expected build ID.
  3. On a mismatch, uninstall the runner and start again.
- **Watch the session log** for known blockers and turn them into typed errors immediately, instead of waiting out a timeout:
  - "Unlock iPhone" → `screen-off` with the hint "unlock the phone";
  - "not trusted" / "explicitly trusted" → `not-trusted`;
  - "expired" → `profile-expired`, wording to be recorded on 2026-10-27;
  - `** TEST FAILED **` / exit → `connection`, with the log tail.

### 5.4 Adopt, stop, restart
- **Adopt:** the daemon stores `{udid, port, build_id, xcodebuild pid}` in state. After a daemon restart it pings first, before starting anything (agent-device: ~1 s adopt vs ~13–38 s cold, their figure).
- **Stop:** `quit` command (the test method returns), then kill the session holder if it's still there.
- **Restart cost:** ~6 s (measured).

### 5.5 Teardown (the iOS form of "never break the user's phone")
- **Built in I3** (`droidctl/ios/manage.py`); measured 26.6 s on the iPhone 12, mostly devicectl's uninstall. It removes only `dev.droidctl.r<8 hex>.runner.xctrunner` (the product scheme, any team). Research builds and other apps stay.
- Stop the runner and its xcodebuild; close our usbmux connections.
- **Uninstall the runner** (`devicectl device uninstall app --device <coredevice id> <bundle>`). agent-device leaves its runner behind; we don't.
- **Disclose in `doctor` and `teardown` what we don't undo:**
  - Developer Mode stays on;
  - the developer disk image stays mounted until reboot;
  - with a free team, removing our last app removes the Trust;
  - the signing certificate and profile stay in the user's Apple account and Xcode.
- **Never:**
  - write keyboard preferences (WDA turns Auto-Correction and Predictive off; we measured that we write none);
  - call accessibility-settings resets;
  - add a "health check" that presses Home (WDA's `/wda/healthcheck` does).

### 5.6 Other device commands (all measured working)
| need | command |
|---|---|
| install an app | `xcrun devicectl device install app --device <cdid> <path.app>` |
| uninstall | `xcrun devicectl device uninstall app --device <cdid> <bundle>` |
| launch (host side) | `xcrun devicectl device process launch --device <cdid> <bundle>` |
| read a file from an app's container (the test app's DTA log) | `xcrun devicectl device copy from --device <cdid> --domain-type appDataContainer --domain-identifier <bundle> --source Documents/dta.log --destination <file>` |
| list devices / details | `xcrun devicectl list devices`, `xcrun devicectl device info details --device <cdid>` (add `--json-output <file>` for parsing) |

---

## 6. Runner implementation rules (each one was learned by a failure)

| # | rule | what happened without it |
|---|---|---|
| R1 | **`@autoreleasepool` around every request** and every socket line | memory grew ~11 MB/min (sim 140→311 MB; iPhone 93→357 MB). With pools: iPhone **flat at 79–82 MB** for 30 min |
| R2 | **Override `-recordIssue:` and set `continueAfterFailure = YES`**; return recorded issues as `ok:false, issues:[…]` | one XCTest failure ("Failed to activate application" while locked) ended the test method, and the runner died |
| R3 | **No XCTest waiting API on the request path:** no `XCUIApplication launch/activate`, `XCUIElement tap`, `typeText` or `waitForExistence` | `activate` while locked killed the runner; `activate` of a missing app blocked > 2 min; `typeText` with no focus blocked ~41 s; `launch` sat in "Wait for … to idle" for **20+ min** on the iPhone |
| R4 | Use **testmanagerd daemon calls with completion blocks and our own timeouts** for everything (§7) | — |
| R5 | **Check `IsValid` before acting on a cached element** (4.7 ms; fails fast with ServerNotFound) | an AX action on an element of a dead pid took **6.2 s** (30 internal retries) |
| R6 | **Lock guard before every input** (§10), failing **closed** | AX presses reached the app *behind the lock screen* (DTA showed `ax_activate` at +2 s, +10 s and +40 s while locked) |
| R7 | **`ping` returns the build ID** | 1 stale start in 11 ran old code |
| R8 | Requests run **on the main thread** (`dispatch_sync` to main); socket threads only frame lines. Replies from daemon calls can arrive on the main queue, so wait by **spinning the run loop** (2 ms steps), never by blocking it with a semaphore | — (the spike's design; the semaphore was used only for the AX action, which replies off-main) |
| R9 | A bounded main-thread watchdog (agent-device idea, MIT): a request that exceeds its budget returns `timeout`; the runner keeps serving | not hit with R3 in place, but XCTest can still block |
| R10 | **Never write preferences, never press Home on our own**, and no interruption auto-handlers (disable XCTest's) | WDA does all three |
| R11 | **Drop the element map per tree** (or cap it); handles belong to one dump, as on Android | the spike's `forget` command shows the map grows without it |
| R12 | Treat timeouts in the first seconds after a **host wake** as retryable (`connection` with retry) | the only error in a 2 h soak was a snapshot timeout in the first second after DarkWake; in lid-closed DarkWake, snapshots took 4.7–9 s (thermal throttling) |

---

## 7. The private APIs we use (all reached by selector; no headers needed)

All via `XCTRunnerDaemonSession.sharedSession.daemonProxy` (KVC `daemonProxy`) unless stated otherwise. Cast `objc_msgSend` to the exact signature.

| purpose | selector / API | signature notes | measured (iPhone) |
|---|---|---|---|
| AX action | `_XCT_performAccessibilityAction:onElement:withValue:reply:` | `(int action, XCAccessibilityElement *el, id value, void(^)(NSError *))`. The error text contains `kAXErrorCannotComplete` when refused | **1.0 ms** |
| fast snapshot | `_XCT_requestSnapshotForElement:attributes:parameters:reply:` | root = `[XCUIApplication valueForKey:@"accessibilityElement"]`; attributes are string names (below); parameters start from `[[XCUIDevice.sharedDevice accessibilityInterface] defaultParameters]`; the reply object may need `valueForKey:@"elementSnapshot"` | Settings 87 ms; 1030 nodes 432–460 ms |
| hit-test | `_XCT_requestElementAtPoint:reply:` | `(CGPoint, void(^)(XCAccessibilityElement *, NSError *))`; **portrait device points** | 3–7 ms |
| attributes of one element | `_XCT_fetchAttributes:forElement:reply:` | `(NSArray<NSString*> *names, el, void(^)(NSDictionary *, NSError *))` | 3.4 ms |
| set value | `_XCT_setAttribute:value:element:reply:` | attribute `@2006` (value), `void(^)(BOOL, NSError *)` | 37 ms |
| type text | `_XCT_sendString:maximumFrequency:completion:` | `(NSString *, NSUInteger freq, void(^)(NSError *))`; freq 60 in the spike | "hello" 277 ms (vs `typeText` 1076 ms) |
| bundle for pid | `_XCT_requestBundleIDForPID:reply:` | `(int, void(^)(NSString *, NSError *))` | part of `hit` + pid: 34 ms |
| launch / bring to front | `_XCT_launchApplicationWithBundleID:arguments:environment:completion:` | `(NSString *, NSArray *, NSDictionary *, void(^)(NSError *))`. A missing app fails at once ("The request to open … failed"); a running app comes to the front with the same pid; locked: fails in 10–60 ms | 60 ms (sim) |
| touches / swipes / pinch | `XCPointerEventPath` `initForTouchAtPoint:offset:`, `moveToPoint:atOffset:`, `liftUpAtOffset:`; `XCSynthesizedEventRecord` `initWithName:displayID:interfaceOrientation:` (fall back to `initWithName:interfaceOrientation:`), `addPointerEventPath:`; `[XCUIDevice.sharedDevice eventSynthesizer]` `synthesizeEvent:completion:` | WDA's path (BSD-3: re-type, don't copy); several paths in one record = multi-touch | tap 272–290 ms (vs `XCUICoordinate tap` 460) |
| subscribe to AX notifications | `[accessibilityInterface _registerForAXNotification:(int) error:(NSError **)]` | `B28@0:8i16^@20`; synchronous | ~1 ms |
| receive AX notifications | wrap `-[XCTRunnerDaemonSession _XCT_receivedAccessibilityNotification:fromElement:payload:]` | `v36@0:8i16@20@28`; payload is a keyed archive; **arrives off the main thread** (I2: lock what the handler shares) | — |
| run-loop / animations idle | `[accessibilityInterface notifyWhenEventLoopIsIdleForApplication:reply:]`, `…notifyWhenNoAnimationsAreActiveForApplication:reply:` | `(XCUIApplication *, void(^)(NSError *))`; fire and keep serving | 3–13 ms when idle (§13.2) |
| notification names | `XCTStringFromAXNotification(int)` via `dlsym` | names XCTest's own numbers only | — |
| element identity | `[snapshot valueForKey:@"accessibilityElement"]` → `XCAccessibilityElement`; its description reads `AX element pid: N, elementID: X.Y` | parse to `"pid/X.Y"`; `processID` is private, so parse or use `bundle_for_pid` | — |
| screenshot | `XCUIScreen.mainScreen.screenshot.PNGRepresentation` (public). WDA's `_XCT_requestScreenshot` is an option for JPEG and scale | portrait pixels always (1170×2532) | 66 ms capture; 372 ms with the 3 MB PNG sent back. **Encode JPEG and scale on the phone** |
| orientation | `XCUIDevice.sharedDevice.orientation` (public, settable) | — | — |
| home / lock | `pressButton:XCUIDeviceButtonHome` (public); `pressLockButton` (private) | — | home 477 ms |
| app state | `XCUIApplication.state` (public; non-blocking): 1 not running, 2 suspended, 3 background, 4 foreground | — | — |

**Snapshot attributes** (string names; the real list is the `XC_kAXXCAttribute*` strings in `XCTAutomationSupport.framework`): the product runner asks for `ElementType`, `Identifier`, `Label`, `Value`, `Frame`, `Traits` and `PlaceholderValue`.
- `IsEnabled`, `IsSelected` and `HasFocus` are **not** real names; the snapshot's `enabled`/`selected` come from the traits.
- **Never `IsVisible` in a whole-app snapshot:** on the 1030-node test app it hung the snapshot past 10 s, while the same snapshot without it took 400 ms (2026-10-21). Use it only for single-element fetches.
- Every node also carries its class name as additional attribute 5004 (`UILabel`, `UISlider`, `_UIAlertControllerActionView`), unasked; the runner passes it on as `class`.
- Ask only for what the mapping uses: attribute cost is the main snapshot cost (WDA's lesson, idb's 130× figure).

**Snapshot parameters** (the defaults we read): `maxArrayCount`, `maxChildren` and `maxDepth` are all INT_MAX, and `traverseFromParentsToChildren` = 1. WDA sets `maxDepth` 50 and adds `snapshotKeyHonorModalViews`.
- `maxDepth` 12 cut Settings to 18 nodes in 17 ms, so depth caps drop real content. Use them only for a partial or incremental read.

**AX action numbers** (iOS, from `XCUIAccessibilityAction`):

| number | action | measured |
|---|---|---|
| 2010 | Press | ok on custom views, SwiftUI Button/Toggle, web content; **refused** (CannotComplete) on UIButton, UISwitch, UITextField, UITableViewCell, UIAlertController buttons, SpringBoard alert buttons |
| 2003 | ScrollToVisible | ok: an off-screen cell at y 2020 moved to y 304 |
| 2006 / 2007 | ScrollDownByPage / ScrollUpByPage | ok, one page (y 0→479); **refused at the end** (like Android's `scroll_forward` returning false) |
| 2004 / 2005 | Increment / Decrement | slider 0.50→0.60→0.50 |
| 2014 / 2015 | next page / previous page | ok on a slider (`ax_scroll 5/6`) |
| 2001, 2002, 2016, 2018 | unknown | **reply ok, nothing happened**: "ok" proves nothing; verify with the diff |

The three call routes (daemon proxy, `XCAXClient_iOS performAction:onElement:value:error:` with an `XCUIAccessibilityAction`, and its `remoteAccessibilityInterface`) all end in testmanagerd and agreed. **Use the daemon proxy.**

**Fragility:** these are private selectors. Every runner call must check `respondsToSelector:` and fail with `unsupported` naming the selector, never crash (idb's "selector signature guard"). Re-verify on every new Xcode/iOS release; the build ID plus the Xcode version go into `ping`.

---

## 8. Runner protocol (the `PROTOCOL.md` subset for iOS)

| method | iOS implementation | notes |
|---|---|---|
| `ping` | `{protocol, version, build_id, backend:"ios-runner", ios_version, model, xcode, screen:{w, h, scale, orientation, locked}, caps:{node_actions:true, has_back:false, events:true, units:"pt"}}` | `screen.locked` from the lock guard (§10) |
| `tree {bundle?, system?, budget_ms, max_nodes}` | daemon snapshot of the foreground app, plus SpringBoard when an alert or a SpringBoard surface is up (§9.3). Returns `dump`, `nodes`, `ms`, `degraded` | the foreground app comes from the hit-test at screen centre, then `bundle_for_pid` |
| `act {dump, handle, action, args, settle, force}` | `IsValid` → lock guard → AX action → on refusal, one checked touch (§11) | returns `{performed, method:"ax"\|"touch", refused?}` |
| `gesture {type, points, ms}` | event synthesizer; tree points converted to portrait device points (§12) | tap / long / swipe / path / pinch |
| `global {name}` | `home`; `lock` for tests only | **no** `back`, `recents`, `notifications`, `quick_settings` → `unsupported` |
| `type {dump, handle, text, clear?}` | focus check, then `sendString`; `set_text` via `setAttribute 2006` | §11.3 |
| `launch {bundle}` / `apps` | daemon launch; app lists come from `devicectl` on the host | — |
| `screenshot {scale, quality}` | `XCUIScreen` → JPEG, scaled on the phone | portrait pixels; the host rotates `--marks` (§12) |
| `current` | `{bundle, pid, state, keyboard, screen_state}` | — |
| `wait_idle`, `wait_for` | on the runner, driven by notifications and idle callbacks, §13 | decided |
| `screen_state` | the lock guard on its own | — |
| `quit` | ends the test method | — |
| `subscribe` / `events` | AX notifications (§13.1), deduped | no "clicked" event; `TouchEventsCompleted` instead |

Commands that need a missing capability return the `unsupported` error kind with a hint, never a silent no-op.

---

## 9. The tree

### 9.1 Raw size and our output (measured)
| screen | nodes | raw | droidctl snapshot |
|---|---|---|---|
| Settings › General (iPhone) | 149 | 16 KB | 35 elements, ~340 tokens |
| Settings root (iPhone; fixture not committed: personal data) | 162 | 17 KB | 17 elements, ~189 tokens |
| test app with a 200-row table (iPhone) | 1030 | 98 KB | 214 elements, ~1034 tokens **before** the off-screen fix (§9.4) |
| Settings, Keyboard, Contacts, Reminders, location alert (WDA, simulator) | 94–168 | 29–54 KB | 11–37 elements, 166–587 tokens |

The unchanged `snapshot.build`/`render` handled all of these through a throwaway mapper (`research/ios-spike/runner2raw.py`): clipping, row merge, regions, list hints, and the alert as a dialog.

### 9.2 Node mapping (runner tree → droidctl raw schema)
| raw field | iOS source | notes |
|---|---|---|
| `class` | XCUIElementType (int) → a role (table below) | the renderer works on roles, so map to the same Android class names the role table already knows (as the spike did), or add iOS names to the role table. **Decision for I1** |
| `id` | `identifier` | only when it differs from `label` (`name` repeats `label` on StaticText) |
| `text` | `label` (StaticText), `value` (editable, unless equal to the placeholder) | — |
| `desc` | `label` for non-text elements | — |
| `hint` | `placeholderValue`, or `label` for fields | — |
| `bounds` | `frame`, in **points**, converted to `[l, t, r, b]` | `ping` says `units:"pt"`; Android is px |
| `flags.enabled` | `IsEnabled` | — |
| `flags.selected` / `focused` | `IsSelected` / `HasFocus` | keyboard focus for typing: `HasFocus` on the field, plus the keyboard present |
| `flags.checkable` / `checked` | Switch/Toggle; `value == "1"` | — |
| `flags.editable` | TextField, SecureTextField (+ `password`), SearchField, TextView | — |
| `flags.clickable` + `actions:["click"]` | Button, Cell, Link, Switch, Toggle, fields, Key, Icon, plus anything with the button trait | iOS has no "clickable" bit; this is a role heuristic. Every action is still verified |
| `flags.scrollable` + scroll actions | Table, CollectionView, ScrollView, WebView | — |
| `visible:false` | `IsVisible` = 0 | §9.4 |
| `uid` | `pid/elementID` | resolver tier 1, §9.5 |
| `window` | app / SpringBoard / keyboard | §9.3 |

**XCUIElementType values seen** (from `XCUIElementTypes.h`):

| value | type |
|---|---|
| 1 | Other |
| 2 | Application |
| 3 | Group |
| 4 | Window |
| 7 | Alert |
| 8 | Dialog |
| 9 | Button |
| 12 | (web checkbox, seen) |
| 19 | Keyboard |
| 20 | Key |
| 21 | NavigationBar |
| 22 | TabBar |
| 24 | Toolbar |
| 25 | StatusBar |
| 26 | Table |
| 32 | CollectionView |
| 33 | Slider |
| 34 | PageIndicator |
| 36 | ActivityIndicator |
| 37 | SegmentedControl |
| 40 | Switch |
| 41 | Toggle |
| 42 | Link |
| 43 | Image |
| 44 | Icon |
| 45 | SearchField |
| 46 | ScrollView |
| 47 | ScrollBar |
| 48 | StaticText |
| 49 | TextField |
| 50 | SecureTextField |
| 52 | TextView |
| 58 | WebView |
| 75 | Cell |

### 9.3 Windows, SpringBoard and alerts
- **An in-app `UIAlertController` replaces the app's tree** with the alert (47 nodes).
- **A system alert (permissions, location) lives in SpringBoard's tree**, not the app's. `tree` must also read SpringBoard when one is up, and render it as a `dialog` window; act on it by label.
  - SpringBoard alert buttons refuse Press, so they take the checked touch.
  - **Never auto-accept** (WDA and Maestro accept by position).
- **Every iOS window has a full-screen frame, the status bar included.** Window bounds must come from the content's union, otherwise the status bar occludes everything and the screen reads as a dialog. The status bar is a system window.
- **Skip:**
  - empty overlay windows (Contacts read as 0 elements and `dialog=yes` until they were skipped);
  - SpringBoard windows in pixel units (1170×2532 frames on a 390×844 screen, and 1206×2622 on the simulator).
- **Finding the foreground app:** hit-test the screen centre, then `bundle_for_pid` (SpringBoard pid 34 on the Home screen). 34 ms.

### 9.4 Mapping rules (each seen in a real tree)
1. **Scroll bars are elements** (`Other "Vertical scroll bar, 2 pages"`, often twice). Turn them into the list's `more↓` hint (they give a page count) and drop them.
2. **Paired switches:** a labelled outer Switch spanning the row, and an unlabelled inner Switch that is the real toggle. This holds for UIKit Settings and for SwiftUI Toggle.
   - Merge them into one element and **act on the inner one**: tapping the outer one's centre hit the label and did nothing, yet WDA reported success.
3. **Duplicate subtrees** (DAG duplicates: `Dictate` twice, scroll bars twice). `_dedupe` must expect them.
4. **Disclosure chevrons** (`Image "chevron.forward"`) became stray `button "chevron"` elements. Absorb them into the row.
5. **`label`, `name` and `value` repeat** on StaticText. Keep one.
6. **Off-screen rows:** a `UITableView` exposes **every** row, with real off-screen frames (214 elements for a 200-row table).
   - Drop nodes with `IsVisible` = 0, or outside their scroll container, and count them into `more↓`.
   - `scroll-to` uses ScrollToVisible (2003) on the target element directly.
7. **Web content** maps cleanly (Button 9, TextField 49 with the placeholder as its value, Link 42, Switch 40 or 12 for checkboxes), each with a duplicate StaticText label.
   - It appears 0.74 s after launch on the iPhone and 3.3 s on the simulator. Until then the tree has only the WKWebView and its scroll bars: settle must not stop on that empty shell.
8. **Value lag:** a tree read straight after `sendString "abc"` showed "a" (simulator). The read-back for typing must wait or poll.

### 9.4b Prototype rules (measured 2026-10-21, `research/ios-decision.md` §14)
R1–R8 in `research/ios-spike/ios_map.py` implement items 1–6 above plus three found on a real app:
- **R1:** off-screen cells whose children report the list's corner.
- **R4:** the main window is the full screen.
- **R6/R7:** switch rows and segment strips.

The prototype cut a real app's menu tab from ~1175 to ~211 tokens and the test app from ~1034 to ~178, with the spatial layer unchanged. `ios/mapping.py` starts from it.

### 9.4c Rules found on a second real app (2026-10-26; its trees stay out of the repo)
- **R9, several targets in one cell:** a cell labelled with its own texts joined by ", " (cards side by side, a row with several price boxes, a tab strip), or an unlabelled cell with three or more texts side by side in one band. Its centre is one of the targets by position: `tap --text "All Markets"` opened "Dividend List" before this rule. The cell stops being a target; each text becomes one, and the runner touches it where it is (no AX press for a StaticText). A title over its subtitle (a card) stays one target, as does a two-text row ("Server Type · prep").
- **R10, wrapper cells:** an unlabelled cell holding cells or controls of its own is only their container (no blank `button … component?` around real ones). A table accessory chevron doesn't count.
- **R11, collapsed buttons:** a button whose only child is its own title laid out zero wide (but full height) isn't on screen. Safari's Page Menu and the keyboard's Emoji key hold an empty 0x0 child and stay.
- **Custom back buttons:** the leading button of a navigation bar named back-like (`arrowBack`, `nav back`, `Geri`, `chevron.left`…, `mapping.BACKISH`) is what `back` taps, besides UIKit's back trait and Safari's `BackButton`.
- **Presented sheets are dialogs:** a view presented over a `UIDimmingView` (page/form sheet, popover) that doesn't fill the screen becomes the window's bounds, flagged `modal`; the snapshot layer then calls it a dialog even at 93% of the screen. The screen behind it is out of the tree, as behind an alert.
- **R12, switch buttons:** a Button whose id names a switch/toggle and whose value is 0/1 is a switch (a dev build's custom switch); a row title that repeats the state ("Debug Off") loses it.
- **R13, read-only text views:** a TextView with the static-text trait (0x40) and without the text-entry trait (0x40000, on every editable field and web input) is text, not an input.
- **App-drawn pop-up cards are dialogs:** an empty, unlabelled full-screen view followed by a card that floats clear of the top and the bottom (10%/90%), with the screen behind out of the tree, as for UIKit's dimming view.
- **The front app when the centre holds nothing accessible** (a story view: a spinner on black): the hit-test falls through to SpringBoard. Four more points are probed; an app answering at two, or at one when it was the last app in front, owns the screen. On the Home screen none answers.
- **Spinners** (ActivityIndicator) map to `progress`.
- **System prompts over an app** (location, notifications): the prompt's view service (e.g. `CoreLocationVanillaWhenInUseAuthPromptPlugin`) owns the screen centre with a 270x180 frame. The runner then also reads SpringBoard, which holds the prompt's title and buttons (a dialog), and never takes a frame that doesn't fill the screen (origin 0,0, both sides ≥ 320 pt) as the screen size: it once cached 270x180 for the runner's life. `launch` accepts a system alert in front after 1 s (`system_alert`).

### 9.5 Element identity (`pid/elementID`)
| change | same ID |
|---|---|
| re-read, same screen | 48/48 |
| switch value change | yes |
| page scroll and swipe on a 200-row `UITableView` | 200/200, labels unchanged (no recycling seen through AX) |
| app to background and back | 216/218 |
| relaunch (new pid) | 0/48 |
| Settings: push General, pop back | 2/30 (Settings rebuilds its cells) |

- **This is resolver tier 1 (`uid`) within one pid and screen,** always with the usual fingerprint check: navigation re-creates elements.
- A stale ID after a relaunch fails fast through `IsValid`.
- **Note:** WDA's element handle for a switch went 404 right after it toggled, but our `pid/elementID` didn't change. WDA's handle is not the AX identity.

---

## 10. Lock guard (screen state)

**Why:** while the phone is locked, the runner keeps running and **AX actions still reach the app behind the lock screen**.
- While locked, the app's tree collapses to 1 node and its state reads 3 or 2.
- SpringBoard shows `SBCoverSheetWindow`, `lockscreen-date-view`, and a `proud-lock` element labelled **"Locked"**.

**Algorithm (fails closed):**
1. Hit-test the screen centre, then `bundle_for_pid`. If the bundle isn't SpringBoard, the state is `clear`.
2. If nothing was hit, or SpringBoard is in front, snapshot SpringBoard:
   - a `"Locked"` label gives `locked`;
   - otherwise `SBCoverSheetWindow` gives `cover-sheet`;
   - otherwise SpringBoard with a hit gives `springboard` (the Home screen, allowed);
   - anything else gives `screen-unknown`.
3. Input commands (act, gestures, typing, set value, launch) are refused for `locked`, `cover-sheet` and `screen-unknown`, unless `force`.

**Measured:**
- **Cost when clear:** 5.6 ms on the iPhone, 6.2 ms on the simulator.
- **Locked:** every command was refused within 0.10–0.19 s. DTA showed **no events** in the locked window.
- **10/10 automated cycles** (simulator): `locked` → `cover-sheet` → `clear` after `home` ×2. All actions were refused, with exactly the 20 expected DTA events and 0 inside a locked window.
- **The first version failed open on the device:** with the screen off, the centre hit-test returns **nothing**, so the state read "unknown" and nothing was blocked.
  - The press reached the suspended app.
  - A tap went to the lock screen.
  - `activate` hung the runner.
- **After an unlock with the cover sheet still up**, `launch` fails ("request to open … failed"), although the app's tree is back. Report `cover-sheet`, or dismiss it with `home` when the user asks for it.
- **Unlocking can't be automated** (passcode or Face ID). The error tells the user to unlock.

**Error kinds:** reuse `screen-off` ("the screen is off or locked") for `locked` and `screen-unknown`, with `data.state`. Add `cover-sheet` only if agents need to tell it apart (proposal: keep it as `screen-off` with `data.state:"cover-sheet"` and a hint).

---

## 11. Actions

### 11.1 Policy (the Android policy, with iOS's methods)
1. **Resolve** the ref on the host as today, giving a handle in the current dump.
2. **On the runner:**
   1. Look up the handle and check `IsValid`.
   2. Run the lock guard.
   3. Check enabled.
3. **Try the AX action.**
   - `ok` → `method:"ax"`, then settle and diff.
   - `refused` (CannotComplete) → nothing ran (no side effects seen; not guaranteed for every app) → **one** touch at the element's hit point, but only if the hit-test assertion passes. That gives `method:"touch"`.
   - Any other error → typed error, no touch.
4. **Hit-test assertion:**
   - `_XCT_requestElementAtPoint` at the target point (converted to device points, §12) returns the deepest element.
   - Pass when it has the same pid and is the target, a descendant of it, **or has a frame inside the target's frame** (`fetch XC_kAXXCAttributeFrame`).
   - The frame rule is needed: a `UISwitch` returns an internal `UIView` that isn't in the snapshot and whose `Parent` isn't the switch, while a `UIButton` returns its StaticText child.
   - Also pass (2026-10-26, a real app; the result says why in `hit_test`):
     - `container`: the hit is an ancestor of the target (a text inside a cell of several targets, R9);
     - `empty-container`: the hit is a table, collection or scroll view with none of its own elements at the point. The AX hit-test walks elements in reading order, not z-order: an empty table laid out under a button answered for the button's point, and a touch there pressed the button;
     - `inconsistent`: the hit's own frame doesn't contain the point. That app's tab bar answered with the neighbouring tab, deterministically, and with a strip 57 pt above it; such an answer is no evidence of anything.
   - The runner's `hit {x, y}` method returns what the hit-test answers at a point (diagnostics).
   - Fail → `occluded` (with what is there now), no touch.
5. **Settle + diff, always.** An `ok` reply doesn't prove an effect (§7). `handled`/`no-change` work as on Android.
6. **After the settle, on the host** (2026-10-26; the run loop idles while an app waits for its server, so the settle can end before the next screen):
   - **loading:** a spinner the action brought up → read again until it's gone (8 s cap, `loading_ms`; "still loading" warning after). A splash (nothing actionable where the screen before had targets) → until something is (3 s cap). Measured: rates 1.0 s, a login 1.3 s, a stock page 2.7 s, each in one command.
     A spinner a table cell swallowed (the cell reads as row "In progress") counts as a spinner.
   - **late push:** a delivered tap that changed nothing → read again until the screen changes (1.2 s cap). A real app's rows went idle >100 ms between the touch and the push and answered "unchanged"; the next screen came ~0.45 s later. A tap that really does nothing costs the 1.2 s.
   - **mid-push:** 3+ overlapping pairs (two screens drawn at once) → read again until it stops changing (1.5 s cap; a screen laid out like that costs one read).
   - **typing into a field that leaves:** keys delivered, then the field is gone (a 6-digit login field submits itself) → success with a warning, never a fallback that types again.
   - **a stale handle** (`gone`/`stale dump`) → re-resolve once on a fresh read, by the locator or the ref's fingerprint. An app that reloads its table on every price tick replaces each cell's element between the read and the act.
7. **The tree cache:** an action resolves its target on the last tree this process read (the daemon keeps it) when the runner's `gen` and `dump` are unchanged (`gen` ~20 ms; a tree 250–460 ms on 300–450 nodes). `snapshot`/`wait` always read. On static screens droidctl's own time per action fell to ~150 ms; live screens (prices) change `gen` and read.

**Measured per control** (DTA confirms each; "fired once" means `ax_activate` was refused, then `touch_began`, then the action ran exactly once):

| control | path |
|---|---|
| custom view with `accessibilityActivate` | AX Press, no touch |
| SwiftUI `Button` / `Toggle` | AX Press: action fired / toggled, no touch |
| web Button / Link / Checkbox | AX Press |
| UIKit `UIButton`, `UISwitch`, `UITextField`, `UITableViewCell`, alert buttons, SpringBoard permission buttons | refused → checked touch, fired once |
| `UISlider` | Increment/Decrement |
| `UIScrollView` / table | ScrollDownByPage/UpByPage; ScrollToVisible for `scroll-to`; a swipe also works |

**There is no touch-free path for standard UIKit controls (measured 2026-10-21, `research/ios-decision.md` §13b):**
- A sweep of ~520 action numbers on a UIButton, UISwitch, cell and text field triggered none of them.
- Some numbers (2025, 2042, 2043) kill the app, so send only an allowlist: 2003, 2004, 2005, 2006, 2007, 2010, 2014, 2015 and the two idle actions.
- VoiceOver itself taps the activation point in this case.
- **The touch point is the element's own `XC_kAXXCAttributeVisiblePoint`**, not the frame centre. iOS puts it on the visible part: under the keyboard it chose the uncovered strip. It is then hit-tested as above.
- **Text needs no touch:** `setAttribute` value (2006) filled a whole real login form, input masks included.

**Web elements belong to WebKit's WebContent process** (I2, 2026-10-21): their `pid/elementID` carries the WebContent pid, not the app's (12 nodes, pid 1911, against 1060 nodes at pid 1910).
- Idle requests sent for that process never replied, and they emptied the web view's tree for good (2/2).
- So settle and every per-app call use the **front app**. `front` itself skips a WebKit hit at the screen centre: it asks other points, else uses the last app.

**Expect most standard UIKit taps to be `method:"touch"`.** That is fine (~280 ms), but it must be reported, and the double-trigger rules carry over unchanged.

### 11.2 Gestures (measured)
| gesture | how | result |
|---|---|---|
| tap | one pointer path, lift at +0.05 s | 272–290 ms |
| long press | tap with a 1.0 s hold (`moveToPoint` at the same point) | returns after 1.26 s |
| swipe / scroll by swipe | two points, 0.3 s | works on tables |
| pinch | **two** pointer paths in one event record | scale 3.26 out / 0.27 in, `touches=2` (iPhone and simulator) |
| path / drag | n points | built, not separately measured |

### 11.3 Text
- **Focus first:** tap the field (or set its value, which also focuses it), then confirm focus.
  - Use the 1018 notification the settle collected: its element is the focused field (107–195 ms after the action).
  - Only if none arrived, fetch `XC_kAXXCAttributeResponderElement` on the application element. It returns the field or its containing cell, in 7–14 ms; with nothing focused it takes ~106 ms and returns nothing.
  - If not focused → `occluded`/`not-focused`, never type.
  - `typeText` with no focus is XCTest's 41 s hang; `sendString` with no focus is unmeasured but must not happen.
- **Type:** `_XCT_sendString` types per character (`editingChanged` per character); 277 ms for 5 characters. `"\n"` fires `textFieldShouldReturn` (return / `ime_enter`).
- **Set / clear:** `_XCT_setAttribute` with value 2006 replaces the value; `editingChanged` fires once (37 ms). Read the value back (no-change / unconfirmed, as on Android).
- **Keys first (2026-10-26):** `type` focuses the field, deletes what it holds with `"\b"` keys, types the text with `sendString` and reads it back; set_value is the fallback (no focus, or the keys didn't verify). On a real app's formatted amount field, set_value changed the shown value but not the app's model: it read back "10000", then the field emptied when editing ended. Keys stuck ("10000" shown as "10.000", "10.000,00" after editing); deletes work ("10.000" → 2 deletes → "100"). 5 characters ≈ 420 ms; a whole `type` 1.1–2.2 s.
- **Web inputs:** tap + `sendString` and `setAttribute` both work.
- **Secure fields:** unmeasured. Treat the value as unreadable.
- **Keyboard state:** `XC_kAXXCAttributeActiveKeyboard` on the application element, a `Keyboard` node (type 19) in the tree, or 1031 `{visible}`.
  - The runner trusts the tree's Keyboard node.
  - While the keyboard is up, `tree` marks the focused input `focused:true`, using `ResponderElement` (the field, or the first input inside the cell it names). The mapping turns that into the `focused` flag.
- **Keyboard dismissal** (`hide-keyboard`; there's no `back` on iOS), measured on the default keyboard (§15.8 of the evidence):
  1. **AX Press on the key with identifier `Return`** dismissed it with no touch: the keys are in the app's tree with locale-independent identifiers. Use a key or accessory button labelled Done/Go/Search/Send/Next the same way. The Return key is an element of type **Button (9)**, identifier `Return`, label `return`, not a `Key` (iOS 18.7.8). With the runner, `hide-keyboard` dismissed it in 5/5, settle median 244 ms.
  2. If the Return key refused the press, the same key typed (`sendString "\n"`).
  3. **No Return key and no Done button (a number pad): `unsupported`**, no newline: on a real app's amount field a typed newline went in as input and cleared the field (2026-10-26). The hint says to tap a label or another field; that app ended editing on a tap on another row. A tap outside and swipes didn't dismiss the S1 keyboard.
  - Success is judged on the mapped screen, not the runner's `hidden` (it counted a keyboard sliding off as shown, and its `0` once read as success).
- **`type` leaves the keyboard up** (setAttribute focuses the field) and reports it. A later action on a covered target uses ScrollToVisible (2003) first.

---

## 12. Coordinates and rotation (measured; four orientations on the iPad simulator, landscape both ways on the iPhone)

**Three spaces:**
- **The tree** is in *interface* points: it rotates with the UI.
- **Hit-test and synthesized events** take *portrait device* points: they don't rotate.
- **Screenshots** are always *portrait pixels*: 1170×2532 in landscape too.

W and H are the app frame's width and height **in the current orientation**:

| device orientation | app frame (iPad) | tree (x, y) → touch/hit point |
|---|---|---|
| Portrait | 820×1180 | (x, y) |
| PortraitUpsideDown | 820×1180 | (W − x, H − y) |
| LandscapeLeft | 1180×820 | (H − y, x) |
| LandscapeRight | 1180×820 | (y, W − x) |

- **The event record's `interfaceOrientation` doesn't matter.** All four values hit with the converted point and none hit with the raw tree point. Only the conversion matters.
- **AX actions don't care** about orientation.
- **iPhones with Face ID don't do PortraitUpsideDown:** setting it left the app in landscape-right.
- **Where the conversion lives:** in the runner, because it alone knows the current orientation at the moment of the touch. The host sends tree points. `ping`/`tree` report `orientation`. `--marks` must rotate the screenshot (or the boxes) using the same table.
- Rotation itself (`XCUIDevice.orientation = …`) logged `rotate size=844x390` on the iPhone.

---

## 13. Settle, events and waits (measured 2026-10-21, iPhone 12)

**iOS does have an event stream; nobody had used it from a third-party runner.** Details and raw numbers are in `research/ios-decision.md` §12; the probe code is in `research/ios-spike/`.

### 13.1 Accessibility notifications
- **Subscribe** with `[XCUIDevice.sharedDevice.accessibilityInterface _registerForAXNotification:(int)n error:&err]` (returns BOOL, ~1 ms). It accepted every number tried (0–5100).
  - `addObserverForAXNotification:handler:` and `registerForAXNotificationsForApplicationWithPID:…` assert "only supported in the local macOS AX interface" (decompiled), so don't use them on iOS.
- **Receive** them by wrapping `-[XCTRunnerDaemonSession _XCT_receivedAccessibilityNotification:fromElement:payload:]` (`v36@0:8i16@20@28`) with `method_setImplementation`, then calling the original.
  - The payload is `NSData` (a keyed archive); the element is an `XCAccessibilityElement` (`pid/elementID`).
  - **Arrivals are not all on the main thread** (I2, 2026-10-21: a settle that read the notification log on the main thread crashed with "mutated while being enumerated"). The runner keeps its event ring, the settle log and the dedupe list under one lock, and pushes to subscribers after releasing it.
  - Some arrive **twice** (4002, 1021, 1006; XCTest registers too). Every true duplicate came within 3 ms (619 pairs), so dedupe identical (number, element, payload) within **5 ms** (`research/ios-decision.md` §15.2).
  - **Register once per runner process:** each extra `_registerForAXNotification:` call adds one more delivery of every event (five copies after four registrations).
- **Names** come from `XCTStringFromAXNotification(int)` (dlsym). It names only XCTest's own: 1006 alert, 1011 orientation, 1018 first responder, 1021 pid status, 1028 HID event, 1053 focus, 4002 user testing.
- **UIKit's public constants**, read on the phone, confirm 1000 screen changed, 1001 layout changed, 1008 announcement and 1009 page scrolled. 1031 and 3031 are named by their payloads (§15.1 of the evidence).

**What arrives** (numbers without a name are from UIKit's public `UIAccessibility*Notification` order, inferred from when they fire):

| n | meaning (seen) | when |
|---|---|---|
| 1000 | screen changed | alert open / close, end of a navigation push / pop, app switch |
| 1001 | layout changed (`data = AXForceUpdateChange` while scrolling) | scrolls, keyboard, transitions |
| 1006 | alert (`kAXAlertNotification`) | `UIAlertController` shown |
| 1008 | announcement (UIKit constant); `AXAnnouncementTypeKeyboardAppearSound`/`…DisappearSound` during keyboard changes | keyboard show and hide |
| 1031 | **keyboard visibility**, payload `{visible = 1/0}`; on show it comes at the start and again when the keyboard is in place (~500 ms later) | keyboard show and hide |
| 3031 | `kAXAppAccessibilityReadyNotification` (payload `code`, `bundleID`) | 0.74–1.19 s after a cold launch |
| 1009 | page scrolled, payload "rows 7 to 14 of 200" | AX page scroll |
| 1017 | text changed (inferred) | once per typed character |
| 1018 | first responder changed; **the element is the newly focused field** | field focus (107–195 ms after setAttribute / tap), alert |
| 1021 | pid status (payload pid, suspended) | app switch, keyboard |
| 1025 | value or frame change (inferred) | switch toggle, transitions, list motion |
| 4002 `TouchEventsCompleted` | a synthesized touch was delivered | ~80–200 ms into a tap |
| 4002 `KeyEventComplete` | a typed key was delivered | per character |
| 4002 `ViewDidAppear` / `ViewDidDisappear` (+ `controllerClass`, `controllerTitle`) | a view controller appeared / disappeared | navigation, alerts, keyboard |
| 4002 `RunLoopIsIdle` / `AnimationsNonActive` | replies to the idle requests below | — |

This is close to Android's event stream, so `events`/`subscribe` and toast-like checks can be offered on iOS too (`caps.events:true`). **There is no "clicked" event**: `TouchEventsCompleted` only says the touch was delivered, not that a control handled it.

### 13.2 Idle callbacks (XCTest's own quiescence, without its waiting wrapper)
- **Run loop:** `[accessibilityInterface notifyWhenEventLoopIsIdleForApplication:(XCUIApplication *)app reply:^(NSError *)]`.
- **Animations:** `notifyWhenNoAnimationsAreActiveForApplication:reply:` (same signature).
- **How they work (decompiled):** each one sends an AX action to the app (`beginMonitoringIdleRunLoopAction` / `detectAnimationsNonActiveAction`) and replies when the app posts the matching 4002 event. The action has no visible side effect in the app; DTA shows none.
- **Fire them without blocking** and keep serving; the reply arrives on the main run loop.
- **Don't poll snapshots while waiting.** A 380 ms snapshot in flight delayed the idle reply to ~290 ms instead of ~3 ms (the daemon serialises).

| measured (iPhone) | run-loop idle | animations idle |
|---|---|---|
| app already idle | 3–13 ms | 3–13 ms |
| after a tap on a switch | +3 ms after the tap returns | +17 ms |
| alert open / dismiss | +3 ms | +290 / +250 ms (the alert animation) |
| Settings push / pop | +2 ms | +430 / +390 ms (the transition) |
| **fling (0.9–2.8 s of deceleration)** | **7–8 ms after the app's own "scroll end" (8/8, DTA timestamps on the same clock)** | **~370 ms: it does not cover scroll deceleration** |

Neither callback alone is enough: the run-loop one misses transitions, and the animations one misses deceleration. Use both.

### 13.3 The settle rule (decided)
After the action returns, on the runner, in the same round trip:
1. Request run-loop idle **and** animations idle (both at once).
   - **Re-arm:** if 1018 or 1031 `visible = 1` arrives after both replied, request both again. Without it, a setAttribute settled 2/3 times before the keyboard animation (the replies came at ~28 ms, the keyboard started at ~51 ms). Re-arming on every late notification also works, but costs a Settings push ~500 ms for nothing (§15.4 of the evidence).
   - **After an AX scroll action** (2003, 2006, 2007, 2014, 2015) the quiet window starts no earlier than 200 ms after the action: the scroll animates for 162–183 ms (16/16) while both idle replies come back in 4–11 ms, and no notification marks it. Proposal from measurements; not yet run on the runner.
2. When both have replied, wait until no change notification (1000, 1001, 1006, 1008, 1009, 1017, 1018, 1020, 1021, 1025, 1031, 3031, and the 4002 `ViewDid*` / `KeyEventComplete` / `TouchEventsCompleted` events) has arrived for **100 ms**. This catches `ViewDidAppear` and screen-changed, which arrive ~20–25 ms after animations idle at the end of a navigation.
3. Cap the whole wait at `timeout_ms` (default 3000) and report `idle:false` when the cap is hit. Cold launches settled in 0.42–1.29 s (Settings, test app, a real app, Safari restoring a page), so 3000 is kept.
4. Take **one** daemon snapshot. Return it with `settle_ms` and the notifications seen (as `events`).

**Scored against ground truth:** the last visible change, from continuous snapshot polling with diffs and with empty wrapper nodes ignored. 14 scenarios × 3 rounds on the iPhone:
- the test app: no-op press, switch, slider, alert open / OK, AX page scroll, fling, focus a field, type, return;
- Settings: app switch, push General, pop;
- switch back to the test app.

**The final snapshot saw the final screen in 42/42.** The 3 flings were checked with the app's own scroll-end time: the polled truth (~400 ms per snapshot there) was too coarse to judge them.

| scenario | action returns | last visible change | settle done (rule) |
|---|---|---|---|
| no-op press / slider / AX page scroll | 6–9 | — | 107–113 |
| switch tap | 274–276 | ~275 | 392–397 |
| alert open / OK | 274–303 | 274–391 / ~300 | 668–690 / 648–657 |
| fling | 364–376 | deceleration end 0.9–2.8 s | idle + 7 ms + 100 |
| focus field (keyboard up) / type "abc" / return (keyboard down) | 272–276 / 251–254 / 240–247 | ~275 / ~250 / ~220 | 792–798 / 360–363 / 700–706 |
| switch to Settings / push General / pop | 65–79 / 289–296 / 281–297 | ~70 / ~630 / ~290 | 230–468 / 838–849 / 785–813 |

- **Compared with the alternatives** (same data): a pure quiet window of 150 ms after the last notification was early in 8/42; 300 ms was right but slower (median 600 ms, max 1.5 s).
- **Polling snapshots is out.** A snapshot costs 90–450 ms, and polling also slows the idle replies.

### 13.4 Other findings from these runs
- **Off-screen recycled table cells come back with shuffled labels on every snapshot** (all at the same off-screen frame). Any tree comparison (diff, signature, `--expect-change`) must ignore off-screen nodes, the same rule as §9.4.6.
- **Empty full-screen `Other`/`Window` wrappers appear and disappear for up to ~400 ms after a transition.** They are invisible after mapping, so diffs must be computed on the mapped snapshot, not the raw tree.
- **A snapshot sent straight after an app is brought to the front:**
  - **§12:** hung for the full 30 s in 2/3 rounds.
  - **§15.6 (re-measured):** didn't hang in 0/33 snapshots, but failed fast with `kAXErrorServerNotFound` ("Error getting main window") in 11/12. One or two immediate retries worked.
  - **After a settle:** 9/9 succeeded first time.
  - **Rule:**
    - settle before any snapshot that follows an activation;
    - retry `kAXErrorServerNotFound` with a short backoff, for up to ~1 s;
    - keep a 5 s snapshot timeout plus one retry, then report `degraded`.
- **Text-field value lag** (§9.4.8) is covered by the rule: typing settles after the `KeyEventComplete`/1017 burst.

### 13.5 Waits
- **`wait_idle`:** the same rule without an action.
- **`wait_for`:** on the runner, re-check the condition on the mapped tree after each settle-quiet window triggered by change notifications, never by fixed polling; cap at `timeout_ms`.
- **The web view shell** (§9.4.7): after a cold launch, the test app's web content appeared at 1.73–1.75 s, long after the settle (~0.4 s), and **no notification marks it** (§15.7 of the evidence). So use `wait_for` on content; AGENTS.md must say so for iOS web views. The same applies to network-bound content (Safari kept changing until ~1.7 s).

## 13.5b Web views in a real browser (Safari, 2026-10-22)
- **A WKWebView ignores AX page scrolls:** 2006/2007 were refused 5/8 and returned "ok" without moving 3/8. Scroll it by a drag.
- **The page's own root** is an `Other` labelled with the page title, as big as the page. **Landmarks** are `Other` nodes labelled with their role. Neither is content.
- **Text with inline links** comes as separate runs ("The", "iPhone", "is a line of", [link] …). Kept as they are.
- **Safari's address field:** for a few seconds after a load, the capsule shows "Reader and Translation Available" instead of the address field (`TabBarItemTitle`). `wait --id TabBarItemTitle` first.
- **Scroll bars:** "Vertical scroll bar, N pages" is the only sign that a full-screen web view scrolls.

## 13.6 The phone's log (I4, 2026-10-21)
- **Route:** `com.apple.syslog_relay`, reached through lockdown (port 62078): StartSession with the Mac's pair record (HostID, SystemBUID), TLS with its HostCertificate and HostPrivateKey, StartService, then a TLS stream.
  - The stream is NUL-separated text lines: `Oct 21 22:18:31 iPhone TestApp(UIKitCore)[2020] <Notice>: …`.
  - It is live only, with no history.
  - Volume: bursts of ~1,500 lines/s.
- **It conflicts with Xcode:** while the stream is open, xcodebuild cannot connect to the iPhone ("Device is busy (Connecting to iPhone)", 60 s timeout, 2/2). Repeated failures left CoreDeviceService at 95% CPU until it was restarted. droidctl therefore:
  - reads only while in use;
  - lets go around its own build and start (an `xcode-busy` flag file).
- **Your own Xcode:** the same conflict will hit it while droidctl is reading, i.e. for 60 s after a droidctl command. Not measured.

## 14. Measured performance budget (iPhone 12, medians)

| operation | ms |
|---|---|
| usbmux RTT (ping) | ~3 |
| daemon snapshot: Settings 162 nodes / test app 1030 nodes | **87 / 432–460** |
| public `XCUIApplication snapshotWithError:` (same screens; don't use) | 137–147 / 658–702 |
| AX action | **1.0** (with lock guard: 7.7–8.2) |
| lock guard when clear | 5.6 |
| hit-test / with pid→bundle | 3–7 / 34 |
| fetch one element's attributes | 3.4 |
| synthesized tap | **272–290** |
| long press (1 s hold) | 1260 |
| sendString 5 chars | 277 |
| set value | 37 |
| screenshot capture / with 3 MB PNG over the wire | 66 / 372 |
| `home` | 477 |
| runner start → first ping (warm, reinstall included) | 5000–6000 |
| `XCUIApplication launch` (don't use) | 2400–2730, and once 20+ min |

**Simulator (Intel Mac, for contrast):** daemon snapshot of 1102 nodes 743 ms; the public one 1246 ms; the S5 in-simulator helper 320–430 ms.

Expected agent-visible costs (estimate, adding up the above):
- **tap on a standard control:** guard + refused AX + hit-test + touch ≈ 300 ms, + settle;
- **press on a SwiftUI control:** ≈ 10 ms, + settle;
- **snapshot of a typical screen:** ≈ 100 ms.

The Android SM-N950F numbers for comparison: snapshot 147 ms, tap+settle 493 ms (PLAN.md).

---

## 15. Robustness evidence

| run | iterations | errors | memory | notes |
|---|---|---|---|---|
| iPhone, 30 min, before the pool fix | 1648 | 0 | 93 → 357 MB (iOS trimmed to ~300; never killed) | snapshot p50 460, act 2.7, tap 272 |
| iPhone, 7 min, with pools | 384 | 0 | flat 82 MB | — |
| **iPhone, 30 min, final runner** (pools + guard + daemon launch) | 1647 | **0** | **flat 79–82 MB** | snapshot p50 451–457, act+guard 7.7–8.2, tap 276–283 |
| simulator, 2 h | 3290 | 1 (first second after host wake) | 134–154 MB | survived 30 min of host sleep |
| simulator, 30 min, Mac kept awake | 1231 | 0 | 134–141 MB | confirms the sleep cause |

**Workload per iteration:**
- a daemon snapshot plus an AX press;
- every 5th iteration, a synthesized tap;
- every 10th, a public tree read and an element-map flush.

**Not run:** days-long sessions, and a profile expiring under a running runner (pending, §5.2).

---

## 16. Error kinds (proposal)

| kind | use on iOS |
|---|---|
| existing `screen-off` | locked, cover sheet, screen unknown (`data.state`), "Unlock iPhone" during start |
| existing `occluded` | the hit-test assertion failed |
| existing `stale-ref` | `IsValid` false, or the handle isn't in the current dump |
| existing `unsupported` | back/recents/notifications; a missing private selector |
| existing `connection` | usbmux connect failed, runner gone, session holder dead, timeouts after a host wake (`data.retryable`) |
| existing `not-installed` | runner not built or installed for this device |
| existing `missing-dep` | no Xcode / `xcodebuild` |
| **new `not-trusted`** | the developer isn't trusted on the phone (free team); hint: Settings → General → VPN & Device Management |
| **new `signing`** | no team, no provisioning, bundle ID taken, device not provisioned; carries xcodebuild's message |
| **new `profile-expired`** | the 7-day profile ran out; hint: run `setup` again (exact device wording pending, §5.2) |

New kinds go into `ERROR_KINDS`, and the AGENTS.md tables get regenerated (`tests/test_docs.py`).

---

## 17. Licensing and what to port

| source | license | take |
|---|---|---|
| agent-device | MIT | the usbmuxd plist client (`runner-usbmux-protocol.ts`), the xctestrun env patch, the build-for-testing / test-without-building split, the startup-failure reasons, the adoption design, the main-thread watchdog, the `record(_:)` kAXError mute. **Port with NOTICE attribution** |
| WebDriverAgent | BSD-3 | the event-synthesis sequence (`XCPointerEventPath` / `XCSynthesizedEventRecord`), the snapshot-parameter tricks, `(elementID, pid)` identity. **Re-type, don't copy**, and keep BSD notice text if any code is adapted |
| idb | MIT | the axbridge design: label assertion on hit-test, selector guards, "ignored action reports success" |
| Maestro | Apache-2.0 (9 files are WDA BSD-3) | runner outline ideas |
| go-ios | MIT | later: Linux and no-Xcode (as a subprocess) |
| pymobiledevice3 | GPL-3 | **never imported**; at most an optional user-installed subprocess |
| mobilecli / droidrun Portal | FSL / AGPL | ideas only |

**Privacy:**
- The spike `project.pbxproj` had the personal team ID written into the TestApp target by Xcode. It now reads `$(S1_TEAM)` like the runner target (fixed 2026-10-21), so builds pass `S1_TEAM=<team>`.
- The product project must never carry a team ID either.
- The Settings-root and home-screen fixtures stay out of the repo (Apple Account name, Wi-Fi SSID, app list).

---

## 18. Testing strategy
- **Ground truth:** the iOS test app's DTA events, never droidctl's output.
  - The spike app writes them to os_log and `Documents/dta.log` (`UIFileSharingEnabled`); the host pulls the file with `devicectl device copy from` (§5.6).
  - **The product's e2e uses the same pull.** `log stream` from a device is an alternative (os_trace), not built.
- **The iOS test app** ports the TESTAPP.md scenarios. Every control kind in §11.1 is a scenario, plus:
  - the 200-row table (off-screen rows, ScrollToVisible);
  - paired switches;
  - an alert and a permission prompt;
  - a WKWebView;
  - a pinch view;
  - rotation;
  - a text field (focus, return, set and clear).
  
  The spike's `TestApp/App.swift` is the seed.
- **Fixtures** come from the iPhone (`dump-fixture` for iOS), never hand-written.
  - Two exist: `tests/fixtures/ios-device/settings_general.json` and `testapp_main.json`.
  - Plus six WDA simulator trees in `tests/fixtures/ios-wda/`, which use a different shape (input for mapping tests only).
- **Mapping tests** (`tests/test_ios_mapping.py`) run offline against the fixtures: windows, paired switches, scroll bars, chevrons, off-screen rows, dedupe, pixel windows.
- **e2e** needs `DROIDCTL_IOS_UDID`:
  - setup → snapshot → each control path → typing → scroll-to → rotation → lock refusal (the user locks the phone; a manual step) → teardown leaves no `dev.droidctl.*` app.
- **Soak:** the spike's `soak.py` workload against the product runner (30 min, 0 errors and flat memory as the bar).

---

## 19. Open questions and unverified items

| item | status | how to close it |
|---|---|---|
| **Free profile expiry in practice** | pending, apps left on the phone; expires 2026-10-27T10:28Z | re-run the launch and `test-without-building` after the date, record the errors, uninstall |
| **iOS 26 on a device** | not available (only iOS 18.7.8 hardware); iOS 26.2 simulator matched on every test | needs hardware |
| Settle design (§13) | **decided and measured 2026-10-21**: 42/42 on the iPhone | re-check on real apps; timeout default above 2.8 s unmeasured |
| Minimum iOS version | **decided 2026-10-21 (user): iOS 17+**, the `devicectl` era. Only 18.7.8 tested on a device, 26.2 on the simulator | test whatever hardware exists |
| Paid team behaviour | **decided 2026-10-21 (user): free and paid teams are both supported**, the free team is the documented path; paid is inferred, unverified | test if the user gets one |
| Secure text fields, `back` replacement (navigation bar back button by role), the number pad's dismissal | unmeasured (default-keyboard dismissal measured, §11.3) | I2 test-app scenarios |
| Settle after an AX scroll action | the 200 ms floor is a proposal from 16/16 DTA timings | run it on the I2 runner |
| Full Keyboard Access as a touch-free activation route | not tried: it needs a Settings toggle on the phone | only with the user's OK; low expectations |
| Multiple scenes / iPad split view, foldables | not tested | later |
| `sendString` without focus | unmeasured (must never happen; the focus check prevents it) | — |
| AX actions behind the lock screen on other iOS versions | the guard makes it moot; seen on 18.7.8 | — |
| Linux / no Xcode (go-ios as the IDE peer) | design only | an optional later milestone |
| Wi-Fi debugging | out of scope (loopback only) | — |
| App Store apps' trees (third-party UIKit, React Native, Flutter) | **one real app done:** a third-party UIKit app's test build (pre-login only): fully readable, 76 ms snapshots, rich identifiers, all controls take the checked touch, settle 420–575 ms; five new mapping gaps (`research/ios-decision.md` §13) | more apps, including post-login flows with test credentials |

---

## 20. Proposed milestones (real iPhone first)

The code-level plan (seams, wiring, tests, work order) is `IOS-INTEGRATION.md`.

Each ends with measured numbers recorded in PLAN.md, as on Android.

- **I1: runner + transport + snapshot on the iPhone.**
  - The `ios/runner` project: rules R1–R12, `ping` with the build ID, the `tree` daemon snapshot, `screen_state`, `quit`.
  - `ios/transport.py` (usbmuxd client, no iproxy) and `ios/runner.py` (build with the user's team, install, detached start, adopt, build-ID check, typed start errors).
  - `ios/mapping.py` plus mapping tests on the device fixtures.
  - `droidctl devices` lists iPhones; `droidctl snapshot` works end to end on the iPhone.
  - `dump-fixture` for iOS, plus new fixtures from a few real apps.
- **I2: actions.**
  - `act` with the AX-first policy, the hit-test assertion and the single touch fallback.
  - Gestures with the rotation table; typing (focus check, `sendString`, `setAttribute`, read-back); scroll / scroll-to.
  - Settle on the runner with the §13.3 rule; `wait`; events; alerts as a dialog; `current`; the lock guard on every input.
  - The iOS test app (TESTAPP scenarios) and e2e with DTA pulled from the phone.
- **I3: setup / teardown / doctor for real users.**
  - Prerequisite checks with typed errors (§3), signing for free and paid teams, profile expiry from `embedded.mobileprovision`.
  - `teardown` uninstalls everything and discloses the rest; `doctor` reports trust, expiry, host sleep/thermal state and the Xcode path.
- **I4: the rest of the command surface.**
  - `launch` / `apps` / `screenshot` (JPEG on the phone, `--marks` in points with rotation) / `logs` (os_trace, design open).
  - MCP and the AGENTS.md capability notes (no `back` and no events on iOS), the soak bar.
- **I5 (optional):** the simulator as a target (same runner, `simctl`, prebuilt unsigned runner), then go-ios for Linux / no Xcode, then the S5 in-simulator helper if simulator speed matters.

**Before I1 (housekeeping): done 2026-10-21.**
- The work stays on the branch `ios-research` (user decision); it is not merged into `main`.
- CLAUDE.md has the iOS lines in "Read first" and an updated Environment section.

---

## Appendix A. Spike files (`research/ios-spike/`)
- `Runner/S2Server.m` (421 lines): the reference for every runner call in §7.
  - The NDJSON server, token, per-request pools, the `recordIssue:` override, the lock guard, the daemon snapshot, act, hit, fetch, setattr, sendstring, dlaunch, synthesized tap/swipe/pinch, orientation, stats.
  - It also has XCTest-waiting commands (`launch`, `activate`, `type`, `tap`), kept only to measure them. **Don't port those.**
- `Runner/S1Tests.m`: the three AX action routes and the action-number sweep.
- `TestApp/App.swift`: the DTA ground-truth app (UIKit + SwiftUI + WKWebView + pinch + table + alert/permission + rotation logging).
- `S1.xcodeproj`: a hand-written project (objectVersion 77, filesystem-synchronized groups); scheme `Runner`; settings `S1_TEAM`, `S1_BUNDLE_PREFIX`.
- `s2.py` (client), `bench.py` and `exp2.py`–`exp6.py` (the measurement rounds), `soak.py`, `cycles.py` (lock cycles), `runner2raw.py` / `ios2raw.py` (tree → raw), `simax.m` (S5 helper), `serve-dev.sh` / `run-sim.sh` / `sim-build-start.sh` (start scripts).
- These paths point at the original scratchpad, and the token comes from an uncommitted `.token`.
