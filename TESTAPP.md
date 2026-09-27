# droidctl-testapp — edge-case lab

A purpose-built Android app (`android/testapp`, Kotlin, Views + Compose, minSdk 26). Every screen reproduces one hard case and knows what droidctl **must** do on it. It drives e2e tests, generates offline fixtures and backs the benchmark.

## Design rules
- **One scenario per screen, launched directly:** `adb shell am start -n dev.droidctl.testapp/.Main --es s <scenario>` (or deep link `droidctl-test://s/<scenario>`). Tests never depend on each other or on navigation order, and `--es s <scenario> --ez reset true` clears state.
- **Ground truth is observable without the UI.** Every interaction logs a structured line under logcat tag `DTA`:
  ```
  DTA {"s":"toggle","ev":"click","id":"wifi","n":1,"state":true}
  ```
  Tests read these lines to assert *exactly-once* taps, typed values and so on. They don't trust droidctl's own output for ground truth.
- **Deterministic:** fixed seed data, no network (errors are simulated), and every timing is configurable via extras (`--ei delay_ms 3000`).
- **Two UI toolkits:** most scenarios exist in a **View** variant and a **Compose** variant (`s=list` / `s=list_compose`), since their accessibility trees differ.
- **Fixtures from real trees:** `make fixtures` runs every scenario, saves each raw tree to `tests/fixtures/trees/testapp/<scenario>.json` and each pruned snapshot to `…/<scenario>.snap.txt` as the golden output. The offline tests then need no phone.

## Scenario matrix
Legend for the expected behaviour: **R** = ref resolution, **S** = snapshot, **A** = action, **W** = wait/settle, **E** = error kind.

### 1. Timing and app state
| scenario | what it does | droidctl must |
|---|---|---|
| `splash` | 1.5 s splash, then content | S: `warning: tiny tree (splash?)`; `wait --text Home` succeeds |
| `delayed` | content appears after `delay_ms` | `wait --text X --timeout` succeeds; with a short timeout, E `timeout` |
| `spinner_forever` | indeterminate progress, never finishes | W: settle returns `settled:false` at its cap and the action still succeeds; S shows `progress` |
| `skeleton` | shimmer placeholders, then real rows | S shows no placeholder junk; `wait --gone` on the loading state |
| `ticker` | text changes every 100 ms (clock/marquee) | W: settle isn't stuck forever; the diff flags only the ticker; signature is stable (text isn't in it) |
| `slow_click` | button reacts after 2 s | A: `changed:true` inside the settle window; with `--settle 0.5`, `changed:false` and no retry (DTA n==1) |
| `disabled_then_enabled` | button enabled after 3 s | S `disabled`; tap on it → E `disabled` (not a silent no-op); `wait` works |
| `error_retry` | simulated network error + Retry | S shows the error text and a Retry button; tap Retry reaches content |
| `pull_refresh` | SwipeRefreshLayout | `swipe down` triggers a refresh (DTA ev=refresh) |
| `ui_hang` | sleeps 10 s on the main thread on tap | the device's 2 s tree read budget returns `degraded:true` without hanging the service; the ANR dialog then shows up as a system window |
| `crash` | throws on tap | the "keeps stopping" dialog shows as a system dialog; `logs` shows the stack trace; `current` reports the package isn't in the foreground |
| `slow_a11y` | custom AccessibilityNodeProvider that sleeps 5 s | tree read times out per node without killing the service → `degraded` |
| `recreate` | rotation / config change recreates the activity | an old ref heals (same signature, new nodes) through the tiers, not by position |

