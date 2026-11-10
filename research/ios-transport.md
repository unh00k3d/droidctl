# iOS transport: pymobiledevice3 and go-ios ("the adb of iOS")

What a host gets from these two projects without Xcode, how much of droidctl's device API they cover, and which one droidctl should use.

**Sources read**
- **pymobiledevice3** (doronz88): shallow clone at `29aadfe` (2026-10-15).
- **go-ios** (danielpaulus): shallow clone at `ca2c56d` (2026-10-09), CHANGELOG top entry `1.3.2 - 2026-08-11`.

**Citation prefixes**
- `P:` is a path in the pymobiledevice3 repo. Package code lives under `P:pymobiledevice3/`, docs under `P:docs/`.
- `G:` is a path in the go-ios repo.

Everything not marked *inferred* is backed by the cited line. Nothing here was run against a device: there is no iPhone on the dev machine. Every "works" below therefore means "the code and its comments claim it works".

This report goes deeper than `research/ios.md` §I3 and §5.6 and corrects them in places. The corrections are collected in §7.

---

## TL;DR

1. **Both projects cover the "adb half" of droidctl almost completely without Xcode.** That half is:
   - devices and listen, pairing, port forwarding;
   - the iOS 17+ tunnel, DDI mounting, Developer Mode;
   - install, uninstall, launch, kill, apps and processes;
   - syslog and os_trace, crash reports, screenshots, location;
   - app-state notifications.

   Neither covers the **"APK half"** (tree, node actions, wait-for-condition on the device) except through an XCUITest runner (WDA, or go-ios's "DeviceKit") that they can *start* but do not contain.
2. **The accessibility-audit service is not a viable tree source. This is now confirmed from the code, not just suspected.**
   - It walks elements one focus move at a time, and each move costs an event round trip (`P:pymobiledevice3/services/accessibilityaudit.py:627-680`).
   - It yields a caption, a spoken description and an opaque element id. It gives no frame, no type and no hierarchy (`:21-81`).
   - go-ios tested frames on a real iPhone 13 running iOS 18.4.1. `AXFrame/AXBounds/AXPosition/AXSize` "return no value even for a live, focused element", and hit-testing by coordinate is simulator-only (`G:ios/accessibility/ax_audit.go:11-28`).
   - pymobiledevice3 dropped this path for its own settings panel because it "went silent on iOS 27's RSD shim" (`P:pymobiledevice3/remote/core_device/screen_stream.py:1706-1713`).
   - The "Activate" action exists in both projects. pymobiledevice3's docstring says it is limited to `task_for_pid-allow` targets (`accessibilityaudit.py:508-565`). go-ios exposes it as API only, with no CLI caller and no test (`G:ios/accessibility/accessibility_control.go:392-453`). **Unverified.**
3. **New since ios.md: real HID touch and keyboard injection without XCTest, on iOS 27+.** CoreDevice's `com.apple.coredevice.hid.universalhidservice` takes raw digitizer reports:
   - tap, drag, a virtual keyboard, and hardware buttons through `hid.indigo`;
   - reports are delivered only while a screen-mirroring media stream is open (`P:pymobiledevice3/remote/core_device/hid_service.py:38-109,557-639`);
   - go-ios documents the same gate: "iOS 27+: an iOS 18 device answers CoreDeviceError 9021, and below 27 the service is not in RSD at all" (`G:ios/hid/hid.go:27-31`).

   This is the iOS analogue of droidctl's `gesture`/`global`. It is coordinate-only, so on its own it does not satisfy "never guess by position" (§5).
4. **Licensing decides the shape.**
   - pymobiledevice3 is **GPL-3.0-or-later** (`P:LICENSE:1-2`, `P:pyproject.toml:6`). droidctl may only run it as a separate program (subprocess), never `import` it.
   - go-ios is **MIT** (`G:LICENSE:1-3`). droidctl may ship its static binary or port its code with attribution.
5. **Recommendation.**
   - Transport: **go-ios as an external binary that droidctl drives as a subprocess.** Use its userspace tunnel agent, which is the one no-root tunnel that *other processes* can use (`G:ios/connect.go:344-372`).
   - Optional: pymobiledevice3 as a user-installed subprocess for its extras (HID over the userspace tunnel, DDI and cryptex edge cases).
   - Do **not** reimplement the iOS 17+ stack in Python under MIT for v1. The cost is a userspace TCP/IP stack plus QUIC/TLS-PSK, RemoteXPC over HTTP/2 and DTX with NSKeyedArchiver. In go-ios that is ≈10k lines excluding gVisor (§5.3).
   - A small Python **usbmux + lockdown** client (≈600–900 lines, *estimate*) is reasonable later, for `devices`, `ping` and pairing checks.

---

## 1. Overview of each project

### 1.1 pymobiledevice3

| aspect | finding |
|---|---|
| what | "Pure Python 3 implementation for interacting with iOS devices"; CLI + Python API; Windows, Linux, macOS (`P:README.md:11-12`) |
| license | GPL-3.0 text (`P:LICENSE:1-2`); `license = "GPL-3.0-or-later"` (`P:pyproject.toml:6`) |
| size | 236 `.py` files, 78.8k lines in the package (`find … \| wc -l`) |
| deps | ~40 runtime deps, including FastAPI/uvicorn (tunneld HTTP API), IPython, `pytun-pmd3` (kernel TUN), `qh3` (QUIC), `hyperframe` (HTTP/2), `srptools`, `developer_disk_image`, `pyimg4` (`P:pyproject.toml:41-83`) |
| layering | `usbmux.py` → `lockdown.py` (pair, `start_lockdown_service` `:877`) → services (`services/*.py`) → DTX (`dtx/`, 3.6k lines) → RemoteXPC (`remote/xpc_message.py`, `remote/remotexpc.py`) → CoreDevice (`remote/core_device/*.py`) |
| tunnels | userspace PyTCP tunnel (default, in-process, no root), macOS "native" tunnel that rides `remoted`, privileged `tunneld` (`P:docs/guides/ios17-tunnels.md:29-48,72-84`) |
| iOS range | old iOS through 27.x. OpenSSL is needed for iOS < 13 (`P:docs/installation.md:63`). It tracks iOS 27.2 features (`P:docs/guides/cli-recipes.md:249`) and iPhone18,x restore captures (`P:pymobiledevice3/restore/tss.py:577`) |
| activity | very active: the clone is one day old, and iOS 27 work is dated 2026-06-15 (`P:pymobiledevice3/remote/core_device/media_stream_offer.py:275`) |
| agent tooling | ships a Claude Code plugin skill "device-operator" (`P:README.md:46-56`, `P:misc/claude-plugin/`) and a `doctor` (`P:pymobiledevice3/doctor.py`) |

**GPL-3 and an MIT tool** (FSF GPL FAQ "Mere aggregation" and "Can I use GPL-covered editors/libraries in a non-free program"; this is a legal reading, *not legal advice*):
- **Subprocess: OK.** Running `pymobiledevice3 …` as a separate program and exchanging command lines plus JSON on stdout is ordinarily two separate programs (aggregation). droidctl stays MIT.
- **`import pymobiledevice3`: not OK** for MIT droidctl. In the FSF's reading, a Python import makes a combined work, and the whole would have to be distributed under the GPL.
- **Copying code is not OK either**, even small helpers. Treat it like droidrun's AGPL Portal in CLAUDE.md: *ideas only*. Wire formats are facts and may be reimplemented from protocol knowledge, but a clean-room habit (write from Apple behaviour or the MIT go-ios, not from pymobiledevice3's source) keeps this unambiguous.
- **Do not bundle it in droidctl's wheel or list it as a hard dependency.** The user installs it themselves (`pipx install pymobiledevice3`), and droidctl detects it on PATH. Bundling GPL software in an MIT wheel is legal as an aggregate, but it drags source-offer obligations into our release process (*inferred*).

