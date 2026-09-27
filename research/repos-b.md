# droidctl research B: uiautomator2, android_world, Maestro, and five recent agent-focused Android tools

Date: 2026-09-27. Every repo was shallow-cloned into `/tmp/research/repos/`. Paths below are relative to each repo root. Star counts come from the GitHub API on the research date.

| Repo | License | Stars | Last commit | Role here |
|---|---|---|---|---|
| openatx/uiautomator2 | MIT | 8.4k | 2026-09-11 | the v1 backend (Python client) |
| openatx/android-uiautomator-server-jar | MIT | 20 | 2026-06-24 | **the on-device server u2 3.x actually runs** (`u2.jar`) |
| openatx/android-uiautomator-server | MIT | 356 | 2024-07-22 | legacy APK/instrumentation server. It still provides the AdbKeyboard IME APK. |
| google-research/android_world | Apache-2.0 | 934 | 2026-09-09 | benchmark and agents (T3A/M3A) |
| google-deepmind/android_env | Apache-2.0 | 1.2k | 2026-09-09 | a11y forwarder wrapper and protos |
| mobile-dev-inc/maestro | Apache-2.0 | 15.8k | 2026-09-25 | the settle and retry logic, plus the MCP hierarchy formatter |
| callstack/agent-device | MIT | 4.8k | 2026-09-27 | **closest prior art**: an agent CLI with @refs, diff and settle |
| appium/appium-mcp | Apache-2.0 | 481 | 2026-09-26 | Appium-based MCP |
| CursorTouch/Android-MCP | MIT | 871 | 2026-07-01 | an MCP built on u2 |
| web-infra-dev/midscene | MIT | 15.0k | 2026-09-24 | vision-first GUI agent (Android package) |
| takahirom/arbigent | Apache-2.0 | 646 | 2026-09-26 | AI test agent built on Maestro |

---

## 0. Findings that change PLAN.md

1. **The u2 server does NOT stay alive between CLI calls.**
   - u2 3.x no longer uses the APK/atx-agent. It pushes `u2.jar` and runs it with `app_process` as a streamed `adb shell` child (`uiautomator2/core.py:76-82`):
     ```python
     command = f"CLASSPATH=/data/local/tmp/u2.jar app_process / com.wetest.uia2.Main -p {port}"
     conn = dev.shell(command, stream=True)
     ```
   - It also registers `atexit.register(self.stop_uiautomator, wait=False)` (`core.py:231`), which closes that shell stream. The README says so plainly: "通常情况下Python程序退出了，UiAutomation就退出了" ("normally when the Python program exits, UiAutomation exits", `README_CN.md:1119`).
   - So a naive `droidctl tap` process pays a jar-md5 check, a server launch and a readiness poll every time: `_setup_jar` runs `toybox md5sum` (`core.py:257-276`), the launch is `app_process`, and `_wait_ready` polls `/ping` every 0.5 s (`core.py:282-306`). That is roughly 1–3 s per call.
   - u2 itself acknowledged this in 2026-06. It added `u2cli`, a local HTTP daemon on 127.0.0.1:17913 that keeps `u2.Device` objects alive (`uiautomator2/agent_cli/server.py`, `client.py:120-141` `ensure_server`).
   - **Consequence: droidctl needs one of two things from day 1.** Either (a) launch the server itself, *detached* (for example `adb shell 'CLASSPATH=/data/local/tmp/u2.jar setsid nohup app_process / com.wetest.uia2.Main -p 9008 >/dev/null 2>&1 &'`), so u2's `_check_alive()` finds it alive and never owns or kills it (`core.py:248-255`, where `stop_uiautomator` only kills `self._process`). Or (b) keep a small local daemon. Option (a) is cheaper, but verify on the device that it survives adb disconnects.
2. **The u2 XML has no scroll hints, no `editable`, no window type and no actions list.** The attributes are exactly `index text resource-id class package content-desc checkable checked clickable enabled focusable focused scrollable long-clickable password selected visible-to-user bounds drawing-order hint display-id` (`android-uiautomator-server-jar/app/src/main/java/com/wetest/uia2/stub/AccessibilityNodeInfoDumper.java:106-152`). The plan's "(N items, more ↓/↑) from the scroll hints" therefore has no data source in v1. Approximate it from geometry: the first or last child touches the container edge, or the container is a RecyclerView/ListView whose children span it. Or defer it to the v2 APK.
3. **Invisible children are dropped on the device.** `dumpNodeRec` only recurses into `child.isVisibleToUser()` (`AccessibilityNodeInfoDumper.java:156-164`). This matters in two ways:
   - Off-screen list items are never in the dump, so there are no "off-screen" summaries like agent-device's.
   - WebView content is often reported as invisible and gets lost. Maestro patched exactly this: `if (child.isVisibleToUser || insideWebView)` (`maestro-android/.../ViewHierarchy.kt:148-156`).
4. **Window order is non-deterministic.** Roots are collected into a `HashSet<AccessibilityNodeInfo>` (`AccessibilityNodeInfoDumper.java:48-72`). The dump contains the active root plus *all* windows on all displays (status bar, nav bar, IME, system overlays) unless `root_in_active=True`. Fingerprints and diffs must not depend on the order of top-level windows. Sort the roots by package/bounds, or key the diff by fingerprint, never by position.
5. **ACTION_CLICK is not reachable through u2 JSON-RPC.**
   - `AutomatorServiceImpl` never calls `performAction`.
   - `UiObject2.click()` computes the visible center and injects a gesture (`uiautomator2/src/main/java/androidx/test/uiautomator/UiObject2.java:519-527`).
   - Python `UiObject.click()` doesn't even use the server-side selector click. It makes three round trips: `waitForExists`, then `objInfo` for the visibleBounds, then `click(x, y)` (`uiautomator2/_selector.py:138-155`), and the server's `click(x, y)` is touchDown, a 100 ms sleep, then touchUp (`AutomatorServiceImpl.java:183-192`).
   - The only a11y-action path is `setText`, which uses `ACTION_SET_TEXT` (`UiObject2.java:963-979`).
   - A cheap "v1.5" route to ACTION_CLICK is a fork of the MIT `android-uiautomator-server-jar` with one extra RPC, `performAction(selector, actionId, args)`. That is much smaller than a full accessibility-service APK. The server already holds a shell-uid UiAutomation that can act on any node.

---

## 1. openatx/uiautomator2 and its on-device server

### 1.1 How UI state is obtained

- Python `d.dump_hierarchy(compressed, pretty, max_depth=50, root_in_active=None)` (`uiautomator2/__init__.py:104-147`) makes one JSON-RPC call, `dumpWindowHierarchy`. It retries 3 times on an empty dump (`@retry(HierarchyEmptyError, tries=3, delay=1)`, `__init__.py:132`). Note the empty-dump retry sleeps 1 s.
- Server side (`AutomatorServiceImpl.java:276-296`):
  ```java
  InstrumentShellWrapper.getInstance().setCompressedLayoutHierarchy(compressed);
  AccessibilityNodeInfoDumper.dumpWindowHierarchy(device, os, maxDepth, rootInActive);
  ```