### 2. Visibility, security, occlusion
| scenario | what it does | droidctl must |
|---|---|---|
| `flag_secure` | `FLAG_SECURE` window | `shot` → E `secure-window` (never a black image passed off as a real screenshot); snapshot still works |
| `sensitive` | `accessibilityDataSensitive` (API 34) views | visible because the service declares `isAccessibilityTool=true`; otherwise documented as a known gap |
| `hidden_a11y` | `importantForAccessibility=noHideDescendants` subtree with a button | not in the default snapshot; `--all` shows it where the platform allows; documented |
| `password` | password field | value never printed (shown as `password` and `len=8`); readback compares length only |
| `overlay_blocker` | transparent full-screen view over a button | R: E `occluded` naming the covering node; `--method action` still works because ACTION_CLICK bypasses touch |
| `partial` | buttons 5%, 30% and 100% visible, and one offscreen | S: the 5% one is dropped unless it has a visible child; the offscreen one → E `offscreen` + hint; `scroll-to` then tap |
| `under_keyboard` | Submit button at the bottom; keyboard open | S `keyboard=shown`; the button is flagged `covered`; tap uses ACTION_CLICK (works) or reports `occluded` for `--method gesture` |
| `alpha_zero` | clickable view with alpha 0, and View.INVISIBLE / GONE | alpha-0 is flagged as a trap if the platform reports it visible; INVISIBLE/GONE absent |
| `zero_size` | 0×0 clickable, and 1 px views | dropped |
| `system_bars` | edge-to-edge content under status/nav bars | rows clipped out of the bars; tap centers never land in bar areas |

### 3. Controls and click semantics
| scenario | what it does | droidctl must |
|---|---|---|
| `buttons` | text Button, ImageButton with a desc, ImageButton **without** a label | the unlabeled one shows as `button (unlabeled) #id`, or `(unlabeled)` plus bounds with `--bounds` |
| `toggle` | switch, checkbox, radio group | exactly-once: DTA n==1 per tap; S shows the state; the diff shows `off→on` |
| `counter` | increments on every click | 10 taps → n==10 (no double-taps from the fallback) |
| `row_nested` | clickable list row containing a clickable star icon | S: the row merges its text but the star stays a separate ref; tap star ≠ tap row (DTA) |
| `touch_only` | custom view with only `onTouchListener` (ACTION_CLICK is a no-op) | A: no TYPE_VIEW_CLICKED → one gesture fallback; `method:"gesture-fallback"`; n==1 |
| `click_no_event` | view that handles performClick but suppresses the a11y click event | no-change + no event → fallback may fire; documents the double-trigger risk; must still be n≤2 and flagged |
| `long_press` | long-press only (context menu) | `long-press` opens the menu; plain `tap` doesn't |
| `double_tap` | double-tap to like | `tap --double` (gesture) works |
| `custom_actions` | row with Archive/Delete a11y custom actions | S `actions=[Archive, Delete]`; `action --ref N Delete` works with no gesture |
| `swipe_only_delete` | swipe-to-delete with **no** a11y action | only `swipe left --ref N` works; S has no `actions=` |
| `slider` | SeekBar + RangeSlider | S `range=3/10`; `set --ref N 7` works via ACTION_SET_PROGRESS |
| `spinner_dropdown` | Spinner, PopupMenu, AutoCompleteTextView | popups show up as separate windows; selecting an option works |
| `tabs_pager` | TabLayout + ViewPager2 | tab taps work; `swipe left` changes the page; offscreen pages are not listed |
| `bottom_nav_drawer` | bottom nav + navigation drawer + toolbar overflow | the drawer opens via its hamburger desc; the overflow `More options` works |
| `canvas` | a game-like Canvas with drawn buttons and no a11y | S: `warning: opaque view (use shot --marks / --point)`; `tap --point` works; coordinates are in device px |
| `virtual_views` | AccessibilityNodeProvider exposing virtual children (calendar grid) | virtual nodes listed; ACTION_CLICK on a virtual node works |
| `drag_reorder` | drag handle to reorder a list | `gesture --path` drag works; refs heal after the reorder |