### 1.2 go-ios

| aspect | finding |
|---|---|
| what | static Go binary `ios` with JSON output by default, plus importable Go modules; aims at "Linux, Windows and Mac OS X" (`G:README.md:10-15,22-29`) |
| license | MIT (`G:LICENSE:1-3`, npm `"license": "MIT"` in `G:npm_publish/package.json`). Key deps are quic-go, gVisor netstack, go-codesign, wintun (`G:go.mod:7-31`). Shipping the binary means shipping their notices (*inferred*: gVisor is Apache-2.0, the rest are mostly MIT/BSD) |
| size | 41.6k lines of non-test Go |
| distribution | `npm install -g go-ios` (`G:README.md:11`), GitHub release binaries for Windows, Linux and macOS (`G:.github/workflows/release-canary.yml:14-103`) |
| layering | usbmux/lockdown/pair in `G:ios/*.go` (3.8k) → DTX (`ios/dtx_codec`, 1.6k) + NSKeyedArchiver (1.5k) → XPC (0.9k) over HTTP/2 (0.4k) → tunnel (4.0k, QUIC + gVisor userspace stack) → CoreDevice services (`appservice`, `display`, `hid`, `fileservice`, …) |
| tunnels | kernel TUN (`sudo`, or `CAP_NET_ADMIN` on Linux) or `--userspace` (no root); runs as an **agent** process with an HTTP info API on `127.0.0.1:28100` (`G:cmd_tunnel.go:43-47`; `G:main.go:198-202`) |
| iOS range | testmanagerd code paths for iOS 11, 12 and later (`G:ios/testmanagerd/xcuitestrunner_11.go`, `_12.go`); AX audit iOS 14–18 tested (`G:ios/accessibility/ax_audit.go:30-37`); HID and display iOS 27+ (`G:ios/hid/hid.go:1-4,27-31`; `G:ios/display/display.go:86`) |
| activity | active: commit 2026-10-09, releases in Aug 2026 (`G:CHANGELOG.md:16-30`); commercial users named (`G:README.md:15`) |
| extras | REST API (`G:restapi/`), App Store Connect signing + re-signing (`G:ios/signing/`), MDM / supervision helpers |

**Lessons for droidctl**
- go-ios is MIT, a single static binary and JSON by default, so it fits droidctl's model of "a host tool we drive and parse", like adb.
- pymobiledevice3 is the more complete and faster-moving reference, especially for iOS 26/27 CoreDevice. Its licence restricts it to an optional subprocess whose code we never copy.

---

## 2. Service map

The table is ordered like droidctl's device API. "RSD" means the iOS 17+ tunnel (RemoteServiceDiscovery).

### 2.1 Device listing, pairing, trust

| capability | pymobiledevice3 | go-ios |
|---|---|---|
| list devices | `usbmux.list_devices` `P:pymobiledevice3/usbmux.py:472`; CLI `usbmux list` `P:pymobiledevice3/cli/usbmux.py:78` | `ios list [--details]` `G:main.go:113`; `G:ios/listdevices.go` |
| attach and detach stream | usbmux `listen` (`P:pymobiledevice3/usbmux.py:259,320,385-386`) | `ios listen` `G:main.go:114`; `G:ios/listen.go:65-88` |
| pair (Trust dialog) | `LockdownClient.pair` `P:pymobiledevice3/lockdown.py:638`; CLI `lockdown pair` `P:pymobiledevice3/cli/lockdown.py:109` | `ios.Pair` `G:ios/pair.go:183` |
| pair with no dialog (supervised) | `pair_supervised(keybag)` `P:pymobiledevice3/lockdown.py:696` | `PairSupervised(p12)` `G:ios/pair.go:32` |
| validate / unpair | `validate_pairing` `:579`, `unpair` `:775` | `ios readpair` `G:main.go:134` |
| RemotePairing record over USB, no prompt | `lockdown remotepairing --pair` (`P:docs/guides/ios17-tunnels.md:231-244`) | the tunnel pairs on first use (`G:ios/tunnel/doc.go:11-32`) |

### 2.2 usbmux port forwarding (our `adb forward`)

| | pymobiledevice3 | go-ios |
|---|---|---|
| forward host→device port | `UsbmuxTcpForwarder` `P:pymobiledevice3/tcp_forwarder.py:127`; CLI `usbmux forward` `P:pymobiledevice3/cli/usbmux.py:24` | `forward.Forward` `G:ios/forward/forward.go:29`; `ios forward --port=a:b` `G:main.go:95` |
| reach a device port with no local listener | `WdaServiceClient` routes HTTP over a lockdown/usbmux connection (`P:pymobiledevice3/services/wda.py:1-7,314-400`) | `forward.StartNewProxyConnection` takes any `io.ReadWriteCloser` (`G:ios/forward/forward.go:88`) |

### 2.3 iOS 17+ tunnel (CoreDevice / RSD)

