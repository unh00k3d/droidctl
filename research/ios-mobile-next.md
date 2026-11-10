# mobile-next (mobile-mcp + mobilecli) on iOS: analysis for droidctl

Sources: shallow clones of `github.com/mobile-next/mobile-mcp` at `d4a83fa` (v1.0.11, 2026-10-14) and `github.com/mobile-next/mobilecli` at `9243a76` (1.0.20, 2026-10-14). Paths below start at those roots (`mobile-mcp/…`, `mobilecli/…`). I only read code; I built and ran nothing. The iOS on-device runner (`mobile-next/devicekit-ios`) is a **separate repo and not in either clone**, so anything about its internals is inferred from the host-side client, and labelled that way.

This builds on `research/repos-a.md` §4, which covers the Android side, the positional-ref anti-pattern, the screenshot scale bug and the batch tool, and on `research/ios.md` §5.5. I don't repeat those here.

**Licensing, read first.**
- mobile-mcp: Apache-2.0 (`mobile-mcp/LICENSE:1-3`, `package.json` `"license": "Apache-2.0"`). We can port code with NOTICE attribution.
- mobilecli: **"Functional Source License, Version 1.1, ALv2 Future License"** (`mobilecli/LICENSE:1-3`; the README badge says `FSL-1.1-Apache-2.0`, `README.md:15`). The license allows use "for any Permitted Purpose", which excludes a "Competing Use": making the software available "in a commercial product or service that … offers the same or substantially similar functionality" (`LICENSE:16-21`). Each version becomes Apache-2.0 "on the second anniversary of the Software's initial availability" (`LICENSE:44-45`). The copyright line is "2025-2026" (`LICENSE:3`), so no version is Apache yet. droidctl is a similar tool, so **mobilecli is ideas only**: never copy its code, as CLAUDE.md already says.

---

## 1. Overview

### 1.1 How the two relate
```
MCP client ──stdio/http──> mobile-mcp (TypeScript, MCP SDK)          mobile-mcp/src/server.ts
                              │ execFileSync per call                mobile-mcp/src/mobilecli.ts:84-108
                              ▼
                           mobilecli CLI (Go) ──unix socket──> mobilecli daemon (auto-started, 30 min idle)
                                                                     mobilecli/cli/client.go:21,35-63
                                                                     mobilecli/daemon/server.go:64-102
                              │
          ┌───────────────────┼────────────────────────────┐
          ▼                   ▼                            ▼
  Android: DeviceKit APK   iOS simulator: devicekit-ios    iOS real device: devicekit-ios
  (JSON-RPC over adb fwd)  XCUITest runner, launched with  runner, launched through go-ios
                           `xcrun simctl launch`           testmanagerd, over the iOS 17+ tunnel
```
- **mobile-mcp now shells out to mobilecli for everything** unless `MOBILEMCP_LEGACY_ROBOT=1` (`mobile-mcp/src/server.ts:253-311`). `MobileDevice` (`mobile-mcp/src/mobile-device.ts:106-377`) implements the `Robot` interface by running `mobilecli <verb> --device ID` and parsing its JSON envelope.
- The binary comes from an npm dependency, `mobilewright` (`package.json`: `"mobilewright": "0.0.66"`), "which bundles mobilecli" (`CHANGELOG.md` 1.0.10). It is resolved from `node_modules/@mobilenext/mobilecli-<os>-<arch>` (`mobilecli.ts:110-155`). `MOBILECLI_PATH` overrides it.
- Each MCP tool call spawns a mobilecli process. That process then hands the call to the resident daemon (`cli/client.go:41-63`), which holds the agent connections. This is the same thin-client-plus-daemon shape droidctl has, except that mobile-mcp is a client of the CLI, not of the daemon socket.
- A screenshot also costs a second spawn, `device info`, to get the screen size for the mapping text (`server.ts:963` → `mobile-device.ts:128-134`).

### 1.2 iOS backends
| path | tree / actions | process start | transport |
|---|---|---|---|
| **mobilecli, simulator** (default) | `devicekit-ios` runner, JSON-RPC `device.dump.ui`, `device.io.tap`, … (`devices/devicekit/*.go`) | `xcrun simctl launch --stderr=… <udid> <…devicekit-iosUITests.xctrunner>` with `SIMCTL_CHILD_DEVICEKIT_LISTEN_PORT` (`devices/simulator.go:216-240,582-631`) | HTTP `localhost:13001-13200/rpc` (`simulator.go:34-35`) |
| **mobilecli, real device** | the same runner (`devices/ios.go:41`) | go-ios `testmanagerd.RunTestWithConfig` in a goroutine, no Xcode (`ios.go:631-689`) | iOS 17+ go-ios tunnel (`ios.go:424-479`), then a go-ios port forward from local 8100-8299 to device port 12004 (`ios.go:36-39,557-596`) |
| **mobile-mcp legacy, real device** | stock Appium WebDriverAgent, `GET /source?format=json`, W3C `/actions` (`mobile-mcp/src/webdriver-agent.ts`) | the user starts WDA, the tunnel and `ios forward 8100` by hand. It checks the ports and fails with a wiki link (`src/ios.ts:70-92`) | `localhost:8100` |
| **mobile-mcp legacy, simulator** | WDA as above | `xcrun simctl launch … com.facebook.WebDriverAgentRunner.xctrunner`, then polls `/status` for 10 s (`src/iphone-simulator.ts:37-69`) | `localhost:8100` |

