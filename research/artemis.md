# Google Artemis: analysis for droidctl

Source: `/tmp/artemis` (shallow clone of github.com/google/artemis, Apache-2.0). All paths below are relative to that root, and line numbers refer to that checkout.
I read the code itself, not just the names. I executed `ui_filter.py` against a synthetic dump to confirm one finding (§A0). Nothing outside `/tmp/research` was modified.

---

## A0. The real observation pipeline (read this first)

```
u2 d.dump_hierarchy(compressed=True)                      clients/ui_automator_client.py:386,439
  -> _parse_hierarchy_xml_to_elements(xml)  FLAT list     clients/ui_automator_client.py:80-160
  -> filter_ui_hierarchy(flat, w, h)                      drivers/android/adb_driver.py:203
  -> fuse_ocr_with_xml(flat, ocr_results)                 mcp/observation.py:90, graph/perception.py:196
  -> format_minimal_list_with_elements(...)  -> text      utils/visualization.py:334-440
```

**Key finding: the tree is flattened before it is filtered.** `_parse_hierarchy_xml_to_elements` appends every node as a dict *without* a `children` key (`ui_automator_client.py:108-158`). The helper-APK path does the same: `normalize_helper_elements` explicitly `pop("children")` (`accessibility_client.py:146`), and XML goes through the same flattener (`:354`).

This has three consequences for `ui_filter.py` in production:
- `_merge_complementary_children` (Strategy 2) and `_prune_parent_child_redundancy` (Strategy 3) **never run**. They only fire `if "children" in node` (`ui_filter.py:398-420`).
- Clipping only happens against the screen rect, because the ancestor rect is always the screen (`:367-373`).
- `semantic_pruning="hoist"` on a leaf just means "drop the node" (`:430-432`).

I confirmed this by running the module on a clickable row that contains `[ImageView][TextView "Wi-Fi"]`:
```
FLAT:   [('LinearLayout',''), ('ImageView',''), ('TextView','Wi-Fi')]   # nothing merged
NESTED: LinearLayout '' -> TextView 'Wi-Fi' desc='[Icon]' bounds=[20,320][900,430]  # merged
```
The unit tests (`tests/unit/utils/test_ui_filter.py`) feed nested dicts, so they exercise code paths that production never reaches.
**Lesson for droidctl:** do the merging on the *tree*, and put a test on the real ingestion path.

---

## A1. `artemis/utils/ui_filter.py`: every strategy

| # | Strategy | Where | Logic / numbers | Verdict |
|---|---|---|---|---|
| 1 | Bounds parsing | `:23-67` | Accepts `"[l,t][r,b]"` (negatives allowed), `{left..}`, `{x,y,w,h}`, and a cached `parsed_bounds`. The parse is cached on the node (`:55-59`). | **Adapt.** We only need the string form. Allow negatives. |
| 2 | Clip to ancestor | `_clip_bounds :86-134` | `max/min` intersection with the ancestor's (already clipped) bounds. If the result is empty, the node is **clamped to a point** on the ancestor's edge and marked `is_clipped=True`, so it is kept ("zero info loss", `:378-379`). The cached `parsed_bounds` is updated too (`:115-118`). | **Adapt.** Intersect with ancestors and scrollables. Fully clipped nodes are *offscreen*: count them for the `more ↓` hint, do not list them. |
| 3 | Min size | `:79-83, :384-393` | `w > min && h > min`. `min = max(5, int(0.005*short_side))`, which is 5px on a 1080-wide screen. Fully clipped nodes are exempt. | **Port** (a cheap noise filter). |
| 4 | Semantic-empty | `_is_semantic_empty :137-168` | A node is kept if it has text, content-desc, or clickable/focusable, or if its class contains `image`/`icon`/`photo`. Everything else is empty. | **Adapt.** Our keep-rule is wider (checkable, long-clickable, scrollable, editable). The *image-class keep* is a good addition, but only when the image is clickable or labelled. |
| 5 | Hoist vs safe | `:424-434` | `hoist` (the default): an empty node is replaced by its kept children, which flattens layout wrappers. `safe`: only empty leaves are dropped. | **Port `hoist`.** It is exactly the plan's "collapse single-child layouts", generalised. |
| 6 | Parent-child redundancy (S3) | `:284-353` | If a child's text or desc equals the parent's text or desc: a form control, a child with children, or an interactive child is kept but its text is blanked; any other child is removed. | **Port** (on the tree). It kills the "Sign in" label duplicated inside a "Sign in" button. |
| 7 | Complementary-children merge (S2) | `:171-281` | Only under a clickable or focusable parent. Every child must be exactly one textual child plus ≥1 non-interactive, non-text "decoration" child. It aborts if any child is a checkbox/switch/radio/edittext or an interactive textless child. The result is **one** node: union bounds, `text=" ".join(texts)`, `content-desc="[desc] [Icon]"` (a textless `image` class contributes `[Icon]`). | **Adapt.** The idea (icon plus label equals one row) is right, but their rule is narrow: exactly one text child. Our plan (an actionable node absorbs *all* descendant text, joined with ` · `) generalises it. Take the `[Icon]` marker for unlabeled images. |
| 8 | Fixed system-bar detection | `_detect_fixed_system_bars :437-511` | The candidate must be anchored at `top<=0` or `bottom>=H`, have width ≥ **95%** of the screen, and be non-scrollable with no scrollable ancestor or descendant. A bottom bar is accepted if its id or class contains `bottom_navigation / navigation_bar / bottom_bar / tab_layout / action_bar`, or its package is `com.android.systemui`, or **25 ≤ h ≤ 15%·H**. A top bar is accepted if it is systemui or **15 ≤ h ≤ 12%·H**. The scan does not recurse into a confirmed bar. | **Adapt.** Use it for the scroll hints and to mark "behind toolbar". |
| 9 | Clamp by bars | `_clamp_by_fixed_bars :514-573` | Each surviving element that overlaps a bar horizontally has its vertical span cut at the bar's edge. It is discarded if the remaining height is `<= max(min_size, 20)` px. Elements equal to the bar are kept. | **Adapt.** A list row half under the bottom nav otherwise gets a centre point that lands *on the nav bar*. This matters for tap accuracy, and droidctl's re-resolve-then-tap should clamp the same way. |