| | pymobiledevice3 | go-ios |
|---|---|---|
| **no-root path** | default **in-process userspace tunnel** (PyTCP) for iOS 17.4+ over USB through the `com.apple.internal.devicecompute.CoreDeviceProxy` lockdown service (`P:docs/guides/ios17-tunnels.md:29-48`; `P:pymobiledevice3/remote/tunnel_service.py:1451-1452`). **Only that process can use it** (`ios17-tunnels.md:74-84,282-290`) | `ios tunnel start --userspace`: a **separate agent process**. Other processes reach device ports by connecting to the agent's local TCP port and writing a 16-byte IPv6 address plus a 4-byte port (`G:ios/connect.go:344-372`). Uses the same CoreDeviceProxy (`G:ios/tunnel/tunnel_lockdown.go:15`) |
| macOS without root | `--native`: rides Apple's `remoted`/`remotepairingd` tunnel through libxpc and ctypes; the two "keep evicting" each other's single RSD connection (`ios17-tunnels.md:37-41`; `P:pymobiledevice3/remote/native_tunnel.py:854`) | `--address/--rsd-port` can reuse a tunnel macOS created (`G:main.go:203-208`) |
| privileged path | `sudo … remote tunneld`: HTTP API on `127.0.0.1:49151`, **unauthenticated** (`ios17-tunnels.md:86-104,139-142`); `sudo … lockdown start-tunnel` creates a TUN (`:200-226`) | `sudo ios tunnel start` (kernel TUN) needs "sudo, an admin shell on Windows, or CAP_NET_ADMIN on Linux" (`G:cmd_tunnel.go:44-47`); Windows also needs `wintun.dll` (`G:README.md:12-13`) |
| iOS 17.0–17.3.1 | no CoreDeviceProxy, so macOS uses the native tunnel and Linux/Windows need **root `tunneld`** (`ios17-tunnels.md:50-70`) | untrusted tunnel service over the device's USB ethernet (`G:ios/tunnel/untrusted.go:26`); Linux NCM driver in `G:ncm/` |
| auto-start | rebuilt per command, with "a little extra startup latency" (`ios17-tunnels.md:289-290`) | only with `ENABLE_GO_IOS_AGENT=user\|kernel`; it spawns a detached `ios tunnel start` (`G:cli_setup.go:82-87`; `G:ios/tunnel/tunnel_api.go:65-104`) and otherwise warns (`G:cli_setup.go:90-93`) |
| teardown | per-process tunnel dies with the process; `tunneld` must be killed | `ios tunnel stopagent`: GET `/shutdown` on the agent, unauthenticated (`G:ios/tunnel/tunnel_api.go:30-36`) |
| what userspace cannot do | external tools (lldb) (`ios17-tunnels.md:78-81`) | **media streams, and therefore HID touch** (`ErrUserspaceTunnelUnsupported`: "the userspace stack registers TCP only", `G:ios/display/receiver.go:12-14`) |

### 2.4 Developer Mode and the Developer Disk Image

| | pymobiledevice3 | go-ios |
|---|---|---|
| reveal the Developer Mode toggle | `amfi.reveal_developer_mode_option_in_ui` `P:pymobiledevice3/services/amfi.py:36-48` | `RevealDevMode` `G:ios/amfi/amfi.go:42` |
| enable Developer Mode from the host | `enable_developer_mode` reboots and auto-accepts the post-restart prompt. **Fails if a passcode is set** (`DeviceHasPasscodeSetError`, `amfi.py:50-87`) | `EnableDevMode` + `EnableDevModePostRestart` `G:ios/amfi/amfi.go:79-150`; `ios devmode enable` `G:main.go:87` |
| DDI < iOS 17 | classic DMG from a downloaded repo (`developer_disk_image`) | GitHub-hosted DMGs per version (`G:ios/imagemounter/imagedownloader.go:20-…`) |
| personalized DDI (17–26) | `auto_mount_personalized`; TSS ticket from Apple (`P:pymobiledevice3/services/mobile_image_mounter.py:646-670`) | `PersonalizedDeveloperDiskImageMounter` `G:ios/imagemounter/personalized_image_mounter.go:37-160`; TSS at `gs.apple.com` (`G:ios/imagemounter/tss.go:16`) |
| Cryptex1 DDI (26.4+ / required on 27+ and newer boards) | `cryptex auto-install`; `mounter auto-mount` picks the right image per iOS version (`P:docs/guides/cli-recipes.md:145-160,294-316`) | `CryptexDeveloperDiskImageMounter` (needs RSD) `G:ios/imagemounter/cryptex.go:47-91` |
| DDI source for 17+ | downloads, cached under `~/.pymobiledevice3` (`cli-recipes.md:166-169`) | **one fixed image, `ddi-17E5179g` (Xcode 15.4), from `deviceboxhq.com`** (`G:ios/imagemounter/imagedownloader.go:95,100-102,131-160`). An old DDI can lack newer services: go-ios's own error says so (`G:ios/connect.go:113-118`), and dtuhidd "ships in the image" (`G:ios/hid/hid.go:27-30`) |
| persistence | mounted until reboot; "mount it once per boot" (`cli-recipes.md:442-448`) | same (`umount` exists, `G:main.go:103`) |

### 2.5 Apps: install, launch, kill, list

| | pymobiledevice3 | go-ios |
|---|---|---|
| install | `installation_proxy.install_from_local` `P:pymobiledevice3/services/installation_proxy.py:253`; `apps install --streaming` (`cli-recipes.md:194-201`) | `ios install --path` (zipconduit) `G:ios/zipconduit/zipconduit_installer.go:100` |
| uninstall | `installation_proxy.uninstall` `:181`; CoreDevice `uninstall_app` `P:pymobiledevice3/remote/core_device/app_service.py:189` | `installationproxy.Uninstall` `G:ios/installationproxy/installationproxy.go:134` |
| list apps | `installation_proxy.browse/get_apps` `:435,491`; CoreDevice `list_apps` (`app_service.py:24-56`; "Required since iOS 26" params at `:48`) | `ios apps [--system\|--all]` `G:ios/installationproxy/installationproxy.go:72-88` |
| launch | DVT `ProcessControl.launch` `P:pymobiledevice3/services/dvt/instruments/process_control.py:188`; CoreDevice `launch_application` (`app_service.py:58`) with `--console` stdout (`cli-recipes.md:217-222`) | DVT `ProcessControl.LaunchApp` `G:ios/instruments/processcontrol.go:40-57` (used by `ios launch`, `G:cmd_device_apps.go:35-64`); CoreDevice `appservice.LaunchApp` `G:ios/appservice/appservice.go:77` |
| kill | DVT `kill` `process_control.py:138`; CoreDevice `send_signal_to_process` `app_service.py:195` | DVT `KillProcess` `G:ios/instruments/processcontrol.go:101`; CoreDevice `KillProcess` `G:ios/appservice/appservice.go:182`; `ios kill --watch` kills on launch (`G:main.go:345-360`) |
| process list | DVT sysmontap; CoreDevice `list_processes` `app_service.py:98`; `processes ps` over diagnosticsd with "no developer tunnel required" (`cli-recipes.md:47-48`) | `ios ps [--apps]` `G:main.go:1440`; `appservice.ListProcesses` `:133` |
| open URL | none found (`grep -i openurl`: no hits) | none generic; `ios webinspector launch <url>` opens Safari only (`G:main.go:184`) |

### 2.6 Screenshots

