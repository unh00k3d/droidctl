# The iPhone backend on Linux (and on macOS without Xcode): the device side

What droidctl needs, on a host without Xcode, to reach an iPhone, start our own XCUITest runner and do everything `devicectl` does for us today. Signing and provisioning (getting a signed `DroidctlRunner-Runner.app` onto a Linux host) is covered by a separate report; here the runner is assumed to exist as a signed `.app`.

**Sources read**
- **go-ios** (danielpaulus), shallow clone of `main` at `6910f3b` (2026-11-03). The latest release is **v1.3.2 (2026-08-11)**; every behaviour cited below was re-checked against the `v1.3.2` tag, and the one place where `main` differs is called out (§2.4).
- **usbmuxd** (libimobiledevice), clone of `master` at `3ded00c` (2025-12-06). The last release is **1.1.1 (2020-06-15)**; 96 commits on `master` since.
- droidctl itself (`droidctl/ios/*.py`, the built `.xctestrun` on this Mac) and the earlier reports: `research/ios-transport.md` (go-ios vs pymobiledevice3, verdict: go-ios as a subprocess), `research/ios-mobile-next.md`, `research/ios-decision.md`. Those are not repeated here.
- go-ios GitHub issues, read with `gh` (cited as `#NNN`).

**Citation prefixes:** `G:` go-ios repo, `U:` usbmuxd repo, `D:` droidctl repo. Nothing was run against a device and no go-ios binary was executed (the release zips were downloaded only to list and hash them). Every "works" means "the code says so"; everything else is marked *inferred* or *estimate*.