- **What "compressed" does** (`uicore/src/main/java/uiautomator/InstrumentShellWrapper.java:72-87`): it clears `FLAG_INCLUDE_NOT_IMPORTANT_VIEWS` and calls `mUiAutomation.setServiceInfo(info)`. It drops views marked not-important-for-accessibility (most plain layouts). This is what we want.
  - A side effect: `setServiceInfo` on every dump also flushes the per-connection a11y node cache. agent-device calls this out as the pre-API-34 cache reset (see §5). So u2 gets cache freshness "for free".
  - Toggling `compressed` between calls flips the service flags and costs a re-config. Pick one mode and stick to it.
  - The README comment "compressed=True: include non-important nodes" (`README.md:485`) is **wrong**. The code does the opposite.
- **Idle waits are already off on the server.** The constructor sets (`AutomatorServiceImpl.java:106-112`):
  ```java
  configurator.setWaitForSelectorTimeout(0L); // Default 10000
  configurator.setWaitForIdleTimeout(0L);     // Default 10000
  configurator.setActionAcknowledgmentTimeout(500); // Default 3000
  configurator.setScrollAcknowledgmentTimeout(200);
  configurator.setKeyInjectionDelay(0);
  ```
  - `getWindowRoots()` still calls `device.waitForIdle()`, which becomes `uiAutomation.waitForIdle(QUIET, 0)` (`QueryController.java:522-536`), so it is effectively a no-op.
  - You can still override the timeouts per session with the `setConfigurator(ConfiguratorInfo)` RPC (`AutomatorServiceImpl.java:1653`).
- **Client-side delays.** `settings` defaults to `operation_delay=(0,0)` and `wait_timeout=20.0` (`settings.py:15-22`). **Pitfall:** every `UiObject` op starts with `must_wait(timeout=None)`, which falls back to `wait_timeout` of **20 s**. A missing element blocks for 20 s unless droidctl always passes `timeout=` explicitly or sets `d.settings['wait_timeout']`.

### 1.2 Latency

- **Transport.** Each JSON-RPC call opens a *new* TCP connection through the adb server (`AdbHTTPConnection.connect` uses `device.create_connection(TCP, port)`, `core.py:97-113`). There is no keep-alive, and gzip is disabled on purpose because of a nanohttpd leak (`core.py:133-141`). Budget roughly one adb round trip plus the HTTP time per call.
- **Import cost** of `uiautomator2` 3.7.0, measured locally: about **150 ms**, mostly `adbutils` and `requests` (from `python -X importtime`). That is acceptable, but a direct `http.client` to an `adb forward`ed port avoids both.
- **`u2.connect()` cost** (`base.py:24-40`, `core.py:219-256`): `adb devices`, then `md5sum` of the jar via shell, then `/ping`, and a launch if the server is dead. See finding 0.1.
- **Selector click is 3 RPCs.** The server exposes a 1-RPC `click(Selector)` that uses `UiObject2.click()` (`AutomatorServiceImpl.java:687-694`). Call it directly as `d.jsonrpc.click(selector_dict)`.
- **`objInfoOfAllInstances(selector)`** returns every match with bounds in one call and clears the a11y cache on API 34+ (`AutomatorServiceImpl.java:896-916`). This is useful for "multiple matches → pick nearest by bounds".
- **u2 shipped its own `u2cli` agent text format** (`agent_cli/server.py:23-111`). It collapses nodes with a single child and no content, and prints `class "text" desc=".." #id [l,t,r,b] click scroll checked focused selected disabled`, indented. The SKILL tells agents to `dump-hierarchy | grep -i keyword`. It has no refs, no merging and no pruning of non-actionable nodes. **droidctl is strictly more compact.** Cite this as the baseline in the benchmark.

### 1.3 Element → action

- `UiObject.click()` (`_selector.py:138-155`): `must_wait`, then `center()` from `objInfo.visibleBounds`, then `session.click(x, y)`. It taps coordinates, but they come from the **live visible bounds at action time**, which is what the plan wants.
- The server's `click(x, y)` is `touchController.touchDown`, `SystemClock.sleep(100)`, then `touchUp` (`AutomatorServiceImpl.java:183-192`). **Every tap costs at least 100 ms on the device.**
- XPath (`xpath.py:236-240, 514-525, 685-694`): the client dumps the full hierarchy, runs lxml xpath, and clicks the element's center. It polls `exists` every 0.2 s (`xpath.py:476-485`). That is a full dump per poll, so it's slow.
- There is no `performAction` anywhere. See finding 0.5.

### 1.4 Text input

- `UiObject.set_text(text)` goes to server `setText(selector, text)`, which runs `obj.toUiObject2().click(); obj.toUiObject2().setText(text);` (`AutomatorServiceImpl.java:669-678`). `UiObject2.setText` does `performAction(ACTION_SET_TEXT, {ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE: text})` on API 21+ (`UiObject2.java:963-979`).
  - Unicode-safe, no IME, replaces the whole content.
  - **But it taps the field center first.** If the field is partly covered (by the keyboard or a bottom bar), that tap can hit something else.
  - `set_text("")` calls `clearTextField` (`_selector.py:348-353`).
  - Pitfall: some apps (Compose/React Native controlled inputs, masked fields) ignore ACTION_SET_TEXT or re-render the old value. Read the text back.
- `d.send_keys(text, clear=False)` (`__init__.py:890-906`): if the AdbKeyboard IME is installed, it broadcasts `ADB_KEYBOARD_INPUT_TEXT` with base64 (`_input.py:107-121`). Otherwise it tries clipboard plus `pasteClipboard` (`__init__.py:431-444`), and on failure installs the IME.
  - `set_input_ime()` **switches the device's default IME and never restores it** (`_input.py:49-64`, `settings put secure default_input_method`). On a user's physical phone this silently replaces their keyboard.
  - Maestro and agent-device both record and restore the previous IME. droidctl must do the same, or avoid the IME path.

### 1.5 Stability and verification

- There is none beyond `waitForExists` and `waitUntilGone`, which the server runs with `device.wait(Until.hasObject(By...), timeout)` (`AutomatorServiceImpl.java:1039-1066`). Use these server-side waits for `droidctl wait` (one RPC with `http_timeout = timeout + 10`, `_selector.py:295-326`).
- Toast capture: an a11y event listener stores the last toast text (`AutomatorServiceImpl.java:119-127`), exposed as `d.last_toast` (`__init__.py:392-396`). **Steal:** report toasts in the post-action result ("toast: Saved").

**Verdicts (u2)**