| path | pymobiledevice3 | go-ios |
|---|---|---|
| lockdown `com.apple.mobile.screenshotr` (pre-17, DDI) | `P:pymobiledevice3/services/screenshot.py:16-35` | — |
| DVT `…services.screenshot` | `P:pymobiledevice3/services/dvt/instruments/screenshot.py:6-21`; CLI `developer dvt screenshot` `P:pymobiledevice3/cli/developer/dvt/__init__.py:447` | `G:ios/instruments/screenshot.go:18-52` (used by `ios screenshot`, `G:main.go:1378`) |
| CoreDevice `screencaptureservice` (PNG only) | `P:pymobiledevice3/remote/core_device/screen_capture_service.py:12-35` | — |
| live HEVC stream (iOS 26/27) | `display serve-web` / `serve-vnc` / `start-video-stream` (`cli-recipes.md:520-561`) | `ios/display` (library only, kernel tunnel) |
| MJPEG | — | `ios screenshot --stream` loops DVT screenshots and **binds `0.0.0.0:3333`** (`G:ios/instruments/screenshot.go:60-72`; `G:main.go:486`) |
| speed | **not measured by either project.** No timings in code or docs | same |

FLAG_SECURE has an iOS analogue (secure text fields, DRM video). Neither project documents how its screenshots treat those. *Unknown*: measure.

### 2.7 Logs (our `logs`) and crash reports

| | pymobiledevice3 | go-ios |
|---|---|---|
| syslog_relay | `SyslogService.watch` `P:pymobiledevice3/services/syslog.py:23-32`; CLI `syslog live` `P:pymobiledevice3/cli/syslog.py:307` | `G:ios/syslog/syslog.go:27-62` (+ shim on 17+, `:16`) |
| os_trace (structured: pid, level, subsystem) | `OsTraceService.syslog` `P:pymobiledevice3/services/os_trace.py:207-225,342`; `create_archive` `:249` | `ostrace.New(pid, filter)` `G:ios/ostrace/ostrace.go:234-342`; `ios ostrace --pid --level --subsystem --match` `G:main.go:154` |
| DVT oslog (NDJSON with format strings) | `developer dvt oslog --format json` (`cli-recipes.md:358-362`) | — |
| crash reports | `CrashReportsManager.pull/clear/watch` `P:pymobiledevice3/services/crash_reports.py:95,203,238` | `crashreport.DownloadReports/ListReports/RemoveReports` `G:ios/crashreport/crashreport.go:23-104` |

**Lesson.** `os_trace` filtered by pid and subsystem is the counterpart of `logcat --pid`. A "watch crash reports" stream (pymobiledevice3 `watch`, `:238`) maps onto our `crash` event kind.

### 2.8 Location, process list, notifications

| | pymobiledevice3 | go-ios |
|---|---|---|
| location (< 17) | `com.apple.dt.simulatelocation` `P:pymobiledevice3/services/simulate_location.py:17-28` | `G:ios/simlocation/simlocation.go:45-177` |
| location (17+) | DVT `LocationSimulation` `P:pymobiledevice3/services/dvt/instruments/location_simulation.py:8-41`; GPX playback (`cli-recipes.md:327-337`) | `G:ios/instruments/location_simulation.go:8-48` |
| **app-state notifications** (foreground and background transitions, pid) | DVT `mobilenotifications` `P:pymobiledevice3/services/dvt/instruments/notifications.py:9-58` | `ListenAppStateNotifications` → `AppStateEvent{ProcessName, Pid, State}` `G:ios/instruments/instruments_notifications.go:28-121` |
| Darwin notifications | `notification observe/post`; `get-state` on 27.2+ (`cli-recipes.md:231-259`) | `G:ios/notificationproxy/notificationproxy.go:17` |

**Lesson.** The app-state notification stream is the only push source for **`current`** and **`wait_for activity`** that needs no runner. Neither project has a "foreground app" query. go-ios's `ui app foreground` goes through DeviceKit (`G:cmd_ui.go:286-290`). droidctl would track foreground from the stream's `state_description` (*inferred*: the state strings were not inspected).

### 2.9 Accessibility audit (`com.apple.accessibility.axAuditDaemon.remoteserver`): in depth

**Wire level.**
- DTX on the global channel, reached over lockdown/usbmux. The iOS 17+ shim name is `…remoteserver.shim.remote` (`P:pymobiledevice3/services/accessibilityaudit.py:276-282`).
- go-ios connects only through usbmux lockdown (`G:ios/accessibility/accessibility.go:10-23`).
- Payloads are NSKeyedArchiver dicts tagged with `ObjectType` (`AXAuditInspectorFocus_v1`, `AXAuditElement_v1`, `AXAuditElementAttribute_v1`, `AXAuditIssue_v1`, …) (`accessibilityaudit.py:220-227,256-273`).

**What the host can do (selectors from both codebases):**

| selector | purpose | where |
|---|---|---|
| `deviceCapabilities`, `deviceApiVersion` | handshake; API 21 = iOS 15 | `G:…/accessibility_control.go:533-578`; `P:…/accessibilityaudit.py:378-385` |
| `deviceSetAppMonitoringEnabled:` | monitor the **foreground app** | `P:…:441-448` |
| `deviceInspectorMoveWithOptions:` (`direction` 3/4/5/6, `includeContainers`, `allowNonAX`) | **move the inspector focus** to prev/next/first/last | `P:…:567-592`; `G:…:510-523` |
| event `hostInspectorCurrentElementChanged:` | the newly focused element: `CaptionTextValue_v1`, `SpokenDescriptionValue_v1`, `ElementValue_v1.PlatformElementValue_v1` (opaque bytes) | `P:…:21-81,642-680`; `G:…:249-390,479-491` |
| `deviceElement:valueForAttribute:` | query **one attribute of one element**. `Label` and `Identifier` work | `G:…:281-339` (pymobiledevice3 lacks it) |
| `deviceElement:performAction:withValue:` with `AXAction-2010` "Activate" | press | `P:…:508-565` ("only … processes that carry the `task_for_pid-allow` entitlement"); `G:…:392-453` |
| `deviceBeginAuditTypes:` → `hostDeviceDidCompleteAuditCategoriesWithAuditIssues:` | audit; each issue carries `ElementRectValue_v1` | `P:…:387-417`; `G:ios/accessibility/ax_audit.go:108-145` |
| `deviceInspectorShowVisuals:` / `SetMonitoredEventType:` | on-screen highlight overlay / selection mode | `P:…:450-477`; `G:…:180-196` |
| `deviceUpdateAccessibilitySetting:withValue:`, `deviceResetToDefaultAccessibilitySettings` | **change device accessibility settings** | `P:…:594-625`; `G:…:461-477,596-635` |

**Can it walk another app's tree?**
- **Only as a flat focus walk.** `iter_elements` enables monitoring and loops: move next, then wait up to 1 s for the focus event.
- It stops on the first repeated element id. It gives up after 5 consecutive 1 s timeouts (`accessibilityaudit.py:638-680`).
- So the cost is **one device round trip per element**, and any stall adds ≥1 s.
- The order is VoiceOver order. Containers are included only as focus stops (`includeContainers: 1`). The result is not a hierarchy.

**Labels?**
- Yes, as a caption or spoken description per focus event.
- go-ios can also ask for `Label` and `Identifier` per element: one more round trip each (`accessibility_control.go:281-339`).

**Frames?** **No.** go-ios tested this on device (`G:ios/accessibility/ax_audit.go:11-28`):