- The runner is mobile-next's own, not WDA. The code still calls it "WebDriverAgent" in logs and comments (`ios.go:494-500`, `requests.go:111-126`), and its HTTP surface is JSON-RPC 2.0 on `/rpc` plus `/health` (`devicekit/requests.go:44-93`, `status.go:10-31`), not WebDriver. Inferred: it is a WDA-style XCUITest bundle with its own protocol.
- **The same RPC verbs run on both platforms.** `device.io.tap` / `device.dump.ui` / `device.io.text` are sent to the Android agent (`devices/android.go:567-576`) and to the iOS runner (`devicekit/tap.go:3-11`, `source.go:151`). Counted across `devices/`, 39 distinct `device.*` method names are in use.
- **A second iOS agent, for webviews only:** an Objective-C HTTP/JSON-RPC server injected into the foreground app. On the simulator it is a dylib loaded with `lldb -p PID -o 'expr dlopen(…)'` (`devices/ios_webview.go:140-175`). On a real device it is an LLDB expression run through the go-ios debugserver proxy (`ios_device_agent.go:21-37,140-260`). Inferred: this works only on debuggable (`get-task-allow`) builds, and it pauses the app for a while (the Flutter comment says ~20 s, `ios_device_flutter.go:26`).
- **Flutter:** on a debug or profile Flutter app, `DumpSource` reads the Dart VM render tree instead of the accessibility tree (`ios.go:1164-1173`, `simulator.go:936-945`).

### 1.3 Activity
- **mobile-mcp:** 64 releases in `CHANGELOG.md`, from 0.0.11 (2025-04-06) to 1.0.11 (2026-10-14), roughly three a week lately. **mobilecli:** 96 releases; the latest is 1.0.20 (2026-10-14), and 1.0.13 to 1.0.20 shipped in three weeks.
- The iOS fixes are recent: no runner relaunch per call (1.0.5, `mobilecli#406`), sliders/alerts/lists kept (1.0.5), the agent auto-installed on simulators (1.0.11, `server.ts:296-305`).
- Both are shallow clones, so I counted no commits.
- **Telemetry is on by default** in mobile-mcp: a PostHog event per tool call with a hashed hostname (`server.ts:155,175-215`), a scarf.sh pixel (`:222-231`), and `get_more_tools`, which sends the agent's stated goal to the developers (`:313-331`). Setting `MOBILEMCP_DISABLE_TELEMETRY` turns all three off.

---

## 2. The iOS path, read in code