| Technique | Verdict |
|---|---|
| `dump_hierarchy(compressed=True)` as the snapshot source, one mode only | **steal** |
| Server idle/selector timeouts already 0 | **steal** (verify with `getConfigurator` in `init`) |
| Client `wait_timeout` of 20 s | **must override** (pass `timeout=` on every op) |
| Python `UiObject.click` (3 RPCs) | **adapt**: call `jsonrpc.click(selector)` directly (1 RPC) |
| `objInfoOfAllInstances` for ambiguity resolution | **steal** |
| `set_text` via ACTION_SET_TEXT | **steal** with a read-back check. Note the pre-click. |
| AdbKeyboard IME via `send_keys` | **adapt**: only on explicit fallback, and always restore the IME |
| `last_toast` | **steal** into the action result |
| Per-call `u2.connect()` in a short-lived CLI | **skip**: detach the server yourself or run a daemon |
| xpath engine | **skip** for actions. It does a full dump per poll. |
| Fork the jar and add a `performAction` RPC (ACTION_CLICK/SCROLL_FORWARD) | **adapt**: v1.5 option before a full APK |

---

## 2. google-research/android_world (+ android_env)

### 2.1 How UI state is obtained

Two methods (`android_world/env/android_world_controller.py:127-137`):

1. **`A11Y_FORWARDER_APP`** (the default). An accessibility-service APK (`com.google.androidenv.accessibilityforwarder`, downloaded as a prebuilt from GCS: `android_env/wrappers/a11y_grpc_wrapper.py:39-44`).
   - It is enabled via `settings put secure enabled_accessibility_services …` (`a11y_grpc_wrapper.py:172-196`).
   - The device *pushes* the forest over gRPC to a server on the host at `10.0.2.2` (the emulator host alias, `:79-93`, `:279-330`). That is emulator-centric; physical devices would need `adb reverse`.
   - The observation is `accumulate_new_extras()['accessibility_tree'][-1]`, retried 5 times with a 1 s sleep (`android_world_controller.py:58-102`). It is also airplane-mode sensitive: gRPC dies without networking, and the wrapper toggles networking back on.
2. **`UIAUTOMATOR`**: `adb shell uiautomator dump`, then `xml_dump_to_ui_elements` (`representation_utils.py:178-219`).

The forest proto is a good **schema template for our v2 APK** (`android_env/proto/a11y/*.proto`):
- Windows carry `window_type` (APPLICATION / INPUT_METHOD / SYSTEM / ACCESSIBILITY_OVERLAY…), `layer`, `title`, `is_active` and `is_focused`.
- Nodes carry `unique_id`, `is_editable`, `actions[]`, `clickable_spans`, `labeled_by_id`/`label_for_id`, `drawing_order`, `hint_text`, `tooltip_text`, `text_selection_start/end` and `depth`.

The XML lacks every one of these.

### 2.2 Format given to the LLM

- **Pruning** (`representation_utils.py:136-163`): keep a node if `not node.child_ids or node.content_description or node.is_scrollable`, meaning leaves, plus anything with a desc, plus scrollables. It is flat, has no hierarchy, and does **not** merge row text into the clickable parent (the text lands on the leaf TextView, not on the clickable row).
- **Validation filter** (`agents/m3a_utils.py:529-560`): drop `!is_visible` and invalid or off-screen boxes (`x_min>=x_max`, `x_min>=screen_w`, `x_max<=0`, and the same for y).
- **M3A/T3A line format** (`agents/m3a.py:204-247`), with a verbose pseudo-JSON dict per element:
  ```
  UI element 7: {"index": 7, "text": "Sign in", "is_clickable": True, "is_long_clickable": False, "is_editable": False, "is_selected": False, "is_checked": False}
  ```
  The index is `enumerate()` *before* the validation filter, so indices are sparse (`m3a.py:250-264`). Roughly 35–50 tokens per element. droidctl's `[7] button "Sign in" #login` is 3–5× denser.
- M3A also draws Set-of-Mark boxes with the index on the screenshot (`m3a_utils.py:150`), and the prompt insists the index must be "VISIBLE in the screenshot and also in the UI element list" (`m3a.py:~108-112`).

### 2.3 Element → action

- `execute_adb_action` (`env/actuation.py:28-72`) runs `element = screen_elements[idx]`, then `bbox_pixels.center`, then `adb shell input tap x y`.
- **Pitfall confirmed in code:** `AsyncAndroidEnv.execute_action` re-fetches state *at action time* and indexes the **new** list with the old index (`env/interface.py:299-310`):
  ```python
  state = self.get_state(wait_to_stabilize=False)
  actuation.execute_adb_action(action, state.ui_elements, ...)
  ```
  If the UI changed between observation and action, index 7 now points to a different element and the tap silently hits the wrong thing. This is the positional-ref failure that droidctl's fingerprint re-resolution exists to prevent.

### 2.4 Text input

- `input_text` (`actuation.py:74-110`): tap the element, `sleep(1.0)`, optionally clear with `input keycombination 113 29` (Ctrl+A) then `keyevent 67`, `sleep(1.0)`, then `type_text`, then **always press Enter**.
- `type_text` (`env/adb_utils.py:511-545`) types **word by word** through `adb input text`, because "long text strings can be typed out of order at the character level" and time out. It is ASCII-only.

### 2.5 Stability

`_get_stable_state` (`env/interface.py:243-290`): poll UI elements until **3 consecutive equal** `ui_elements` lists, sleeping at least **0.5 s** between checks, with a **6 s** timeout. Worst-case stable is 1.0 s or more. Equality is by dataclass, which includes bboxes, so a blinking cursor or clock can prevent stability. Transitions also have fixed sleeps (`time.sleep(1.0)` around input).

### 2.6 Verification

None per action. Task-level success comes from `is_successful` evaluators (SQLite/app state), not from UI deltas.

**Verdicts (android_world)**

| Technique | Verdict |
|---|---|
| Forest proto schema (window_type, actions, editable, labeled_by, unique_id) | **steal** as the v2 APK wire schema |
| Device pushes to host over gRPC at 10.0.2.2 | **skip**: emulator-only. Use a device-side server plus `adb forward`. |
| Keep leaves, desc and scrollables, flat | **skip**: no row merging, and too many tokens |
| Pseudo-JSON per element | **skip**: 3–5× bigger than the line format |
| Invalid/off-screen bbox filter | **steal** (same as plan step 1) |
| Positional index resolved against a fresh list | **anti-pattern**: droidctl's fingerprint plus stale-ref error fixes it |
| Word-by-word `adb input text` | **skip** (ASCII-only). ACTION_SET_TEXT first. |
| 3-consecutive-equal stability loop | **adapt**: compare a *pruned* fingerprint hash, not raw elements, with a shorter interval |
| Always pressing Enter after typing | **skip**. Make it opt-in (`--enter`). |

---

## 3. mobile-dev-inc/maestro