> Per-element frame via deviceElement:valueForAttribute: (AXFrame / AXBounds / AXPosition / AXSize). The attribute-query path itself works (e.g. "Label" resolves), but the geometry attributes return no value even for a live, focused element. On real hardware an element's on-screen rect is only delivered bundled in the audit result (ElementRectValue_v1 …).
> Coordinate hit-testing via deviceFetchElementAtNormalizedDeviceCoordinate: … returns an empty reply on devices; the service advertises deviceFetchResolvesElementsOnSimulator, i.e. coordinate resolution is simulator-only.

An audit rect exists only for elements that *fail* a check (contrast, hit region, dynamic text, …), so it is not a frame source.

**Actions and focus?**
- Focus moves work: that is how the walk works.
- `Activate` is implemented in both projects but proven in neither. pymobiledevice3's docstring limits it to `task_for_pid-allow` targets. go-ios has no CLI caller and no test (`grep PerformAction` hits only the definition).
- The main CLI, `ios ax`, only switches the inspector to the device, enables selection mode and moves focus (`G:main.go:1012-1035`).
- No scroll, set-value or custom actions are exposed by either.

**Side effects the user would see** (these matter for "never break the user's phone"):
- **The inspector overlay** (`ShowVisuals`) visibly highlights elements.
- **On iOS 27, selection mode eats real touches.** "the AX daemon consumes touches to move the inspector focus … the touched app never receives the touch event" (`G:…/accessibility_control.go:187-196`). A leftover monitored-event-type therefore *breaks the phone's touch input for that app* until it is reset.
- **The settings writers** can change accessibility settings device-wide (`:461-477`), and go-ios's `UpdateAccessibilitySetting` *panics* on error (`:466`).
- **The DTX connection can deadlock** if `hostInspectorMonitoredEventTypeChanged` events are not drained. The device emits them "when the device is physically interacted with, or on iOS 27 when tapping via XCUITest" (`:493-508`; `G:CHANGELOG.md:18-20`).
- **Version decay.** pymobiledevice3 moved its settings UI to CoreDevice `ConfigurationService` because the AX-audit DTX path "only worked when usbmuxd could reach the device … AND went silent on iOS 27's RSD shim" (`P:pymobiledevice3/remote/core_device/screen_stream.py:1706-1713`).

**Verdict: Avoid as a snapshot backend.** It is flat, slow, frameless, untyped, and visibly intrusive, its action support is unproven, and it decays on iOS 27. It is still usable for **ideas only**: an "accessibility audit" command (contrast, hit region, missing labels) that droidctl could add as a QA feature.

### 2.10 Starting XCUITest / WDA without Xcode

| | pymobiledevice3 | go-ios |
|---|---|---|
| testmanagerd client | `XCUITestService.run` opens two testmanagerd DTX connections plus DVT process control, `init_ctrl_session`/`init_session`, launches the runner suspended, `authorize_test`, then `start_executing_test_plan` (`P:pymobiledevice3/services/dvt/testmanaged/xcuitest.py:119-226`); service names by iOS version (`:61-63`) | `RunTestWithConfig` / `StartXCTestWithConfig` (`G:ios/testmanagerd/xcuitestrunner.go:292-323`); `ios runwda`, `runtest`, `runxctest` (`G:main.go:139-141,474`) |
| WDA convenience | `developer wda` CLI waits up to 30 s for the runner (`P:pymobiledevice3/cli/developer/wda.py:48-54`), then tap/press/type/swipe/list-items/screenshot (`:117-445`); `WdaServiceClient` without a forwarder (`services/wda.py:314-636`) | `ios ui run wda\|devicekit` starts the runner and forwards its port (`G:cmd_device_ui_run.go`); `ios ui tap/swipe/type/source/screenshot/…` speak WDA HTTP or DeviceKit JSON-RPC (`G:cmd_ui.go:21-27,108-219,357-386`) |
| **signing without a Mac** | — | **`ios sign provision appstoreconnect`** creates a development certificate and profile through the App Store Connect API (`G:ios/signing/appstoreconnect.go`; `G:main.go:144,489-491`). **`ios sign app` / `ui install`** re-sign with `go-codesign` on any host (`G:ios/signing/signing.go:13,320-378`; `G:main.go:145,493-500`). That needs an ASC API key, so a paid developer account (*inferred*) |
| prebuilt runners | — | downloads `WebDriverAgentRunner-13.2.0.zip` and `devicekit-ios-runner-0.0.18.ipa` from **`deviceboxhq.com`** (`G:cmd_device_ui_install.go:18-20`). Provenance and licence of DeviceKit not checked: **treat as an untrusted third-party binary** |

**Lessons for droidctl**
- The droidctl "agent" on iOS is an XCUITest runner, and either project can start it from Linux.
- go-ios goes one step further: it can provision, sign and install it from Linux through the App Store Connect API.
- droidctl should build and sign its *own* runner, never download one from a third-party domain.

### 2.11 HID / touch injection (new; not in ios.md)

- **Service.** `com.apple.coredevice.hid.universalhidservice` (dtuhidd, from the DDI) (`P:pymobiledevice3/remote/core_device/hid_service.py:16-20,389`).
- **Wire format** (sniffed from Xcode mirror sessions):
  - mainTouchscreen `_ServiceID 257`: 58-byte report 0x09 with contact/release state and X/Y **normalised 0..65535** (`:58-76,111-118,325-349`);
  - a gesture/pointer surface (`:43-56`);
  - a host-registered **virtual keyboard**: 240-bit usage bitmap (`:78-98,450-554`). While it is attached, "backboardd hides the on-screen software keyboard" (`:205-211`);
  - buttons through `com.apple.coredevice.hid.indigo` `IndigoButtonEvent` (home, lock, volume, mute, siri) (`:352-376`; `P:pymobiledevice3/cli/developer/core_device.py:553-598`).
- **Auth gate.** Without an active media stream, reports are accepted and dropped ("ignoring digitizer event … from unsupported service"). `touch_session` opens a display stream, discards the RTP and waits 0.3 s (`hid_service.py:100-109,557-639`).
- **Camera/mic conflict.** A foreground app using the camera or microphone blocks the stream with error 9022 (`P:pymobiledevice3/remote/core_device/display_service.py:15-36`).
- **Versions.** iOS 27+ (`G:ios/hid/hid.go:1-4,27-31`). go-ios adds that it needs a kernel tunnel (`G:ios/hid/session.go:48-52`; `G:ios/display/receiver.go:12-14`). pymobiledevice3 runs it over its in-process userspace tunnel (`P:docs/guides/ios17-tunnels.md:273-275`).
- **Exposure.**
  - pymobiledevice3 has CLI commands `tap`, `drag`, `swipe`, `session` (a batch inside one stream), `type`, `key` and `button` (`P:pymobiledevice3/cli/developer/core_device.py:601-990`).
  - go-ios has a library only: `hid.Session.TouchDown/Up`, which lifts a held finger on `Close` (`G:ios/hid/session.go:64-127`). No CLI calls it (`grep hid.NewSession`: no non-test hits).
