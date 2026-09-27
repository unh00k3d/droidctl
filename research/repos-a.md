# droidctl research — repos A: droidrun (+portal), minitap mobile-use, mobile-next mobile-mcp

Date: 2026-09-27. All clones are shallow, in `/tmp/research/repos/`. Extra sources pulled because the
real logic lives outside the named repos:
- `mobilerun-core-local` 0.6.0 wheel (droidrun's actual Android driver + Portal client), unpacked to `/tmp/research/pkgs/mobilerun_core_local/`
- `mobile-next/mobilecli` (Go binary that mobile-mcp shells out to for everything since ~1.0), in `/tmp/research/repos/mobilecli`
- mobile-mcp `src/server.ts` at tag 0.0.30, saved as `/tmp/research/mcp-server-0.0.30.ts`, so the historical mis-click cause can be seen

| repo | GitHub now | stars | license | last push | HEAD |
|---|---|---|---|---|---|
| droidrun/droidrun | redirects to **droidrun/mobilerun** | 9,481 | MIT | 2026-09-25 | 4f168cb (2026-09-16) |
| droidrun/droidrun-portal | redirects to **droidrun/mobilerun-portal** | 381 | **AGPL-3.0** (LICENSE file, "Mobilerun Portal"; GH shows NOASSERTION) | 2026-09-16 | d4cb7d6 |
| mobilerun-core-local (PyPI) | droidrun/mobilerun-core-local | – | MIT | 0.6.0 | – |
| minitap-ai/mobile-use | same | 3,182 | Apache-2.0 | 2026-09-14 | 62913c9 |
| mobile-next/mobile-mcp | same | 7,489 | Apache-2.0 | 2026-09-23 | 18d0e8c (v1.0.5) |
| mobile-next/mobilecli | same | 278 | NOASSERTION (check before copying) | 2026-09-26 | – |

**Licensing matters for droidctl.** Portal is AGPL-3. Read it for ideas, but don't paste its Kotlin into our v2 APK
unless droidctl's APK is also AGPL. The Python side (mobilerun, core-local) is MIT and mobile-use/mobile-mcp are
Apache-2.0, so code can be copied from them with attribution.

---

## 1. droidrun / mobilerun (Python) + mobilerun-core-local (driver)

### 1.1 Architecture
- The agent framework (`mobilerun/agent/*`, llama-index workflows, many LLM providers) is layered on a thin
  **StateProvider → DeviceDriver** split.
  - `mobilerun/tools/ui/provider.py`: `AndroidStateProvider` fetches raw state, runs a `TreeFilter`,
    then a `TreeFormatter`, and produces a `UIState` holding both `formatted_text` (the LLM text) and `elements` (for taps).
  - The driver lives in the **separate** MIT package `mobilerun-core-local`: `driver/android/adb.py`
    (`AndroidDriver`) plus `transport/android/portal_client.py` (`PortalClient`). The repo only re-exports it
    (`mobilerun/tools/driver/__init__.py:1-26`).
- The transport is `async_adbutils` (an async fork of adbutils). There is no uiautomator2.

### 1.2 How the UI tree is obtained
1. **Portal present (normal path).** `PortalClient.get_state()` calls `GET http://localhost:<fwd>/state_full` with a
   Bearer token (`portal_client.py:342-394`). If that fails it falls back to
   `adb shell content query --uri content://com.mobilerun.portal/state_full` (`:396+`).
   - The token itself is read over ADB via the content provider `auth_token` (`:82-124`). This is a nice trick:
     only someone with ADB can obtain the token for the device's HTTP server.
   - It reuses an existing `adb forward` if one exists (`:205-233`). If ping fails, it toggles the server on through
     the content provider and retries (`:176-199`).
   - **Latency anti-pattern:** each call creates a new `httpx.AsyncClient()` (`:358`, and likewise for screenshot
     and ping), so there is no keep-alive and each state read pays a new TCP connect through the adb forward.
2. **No Portal.** `uiautomator dump /dev/tty`, parsed into the same dict shape (`adb.py:370-402`). This is the slow
   path (~1-3 s per dump).
3. **Retry/recovery** (`provider.py:33-129`) uses backoff delays of 1, 2, 3, 5, 8 and 10 s over 7 attempts. After 5
   failures it **restarts the a11y service** by setting `accessibility_enabled 0`, rewriting
   `enabled_accessibility_services`, setting it back to 1, and toggling the TCP server (`provider.py:249-292`).
   This is a reasonable "wedged a11y" recovery.

### 1.3 What the LLM sees (exact format)
`IndexedFormatter` (`mobilerun/tools/formatters/indexed_formatter.py:22-99, 227-266`). I generated real output by
running their formatter and `ConciseFilter` on a small Settings-like tree (`/tmp/research/dr_example.py`):

```
**Current Phone State:**
• **App:** Settings (com.android.settings)
• **Keyboard:** Hidden
• **Focused Element:** ''

Current Clickable UI elements:
'index. className: resourceId; checkedState, text - bounds(x1,y1,x2,y2)':
1. FrameLayout: "android.widget.FrameLayout" - (0,0,1080,2400)
2. LinearLayout: "android.widget.LinearLayout" - (0,0,1080,2400)
3. TextView: "android:id/title", "Wi-Fi" - (40,100,600,180)
4. Switch: "com.android.settings:id/switch_widget", "com.android.settings:id/switch_widget" ; isChecked=True - (900,100,1040,180)
5. RecyclerView: "com.android.settings:id/recycler_view", "com.android.settings:id/recycler_view" - (0,200,1080,2300)
6. LinearLayout: "android.widget.LinearLayout" - (0,200,1080,380)
7. TextView: "HomeNet" - (40,220,800,280)
8. TextView: "Connected" - (40,290,800,350)
```

Observations:
- The heading says "Clickable UI elements", but **every visible node >5 px is listed**, including pure layouts.
  No clickable, scrollable or editable flag is printed, so the LLM cannot tell row 6 is the tappable one.
- `text` falls back through text, contentDescription, resourceId, className (`:236-241`). The result is
  duplicated noise such as `"…switch_widget", "…switch_widget"` and `"android.widget.FrameLayout"`.
- `_flatten_with_index` returns `children: []` per node, so the indentation code path never fires. The hierarchy
  is lost, and a list row's text is **not merged** into the clickable row.
- Bounds are printed for every line, which is a big token cost. They exist so the model can reason about
  coordinates or vision.
- With `use_normalized`, bounds are rescaled to 0-1000. In vision mode a "coordinate contract" note is appended and
  the screenshot is resized with a labeled grid (`provider.py:307-360`). That is a lot of machinery to keep screenshot
  space and tap space aligned. droidctl avoids the whole problem by never asking the model for coordinates.

### 1.4 Filters (pruning) with thresholds
- `ConciseFilter` (`filters/concise_filter.py:29-67`) drops a node when it doesn't intersect the screen or when
  `w <= 5 or h <= 5` (`min_element_size` = 5, which Portal also sends in `device_context.filtering_params`).
  Parents are kept. **Nothing is pruned on semantics.**
- `DetailedFilter` (`filters/detailed_filter.py`):
  - visibility threshold 10% of the node's area on screen (`:12,48-74`), with parent preservation (`:133-157`)
  - optional clip-to-screen
  - **drops Gboard nodes** by `resourceId.startswith("com.google.android.inputmethod.latin:id/")` (`:106-109`)
  - `ignoreBoundsFiltering` escape (`:136`)
- Which filter runs: `droid_agent.py:698` uses `ConciseFilter() if vision_enabled else DetailedFilter()`, and the CLI
  device commands use Concise (`cli/device_commands.py:263`).

### 1.5 From index to action
- `click(index)` (`agent/utils/actions.py:209-238`) calls `ctx.ui.get_element_coords(index)` and then
  `driver.tap(x, y)`, which is `adb shell input tap` via `device.click` (`adb.py:144-146`).
  **Pure coordinates, taken from the snapshot the LLM saw. There is no re-resolution against the live UI and no
  node action.** Portal has `/tap` via `dispatchGesture` (below), but the Python driver doesn't use it for clicks.
- Coordinates are the centre of the bounds, adjusted by **tap blockers** (`tools/ui/state.py:63-125`):
  - At format time, `_add_tap_blockers` (`indexed_formatter.py:175-208`) records, for each node, any **sibling**
    in the same `windowId` with a higher `drawingOrder` that is visible and clickable and whose rect overlaps.
  - If the centre falls inside a blocker, `find_uncovered_point` (`helpers/geometry.py:13-41`) subtracts the
    blocker rects and taps the centre of the largest remaining rectangle.
  - The comment says it plainly: "drawingOrder is relative to siblings, not a global z-index … Missing or tied order
    is insufficient evidence".
  - This geometry is **worth stealing** for our coordinate fallback (when ACTION_CLICK is unavailable or
    `clickable=false`).
- The success message echoes element info and child texts (`actions.py:219-233`). That is a good pattern (see 1.8).
- Parallel tool calls (`fast_agent/system.jinja2:83-100`): the prompt teaches `type(index=3)` followed by
  `click(index=5)` in one turn, with both resolved against the *same* stale snapshot. After typing, the IME covers the
  lower screen, so index 5's coordinates can land on the keyboard. **Pitfall. droidctl's re-resolution at action time
  prevents it.**

### 1.6 Text input
- **With Portal:** `POST /keyboard/input {base64_text, clear}` goes to Portal's own IME `MobilerunKeyboardIME`. That
  IME is **enabled and selected on connect** via `ime enable` and `ime set` (`portal.py:543-557`, never undone: `disable_keyboard()` at `:559` has no callers;
  `adb.py:127-137`), so the user's keyboard is replaced while the tool is connected.
- **ADB-only fallback** (`adb.py:310-337, 405-422`):
  - `input text`, chunked at 200 chars, spaces become `%s`
  - **rejects any non-ASCII** and literal `%s` with a clear error, which is honest
  - clear = `input keycombination KEYCODE_CTRL_LEFT KEYCODE_A`, then DEL; falls back to MOVE_END plus N×DEL
    (batched `input keyevent --delay 0 67 67 …` 50 per call)
  - verifies the clear by re-dumping and checking the focused node's text length, treating `text == hint` as
    empty (`:356-368`)

### 1.7 Waits and stability
- `after_sleep_action: 1.0` s is a **fixed sleep after every action** (`executor_agent.py:259`,
  `fast_agent.py:592`, `config_example.yaml:17`).
- `wait_for_stable_ui: 0.3` is defined in config (`config_manager.py:130`) but **never used anywhere**, so there is
  no real stability detection.
- Macro replay does have a similarity gate (`macro/matcher.py:18-43`):
  - Jaccard over node "semantic keys" `(resource_id, class, text, content_desc, clickable, enabled, focused)`
    (`macro/state.py:42-51`), weighted 0.85 nodes + 0.15 phone state, threshold 0.85
  - it polls until the pre-state matches before replaying a step (`macro/replay.py:157-181`)
  - swipes are followed by a hard `sleep(2)` (`replay.py:222-224`)

### 1.8 Error handling
- `ValueError` messages list the available indices (first 20) when an index is missing (`state.py:70-78`).
  That is good agent UX.
- An element with no bounds gives an explicit message, and a fully obscured element gives "No clear tap point".
- Every action returns `ActionResult(success, summary)`, and failures are strings, not typed.

### 1.9 Verdicts (droidrun Python side)
| technique | verdict | note |
|---|---|---|
| Portal content-provider auth token, then HTTP over `adb forward` with Bearer | **adapt (v2)** | nice security model for our APK |
| Recovery: toggle a11y service after N failed reads | **adapt (v2)** | but *append* to `enabled_accessibility_services`, don't overwrite (see 2.6) |
| IndexedFormatter output | **skip** | lists all nodes, no flags, duplicated ids, no row merge, bounds on every line |
| Keyboard-node filter by IME package | **steal** | generalize: drop every node whose window is `TYPE_INPUT_METHOD` or package is the current IME; report only "keyboard: shown" |
| 10%-visible rule + parent preservation | **steal** | cheap and correct; fits prune step 1 |
| Tap-blocker / largest-uncovered-rect geometry | **steal** (MIT) | for the coordinate fallback path only |
| Index → stale coordinates → `input tap` | **skip** | the very thing droidctl is designed against |
| Fixed 1 s post-action sleep | **skip** | replace with event/fingerprint-based settle |
| Jaccard semantic-key similarity | **steal** | good basis for the "did UI change" score and `--diff` matching |
| ADB-only text: ASCII-only guard with explicit error | **steal** | as the last fallback with a clear `bad-args`-style message |
| Clear via select-all+DEL, verify with focused text / hint check | **adapt** | u2 `clear_text()` / ACTION_SET_TEXT "" first |
| New HTTP client per request | **skip** | keep one persistent session (the chromectl daemon lesson) |

---

## 2. droidrun-portal / mobilerun-portal (Kotlin accessibility-service APK, AGPL-3)

### 2.1 Surfaces and protocol
- Package `com.mobilerun.portal`. The a11y service is `.service.MobilerunAccessibilityService` and the IME is
  `.input.MobilerunKeyboardIME` (`core-local portal.py:32-46`).
- **Three transports** all dispatch to one `ApiHandler`/`ActionDispatcher`:
  1. **HTTP `SocketServer` on device port 8080.** Every path except `/ping` requires
     `Authorization: Bearer <token>` (`SocketServer.kt:185`). GET routes include `/state_full[?filter=false]`
     (filter defaults to true, `:361-365`), `/state`, `/phone_state`, `/screenshot[?hideOverlay=false]`,
     `/version`, `/packages`, `/clipboard/get`. POST actions go through `ActionDispatcher.dispatch`.
  2. **ContentProvider** `content://com.mobilerun.portal/<path>` (`MobilerunContentProvider.kt:540-548`). It is
     reached with `adb shell content query|insert --uri … --bind k:s:v`. It works without port forwarding but pays
     one `adb shell` per call.
  3. **WebSocket** JSON-RPC-ish plus a reverse connection to their cloud. There is also WebRTC/scrcpy streaming,
     irrelevant for droidctl.
- Action names (`ActionDispatcher.kt:75-270`): `tap`, `swipe`, `global`, `app`, `app/stop`, `keyboard/input`,
  `keyboard/clear`, `keyboard/key`, `clipboard/get|set`, `overlay_offset`, `screenshot`, `packages`, `state`,
  `files/*`, `install`, …
  **There is no "click node by id/index" action.** `ACTION_CLICK` is only used internally for auto-accepting system
  dialogs (`ApiHandler.kt:1024,1037`; `MediaProjectionAutoAccept.kt`; `PackageInstallerAutoAccept.kt:122`).

### 2.2 Tree building (`core/AccessibilityTreeBuilder.kt`)
- `MIN_ELEMENT_SIZE = 5`, `VISIBILITY_THRESHOLD = 0.01` (1% visible) (`:15-17`). Children are processed first,
  and a parent is kept if it passes or has kept children (`:80-124`).
- Each node serializes **~45 fields** (`:127-359`):
  - text, desc, hint, stateDescription, tooltip, paneTitle, error
  - boundsInScreen and boundsInParent
  - all boolean states, inputType, liveRegion, windowId, drawingOrder, maxTextLength, granularities
  - rangeInfo, collectionInfo, collectionItemInfo, extras, the full actionList with names
  - labelFor/labeledBy flags, `uniqueId` (API 33+), `containerTitle` (API 34+)

  The payload is huge; pruning happens host-side.
- **Traversal guards:** a max depth and cycle detection via an active-path set (`:50-69`,
  `AccessibilityTraversalGuard`). Every call to `getChild`/`getBoundsInScreen` is wrapped in try/catch
  RuntimeException, because binder calls on dying windows throw.
- **Multi-window roots** (`core/AccessibilityRootResolver.kt`):
  - start from `rootInActiveWindow`
  - add any `TYPE_APPLICATION` windows with a *higher layer* than the active one, sorted by layer desc
  - if there is no active root, use app plus system windows
  - dedupe by windowId and root identity

  Additional roots are appended as children of the primary tree (`StateRepository.kt:39-69`). This is how
  dialogs and popups that aren't the "active window" still appear. **Worth copying conceptually for v2.**
  uiautomator2's dump already includes all windows.
- **Wedged-binder protection** (`api/A11yReadGate.kt`):
  - every a11y read runs single-flight per key on a bounded daemon pool with a **2 s budget**
    (`ApiHandler.kt:83`)
  - on timeout it returns the **last good snapshot marked `degraded:true, degradedReason, snapshotAgeMs`**
  - the stuck worker is sacrificed, not cancelled

  **Steal this for v2.** It is excellent.

### 2.3 Element indexing and overlay
- Separately from `state_full`, the service keeps a "visible elements" list (`MobilerunAccessibilityService.kt:651-842`).
  - It is a DFS over the resolved roots. Every node that intersects the screen and is >5 px gets an
    `overlayIndex = counter++` (`:745-781`).
  - Label = text, then desc, then the id suffix, then the short class (`:754-760`).
- The overlay (`ui/overlay/OverlayManager.kt`) draws numbered boxes (set-of-marks) and refreshes every
  **250 ms** (`REFRESH_INTERVAL_MS`, `:66`) while visible.
- Screenshots hide the overlay by default (`hideOverlay=true`).
- The numbering is DFS order, like the Python `ConciseFilter` flatten, so the on-screen numbers ≈ LLM indices.
  This is fragile: any filter difference shifts the numbers.

### 2.4 Clicks and gestures
- `GestureController.tap` uses `dispatchGesture` with a 50 ms single-point stroke; swipe duration is clamped to
  10-5000 ms; `performGlobalAction` handles back/home/recents (`service/GestureController.kt:14-86`).
- The result is only "dispatched: true/false", with no completion callback awaited.
- These are **coordinates again**. Portal never offers ACTION_CLICK on a node to the host.

### 2.5 Text input (the best part)
- `ApiHandler.keyboardInput` (`ApiHandler.kt:363-402`) tries the IME first, then falls back to accessibility
  `ACTION_SET_TEXT`.
- **IME path** (`input/InputConnectionTextEditor.kt`):
  - `finishComposingText`, then read an `ExtractedText` snapshot, then `setSelection`, then
    `commitText(text, 1)`, then **read back and verify**
  - results are typed: `Verified | Rejected | AcceptedUnverified | CommitOutcomeUnknown | InputSessionChanged`
  - it tracks an input-session **generation** so it can detect that focus moved mid-input
  - 2 attempts, 100 ms apart
  - for `clear=true` it requires the extracted text to cover the whole field before replacing
  - it deliberately **does not fall back** when the outcome is uncertain, to avoid double-typing
- **A11y path** (`MobilerunAccessibilityService.kt:954-1030`):
  - target is `findFocus(FOCUS_INPUT)`, else the first editable node
  - computes the final text honoring the selection and `text == hint` meaning empty
  - `performAction(ACTION_SET_TEXT)`, then `ACTION_SET_SELECTION` to put the cursor at the end
- Unicode works on both paths.

### 2.6 Install and enable flow (`mobilerun_core_local/driver/android/portal.py`)
- Downloads the APK from GitHub releases, with a version map pinned to the SDK version via a gist (`:27-30,106-128`).
  It runs `adb install -r -g` with `uninstall=True`.
- Enable (`:400-418`) runs `settings put secure enabled_accessibility_services <svc>` then
  `settings put secure accessibility_enabled 1`. **This overwrites** any other enabled services (TalkBack, password
  managers, and so on). **Pitfall; droidctl must read, append with `:`, then write.**
- It polls the content provider until the service answers (`_wait_for_portal_service`, 10 s). On failure it opens
  `ACCESSIBILITY_SETTINGS` for the user (`:636-642`).
- Service config XML: `typeWindowStateChanged|typeWindowContentChanged`, `flagIncludeNotImportantViews|flagReportViewIds|flagRetrieveInteractiveWindows`,
  `canPerformGestures`, `canTakeScreenshot`, `isAccessibilityTool=true`, `notificationTimeout=100`.
- `onServiceConnected` then **overwrites** `serviceInfo` with `TYPES_ALL_MASK` and flags
  `REPORT_VIEW_IDS|RETRIEVE_INTERACTIVE_WINDOWS|REQUEST_TOUCH_EXPLORATION_MODE` (`MobilerunAccessibilityService.kt:277-292`).
  - This silently drops `INCLUDE_NOT_IMPORTANT_VIEWS`.
  - It requests touch-exploration mode, which would turn the phone into explore-by-touch for the human. It is
    probably a no-op because the XML lacks `canRequestTouchExplorationMode`.
  - **Do not copy either.**
- Activity name comes from `TYPE_WINDOW_STATE_CHANGED` class names not starting with `android.` (`:327-336`).
  Keyboard visibility = any window of `TYPE_INPUT_METHOD` (`:863-877`). **Steal both** for the snapshot header in v2.
  In v1, u2 gives `d.app_current()`, and the IME can be read from `dumpsys input_method` `mInputShown`.

### 2.7 Verdicts (Portal)
| technique | verdict |
|---|---|
| Token via content provider, HTTP via adb forward | **adapt (v2)** |
| ContentProvider transport as a no-forward fallback | **adapt (v2)**, useful for first-contact/health |
| Serialize ~45 fields per node, prune host-side | **skip**: prune on-device, ship compact JSON (and support `?full=1`) |
| Multi-window root resolution (active + higher-layer app windows) | **steal idea (v2)** |
| A11yReadGate: 2 s budget, single-flight, degraded last-good snapshot | **steal idea (v2)**, and expose `degraded` in `snapshot --json` |
| Traversal depth/cycle guards and try/catch per binder call | **steal idea (v2)** |
| Overlay numbered boxes | **skip for v1**; maybe `droidctl shot --marks` later (render host-side from bounds, no APK needed) |
| dispatchGesture taps | **adapt (v2)** as fallback only; primary = `performAction(ACTION_CLICK)` on the re-resolved node |
| IME commitText with read-back verification and typed outcome | **steal idea (v2)**; in v1 use u2's FastInputIME `send_keys` plus a read-back of the node text |
| ACTION_SET_TEXT with the hint==text rule | **steal** (v1 via u2 `set_text`, v2 native) |
| Overwrite `enabled_accessibility_services` | **skip, it's a bug** |
| Override serviceInfo flags at runtime (drops not-important views, requests touch exploration) | **skip** |

---

## 3. minitap-ai/mobile-use (Artemis ancestor)

### 3.1 Architecture
- A LangGraph multi-agent pipeline: planner → orchestrator → **contextor** (grabs device state) → **cortex**
  (decides, sees hierarchy plus screenshot) → **executor** (turns decisions into tool calls) → tools.
  See `graph/graph.py`.
- Device layer is `controllers/android_controller.py` (adbutils for input) plus `clients/ui_automator_client.py`
  (uiautomator2 for hierarchy, screenshot and text).
- Their README credits the multi-agent split for their 100% AndroidWorld result. The device layer itself is basic.

### 3.2 UI tree acquisition and latency
- `UIAutomatorClient.get_screen_data()` (`clients/ui_automator_client.py:286-312`) takes **a screenshot and a
  `dump_hierarchy(compressed=True)` on every contextor step**. Screenshots are always taken, even when unused.
- `_ensure_connected` calls `self._device.info` (an HTTP round-trip) **on every call** as a liveness probe
  (`:202-225`).
- **It uninstalls Maestro** (`dev.mobile.maestro`) from the user's device without asking, because it conflicts with
  the u2 instrumentation (`:166-178, 218-219`). The conflict is real (both are UiAutomation clients; only one can
  hold it). **For droidctl: detect it and fail with a typed error (`device`, with a hint). Never uninstall silently.**
- Tap helpers re-fetch the whole screen data, **including a screenshot**, just to find an element by id or text
  (`unified_controller.py:72` then `android_controller.get_ui_hierarchy()` then `get_screen_data()` at
  `android_controller.py:203-206`).

### 3.3 What the LLM sees
- There is **no pruning at all.** `_parse_hierarchy_xml_to_elements` (`ui_automator_client.py:39-109`) flattens
  every XML node and every attribute (content-desc is duplicated into `accessibilityText`). The cortex dumps it as
  `json.dumps(elements, indent=2)` (`agents/cortex/cortex.py:80-83`) plus a JPEG-50 screenshot (`:85-90`).
- Example (one node from `/tmp/research/sample.xml` run through their parser):
  ```json
  {
    "index": "0", "text": "", "resource-id": "", "class": "android.widget.LinearLayout",
    "package": "com.android.settings", "content-desc": "", "accessibilityText": "",
    "checkable": "false", "checked": "false", "clickable": "true", "enabled": "true",
    "focusable": "true", "focused": "false", "scrollable": "false", "long-clickable": "false",
    "password": "false", "selected": "false", "bounds": "[0,200][1080,380]"
  }
  ```
  That is ~110 tokens per node (7 nodes came to 3,076 chars, about 770 tokens). A typical 150-node screen is
  **~15-20k tokens**, plus the image.
- The prompt demands `target = {resource_id, resource_id_index, text, text_index, bounds:{x,y,width,height}}`
  (`agents/cortex/cortex.md:44-63`). But the hierarchy gives bounds as the string `"[x1,y1][x2,y2]"`, so **the LLM
  must convert corner pairs into x/y/width/height itself.** That is an arithmetic step where mis-taps are born.

### 3.4 From element reference to action
- `tap` tool (`tools/mobile/tap.py:52-120`): **it tries COORDINATES FIRST.** It takes the centre of the
  LLM-supplied bounds and runs `adb shell input tap`, then `resource_id + index`, then `text + index`.
  - The prompt claims the order is "if ID fails → tries bounds → tries text" (`cortex.md:61`), which contradicts
    the code.
  - "Success" means only that `adb shell` didn't throw (`android_controller.py:58-75`). A tap on the wrong spot is
    reported as success, so the fallbacks never run. **There is no UI-change verification.**
- `tap_element` by id or text (`unified_controller.py:50-87`) is a fresh dump, then the Nth match, then the centre
  of the bounds, then `input tap`. It re-resolves (good), but by coordinates.
- **Schema-drift bug:** `find_element_by_resource_id` looks up `element.get("resourceId")`
  (`utils/ui_hierarchy.py:65`), but the u2 parser produces `"resource-id"`. So focusing and reading back text by
  resource-id **never matches on Android**. It silently falls through to bounds. The unit tests use `resourceId`
  fixtures (`utils/test_ui_hierarchy.py:26`), so they pass.
- `get_bounds_for_element` expects a dict `{x,y,width,height}` (`ui_hierarchy.py:124-132`), but u2 elements carry
  a string, so it logs an error and returns None.
- **Lesson: one element schema end-to-end, with fixtures produced by the real parser from real dumps.**
  PLAN.md's `tests/fixtures/dumps/*.xml` approach is right.

### 3.5 Text input
- `send_text`: `d.set_fastinput_ime(True)`, `d.send_keys(text)`, then `set_fastinput_ime(False)` **every call**
  (`ui_automator_client.py:237-252`). Unicode works. Switching the IME twice per call is slow (~0.5-1 s) and makes
  the keyboard flicker.
- ADB fallback (`android_controller.py:121-143`): it splits on `%s` and wraps in single quotes, but also
  backslash-escapes `&<>|;()$\`"'` *inside* those single quotes, so the backslashes are typed literally. **Bug.**
- `focus_and_input_text` (`tools/mobile/focus_and_input_text.py`):
  1. focus (tap) if not focused
  2. `tap_bottom_right_of_element` at 99%/99% of the bounds to move the cursor to the end (`tools/utils.py:91-101`)
  3. type
  4. re-dump and **report the field's full content back** ("Here is the whole content of input with id …")

  The read-back idea is good. The corner-tap cursor hack is fragile (it can hit clear-buttons or eye icons).
- Clearing (`focus_and_clear_text.py`, `android_controller.erase_text`) uses **one `adb shell input keyevent
  KEYCODE_DEL` per character** (`android_controller.py:286-294`, default 50). That is ~50 shell round-trips, with
  retries that re-read the text and treat `text == hint` as empty.

### 3.6 Waits and stability
- There are none after actions. A `wait_for_delay` tool exists (`tools/mobile/wait_for_delay.py:46`, capped at
  60 s), and it **calls `time.sleep` inside an async tool**, blocking the event loop.
- Stability is left to the LLM loop (the next contextor pass).

### 3.7 Error handling
- The tool collects `attempts[]` with the selector and error of each strategy and returns them in the ToolMessage
  (`tap.py:37-128`). The prompt teaches: "'Out of bounds' = stale bounds. 'No element found' = screen changed.
  Adapt, don't retry blindly." (`cortex.md:63`). **The attempt log and the guidance are good;** droidctl's typed
  `stale-ref` / `not-found` / `no-change` are the structured version of this.
- `validate_coordinates_bounds` rejects taps outside the screen before sending them (`tools/utils.py:241-260`).

### 3.8 Verdicts (mobile-use)
| technique | verdict |
|---|---|
| Full unpruned JSON hierarchy plus screenshot every step | **skip** |
| u2 `dump_hierarchy(compressed=True)` as the source | **steal** (already the plan) |
| Multi-locator Target (id+idx / text+idx / bounds) with a fallback chain | **adapt**: the fingerprint re-resolution in PLAN.md is this, done right: id+text first, then ancestor path, bounds only as a tiebreak, never "coords first" |
| "Success = shell returned 0" | **skip**; verify by UI change |
| Attempts log in the error message | **steal**: include tried locators in `error.message`/`error.detail` |
| Read back the field value after typing | **steal**: `type` returns `{"value": "<field text now>"}` |
| Tap bottom-right to move the cursor | **skip**; use ACTION_SET_SELECTION / u2 `set_text` which replaces |
| FastInputIME toggled per call | **adapt**: enable once in `init`/per `run` session, restore on exit/`stop`; or use `UiObject.set_text` (ACTION_SET_TEXT) first |
| Per-char DEL via `adb shell` | **skip**; `clear_text()` / SET_TEXT "" / batched keyevents |
| Silently uninstall Maestro | **skip**; detect, then typed error with a hint |
| `.info` probe on every call | **skip**; rely on the exception and reconnect once |
| Blocking `time.sleep` in an async tool | **skip** |

---

## 4. mobile-next/mobile-mcp (+ mobilecli)

### 4.1 Architecture
- A TypeScript MCP server (`src/server.ts`, 1217 lines).
  - Since v1.0 **every device op shells out to `mobilecli`** (Go). mobilecli now keeps a background daemon on a
    unix socket plus an embedded on-device agent (DeviceKit APK) (`CHANGELOG.md:11,33,46`).
  - A "legacy robot" (`MOBILEMCP_LEGACY_ROBOT=1`) is the old pure-adb `src/android.ts`.
  - The Robot is cached per device (`server.ts:235-244`).
- `mobile-device.ts:111-122` spawns `mobilecli` per call (`execFileSync`). Each screenshot also calls
  `device info` to get the screen size (`:126-132`).

### 4.2 UI tree acquisition
- **mobilecli Android** (`devices/android.go:1673-1699`):
  - Flutter (Dart VM service) if the app is a debuggable Flutter app
  - else the on-device agent's node dump (`dumpUiNodes`)
  - else `adb exec-out uiautomator dump /dev/tty`, with **up to 10 retries on "null root node returned by
    UiTestAutomationBridge"** (`:1632-1650`, and the same in `android.ts:484-499`)

  The retry-on-null-root is **worth stealing**: it is the classic transient failure while the UI is changing.
- **Filter** (`android.go:1500-1504`): keep the node if it has text, content-desc, hint or resource-id, or is
  clickable or checkable, **and** `w>0 && h>0`.
  - **No visibility and no off-screen check.** The `Visible` field is never consulted.
  - **No scrollability or editability.**
  - Non-kept nodes are elided and their children hoisted (`:1582-1616`).
- **Legacy `android.ts:342`** was even looser on one axis and stricter on another. Its keep rule was
  `text || content-desc || hint || resource-id || checkable` with **no `clickable` at all**, so unlabeled icon
  buttons were invisible. It also kept pure containers that merely have a resource-id.

### 4.3 What the LLM sees
- The text format (`format-elements.ts:39-88,127-135`) became the default in 1.0.3 (2026-09-08):
  ```
  One element per line: @ref Type text= label= name= value= id= at=x,y size=WxH [focused] [selected] [checked] [disabled]
  @e1 RecyclerView id="com.android.settings:id/recycler_view" at=0,200 size=1080x2100
  @e2 LinearLayout at=0,200 size=1080x180
  @e3 TextView text="HomeNet" id="android:id/title" at=40,220 size=760x60
  @e4 TextView text="Connected" id="android:id/summary" at=40,290 size=760x60
  @e5 ImageButton at=960,400 size=100x100
  ```
  (This was hand-derived from `sample.xml` using mobilecli's rules: pre-order refs, clickable kept.)
  - The format is compact and emits only non-default states. **Good ideas:** the `only emit non-default states,
    otherwise don't confuse llm` rule, and the one-line header describing the schema.
  - Clickable, scrollable and editable are **not shown**. There is no row-text merge (`@e2` is the tappable row,
    but its label lives in `@e3`/`@e4`), and the hierarchy is flattened.
  - `at=` is the **top-left corner**, not the centre.
- **Old JSON format** (0.0.x, `/tmp/research/mcp-server-0.0.30.ts:307-330`):
  `{"type","text","label","identifier","coordinates":{"x","y","width","height"}}`, to be used with
  `mobile_click_on_screen_at_coordinates(x,y)`.

### 4.4 WHY it mis-clicks (evidence)
1. **The model had to compute tap points itself.** Until 1.0.3 (2026-09-08) the only way to click was
   `click(x, y)`. The element list gave `x,y,width,height` where x,y is the **top-left**, and the tool description
   said only "use list_elements_on_screen to find the coordinates" (`0.0.30 server.ts:268-279`). A model that
   passes `x,y` straight through taps the element's **corner**. That is often the padding of the parent or the
   neighbour, especially for small icons and list rows. The current text format still prints `at=x,y` (top-left)
   right next to a coordinate-taking click tool.
2. **The screenshot was scaled to dp but taps were in px.** In 0.0.x, `mobile_take_screenshot` resized Android
   screenshots to `width / (density/160)` (`0.0.30 server.ts:445-456`). On a 1080 px, 420 dpi phone that makes the
   image ~411 px wide. There was **no mapping text**, while `input tap` expects physical px. A model reading
   coordinates off the screenshot therefore tapped at ~38% of the intended x and y, toward the top-left.
   - Issues #29/#163 were closed only by PR #440 in **1.0.4 (2026-09-13)**. That PR now prepends
     "multiply its x by R and y by R" (`coordinate-mapping.ts:18-30`, `server.ts:891-897`). **The LLM still does
     the multiplication.**
   - The default is now `maxSize=1024` JPEG (`server.ts:29, 862-869`).
   - There is also the provider-side downscale: Claude and others downscale large images, which adds a second
     scale factor the tool can't see.
3. **Positional refs are re-resolved to a different element.** Since 1.0.3, `click(ref="@e5")` makes mobilecli
   re-dump and number the tree in DFS pre-order (`types/screen.go:33-44`), then tap the **centre of whatever is 5th
   now** (`commands/input.go:166-180`). The comment is explicit: *"Refs are positional against a fresh dump; there
   is no staleness tracking."*
   - After any content change (list loaded, toast, keyboard, dialog, lazy row), `@e5` silently means another
     element. The tool returns "Clicked on element @e5" with no identity check.
   - `mobile_batch_commands` makes this worse. It runs click/type/click back-to-back with **no settle wait and no
     re-list between steps** (`server.ts:1162-1215`), so refs from before step 1 are resolved after step 1 changed
     the screen.
4. **Unlabeled but clickable targets.** In the legacy robot they were dropped. In mobilecli they are listed as
   bare `ImageButton at=… size=…`, so the model falls back to screenshots and coordinates for exactly the icon
   buttons that most need precision.
5. **No off-screen/visibility filter.** Elements scrolled out of view, or hidden behind a sheet, are listed with
   real coordinates. Tapping them hits whatever is on top.
6. **Rotation and override size.** Legacy `getScreenSize` uses `wm size`, whose last token is the override size if
   one is set. That is fine for input, but it ignores rotation for swipe maths (`android.ts:104-122,176-208`).
7. **Nothing verifies the outcome.** Every tool returns "Clicked on screen at coordinates: x, y" regardless of
   effect.

### 4.5 Text input
- Legacy (`android.ts:420-445`): ASCII goes through `adb shell input text` with shell-escaping. Non-ASCII needs the
  **DeviceKit** APK: it base64s the text into a clipboard broadcast, sends `KEYCODE_PASTE`, then clears the
  clipboard. Otherwise it gives an actionable error.
- mobilecli (`devices/android.go:956-990`): `\b` and `\n` map to keys; ASCII goes to the agent's `device.io.text`
  (KeyCharacterMap); anything else uses clipboard set + paste + clear.
- The clipboard-paste trick is **adapt**-worthy as the last Unicode fallback when neither SET_TEXT nor FastInputIME
  works (e.g. in secure fields, where SET_TEXT is refused). It clobbers the user's clipboard, so save and restore it.

### 4.6 Waits, errors, and other good bits
- There are no waits (only a 100 ms delay in doubleTap). `ActionableError` messages end in "Please fix the issue and
  try again." Anything else is logged, and the tool returns a text error.
- **Good design bits to keep:**
  - Server instructions (`server.ts:78-87`): "read the screen with list_elements, not screenshots … batch known
    sequences … prefer open_url/launch_app over navigating". That matches chromectl's AGENTS.md style.
  - `listElementsAtEnd` on batch: return a fresh snapshot after a batch. droidctl `run --json` could append the
    final snapshot (or diff) automatically.
  - Launch uses `monkey -p pkg -c LAUNCHER 1` (no activity resolution needed) with optional per-app locale
    `cmd locale set-app-locales` (`android.ts:148-165`).
  - `open_url` refuses non-http(s) schemes unless an env flag is set (`server.ts:750-752`).
  - The Flutter semantics fallback via the Dart VM service is a niche but real gap in a11y dumps (v2+ idea).

### 4.7 Verdicts (mobile-mcp)
| technique | verdict |
|---|---|
| Positional `@eN` refs re-resolved by index in a fresh dump | **skip**: the canonical anti-pattern; droidctl fingerprints plus `stale-ref` |
| Coordinates-first click tool, top-left `at=` | **skip**; if bounds are ever printed, print the centre or `[x1,y1,x2,y2]`, and never ask the model for px |
| Screenshot scale plus "multiply by R" text | **skip**; screenshots are for humans/visual checks only; if a coordinate tap is ever needed, accept coords in **screenshot space** with the image size and convert in code |
| One-line text format, non-default-states-only, schema header | **steal** |
| Keep rule: labelled OR clickable/checkable, hoist children of dropped nodes | **adapt**: add scrollable/editable/long-clickable, a visibility check, and the row-merge |
| uiautomator "null root node" retry loop (10×) | **steal** (u2 has its own retries, but guard `dump_hierarchy` for empty/`null root` too) |
| Batch with no settle between steps | **skip**; droidctl `run` steps must settle (wait-for-idle) between actions |
| `listElementsAtEnd` | **steal** as `run --snapshot-at-end` / auto-diff |
| Clipboard-paste Unicode fallback | **adapt** (save and restore the clipboard) |
| DeviceKit/agent APK instead of `adb shell input` (JVM fork per call, ~300-700 ms) | **agree**; v1 u2 already avoids `input` forks; v2 own APK |
| `monkey -p … LAUNCHER 1` launch | **steal** (fallback to u2 `app_start`) |

---

## 5. Cross-cutting conclusions for droidctl

### 5.1 None of the three do element-based actions
All three end in **coordinates**: droidrun uses stale snapshot centres, mobile-use uses LLM-computed centres, and
mobile-mcp uses positional-ref centres. Only droidrun-portal's internal auto-accept code calls `ACTION_CLICK`.
droidctl's "re-resolve by fingerprint, then `UiObject.click()` (v1) / `performAction(ACTION_CLICK)` (v2), then
verify" is genuinely differentiated. Keep the coordinate path only as the fallback for non-clickable targets, and
use droidrun's tap-blocker geometry there.

### 5.2 Snapshot format: borrow and avoid
- **Borrow:**
  - mobile-mcp's one-line-per-element format with only non-default states
  - droidrun's keyboard-node exclusion and 10%-visibility-with-parent-preservation
  - Portal's multi-window roots (dialogs/popups)
  - Portal's `stateDescription` / `hint` / `error` fields (`error` = form validation text; worth showing)
  - `collectionInfo` for "(N items)"
- **Avoid:**
  - listing all nodes (droidrun)
  - dumping all attributes as JSON (mobile-use)
  - duplicated id-as-text fallbacks
  - top-left `at=` coordinates
  - no clickable flag
- Nobody merges row text into the clickable row. That is PLAN.md's step 3, and it's the biggest token and accuracy
  win.

### 5.3 "Did the UI change" verification
- Nobody does it. droidrun's macro Jaccard over `(id, class, text, desc, clickable, enabled, focused)` keys with a
  0.85 threshold is a ready-made similarity metric.
- Settle strategy:
  - v1: u2 `d.wait_idle()`-style polling / compare successive compressed dumps until two consecutive fingerprints
    match (cap ~1.5 s)
  - v2: a11y event quiet period (`notificationTimeout` 100 ms, as Portal uses)
- Report `changed: true/false, similarity: 0.xx, new_window/package/activity`.

### 5.4 Text input ladder (v1)
1. `UiObject.set_text()` = ACTION_SET_TEXT on the re-resolved node (Unicode, no IME switch). Treat `text==hint` as
   empty, as droidrun, Portal and mobile-use all do.
2. Otherwise use FastInputIME `send_keys`, enabled once per session and restored at the end (not per call).
3. Otherwise, for non-ASCII, clipboard paste with save/restore.
4. Otherwise, for ASCII, `input text` in chunks of 200 with `%s` spaces.

After typing, read the field back and return `value`. Never report plain success. Mirror Portal's typed outcomes
(`verified`, `unverified`, `rejected`).

### 5.5 Latency
- Keep one persistent u2/HTTP session (no per-call client like droidrun's httpx, no per-call `.info` probe or
  screenshots like mobile-use, no process spawn per op like mobile-mcp).
- Enable the IME once.
- Batch keyevents in one `input keyevent --delay 0 …`.

### 5.6 Safety pitfalls seen
- Overwriting `enabled_accessibility_services` (droidrun)
- Silently uninstalling Maestro (mobile-use)
- Replacing the user's keyboard (droidrun runs `ime set` on every connect; `disable_keyboard()` exists at core-local `portal.py:559` but nothing in mobilerun or core-local calls it)
- Clobbering the clipboard (mobile-mcp)

droidctl should detect and restore in each case, and surface typed errors.