### 3.1 How UI state is obtained

- **Device side.** An instrumentation test, `MaestroDriverService.grpcServer()`, runs a Netty **gRPC** server on port 7001 inside `am instrument` (`maestro-android/src/androidTest/java/dev/mobile/maestro/MaestroDriverService.kt:86-111`). The host talks over `adb forward` with a persistent HTTP/2 channel and keepalive settings. The RPCs are `viewHierarchy`, `screenshot`, `tap`, `inputText`, `eraseAllText`, `isWindowUpdating`, `launchApp`, `deviceInfo` and a few more (`maestro-proto/src/main/proto/maestro_android.proto`).
- **Configurator:** `setActionAcknowledgmentTimeout(0)`, `setWaitForIdleTimeout(0)`, `setWaitForSelectorTimeout(0)` (`MaestroDriverService.kt:87-90`).
- **Cache refresh before every dump** (`MaestroDriverService.kt:237-249`):
  ```kotlin
  private fun refreshAccessibilityCache() {
      uiDevice.waitForIdle(500)
      uiAutomation.serviceInfo = null
  }
  ```
  This works around stale `AccessibilityInteractionClient` cache copies.
- **Dumper** (`ViewHierarchy.kt`), a modified AccessibilityNodeInfoDumper. It uses reflection to call `UiDevice.getWindowRoots` for all windows. On top of the stock dumper it:
  - adds `hintText`, `important-for-accessibility` and `error` (the field error text);
  - **blanks `text` when `isShowingHintText`** (`AccessibilityNodeInfoExt.kt`: `getTextOrFallback`);
  - **descends into invisible children under a WebView** (`ViewHierarchy.kt:148-156`);
  - injects the current **toast** as a synthetic node (`ViewHierarchy.kt:66-86`).
- **Host side** (`maestro-client/.../drivers/AndroidDriver.kt:324-362`):
  - It parses the XML into a `TreeNode`, maps `content-desc` to `accessibilityText`, and optionally augments WebView content through Chrome DevTools (`androidWebViewHierarchyClient.augmentHierarchy`).
  - It can exclude keyboard nodes by resource-id prefix `com.google.android.inputmethod.latin:id/` (`AndroidDriver.kt:344-362`).
  - Keyboard visibility is detected by searching the hierarchy JSON for that prefix (`:374-386`). That is Gboard-only, so it is brittle.

### 3.2 Format given to the LLM (Maestro MCP `inspect_screen`)

`maestro-cli/src/main/java/maestro/cli/mcp/tools/ViewHierarchyFormatters.kt` and `InspectScreenTool.kt`:

- **Compact JSON with a one-time schema.** A `ui_schema` block holds the abbreviations and default values: `b` = bounds, `txt`, `rid`, `a11y`, `hint`, `cls`, `scroll`, `c` = children. Defaults are `enabled:true`, `clickable:false` and so on. After that, each element emits only non-default keys (`:222-249`, `:338-373`).
- **Pruning** (`compactTreeData`, `:282-305`):
  - Zero-size nodes are skipped (their children are promoted).
  - Nodes with only default values are skipped (their children are promoted).
  - Everything else is kept, including plain text nodes.
  - There is no merging, and bounds are always included.
- A compact CSV variant also exists (`:95-154`):
  ```
  id,depth,bounds,text,resource_id,accessibility,hint,class,value,scrollable,clickable,enabled,focused,selected,checked,parent_id
  3,1,"[330,768][386,824]",,"fabAddIcon","Increment",,,,,1,,,,,0
  ```
- The tool description embeds guidance for the agent: "Always copy `txt` values verbatim from this output; never author them from a screenshot … Maestro's `text:` matcher is full-string regex with IGNORE_CASE" (`InspectScreenTool.kt`). **Steal the idea**: put anti-hallucination guidance in AGENTS.md/SKILL ("copy labels verbatim; prefer refs").

### 3.3 Element resolution

- The selector becomes basic filters (text regex over **text ∪ hintText ∪ accessibilityText ∪ error**, `Filters.kt:58-108`; id regex; state flags).
- Then `deepestMatchingElement` (`Filters.kt:284-297`) prefers the innermost matching node, typically the TextView, not the row.
- Then `clickableFirst()` sorts clickable matches first unless `index:` is given (`Orchestra.kt:1700-1725`).
- It taps the **center of the deepest match's bounds**. It relies on touch dispatch to reach the clickable ancestor, which works for coordinate taps but would **not** work for ACTION_CLICK (droidctl v2 must walk up to the clickable ancestor, as the plan says).
- The lookup is retried until found: `lookupTimeoutMs = 17000`, optional 7000 (`Orchestra.kt:137-138`).
- **Re-resolution at tap time.** `ViewHierarchy.refreshElement(node)` (`maestro-client/.../ViewHierarchy.kt:52-63`) finds the node in the *new* hierarchy whose attributes equal the old ones *except bounds*, and only if there is **exactly one** match. Otherwise it keeps the old bounds. This is a fingerprint re-resolve, almost identical to droidctl's plan, and a good validation of the approach.
- **Visibility check.** `isVisible(node)` hit-tests the element's center against the tree (`getElementAt`, children in reverse = topmost first). It is visible only if the topmost node at that point is the element itself (`ViewHierarchy.kt:40-50`). **Steal** this as a cheap occlusion check before tapping.

### 3.4 Tap, settle and retry-until-changed (`maestro-client/src/main/java/maestro/Maestro.kt`)

**`tap()`** (`:216-274`):
1. `waitForAppToSettle(initialHierarchy)`.
2. Re-resolve the element via `refreshElement`.
3. If a scroll just happened and the settle was inconclusive, call `refreshElementUntilStable`: poll until the element's **bounds are identical in two consecutive fetches** (the scroll-momentum fix MA-4124, `:285-331`).
4. `performTap`.
5. If `waitUntilVisible` is set and the hierarchy did not change and the element is not visible, wait up to 10×1 s for it and tap again.

**`hierarchyBasedTap`** (`:398-436`), the retry-if-no-change logic:
```kotlin
val retries = if (retryIfNoChange) 2 else 1
repeat(retries) {
    driver.tap(Point(x, y))
    val hierarchyAfterTap = waitForAppToSettle(waitToSettleTimeoutMs = waitToSettleTimeoutMs)
    if (hierarchyAfterTap == null || hierarchyBeforeTap != hierarchyAfterTap) return  // changed → done
}
```
- `screenshotBasedTap` (`:438-505`) adds a screenshot diff: `ImageComparison(...).differencePercent > 0.005` (0.5%, `SCREENSHOT_DIFF_THRESHOLD`, `Maestro.kt:783`) counts as changed.