Unit tests (`tests/unit/utils/test_ui_filter.py`) cover: semantic_empty `:23`, clip `:43/:57`, hoisting `:71`, dynamic min size `:99`, ancestor propagation `:112`, sibling merge plus its negative cases `:127-:186`, parent-child redundancy `:213`, fixed bars `:298`, occlusion warning `:336`. `tests/unit/test_hierarchy_fidelity.py:49` covers negative bounds and `:70` covers hint/error display. **Port** the test *cases* as fixtures for `test_snapshot.py`.

---

## A2. Formatting for the LLM: `utils/visualization.py:334-440`

```python
text = node.get("text") or node.get("content-desc") or ""
hint = node.get("hint"); error = node.get("error")
shown = text.strip() or hint
...
line = f"[{idx}] {kind}: '{shown}' | Bounds: {norm_bounds}"     # kind = Text | Hint
if error: line += f" | Error: '{error}'"
```
- **Index:** a 1-based running counter over the *flat, filtered* list, in document order (`:344`).
- **What is shown:** only the label (text, else content-desc, else hint) and the **bounds normalised to 0-1000** (`:348-353`). **No class/role, no resource-id, no clickable/checked/selected/enabled flags, no scrollable containers.** OCR lines are `[N] OCR Text: '...' | Bounds: ...` (`:382`).
- **Only labelled nodes get an index.** A clickable ImageButton without a desc is *not listed*. The agent has to use the screenshot plus `[x,y]` with a mandatory `target_description` (`mcp/action_executor.py:413-422, 494-503`).
- **Dedup:** the same text within **8 px** of centre distance is dropped (`:355-362`).
- **Occlusion warning** (`_inject_mutual_occlusion_warnings :173-255`): for every pair (O(n²)) with intersection ≥ **50%** of either area, a warning is appended unless the pair is "concentric nesting". Concentric nesting means fully contained, the container area is more than **2×**, and the centre distance is below **20%** of the max dimension. The suffix is `(WARNING: may overlap with [a] and [b] (+k more), possible occlusion)`. This catches FABs over list rows.
- **Lists/scrollables:** there is no container notion and no "more ↓". Scrolling is blind: `swipe up/down` plus re-observe.
- **Token size** (my estimate from the format): one line such as `[12] Text: 'Network & internet' | Bounds: [37,301][962,346]` is about 18-22 tokens. A typical 30-60-element screen is about 600-1300 tokens, and roughly 40% of that is bounds. The list travels *together with a screenshot*, so it is a supplement, not the sole representation.

**Verdict:**
- **Skip** their line format: no roles or flags, bounds in every line, unlabeled controls dropped.
- **Port** two pieces: the `Hint:` vs `Text:` distinction plus `Error:` (an empty input shows its hint, and a rejected one shows its error), and the **occlusion warning** as an `(overlaps [n])` flag.
- **Port** the 8px same-text dedup.

---

## A3. Index → action

`mcp/action_executor.py:_resolve_index :435-485`:
- The index is looked up in `state.indexed_elements` (the list from the *last* observation, set at `:202-204`) as `elements[index-1]`.
- `center` is the centre of the element's bounds, normalised to 0-1000. It is `(l+r)//2`, computed at format time (`visualization.py:409`).
- It records `target_text / target_bounds / target_resource_id / target_class` for the safety net.
- The actuator converts back to pixels: `x = clamp(nx*W/1000, 0, W-1)` (`mcp/actuators/adb.py:127-131`).
- The driver then runs `adb shell input tap x y`, or `input swipe x y x y dur` for durations ≥500 ms (`drivers/android/adb_driver.py:229-252`).
- `drivers/base.py:find_element` (`:223-281`) does substring text or id match with an index, and always re-derives the centre from the *live* bounds rather than a cached centre. Its comment explains why: the centre "can lag one layout transition behind".

Implications:
- The tap point is the centre of the **labelled node**: the TextView inside the row, not the clickable ancestor. That usually works because Android dispatches the touch to the clickable parent.
- The **normalise-to-1000 round trip loses precision:** about ±1.2 px on 1080 and ±2.4 px on 2400.

**Verdict:**
- **Skip** index→stored-centre. That is precisely what droidctl's re-resolve-at-action-time replaces.
- **Port** "always use live bounds, never a cached centre".
- **Skip** 0-1000 normalisation: use raw pixels internally.

---

## A4. Pre-action target verification ("Safety Net", XML-first)

Gate: `agents/validator/execution_loop.py:_run_precondition_gate :78-127`. It runs the XML check when the action has index metadata (text, bounds or resource-id, and not OCR or explorer). If that is bypassed or fails, it falls back to a VLM pixel check.