### 2.1 Element list
- **Tree:** the runner's `device.dump.ui {format: json}` returns a nested XCUITest snapshot of `type, label, name, value, placeholderValue, rawIdentifier, enabled, selected, hasFocus, rect, children` (`devicekit/source.go:21-33,148-166`). The dump timeout is 55 s, "to snapshot a heavy screen" (`requests.go:40-42`). `--format raw` passes the runner's tree through unfiltered (`source.go:129-146`).
- **Filter** (`source.go:49-127`). It keeps:
  - one of 26 element types: `TextField, TextView, Button, Switch, Icon, SearchField, StaticText, Image, SecureTextField, WebView, Slider, ProgressIndicator, ActivityIndicator, Picker, PickerWheel, Link, Tab, TabBar, NavigationBar, Toolbar, Alert, Sheet, Cell, Table, CollectionView, ScrollView` (`:49-56`);
  - **or** any element with a non-empty `accessibilityIdentifier` (`:76-78`, mobilecli#341);
  - **and** only if `isVisible(rect)`, which means `x>=0 && y>=0 && w>0 && h>0` (`:35-37`);
  - **and** only if it has a label, name, identifier or placeholder, or its type is in the "always" list (inputs, buttons, switches, sliders, pickers, webviews) (`:58-61,84-88`).

  Rejected nodes have their children hoisted (`:63-82`). The hierarchy of kept nodes is preserved (`Children`, `:106`), and nested same-rect WebViews collapse (`:116-124`). A `Switch` with value `"1"` becomes `checked` (`:109-114`).
- **What the filter misses:**
  - It never reads XCUITest's `isVisible` or hittability (the struct has no such field, `:21-33`). So a cell scrolled **below** the screen (`y > screen height`) or hidden behind a sheet or alert is listed with real coordinates. Only negative x/y is rejected. The legacy WDA path at least required `isVisible=="1"` (`mobile-mcp/src/webdriver-agent.ts:236-247`).
  - It does nothing about modality: when an `Alert` is up, the elements behind it stay listed. Inferred: XCUITest's snapshot normally contains both.
  - It has no notion of the keyboard. `dump ui --full` claims to add the on-screen keyboard (`cli/dump.go:83-84`), but the iOS `DumpSource` ignores its options (`ios.go:1164`, `simulator.go:936`: `_ DumpOptions`).
  - Unlabeled, identifier-less `Cell`s, `Image`s and `StaticText`s are dropped (`:84-88`). A tappable row whose label lives on a child `StaticText` therefore appears as the child text only (no row merge, as on Android, `repos-a.md` §4.3).
- **Fields the LLM sees.** There are two different text formats:
  - **mobilecli `snapshot` / `dump ui --format text`** (`types/screen_text.go:8-76`): indented to show nesting, `@e7 [Button] "Add" #add-button (disabled)`. The description is the first non-empty of text/label/name/value/placeholder (`:49-56`). It prints **no coordinates** and only non-default states.
  - **mobile-mcp `mobile_list_elements_on_screen`** (`src/format-elements.ts:9-58,97-105`): **flattened** (`mobile-device.ts:83-104`), one line per element. It prints `label= name= value= id=` side by side (on iOS, `label` and `name` are usually the same string, inferred from XCUITest semantics, which doubles the tokens), plus `at=x,y size=WxH` (top-left corner) in **points**.
- **Token size (estimate, not measured: I have no captured iOS tree and the unit-test trees are hand-written):** a mobile-mcp line is ~15-30 tokens. A typical iOS list screen with ~40 kept elements comes to roughly 0.8-1.2k tokens, about the same as droidctl's Android snapshot. mobilecli's own text format is perhaps 30% smaller (no coordinates, one description field).

### 2.2 Actions
| action | how (mobilecli → runner) | element or coordinate? |
|---|---|---|
| tap | `device.io.tap {x,y}` (`devicekit/tap.go:3-11`) | **coordinate**. `@eN` is resolved by re-dumping, renumbering in DFS pre-order and tapping the rect centre (`commands/input.go:136-182`: "Refs are positional against a fresh dump; there is no staleness tracking") |
| double tap | mobile-mcp resolves the ref once, then sends two `io tap` (`mobile-device.ts:268-284`) | coordinate |
| long press | `device.io.longpress {x,y,duration s}` (`longpress.go:3-12`) | coordinate, ref → centre as above |
| swipe | `device.io.swipe {x1,y1,x2,y2,duration?}` (`swipe.go:3-18`). mobile-mcp's directional swipe is a fixed 400-unit stroke through the screen centre (`mobile-device.ts:136-167`), and on iOS those are **points** | coordinate |
| gesture / pinch | `device.io.gesture` with W3C-style pointer actions (`devicekit/gesture.go`, `commands/input.go:96-134`) | coordinate |
| type | `device.io.text {text}` into whatever has focus (`send-keys.go:3-10`). Nothing targets a field and nothing reads the value back | none |
| button | only `HOME, VOLUME_UP, VOLUME_DOWN, LOCK`, and `ENTER` sent as `"\n"` text (`devicekit/press-button.go:5-28`). There is no BACK on iOS, and BACKSPACE is "typed as \b; unverified on a device" (`mobile-mcp/plugin/hooks/keys.ts:20`) | n/a |
| key combos | `device.io.keys` (`ios.go:882-884`) | n/a |

- The legacy WDA robot does the same with W3C `/actions`, and **creates and deletes a WebDriver session around every action** (`webdriver-agent.ts:71-77`). That is three HTTP round trips per tap (inferred cost: tens of ms each on USB).
- Nothing in either code base calls an element-level XCUITest action (`XCUIElement.tap()`, `typeText` on an element, `adjust(toPickerWheelValue:)`). Inferred: the runner could, but the wire protocol only carries coordinates.

### 2.3 Screenshots and streaming
- **Screenshot:** the runner's `device.screenshot {format: png}` returns base64 (`devicekit/screenshot.go:10-34`), and the host scales, crops (`--clip` in points, converted to pixels) and turns it into a JPEG (`utils/image.go:89-130`). The simulator uses the runner too, not `simctl io screenshot` (`simulator.go:96-102`). Legacy: WDA `/screenshot` (`webdriver-agent.ts:295-300`).
- **Coordinate mapping:** mobile-mcp tells the model in prose to "multiply its x by R" (`src/coordinate-mapping.ts:18-30`), noting that "on iOS the screen is measured in points while a full-size screenshot is in pixels" (`:11-14`). The tap tools' schemas still say "in pixels" (`server.ts:683-684,714-715,745-746`), which is wrong for iOS, where they are points.
- **Streaming:** two paths.
  - **MJPEG** from the runner at `/mjpeg` on the agent port (`ios.go:1150-1162`, `simulator.go:955-958`).
  - **H.264** on a real device, through a **ReplayKit broadcast upload extension** in a companion app (`ios.go:1625-1696`). mobilecli launches the app, then drives the system broadcast picker *by UI automation*: it polls the dump for an element named `BroadcastUploadExtension`, taps its centre, then does the same for `Start Broadcast` (`ios.go:1268-1296,1298-1363`). Concurrent viewers share one broadcast (`ios.go:1127-1147`).

  The mobile-mcp `/mobile-mirror` Claude Code plugin shows that stream in a terminal pane (`CHANGELOG.md` 1.0.8, `plugin/hooks/`).

### 2.4 Apps
- **Real device** (all via go-ios services, after `startTunnel()`):
  - launch: `instruments.ProcessControl.LaunchAppWithArgs`, with `-AppleLanguages (…)` for the locale (`ios.go:762-805`);
  - terminate: `installationproxy.BrowseAllApps` maps bundle ID → `CFBundleExecutable`, then `DeviceInfoService.ProcessList`, then a kill of the first process whose **name** matches (`ios.go:807-876`);
  - list: `installationproxy.BrowseAllApps`, where `onlyLaunchable` is accepted and ignored (`ios.go:890-930`);
  - install: `zipconduit` (`ios.go:1179-1205`);
  - uninstall: `installationproxy.Uninstall` (`ios.go:1211-1242`);
  - clear data: "not supported on real iOS devices" (`ios.go:1207-1209`).
- **Simulator** (`simctl`):
  - launch, `terminate` (exit status 3 / ESRCH is mapped to "not running" vs "not installed", `simulator.go:287-343`), `listapps` (a plist, `:674-707`), `openurl` (`:669-672`), `install`/`uninstall`;
  - simulators are found by reading `~/Library/Developer/CoreSimulator/Devices`, and on non-darwin hosts the list is empty (`simulator.go:140-160`).
- **Foreground app:** the runner's active-app call, enriched with the version from the app list and the **on-screen view controller** reported as the "activity" (`devices/wda_foreground_app.go:13-45`, `CHANGELOG.md` 1.0.5).
- **Open URL:** the runner's `device.url` on a real device (`ios.go:886-888`), `simctl openurl` on a simulator. mobile-mcp refuses non-http(s) schemes by default (`server.ts:817-823`).

### 2.5 Real device vs simulator
| concern | simulator | real device |
|---|---|---|
| listing | CoreSimulator dir, darwin only (`simulator.go:140-160`) | go-ios usbmux, deduped by UDID; untrusted or locked devices are skipped, not fatal (`ios.go:165-194`) |
| agent install | download `devicekit-ios-Sim-<arch>.zip` from GitHub releases, **SHA-256 pinned** (`commands/agent.go:23,28-33,183-240`); mobile-mcp does this automatically (`server.ts:296-305`) | download `devicekit-ios-runner.ipa` (pinned), then **re-sign** with a user-supplied provisioning profile: `openssl smime` to decode it, `security find-identity`, `codesign` (`utils/resign.go:34-99,279,409,491`); `--provisioning-profile` is required (`agent.go:135-138`). The error text says "provisioning profiles require a paid Apple Developer Program membership" (`resign.go:397-405`). Inferred: `security`/`codesign` make this step macOS-only even though the runner launch is not |
| agent start | `simctl launch` with an env-var port; the port of a runner left over from a previous run is rediscovered **by parsing `ps` output for `DEVICEKIT_LISTEN_PORT=`** (`simulator.go:556-580,865-934`) | testmanagerd via go-ios (`ios.go:631-689`); afterwards, if the runner is in front, press HOME (`ios.go:610-627`) |
| boot / shutdown | `simctl boot`/`bootstatus`/`shutdown` (`simulator.go:436-495,521-554`) | "not supported for real iOS devices" (`ios.go:227-233`) |
| tunnel | none | iOS ≥17: go-ios tunnel manager, a 10 s readiness poll (`ios.go:424-479`) |

### 2.6 Starting the runner, step by step (real device, `ios.go:481-538`)
1. `GET /health` on the cached client. If it answers, the runner is reused (since 1.0.5 it is no longer torn down per call).
2. Find the installed runner by **bundle-ID suffix**, because re-signing adds a team prefix (`:540-555`).
3. Start the tunnel if iOS ≥17.
4. Forward a free local port (8100-8299) to device port 12004.
5. Check `/health` again: an earlier mobilecli process may have left the runner running.
6. `RunTestWithConfig` in a goroutine, then poll `/health` every 500 ms for 20 s. Fatal stderr markers (`Library not loaded:`, `Termination Reason:`, `Fatal error:`) fail fast (`requests.go:95-150`). On the simulator the stderr file is read back (`simulator.go:599-628`), and on failure the runner is terminated (`:620-626`).

Every command runs this through `FindDeviceWithAgent` (`commands/commands.go:240-254`).

### 2.7 Errors
- **mobilecli:** an envelope `{status: "ok"|"error", data, error: string}` (`commands/commands.go:13-55`). The JSON-RPC server uses only the generic codes −32700…−32000 (`server/server.go:29-44`). There are **no error kinds**: "agent is not installed, use 'mobilecli agent install …'" (`ios.go:554`), "simulator is offline, use 'mobilecli device boot …'" (`simulator.go:532`) and "ref @e5 not found on current screen" (`commands/input.go:177-179`) are all plain strings.
- **mobile-mcp:** an `ActionableError` becomes `"<msg>. Please fix the issue and try again."` with `isError: true`; anything else becomes `Error: <msg>` (`server.ts:158-173`). An empty dump (screen off or locked) is turned into an actionable hint (`mobile-device.ts:295-304`).
- Error messages are good at saying the next step: they name the exact command to run. Programmatically they are poor: an agent can't branch on `stale-ref` vs `not-installed`.

---

## 3. Feature matrix against droidctl
Legend: **M** = mobile-mcp, **C** = mobilecli, **D** = droidctl. "Better" is judged by an agent's success rate and safety, not by breadth.

### 3.1 Device API (droidctl `PROTOCOL.md` methods vs their agent RPCs)
| droidctl method | M / C equivalent | better | why |
|---|---|---|---|
| `ping` | `/health`, `device.info` (`status.go`, `ios.go:936-959`) | D | ours also carries the service state, generation and screen state |
| `tree` (all windows, handles, flags, actions, collection/range) | `device.dump.ui` (filtered on the host, no handles, no actions, no scrollable/editable flags) | D | we have handles per dump, custom actions, scroll hints and window types |
| `act {handle, gen, action, settle}` | none; everything is `device.io.tap {x,y}` | D | node actions plus staleness by dump id; they have no element action on the wire |
| `gen` | none | D | — |
| `gesture` | `device.io.tap/longpress/swipe/gesture` | tie | both inject pointer paths; their W3C-style multi-finger `gesture` is a clean shape |
| `global` | `device.io.button` (iOS: HOME/LOCK/volume only) | D (Android) | iOS has no BACK; on iOS the parity gap is real, not their fault |
| `wait_idle`, `wait_for` | none (Android's `dump.ui` has a `WaitUntilIdle` param, `android_device_server.go:347`; iOS has nothing) | D | — |
| `subscribe`/`events` | none (logs only: `device logs` via ostrace, `ios.go:1764-1834`) | D | — |
| `screenshot` | `device.screenshot` + host crop/scale | tie | their element crop (`--clip`, points→pixels) is the same idea as our `shot --crop REF` |
| `current` | `device.apps.foreground` (+ view controller) | tie | the view controller name as "activity" is a good iOS analogue of our activity |
| `clipboard` | `device.clipboard.get/set/clear` | tie | — |
| — | `device.location.set`, `device.io.orientation.*`, `device.io.hinge.set`, `device.settings.apply`, `device.crashes.*`, `device.screenrecord`, `device.webview.*` | C | breadth we don't have (location, orientation, crashes, recording, webviews) |

### 3.2 CLI commands
| droidctl | mobilecli | better | why |
|---|---|---|---|
| `devices` | `devices [--platform --type --include-offline]` | C | one list across Android, iOS simulators and real iOS devices, plus a remote fleet |
| `setup`/`teardown`/`doctor`/`diagnose` | `agent install/status/uninstall`, `daemon status` | D | we restore settings and classify blank screens; they install and check status only |
| `snapshot` (spatial, diff, find, in) | `snapshot` = `dump ui --format text`; `dump ui --format json|raw` | D | pruning, row merge, regions, diff, carry_refs |
| `where`, `current` | `apps foreground` | D | — |
| `shot [--marks --crop]` | `screenshot [--format --quality --scale --max-size --clip]`, `screencapture` (MJPEG/AVC) | C on streaming, D on marks | — |
| `tap/long-press/type/scroll/scroll-to/swipe/action/set/focus/expand/collapse/dismiss/gesture` | `io tap/longpress/swipe/pinch/text/keys/button` | D | element-based and verified, vs coordinates |
| `back/home/recents/…/press` | `io button`, `io keys` | tie | — |
| `wait`, `watch`, `mark`, `logs` | `device logs` (filters `key=value`) | D | waits/events; their structured log filters (`process`, `subsystem`, `category`) are a good iOS `logs` shape |
| `launch/stop-app/apps/install/open-url` | `apps launch/terminate/list/install/uninstall/clear/path/foreground`, `url` | C | `--locale` per launch, `clear`, `path` |
| `run`, `serve --stdio`, `mcp` | `server start` (JSON-RPC HTTP+WS, `localhost:12000`, `cli/server.go:8`), `daemon` | tie | — |
| — | `fs push/pull/ls/mkdir/rm` (app container), `device reboot/boot/shutdown/orientation/fold/location/settings`, `crashes`, `screenrecord`, `webview *`, `remote allocate/release`, `auth` | C | breadth (simulator lifecycle, app container files, crash reports) |

### 3.3 MCP tools, one for one
mobile-mcp registers **34** tools (`server.ts:319-1340`); droidctl registers **12** (`droidctl/mcp.py:21-36`).
| droidctl tool | mobile-mcp tool(s) | better | why |
|---|---|---|---|
| `snapshot` | `mobile_list_elements_on_screen` | D | stable refs, pruning, regions; theirs is flat with top-left `at=` |
| `tap` | `mobile_click_on_screen_at_coordinates`, `mobile_double_tap_on_screen` | D | ours fails with `stale-ref`/`ambiguous`; theirs taps the centre of whatever is Nth now |
| `type` | `mobile_type_keys {text, submit}` | D | ours targets a field and reads the value back; theirs types into focus |
| `scroll` | — (only swipe) | D | — |
| `swipe` | `mobile_swipe_on_screen` | tie | — |
| `action` | — | D | custom actions (iOS analogue: `accessibilityCustomActions`, inferred) |
| `key` | `mobile_press_button` | tie | both list the per-platform keys in the schema (`server.ts:792-801`) |
| `wait` | — | D | they have no wait at all |
| `launch` | `mobile_launch_app {packageName, locale}` | M | `locale` is useful on iOS (`-AppleLanguages`) |
| `shot` | `mobile_take_screenshot`, `mobile_save_screenshot` | D | ours is for pixels and marks; theirs invites coordinate taps ("multiply by R") |
| `logs` | `mobile_get_device_logs` | tie | — |
| `devices` | `mobile_list_available_devices` + 4 remote-fleet tools | M (breadth) | one list across both platforms |
| — | `mobile_list_apps`, `mobile_get_foreground_app`, `mobile_terminate_app`, `mobile_install_app`, `mobile_uninstall_app`, `mobile_open_url`, `mobile_get_screen_size`, `mobile_set/get_orientation`, `mobile_fold_device`, `mobile_set_location`, `mobile_clipboard`, `mobile_start/stop_screen_recording`, `mobile_list_crashes`, `mobile_get_crash`, `mobile_batch_commands`, `get_more_tools` | M (breadth), D (tokens) | each definition costs context every turn; our `run` isn't an MCP tool (theirs, `batch`, is) |

### 3.4 Behaviour
| capability | mobile-mcp | mobilecli | droidctl | better |
|---|---|---|---|---|
| snapshot compaction | flatten + `at=/size=` + label/name duplication (`format-elements.ts`) | type + id + visibility filter, hierarchy kept, no coordinates (`source.go`, `screen_text.go`) | prune, row merge, dedupe, regions, `--max` | D |
| visibility / occlusion | legacy only: `isVisible=="1"` | x,y ≥ 0 only; no below-screen check, no occlusion | clip, ≥10% visible, drawingOrder hit test → `covered` | D |
| refs | `@eN` passed through | DFS pre-order index in a fresh dump (`types/screen.go:40-54`) | fingerprint, re-resolve in tiers, `carry_refs` | D |
| staleness | none | none ("no staleness tracking", `input.go:168`) | `stale-ref` (gone/shifted/occupied), signature check | D |
| action verification | returns "Clicked on element @e5" (`server.ts:696`) | returns "Tapped … at (x,y)" (`input.go:161-163`) | settle + diff, `handled`, `no-change`, `--expect-change` | D |
| typed errors | `isError` + prose | `status: error` + prose | `ERROR_KINDS` (21 kinds, `droidctl/core.py:55`) | D |
| batch | no settle between steps (`server.ts:1306-1330`) | — | `run` settles every step | D |
| setup safety | auto-installs the agent on simulators | pinned SHA-256 artifacts, suffix match for re-signed bundle IDs | append-only settings, teardown restores | D (Android); their pinned-hash download is good |
| iOS at all | yes | yes | no | M/C |

---

## 4. What to copy as ideas, and what they do badly

### 4.1 Copy (as ideas; mobilecli code is FSL)
1. **No Xcode at runtime.** The runner starts with `simctl launch` plus `SIMCTL_CHILD_*` env on a simulator (`simulator.go:216-240`) and with go-ios testmanagerd on a device (`ios.go:657-674`). Only a one-time re-sign needs Apple tooling. This is the evidence `ios.md` §5.5 noted. Inferred: a droidctl iOS runner can be launched the same way from Python (pymobiledevice3 is GPL-3, so go-ios-as-a-subprocess or our own minimal DTX client would need a licensing decision).
2. **Prebuilt runner, pinned by hash, downloaded on `setup`** (`agent.go:28-33,183-221`). The simulator build needs no signing, so `setup` on a simulator needs no Apple account. That matches our "the APK is committed" model.
3. **One wire vocabulary for both agents** (`device.io.tap`, `device.dump.ui` on the APK and on the runner). Our version: the iOS runner speaks droidctl's `PROTOCOL.md` (`ping/tree/act/gesture/global/screenshot/current/wait_*`), so `snapshot.py`/`resolve.py`/`act.py` stay platform-neutral.
4. **Reuse a live runner before starting one:** `/health` first, then rediscover the port (`ios.go:494-519`, `simulator.go:556-580`). Fail fast on fatal stderr markers instead of a 20 s timeout (`requests.go:146-150`). Background the runner with HOME if it came to the front (`ios.go:613-627`). droidctl's analogue: Backend B's relaunch-on-ping-failure.
5. **Find the runner by bundle-ID suffix,** because re-signing changes the prefix (`ios.go:540-555`).
6. **Always keep an element that has an `accessibilityIdentifier`,** whatever its type (`source.go:76-78`). That is the iOS twin of our "keep nodes with an id inside rows".
7. **Map XCUITest types onto roles** (Switch value `"1"` → checked, `source.go:109-114`; collapse nested same-rect WebViews, `:116-124`). droidctl needs a role table for `XCUIElementType*` → `button/input/switch/list/row/…`.
8. **Foreground "activity" = the top view controller** (`wda_foreground_app.go:39`). It gives our screen signature an iOS equivalent of `activity`.
9. **Per-launch locale** (`-AppleLanguages`, `ios.go:794-796`) and per-launch env, cheap on iOS and useful for tests.
10. **Points throughout.** Rects, taps and `screenSize` are all in points (`ios.go:950-955`), and only the screenshot is in pixels, with the crop converting (`image.go:97-101`). droidctl on iOS should print and accept points, and convert only for `shot --marks`.
11. **Error text names the exact next command** ("use 'mobilecli agent install --device X --provisioning-profile <path>'", `ios.go:554`). We already do this; keep it for iOS `setup`.
12. **Structured device-log filters** (`process`, `subsystem`, `category`, `key!=value`, `server.ts:27`) for an iOS `logs`, where os_log replaces logcat tags.

### 4.2 What they do badly
- **They violate "never guess by position", on iOS too.** The iOS ref is a DFS index into a fresh dump (`types/screen.go:40-54`), resolved to a rect centre and tapped (`input.go:166-182`). Any content change (a lazy cell, a keyboard, an alert) silently retargets `@eN`. The server instructions claim "tapping by ref survives layout differences" (`server.ts:93-95`), and the list tool says "Refs and coordinates stay valid as long as the screen does not change" (`:775`). Both are untrue in exactly the cases that matter.
- **Only coordinates go over the wire.** Even with a perfect resolver, a tap is a synthesized touch at a centre, so a covered element is "tapped" through whatever is on top. XCUITest's own element `tap()` at least checks hittability (inferred from Apple's XCUIElement docs, not this code).
- **Offscreen and occluded elements are listed** (`source.go:35-37`): there is no `y < screenHeight`, no `isVisible`, no alert modality. An agent will "tap" a cell below the fold and hit the tab bar.
- **They lean on screenshots:** `mobile_take_screenshot` returns prose telling the model to multiply by R (`coordinate-mapping.ts:28-29`), and the tap schemas say "pixels" while iOS takes points (`server.ts:683-684`).
- **Typing is blind:** no target field, no read-back, ENTER is `"\n"` (`press-button.go:13-15`), BACKSPACE is "unverified on a device" (`plugin/hooks/keys.ts:20`).
- **Nothing verifies the outcome** of any action (§3.4), and the batch tool has no settle between steps.
- **Termination kills by executable name** from the process list (`ios.go:865-875`). Inferred: two bundles with the same `CFBundleExecutable` (common for "App"-named Xcode templates) could kill the wrong one.
- **The legacy WDA robot opens a WebDriver session per action** (`webdriver-agent.ts:71-77`). Its keep filter uses `!== null` on fields that may be `undefined` (`:247`), so inferred, it keeps more than intended.
- **The broadcast picker is automated by display name** (`ios.go:1272-1296`). That breaks on a localized iOS (inferred: "Start Broadcast" is a localized system string).
- **Port discovery by `ps` env scraping** (`simulator.go:865-934`) is fragile. droidctl would rather have the runner write its port to a known file in the simulator's data dir, or use a fixed per-UDID port.
- **Unit-test trees are hand-written** ("A realistic subtree as returned by WDA", `devicekit/source_test.go:19-22`). That is exactly what CLAUDE.md forbids for us.
- **Telemetry and phone-home are on by default** (`server.ts:175-231,313-331`). droidctl has none, and should keep it that way.

### 4.3 Verdicts
| technique | verdict |
|---|---|
| own XCUITest runner speaking a small JSON-RPC (not WebDriver) | **adopt the shape**; ours speaks `PROTOCOL.md` and adds element handles plus `act` |
| `simctl launch` + `SIMCTL_CHILD_` env for the simulator runner | **adopt** (idea) |
| go-ios testmanagerd launch on devices, no Xcode | **adopt the idea**; the library choice is a licensing gate (go-ios is MIT, inferred from its repo, not checked here; pymobiledevice3 is GPL-3) |
| prebuilt runner, pinned SHA-256, re-sign with the user's profile | **adopt** (simulator: no signing; device: user-supplied profile) |
| positional `@eN` refs, coordinate-only wire protocol | **skip** |
| visibility = `x,y >= 0` | **skip**; clip to the screen, use XCUITest `isHittable`/frame-in-window, alert modality |
| flattened text with `label=` + `name=` + `at=` | **skip**; our compact lines with one label, one role, non-default states |
| "multiply by R" screenshot mapping | **skip** |
| keep any element with an `accessibilityIdentifier` | **steal** |
| view controller as the iOS "activity" | **steal** |
| `--locale` on launch | **steal** (for iOS and Android: `cmd locale set-app-locales`, `repos-a.md` §4.6) |
| MJPEG from the runner; ReplayKit broadcast for H.264 | **note** (streaming is out of scope for v1) |
| LLDB-injected in-app agent for webviews | **skip** (debuggable builds only, pauses the app, far outside our model) |
| telemetry on by default | **skip** |

---

## 5. The cross-platform abstraction

**What they built.**
- mobilecli has one Go interface, `ControllableDevice`, with 38 methods (`devices/common.go:185-231`): identity (`ID/Name/Platform/DeviceType/Version/State`), `Tap(x,y)`, `LongPress`, `Swipe`, `Gesture`, `SendKeys`, `PressButton`, `PressKeys`, apps, `DumpSource() []ScreenElement`, screenshots, logs, crashes and app-container files.
- **Optional capabilities are separate small interfaces** found by type assertion: `LocationSettable`, `AnimationConfigurable`, `AppearanceConfigurable`, `WebViewable` (`common.go:235-260`, four interfaces).
- Unsupported operations in the main interface return an error string ("boot is not supported for real iOS devices", `ios.go:227-233`; "--activity is not supported on iOS", `ios.go:768-770`).
- Implementations: `AndroidDevice`, `IOSDevice` (real), `SimulatorDevice`, and a remote fleet device (`devices/remote.go`).
- **The normalized element type is shared**: `types.ScreenElement` (`types/screen.go:21-38`). Its comments mark the platform-specific fields: `Range` "android only", `Focused` "android tv, and ios when hasFocus is reported".
- mobile-mcp mirrors this one level up: a `Robot` interface (`src/robot.ts:63-215`) with optional methods (`tapByRef?`, `setLocation?`, `fold?`, `getLogs?`). Tools check for them and raise an `ActionableError` when one is missing (`server.ts:690-693`).

**Is it a good template for a droidctl `Device` protocol? Partly.**
- **Good:**
  - one device list keyed by `platform` + `type` (real/emulator/simulator);
  - small optional-capability interfaces instead of one fat interface full of "unsupported" stubs;
  - a normalized element struct with platform-specific optional fields;
  - the **same wire verbs on both on-device agents**, so the host code above the device layer is platform-neutral;
  - lifecycle (`StartAgent`, cleanup hooks, `ShutdownHook`) inside the device object.
- **Wrong level for us:**
  - Their seam is *coordinates plus a pre-filtered element list*. The resolver and the action semantics would have to live on the device side, or not exist, as here.
  - droidctl's seam is already one level lower and richer: `tree` (raw, with handles), `act {handle, gen, action}`, `gesture`, `global`, `wait_*`, `events`, `current`, `screenshot` (PLAN "Device API", "Device backends"). Backend B proved that an alternative implementation of that contract leaves `snapshot.py`, `resolve.py`, `act.py` and the daemon unchanged.
  - The iOS backend should therefore be **a third implementation of `PROTOCOL.md`**, not a port of `ControllableDevice`. In concrete terms (inferred design):
    - the iOS runner returns an XCUITest snapshot mapped to our raw node schema (`class` = XCUIElementType, `id` = identifier, `text`/`desc` from value/label, `hint` = placeholder, `flags` from enabled/selected/hasFocus/type, `bounds` in points, `window` from the app/alert/keyboard);
    - `act` re-finds the element on the device (by our fingerprint, uniquely, or `stale`) and calls the XCUIElement action;
    - `global` maps home/lock and has no BACK;
    - `wait_idle` uses XCUITest's quiescence or a poll on the snapshot hash (to be measured).
  - The **host** needs only what mobilecli's interface handles above the wire: per-platform lifecycle (setup/launch runner/tunnel), app management (simctl/go-ios vs adb) and device listing. That is a small `Platform` adapter next to `device.py`, with capability flags (`has_back`, `has_custom_actions`, `units: px|pt`) rather than error stubs.
- **Don't copy:** platform-specific fields that silently mean different things (their `Focused`, `Checked` on iOS derived from Switch value only). Every field in our node schema should have one meaning on both platforms, or be absent.

---

## 6. Lessons for droidctl
1. **Our differentiator holds on iOS.** The most direct competitor covers both platforms but still resolves refs by position, taps coordinates, lists offscreen elements and verifies nothing (§4.2). An iOS backend that keeps fingerprints, typed errors and settle+diff would be different in kind, not just a port.
2. **Make the iOS runner speak `PROTOCOL.md`.** It is mobilecli's best structural idea (one verb set across agents), applied at our richer seam (`tree`/`act` with handles), so the host stays platform-neutral.
3. **The wire protocol must carry element actions, not just points.** `act` re-finds the element on the device and uses XCUIElement actions. Coordinate gestures remain the explicit fallback (`--point`, `--method gesture`), as on Android.
4. **Visibility needs real rules on iOS:** clip the frame to the screen, read `isHittable` (or our own hit test from the frame and the window stack), treat alerts and sheets as modal (like our `dialog=yes`), and use the keyboard frame for occlusion. mobilecli's `x,y >= 0` rule is the cautionary example.
5. **Units are points on iOS.** Print points, accept points, convert only for screenshots and marks. Say so in the snapshot header (`units=pt`), so the "pixels" mislabel can't happen.
6. **Setup without Xcode is feasible:** a simulator runner launched with `simctl launch` + env, a device runner through testmanagerd. Ship a prebuilt runner pinned by hash; on devices, re-sign with the user's profile. Whether re-signing can avoid macOS-only tools is open (they use `codesign`/`security`).
7. **Lifecycle lessons carry over from Backend B:** reuse before launching (`/health`), relaunch on ping failure, fail fast on fatal stderr, background the runner after launch, and find it by bundle-ID suffix.
8. **iOS parity gaps are real and should be capability flags,** not silent no-ops: no BACK, no system-wide custom actions (inferred), no `takeScreenshot` vs FLAG_SECURE analogue, `clear` app data only on simulators. Report them as `unsupported` with a hint.
9. **Steal the small things:** always keep elements with an identifier, the view controller as the screen's "activity", `--locale` on launch, structured log filters, and pinned-hash artifact downloads.
10. **Don't copy their testing or their telemetry.** Capture iOS fixtures from real simulators and devices (a `dump-fixture` for iOS), never hand-write XCUITest trees, and never phone home.
11. **Licensing:** mobilecli is FSL-1.1, and droidctl would be a "Competing Use" in a commercial setting, so it is ideas only. mobile-mcp (Apache-2.0) can be ported with NOTICE, but almost all of its iOS logic now lives in mobilecli; the only Apache iOS code is the legacy WDA robot (`src/webdriver-agent.ts`, `src/ios.ts`, `src/iphone-simulator.ts`), and §4.2 says not to copy it.