- **Typing.** ASCII only, through a US layout map (`hid_service.py:153-203`; CLI rejects other characters at `core_device.py:914-917`). There is no Unicode path, unlike droidctl's `set_text`.

**Lessons for droidctl**
- On iOS 27+, `gesture`, `global` (home, lock, volume) and a fallback `type` are possible with no runner at all.
- It is coordinate-based, so it can only back `--point` and gesture commands, or a tap at a ref whose frame came from a runner snapshot. It cannot replace element actions.
- The media-stream gate means a held session costs encoder work on the phone (*inferred*). It also conflicts with Camera and Voice Memos, so `doctor` should report a session that has not been closed.

---

## 3. Feature matrix against droidctl

Device-API methods come from PLAN.md "Device API". CLI commands come from "Command inventory (v1)". The gap column says what neither project provides.

| droidctl | pymobiledevice3 | go-ios | gap |
|---|---|---|---|
| `devices` | `usbmux list` (`cli/usbmux.py:78`) | `ios list --details` (`main.go:113`) | none |
| `setup` | pair `lockdown.py:638`; Developer Mode `amfi.py:50`; DDI `mobile_image_mounter.py:696`; install `installation_proxy.py:253` | `pair.go:183`; `amfi.go:150`; `image auto` (`main.go:100`); `install` | trust needs an on-device tap (unless supervised); Developer Mode needs no passcode, or a manual toggle; **the runner must be signed** |
| `teardown` | `unpair` `lockdown.py:775`; uninstall; `umount-personalized` | `uninstall`, `image unmount` | Developer Mode cannot be turned off from the host in either (no disable action found) |
| `doctor` | `doctor.py` (host sees device vs usbmuxd lists it) | `ios tunnel ls`, `rsd ls` (`main.go:157,138`) | droidctl must compose it |
| `ping` | `lockdown info`; RSD handshake | `ios info` (`main.go:104`) | runner health is the runner's job |
| `tree` / `snapshot` | WDA `get_source` `services/wda.py:518` (through a runner); AX focus walk `accessibilityaudit.py:627` | `ios ui source` (`cmd_ui.go`, WDA/DeviceKit) | **no tree without a runner.** AX walk: no frames (`ax_audit.go:11-28`) |
| `act` (element) | WDA `find_element`+`click` `wda.py:428-466`; AX `perform_press` (unverified) | WDA/DeviceKit through `ios ui`; AX `PerformAction` (API only, unverified) | element actions need the runner |
| `gesture` | HID tap/drag (iOS 27+) `core_device.py:753-817`; WDA swipe `wda.py:592` | `hid.Session` (iOS 27+, kernel tunnel); `ios ui tap/swipe/longpress` (runner) | pre-27 needs the runner; no multi-touch or pinch in HID (single contact, `hid_service.py:73-76`) |
| `global` back/home/… | HID `hid button home/lock/volume/siri` `core_device.py:561-617` (iOS 27+); WDA `press_button` | `ios ui button` (WDA: home only, `cmd_ui.go:219`) | **iOS has no "back"**: it is per-app UI |
| `wait_idle` | — | — | needs the runner (XCTest quiescence) |
| `wait_for` text/id/gone | — | — | runner. `wait --activity` ≈ app-state notifications (below) |
| `events` / `subscribe` / `watch` | app-state `dvt/instruments/notifications.py`; crash `crash_reports.py:238`; AX `iter_events` `accessibilityaudit.py:479` | `ListenAppStateNotifications` `instruments_notifications.go:28` | no toast, content-changed or clicked events without the runner |
| `logs` | `syslog live`, `os_trace` (`os_trace.py:342`), `dvt oslog --format json` | `ios syslog`, `ios ostrace --pid …` (`main.go:153-154`) | none |
| `screenshot` / `shot` | DVT `dvt/instruments/screenshot.py:21`; CoreDevice `screen_capture_service.py:24`; screenshotr | DVT `instruments/screenshot.go:38` | latency unmeasured; secure-content behaviour unknown |
| `current` | app-state stream (derive) | app-state stream; `ui app foreground` (DeviceKit only) | no direct "foreground app" query without a runner |
| `launch` | DVT `process_control.py:188`; CoreDevice `app_service.py:58` | `ios launch` (`cmd_device_apps.go:35`) | none |
| `stop-app` | DVT `kill` `process_control.py:138`; CoreDevice signal `app_service.py:195` | `ios kill <bundleID>` (`cmd_device_apps_kill.go:81`) | none |
| `apps` | `installation_proxy.py:435-491`; CoreDevice `list_apps` | `ios apps` (`installationproxy.go:72-88`) | none |
| `install` | `installation_proxy.py:253`; streaming | `ios install` (`zipconduit_installer.go:100`) | none |
| `open-url` | — | `webinspector launch <url>` (Safari only, `main.go:184`) | **no generic URL/deep-link open** found in either; would need the runner (`XCUIApplication.open`) (*inferred*) or the user's Safari |
| clipboard (`type` paste fallback) | CoreDevice pasteboard `remote/core_device/pasteboard_service.py:248` | `ios pasteboard set/get` (iOS 17+, `main.go:123`) | none (useful for Unicode text) |
| location (not in v1) | `dvt simulate-location` | `setlocation` | none |

**Coverage summary** (*count, not measured*): of droidctl's 13 device-API methods, both projects cover `ping`, `screenshot`, and partly `current`/`events`/`subscribe`. They cover `gesture`/`global` only on iOS 27+. They cover none of `tree`, `act`, `gen`, `wait_idle` or `wait_for`. Of the CLI's app and device commands they cover everything except `open-url`.

---

## 4. Setup reality

### 4.1 What the user must do (either tool)

1. **Cable and trust.** Plug in, then tap "Trust" and enter the passcode on the phone (lockdown `pair`). Only a *supervised* device pairs without the dialog (`P:pymobiledevice3/lockdown.py:696`; `G:ios/pair.go:32`; `G:main.go:414`).
2. **Developer Mode** (iOS 16+). Either enable it in Settings (the toggle appears after a developer tool contacts the phone, or via `amfi reveal`), or run `amfi enable-developer-mode` / `ios devmode enable`.
   - The host path works only **if no passcode is set** (`P:pymobiledevice3/services/amfi.py:50-75`).
   - The phone reboots, and the post-restart prompt is auto-accepted (`:77-87`; `G:ios/amfi/amfi.go:116-150`).
   - *Inferred*: most real users have a passcode, so for them this is a manual Settings step plus a reboot.
3. **DDI.** It must be mounted each boot for DVT, testmanagerd, screenshots, HID and display.
   - It is downloaded from the Internet, and the personalized and cryptex variants are signed per device by Apple's TSS server (`G:ios/imagemounter/tss.go:16`; `P:docs/guides/cli-recipes.md:145-169`).
   - It needs network access on first use, then uses a cache.
