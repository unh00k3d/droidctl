# iOS support: handoff (state at 2026-10-22)

You are picking up droidctl's iOS workstream. **droidctl drives a real iPhone today** through the same CLI, daemon and MCP server as Android. The milestones are I1–I4, plus a real-app, MCP and easier-setup round. What remains:
- one experiment that can't run before 2026-10-27;
- a dedicated iOS test app (waiting on that experiment);
- verifying the easier-setup paths on a fresh Mac or iPhone;
- a backlog of gaps (§7);
- optionally, I5 (simulator).

## 1. Read in this order
1. **This file:** state, rules, how to run things, what's next, hazards.
2. **`PLAN.md` → "iOS milestones":** what each milestone built, with measured numbers, findings and what was left undone.
3. **`IOS-INTEGRATION.md`:** the plan the code followed. Its top status block lists where the build differs from the plan.
4. **`IOS.md`:** the spec and the measured behaviour of iOS. §11 actions, §12 coordinates, §13 settle, events, waits, web views and the phone's log, §14 the performance budget.
5. **`research/ios-decision.md`:** the evidence, chronologically. **`research/ios-spike/`:** the spike code, still useful for re-measuring.
6. **The repo's rules:** `CLAUDE.md` and `PLAN.md`:
   - measure, don't assume;
   - fixtures come from real devices;
   - never break the user's phone;
   - never guess by position;
   - **commit only when asked.**

