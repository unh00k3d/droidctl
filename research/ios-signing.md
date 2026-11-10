# iOS signing and provisioning without Xcode (macOS and Linux)

How droidctl can get its XCUITest runner signed for a user's iPhone **without `xcodebuild -allowProvisioningUpdates`**: on macOS (to escape the "No Accounts" blocker, `IOS-HANDOFF.md` §7.0) and on Linux (no Xcode at all). The device half on Linux (install, launch, tunnel, DDI) is go-ios and is covered by a separate report; this one covers **build, sign, provision**.

_2026-11-09. Nothing here was run: no repo code was executed, nothing was signed, no account was signed in to, and the iPhone was not touched. Every "works" means "the code, its docs or its CI claim it works". Estimates are labelled as estimates._

**Sources read** (shallow clones, untrusted; commit and date of HEAD):

| prefix | repo | HEAD | license (checked in the file) |
|---|---|---|---|
| `W:` | appium/WebDriverAgent | `3d6a4e0` 2026-10-17 | BSD-3 (`LICENSE`) |
| `X:` | appium/appium-xcuitest-driver | `e76f78c` 2026-11-08 | Apache-2.0 |
| `G:` | danielpaulus/go-ios | `6910f3b` 2026-11-03 | MIT |
| `GC:` | aluedeke/go-codesign (go-ios's signer) | `5ed22b9` 2025-12-28 | MIT |
| `M:` / `DK:` | mobile-next/mobilecli, mobile-next/devicekit-ios | `83e47ca`, `90e2c3e` | **FSL-1.1 (Functional Source License, ALv2 future)**: not open source today; ideas only |
| `R:` | indygreg/apple-platform-rs (rcodesign = `apple-codesign`; `app-store-connect`) | `0ebbd2e` 2026-08-08 | `apple-codesign` **MPL-2.0**; `app-store-connect` Apache-2.0 OR MIT (per-crate `Cargo.toml`) |
| `Z:` | zhlynn/zsign | `9299685` 2026-10-20 | MIT |
| `L:` | ProcursusTeam/ldid | `af86971` | **AGPL-3.0** (`COPYING`) |
| `AS:` / `ASig:` | altstoreio/AltStore, rileytestut/AltSign | `56854e6`, `1c44cfd` (2020!) | AltStore **AGPL-3.0**; AltSign MIT (`AltSign.podspec:7`, no LICENSE file in the tree) |
| `SS:` / `SSA:` | SideStore/SideStore, SideStore/apple-private-apis | `4f3d18d`, `03beb1a` (2024-11) | SideStore **AGPL-3.0**; apple-private-apis **MPL-2.0** |
| `SL:` / `PV:` / `AV:` | Dadoum/Sideloader, Dadoum/Provision, Dadoum/anisette-v3-server | `631abcc`, `7717ce1`, `6848957` | Sideloader **GPL-3.0**; Provision **LGPL-2** (`COPYING-LGPLv2`); anisette-v3-server **no license file** (all rights reserved by default) |
| `XT:` | xtool-org/xtool | `a2875dd` 2026-11-07 | MIT (`LICENSE.md`) |
| `IL:` / `IS:` | nab138/iloader, nab138/isideload | `96ed061`, `326e562` | MIT (iloader's branding excluded, `LICENSE-BRANDING`) |
| `PI:` | khcrysalis/PlumeImpactor | `b13d995` 2026-10-24 | MIT (vendors rcodesign 0.29 MPL-2.0 under `3rdparty/`) |
| `I:` | facebook/idb | `291575b` | MIT |
| `XL:` | Xcode 26.2 `Contents/Resources/en.lproj/License.rtf` ("Xcode and Apple SDKs Agreement", licenseID EA1910) | local | — |
| `D:` | droidctl's own built runner, `~/.droidctl/ios/build/<team>/Build/Products/Debug-iphoneos/DroidctlRunner-Runner.app` (Xcode 26.2), inspected with `otool`/`codesign -d`/`plutil` only | local | — |
| `A:` | Apple docs (JSON of developer.apple.com/documentation/appstoreconnectapi/…) | fetched 2026-11-09 | — |

---

## TL;DR

1. **A prebuilt, unsigned runner is standard practice and works on iOS 17+.** Appium builds WDA with `CODE_SIGNING_ALLOWED=NO` on CI and ships it **with `Frameworks/` removed**, so XCTest resolves from the device's personalized DDI (`W:Scripts/ci/build-real.sh:3-18`, `X:docs/guides/run-prebuilt-wda.md:72-80`). go-ios and mobilecli then re-sign it per user. Our runner already carries the DDI rpaths (`/System/Developer/Library/Frameworks`, `D:` `otool -l`). Stripped, the bundle is ~0.4 MB of our code + Apple's 131 KB `XCTRunner` host binary (est. from `D:`), instead of 20 MB.
2. **Redistributing Apple's XCTest frameworks is not permitted by the Xcode license** (`XL:` §2.7 "not … redistribute … the Apple Software … in whole or in part"). Stripping `Frameworks/` removes ~18 MB of it. The **host executable is still Apple's `XCTRunner`** (thinned copy of `Platforms/iPhoneOS.platform/Developer/Library/Xcode/Agents/XCTRunner.app/XCTRunner`, `D:`). Appium, go-ios's download and mobilecli all ship it anyway. It's a residual risk the user must accept, or we avoid it (§A.4).
3. **Signing without Xcode is solved, on Linux too.** Options: go-codesign (MIT, Go, used by go-ios with real-device e2e), zsign (MIT, C++, signs `.xctest` nested bundles, `Z:src/bundle.cpp:93-96`) and rcodesign (MPL-2.0, Rust, recursive by default). Our runner needs only four entitlements (`application-identifier`, `team-identifier`, `get-task-allow`, `keychain-access-groups`, `D:`) and one profile, so it's the easy case. ldid is AGPL: avoid. On macOS, `/usr/bin/codesign` ships with the OS, not Xcode.
4. **Paid team: fully solved by the App Store Connect API, from any OS, without a password.** A **team** API key (Individual keys "aren't able to use Provisioning endpoints", `A:` creating-api-keys) can create the cert (CSR → `POST /v1/certificates`), register the device, register the bundle ID and create an `IOS_APP_DEVELOPMENT` profile. go-ios already does exactly this (`G:ios/signing/appstoreconnect.go:73-216`), as does rcodesign's `app-store-connect` crate. JWTs live ≤ 20 min and are minted locally from the `.p8`, so renewal needs no human.
5. **Free Apple ID: only the private Xcode protocol works.** That means a GrandSlam (GSA) SRP login, so the password goes to Apple only, never in the clear. It needs 2FA once per "machine" and **anisette** headers, then the `developerservices2.apple.com/services/QH65B2/*.action` plist API that Xcode uses. Anisette comes from:
   - **macOS:** natively from `AOSKit` (`AS:AltServer/Anisette Data/AnisetteDataManager.swift:118-136`).
   - **Linux:** a remote anisette-v3 server (xtool's default is a third-party host, `XT:…/OmnisetteADIProvider.swift:11`) or a self-hosted `anisette-v3-server` in Docker. The server never sees the password (`XT:Sources/XToolSupport/AuthCommand.swift:81-87`).

   Free-team limits: 3 apps, **10 App IDs per 7 days** (`ASig:…/NSError+ALTErrors.m:104`), 7-day profiles (`TimeToLive 7`, `D:`), and a cap on development certificates (error 7460, `IS:…/cert_identity.rs:334-352`).
6. **Renewal without the password is possible.** Store the `adsid` + `com.apple.gs.xcode.auth` token (xtool `AuthToken.swift:60-85`, PlumeImpactor `plume_store/src/gsa_account.rs:4-8`) plus the anisette machine state. Once that token expires, either the password is needed again (iloader keeps it in the OS keyring, `IL:src-tauri/src/account.rs:53,88-91`) or the user re-logs in; the same anisette identity avoids a second 2FA (`PV:README.md:19-21`). **The token lifetime is unmeasured**: Apple returns it as `duration`/`expiry` (`IS:isideload/src/auth/apple_account.rs:1007-1017`). Measure it.
7. **Recommendation (§D):** ship a prebuilt, stripped, unsigned runner. Sign it in-process (Python port of the ~1k-line nested-bundle signer, or go-codesign/zsign as a subprocess). Provision through two backends:
   - **ASC API key** (paid, any OS, zero prompts);
   - **Apple ID login** (free, any OS; native anisette on macOS, self-hosted or opt-in remote anisette on Linux), porting from MIT xtool/isideload/AltSign/PlumeImpactor.

   Keep `xcodebuild` as a fallback on macOS. Never revoke a certificate droidctl didn't create.

---

## A. Prebuilt runner

### A.1 What the bundle contains today (`D:`, Xcode 26.2, Debug-iphoneos, 20 MB)
| path | size | owner |
|---|---|---|
| `DroidctlRunner-Runner` (host executable) | 131 KB | **Apple**: the XCTRunner agent, thinned to arm64 (same `otool -L` as `Xcode.app/…/Agents/XCTRunner.app/XCTRunner`, which is arm64+arm64e) |
| `PlugIns/DroidctlRunner.xctest` | 204 KB (+1.6 MB dSYM, droppable) | **ours** (bundle id `dev.droidctl.r<hex>.runner`, type BNDL) |
| `Frameworks/XCTest, XCTestCore, XCUIAutomation, XCTAutomationSupport, XCUnit, XCTestSupport, Testing .framework`, `libXCTestSwiftSupport.dylib` | ~18 MB | **Apple** (from `Platforms/iPhoneOS.platform/Developer/Library/{Frameworks,PrivateFrameworks}`) |
| `Info.plist` | — | Xcode-generated from the XCTRunner template (usage strings, `MinimumOSVersion 13.0`) |
| `embedded.mobileprovision`, `_CodeSignature` | — | per user |

- **Entitlements of the signed app** (`codesign -d --entitlements`): `application-identifier`, `com.apple.developer.team-identifier`, `get-task-allow = 1`, `keychain-access-groups`. The template asks for only `get-task-allow` (`XCTRunner.app/RunnerEntitlements.plist`).
- **Profile:** "iOS Team Provisioning Profile: dev.droidctl.r….runner.xctrunner", `IsXcodeManaged 1`, `TimeToLive 7`, explicit App ID (no wildcard on a free team).
- **Rpaths:**
  - host: `@executable_path/Frameworks`, `/System/Developer/Library/Frameworks`, `/System/Developer/usr/lib`, `/Developer/Library/Frameworks`, `/Developer/usr/lib`;
  - our `.xctest`: `@loader_path/Frameworks`, and it links `@rpath/XCTest.framework` + `@rpath/libXCTestSwiftSupport.dylib`.

  So with `Frameworks/` gone, the host's rpaths reach the DDI copies. *Inferred:* dyld also searches the main executable's rpaths for the dlopen'd `.xctest`. WDA proves the pattern, but it adds DDI rpaths to its own framework too (`W:WebDriverAgent.xcodeproj/project.pbxproj:5113-5121`). Add `/System/Developer/Library/{Frameworks,PrivateFrameworks}` to our test target's `LD_RUNPATH_SEARCH_PATHS` to be safe.

### A.2 How others ship it
| project | real-device artifact | XCTest frameworks | who signs |
|---|---|---|---|
| **Appium WDA** | `WebDriverAgentRunner-Runner.zip` per release, built `xcodebuild build-for-testing … CODE_SIGNING_ALLOWED=NO ARCHS=arm64` (`W:Scripts/ci/build-real.sh:3-8`, matrix `W:.github/workflows/publish.js.yml:38`) | **excluded** "so XCTest dependencies resolve from the real device's developer support libraries" (`W:Scripts/ci/build-real.sh:12-18`; since v5.10.0, `W:CHANGELOG.md:1783`) | the user, with any third-party tool; Appium only launches it (`X:docs/guides/run-preinstalled-wda.md:39-51`) |
| Appium driver | iOS 17+ only for prebuilt/preinstalled (`X:…/run-prebuilt-wda.md:66-70`) | "The runner app should not include embedded `Frameworks/XC**` copies" (`X:…/run-preinstalled-wda.md:71`); a runner installed by xcodebuild *with* them "can cause the runner process to exit" when launched preinstalled (`:80-82`) | — |
| **go-ios** | `ios ui install (wda|devicekit)` downloads a prebuilt artifact (from deviceboxhq.com) or takes `--path`, then signs and installs (`G:main.go:498-500`) | as shipped | go-codesign (`G:ios/signing/signing.go:320-378`) |
| **mobilecli / devicekit-ios** (FSL) | unsigned IPA from `make ipa-unsigned` (`DK:Makefile:53-71`); user runs `mobilecli agent install --provisioning-profile …` (`M:README.md:322`) | **embedded**, and with Xcode 27, which "stopped embedding XCTest in the runner", it copies them in (`DK:scripts/patch-runner.sh:18-52`) | mobilecli |
| **idb** | no prebuilt device runner; on a device the user brings a signed test bundle; idb ad-hoc-signs only for the simulator (`I:FBControlCore/Configuration/XCTestShimConfiguration.swift:84-89`) | — | user / Xcode |

Takeaways:
- The two strategies conflict. Appium strips the frameworks for a runner *launched as a plain app* over CoreDevice process control: `X:…/run-preinstalled-wda.md:56-62` documents env vars `USE_PORT`, `WDA_PRODUCT_BUNDLE_IDENTIFIER`, `W:lib/wda-strategies.ts:318-333`. mobile-next embeds them for a runner started through an XCTest session.
- For droidctl, which starts the runner through an XCTest session (xcodebuild today, go-ios `runtest`/`ui run` on Linux), **both should work**. Which one is right is a measurement (§D.4 M1).
- Note `X:…/run-preinstalled-wda.md:58-62`: on iOS 27, devicectl-launched runners never background. That's the other report's concern.

### A.3 Can the runner rely on the iOS 17+ personalized DDI?
**Yes; this is what Appium ships for iOS 17+** (`X:…/run-prebuilt-wda.md:77-78`: "WebDriverAgent packages for real devices do not have embedded XCTest frameworks so that they can run on iOS 17+ devices").
- The DDI mounts `/System/Developer/Library/{Frameworks,PrivateFrameworks}` and `/System/Developer/usr/lib`, which the rpaths above target.
- Risk: our `.xctest` was compiled against the Xcode 26.2 SDK, and the DDI's XCTest is whatever version the host mounted. The link uses `compatibility version 0.0.0` (`D:` `otool -L`), so dyld won't refuse it, but a newer symbol we call could be missing on an older DDI.
- Mitigation: build with the oldest Xcode that supports our features, and keep the runner ObjC with a small API surface (it already is).

### A.4 Licensing of what we'd ship
- **Xcode and Apple SDKs Agreement** (`XL:`):
  - The header says Apple Software is authorized "ONLY FOR EXECUTION ON AN APPLE-BRANDED PRODUCT RUNNING MACOS" (line 5).
  - §2.2: no distributing Applications built with the iOS SDK without a separate agreement.
  - §2.5: copies only "in its entirety".
  - §2.7: "You agree not to … upload to or host on any website or server, sell, redistribute, or sublicense the Apple Software … in whole or in part".
- **Consequences (not legal advice):**
  - Shipping the 8 frameworks/dylib: clearly redistribution. **Strip them** (as Appium does).
  - The `XCTRunner` host binary: still Apple's. Appium (BSD project), go-ios's download and mobilecli all redistribute it, and nobody we read addresses it.
  - Options:
    - (a) accept the industry-standard risk;
    - (b) don't ship a prebuilt runner on macOS (build it locally with the user's Xcode, as now), and ship it only for Linux;
    - (c) replace XCTRunner with our own tiny host app that links `XCTest` from the DDI. Unverified whether testmanagerd accepts a non-XCTRunner host. Spike-worthy, not a plan.
  - Our `.xctest` was compiled with the iOS SDK. That is normal app development and is our own code.
  - The Apple ID route itself uses Apple's private developer services (§C), whose "Apple Services" terms (`XL:` §2.9, line 94: "access such Apple Services only through the mechanisms provided by Apple") don't bless third-party clients. Every sideloading tool lives with this; see §D.3.

---

## B. Signing without Xcode

What the runner needs, as a checklist:
1. Sign the nested `PlugIns/*.xctest` (and frameworks, if kept) first, then the app.
2. Embed one `embedded.mobileprovision`, in the app only.
3. Rewrite `CFBundleIdentifier` to the per-team id (app `…runner.xctrunner`, plugin `…runner`).
4. Entitlements from the profile.
5. CMS signature with the Apple Development cert + key, SHA-256 code directories.

| tool | language / form | Linux | nested bundles, `.xctest` | profile + entitlements | bundle-id rewrite | license | notes |
|---|---|---|---|---|---|---|---|
| **go-codesign** | Go lib + CLI (also `npx`) | yes (`GC:README.md:14-20`) | "Automatically signs frameworks, plugins, and extensions"; nested first, CodeResources after (`GC:pkg/codesign/resign.go:78,102-103`) | yes; no profile in nested bundles (`resign.go:78`) | yes (`resign.go:164`) | **MIT** | "Alpha" (`README.md:9`). Used by go-ios `ios sign app`, which has real-device signing e2e (`G:CHANGELOG.md:94`). **Best fit to port or call.** |
| **zsign** | C++ CLI (OpenSSL) | yes (`Z:README.md:31,56-80`) | `.app/.appex/.framework/.xctest` (`Z:src/bundle.cpp:93-96`) | `-m` profile (several for extensions), `-e` entitlements (`README.md:104,114`) | `-b` | **MIT** (`LICENSE`, © 2026 zhlynn) | Mature, fast; the sideloading community's default. Subprocess candidate; we would need to build/ship binaries per OS. |
| **rcodesign** (apple-codesign) | Rust CLI/lib | yes | recursive by default (`R:apple-codesign/docs/apple_codesign_rcodesign_signing.rst:14-20`) | **no provisioning-profile handling** (no `mobileprovision` in `R:apple-codesign/src`); copy the profile and pass `--entitlements-xml-path` yourself | manual | **MPL-2.0** (file-level copyleft; OK as a subprocess or unmodified dependency) | Last release 0.29.0 (2024-11-29, `R:apple-codesign/CHANGELOG.md:37-39`); still used by xtool (`swift-rcodesign`, `XT:Package.swift:55`), PlumeImpactor and isideload (`apple-codesign-quick`, `IS:README.md`). |
| **ldid** (Procursus) | C++ | yes | yes | entitlements yes | — | **AGPL-3.0** | **Avoid** (licence, and aimed at jailbreak use). |
| `/usr/bin/codesign` | macOS system tool | no | `--deep` discouraged; sign inside-out | `--entitlements` | manual | Apple (ships with macOS, **not** with Xcode) | On macOS it signs with the keychain identity Xcode already created: no new cert needed. |

**Can they produce a runner iOS 17/18 accepts?** Evidence is indirect but good. All three are used to re-sign WDA/DeviceKit runners (go-ios `ui install`, iloader/xtool/Plume for apps). The runner is the simplest case: no app extensions, no app groups, four plain entitlements. Unverified on our phone: §D.4 M2.

**Cost of porting to Python** (estimate): a Mach-O code-signature writer is code directory + requirements + entitlements (XML + DER) + a CMS blob. With CodeResources that is ~800–1500 lines, plus `cryptography`/`asn1crypto` (both permissive). go-codesign (MIT) is the cleanest reference. The alternative is shipping a go-codesign binary per OS (we will ship go-ios on Linux anyway, and go-ios *contains* go-codesign: `ios sign app`). **Cheapest path: call `go-ios sign app` as a subprocess on Linux.**

---

## C. Getting a certificate + profile without Xcode

### C.1 Paid team: App Store Connect API (official, any OS)
- **Key:** a **Team** API key. "Individual keys aren't able to use Provisioning endpoints"; generating team keys needs an **Admin** account in App Store Connect (`A:` creating-api-keys). Which non-Admin roles may write certificates/profiles is not stated on that page. Check it in the role table before documenting a minimum role.
- **Auth:** an ES256 JWT minted locally from the `.p8`. "For most requests, App Store Connect rejects a token with a lifetime greater than 20 minutes" (`A:` generating-tokens). There is no session to expire; renewal is fully unattended as long as the key file exists.
- **Flow,** as go-ios implements it in `G:ios/signing/appstoreconnect.go`:
  1. `EnsureBundleID` `GET/POST /v1/bundleIds` (`:73-104`);
  2. `EnsureDevice` `GET/POST /v1/devices` (`:115-145`);
  3. `CreateCertificate` from a locally generated RSA CSR, `certificateType IOS_DEVELOPMENT` (`:147-170`, CSR `:336`);
  4. `CreateDevelopmentProfile` `profileType IOS_APP_DEVELOPMENT` with bundle id + cert + device (`:191-225`).

  CLI: `ios sign provision appstoreconnect --bundleid … --asc-key-id … --asc-issuer-id … --asc-private-key … --p12-output … --profile-output … [--revoke-existing]` (`G:main.go:489-491`).
- The `certificateType` enum has `DEVELOPMENT` and `IOS_DEVELOPMENT` (`A:` certificatetype).
- rcodesign's sister crate offers the same through a CLI: `bundle register/list`, `certificate create/list`, `device register/list`, `profile create/list` (`R:app-store-connect/src/cli.rs:93-102,202-210,270-282,335-353`), under Apache-2.0 OR MIT, so it is portable.
- **Device-registration cost:** a paid team gets 100 devices per type per membership year (Apple's published limit, from memory; not re-checked here). A profile lasts 1 year (an Apple fact we did not re-check here; measure it with M3).
- **Certificates:** each new dev cert consumes a slot. **Create one droidctl cert, store its key, and reuse it.** Never mass-revoke: go-ios's `--revoke-existing` revokes "a leftover go-ios certificate" (`G:main.go:490`), but `revokeAllDevelopmentCertificates` (`G:ios/signing/signing.go:269`) is broader. Don't copy that.
- **fastlane** (MIT, Ruby) offers the same with `match`/`sigh` (ASC key or Apple-ID cookie login). It's a heavy Ruby dependency and doesn't add anything for us.

### C.2 Free Apple ID: the Xcode-private protocol
**Login (GrandSlam):**
- `gsa.apple.com/grandslam/GsService2` lookup (`XT:Sources/XKit/GrandSlam/Lookup/GrandSlamLookupManager.swift:15`).
- SRP init/complete: the password is used in SRP, not sent; requests in `XT:Sources/XKit/GrandSlam/Requests/Concrete/`.
- 2FA: trusted-device push or SMS (`IS:isideload/src/auth/apple_account.rs:47-111`; `SL:frontends/cli/source/cli_frontend.d:130-140`).
- Then an **app token for `com.apple.gs.xcode.auth`** with `duration` and `expiry` (`XT:…/GrandSlamFetchAppTokensOperation.swift:17,29-36`; `IS:…/apple_account.rs:1007-1017`).

**Anisette** (`X-Apple-I-MD`, `-MD-M`, `-MD-RINFO`, device headers) proves a "real" Apple machine:
| where | how | trust/ToS notes |
|---|---|---|
| macOS | `AOSKit` `AOSUtilities.retrieveOTPHeadersForDSID("-2")` + `machineUDID`/serial, dlopened from `/System/Library/PrivateFrameworks` (`AS:AltServer/Anisette Data/AnisetteDataManager.swift:118-160`). AltServer falls back to an XPC helper and a Mail plug-in (`:70-110`). AltDaemon uses AuthKit's `AKAppleIDSession`/`AKDevice` (`AS:AltDaemon/AnisetteDataManager.swift:29-59`). | Native: the Mac's own identity, no third party. From Python: a ~50-line ObjC helper or `ctypes` + objc runtime (estimate). Whether a non-Apple-signed process still gets headers on macOS 15 is **unverified** (M5). |
| Linux, remote | anisette **v3** protocol: client keeps the ADI state, server only computes OTPs. xtool defaults to `https://mac-ani.jkcoxson.com` (`XT:…/OmnisetteADIProvider.swift:7-12`); list at `servers.sidestore.io` | "your credentials are only ever sent to Apple" (`XT:Sources/XToolSupport/AuthCommand.swift:81-87`). The server learns your machine identity and timing. A third party in the loop: opt-in only. |
| Linux, self-hosted | `docker run … dadoum/anisette-v3-server` (`AV:README.md`), built on Provision, which runs Apple Music for Android's `libstoreservicescore.so`/`libCoreADI.so` (`PV:README.md:51-53`) | No third party. Executes Apple binaries on non-Apple hardware (same ToS problem as all sideloaders). anisette-v3-server has **no license** → don't vendor; let the user run it. |
| Linux, in-process | Provision (LGPL-2, D) or SideStore `omnisette` (MPL-2.0, Rust) | LGPL/MPL as a separate binary is fine; still the Android-library route. |

**Developer services** (what Xcode calls): `https://developerservices2.apple.com/services/QH65B2/<platform>/<action>.action?clientId=XABBG36SBA`, XML-plist bodies.
- References: `XT:Sources/XKit/DeveloperServices/DeveloperServicesAPIVersion.swift:88-110`, `SL:source/server/developersession.d:36-38`, PlumeImpactor `crates/plume_core/src/developer/qh/`.
- Headers: `X-Apple-GS-Token` (adsid + xcode token) + anisette.
- Actions used by every sideloader: `listTeams`, `addDevice`/`listDevices`, `addAppId`/`listAppIds`, `listAllDevelopmentCerts`, `submitDevelopmentCSR` (`IS:isideload/src/dev/certificates.rs:150-172`), `revokeDevelopmentCert` (`:128-145`), `downloadTeamProvisioningProfile` (`SL:frontends/cli/source/app_id.d:184-235`).
- xtool also speaks the newer JSON:API flavour through an OpenAPI client (`XT:Sources/DeveloperAPI`).

**Free-team limits** (from code comments/strings; they are Apple's server rules, not documented by Apple):
- **3 installed apps/extensions** per device: our own `runner.classify_output` matches it; `SS:AltStore/My Apps/MyAppsViewController.swift:1047`.
- **10 App IDs per 7 days**: `ASig:AltSign/Categories/NSError+ALTErrors.m:104`, server code 9120 (`ASig:AltSign/Apple API/ALTAppleAPI.m:377-380`); `SS:…/AppIDsViewController.swift:304`. Deleting App IDs doesn't give the quota back (`SL:frontends/cli/source/app_id.d:131`).
- **7-day profiles** (`D:` TimeToLive 7).
- **Development-certificate cap**: error **7460** "Maximum number of certificates reached" (`IS:isideload/src/sideload/cert_identity.rs:334-352`). isideload offers revoke / error / prompt policies (`IS:…/sideload/builder.rs:37-43`). The exact number (commonly said to be 2 for free teams) is **unverified**.
- **Danger for droidctl:** the user's Xcode already holds a free-team cert. If droidctl submits its own CSR it may hit 7460, and revoking Xcode's cert breaks the user's own Xcode signing. **Rule:**
  - On macOS, reuse the existing Apple Development identity (its key is in the login keychain, usable by `/usr/bin/codesign`).
  - Otherwise create one cert named "droidctl", keep its key, and never revoke a cert droidctl didn't create without asking.
  - Team profiles from `downloadTeamProvisioningProfile` include the team's dev certs (*inferred* from Xcode-managed profile behaviour). Check with M3.

### C.3 Libraries/CLIs we could call or port
| project | lang | what | storage of secrets | license → use |
|---|---|---|---|---|
| **xtool** | Swift (Linux/WSL/macOS) | `auth login` (password or ASC key, `XT:Sources/XToolSupport/AuthCommand.swift:10-16`), `ds teams/certificates/devices/identifiers/profiles`, `install <ipa>` = provision + rcodesign + install | token (not password) + team in a plain file (`DirectoryStorage`, `XT:Sources/XKit/Utilities/KeyValueStorage.swift:28,87-93`) | **MIT → port freely.** As a subprocess it rewrites bundle ids to its own `XTL-<id>.…` scheme (`XT:Sources/XKit/Utilities/ProvisioningIdentifiers.swift:24-27`) and needs a Swift runtime: better as reference. |
| **isideload** (+ iloader GUI) | Rust lib | full Apple-ID login, dev API, cert reuse/7460 handling, sign (apple-codesign-quick), install (idevice) | iloader: **password in OS keyring**, re-login each refresh (`IL:src-tauri/src/account.rs:53,88-91`) | **MIT → port** |
| **PlumeImpactor / plumesign** | Rust CLI | `account login/certificates/devices/register/app-ids`, `sign`, `device install` (`PI:apps/plumesign/src/commands/account.rs:23-37`) | **adsid + xcode GS token, not the password** (`PI:crates/plume_store/src/gsa_account.rs:4-8`); validates by `listTeams` (`PI:crates/plume_core/src/developer/session.rs:71-73`) | **MIT** (vendored rcodesign MPL) → port or subprocess |
| AltSign | ObjC | the original GSA + dev-API client | — | MIT, but 2020 HEAD on default branch: reference only |
| Sideloader | D | CLI `team/cert/app-id/device/install`; asks password every run ("TODO Keyring", `SL:frontends/cli/source/cli_frontend.d:109-123`) | none | **GPL-3 → ideas or subprocess only** |
| AltStore/AltServer, SideStore | Swift | GUI apps | not checked | **AGPL → ideas only** |
| apple-private-apis (`icloud_auth`, `omnisette`) | Rust | GSA + anisette | — | MPL-2.0, stale since 2024-11 |
| go-ios | Go | ASC-key provisioning + go-codesign signing (`G:main.go:144-145,489-500`) | ASC key via flags/env `GO_IOS_ASC_*` (`G:ios/signing/appstoreconnect.go:33-50`) | **MIT → subprocess (already planned) and port** |

**Maintained subprocess for the free-team login?** No MIT CLI does exactly "log in once, then emit a cert+profile for bundle X, device Y, non-interactively":
- plumesign is closest (MIT, stores token);
- xtool is close but rewrites bundle ids;
- go-ios has no Apple-ID login (ASC only).

Porting the login + 6 dev-API calls into Python is the cleaner option (estimate: 600–1000 lines; SRP via a small permissive lib or hand-rolled, plus plist and anisette clients).

### C.4 Renewal without the password; how long it takes
- **Paid (ASC key):** no session; a JWT per run. Renewal = `POST /v1/profiles` (+ re-sign + reinstall). Fully unattended.
- **Free (Apple ID):**
  - Persist `adsid` + the `com.apple.gs.xcode.auth` token (+ expiry) and the anisette identity. On macOS that identity is the Mac's own (AOSKit); on Linux it's the client-side ADI state (Provision stores `~/.adi/adi.pb`, `PV:README.md:19-21`; xtool ties the token to its anisette provider id, `XT:Sources/XToolSupport/AuthToken.swift:64-70`).
  - While the token is valid, renewal is silent (PlumeImpactor's model).
  - The **token lifetime is not in any code we read**; it's server-provided. iloader's choice to keep the password in the keyring *suggests* tokens don't reliably outlast weeks (inference).
  - Re-login on the same anisette identity normally skips 2FA ("your machine is remembered as safe", `PV:README.md:19-21`). xtool's `logout --reset-2fa` exists for the opposite case (`XT:Sources/XToolSupport/AuthCommand.swift:162-172`).
- **Duration, estimates only:**
  - dev-API calls: ~1–3 s;
  - re-sign of a stripped ~0.4 MB runner: <1 s (20 MB with frameworks: a few s);
  - install: ~5–10 s.

  The measured comparison is our Xcode re-sign, 4.5 s (`IOS-HANDOFF.md` §7.1.4), and the full first build+sign+install+start, 30 s (§7.3).
- **Re-sign vs re-install:** a new profile means a new signature, which means reinstalling the runner (it's the same bundle id, so no extra app slot or App ID). The App ID is reused across renewals, so the 10/week quota is not consumed.

---

## D. Recommendation

### D.1 Design
1. **Runner artifact.** CI (macOS, pinned Xcode) builds `build-for-testing CODE_SIGNING_ALLOWED=NO`, then strips `Frameworks/` and the dSYM. Publish one `droidctl-runner-<ver>.zip` per release, with a sha256, **not** inside the wheel (keeps the wheel free of Apple binaries; download on first `setup`). Add the DDI rpaths to the test target. Keep a "build locally with Xcode" path on macOS for users who'd rather not download Apple's XCTRunner from us (§A.4).
2. **Signer.** One function `sign(app, identity, profile, bundle_prefix)`:
   - macOS: `/usr/bin/codesign` with the keychain identity (no extra binary, reuses Xcode's cert).
   - Linux: `go-ios sign app` (go-codesign), since go-ios is already the Linux device layer.
   - Later, optionally, a Python port of go-codesign (MIT) to drop the dependency.
3. **Provisioner,** two backends behind one interface `ensure_profile(team, udid, bundle_id) -> (identity, profile)`:
   - **`asc`** (paid, any OS): `droidctl setup --asc-key <p8> --asc-key-id … --asc-issuer …`. Port ~250 lines from go-ios's MIT client, or call `go-ios sign provision appstoreconnect`. The key lives in droidctl's home with 0600 permissions (or the OS keyring).
   - **`appleid`** (free or paid, any OS): `droidctl login`.
     - It runs GSA SRP, so droidctl's process sees the password once and only Apple receives the SRP proof. That is weaker than "only Apple's UI sees it" (§D.3).
     - 2FA code at the prompt (or the push on the user's devices).
     - The team pick.
     - Store adsid + token + expiry (+ on Linux the ADI state) in the OS keyring (macOS Keychain / Secret Service), never the password by default. `--remember-password` opt-in for unattended re-login, iloader-style.
     - Anisette: AOSKit on macOS; on Linux `--anisette <url>` with a self-hosted server documented first and a public v3 server only as an explicit opt-in.
   - **`xcode`** (macOS, today's path): stays as the fallback when Xcode's account works.
4. **Renewal:** unchanged trigger (`runner.renew_if_due`, <1 day left). The provisioner fetches a fresh profile; the signer re-signs; the device layer reinstalls; the runner restarts. With a valid token or an ASC key, renewal is silent. With an expired token, a typed error `signed-out` says "run `droidctl login`" (the existing `signed-out` kind fits).
5. **Certificates:** reuse before create. Never revoke what droidctl didn't create. On 7460, stop with a typed error listing the certs (`signing`/`cert-limit`), and leave the choice to the user.

### D.2 User-facing steps
| | macOS | Linux |
|---|---|---|
| free | `droidctl setup` → (if Xcode's account works: as today) else prompts `droidctl login` once (Apple ID, password, 2FA) → done; renewals silent while the token lives | install go-ios (+ usbmuxd), optionally `docker run dadoum/anisette-v3-server` → `droidctl login --anisette http://localhost:6969` (Apple ID, password, 2FA) → `droidctl setup` → trust the developer profile on the phone (Settings > VPN & Device Management), as today |
| paid | create a Team API key once (App Store Connect > Users and Access > Integrations, Admin) → `droidctl setup --asc-key …` → never prompted again | same |

### D.3 Risks
- **Apple ToS:**
  - Third-party clients of GSA/developerservices are unsanctioned (`XL:` §2.9 line 94).
  - Running Apple's Android ADI libraries on Linux runs Apple software on non-Apple hardware (`XL:` line 5, §2.7).
  - Apple can disable the account's provisioning (`XL:` §2.3: "may revoke Your Provisioning Profiles at any time").
  - Practice: thousands of AltStore/SideStore users, no known bans for this use, but xtool and Provision both advise a secondary Apple ID (`XT:…/AuthCommand.swift:86`, `PV:README.md:5-7`). Say so in the docs.
  - ASC is the only official route (paid).
- **Password exposure:** with `appleid`, droidctl's process reads the password (as AltServer, xtool and iloader do). The truly "only Apple's flow" option on macOS is today's Xcode account. A browser-based OAuth-style flow for Xcode tokens does not exist (none of the projects found one). Mitigations: read the password from the tty with no echo, never log it, don't store it unless asked, and use SRP (the password itself never goes on the wire).
- **Token storage:**
  - The xcode GS token + adsid can manage the team's certs/devices/App IDs until it expires. Treat it like a password: OS keyring, 0600 fallback file, `droidctl logout` deletes it.
  - xtool stores it in a plain file (`DirectoryStorage`); we should do better.
  - An ASC `.p8` is long-lived and powerful (Admin key): the same treatment, plus docs telling users to revoke it in ASC when done.
- **Remote anisette:** a third party learns your machine identity and activity timing (not the password). Default off.
- **Free limits:** the 3-app and 10-App-ID/week rules are unchanged. Keep one fixed runner bundle id per team (as now) so renewals don't consume App IDs.
- **XCTRunner redistribution** (§A.4).
- **DDI/XCTest version skew** with a stripped runner (§A.3).

### D.4 What to measure (each a small spike; none needs the user's password except M4–M6)
- **M1, stripped runner:** build our runner unsigned on this Mac, strip `Frameworks/`, sign with `/usr/bin/codesign` + the existing cached profile, install, and start it through `xcodebuild test-without-building` and through go-ios `runtest`.
  - Pass: ping with our build id on iOS 18.7.8.
  - Also try the unstripped bundle, to pick the strategy in §A.2.
- **M2, signers on Linux** (go-codesign via `go-ios sign app`, zsign, rcodesign): re-sign the same unsigned zip with the same p12+profile. Pass: install + start, and `codesign -dv --verbose=4` on the Mac shows equivalent CodeDirectory/entitlements to the Xcode-signed one.
- **M3, ASC** (needs a paid team, so unverified until one exists): fresh key → cert + device + bundle id + profile.
  - Time each call.
  - Check the profile's `ExpirationDate` (expect ~1 year) and that renewal needs no prompt.
- **M4, free-team login via AOSKit on macOS 15.7:** does an unsigned Python/ObjC helper get anisette headers?
  - Log in, `listTeams`, `downloadTeamProvisioningProfile` for `dev.droidctl.r<hex>.runner.xctrunner`.
  - Does the profile include Xcode's existing cert, so no new cert is needed?
  - Time end-to-end.
- **M5, token lifetime:** record `duration`/`expiry` of the `com.apple.gs.xcode.auth` token at login. Then retry `listTeams` daily with the stored token until it fails. This decides whether silent renewal holds across a 7-day cycle.
- **M6, Linux anisette:** self-hosted `anisette-v3-server` + login from a Linux VM with a **secondary** Apple ID.
  - Count 2FA prompts across two logins on the same ADI state (expect 1).
- **M7, cert limit:** on the secondary free account, how many dev certs before 7460. Do it without touching the user's main team.
- **M8, renewal time:** full silent renewal (profile → sign → install → start) on the phone, vs today's 4.5 s Xcode re-sign + restart.