### 4. Text input
| scenario | what it does | droidctl must |
|---|---|---|
| `form` | hint, setError, multiline, number-only, maxLength | S `hint=`, `error=`, `empty`; type into multiline keeps `\n`; maxLength truncation reported in `value` |
| `unicode` | plain field | type `"Çağrı ğüşıöç 日本 🙂 مرحبا"` reads back exactly |
| `formatter` | TextWatcher that formats phone numbers | `value` returns the formatted text; not reported as a failure |
| `reject_set_text` | custom EditText that ignores ACTION_SET_TEXT | the ladder falls back to clipboard paste; `method:"paste"`; the clipboard is restored afterwards |
| `otp` | 6 single-char boxes with auto-advance | `type --ref first 123456` fills all six boxes via the fallback (or documents the per-box way) |
| `search_enter` | SearchView acting on IME enter | `--enter` works via ACTION_IME_ENTER |
| `webview_form` | WebView with an input + submit | typing and tapping inside a WebView works (web a11y nodes) |

### 5. Lists and scrolling
| scenario | what it does | droidctl must |
|---|---|---|
| `long_list` | RecyclerView of 1,000 rows (+ `list_compose` LazyColumn) | S `list 8/1000 more↓`; `scroll-to --text "Row 734"` works with a cap; the snapshot stays <2k tokens |
| `list_insert_top` | a new row inserted at the top every N seconds | refs to existing rows heal by identity, never by index (the android_world bug) |
| `duplicates` | 20 rows each with an identical "Delete" button | tapping the "Delete" in row 7 resolves through the row's label/ancestor; a bare `--text Delete` → E `ambiguous` listing candidates |
| `lookalike_ok` | screen A "OK" → screen B also has "OK" at the same position | a stale ref from A on B → E `stale-ref` (signature changed; tiers 3–4 disabled) |
| `nested_scroll` | horizontal carousel inside a vertical list | both scrollables listed; `scroll --ref carousel right` moves only the carousel |
| `infinite` | loads more on reaching the end | `scroll-to` stops at its cap with a clear message |
| `huge_tree` | 5,000 nodes, 100 levels deep | the tree dump stays under budget, no recursion overflow; the snapshot is capped with a truncation note |

### 6. Windows, dialogs, system UI
| scenario | what it does | droidctl must |
|---|---|---|
| `dialogs` | AlertDialog, full-screen dialog, bottom sheet | S `dialog=yes`; only the dialog plus a short background summary; dismiss via `back` |
| `snackbar_toast` | Snackbar with an Undo action; Toast | the toast appears in the header and the action result; the Snackbar action is tappable |
| `permission` | runtime permission request (system permissioncontroller window) | foreign-package dialog shown with its package; `tap --text Allow` works |
| `notification` | posts a notification with an action | `notifications` opens the shade; the notification and its action are tappable |
| `back_confirm` | app intercepts back ("Exit?") | `back` shows the dialog in the diff |
| `deep_link` | intent filter | `open-url droidctl-test://s/form` lands on the form |
| `keyboard_toggle` | focus an input to show the IME | `keyboard=shown/hidden` tracked; IME nodes are never listed as app elements |

### 8. Spatial layout
| scenario | what it does | droidctl must |
|---|---|---|
| `cart` | rows with −/qty/+ controls, top bar with icons, bottom total + Checkout | S: region headers; each row's controls on one line; `tap --text "+" --right-of "Wireless Mouse"` hits the right one (DTA) |
| `calendar` | month grid (GridView + Compose LazyVerticalGrid variants) | S renders a 7-column table; "tap the 14th" resolves via the grid cell ref |
| `keypad` | 3x4 PIN keypad without collectionInfo (aligned buttons only) | the alignment heuristic renders a grid; the typed PIN is correct per DTA |
| `photo_grid` | image-only tiles with descs, some without | unlabeled tiles are labelled by position (`row 2 col 3`) |
| `unlabeled_icons` | toolbar icons with no desc, only resource-ids | `share?` / `delete?` labels inferred from ids, marked as inferred |
| `label_left_form` | inputs with no hint, labels as TextViews to the left or above | inputs get `"Email"` etc. via the label-for heuristic |
| `cards` | 4 cards, each with an identical "Buy" button | row grouping plus `--below "Premium"` disambiguates; bare `--text Buy` → `ambiguous` |
| `fab_sheet_drawer` | FAB over a list, bottom sheet, nav drawer | regions `fab` / `sheet` / `drawer` shown; the FAB isn't merged into list rows |
| `rtl` | the same cart screen under an RTL locale | reading order and `--right-of` follow the screen, not the locale; documented |
| `layout_bugs` | overlapping buttons, ellipsized text, a 30dp touch target | `layout-check` reports all three |