## 2. Status
**What works on the iPhone** (iOS 17+, free or paid Apple team; verified only on the setup in §4):
- **Setup and maintenance:** `devices`, `setup` (a guided checklist, §6), `doctor`, `teardown`.
- **Reading:** `snapshot`, `where`, `dump-fixture`.
- **Acting:**
  - `tap`, `long-press`, `type`, `set` (text), `focus`, `scroll`, `scroll-to`, `swipe`, `gesture`;
  - `back` (taps the screen's back button), `home`, `hide-keyboard`, `press enter`.
- **Waiting and watching:** `wait` (text, id, desc, window, pkg, gone, idle), `current`, `watch`.
- **Apps:** `launch` (`--stop` for a cold start), `stop-app`, `apps`, `install` (`.app`/`.ipa`), `open-url`.
- **Output:** `shot` (`--marks`, `--crop`), `mark`, `logs` (`--pkg`, `--level`, `--tag`, `--marks`).
- **MCP:** `droidctl mcp`, the same tools.
- **Real apps verified:** Safari; test and dev builds of third-party apps (pre-login, 2026-10-26: settings, splash, pop-ups, a form, the login form read-only); a second real app (every tab, details, search; logged in 2026-10-26, read-only). PLAN.md "A second real app" and "Moving quickly through three real apps".
- **`unsupported` on iOS, by design:** `recents`, `notifications`, `quick-settings`, `expand`/`collapse`/`dismiss`, `press` keys other than enter, `--toast`, `--activity`, `launch --clear`, `install --grant`, the clipboard.

**How it is built** (all in `droidctl/ios/`):

| file | what |
|---|---|
| `xcode/DroidctlRunner/DroidctlRunner.m` | the runner: one XCUITest in ObjC that serves `android/agent/PROTOCOL.md` (JSON-RPC 2.0) on 127.0.0.1:27100 on the phone. Private XCTest calls by selector, each guarded. Methods: ping, tree, act, gesture, type, hide_keyboard, launch, open_url, screenshot, mark, global home, events/subscribe, wait_idle, wait_change, current, hit (diagnostics), quit |
| `transport.py` | a usbmuxd client (no iproxy) |
| `devices.py` | `xcrun devicectl` wrappers (list, apps, processes, terminate, install, launch, lock state) |
| `runner.py` | build per team, detached `xcodebuild test-without-building` start, adopt, build-ID check, auto-renewal, typed start errors |
| `client.py` | `IosClient` (an `AgentClient` over usbmux). It maps trees, turns settle specs into the iOS rule, and runs `wait_for` on the mapped tree |
| `mapping.py` | the runner's tree → droidctl's raw tree (Android class names, flags, actions), so snapshot, resolve, act and MCP run unchanged |
| `setup.py` | the guided setup |
| `manage.py` | teardown and doctor |
| `syslog.py` | the phone's log over lockdown `syslog_relay` |

**The key policies** (spec `IOS.md`):
- **Acting:** an allowlisted AX action first. Standard UIKit controls refuse it; then **one** touch at the element's `VisiblePoint`, only after an enforced hit-test (else `occluded`).
- **Typing:** keys into the focused field first (delete, type, read back); `setAttribute` as the fallback (it bypassed a formatted field's model, 2026-10-26).
- **Settling:** run-loop idle + animations idle, re-armed on focus or keyboard-shown, a 200 ms floor after AX scrolls, 100 ms quiet, a 3 s cap.
- **The lock guard** fails closed (`screen-off`).

**Commits on `ios-research`** (historical; the branch was squash-merged into `main` as `2c4a288` on 2026-11-10 and deleted):
- `b39437f` I1, `7580086` I2, `284f253` I3, `cc51b6a` I4.
- **Uncommitted when this was written:** the 2026-10-22 round (real apps, MCP, easier setup; PLAN.md "Real apps, MCP and an easier setup"), the 2026-10-26 second-app round (PLAN.md "A second real app") and this file. Commit when the user asks.

**Tests:**
- **Unit:** `tests/test_ios_*.py` (about 150 tests; 1061 in the whole unit suite; `test_ios_lifecycle.py` covers setup's steps, the runner's start/connect, teardown, doctor and the syslog/Xcode sharing with fakes), on real runner trees (`tests/fixtures/ios-runner`), real devicectl JSON (`ios-devicectl`, scrubbed) and real host output (`ios-host`). Plus `tests/fakes.py` `FakeUsbmux`/`FakeRunner`.
- **On the iPhone:** `tests/e2e/ios/` has 38 tests: `test_i1`–`test_i4`, `test_real_apps` (Safari needs the internet; one test is an MCP session), `test_setup_fresh` and `test_testapp_forms` (5, skipped until the test app is installed). 33 pass, 5 skip (2026-10-26). The full set takes about 4 minutes.
- **Third-party apps have no committed tests or fixtures** (their trees stay out of the repo). The rules found on them (`IOS.md` §9.4c) are guarded by checks over every real fixture (no new back buttons, no change to Settings/Safari/test-app trees); positive fixtures need the same patterns in `ios/testapp` (§7.2).

## 3. User decisions and preferences (from the user, 2026-10-19 … 22)
- **iOS is wanted, real iPhone first.** The simulator is secondary; no decision may rest on simulator-only behaviour.
- **Branch:** the work stayed on `ios-research` until the user merged it into `main` (2026-11-10); it now continues on `main`. **Commit only when asked.**
- **Supported:** iOS 17+. Free and paid Apple teams; the free team is the documented path.
- **Integration decisions (2026-10-21):**
  - the runner speaks the Android agent protocol;
  - **no auth or token** (the runner listens on loopback on the phone; the accepted risk is in `IOS-INTEGRATION.md` §3);
  - mapping lives on the host;
  - the runner is in ObjC;
  - a **guided setup** that tells the user exactly where they are needed.
- **Setup must be "extremely easy"** (2026-10-22): droidctl does signing, installing, starting and renewing. The user only signs in to Xcode and makes the taps on the phone that it asks for (§6).
- **Android is considered done** by the user (their own Android 16 testing).
- **No iOS 26 phone:** iOS 26 coverage is the simulator only. Known; don't ask again.
- **The free-profile expiry experiment:** the two S1 spike apps stay on the iPhone until after **2026-10-27T10:28Z** (§7.1).
- **Third-party apps the user lets droidctl drive:**
  - **Read-only:** no transactions and no settings changes (leave debug/log/mock switches alone).
  - **Credentials** were given in chat once and are **never written down**; ask the user.
  - **Never commit its trees or screenshots.** They live in the git-ignored `research/ios-spike/qa/` or a scratchpad.
  - It logs out after a few minutes in the background; don't hammer the login.
- **Privacy:**
  - The iPhone's IDs and the Apple team ID stay out of the repo (they're in the user's Claude memory, `ios-support-go.md`).
  - Never commit Settings-root or Home-screen trees (Apple Account name, Wi-Fi SSID, the user's apps) or the full app list.
  - Fixtures are scrubbed (`tests/fixtures/ios-devicectl/capture.py`; Safari tab UUIDs zeroed).
- **One-off approvals don't carry over:** on 2026-10-22 the user allowed restarting CoreDeviceService once (§8). Ask again next time.

## 4. Environment
- **Host:** an Intel Mac, macOS 15.7.9.
- **Xcode 26.2 at `~/Downloads/Xcode.app`**, not in `/Applications`. droidctl finds it by itself (`devices.developer_dir()`). For your own `xcrun`/`xcodebuild`, export `DEVELOPER_DIR=/Users/enesb/Downloads/Xcode.app/Contents/Developer`.
- **The phone:** iPhone 12, iOS 18.7.8, 390×844 pt @3x, Developer Mode on, paired, free personal team.
  - It locks itself, so ask the user to unlock it when a start fails with `screen-off` ("Timed out while enabling automation mode" means a passcode prompt is waiting).
  - Two IDs: the hardware UDID (`00008101-…`, used by droidctl's `-d` and by xcodebuild) and the CoreDevice ID (a UUID, used by devicectl; droidctl looks it up itself). Both are in memory; `research/ios-spike/.cdid` (git-ignored) has the CoreDevice ID.
- **The user's own `iproxy 2222:22` and `iproxy 2244:44` run on this Mac. Never kill them.**
- **Python:**
  - The repo's `.venv` exists: run `.venv/bin/pytest`, `.venv/bin/droidctl`, `.venv/bin/python -m droidctl`.
  - **`python -m droidctl` always runs in-process;** only the `droidctl` console script goes through the daemon.
  - **The daemon keeps the code it started with:** after changing Python code, run `droidctl daemon stop`.
  - On macOS, run the unit suite with `--basetemp=/tmp/…`: pytest's default temp path is too long for the daemon's unix socket.
- **droidctl's state on this Mac:**
  - `~/.droidctl/state.json` (per device: team, port, build ID, holder pid);
  - `~/.droidctl/ios/build/<team>/` (the runner's build cache);
  - `~/.droidctl/ios/<udid>/session.log` (the runner's xcodebuild session log; look here first when a start fails);
  - the flag files `xcode-busy` and `syslog-reading` (§8).

## 5. Live state right now (2026-10-26)
- **Third-party apps** are logged out. Permission prompts answered on the user's behalf: microphone "Don't Allow", location "Allow Once" (asked again each launch), notifications "Allow" (the user's choices).
- **The runner was rebuilt** with the 2026-10-26 changes (same profile, `ebea9659…`, expires 2026-10-28T15:15Z).

**From 2026-10-22:**
- **Installed on the iPhone** (all 3 free-team slots):
  - droidctl's runner `dev.droidctl.rae309727.runner.xctrunner`;
  - the S1 spike apps `dev.droidctl.s1.testapp` and `dev.droidctl.s1.runner.xctrunner`. Their profiles expire 2026-10-27T10:28Z.
  - The droidctl runner's own profile expires 2026-10-28T15:15Z.
- **Nothing of ours is running:** the runner and the daemon were stopped. The next droidctl command starts them (about 6 s).
- **CoreDeviceService** was restarted on 2026-10-22 (with the user's OK) after it got stuck at 95% CPU; it was idle afterwards.
- **CoreDeviceService stuck again on 2026-10-26** (~100% CPU after a USB drop): every `devicectl` call timed out (`launch` after 60 s), taps/snapshots kept working; the app was opened from its Home-screen icon. Restarted with the user's OK: it ignored SIGTERM, so `kill -9` (launchd starts it again on demand); afterwards ~2% CPU and `devicectl device info apps` answered in 0.24 s.
- **Third-party apps** are logged out.

## 6. How to run things
```sh
droidctl setup                      # iPhone plugged in and unlocked: the guided checklist (no args needed with one phone)
droidctl doctor                     # read-only checks, incl. the profile's expiry and the runner
droidctl snapshot -d <UDID>         # -d is needed when an Android phone is attached too
droidctl daemon stop                # after code changes; also releases the phone's log stream (§8)
python -c "from droidctl.ios import runner; runner.stop('<UDID>')"   # stop the runner
.venv/bin/pytest -q --basetemp=/tmp/claude-502/pt tests --ignore=tests/e2e        # unit suite
DROIDCTL_IOS_UDID=<UDID> .venv/bin/pytest -q --basetemp=/tmp/claude-502/pt tests/e2e/ios   # on the iPhone
droidctl dump-fixture <name> -d <UDID> --dir tests/fixtures/ios-runner --pkg <bundle>     # a new real fixture
```
- **The guided setup** (`droidctl/ios/setup.py`, 7 numbered steps: xcode, attached, developer-mode, team, unlocked, build, start). Each step skips when it's already fine. When the user is needed it says what to do, helps once, and waits (non-interactive: a typed error with the instructions and the command to re-run). The help it gives:
  - **No Apple ID in Xcode:** it opens Xcode. The team then comes from Xcode's record of signed-in accounts (`defaults export com.apple.dt.Xcode`), with no certificate needed first. With several teams, an interactive pick.
  - **Not paired:** `devicectl manage pair` raises "Trust This Computer?".
  - **Developer Mode off:** AMFI "reveal" makes the switch visible.
  - **Untrusted profile:** it opens Settings > General > VPN & Device Management on the phone.
  - **Free-team 3-app limit:** it removes only earlier droidctl runners (`dev.droidctl.r<8 hex>…`), and otherwise names the apps.
- **Renewal:** `setup` and any command re-sign the runner when its profile ends within a day (`runner.renew_if_due`).
- **Re-measuring with the spike runner:** see `research/ios-spike/README.md`. It needs a free app slot, so it must wait until after the expiry experiment.

## 7. Open tasks, in order
### 7.0 Blocker found 2026-10-22: Xcode's command-line builds see no Apple ID
- `xcodebuild -allowProvisioningUpdates` fails with **"No Accounts"**, from Claude's shell and from the user's own Terminal alike, although Xcode > Settings > Accounts shows the Apple ID. Xcode's prefs agree: `DVTDeveloperAccountManagerAppleIDLists` is empty while `IDEProvisioningTeamByIdentifier` still holds the team.
- Consequences: no new profile can be issued, so neither the test app (§7.2) nor the runner's renewal (§7.1, due 2026-10-28T15:15Z) can be signed. The runner keeps working on its cached profile until then.
- droidctl now says so: `setup`/`doctor` show the team as "Xcode sees no Apple ID" with a warning (`setup.parse_xcode_teams` `signed_in`), block with reason `signed-out` when a profile is needed, and xcodebuild's "No Accounts" is classified `signing`/`signed-out`.
- **Since 2026-10-26** a signed-out team still builds while its profile is valid and not due: droidctl reads the profile from its own build or from Xcode's profile cache (`setup.cached_profile`), so rebuilding the runner after a source change, setup after `teardown` and setup from an empty droidctl home all work until 2026-10-28T15:15Z. After that, nothing signs until the account is fixed.
- **The fix is the user's:** in Xcode > Settings > Accounts, select the Apple ID; sign in again if asked, otherwise remove (−) and re-add (+ > Apple ID) it. Then re-run the profile test (move droidctl's cached `ebea9659….mobileprovision` aside, re-sign; the user OK'd this) and the test-app install.
- **The user decided (2026-10-22):** drop the expiry experiment and remove both S1 apps to make room for the test app. Not done yet: removing them is pointless until signing works.

### 7.1 Free-profile expiry (only after 2026-10-27T10:28Z, when the user asks)
1. `xcrun devicectl device process launch --device <CDID> dev.droidctl.s1.testapp`.
2. An `xcodebuild test-without-building` of the S1 runner.
3. Record both exact error texts in `research/ios-decision.md` §11 and match them in `runner.classify_output` (the `profile-expired` rule is a guess today).
4. **Check droidctl's own renewal.** Measured 2026-10-22: a forced re-sign 6 days before expiry got the *same* cached profile back from Xcode (UUID `ebea9659…`, 4.5 s). So renewal now re-signs once per profile in its last day and reports honestly (`runner.renewal_due`/`resign`/`renewed`); after expiry it re-signs on every start until Xcode issues a new profile, or fails `profile-expired`. **To check on or after 2026-10-28T15:15Z:** any droidctl command; `doctor`'s `profile` check should then show a new expiry (≈ +7 days). If Xcode still hands back the expired profile, the fix is to move droidctl's own cached profile (`~/Library/Developer/Xcode/UserData/Provisioning Profiles/<uuid>.mobileprovision`, name "iOS Team Provisioning Profile: dev.droidctl.r…runner.xctrunner") aside before re-signing; that needs the user's OK (the auto-mode classifier refused it as destructive, 2026-10-22).
5. **Uninstall both S1 apps** (`xcrun devicectl device uninstall app --device <CDID> <bundle>`). The droidctl runner stays, so the Trust stays.
6. Delete the memory note `ios-profile-expiry-test.md`.

### 7.2 The dedicated iOS test app: built 2026-10-22, waits for a slot (after 7.1)
- **Source:** `ios/testapp/` (`DroidctlTestApp.xcodeproj`, `TestApp/App.swift`). Tab "Main" is the S1 screen unchanged (same ids, so the I2 tests carry over); tab "Forms" has a number pad with a Done bar, one without, a secure field (logs the length only), a masked phone field, and a splash (1.2 s) → home transition. Builds for the simulator and the device; on the iOS 26.2 simulator it launches and writes `DTA app ready`.
- **Install (takes 1 of the 3 free-team slots):** `.venv/bin/python scripts/ios_testapp.py -d <UDID>`; bundle `<runner prefix>.testapp` (teardown never removes it).
- **Then run:** `DROIDCTL_IOS_TESTAPP=<that bundle> DROIDCTL_IOS_UDID=<UDID> .venv/bin/pytest tests/e2e/ios` — the I2/I3/I4 tests use it, and `test_testapp_forms.py` (5 tests, skipped with the S1 app) runs. Those 5 have never run: expect to adjust them to what droidctl really does (e.g. `hide-keyboard` on a number pad without Done).
- **Still to add:** landscape (needs the phone rotated by the user); the second real app's patterns, so they get real fixtures (`IOS.md` §9.4c): a cell of side-by-side cards labelled "A, B, C"; an unlabelled tab strip of 4 labels; a wrapper cell around buttons; a button with a zero-width title; a custom `arrowBack` bar button; a page sheet; an empty table under a button.

### 7.3 Verify the easier setup on a fresh Mac or iPhone (needs the user)
**Verified 2026-10-22 on this phone** (`tests/e2e/ios/test_setup_fresh.py`): the Developer Mode reveal (AMFI over lockdown TLS, accepted), setup from an empty droidctl home (build from scratch, sign, install over, start: 30 s), the interactive terminal mode (intro and ✓ [n/7] for each step, under a pty), an honest re-sign. The checklist's waiting/told-once/timeout logic and every step's branches are unit-tested (`tests/test_ios_lifecycle.py`).

**Still never run for real** (needs a phone or Mac that was never set up):
- the pair prompt raised by `devicectl manage pair`;
- opening Xcode when no Apple ID is signed in;
- Xcode creating the Apple Development certificate by itself under `-allowProvisioningUpdates`;
- the interactive team pick (needs two teams);
- the interactive *waiting* (a step that needs the user, e.g. locked phone, while a person watches).

### 7.4 Backlog (gaps found, not yet done)
- **Splash screens:** an action that leads to a splash settled during the splash (a real app; the welcome screen came ~1 s later). `wait --text` covers it; consider a host-side catch-up like Android's `_catch_up`.
- **Inline web text** comes as separate runs ("The", "iPhone", "is a line of", [link]…). That costs tokens; consider merging adjacent text runs within a paragraph.
- **Scrolls in Safari** take ~3.5 s, because its run loop only goes idle 2.5–2.7 s after a drag. The page really does change until then (lazy content), so the wait is real.
- **Unsupported for now:**
  - `logs` has no crash reports;
  - `set` on a slider (it steps 10% only);
  - Full Keyboard Access is untried, and trying it needs the user's OK for a Settings toggle.
- **Unverified:**
  - landscape (touch conversion and screenshot rotation are implemented, never run);
  - secure fields;
  - a paid team;
  - other iPhones, iOS versions and Xcode versions (`doctor` flags untested Xcode/iOS pairs: `manage.TESTED`);
  - whether the user's own Xcode is blocked for 60 s after a droidctl command (§8).
- **Merge / push of `ios-research`:** done 2026-11-10 (squash-merged into `main`, branch deleted).

### 7.5 I5 (optional)
The simulator as a target (the same runner, `simctl`, a prebuilt unsigned runner), then go-ios for Linux / no Xcode.

## 8. Hazards (each one bit us once)
- **Never send unknown AX action numbers.** 2025, 2042 and 2043 *terminate the app*. The allowlist (in the runner): 2003, 2004/2005, 2006/2007, 2010, 2014/2015, plus the two idle actions through their selectors.
- **"ok" doesn't mean it happened.** Many actions reply ok and do nothing; for example, AX page scrolls on a WKWebView (0/8 moved). Always settle and diff.
- **Enforce the hit-test before every touch.** Unenforced, a touch hit the number pad's "2" key instead of the login button.
- **The lock guard must fail closed.** AX presses reach apps behind the lock screen.
- **XCTest waiting APIs hang or kill the runner** (`launch`, `activate` while locked, `typeText` without focus). Use the daemon calls with completions.
- **Web elements belong to WebKit's WebContent process** (their pid isn't the app's). Idle requests sent to that pid never replied, and they emptied the web view's tree. Settle and `front` use the host app.
- **AX notifications arrive off the main thread.** The runner's event state is locked; keep it that way.
- **An open syslog stream keeps Xcode off the iPhone:** xcodebuild waited 60 s on "Device is busy (Connecting to iPhone)". After repeated failures like that, **CoreDeviceService sat at 95% CPU** and every start took 42–62 s, until it was restarted.
  - The ring reads only while droidctl is in use, and lets go around the runner's build and start (`syslog.xcode_busy`).
  - Never hold a log stream open across an xcodebuild.
  - If starts are slow, check CoreDeviceService's CPU (`top -pid $(pgrep -f CoreDeviceService.xpc)`) and **ask the user** before restarting it.
- **Replacing the runner app while it runs ends the session:** `install` refuses droidctl's runner.
- **Teardown and slot-freeing touch only `dev.droidctl.r<8 hex>.runner.xctrunner`**, never the S1 research apps or other apps.
- **Free team:** uninstalling every app of the team removes the Trust; profiles last 7 days; 3 apps per device.
- **Stale runner binary:** 1 start in 11 ran old code. The build-ID check is mandatory.
- **Don't poll snapshots while waiting for idle:** the daemon serialises, and the idle replies come late.
- **Screen comparisons:** compare mapped, on-screen elements only. Off-screen recycled cells shuffle their labels on every read.

## 9. Key numbers (iPhone 12, iOS 18.7.8; details in PLAN.md and `IOS.md` §14)
| what | measured |
|---|---|
| usbmux round trip | ~1–3 ms |
| snapshot: Settings / test app (1,060 nodes) | 87–150 ms / ~450 ms |
| runner start → first ping / adopt through the daemon | 5–6 s / 0.74 s |
| `setup` when all is in place / after `teardown` | 5.9 s / ~9 s |
| tap via AX / via the checked touch (settle; whole command through the daemon) | ~110 ms / 405–670 ms; ~1.1 / 1.4–1.7 s |
| type 5 characters (setAttribute) / hide-keyboard (settle) | ~700 ms / ~244 ms |
| AX page scroll (test app) / Safari drag (settle) | ~305 ms / ~2.6–2.8 s |
| `launch` / cold `--stop` / `open-url` (Safari) | 1.0–1.5 s / ~1.7 s / 1.1–1.3 s |
| `shot` / `--marks` | 110–150 / 130–180 ms |
| `apps` / `logs --pkg` / `teardown` | ~370 ms / ~950 ms / 26.6 s |
| correctness, 11 actions × 5 rounds judged by DTA | 55/55, each handler fired exactly once |
| 30-min soak (spike) | 0 errors, memory flat at 79–82 MB |