4. **iOS 17+: a tunnel.** See below.
5. **For UI automation: a signed runner** (WDA or our own). On macOS this means Xcode or `xcodebuild`. On Linux it means go-ios's App Store Connect provisioning plus re-signing (`G:main.go:489-500`). With a free Apple ID the developer must also be trusted on the phone (Settings → VPN & Device Management) (*inferred*, Apple behaviour, not in either repo).

### 4.2 macOS

| step | pymobiledevice3 | go-ios |
|---|---|---|
| usbmuxd | system `usbmuxd` "works out of the box" (`P:docs/installation.md:57-59`) | same |
| tunnel, iOS 17.4+ | **no root**: in-process userspace (default) or `--native` on Apple's `remoted` (`ios17-tunnels.md:29-48`) | `ios tunnel start --userspace` (no root) or `sudo ios tunnel start`. On macOS 26+, `--pair-record-path=default` is TCC-blocked (`G:cmd_tunnel.go:58-66`) |
| tunnel, iOS 17.0–17.3.1 | no root (native tunnel) | untrusted tunnel service |
| left running | nothing (per-process); `tunneld` only if started | the **agent** process if started (HTTP `127.0.0.1:28100`); a `utun` if kernel mode |

### 4.3 Linux

| step | pymobiledevice3 | go-ios |
|---|---|---|
| usbmuxd | install libimobiledevice `usbmuxd` (`P:docs/installation.md:50-55`). Stock Linux usbmuxd has no network-device support, so there is no Wi-Fi without `--mobdev2` (`ios17-tunnels.md:328-341`) | same; Docker build in `G:usbmuxdbuild/Dockerfile` |
| tunnel, iOS 17.4+ | **no root** userspace (`ios17-tunnels.md:66-70`) | `--userspace` no root; kernel needs `CAP_NET_ADMIN` (`G:cmd_tunnel.go:44-47`) |
| tunnel, iOS 17.0–17.3.1 | **root `tunneld`** (`ios17-tunnels.md:50-62`) | kernel tunnel + NCM (`G:ncm/`) (*inferred*) |
| HID touch, iOS 27 | works over userspace (`ios17-tunnels.md:273-275`) | **kernel tunnel → root/CAP_NET_ADMIN** (`G:ios/display/receiver.go:12-14`) |
| runner signing | — | ASC API + go-codesign |

### 4.4 What is left behind (input to our teardown rule)

| artefact | where | undo |
|---|---|---|
| host pair record + device "trusted computer" | host lockdown dir / `~/.pymobiledevice3`; phone | `lockdown unpair` (`P:pymobiledevice3/lockdown.py:775`). Removing trust also affects Finder/iTunes sync with this host (*inferred*) |
| RemotePairing record | host (`remote_<udid>.plist`, `ios17-tunnels.md:246-252`) | delete file |
| Developer Mode ON | phone setting, persists | **no host-side disable in either tool**; user turns it off in Settings |
| DDI mounted | phone, until reboot | `umount` / reboot |
| tunnel daemon | `tunneld` (root, :49151, unauthenticated) / go-ios agent (:28100, unauthenticated `/shutdown`) | kill it / `ios tunnel stopagent` |
| runner app + developer profile | phone | uninstall; profile removal by the user |
| AX inspector state | phone: visuals overlay, monitored event type (iOS 27 eats touches), app monitoring | `TurnOff` (`G:ios/accessibility/accessibility_control.go:215-231`) |
| changed accessibility settings | phone | `reset_settings` resets **all**, including the user's own (`P:…/accessibilityaudit.py:622-625`) |
| media-stream session / virtual keyboard | phone, only while open | close the session (`hid_service.py:626-639`) |

**Lessons for droidctl**
- `setup` on iOS is mostly *user* steps: trust, Developer Mode with a reboot, signing.
- droidctl's job is to detect each missing step and print the exact fix, as `diagnose` does on Android.
- `teardown` must never call `reset_settings`, which would destroy the user's own accessibility settings. It must stop any tunnel agent it started, and it must say plainly that Developer Mode stays on.

---

## 5. Verdict

### 5.1 Which transport

| option | verdict | why |
|---|---|---|
| **go-ios binary as a subprocess** | **Adopt** | MIT; one static binary per OS; JSON output; covers the whole adb half; its **userspace tunnel agent is reachable from other processes without root** (`G:ios/connect.go:344-372`), which matches droidctl's daemon model; testmanagerd runner launch; Linux signing |
| go-ios as Go source compiled into a droidctl helper | Adapt (later) | MIT allows it. A small Go "droidctl-ios" helper could import `ios/…` and expose exactly our JSON-RPC (ping, screenshot, launch, events from app-state notifications, HID), avoiding go-ios CLI drift. The cost is a Go toolchain in our build |
| pymobiledevice3 as an optional subprocess | Adapt | best iOS 26/27 coverage (Cryptex DDI, HID over the userspace tunnel, CoreDevice screenshot). But GPL means user-installed and detected, with no import and no copied code. Its no-root tunnel is per-process, so every call pays tunnel setup (`ios17-tunnels.md:289-290`) unless we keep one long-lived `pymobiledevice3` process (no stable stdin RPC was found; *inferred*) |
| import pymobiledevice3 | **Avoid** | GPL-3 contaminates MIT droidctl |
| reimplement everything in Python (MIT) | Avoid for v1 | see the size estimate below |
| AX audit as the tree source | **Avoid** | §2.9 |

### 5.2 How to call it

- **The daemon owns the go-ios agent.** On first iOS use, `droidctl` starts `ios tunnel start --userspace --udid=X` (it needs no root) and records the pid. `teardown` and `daemon stop` shut it down with `stopagent`.
  - This mirrors the "persistent adb forward" lesson in ios.md.
  - Bind the agent to 127.0.0.1, which is the default (`G:main.go:198-202`).
- **One-shot calls** run `ios <cmd> --udid=X` and parse the JSON: `list`, `apps`, `launch`, `kill`, `screenshot`, `syslog`/`ostrace`, `image auto`, `devmode get`.
- **Streams** run long-lived `ios ostrace --follow` / `ios instruments notifications` subprocesses read line by line. They feed `logs`, `events` and `current`.
- **The tree and node actions come from our own XCUITest runner** speaking droidctl's JSON-RPC (`PROTOCOL.md`), forwarded with `ios forward`. Its launch goes through `ios runtest` / `ui run`, or `xcodebuild` on macOS. This is ios.md's option (a). The transport only starts and forwards it.
- **HID** (iOS 27+): only where a coordinate action is explicit (`--point`, `gesture --path`, `home`/`lock`). Needs a kernel tunnel in go-ios (root) or pymobiledevice3's `universal-hid-service session` (no root).

### 5.3 Reimplementing a protocol subset in Python (MIT): size

Measured from go-ios (non-test Go lines, `wc -l`). Python ports are assumed to be of similar size (*estimate*).