**Licenses.** go-ios: **MIT** (`G:LICENSE:1-3`, "Copyright (c) 2019 danielpaulus"). Its binary statically links, among others, gVisor (Apache-2.0), quic-go (MIT), golang.org/x/* (BSD-3), howett.net/plist, docopt-go (MIT) (`G:go.mod` `require` block). usbmuxd: **GPL-2.0/GPL-3.0** (`U:COPYING.GPLv2`, `U:COPYING.GPLv3`): we only talk to it over its socket, as we already do on macOS, and never ship or link it. Apple's Developer Disk Images are Apple software under the Xcode licence: **droidctl must not redistribute them**.

---

## TL;DR

1. **Our usbmux client works against Linux usbmuxd unchanged.** The socket is `/var/run/usbmuxd`, mode 0666 (`U:src/main.c:56,229`); it speaks the plist protocol with `ListDevices`, `Connect`, `ReadPairRecord` (`U:src/client.c:696,739,757`); devices report `ConnectionType` `"USB"` and the hyphenated new-style UDID, the same as macOS (`U:src/client.c:404,408`; `U:src/usb.c:297-302`). So **the runner connection (`client.py`), `syslog.py` and the AMFI reveal need no change.** One small bug: go-ios-style `USBMUXD_SOCKET_ADDRESS=unix:///path` crashes our parser (§1.4).
2. **No root at run time.** Root is only needed to install the `usbmuxd` package. Pairing ("Trust") is done by usbmuxd itself (its preflight shows the dialog and pairs when the user taps Trust). The iOS 17+ tunnel runs as go-ios's **userspace agent** with no root. The DDI mount needs no root but **does need the network** (Apple TSS) unless the phone already has a manifest.
3. **iOS floor on Linux: 17.4.1, not 17.0.** go-ios takes the root-free lockdown/CoreDeviceProxy tunnel only for versions *strictly greater* than 17.4.0. 17.0–17.4.0 need a kernel TUN (root/`CAP_NET_ADMIN`) plus the device's NCM interface (`G:ios/tunnel/tunnel_api.go:649-665`).
4. **Starting our runner** is `ios runtest --test-runner-bundle-id=<runner>.xctrunner --xctest-config=DroidctlRunner.xctest --env=DROIDCTL_PORT=27100 --log-output=<file>`, with the go-ios agent running. It holds the session the way `xcodebuild test-without-building` does. Caveats:
   - it does not install the app (`ios install` first);
   - it **exits 0 on failure** (the error goes only to a stderr log line);
   - it **drops the device's reason for a failed launch**: an untrusted profile and a locked phone look the same (§3.4);
   - it has no SIGTERM handler.
5. **The biggest unknown is the DDI.** By default go-ios mounts **one fixed image, Xcode 15.4's `17E5179g`, from deviceboxhq.com**, while our runner is built with Xcode 26.2, whose DDI on this Mac is **`17C52`**. Whether an Xcode-26 XCTest runner works against the testmanagerd of a 15.4 DDI is untested anywhere we could find. That is the first thing to measure. The fallback is to point go-ios at the user's own copy of Xcode's DDI.
6. **Most of `devicectl` can be replaced by our own Python over lockdown**, which is cross-platform and needs no tunnel: device name and version, lock state, Developer Mode status, apps, uninstall, and syslog (already done). **go-ios is needed only for:** the tunnel agent, DDI mounting, `runtest`, `install`, and `ps`/`kill`. Real gaps: `open-url` with a payload at setup time (the Trust page), and the marketing name ("iPhone 12").
7. **Packaging:** download a pinned go-ios release on first use and check its sha256 ourselves. go-ios publishes no checksums. The Linux zip is 16.6 MB and holds amd64 and arm64 binaries of ~22 MB each. Pin the exact version: go-ios `main` already changes DDI behaviour in a way that would hurt our users (§2.4).

---

## 1. Linux prerequisites

### 1.1 usbmuxd: which version

| question | answer | source |
|---|---|---|
| last release | 1.1.1 (2020). Distros ship 1.1.1 or a git snapshot of it (*not checked per distro*) | `U:NEWS:1-3`; `git log` |
| what changed after 1.1.1 that matters for iOS 17+ | **USB "mode" switching**: 1 = initial, 2 = "Valeria" (screen capture), 3 = CDC NCM (USB Ethernet). The default was set to 3 in 2023-09 and **reverted to 1 in 2024-04**; `USBMUXD_DEFAULT_DEVICE_MODE` overrides it. Modes 4 and 5 were added in 2024-08 | `U:src/usb.c:706-707,789-791`; commits `c7a0dd9`, `f8be60c`, `df80b73` |
| does droidctl need NCM / mode 3? | **No, on iOS ≥ 17.4.1.** go-ios's userspace tunnel goes through the lockdown service `com.apple.internal.devicecompute.CoreDeviceProxy` over plain usbmux (`G:ios/tunnel/userspace_tunnel.go:192-197`; `G:ios/tunnel/tunnel_lockdown.go:15`). NCM is needed only by the 17.0–17.4.0 "untrusted tunnel" path, which also needs a kernel TUN | `G:ios/tunnel/tunnel_api.go:649-665`; `G:ios/tunnel/untrusted.go` |
| go-ios's own reference build | usbmuxd, libusbmuxd, libimobiledevice(-glue) and libplist, all from git `master` | `G:usbmuxdbuild/Dockerfile:1-19` |

**Conclusion (inferred):** any packaged usbmuxd ≥ 1.1.0 that speaks the plist protocol is enough for what droidctl does: USB lockdown, port connect, pair records and the CoreDeviceProxy tunnel. Measure this on Debian 12/13 and Ubuntu 24.04 (§6.4, V1).

### 1.2 Pairing ("Trust") from Linux, and where pair records live
- **usbmuxd pairs by itself.** On attach, its "preflight":
  - checks for a pair record and, if there is none, calls `lockdownd_pair` to make the Trust dialog appear (`U:src/preflight.c:178-211,257-260`);
  - pairs when the phone posts `com.apple.mobile.lockdown.request_pair`, i.e. when the user taps Trust (`U:src/preflight.c:103-112`);
  - logs "Device … is locked with a passcode. Cannot pair." and waits while the phone is locked (`U:src/preflight.c:321-324`).
  - Preflight is a build option (`--no-preflight`, `U:src/main.c:513,596`); distro packages build it in (*inferred*).
- **Pair records live in `/var/lib/lockdown/<UDID>.plist`** (`U:src/conf.c:64,129-131`; `/var/db/lockdown` on macOS), next to `SystemConfiguration.plist` (the host's `SystemBUID`). They belong to the `usbmux` user. droidctl never reads the files; it asks the socket (`ReadPairRecord`, `D:droidctl/ios/transport.py:134-138`, `D:droidctl/ios/syslog.py:90-96`). That already works on macOS.
- **go-ios `ios pair`** does the same through usbmuxd (`SavePairRecord`). The first call returns "Please accept the PairingDialog on the device and run pairing again!", so it has to be run twice (`G:ios/pair.go:183-246`). droidctl does not need it: usbmuxd's preflight covers it, and `ios pair` is only a fallback when preflight is compiled out.
- **The go-ios tunnel's own "pair record" is something else.** It is a `selfIdentity.plist` for RemotePairing, written to `--pair-record-path`, which **defaults to `.`, the current directory** (`G:cmd_tunnel.go:54-57`; `G:ios/tunnel/pairrecord.go:50-60`). On iOS > 17.4 the lockdown tunnel doesn't use it, but the file is created anyway. **droidctl must pass `--pair-record-path=~/.droidctl/ios/go-ios/`.**
- *Security (inferred from the code):* the socket is world-writable and `ReadPairRecord` has no caller check (`U:src/client.c:757`). So on a multi-user Linux host **any local user can read the pair record** (the host's private key) and talk lockdown to the phone. That is usbmuxd's model, not ours; `doctor` can mention it.

### 1.3 udev, permissions, root

| need | root? | detail |
|---|---|---|
| install `usbmuxd` (it brings the udev rule and the systemd unit) | yes, once | the rule matches Apple PIDs `05ac:12[9a]x`/`190[1-5]`, sets `bConfigurationValue=0`, `OWNER="usbmux"` and starts usbmuxd; on remove it runs `usbmuxd -x` (`U:udev/39-usbmuxd.rules.in:4-16`). The unit runs `usbmuxd --user usbmux --systemd` (`U:systemd/usbmuxd.service.in:6`) |
| talk to usbmuxd | no | socket mode 0666 (`U:src/main.c:229`); **no group membership needed** |
| go-ios userspace tunnel | no | the root check runs only without `--userspace` (`G:cmd_tunnel.go:44-47`) |
| go-ios kernel tunnel | root or `CAP_NET_ADMIN` | droidctl never uses it (and HID on iOS 27 needs it in go-ios, `research/ios-transport.md` §2.11) |
| DDI mount, install, runtest | no | lockdown/RSD services only |
| VM or container | — | the phone must be passed through as a USB device. It re-enumerates when its configuration changes, so pass it through by vendor/product, not by bus address (*inferred*; compare the Android notes in `CLAUDE.md` on the KVM VM) |

### 1.4 Does `droidctl/ios/transport.py` work against Linux usbmuxd?
**Yes, unchanged.**
- `DEFAULT_SOCKET = "/var/run/usbmuxd"` (`D:droidctl/ios/transport.py:18`). Modern distros make `/var/run` a symlink to `/run`; usbmuxd's unit uses `runstatedir` (commit `2707534`).
- **Messages:** `ListDevices`, `Connect` (with `PortNumber` in network byte order) and `ReadPairRecord` are all handled (`U:src/client.c:696-757`).
- **Device records:** the field names (`DeviceID`, `Properties.SerialNumber`, `ConnectionType`, `ProductID`) match what we parse (`U:src/client.c:397-410`; `D:transport.py:116-121`).
- **UDID format:** usbmuxd inserts the hyphen into 24-character serials (`U:src/usb.c:297-302`), so UDIDs look the same as on macOS, and `DROIDCTL_IOS_UDID` values carry over.
- **Wi-Fi devices:** libimobiledevice's usbmuxd has no network devices (`research/ios-transport.md` §4.3), so our USB-only filter changes nothing.

**One fix to make:** `socket_address()` accepts libimobiledevice's `UNIX:/path` and `host:port` (`D:transport.py:29-38`). go-ios documents `unix:///path` and `tcp://host:port` and lower-cases the scheme (`G:ios/usbmuxconnection.go:32-55`). With `unix:///var/run/usbmuxd` our code splits on the last `:` and hits `int("///var/run/usbmuxd")`, which raises an uncaught `ValueError`. Accept `unix://`, `UNIX:` and `tcp://` case-insensitively.

**Also untested on Linux:** our lockdown TLS (`D:syslog.py:99-117`). It sets `@SECLEVEL=0` and TLS 1.0 as the minimum, because lockdown's certificates are old-style. Distros with system crypto policies (Fedora/RHEL `DEFAULT`, Debian's OpenSSL 3 defaults) may still refuse the handshake (*inferred*). This is V2 in §6.4.

---

## 2. iOS 17/18 on Linux with go-ios

### 2.1 The userspace tunnel agent

| aspect | behaviour | source |
|---|---|---|
| start | `ios tunnel start --userspace [--udid=X] [--tunnel-info-port=P] [--pair-record-path=DIR]`. With `--udid` the agent manages that one device only | `G:cmd_tunnel.go:53-70`; `G:main.go:1919-1967` |
| control API | HTTP on `127.0.0.1:28100` by default (`--tunnel-info-host`, `GO_IOS_AGENT_HOST`): `GET /health`, `GET /ready` (200 after the first device scan), `GET /tunnels`, `GET`/`DELETE /tunnel/<udid>`, **`GET /shutdown`, unauthenticated** (exits after 1 s) | `G:ios/tunnel/tunnel_api.go:107-205`; `G:main.go:191-210` (options) |
| how other processes use it | `GET /tunnel/<udid>` returns `{address, rsdPort, userspaceTun, userspaceTunPort}`. A client connects to `localhost:<userspaceTunPort>` and writes 16 bytes of IPv6 address plus a 4-byte little-endian port; from then on the socket is a TCP stream to that device port | `G:ios/connect.go:344-372`; listener on `localhost` at `G:ios/tunnel/userspace_tunnel.go:219` |
| per-device data port | `userspaceTunPort` = the agent's info port + 1, 2, … in order of attachment. A second agent on another info port does not collide | `G:ios/tunnel/tunnel_api.go:358-378,453-458` |
| how `ios <cmd>` finds it | each invocation asks the agent at `--tunnel-info-port`, then does a **fresh RSD handshake**, only for commands that need RSD (`runtest`, `runwda`, `ps`, `kill`, `launch`, `screenshot`, `image`, …) | `G:cli_device_resolution.go:34-117`; `G:main.go:1969-1984` |
| auto-start | only with `ENABLE_GO_IOS_AGENT=user\|kernel`, which spawns a detached `ios tunnel start` on the default port. Otherwise every command warns "go-ios agent is not running" | `G:cli_setup.go:83-93`; `G:ios/tunnel/tunnel_api.go:65-99` |
| reconnect | rescans every 1 s; tunnels of unplugged devices are stopped; failed devices back off 30 s, 60 s, … up to 5 min. A device-list error drops all tunnels, and they are rebuilt on the next tick | `G:main.go:1943-1957`; `G:ios/tunnel/tunnel_api.go:408-520` |
| start timeout | 10 s per tunnel attempt | `G:ios/tunnel/tunnel_api.go:372` |
| what userspace can't do | media streams (and so HID touch on iOS 27) | `research/ios-transport.md` §2.3, §2.11 |

**Start time** (*estimate*, nothing measured): the first scan runs on the 1 s ticker, then lockdown plus the CoreDeviceProxy handshake. Expect **~1–3 s to `/ready` and a tunnel for the phone**. On top of that, each `ios` command pays the RSD handshake again, *estimated* at 0.1–0.5 s.

**Stability, from issues.**
- go-ios's own device farm "tops out at iOS 18" (maintainer in `#772`), which matches our iPhone 12 on 18.7.8.
- **iOS 26.5:** the userspace tunnel "connects, then drops ~6s later" (`#772`, open). **iOS 26.7.1/27:** `image auto` fails (`#875`, `#881`, open).
- So **iOS 26.x on Linux is not ready**, and droidctl should state iOS 17.4.1–18.x as the tested range until we measure newer versions.

**droidctl's use of it.**
- Run **our own agent** per phone, on a private info port (for example 28150 + n, not 28100). That way it neither collides with nor adopts a user's own go-ios agent.
- Pass `--udid` and `--pair-record-path`.
- The daemon owns it: it starts it, waits for `/ready`, then for `GET /tunnel/<udid>`, and records the pid.
- `teardown` and `daemon stop` send `GET /shutdown`. This follows the existing verdict in `research/ios-transport.md` §5.2.
- **Our runner connection does not go through the tunnel**: it stays on usbmux `Connect` to 127.0.0.1:27100 on the phone (`research/ios-decision.md` §10, "Transport"). The tunnel is only for go-ios's own commands (testmanagerd, appservice, DVT).
- *Security:* both the info API and the data port are unauthenticated on localhost. **Any local process can reach any service on the phone through the data port**, including testmanagerd and DVT. Keep both bound to loopback, and have `doctor` warn if `GO_IOS_AGENT_HOST` is set to anything else.

### 2.2 Personalized DDI (iOS 17+)

| question | answer | source |
|---|---|---|
| which image | if `--basedir` already holds a `Restore/BuildManifest.plist`, it is used offline (since 1.3.1). Otherwise go-ios downloads **`ddi-17E5179g.zip` (Xcode 15.4) from `https://deviceboxhq.com/`**, with no checksum. The default basedir is `./devimages` | `G:ios/imagemounter/imagedownloader.go:91-102,133-172,180-228,352-372`; `G:cmd_device_debug.go:248-262`; `G:CHANGELOG.md` 1.3.1 |
| network to Apple? | **yes, the first time.** go-ios first asks the phone for an existing personalization manifest (`QueryPersonalizationManifest`) and reuses it; otherwise it gets a nonce and asks **`https://gs.apple.com/TSS/controller?action=2`** to sign it. `--proxyurl`/`HTTP_PROXY` apply | `G:ios/imagemounter/personalized_image_mounter.go:75-103,167`; `G:ios/imagemounter/tss.go:16-26`; `G:main.go:191-210` |
| how long the manifest lasts | unknown. The code says it avoids TSS "on re-mounts"; whether it survives a reboot is not stated (V5) | ibid. `:75` |
| root? | no: `mobile_image_mounter` over lockdown | `G:ios/imagemounter/personalized_image_mounter.go:36-40` |
| persistence | mounted until the phone reboots (`research/ios-transport.md` §2.4) | |
| exit code | **`image auto` exits 0 when the download fails** (logs an error and returns) | `G:cmd_device_debug.go:256-260` |
| iOS 26.4+/27 | a Cryptex DDI path exists on `main` only (`cryptex.go`, 599 lines, not in v1.3.2) | `git diff v1.3.2..main --stat` |

**The DDI version question.**
- The phone runs testmanagerd (and DVT, and appservice support) from the DDI. Our runner carries the XCTest frameworks of the Xcode that built it.
- Today the Mac pair is Xcode 26.2 with its DDI: `/Library/Developer/DeveloperDiskImages/iOS_DDI`, `ProductBuildVersion` **17C52**, 21 MB, `XCTest` project version 24507. That is the only pair we have verified (`D:droidctl/ios/manage.py:21`).
- go-ios would mount 17E5179g (Xcode 15.4) instead. Whether testmanagerd 15.4 talks to XCTest 26 (protocol 36 is what go-ios sends, `G:ios/testmanagerd/xcuitestrunner.go:446`) is **unknown**. This is V3, the first measurement.

Options, in order of preference:
1. If any DDI is already mounted (for example by Xcode on another machine since the last reboot), **use it and never touch it**.
2. A user-supplied DDI directory (`DROIDCTL_IOS_DDI=/path/to/iOS_DDI`, copied from `/Library/Developer/DeveloperDiskImages/iOS_DDI` on any Mac with Xcode), passed as `--basedir`.
3. go-ios's default download, which is Apple's image, re-hosted by a third party. The image is TSS-personalized, so the phone rejects a tampered one; but the zip is extracted on the host (`G:ios/utils.go:242`).

droidctl must not host Apple's DDI itself.

### 2.3 Developer Mode
- **Status:** `QueryDeveloperModeStatus` on `com.apple.mobile.mobile_image_mounter` returns `DeveloperModeStatus` (`G:ios/imagemounter/imagemounter.go:368-405`). That is plain lockdown, so we can call it from our Python (`D:syslog.py:120-144` already starts lockdown services).
- **Reveal:** AMFI action 0. We already do this (`D:droidctl/ios/setup.py:375-384`; `G:ios/amfi/amfi.go:39-76`).
- **Enable from the host:** action 1, then reboot, then action 2 after the restart (`G:ios/amfi/amfi.go:78-148`). It fails when a passcode is set (`research/ios-transport.md` §2.4), and it reboots the user's phone. **droidctl keeps the manual instructions**; this is a "never break the user's phone" matter.

### 2.4 Version pinning: an example from right now
In **v1.3.2**, `MountImage` keeps any image that is already mounted ("there is already a developer image mounted … aborting") and returns success (`v1.3.2:ios/imagemounter/imagemounter.go:230-246`). On **`main`** it compares signatures and **unmounts a "wrong" image to mount its own** (`G:ios/imagemounter/imagemounter.go:274-300`). On a phone that is also used with Xcode, that would swap Xcode's DDI under a running Xcode session.

So droidctl should:
- pin the exact release;
- check `LookupImage` itself, over lockdown (`G:ios/imagemounter/imagemounter.go:312-350` shows the request), before calling `ios image`;
- never call `image auto` when anything is mounted.

---

## 3. Starting OUR runner with go-ios

### 3.1 What our xctestrun says (`runner.xctestrun()`, built on this Mac)
`~/.droidctl/ios/build/<team>/Build/Products/DroidctlRunner_iphoneos26.2-arm64.xctestrun` is **FormatVersion 1** with one key, `DroidctlRunner`. It holds:
- `TestHostBundleIdentifier` = `dev.droidctl.r<8hex>.runner.xctrunner` and `TestHostPath` = `__TESTROOT__/Debug-iphoneos/DroidctlRunner-Runner.app` (20 MB);
- `TestBundlePath` = `__TESTHOST__/PlugIns/DroidctlRunner.xctest`;
- `IsUITestBundle` true, `UseUITargetAppProvidedByTests` true, **no `UITargetAppPath`**;
- `ProductModuleName` `DroidctlRunner`;
- `TestTimeoutsEnabled` false, `DefaultTestExecutionTimeAllowance` 600;
- `EnvironmentVariables` and `TestingEnvironmentVariables` set by Xcode 26 (`DYLD_INSERT_LIBRARIES=/usr/lib/libMainThreadChecker.dylib:/usr/lib/libRPAC.dylib`, `PERFC_*`, …).

The runner has one test, `-[DroidctlRunner testServe]`, and reads `DROIDCTL_PORT` from its environment, falling back to 27100 (`D:droidctl/ios/xcode/DroidctlRunner/DroidctlRunner.m:23,1393-1413`). xcodebuild maps `TEST_RUNNER_DROIDCTL_PORT` to it (`D:runner.py:198`); go-ios has no `TEST_RUNNER_` mapping.

### 3.2 The invocation

| go-ios command | maps to | verdict |
|---|---|---|
| **`ios runtest --udid=U --test-runner-bundle-id=dev.droidctl.rXXXXXXXX.runner.xctrunner --xctest-config=DroidctlRunner.xctest --env=DROIDCTL_PORT=27100 --log-output=~/.droidctl/ios/U/session.log --tunnel-info-port=P`** | `TestConfig{TestRunnerBundleId, XctestConfigName, Env}` with no `BundleId`, so no target app, which is exactly our xctestrun (`G:cmd_device_testing.go:25-81`; `G:main.go:69-190` usage) | **recommended.** It needs nothing from the Mac build except the `.app`, so it works with a runner signed on Linux |
| `ios runxctest --xctestrun-file-path=…` | parses FormatVersion 1/2 and copies `EnvironmentVariables` + `TestingEnvironmentVariables` + `UITargetAppEnvironmentVariables` into the launch env (`G:ios/testmanagerd/xctestrunutils.go:44-81,94-196`) | possible on a Mac, but it would inject Xcode 26's `DYLD_INSERT_LIBRARIES` (`libRPAC.dylib`) over go-ios's own and needs the `.xctestrun`, which Linux doesn't have. No `--env` |
| `ios runwda --bundleid=R --testrunnerbundleid=R --xctestconfig=DroidctlRunner.xctest --env=DROIDCTL_PORT=27100` | the same `RunTestWithConfig`, with the runner as its own "target app", which is how WDA users run it (`#631`). **It handles SIGINT/SIGTERM**: it cancels the context, so go-ios kills the runner (`G:cmd_device_testing.go:114-182`; `G:ios/testmanagerd/xcuitestrunner.go:452-474`) | fallback, if a clean stop matters more than an exact mapping (V6) |

What `runtest` does on iOS 17+ (`G:ios/testmanagerd/xcuitestrunner.go:323-487`):
1. Two DTX connections to testmanagerd over the tunnel.
2. installation_proxy `BrowseUserApps`; the runner must already be installed, otherwise "Did not find test app … Is it installed?" (`:687-703`).
3. `initiateSessionWithIdentifierAndCaps`.
4. CoreDevice **appservice** `LaunchAppWithStdIo` of the runner, with `XCTestSessionIdentifier`, `XCTestBundlePath=<app>/PlugIns/DroidctlRunner.xctest`, `XCTestManagerVariant=DDI`, `DYLD_INSERT_LIBRARIES=/Developer/usr/lib/libMainThreadChecker.dylib`, `OS_ACTIVITY_DT_MODE=YES` and our env merged on top (`:500-562`).
5. `authorizeTestSessionWithProcessID`, then `startExecutingTestPlanWithProtocolVersion(36)`.
6. Block until a connection closes, the test finishes or the context ends; then kill the runner by pid (`:452-474`).

The XCTest configuration it sends has `testTimeoutsEnabled=false` and nil allowances (`G:ios/nskeyedarchiver/objectivec_classes.go:132-151`). That is what xcodebuild does with our xctestrun, and our xcodebuild sessions ran 30 min (`research/ios-decision.md` §11 "Soak"). Issue `#495` ("WDA is closing after 10 min") is therefore *probably* not about us, but the soak test (V7) must confirm it.

**Install first:** `ios install --path=DroidctlRunner-Runner.app` (zipconduit over plain usbmux, no tunnel; a `.app` folder or an `.ipa`: `G:main.go:793-799`; `G:ios/zipconduit/zipconduit_installer.go:63-100`). xcodebuild did this implicitly. Whether a reinstall of an unchanged build can be skipped: compare the installed `CFBundleVersion`/build id through installation_proxy first.

### 3.3 Time to first ping
*Estimate* (nothing measured): agent `/ready` ~1–3 s if it isn't already up. Then `runtest`: two DTX connections, a browse of the user apps, the launch, the authorization and the test plan, each a round trip over the tunnel, plus the runner's own start. Our xcodebuild start is **5.1–5.3 s** (`D:syslog.py:8-14`); expect go-ios to be similar or faster, since it skips xcodebuild's own start-up. Measure the full start (V4).

### 3.4 How it reports failure

| failure | what go-ios gives us | source |
|---|---|---|
| any `RunTestWithConfig` error | an **Info-level** stderr log line `"Failed running Xcuitest"` with `error`, then the results JSON on stdout. **Exit code 0** | `G:cmd_device_testing.go:71-76` |
| device not found, bad args | `exitIfError` → Error log, exit 1 | `G:main.go:2015-2025` |
| runner not installed | error "Did not find test app for '…' on device. Is it installed?" | `G:ios/testmanagerd/xcuitestrunner.go:702` |
| no tunnel / DDI missing | "service '…' is not available in RSD … run `ios image auto`" or "Cannot connect … missing tunnel address and RSD port" | `G:ios/connect.go:113-118,124-160` |
| **launch refused (untrusted profile, locked, expired profile)** | **"pidFromResponse: could not get pid from response"**: appservice's `launchApp` never calls `getError`, so the device's `CoreDevice.error` (which carries the reason) is thrown away | `G:ios/appservice/appservice.go:110-124,286-306` |
| runner crashes or quits | "lost connection to testmanagerd. the test-runner may have been killed", or the test simply finishes | `G:ios/testmanagerd/xcuitestrunner.go:452-468` |
| runner stdout/stderr | copied to `--log-output` (with `OS_ACTIVITY_DT_MODE=YES`, `NSLog` goes there too, so our "droidctl: runner … ready" line should appear) | `:419-427`; `G:cmd_device_testing.go:57-69` |

So droidctl's go-ios start loop is the same as `_start` (`D:runner.py:191-233`): poll `ping` over usbmux, watch the child process, and read stderr plus `--log-output`. **But `classify_output` must get go-ios rules, and it cannot tell "untrusted" from "locked" from the start error.** We disambiguate ourselves:
- read lockdown `PasswordProtected` (it means "locked right now", per `G:main.go:405-406` and `G:ios/mcinstall/mcinstall.go:542-544`);
- if the phone isn't locked, treat the failure as untrusted/expired: check the profile expiry from the `.app`'s `embedded.mobileprovision`, and otherwise give the Trust instructions;
- optionally confirm with one DVT `ios launch <runner>`, whose `processcontrol` reply carries the NSError (`G:ios/instruments/processcontrol.go:100-140`). That is V8.

### 3.5 Stopping it
- **`runtest` installs no signal handler** (`G:cmd_device_testing.go:25-81`), so SIGTERM ends the go-ios process at once, without its `killTestRunner`.
- Under xcodebuild, testmanagerd "ends the runner within seconds" when the IDE side dies (`research/ios-decision.md` §10 S2). We expect the same here (*inferred*, V6).
- droidctl's `stop()` already sends the runner's `quit` RPC first (`D:runner.py:236-255`). `testServe` then returns, go-ios sees the test finish, kills the (dead) pid and exits by itself. SIGTERM to the process group stays as the fallback, then `ios kill --pid=<runner pid>`.

### 3.6 Known iOS 18 issues
- Tunnel: TLS-PSK for iOS 18.2+ on the untrusted path (`#748`; `G:ios/tunnel/tunnel_tcp.go:9-15`). Not on our lockdown path.
- `#570` ("can't run any XUITest … iOS 18.2", closed): a user invocation without `--xctest-config`. **Always pass all ids.**
- No open iOS 18 `runtest` bug was found. The open bugs are iOS 26.5+ (§2.1).

---

## 4. Every devicectl/Xcode operation, and its replacement

"lockdown (ours)" means a small Python service client over our usbmux transport, the way `syslog.py` already starts `syslog_relay`. It needs no tunnel, no DDI and no go-ios, and it works the same on macOS. go-ios's MIT code is the reference for the plist shapes.

| droidctl use (today) | today | without Xcode | tunnel/DDI | gap |
|---|---|---|---|---|
| list phones, UDID (`transport.list_devices`) | usbmux | unchanged | no | — |
| name, model, iOS version (`devices.list_devices`, `cli._ios_model`) | `devicectl list devices` (`D:devices.py:89-109`) | lockdown `GetValue`: `DeviceName`, `ProductType`, `ProductVersion` (go-ios `ios info`/`list --details`, `G:main.go:1485-1510,1595-1620`) | no | **no marketing name** ("iPhone 12"): ship a `ProductType` → name table, or show `iPhone13,2` |
| paired? | usbmux `ReadPairRecord` | unchanged | no | — |
| Developer Mode status | devicectl `developerModeStatus` | lockdown `mobile_image_mounter` `QueryDeveloperModeStatus` (`G:ios/imagemounter/imagemounter.go:368-405`) | no | — |
| reveal the Developer Mode toggle | lockdown AMFI (ours) | unchanged | no | — |
| lock state (`devices.lock_state`) | `devicectl device info lockState` (`D:devices.py:121-127`) | lockdown `GetValue PasswordProtected` = currently locked (`G:main.go:405-406`) | no | `unlocked_since_boot` has no lockdown equivalent found; `doctor` drops it |
| list apps / one app (`devices.apps`, `devices.app`) | `devicectl device info apps` (`D:devices.py:130-172`) | installation_proxy `Browse` (`G:ios/installationproxy/installationproxy.go:72-88,213-260`), ours or `ios apps --all` | no | `builtByDeveloper` (used for teardown's trust note, `D:manage.py:62`) → `ProfileValidated` / `SignerIdentity` attributes (*inferred*, V9) |
| install `.app`/`.ipa` | `devicectl device install app` | `ios install --path` (zipconduit) | no | — |
| uninstall | `devicectl device uninstall app` | installation_proxy `Uninstall`, ours or `ios uninstall` (`G:main.go:801-807`) | no | — |
| process list, pids of an app (`devices.processes`, `pids_of`) | `devicectl device info processes` | `ios ps --apps` → `[{Pid, Name, RealAppName, IsApplication, StartDate}]` (`G:ios/instruments/instruments_deviceinfo.go:16-22`; `G:main.go:1440-1462`) | **yes, both** | does `RealAppName` hold the executable path that `pids_of` matches on? (V9) |
| kill (`devices.terminate`) | `devicectl device process terminate` | `ios kill --pid=N` (DVT `killPid:`, `G:cmd_device_apps_kill.go:76-110`) | yes, both | — |
| launch app | the runner RPC `launch` (`D:act.py:1848-1871`) | unchanged | — | — |
| open URL | the runner RPC `open_url` | unchanged | — | — |
| open the Settings Trust page at setup (`setup.open_settings`, `--payload-url`) | devicectl | **none.** `ios launch` takes args/env but no URL payload (`G:cmd_device_apps.go:35-59`), and the runner can't help before it is trusted | — | instructions only |
| screenshot (`shot`) | the runner | unchanged; `ios screenshot` (DVT) is a fallback (`G:main.go:1378-1400`) | DVT: yes | — |
| syslog (`logs`, daemon ring) | ours over lockdown (`D:syslog.py`) | unchanged. The `xcode_busy` flag is Xcode-specific; whether a syslog stream disturbs `runtest` is V10 | no | — |
| os_trace (structured) | not used | `ios ostrace` if ever needed (`research/ios-transport.md` §2.7) | yes | — |
| build and sign the runner | xcodebuild | the other report | — | — |
| start/stop the runner | xcodebuild `test-without-building` | `ios runtest` (§3) | yes, both | failure reason (§3.4) |
| host checks (`doctor` `host`: `pmset`) | macOS only (`D:manage.py:80-97,216-230`) | Linux: skip, or `systemd-inhibit --list` | — | — |

---

## 5. Packaging go-ios

- **Release assets (v1.3.2):**
  - `go-ios-linux.zip` 16,638,255 B, holding `ios-amd64` (23.3 MB) and `ios-arm64` (21.7 MB);
  - `go-ios-mac.zip` 16,821,315 B, one universal `ios` (45.4 MB). Its arm64 slice is ad-hoc linker-signed and its x86_64 slice is unsigned, so it runs without a Developer ID (checked with `codesign -dv --arch …`);
  - `go-ios-win.zip` 8.8 MB.
  - Built with `go build -ldflags="-s -w"` (`G:.github/workflows/release.yml:65-164`). No checksum or signature files are published.
  - sha256 measured here: linux `a55fdb4c5073…c125604`, mac `100f225bfdd0…c230c57`.
- **Other channels:** the npm package `go-ios` (`G:npm_publish/package.json`), or `go install` from source (go 1.26, `G:go.mod:3-5`).
- **Recommended: download on first use, pinned.**
  - `droidctl setup` fetches `go-ios-<os>.zip` for a version and sha256 pinned in droidctl's source.
  - It unpacks the right binary to `~/.droidctl/bin/go-ios-1.3.2/ios`.
  - Override with `DROIDCTL_GO_IOS=/path/to/ios`; offline installs use the override.
  - This is the same model as the committed agent APK, but downloaded rather than committed, to keep the wheel small (+16 MB otherwise).
  - Downloading with urllib sets no quarantine attribute on macOS (*inferred*).
- **NOTICE:** if droidctl only downloads the official release, we redistribute nothing. Credit go-ios (MIT) in `NOTICE` anyway, because we depend on its CLI behaviour. If we ever **ship** the binary (in the wheel), we must also carry go-ios's `LICENSE` and the third-party licences of its statically linked modules (gVisor Apache-2.0 and the others in `G:go.mod`).
- **Pinning risk.** The CLI is the contract, and it moves:
  - DDI mount semantics are already changing on `main` (§2.4);
  - `runtest` gained JSON stdout only in 2026-06 (`#573`);
  - Cryptex/iOS 26.4+ support is unreleased;
  - exit codes are inconsistent (`runtest` and `image auto` exit 0 on failure).

  Mitigations:
  - pin one version;
  - parse stdout JSON and stderr JSON logs defensively (logs are JSON by default, `G:cli_setup.go:40-52`);
  - check outcomes ourselves (ping the runner, `LookupImage`, installation_proxy) instead of trusting exit codes;
  - add a doctor line "go-ios <ver> with iOS <x>: tested / untested", like the existing Xcode/iOS pair check (`D:manage.py:139-142`).
  - New iOS majors will need go-ios bumps, so the pin must be easy to move. That is a droidctl release, or `DROIDCTL_GO_IOS` meanwhile.

---

## 6. A design for droidctl

### 6.1 Backend seam

```
droidctl/ios/
  transport.py   usbmux (unchanged; fix USBMUXD_SOCKET_ADDRESS schemes)
  lockdown.py    NEW, split out of syslog.py: pair record, StartSession/TLS, start_service, get_value;
                 installation_proxy (browse, lookup, uninstall); image_mounter (LookupImage,
                 QueryDeveloperModeStatus); amfi reveal. Pure Python, used by BOTH backends.
  syslog.py      unchanged API; imports lockdown
  backend.py     NEW: class Backend (protocol) + pick(udid):
                   DROIDCTL_IOS_BACKEND=xcode|goios, else xcode when devices.developer_dir() else goios
  xcode.py       today's devices.py devicectl calls + runner.py xcodebuild start (the holder)
  goios.py       NEW: binary (resolve/download/verify), run(args) -> (json, logs), agent lifecycle
                 (start/ready/tunnel/shutdown), ensure_ddi, install, ps, kill, runtest holder
  runner.py      build (Xcode only), ids, start/stop/connect -> backend.start_holder / stop_holder
  devices.py     backend-neutral facade: list_devices, find, lock_state, apps, app, install,
                 uninstall, processes, terminate (each -> lockdown or backend)
```

| function today | Xcode backend | go-ios backend |
|---|---|---|
| `devices.list_devices/find` (`D:devices.py:108-118`) | devicectl | usbmux + lockdown `GetValue`. **Drop `cdid` from the callers** (`D:act.py:1835-1837`, `D:setup.py:200`): devicectl's id stays inside `xcode.py` |
| `devices.lock_state` | devicectl | lockdown `PasswordProtected` (both backends could use it) |
| `devices.apps/app/uninstall` | devicectl | lockdown installation_proxy (both could use it) |
| `devices.install` | devicectl | `ios install --path` |
| `devices.processes/terminate` | devicectl | `ios ps --apps` / `ios kill --pid` (needs the agent and the DDI) |
| `devices.developer_dir/xcode_env` | as now | not used |
| `runner.build` | xcodebuild | not available. The signed `.app` comes from the signing work; `build_id` comes from the artifact's manifest instead of `source_hash()`+team (`D:runner.py:33-46`) |
| `runner._start` (`D:runner.py:191-233`) | xcodebuild holder | `goios.ensure_agent` → `ensure_ddi` → install if the build id differs → Popen `ios runtest …` (start_new_session) → the same ping loop; `holder_pid` = the go-ios pid |
| `runner.stop` | `quit` RPC + `killpg` | the same; also `GET /shutdown` on the agent in `teardown`/`daemon stop` only (the agent is shared across runner restarts) |
| `runner.classify_output` | xcodebuild rules | plus go-ios rules ("Did not find test app", "not available in RSD", "could not get pid", "lost connection to testmanagerd", TSS errors), then the lockdown disambiguation (§3.4) |
| `syslog.xcode_busy` | as now | a no-op unless V10 shows a conflict |
| `setup.STEPS` (`D:setup.py:387-390`) | as now | the list in §6.2 |
| `manage.teardown` (`D:manage.py:51-74`) | as now | uninstall through installation_proxy; stop the runner; shut down **our** agent; disclose: DDI mounted until reboot, pair record in `/var/lib/lockdown`, go-ios files under `~/.droidctl/` |
| `manage.doctor` | as now | the checks in §6.2 read-only, plus agent health, the pinned go-ios version, the DDI build, and the iOS range |

### 6.2 `droidctl setup` on Linux

1. **usbmuxd**: the socket answers `ListDevices`. Otherwise `Need("missing-dep")`: "install usbmuxd (Debian/Ubuntu: `sudo apt install usbmuxd`; Fedora: `sudo dnf install usbmuxd`), then reconnect the iPhone."
2. **go-ios**: `DROIDCTL_GO_IOS` or the pinned download (version + sha256). `missing-dep` when offline and absent.
3. **iPhone connected and trusted**: usbmux lists it, and `ReadPairRecord` has a record. Otherwise: "unlock it and tap Trust". usbmuxd's preflight shows the dialog by itself; `ios pair` is the fallback.
4. **iOS version**: ≥ 17.4.1 (§2.1) and ≤ the newest version tested with the pinned go-ios (warn above it).
5. **Developer Mode**: `QueryDeveloperModeStatus`; reveal once; manual instructions (unchanged text).
6. **Unlocked**: lockdown `PasswordProtected` false.
7. **Tunnel**: start our agent (`ios tunnel start --userspace --udid=U --tunnel-info-port=P --pair-record-path=~/.droidctl/ios/go-ios`), wait for `/ready` and `/tunnel/U`.
8. **Developer disk image**: `LookupImage` over lockdown. If something is mounted, keep it. Otherwise `ios image auto --basedir=$DROIDCTL_IOS_DDI or ~/.droidctl/ios/ddi`. The first time this needs internet access to `gs.apple.com` (and to deviceboxhq.com, unless the user supplied a DDI). Verify with `LookupImage`, not the exit code.
9. **Runner app**: a signed `.app` present (from the signing work); install it if the installed build differs.
10. **Developer profile trusted** (free team): detected only through step 11's failure plus the §3.4 disambiguation. Instructions only; we can't open the Settings page.
11. **Start the runner**: `ios runtest …`, then ping with the build id.

### 6.3 macOS without Xcode
- The same go-ios backend: macOS already runs usbmuxd.
- Differences:
  - `--pair-record-path=default` is TCC-blocked on macOS 26 (`G:cmd_tunnel.go:58-66`), so pass our own directory, as on Linux.
  - Coexistence with macOS's own CoreDevice daemons (`remoted`/`remotepairingd`), which may tunnel the same phone, is untested (V11). go-ios's `--address/--rsd-port` can reuse a macOS tunnel instead (`G:main.go:203-208`; proposal `#713`, open).
- `pick()` prefers Xcode whenever `developer_dir()` finds one, so today's users see no change.

### 6.4 Verification plan (real hardware; iPhone 12, iOS 18.7.8, free team, plus the Debian KVM VM with the phone passed through)

| # | measure | pass when |
|---|---|---|
| V1 | packaged usbmuxd on Debian 12/13 and Ubuntu 24.04: version, attach, Trust dialog from preflight, `ReadPairRecord`, our `transport.list_devices`/`connect` to the runner port; usbmuxd surviving a USB replug inside the VM | droidctl lists the phone and pings a runner with no code changes |
| V2 | our lockdown TLS (`syslog.listen`, AMFI reveal) on those distros' OpenSSL 3 | the handshake succeeds; otherwise find the minimal `SSLContext` change |
| V3 | **the DDI × runner matrix**: our Xcode-26.2 runner with (a) 17C52 copied from a Mac, (b) go-ios's 17E5179g. `runtest` + snapshot/tap/type e2e | (a) is required; (b) decides whether users without a Mac-copied DDI can use droidctl |
| V4 | timings, median of 10: agent start → `/ready` and tunnel; `image auto` cold (TSS) and warm; `install`; `runtest` → first ping; per-command overhead of `ios ps`/`kill` | numbers recorded in PLAN/IOS docs; first ping ≤ the xcodebuild 5.3 s or explained |
| V5 | personalized manifest reuse: mount, reboot, mount again offline | know whether every reboot needs Apple's TSS (setup text) |
| V6 | stop paths: `quit` RPC → does go-ios exit? SIGTERM to `runtest` → is the runner gone, and how fast? the same with `runwda` | no runner left running after `stop` |
| V7 | soak: a 60 min session with the daemon ring reading syslog; USB replug; phone lock/unlock; host suspend | the runner and tunnel recover or fail with a typed error; nothing ends at 10 min (`#495`) |
| V8 | failure wording: untrusted profile, expired profile, locked at start, runner not installed, Developer Mode off, DDI not mounted, the free-team 3-app limit at install | each maps to the right `ERROR_KINDS` entry via the go-ios output plus lockdown checks |
| V9 | `ios ps --apps` `RealAppName` vs `pids_of`; installation_proxy attributes for "built by developer" and the app path | `stop-app`, `logs --pkg` and teardown's trust note behave as on macOS |
| V10 | a syslog_relay stream open while `runtest` starts (the macOS "Xcode busy" effect) | no stall, so `xcode_busy` stays Xcode-only |
| V11 | macOS without Xcode in the PATH: go-ios agent next to CoreDevice; a phone also used by Xcode on the same Mac | no tunnel tug-of-war, or documented |
| V12 | iOS 17.4.0 exactly, and an iOS 26.x phone if one is available | confirms the floor (§2.1) and the 26.5 tunnel drop (`#772`) |
| V13 | teardown on Linux: uninstall our runner only; agent stopped; no files left outside `~/.droidctl` (the `selfIdentity.plist` in the CWD!) | `git status`-clean CWD, and the phone's app list without our runner |

---

## 7. What this changes in earlier reports
- `research/ios-transport.md` §4.3 lists the Linux tunnel for iOS 17.0–17.3.1 as "kernel tunnel + NCM (*inferred*)". **Confirmed, and it reaches up to 17.4.0 inclusive** (`G:ios/tunnel/tunnel_api.go:651`, a strict `GreaterThan`).
- `research/ios-transport.md` §2.4 says go-ios mounts the fixed 17E5179g image. Still true in v1.3.2, but **an image already in `--basedir` is now used offline** (1.3.1). That makes "bring your own DDI from a Mac" a supported path.
- `research/ios-decision.md` §4 ("Linux / no Xcode: go-ios `runtest`"): the exact invocation is §3.2 above. `runtest` is a session holder like xcodebuild, but with weaker error reporting (§3.4).
