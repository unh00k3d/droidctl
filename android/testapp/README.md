# droidctl test app (`dev.droidctl.testapp`)

The edge-case lab from `TESTAPP.md`: one scenario per screen, ground truth in logcat.

## Build and install
```bash
cd android
./gradlew :testapp:assembleDebug          # needs ANDROID_HOME or local.properties
adb install -r -t testapp/build/outputs/apk/debug/testapp-debug.apk
```
The debug build is the one we install. It stays **debuggable** so tests can `run-as` into its uid. It is signed with the agent's committed key (`../agent/droidctl-debug.keystore`), so `install -r` upgrades work from any machine.

Unlike the agent, the test app has dependencies: AndroidX appcompat, Material, RecyclerView, ViewPager2, SwipeRefreshLayout and Compose (BOM 2025.04.01, the last line that builds against compileSdk 35). The Compose compiler plugin is 2.2.10, matching the Kotlin Gradle plugin that AGP 9.4.1 bundles.

## Launch contract
```bash
adb shell am start -n dev.droidctl.testapp/.Main --es s <scenario> [--ez reset true] [--ei delay_ms 3000 ...]
adb shell am start -a android.intent.action.VIEW -d droidctl-test://s/<scenario>
adb shell am start -n dev.droidctl.testapp/.Main --es s __list__   # dumps the registry to logcat, then finishes
```
- **`reset=true` is a hard reset.** It starts a fresh activity in a fresh task (`NEW_TASK|CLEAR_TASK`) and clears the app's preferences, so no dialog, popup or view state from the previous scenario survives. Without it, the running activity (`singleTask`) swaps the scenario in place. For a cold process, add `-S` to `am start`.
- **Capture check:** the activity title, and so the accessibility window title, is `s:<scenario>`. Fixture capture should verify it before saving.
- With no `s`, the app shows an index of all scenarios.

## Ground truth: `DTA` logcat lines
```
DTA {"s":"toggle","ev":"click","id":"wifi","n":1,"state":true}
```
- **Fields:**
  - `s`: the scenario.
  - `ev`: the event.
  - `id`: the element, usually its resource-id name.
  - `n`: how many times this `(ev, id)` pair has happened since the scenario was shown.
  - Other fields are event-specific (`value`, `state`, `item`, `row`, …).
- **Events every scenario logs:** `shown` once its first frame is posted (`recreated` says whether it came back from a config change), and `reset` when launched with `reset=true`.
- **Where each scenario's events are listed:** `scenarios.json`.
- Read them with: `adb logcat -d -v brief DTA:I '*:S'`. To take only lines after a point in time, use `-T 'MM-DD hh:mm:ss.mmm'`, and quote the device-side `date` format as a single shell argument.

## Registry and `scenarios.json`
- **The single source is `ScenarioSpecs.kt`** (`SPECS`): pure Kotlin metadata (name, group, toolkit, desc, events).
- **Builders:** `Scenarios.kt` maps each name to a screen builder. Its `init` fails if the two sets differ.
- **`scenarios.json` is generated from `SPECS`,** and a JVM unit test fails if it is stale. Regenerate it with:
  ```bash
  ./gradlew :testapp:testDebugUnitTest -Pdroidctl.updateScenarios=true
  ```
- **The e2e coverage gate** (TESTAPP.md) should read `scenarios.json`.
- **View ids:** views get real resource ids (so the tree reports `viewIdResourceName`) via `Sc.rid(name)`. After adding ids, run `python3 android/testapp/gen_ids.py`: it regenerates `res/values/ids.xml` from the string literals in the code, and `rid` fails loudly for a missing one. Compose screens use `testTag` with `testTagsAsResourceId`.

## Notes and known behaviour
- **Group 7** is device-level and has no screens. Group 0 holds one utility, `peer_uid_probe`: it connects to `@droidctl` as the app's uid and logs the reply. On the SM-N950F (API 28, SELinux enforcing), `connect()` itself fails with `EACCES`, so another app never reaches the agent's peer-UID check.
- **Compose variants:** `buttons`, `toggle`, `counter`, `row_nested`, `custom_actions`, `slider`, `form`, `list` (`list_compose`), `duplicates`, `cart`, `calendar`. The other scenarios are Views only.
- **`huge_tree` (~5,000 views) takes about 10 s to show** on the SM-N950F with the agent's service enabled. Wait for its `shown` event.
- **`rtl`:** sets the layout direction to RTL rather than switching the device locale, which would need a system setting.
- **`sensitive`:** only marks views as data-sensitive on API 34+. On older devices it says so on screen.
- **`permission`:** once CAMERA is granted, reset it with `adb shell pm revoke dev.droidctl.testapp android.permission.CAMERA`.
- **`ui_hang` and `crash`:** they do what they say when tapped. Relaunch with `reset=true` afterwards.