| layer | go-ios lines | needed for |
|---|---|---|
| usbmux + lockdown + pair + TLS (`ios/*.go`) | 3,786 (includes many lockdown-value helpers; the core is ~800, *estimate*) | devices, pair check, `StartService`, forward |
| DTX + NSKeyedArchiver | 1,581 + 1,478 | DVT screenshot, process control, app-state notifications, testmanagerd |
| XPC + HTTP/2 | 857 + 392 | every CoreDevice / RSD service |
| tunnel (pairing, QUIC/TCP tunnel, framing) | 3,991, **plus a userspace TCP/IP stack** (go-ios uses gVisor; pymobiledevice3 uses the external `pmd-pytcp` package) | anything developer-related on iOS 17+ |
| testmanagerd | 2,271 | starting our runner without Xcode |

**Estimates for Python:**
- **Phase 1 (pre-17 parity, plus `devices`/`ping` everywhere):** a stdlib usbmux + lockdown client in ≈600–900 lines. That is plist over a unix socket, then TLS with the pair record.
- **Full iOS 17+ developer stack:** ≥10k lines plus a userspace TCP stack, or root.

That is a second droidctl-sized project, and it would chase Apple changes that both projects already track week by week. **Not worth it for v1.**

### 5.4 Is the accessibility-audit route a viable tree source?

**No** (§2.9).
- **Per element** it costs one round trip (≥1 s per stall), and the result has no frame, no type and no hierarchy.
- **Activation is unproven.** Its effects are visible on screen, and on iOS 27 it can eat touches.
- **It is decaying** on iOS 27.

The tree must come from XCTest (a runner). The only no-runner sources are the app-state stream (foreground app), screenshots, and on iOS 27+ HID input.

---

## 6. Lessons for droidctl

1. **Keep the backend seam.** iOS needs a **device backend** that implements `PROTOCOL.md` through an XCUITest runner. It also needs a **transport** module (go-ios subprocess) with the same role `adb` has today: devices, forward, install, launch, logs, screenshot. Don't merge the two.
2. **Prefer the userspace tunnel agent the daemon owns**, never a root daemon. droidctl starts it, health-checks it with `/health`, and stops it. It is not left running after `teardown` (`G:ios/tunnel/tunnel_api.go:30-45`).
3. **Never use the AX-audit service as a backend**, and never write accessibility settings. If droidctl ever touches the inspector for an audit command, it must restore the monitored event type and visuals (`TurnOff`), because a leftover selection mode eats touches on iOS 27.
4. **`current` and `wait --activity` can work without the runner** by tracking DVT app-state notifications. Port the idea; go-ios's code is MIT if needed.
5. **The iOS 27+ HID path** gives `home`, `lock`, volume, explicit-point gestures and ASCII typing with no runner. It is a fallback, not the primary act path, and it is coordinate-based, so only explicit or event-gated uses are allowed (Non-negotiables).
6. **Typed errors to add:**
   - `untrusted` (pairing dialog pending);
   - `developer-mode-off`;
   - `ddi-missing` / `ddi-outdated` (go-ios: "service is not in RSD … make sure a recent image is mounted", `G:ios/connect.go:113-118`);
   - `tunnel-down`;
   - `media-in-use` (code 9022, `P:pymobiledevice3/remote/core_device/display_service.py:15-36`);
   - `runner-not-signed`.
7. **Supply chain.** go-ios downloads DDIs and runners from `deviceboxhq.com` (`G:ios/imagemounter/imagedownloader.go:100-102`; `G:cmd_device_ui_install.go:18-20`). droidctl should pass an explicit `--basedir`, prefer images from the user's Xcode, and build and sign its own runner.
8. **Security of local listeners.** Both tunnel daemons expose unauthenticated HTTP (pymobiledevice3 `tunneld` :49151; go-ios agent :28100). `ios screenshot --stream` binds `0.0.0.0` (`G:ios/instruments/screenshot.go:70`). droidctl must keep them on loopback and warn in `doctor` if they are bound wider.
9. **Measure before choosing defaults.** Neither project publishes latency. A spike should time each of these on a real iPhone:
   - `ios screenshot`;
   - DVT vs CoreDevice screenshot;
   - userspace-tunnel cold start;
   - an app-state notification's delay after a launch;
   - an HID tap through `session`.
10. **Version matrix for `doctor`:** < 17 (lockdown only), 17.0–17.3.1 (root tunnel on Linux), 17.4–26.3 (personalized DDI), 26.4+ (cryptex DDI), 27+ (cryptex required, HID available, AX DTX silent).

---

## 7. Where `research/ios.md` is wrong or incomplete

| ios.md | correction |
|---|---|
| §I3: "Building and signing the runner: yes [needs a Mac]" (`ios.md:188`) | go-ios provisions through the App Store Connect API and re-signs with go-codesign on any host (`G:main.go:144-145,489-500`; `G:ios/signing/signing.go:320-378`). A Mac is still needed to *build* the runner (*inferred*). Signing needs an ASC key (paid account, *inferred*) |
| §I3: mobile-mcp "expects go-ios's tunnel on port 60105" (`ios.md:177`) | current go-ios's tunnel-info API defaults to **28100** (`G:main.go:198-199`). If mobile-mcp hard-codes 60105, it targets an older go-ios or its own configuration (not re-checked) |
| §5.6 / device options: "(d) … press only for `task_for_pid-allow` apps" (`ios.md:309,390`) | that is pymobiledevice3's docstring, not a measurement. go-ios implements the same press with no caveat and no test. **Unverified either way.** Also missing from ios.md: go-ios's attribute query (`Label`/`Identifier`), its on-device proof that frames are unavailable and hit-testing is simulator-only (`ax_audit.go:11-28`), and the iOS 27 decay (`screen_stream.py:1706-1713`) |
| §5.6: no HID mentioned; ios.md treats acting as XCTest-only (§I2) | iOS 27+ CoreDevice HID touch, keyboard and buttons exist with no runner (§2.11) |
| §5.6: "screenshot (`developer dvt screenshot`)" | there are three screenshot services: lockdown screenshotr, DVT, and CoreDevice `screencaptureservice` (§2.6) |
| Mapping table: "Developer Mode and trust are user actions" (`ios.md:378`) | Developer Mode can be enabled from the host when no passcode is set (`amfi.py:50-87`; `amfi.go:79-150`). Trust can be skipped on supervised devices (`lockdown.py:696`; `pair.go:32`) |
| §I3: pymobiledevice3 no-root tunnel | correct, but **in-process only**, so useless to a separate droidctl process (`ios17-tunnels.md:282-290`). go-ios's userspace agent *is* shareable (`G:ios/connect.go:344-372`). That difference drives the verdict |
| Q9: "Is subprocess use of GPL-3 pymobiledevice3 acceptable?" | yes as a separate, user-installed program (§1.1). Not as an import, and not by copying its code |