**Settle** (`maestro-client/.../utils/ScreenshotUtils.kt:38-74`): fetch the hierarchy repeatedly, **up to 10 times with a 200 ms sleep**, and return as soon as **two consecutive hierarchies are equal**. The worst case is about 2 s plus the fetch time. The Android variant (`AndroidDriver.kt:727-759`) first asks the device `isWindowUpdating(appId)`, which is `uiDevice.waitForWindowUpdate(appId, 500)` (`MaestroDriverService.kt:353-365`), inside a `WINDOW_UPDATE_TIMEOUT_MS = 750` loop. It only runs the hierarchy-equality loop while the window is updating. `waitUntilScreenIsStatic` compares two screenshots at a 0.5% threshold.

### 3.5 Text input

- For ASCII, the gRPC `inputText` sends **one keycode per character** with `Thread.sleep(75)` between them (`MaestroDriverService.kt:314-330`, `setText` mapping at `:516-570`). That is slow: about 75 ms per character, and it goes through the real IME.
- For non-ASCII, `inputUnicodeText` switches to Maestro's own IME, broadcasts base64url chunks (split at surrogate-pair-safe boundaries), then **restores the original IME in `finally`** (`AndroidDriver.kt:1365-1413`).
- Erasing is N× `pressDelete` (`MaestroDriverService.kt:295-312`).
- Long press is `adb shell input swipe x y x y 3000` (`AndroidDriver.kt:278-282`), a 3 s hold.

**Verdicts (Maestro)**