`agents/validator/precondition_xml.py`:
- It only validates `tap, long_press_on, focus_and_input_text, focus_and_clear_text` (`:41-46`).
- **Retry:** 3 attempts, 0.4 s apart (`:64-65`).
- **Scale factor:** `sqrt(W²+H²) / sqrt(1080²+2400²)` (≈2631.8), used to make every pixel threshold resolution-independent (`:607-610`).
- **Text normalisation** (`:159-166`): lowercase, strip `(\d+\+?)` and `[\d+\+?]` badges ("Inbox (3)" → "inbox"), strip punctuation.
- **Aggregate text** includes descendants (`:169-179`), which makes it container-aware.
- **Text similarity** (`:182-190`): exact 1.0, substring 0.8, otherwise `difflib.SequenceMatcher.ratio()`.
- **Bounds** (`:193-220`): IoU. There is a **size-mismatch flag** when the width or height ratio is above **2.5**. When that flag is set and the id does not match, the text, bounds and coordinate signals are all zeroed (`:323-326`). This stops a small widget from matching its giant container.
- **Weights** (`:49-52`): `W_ID=0.5, W_TEXT=0.4, W_BOUNDS=0.3, W_COORD=0.3` (containment of the original tap point). A present id gives 1.0; a missing id gives a soft **+0.3**; a mismatched id gives a hard **−0.5** (`:243-255`). The score is `Σsignals/Σweights`. The identity score uses only id and text.
- **Distance decay:** `score *= max(0.5, 1 - dist/800)` (`:342-343`).
- **Pass threshold:** **0.55** if `dist ≤ 150·scale`, else **0.75** (`:657`).
- **Self-heal:** if the element passes and moved by ≤ **200·scale** px, its coordinates are replaced with the new centre. Beyond that it still passes, but the coordinates are left alone (`:359-386`).
- **Failure taxonomy** (`_classify_failure :530-584`), fed back to the agent as an "incident":
  - `TARGET_SHIFTED`: `dist ≤ 100·scale` (or **300·scale** with an id match) *and* identity ≥ **0.85**, or ≥ **0.5** with an id match. The response includes `new_center/new_bounds`.
  - `TARGET_OCCUPIED`: the smallest element containing the point has content or is interactive, and is not full-screen (within 10 px of W and H) and not more than **3×** the target area. The response includes an occupant description.
  - `TARGET_DISAPPEARED`: everything else.
- The pixel fallback (`precondition_pixel.py`, `pixel_safety_net.md`) asks a VLM to compare crops around a red dot (radius 15) and return `{"is_present", "confidence"}`.

**Verdict: Adapt, and this is the most valuable piece.** droidctl's ref resolution *is* this matcher, with the difference that we resolve by fingerprint first. Steal:
1. The weighted multi-signal score, with the id hard-penalty, the 2.5× size guard and distance decay.
2. Badge-stripping text normalisation.
3. The three-way failure classification. Map it to error kinds: `stale-ref` gets sub-reasons `shifted` (auto-heal plus report), `occupied` ("covered by [n] 'Allow'", which is great for dialogs) and `gone`.
4. The resolution-independent scale factor.

**Skip** the VLM fallback.

---

## A5. Keyboard / IME / text entry

- **Focus before typing** (`mcp/actuators/adb.py:45-98`): find the *smallest* element containing the point that is focusable, clickable or EditText. If it is `focused=="true"`, skip the tap. Otherwise tap, then **sleep 1.0 s** "for the keyboard".
- **Clear:**
  - `input keyevent 123` (MOVE_END), then `--meta 1 122` (Shift+MOVE_HOME select), then `67` (DEL), then 20×`67` (`adb_driver.py:309-317`).
  - The controller variant uses `input keycombination 113 29` (Ctrl+A), then DEL, then 20×DEL, falling back to 30×DEL (`controllers/unified_controller.py:239-255`).
  - Append mode sends only MOVE_END.