**Spatial benchmark:** 30 questions/tasks such as "tap the icon right of X", "pick Oct 14", "which button is bottom-right", "enter PIN 4821". Run each against: flat list only, default spatial, `+--geo`, `+--map` and `+shot --marks`. Record success rate and tokens, then pick the defaults from data.

### 7. Device-level (driven by tests, not app screens)
| case | droidctl must |
|---|---|
| screen off / locked | `doctor` + E `screen-off` with a hint (`wake`) |
| service killed / disabled mid-run | E `not-installed`/`suppressed` with a hint; auto-setup re-enables it |
| uiautomator2/Appium attached (UiAutomation suppresses the service) | E `suppressed` naming the likely cause |
| TalkBack/other a11y services enabled before `setup` | still enabled after `setup` and after `teardown` |
| APK older than the host | auto-upgrade with `install -r`, then continue |
| two devices attached, no `-d` | E `bad-args` listing serials |
| first command with no daemon running | daemon auto-starts detached; the stderr notice (or the `--json` `daemon.started`) appears exactly once; the next call reports `mode:"daemon"` |
| `DROIDCTL_NO_DAEMON=1` | the command works in-process, `mode:"inprocess"`; no droidctl process remains afterwards (`pgrep`) |
| `DROIDCTL_AUTOSTART=0` with no daemon | E `no-daemon` with a hint |
| daemon from an older version running | the client triggers a restart; the command succeeds on the new version |
| daemon killed with -9 mid-session | the next call finds the stale socket, respawns, and succeeds |
| spawning blocked (simulated sandbox) | automatic in-process fallback, `mode:"inprocess"` |
| two phones, a slow `wait` on phone A | commands on phone B are not delayed (per-device locks) |
| toast fires between two agent calls | reported on the next response ("toast while idle") |
| cache validity | a snapshot after no change is a cache hit (<5 ms daemon side); after a background content change it is a miss |
| `droidctl mcp` | MCP Inspector passes; initialize → tools/list → tools/call snapshot/tap works on the `cart` scenario; tool list == command registry |

## Test harness
- **`tests/e2e/` (pytest, `DROIDCTL_SERIAL` required):** one test module per group above. The `scenario(name, **extras)` fixture launches the screen, clears logcat, and returns a `dta()` helper that parses the `DTA` lines.
- **Assertions come in three kinds:**
  1. **Ground truth:** DTA counts and values.
  2. **droidctl output contract:** the `--json` shape, `changed`, `method`, `error.kind`.
  3. **Budgets:** tokens per snapshot and latency per call. The e2e run records these into `bench.json`.
- **Coverage gate:** a unit test asserts that every scenario in the app's `Scenarios.kt` registry has at least one e2e test *and* a golden fixture. A new scenario can't be added silently.
- **Golden fixtures:** `make fixtures` regenerates them from the device. The offline tests (`test_snapshot.py`, `test_resolve.py`) run against the trees in CI with no phone attached. A diff in `*.snap.txt` is reviewed like code.
- **Real apps as well:** a small set of trees captured from real apps (Settings, Play Store, a Compose-heavy app, Chrome) guards against the synthetic-only blind spot that bit mobile-use.

## Milestone placement
- **M2:** the app skeleton, scenario registry, DTA logging, plus groups 3, 4, 5 and 8, which the snapshot and resolver milestones need first.
- **M7:** groups 1, 2 and 6, which exercise settle, fallback and errors.
- **M8:** group 7, the coverage gate, and a benchmark run over all scenarios.