| Technique | Verdict |
|---|---|
| Persistent device server plus a persistent host channel | **steal** (the u2 server detached, plus a direct HTTP client) |
| `serviceInfo = null` / `setServiceInfo` cache flush before each dump | **steal**. u2 already does it implicitly when it sets the compressed flag. Keep that call in the path. |
| `refreshElement`: exact attribute match minus bounds, unique only | **steal** as step 1 of ref re-resolution |
| `refreshElementUntilStable`: tap only once bounds hold across two fetches after a scroll | **steal** for tap-after-swipe |
| `isVisible` hit test (topmost node at center == target) | **steal** as an occlusion guard, returning `error.kind: occluded` |
| Retry-if-no-change (tap again once if the hierarchy is unchanged) | **adapt**: *report* `changed:false` by default. Retry only with `--retry`, because blind retries double-toggle switches and double-submit forms. |
| Settle = two equal consecutive hierarchies, 200 ms × 10 | **steal**, but compare the pruned-snapshot hash |
| `waitForWindowUpdate(pkg, 500)` pre-check | **adapt** (needs a server RPC; u2 has no equivalent). Skip in v1. |
| Screenshot diff at 0.5% as a fallback change signal | **adapt**: only for canvas/game/WebView screens where the tree doesn't change |
| Text matched over text ∪ hint ∪ desc ∪ error | **steal** for `--text` / `--find` |
| Blank text when `isShowingHintText` | **steal** (v2). In v1 detect `text == hint` and mark the field `empty`. |
| WebView invisible-children workaround | **steal** (needs a server change, so it's another reason for the jar fork) |
| Toast as a synthetic node | **adapt**: report the toast in the action result |
| Keycode-per-char input at 75 ms | **skip** |
| Unicode IME with restore-in-finally | **steal** the restore discipline |
| Compact JSON with `ui_schema` defaults | **adapt** for `--json`: omit default flags. Don't abbreviate keys (agents mis-copy them; Maestro had to warn in the tool description). |

---

## 4. Additional repos (2025–2026, agent-focused)

### 4.1 callstack/agent-device (MIT, 4.8k stars, commits daily): the closest prior art

This is a TypeScript CLI plus daemon plus MCP for iOS/Android/others. Its model is the same as droidctl's: an `@e` ref snapshot, diff, and settle.

**UI state on Android.** It uses its **own instrumentation APK** (`android/snapshot-helper/`) instead of `uiautomator dump`:
- **Persistent session mode.** `am instrument -e sessionPort <p>` keeps the instrumentation alive and serves `snapshot|viewport|gesture|quit` over a device-local TCP line protocol, "avoid[ing] UiAutomation connect/teardown cost per call" (`android/snapshot-helper/README.md` §Persistent Session).
- **Bounded idle wait.** `automation.waitForIdle(quietMs=min(100, timeout), timeout=500)` (`SnapshotInstrumentation.java:27-28, 339-349`). The comment notes that using the full timeout as the quiet window "made every stable snapshot pay a fixed 500 ms tax".
- **Cache clear before each traversal** (`AccessibilityTreeCapture.java:67-93`). API 34+ uses `automation.clearCache()`; older versions use `automation.setServiceInfo(automation.getServiceInfo())`. The reason given is that Compose Navigation 3 swaps content inside one AndroidComposeView and the cache goes stale.
- **`FLAG_RETRIEVE_INTERACTIVE_WINDOWS`** enables `getWindows()` for keyboards and overlays (`:95-113`).
- Caps: `maxDepth=128`, `maxNodes=5000`, `timeoutMs=8000`. Output is uiautomator-compatible XML, chunked as base64 in instrumentation status.
- A **headless test IME** (`android/ime-helper/`): `onEvaluateInputViewShown()` returns false, so the keyboard adds **zero nodes to the tree** and text arrives as base64 broadcasts. The README warns to restore the previous IME ("critical — do not skip on a real device").

**Format.** A text tree with refs (`website/docs/docs/snapshots.md`):
```
Snapshot: 9 visible nodes (14 total)
@e1 [application] "Contacts"
  @e4 [other] "Lists"
    @e6 [button] "Lists"
    @e8 [other] "John Doe"
[off-screen below] 2 interactive items: "All Contacts", "New List"
```
Legend (`src/commands/schema/cli-help.ts:151`): `@e12 [button] label="Add to cart" enabled hittable -> press @e12`.

Distinctive output rules:
- **Off-screen summaries** carry up to 3 labels per direction (`packages/capture-kit/src/mobile-snapshot-semantics.ts:265-292`).
- An **unchanged repeated snapshot returns a compact acknowledgement** instead of the tree (`--force-full` overrides).
- `-s <label|@ref>` scopes the snapshot.

**Pruning** (`packages/platform-android/src/ui-hierarchy-inclusion.ts:13-116`):
- Drop `visibleToUser=false` and non-positive rects.
- Keep touch/focus targets and scrollables that have hittable descendants.
- Keep a text/id "proxy" node only if an ancestor or descendant is hittable or it's inside a collection. This is the Compose case, where the content-desc sits on a passive `android.view.View` inside the clickable container.
- Drop layouts/ViewGroup/View with no text.
- **Generic ids matching `^[\w.]+:id\/[\w.-]+$` don't count as meaningful labels** (`ui-hierarchy-node.ts:81-85`).

**Refs and lifetime** (`docs/adr/0014-session-ref-frame-lifetime.md`). This is a strict policy: **every mutating action expires all refs** ("ref frame"). The next mutation with a plain `@e2` is rejected (`ref_frame_expired`) unless the action's `--settle` diff re-issued refs. A generation suffix `@e12~s42` pins a ref to a frame. The motivating bug (#1239) was `snapshot → press @e1 → press @e2`: the second press was dispatched against pre-navigation coordinates. The ADR's words: "A false successful tap on the new screen is worse than a clear stale-ref failure."

**Actions.** The ref resolves to a rect, and the tap is `adb shell input tap x y` (`packages/platform-android/src/input-actions.ts:27-29`). Coordinates are guarded by an explicit guarantee matrix (`packages/contracts/src/interaction-guarantees.ts:30-78`):
- `disambiguation`: distinct subtrees fail and list candidates; geometry never wins.
- `occlusion`: covered targets are refused (using `drawing-order`).
- `keyboardOcclusion`: a tap point behind the IME is refused.
- `parentOwnedTouchPoint`: a parent tap point is chosen outside its interactive children.
- `offscreen`: a center outside the viewport is refused.
- `nonHittable`: the target is promoted to a hittable ancestor.

**Settle and verify.**
- `--settle` (`src/commands/interaction/runtime/settle.ts`, `stable-capture.ts:33-52`) polls interactive-only captures every ≤300 ms until the tree has been unchanged for **quiet = 500 ms** (1500 ms for broad transitions such as closing a modal), with a **10 s** timeout. It returns the diff against the pre-action tree, capped at **80 changed lines** plus a 20-entry unchanged-interactive tail.
- Settle is best-effort and never fails the action.
- A settled tree with **≤5 nodes** gets a warning that it is probably a splash screen and the agent should wait for text.
- If the UI never settles, the hint says so ("animation, carousel, or ticker?").

**Fill verification.**
- After text entry it samples the hierarchy every **150 ms** until the typed text holds for two consecutive samples (`packages/platform-android/src/fill-verification.ts:45-50`).
- It detects "text landed in a different field" (focused edit vs. edit at point).
- `adb input text` is chunked at **8 chars** because longer strings get truncated in some IME states (`text-input.ts:37-42`).

**Verdicts (agent-device)**

| Technique | Verdict |
|---|---|
| Persistent on-device session (no per-call UiAutomation connect) | **steal** (in v1 that means the detached u2 server) |
| waitForIdle with quiet=100 ms and timeout=500 ms | **steal** for the v2 APK. v1 relies on the u2 server's 0 plus our own settle. |
| Cache clear before each capture | **steal** (see §3) |
| Proxy-label rule for Compose content-desc on a passive View | **steal** in the merge step |
| Generic `pkg:id/foo` ids are not labels | **adapt**: keep the id as `#foo`, but don't treat it as a label for "labelled" pruning |
| Off-screen summaries | **skip in v1** (u2 drops invisible nodes, see finding 0.3). Revisit in v2. |
| "Unchanged" acknowledgement for repeated snapshots | **steal**: it's cheap and saves tokens |
| Ref frames expire on every mutation | **adapt**: droidctl re-resolves by fingerprint, but it should *also* scope refs to a screen signature (package + activity + window set). A cross-screen ref should give `stale-ref`, not a fingerprint match on a look-alike "OK" button. The action's own diff should re-issue refs so the agent can chain actions without a new snapshot. |
| Guarantee matrix (occlusion, keyboard, offscreen, hittable-ancestor, ambiguity-fails) | **steal** as typed error kinds: `occluded`, `offscreen`, `ambiguous` |
| `--settle` diff capped at 80 lines, plus a tiny-tree warning | **steal** (the plan's ≤10-line diff is probably too tight for a screen change. Use counts plus the first N lines.) |
| Fill verification, sampled until held twice at 150 ms | **steal** for `type` read-back |
| Headless IME that adds no tree nodes | **adapt** (v2). In v1 prefer ACTION_SET_TEXT so no IME switch is needed. |
| `adb input text` in 8-char chunks | **steal** if we ever fall back to `input text` |
| TypeScript daemon complexity (≈100 kLoC) | **skip**: droidctl's value is being small |

### 4.2 appium/appium-mcp (Apache-2.0, 481 stars, active) and 4.3 CursorTouch/Android-MCP (MIT, 871 stars)

These were analysed by a sub-agent. The file:line refs below were reported from the clones.

**appium-mcp.**
- **State.** State comes from `driver.getPageSource()`, the full XML (`src/command.ts:320-326`). The session caps set `appium:settings[waitForIdleTimeout]=0`, `actionAcknowledgmentTimeout=0` and `waitForSelectorTimeout=0` (`src/tools/session/create-session.ts:139-144`). `ignoreUnimportantViews` is off by default.
- **Format.** `generate_locators` returns JSON per interactable element, with several locator strings each (`src/tools/test-generation/locators.ts:60-68`). That is very verbose.
- **Pruning.** Keep EditText/Button/ImageButton/CheckBox/RadioButton/Switch/ToggleButton/TextView, or anything clickable or focusable (`locators/element-filter.ts:87-107`).
- **Locators.** The priority for UiAutomator2 is `accessibility id > id > xpath > -android uiautomator > class name` (`locators/locator-generation.ts:129-131`). Each candidate is **checked for uniqueness** against the page source (`:45-50`).
- **Re-resolve with fallback chain.** `handleAndroidAlert` (`tools/interactions/handle-alert.ts:72-100`) re-reads the source, matches by text/desc (clickable first), then tries each generated locator in order until one resolves.
- **Text.** `setValue`, i.e. UIA2 setText (replaces content), `src/command.ts:196-203`.
- **End of list.** `scroll_to_element` treats identical page source before and after a swipe as the end of the list (`tools/gestures/handlers/scroll-to-element.ts:33-55`, max 10 attempts by default, 80 at most).
- **Errors.** Normalized error codes (ELEMENT_NOT_FOUND, STALE_ELEMENT, TIMEOUT…) and `durationMs` evidence (`tools/evidence.ts:14-46, 121-141`).
- **VLM finder.** An optional Qwen-VL finder returns `ai-element:<cx>,<cy>:<bbox>` pseudo-ids, backed by an LRU cache keyed by instruction and screenshot hash (`ai-finder/vision-finder.ts:31-90, 237-263`).

**Android-MCP.**
- **State.** It uses u2 with one persistent `u2.connect()` (`src/android_mcp/mobile/service.py:113-116`). The dump and the screenshot run in parallel threads (`:134-164`).
- **Format.** A `tabulate` table, one element per row (`tree/views.py:23-27`):
  ```
  Label  Name     ResourceId  Class                   Coordinates
  3      Sign in  btn_login   android.widget.Button   (540,1210)
  ```
- **Pruning.** Keep `enabled` nodes that are focusable, clickable, long-clickable, checkable, scrollable, selected or password, or whose class is in 8 widget classes (`tree/config.py:1-10`, `tree/service.py:85-94`). Drop nodes with no name.
- **Label rule.** The name comes from **descendant text, but the recursion stops at actionable children** (`tree/service.py:51-83`). This is exactly the "merge row text into the clickable row" rule, with the correct boundary.
- **Actions.** Actions are *coordinate* clicks taken from the table. Refs are never re-resolved.
- **Type bug.** `Type(text, x, y)` ignores x and y and types into whatever is focused, via FastInputIME (`__main__.py:346-350`). That is a real-world example of the "text went to the wrong field" failure.
- **Waits and verification.** No waits or verification, apart from `Wait(duration)` sleeps.
- **Screenshot.** Optional Set-of-Mark screenshot annotation and a 256-color quantized PNG (`tree/service.py:96-141`, `mobile/service.py:178-208`).

**Verdicts**

| Technique | Verdict |
|---|---|
| Descendant-label merge that stops at actionable children (Android-MCP) | **steal**. This is the exact boundary rule for plan step 3. |
| Uniqueness-checked locator list per ref with an ordered fallback (appium-mcp) | **steal** for `locate.py`. Priority: resource-id+text → desc → text → class+ancestor-path → bounds. |
| Unchanged source after a swipe means end of list | **steal** for `swipe` / `scroll-to` (`reached_end: true`) |
| Normalized error kinds and `durationMs` | **steal** (already chromectl style). Add `timing` to results. |
| Idle timeouts 0 | **steal** (u2 already does it) |
| Set-of-Mark screenshot plus a quantized PNG | **steal** for `shot --marks` (the plan already has it). Use high-contrast colors. |
| Parallel dump and screenshot | **adapt**: only when `--shot` is requested |
| Raw page source / verbose locator JSON as the LLM view | **skip** |
| Built-in VLM finder | **skip**: the calling agent is already multimodal |
| Pseudo-ref grammar for coordinates (`ai-element:x,y`) | **adapt**: `tap @540,1210` as an explicit escape hatch, reported as `mode:"coords"` |

### 4.4 web-infra-dev/midscene (MIT, 15k stars) and 4.5 takahirom/arbigent (Apache-2.0, 646 stars)

These were analysed by a sub-agent. I spot-checked `detectStuckScreen.kt`, the midscene yadb strategy and the no-Ctrl+A comment.

**midscene (packages/android): vision-first**

**UI state is a screenshot only.** `screenshotBase64()` (`packages/android/src/device.ts:1366-1495`) tries sources in this order:
1. An optional **persistent scrcpy H.264 stream**, set to all-I-frames (`i-frame-interval=0`), `maxFps:10`. It keeps only the latest keyframe and decodes it with ffmpeg on demand. A frame counts as fresh if it is under 500 ms old; if nothing arrives within 300 ms, it falls back to adb (`scrcpy-manager.ts:26,38,49,71-75,438-446`).
2. `adb.takeScreenshot`.
3. `screencap` plus pull.
4. "yadb" for FLAG_SECURE screens.

Supporting details:
- Screen size, orientation and scale are cached (`device.ts:940,1153,1240`).
- Images are shrunk and sent as JPEG q90 (`core/src/agent/screenshot-preparation.ts:42-64`).
- A `uiautomator dump --compressed` tree exists (`ui-tree-capture.ts:111-131`, 3 retries, 250 ms apart), but core **never puts it in the prompt** (`core/src/device/index.ts:222`).

**What the LLM sees and how it clicks.** The model sees the screenshot plus a natural-language target and returns a bbox. Qwen uses `bbox_2d` normalised to 0-1000 (`core/src/ai-model/models/qwen.ts:24-26`). "deepLocate" crops and upscales for a second pass (`cropMaxLongEdge:1000`, `core/src/service/utils.ts:28-29`). The tap is `input swipe x y x y 150` at the bbox center (`device.ts:2149-2159`). There are no refs; every action is located again from a fresh screenshot.

**Text input.**
- ASCII goes through `input text`.
- Non-ASCII text, shell metacharacters, or text containing both quote types go through **yadb** (`app_process … com.ysbing.yadb.Main -keyboard '<text>'`, a pushed dex with no IME install, `device.ts:899-907,1998-2014`).
- Clearing is MOVE_END then 100× DEL/FORWARD_DEL, **avoiding Ctrl+A because Huawei/HarmonyOS ROMs type a literal "a"** (`device.ts:1632-1665`, comment at `:1651`).

**Waiting and verification.**
- There are only fixed sleeps: `waitAfterAction` is 300 ms (`core/src/agent/task-builder.ts:335-338`), 500 ms after a scroll, and 1000 ms after scroll-until.
- Verification is left to the LLM: "mark finished only AFTER you have confirmed … in the screenshot" (`prompt/planning/system-prompt.ts:124`).
- The locate cache (xpath/feature) only works on web; Android doesn't implement `cacheFeatureForPoint` (`task-builder.ts:595-639`).
- Scroll-to-edge is a blind 10 drags, because it can't detect the end of a list (`device.ts:73-76`).

**arbigent: an AI test agent on the Maestro API**

**UI state.**
- It calls Maestro `viewHierarchy(false)` over Maestro's gRPC server and also takes a screenshot on every step (`ArbigentDevice.kt:295-296`, `ArbigentAgent.kt:944-960`).
- The connection is persistent and reconnects only after a read fails. The comment there says a pre-check "doubled the cost of every read" (`ArbigentDevice.kt:281-294`).
- One fetch per step feeds the element list, the tree and focus (`:350-374`).
- If bounds haven't been laid out yet, it retries 2× with a 1 s gap (`:376-393`).

**Format** (`UserPromptTemplate.kt:16-42`). The prompt is a Set-of-Mark screenshot plus an `index:element` list:
```
<ELEMENTS>
index:element
3:Button(text=Sign in, id=login_btn, clickable=true)
```
- The class is shortened to its simple name, and the id is the suffix after `/` (`ArbigentDevice.kt:128-130,166-172,855-908`).
- Labels are **hoisted from descendants by DFS** into the clickable container (`:861-867`).
- Apps can supply `[[aihint:…]]` content-desc hints (`:820-852`).

**Pruning** (`optimizeTree2`, `:930-995`):
- Drop the status_bar id, zero-size nodes, and subtrees with no meaningful descendant. Meaningful means non-blank text/desc/hint, or checked/clickable/focusable/selectable (`:910-926`).
- Collapse non-meaningful wrappers: promote a single child, hoist multiple children.
- Drop off-screen nodes via Maestro `filterOutOfBounds` (`:142-146`).
- There are no numeric caps.

**Actions.** `ClickWithIndex` taps the bounds center of `elements[index]` from the step-start list, with **no re-resolution** (`AgentCommands.kt:69-78`). Re-resolution exists only for TV/replay (`ArbigentDevice.kt:495-518,1066-1069`). There, identity is the sorted attributes minus bounds/focused/selected, recursive over children, plus an occurrence index, and it must be **exactly one match** or return null.

**Verification.** `detectStuckScreen` compares the new screenshot with the previous one pixel by pixel, with exact equality (`detectStuckScreen.kt:6-31`). If they're identical, it tells the LLM: "The current screen is identical to the previous one. Please try other actions." (`ArbigentAgent.kt:1391-1404`).

**Cache.** AI decisions are cached under the key `uitree-<hash(optimizedTree)>-context-<hash>` (`ArbigentAgent.kt:1366-1368`).

**Verdicts (midscene, arbigent)**

| Technique | Verdict |
|---|---|
| arbigent pruning (meaningful attributes, wrapper collapse, zero-size and status-bar drops) | **steal**. It is close to the plan. |
| arbigent DFS label hoisting into the clickable container | **steal** (same idea as Android-MCP, which also stops at actionable children) |
| arbigent identity = attributes minus volatile fields (bounds, focused, selected) + occurrence index, unique match | **steal**. Exclude `focused`/`selected`/`checked` from the fingerprint, because they flip when you tap. |
| arbigent `Class(text=…, id=suffix)` line | **adapt**. The plan's role-based line is denser. |
| Hash of the pruned tree as the UI-state key | **steal** as the change signal and snapshot memo |
| Stuck screen via exact pixel equality | **adapt**: use the pruned-hash comparison. Pixel equality breaks on clocks and cursors. |
| Reconnect only on failure; one fetch per step | **steal** |
| midscene yadb for non-ASCII input (no IME switch) | **adapt**: the best fallback if ACTION_SET_TEXT fails and we don't want to switch the IME |
| midscene clear without Ctrl+A (MOVE_END + DEL×N) | **steal** for the key-based clear fallback |
| midscene cached screen size/density | **steal** |
| scrcpy keyframe stream | **skip** for v1. Possibly useful for a fast `shot` later. |
| Vision bbox locate, fixed 300 ms sleeps, blind 10-drag scroll-to-edge | **skip** |

---

## 5. Cross-cutting pitfalls for droidctl (with evidence)

1. **Server lifetime.** u2 kills its server at Python exit (`core.py:231`, `README_CN.md:1119`). Detach it, or run a daemon. Measure per-call latency before and after this change. This is the single biggest latency item.
2. **A 20 s default wait on a missing element** (`settings.py:16`, `_selector.py:295-341`). Always pass `timeout=`.
3. **Stale a11y cache** with a persistent UiAutomation (Maestro `MaestroDriverService.kt:237-249`, agent-device `AccessibilityTreeCapture.java:67-93`). u2 flushes implicitly through `setServiceInfo` in `setCompressedLayoutHierarchy`. **Don't** bypass `dumpWindowHierarchy` in favour of other RPCs (for example `objInfo` polling) without also flushing (`objInfoOfAllInstances` flushes only on API 34+).
4. **Non-deterministic root order** (a HashSet in the dumper). Normalize before hashing or diffing.
5. **System windows in the dump.** Status bar, nav bar, IME and `com.android.systemui` overlays appear in every dump. Filter by package: keep the foreground app package plus dialogs and permission controllers, and collapse the IME to the header flag "keyboard: shown". Otherwise the clock ticking in the status bar makes **every** change-check report `changed:true`. Hash only the app windows.
6. **Positional refs** (android_world `interface.py:299-310`, agent-device ADR 0014 bug #1239). Fingerprint re-resolution is right. Also bind refs to a screen signature, and return `stale-ref` rather than tapping a look-alike.
7. **Look-alike matches.** Maestro only accepts a *unique* exact match (`ViewHierarchy.kt:52-63`), and agent-device fails ambiguous matches with a candidate list. The plan's "if it matches several, pick the nearest by bounds" is fine as a tie-breaker *only* if the fingerprint otherwise matches exactly (same id, text and ancestor path). Otherwise return `ambiguous`.
8. **The IME is left switched** (u2 `_input.py:49-64`). Restore it, or don't switch.
9. **set_text taps first** (`AutomatorServiceImpl.java:672-673`). If the field center is under the keyboard or another view, the tap lands elsewhere. Check occlusion first, or fork the jar to skip the click. ACTION_SET_TEXT doesn't need focus.
10. **Hint text shows up as `text`** on API 26+ for empty fields (Maestro `AccessibilityNodeInfoExt.kt`). Flag `empty` when `text == hint`. Otherwise the agent thinks the field is filled with "Search…".
11. **Retrying a toggle** (Maestro retry-if-no-change). A second tap on a switch or checkbox undoes the first. Report, don't retry, by default.
12. **Settle is flaky on animated screens** (carousels, clocks, progress). Use agent-device's pattern: best effort, never fail the action, return `settled:false` plus a hint after the budget. Add a tiny-tree (≤5 nodes) splash warning.
13. **WebView content is missing** in u2 dumps (invisible-children filter). Document it and fall back to `shot`. This is a jar-fork candidate.

## 6. Recommended technique stack for droidctl v1 (summary)

- **Transport:** the detached u2 jar server, with droidctl talking JSON-RPC over `adb forward` (one forward, cached in state). Use `jsonrpc.click(selector)` (1 RPC), `objInfoOfAllInstances`, `setText`, `waitForExists`/`waitUntilGone` and `dumpWindowHierarchy(compressed=True, 50)`.
- **Snapshot:** as planned, with these additions:
  - the Android-MCP stop-at-actionable merge;
  - the agent-device Compose proxy-label rule;
  - package filtering of system windows;
  - text==hint → `empty`;
  - Maestro's text∪hint∪desc∪error matching for `--find` / `--text`;
  - a repeated identical snapshot returns "unchanged".
- **Ref resolution:**
  1. Maestro-style exact-attributes-minus-bounds unique match.
  2. Else an appium-style ordered locator chain with uniqueness checks.
  3. Else nearest by bounds among exact-fingerprint candidates.
  4. Else `stale-ref` / `ambiguous`.
  Scope everything by screen signature.
- **Before a tap:** a Maestro `isVisible` hit test, the agent-device offscreen and keyboard-occlusion checks, and walking up to a clickable ancestor.
- **After an action:** a settle loop on the hash of the pruned app-window snapshot (two equal consecutive captures, poll about 150–200 ms, quiet ≥300 ms, cap 1.5–3 s). Return `changed`, `settled`, the diff (counts plus up to about 40 lines), re-issued refs, the last toast and `durationMs`.
- **Text:** ACTION_SET_TEXT via `setText`, then read back (sample every 150 ms, hold ×2). If it didn't take, fall back to the u2 IME **with IME restore**.
- **v1.5/v2:** fork the MIT jar or build our own APK with a `performAction` RPC, cache clear, the interactive-windows flag, window types, `actions[]`/scroll hints, WebView children and `isShowingHintText`. Use the android_env forest proto as the schema reference.