- **Typing tiers** (`adb_driver.py:307-370`):
  1. Clipboard (u2 `set_clipboard` or the helper's `clipboard` command) then `input keyevent 279` (PASTE). This handles unicode and multiline without touching the IME.
  2. If `settings get secure default_input_method` contains `adbkeyboard`: `am broadcast -a ADB_INPUT_B64 --es msg <b64>`.
  3. `input text` with shell escaping and space→`%s`. Newlines become `keyevent 66`.
  - `drivers/android/input_ime.py:54-120` is an alternative order: an ASCII fast path first, then u2 `send_text` (FastInputIME on, `sleep 0.3`, `send_keys`, `sleep 0.5`, off; `ui_automator_client.py:327-347`), then ADBKeyboard, then `input text`.
  - Literal `\n` sequences from the LLM are normalised (`adb_driver.py:326`).
- **No keyboard-visible detection** anywhere in the Python. The helper's XML does include `window-type="input_method"` roots, which would provide it.
- **Verdict:**
  - **Port** clipboard+PASTE as the unicode/multiline fallback after u2 `set_text`.
  - **Port** the clear sequence as a fallback when `set_text("")` fails.
  - **Port** "skip the focus tap if already focused".
  - **Skip** the fixed 1 s sleep. Poll for `focused=true` or the IME window instead.
  - **Add** keyboard detection (the IME window in the helper dump; for u2, `dumpsys input_method | grep mInputShown`).

---

## A6. Waiting / stability / "did it change"

- **What actually runs:** fixed sleeps only.
  - `get_screen_data` sleeps **0.3 s** (`adb_driver.py:123-124`).
  - `observe()` settles **400 ms** (`mcp/observation.py:43`, called with 400 at `action_executor.py:198`).
  - Perception sleeps **0.4 s** unless the last action was non-UI (`graph/perception.py:38-60, 152-162`).
  - After an action there is **no effect polling**, by design: "dispatched only says the device accepted the command" (`execution_loop.py:143-144`). The old poll path was deleted as dead code (`validator.py:44-49`).
- **Present but unused in the main path:** `utils/image_diff.py`.
  - `wait_for_screen_stability` (`:138-235`): max **1.5 s**, poll every **0.2 s**, stable when the changed-pixel ratio is below **0.1%**.
  - `check_ui_change` (`:28-135`): the global threshold is **0.0001**. There is also an ROI check of **±50·scale px (min 15)** around the tap; a change of more than **5%** of the ROI pixels counts as changed.
  - Both mask the top **10%** and bottom **5%** (status and nav bars), and both apply GaussianBlur 5×5 with binary threshold **25** (ROI: 3×3, threshold **20**).
- **Loop detection for the LLM:**
  - A pixel-exact compare against the last **3** steps (≤**3** differing pixels at colour tolerance **8**, after symmetric JPEG q75 re-encode) produces "screen unchanged since step N" (`agents/operator/prompts.py:871-958`).
  - A 64-bit **dHash** Hamming distance ≤ **5** (calibrated: same screen ≤4, different ≥7) against older steps produces "returned to earlier state" (`prompts.py:975-1063`, `config/agent.py:565-577`, `utils/image_hash.py`).
- **Verdict:**
  - **Skip** pixel diffs for the core. Our XML-hash poll is cheaper and semantic.
  - **Port** the ROI idea into the diff: report whether the change was *near the tapped element* or elsewhere.
  - **Port** the status-bar masking. Exclude `com.android.systemui` nodes and the clock from the change hash, or you will get false `changed:true` every minute.
  - **Adapt** loop detection as an optional `snapshot` header note ("same as 2 actions ago"), using the hash of the pruned snapshot.

---

## A7. OCR / visual fallback

- **Trigger:** there is none based on gaps. OCR (Google Vision) runs on **every** perception turn whenever an API key is configured (`graph/perception.py:173-194`). It is also exposed as an on-demand tool (`tools/mobile/ocr.py`).
- **Status bar handling:** the status bar is cropped first. Its height is the bottom of a systemui node at top 0, else **4%** of H (`ocr_xml_fusion.py:90-102`).
- **Fusion** (`ocr_xml_fusion.py:145-318`):
  - Each OCR box is assigned to the **smallest** XML node covering ≥**90%** of the OCR box, else ≥**70%**.
  - Nodes larger than **10%** of the screen are excluded unless the class contains text/button/edit/search/input.
  - Boxes are grouped into lines within **10 px** of vertical centre, and merged horizontally when the gap is below **30 px**.
  - A group is discarded if it is a substring of the XML text or if difflib > **0.8**.
- **Low-value text:** single non-digit/non-CJK characters, the placeholders `search / enter text / placeholder`, and `[Icon]`, `[Image]`, `[Picture]` (`:29-55`).
- **Hit-test:** XML elements are preferred over OCR elements. Within each group the smallest covering box wins (`utils/element_hit_test.py:45-80`).
- **Verdict: Skip for v1.** It is only relevant to canvas, Flutter or game screens. Note it for a later `--ocr` option. The one reusable rule is the fusion one ("smallest node covering ≥90% of the box, giant containers excluded"), which is also a good hit-test rule for `shot --marks`.

---

## A8. Misc device heuristics worth copying

- **Swipes** (`adb_driver.py:274-305`): x is fixed at **60% of width** to avoid edge-back gestures and alphabet fast-scroll bars. Vertical swipes go **0.7H→0.3H** over **800 ms**, which avoids a fling and keeps about 50-60% overlap. Horizontal swipes go 0.75W↔0.25W. **Port.**
- **Maestro conflict:** `dev.mobile.maestro` breaks `u2.connect`, so Artemis uninstalls it (`ui_automator_client.py:236-247`). **Port as a diagnostic**; do not auto-uninstall.
- **u2 connect:** 3 retries with 1 s·attempt backoff; liveness is checked through `d.info` (`:276-312`). **Port.**
- **Screenshots:** `adb exec-out screencap -p` is preferred over u2 because it is more reliable with popups; JPEG q80 (`:397-422`). **Port.**
- **Awake policy:** `svc power stayon usb` plus `input keyevent KEYCODE_WAKEUP` (`runtime/awake_service.py:105-107`). **Port** into `init`.
- **Current package:** adbutils `current_app`, falling back to `dumpsys window displays | grep -E 'mCurrentFocus|mFocusedApp'` (`adb_driver.py:402-427`). **Port.**
- **Launch:** `monkey -p PKG -c android.intent.category.LAUNCHER 1` (`:385-392`). **Port.**

---

## B. The helper APK: `packages/artemis-accessibility-helper`

### B1. Build
- There is a Gradle project (`build.gradle.kts`: AGP 8.5.0; `app/build.gradle.kts`: `compileSdk 35`, `minSdk 24`, `targetSdk 35`, `versionCode 6`, `versionName 1.2.0`, Java 8, **zero dependencies**, APK under 30 KB).
- There is also a no-Gradle script, `build_apk.sh`: `aapt2 compile/link` → `javac -source 8` → `d8 --min-api 24` → a Python zip-append of `classes.dex` → `zipalign -p 4` → `apksigner` with a **committed `debug.keystore`**. The keystore gives a stable signature, so `install -r` upgrades work.
- The script writes `helper_manifest.json` `{version_code, version_name, sha256, built_at}`. The host reads it to decide install versus upgrade (`runtime/helper_manager.py:183-199, 693-767`). The prebuilt APK is committed.
- **Verdict: port the build script approach as-is.** No Gradle daemon, a reproducible tiny APK, and a manifest-driven upgrade.

### B2. Manifest and service config
- `AndroidManifest.xml`: permissions `INTERNET`, `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_SPECIAL_USE`, `POST_NOTIFICATIONS`. The service `.ArtemisAccessibilityService` has `BIND_ACCESSIBILITY_SERVICE` and `foregroundServiceType="specialUse"`. The receiver `.TokenReceiver` is `exported` and **guarded by `android.permission.WRITE_SECURE_SETTINGS`**.
- `res/xml/accessibility_service_config.xml` sets:
  - `accessibilityEventTypes="typeWindowStateChanged"`
  - `notificationTimeout=100`
  - `flagDefault|flagRetrieveInteractiveWindows|flagReportViewIds`
  - `canRetrieveWindowContent`, `canPerformGestures`, `canTakeScreenshot` all true
- `onServiceConnected` re-applies these flags at runtime and **clears `FLAG_INCLUDE_NOT_IMPORTANT_VIEWS`**, so the tree matches u2's `compressed=True` (`ArtemisAccessibilityService.java:92-109`). It starts a foreground notification on channel `artemis_helper_quiet` at IMPORTANCE_MIN, id 18888, to survive OEM killers (`:128-185`). `onStartCommand` returns START_STICKY.
- The only event handled is `TYPE_WINDOW_STATE_CHANGED`, which records the current package and activity (`:198-207`).

### B3. Install and enable (host side: `runtime/helper_manager.py`)
```
adb -s S install -r -g ArtemisAccessibilityHelper.apk                            (:755)
settings get secure enabled_accessibility_services                              (:600)
settings put secure enabled_accessibility_services <existing>:com.artemis.helper/.ArtemisAccessibilityService   (:612-620)
settings put secure accessibility_enabled 1                                      (:621)
```
- **Enable:** the services list is colon-joined, and existing services are preserved. The write is re-read after **0.4 s**, with up to **5** attempts, because AccessibilityManager may prune a not-yet-resolved component right after install (`:590-629`).
- **Revive** a dead-but-enabled service (force-stopped or ROM-killed): remove it from the list, wait **0.3 s**, re-add it (`:631-664`).
- **If the settings write is rejected** (some OEMs): `am start -a android.settings.ACCESSIBILITY_SETTINGS`, and tell the user the manual path (`:127-131, :768-773`).
- **Version:** `dumpsys package com.artemis.helper | versionCode=` (`:394-404`).
- **Uninstall:** remove it from the setting, then `adb uninstall` (`:789-807`).
- **Concurrency:** a file mutex serialises installs across processes (timeout 90 s, stale after 180 s) (`:546-588`).

### B4. Auth token
- **Host:** one `secrets.token_hex(24)` (48 hex characters) per host, stored in a file with mode `0600` (`:308-339`).
- **Push:**
  ```
  adb shell am broadcast -n com.artemis.helper/.TokenReceiver -a com.artemis.helper.SET_TOKEN --es token <hex>
  ```
  Success is detected by `"Broadcast completed"` (`:341-368`). Only a sender that holds WRITE_SECURE_SETTINGS (the adb shell user) passes the receiver guard.
- **Device side:** `TokenStore` keeps the token **in memory only**, so a rebind loses it and the helper answers 401 until the host re-pushes. Comparison is constant-time with `MessageDigest.isEqual` (`TokenStore.java`).
- **The host pushes on every attach and on any 401**, then retries once (`accessibility_client.py:238-250`).
- The token is accepted as the `X-Artemis-Token` header, the `?token=` query parameter, or a JSON `token` field (RPC).
- **Verdict: port as-is.** It is the right threat model: loopback is reachable by every app on the phone.

### B5. Transport: `CommandServer.java`
- **Binding:** `ServerSocket` bound to **127.0.0.1:18888**, backlog 50, a cached thread pool, and a 10 s socket timeout (`:59-87, :130-133`). The host reaches it with `adb forward --no-rebind tcp:0 tcp:18888`: adb picks the host port, and an existing forward for the same serial is reused (`helper_manager.py:813-829, 413-426`). The session is invalidated when `adb devices -l`'s `transport_id` changes, i.e. on replug (`:866-875`).
- **Two protocols on one port, sniffed from the first line** (`:136-149`):
  - **HTTP/1.1**, one request per connection with `Connection: close`. It has a hand-written parser that is byte-accurate for Content-Length (`:93-128, 176-256`). Header lines are capped at 16 KB and bodies at 4 MB (413 above that).
  - **Line-delimited JSON-RPC:** `{"cmd":..., "token":..., ...}\n` answered by `{json}\n` (`:262-287`). The host client does not use it.
- **Endpoints:**

| Endpoint | Auth | Returns |
|---|---|---|
| `GET /ping` or `/` | no | `{success, service, version_code, version_name, protocol_version:2, port, auth_required:true, token_set, authenticated}` plus `package`/`activity` if authed (`:384-400`) |
| `GET /snapshot?fields=xml[,elements,tree]&include_invisible=1` | yes | Atomic screenshot plus dump (below) |
| `GET /dump_xml`, `/hierarchy.xml`, `/dump?format=xml` | yes | Raw UIAutomator-compatible XML (`application/xml`) |
| `GET /dump`, `/hierarchy` | yes | JSON `{xml, elements[], tree, rotation, width, height, node_count, skipped_invisible, truncated, window_count, elapsed_ms, package, activity}` |
| `POST /action` or `/rpc` with body `{"cmd":..., ...}` | yes | `{success:bool, error?}` |

- **`/action` commands** (`:293-376`):
  - `tap {x,y,timeout=1500}`
  - `double_tap {x,y,timeout=2000}`
  - `long_press {x,y,duration=1000}`
  - `swipe {x1,y1,x2,y2,duration=300}`
  - `type {text, append=false}`
  - `clear`
  - `clipboard {text}`
  - `global {action: back|home|recents|notifications|quick_settings|power_dialog|toggle_split_screen|lock_screen|take_screenshot}`
  - `dump | dump_ui | snapshot | dump_xml | ping`
- The version contract is `PROTOCOL_VERSION=2`; the host requires `MIN_PROTOCOL_VERSION=2` and force-reinstalls otherwise (`helper_manager.py:926-950`).

### B6. `HierarchyDumper.java` and `A11yNode.java`: output format and attributes read
- **Root discovery** (`:466-582`), in three tiers:
  1. `getWindows()` sorted by **layer, descending**. Every window's root is kept: app, dialog, system, IME, overlay, split divider. Roots are deduplicated by hashCode.
  2. If there is no APPLICATION window: `getRootInActiveWindow()`.
  3. If still empty: `findFocus(INPUT)`, then `findFocus(ACCESSIBILITY)`, walking up to the root.
  - The whole discovery retries with backoff `40, 80, 120, 160, 220, 300 ms` while it is empty (`:399-409`).
- **Node walk** (`snapshotNode :613-765`):
  - Limits are `MAX_DEPTH=75` and `MAX_NODES=8000`, reported as `truncated`.
  - A child with **`isVisibleToUser()==false` is skipped** (roots are always kept) unless `include_invisible=1`. Skipped nodes are counted in `skipped_invisible`.
  - **Bounds** are `getBoundsInScreen ∩ clip`. The clip starts as display ∩ window bounds and is **narrowed to every scrollable ancestor's bounds**. This mirrors UIAutomator's `getVisibleBoundsInScreen`/trimScrollableParent, so bounds are never negative or offscreen. An empty intersection gives empty bounds.
  - On API 33+ it uses `getChild(i, FLAG_PREFETCH_DESCENDANTS_HYBRID = 1<<3)` and `window.getRoot(same)` to batch Binder IPC (`:442-458`).
  - `recycle()` is called only below API 30.
- **Attributes read:** text, contentDescription, packageName, className, viewIdResourceName, clickable, checkable, checked, enabled, focusable, focused, scrollable, longClickable, password, selected, visibleToUser, `isEditable`, `getError`, `getDrawingOrder`, and further fields by API level:
  - API 26+: `getHintText`. If `isShowingHintText()`, **text is blanked** so the hint does not masquerade as a value.
  - API 28+: `isHeading`, `isScreenReaderFocusable`, `getPaneTitle`, `getTooltipText`.
  - API 30+: `getStateDescription` (Compose expanded/collapsed/"On").
- **XML** (`A11yNode.writeXml :85-157`) is standard UIAutomator `<hierarchy rotation=".."><node index text resource-id class package content-desc checkable checked clickable enabled focusable focused scrollable long-clickable password selected visible-to-user bounds drawing-order .../></hierarchy>`.
  - Optional extras are `hint`, `editable`, `heading`, `screen-reader-focusable`, `state-description`, `error`, `pane-title`, `tooltip`.
  - Window roots also carry `window-id window-type(application|input_method|system|accessibility_overlay|split_screen_divider) window-layer window-active window-focused`.
  - `XmlUtils` escapes `& < > " '` and tab/LF/CR, and drops XML-1.0-illegal characters and unpaired surrogates.
- **JSON elements** (`collectFlatElements`) are the same fields, with booleans, `parsed_bounds`, and `window_*` fields. A node is included if it has content (text/desc/resId/stateDesc/error/paneTitle), is interactive (clickable/scrollable/checkable/focusable/longClickable/editable) or is a heading (`:256-273`).
- **Atomic snapshot** (`:258-317`):
  - On API 30+ it starts `takeScreenshot(DEFAULT_DISPLAY)` asynchronously and dumps concurrently.
  - It waits ≤ **2.5 s** for the screenshot. On `ERROR_TAKE_SCREENSHOT_INTERVAL_TIME_SHORT` (3), which is the framework limit of about one per **333 ms**, it retries once after **350 ms**.
  - The HardwareBuffer is copied to ARGB_8888 and encoded as JPEG q80 base64, and `width`/`height` are set from the bitmap.
  - Below API 30 it returns `has_screenshot:false` plus `screenshot_error`.
- **Display info** comes from `getMaximumWindowMetrics` (API 30+) or `getRealMetrics`, and rotation is 0-3 (`DisplayUtils.java`).

### B7. `GestureController.java`
- **tap:** a `Path.moveTo(x,y)` stroke of **60 ms** via `dispatchGesture`. The call is posted on the main looper and made synchronous with a CountDownLatch; the result is onCompleted/onCancelled (`:305-311, 434-472`).
- **doubleTap:** two taps **100 ms** apart. **longPress:** duration clamped to **500-5000 ms**. **swipe:** a line of **50-5000 ms**.
- **setText(text, append)** (`:353-382`):
  - `findInputNode` looks for `findFocus(FOCUS_INPUT)` in each root, else the first `isEditable && isFocusable && isEnabled && isVisibleToUser` node in DFS order (`HierarchyDumper.java:784-831`).
  - Append reads the existing text unless it is showing the hint.
  - It then calls `performAction(ACTION_SET_TEXT, ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE)`.
  - **It never targets a specific node**; it always uses the focused or first editable field.
- **clearText** is `setText("")`. **setClipboard** calls `ClipboardManager.setPrimaryClip` on the main thread, with a 2 s wait. **performGlobalAction** maps the names listed in B5.

### B8. UiAutomation conflict (critical for droidctl)
Android **unbinds every accessibility service** while a `UiAutomation` connection created without `FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES` is alive. u2's server (`app_process … com.wetest.uia2.Main` / `com.github.uiautomator`) and Appium's server both create one (`helper_manager.py:51-59`). The APK does not "avoid" UiAutomation. It simply *is not* UiAutomation, so it co-exists with Mobly/Espresso but **not with u2**.

The host handles this in `attach()` (`:886-924`). If the helper is silent but installed:
- It scans `ps -A -o PID,ARGS` for the markers `com.wetest.uia2.Main` / `com.github.uiautomator` (u2) or `io.appium.uiautomator2`.
- It kills u2's PIDs plus `am force-stop com.github.uiautomator`, waits **1.0 s**, and re-pings (12 × 0.5 s).
- It **never** kills Appium; it names Appium in the error instead, and suggests the `disableSuppressAccessibilityServices` capability.
- `UIAutomatorClient.disconnect(stop_server=True)` calls `d.stop_uiautomator()` to release the connection (`ui_automator_client.py:452-483`).
- Ping attempts: 12 after provisioning and 3 when lazy, 0.5 s apart.

**Consequence for droidctl:** v1 (u2) and v2 (APK) are **mutually exclusive at runtime**. Switching backends means stopping u2's server. Plan for a `backend` state and a `doctor` check. Fallback from the helper to u2 is one-way per call until u2 is stopped again. An `auto` mode (helper, else u2) also exists: `HelperEmptyHierarchy` triggers the u2 fallback (`accessibility_client.py:99-105`). Parity between backends is validated by `core/diagnostics/hierarchy_parity.py`, which requires label recall ≥ **0.9** and precision ≥ **0.8** at IoU ≥ **0.5**.

### B9. Python client: `clients/accessibility_client.py`
- It uses urllib with a 6 s request timeout (8 s for `/snapshot`). `get_screen_data` is one call to `/snapshot?fields=xml`, falling back to adb screencap when there is no screenshot.
- Repairs are single-shot: a 401 re-pushes the token and retries once. A transport error on a **read** (no payload) rebuilds the tunnel and retries once. **Actions are never replayed**, because they may already have executed (`:222-262`). This is a good rule to port.
- Input methods: `press_key` uses the helper's global action for back/home/recents/notifications/quick_settings, and otherwise `adb shell input keyevent KEYCODE_X`. `tap`/`swipe` go to the helper `/action`, and `send_text` is `type` with `append=True`.

### B10. What is missing for droidctl v2 (what our APK must add)
1. **Node-addressed actions.** Every action is coordinate-based or "focused field". We need `click {locator}` → `performAction(ACTION_CLICK)` on the resolved node, walking up to the nearest clickable ancestor. We also need `ACTION_LONG_CLICK`, `ACTION_SET_TEXT` on a *specific* node, `ACTION_FOCUS`, `ACTION_SCROLL_FORWARD/BACKWARD`, `ACTION_SHOW_ON_SCREEN` (scroll-into-view) and `ACTION_SET_SELECTION`, with a coordinate `dispatchGesture` fallback when `performAction` returns false.
2. **Node identity.** Dumps carry no stable id. Only `index` (the child position) and window-id are available. We need either the fingerprint (resource-id, class, text, desc, ancestor path) resolved **on-device** in one IPC, or a per-dump node table that maps our ref to `AccessibilityNodeInfo` plus a generation counter. `AccessibilityNodeInfo` source ids are not exposed publicly, so plan on the fingerprint-plus-path approach with the bounds hint used as the tie-break.
3. **Change events / wait-for-idle.** The service subscribes only to WINDOW_STATE_CHANGED. For cheap `changed:true` and waits we want `TYPE_WINDOW_CONTENT_CHANGED | TYPE_VIEW_SCROLLED | TYPE_WINDOWS_CHANGED | TYPE_VIEW_TEXT_CHANGED`, with a monotonically increasing `ui_generation` counter and `last_event_ms`. Add a `GET /wait?since=<gen>&idle_ms=300&timeout=…` long-poll. This replaces host-side hash polling and fixed sleeps.
4. **A server-side filter or selector** (`/find?text=…&id=…`) so a `tap --text X` costs one round trip. Optionally, the pruned snapshot could be built on-device.
5. **Keep-alive transport.** It is `Connection: close` per request. The raw JSON-RPC mode exists but is one command per connection. A persistent line-JSON socket would cut latency.
6. **IME state** as a first-class field. The IME window is present as `window-type=input_method`; expose `keyboard_shown` in `/ping` and `/snapshot`.
7. **Toasts:** `TYPE_NOTIFICATION_STATE_CHANGED` with `Toast` class text. u2 has a toast API; the helper does not.
8. **Security and UX:** keep the token scheme. Note that Android 13+ "restricted settings" do not block adb-installed APKs; adb installs are fine.

---

## C. Consolidated numeric heuristics

| Heuristic | Value | Where |
|---|---|---|
| Min element size | `max(5, 0.005·min(W,H))` px, strictly greater than | `ui_filter.py:385-393` |
| Fixed bar width | ≥ 95% W | `ui_filter.py:474` |
| Bottom bar height | 25 px … 15% H (or id keywords / systemui) | `:491` |
| Top bar height | 15 px … 12% H (or systemui) | `:498` |
| Sliver discard after bar clamp | height ≤ max(min_size, 20) px | `:556` |
| Default screen when unknown | 1080×2400 | many |
| Bounds normalisation | 0-1000 per axis | `visualization.py:348` |
| Same-text dedup | centre distance < 8 px | `visualization.py:360` |
| Occlusion warning | overlap ≥ 50% of either; nesting = contained, >2× area, centre dist < 20% max dim | `visualization.py:210-236` |
| Safety net retries | 3 × 0.4 s | `precondition_xml.py:64` |
| Scale factor | diag / 2631.8 | `:607-610` |
| Weights | id .5, text .4, bounds .3, coord .3; missing id +.3, wrong id −.5 | `:49-52, 243-255` |
| Size mismatch | w or h ratio > 2.5 | `:207` |
| Distance decay | max(0.5, 1 − d/800) | `:342` |
| Pass threshold | 0.55 if d ≤ 150·s, else 0.75 | `:657` |
| Self-heal radius | ≤ 200·s px | `:363` |
| Shift classification | d ≤ 100·s (300·s with id) and identity ≥ 0.85 (≥ 0.5 with id) | `:396-407` |
| Occupant not a blocker | full-screen (±10 px) or > 3× target area | `:507-517` |
| Text similarity | exact 1.0 / substring 0.8 / difflib | `:182-190` |
| Settle sleeps | 0.3 s (driver), 0.4 s (observe / perception) | `adb_driver.py:124`, `observation.py:43`, `perception.py:159` |
| Stability (unused) | ≤ 1.5 s, 0.2 s poll, < 0.1% pixels; mask top 10% / bottom 5%; blur 5, threshold 25 | `image_diff.py:138-235` |
| Change ROI (unused) | ±50·s px (min 15), > 5% pixels; global > 0.01% | `image_diff.py:28-131` |
| Same-screen note | ≤ 3 px differ at tolerance 8, last 3 steps | `prompts.py:878-880` |
| dHash revisit | 64-bit, Hamming ≤ 5 | `config/agent.py:565` |
| Focus-then-type wait | 1.0 s | `actuators/adb.py:97` |
| Clear sequence | MOVE_END, Shift+MOVE_HOME, DEL, 20×DEL (or Ctrl+A variant) | `adb_driver.py:311-316` |
| FastInputIME sleeps | 0.3 s on, 0.5 s after send | `ui_automator_client.py:340-345` |
| Swipe | x = 0.6 W; y 0.7→0.3 H; 800 ms; horizontal 0.75↔0.25 W | `adb_driver.py:280-304` |
| Tap long-press switch | duration ≥ 500 ms → `input swipe x y x y d` | `adb_driver.py:238` |
| OCR fusion | overlap ≥ 0.9 / ≥ 0.7 of OCR area; giant node > 10% screen; line 10 px; merge gap 30 px; dedup difflib > 0.8; status bar 4% H | `ocr_xml_fusion.py` |
| wait_for_text | poll 0.5 s, default 5 s | `actuators/adb.py:346-360` |
| Helper dump limits | depth 75, 8000 nodes; empty-root backoff 40/80/120/160/220/300 ms | `HierarchyDumper.java:51-52, 399` |
| Helper screenshot | wait ≤ 2.5 s; rate-limit retry 350 ms; JPEG q80 | `HierarchyDumper.java:185-186, 280, 287` |
| Helper gestures | tap 60 ms; double 100 ms gap; long 500-5000 ms; swipe 50-5000 ms; default timeouts 1500/2000/2500/3000 ms | `GestureController.java` |
| Helper server | 127.0.0.1:18888, backlog 50, socket timeout 10 s, header ≤ 16 KB, body ≤ 4 MB | `CommandServer.java` |
| Host helper timeouts | request 6 s (/snapshot 8 s); ping 2 s; ping retries 12 / 3 × 0.5 s; u2 release settle 1.0 s | `accessibility_client.py:162,315`, `helper_manager.py:103-106` |
| Enable retry | 5 × 0.4 s; revive gap 0.3 s | `helper_manager.py:115-117` |
| Token | 48 hex characters, file mode 0600 | `helper_manager.py:328-331` |
| u2 connect | 3 attempts, 1 s·n backoff | `ui_automator_client.py:296-298` |

---

## D. Verdict summary for droidctl

**Steal now (v1):**
1. **Tree-level pruning, done on the tree:** hoist wrappers, parent-child redundancy, row merge with the `[Icon]` marker, and min size scaled to the screen. Test it through the *real* dump→parse path; Artemis's flat-list bug is the cautionary tale.
2. **Visible-bounds clipping** to scrollable ancestors, plus **fixed-bar clamping** with the 20 px sliver rule, so row centres never land on the toolbar or nav bar.
3. **The safety-net matcher** as the ref re-resolver: weights, id penalty, 2.5× size guard, badge-stripped text, distance decay, scale factor. Use its **shifted / occupied / gone** classification in `stale-ref` errors.
4. **Hint vs text vs error** for inputs, the **occlusion flag**, and the 8 px dedup.
5. **Text entry fallbacks:** `set_text`, then clipboard+PASTE, then the clear keyevent sequence. Skip the focus tap when the field is already focused.
6. **Swipe geometry** (60% x, 0.7→0.3, 800 ms); **mask systemui** from change detection.
7. **Transport rule:** retry reads once, never replay actions.

**Skip:**
- The index→cached-centre flow (we re-resolve).
- 0-1000 normalisation.
- The bounds-in-every-line format.
- The VLM pixel safety net.
- OCR in v1.
- Fixed settle sleeps (poll the XML hash instead).

**v2 APK:** start from their skeleton, which is excellent. That covers the token-over-WRITE_SECURE_SETTINGS broadcast, loopback 18888 with `adb forward tcp:0`, the multi-window dump with visible-bounds clipping and prefetch, the atomic screenshot, the enable/revive/verify loop, the no-Gradle build and the manifest-driven upgrade. Then add:
- node-addressed `performAction` (click, long-click, set_text, scroll, show_on_screen)
- on-device fingerprint resolution
- event-driven `ui_generation` and `/wait` idle long-poll
- `keyboard_shown`
- a persistent socket

Remember that **u2 and the APK cannot run at the same time.**
