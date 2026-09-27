package dev.droidctl.testapp

/**
 * The scenario registry's metadata: the single source for scenarios.json, the
 * `__list__` dump and the e2e coverage gate. Pure Kotlin (no Android types), so
 * a JVM unit test can check scenarios.json against it. Every scenario also logs
 * `shown` when it is on screen; that is not repeated in [events].
 */
data class Spec(val name: String, val group: Int, val toolkit: String, val desc: String, val events: List<String>)

private fun v(name: String, group: Int, desc: String, vararg ev: String) = Spec(name, group, "views", desc, ev.toList())
private fun c(name: String, group: Int, desc: String, vararg ev: String) = Spec(name, group, "compose", desc, ev.toList())

val SPECS: List<Spec> = listOf(
    // 0. utilities (not UI scenarios)
    v("peer_uid_probe", 0, "connects to the agent socket @droidctl from this app's uid and logs the reply (expects -32001)", "probe"),

    // 1. timing and app state
    v("splash", 1, "1.5 s splash (splash_ms), then Home", "content_shown", "click"),
    v("delayed", 1, "content appears after delay_ms (default 3000)", "content_shown", "click"),
    v("spinner_forever", 1, "indeterminate progress that never finishes", "click"),
    v("skeleton", 1, "placeholder boxes (desc Loading) replaced by rows after delay_ms", "loaded", "click"),
    v("ticker", 1, "a clock text that changes every interval_ms (100)", "click"),
    v("slow_click", 1, "button reacts after delay_ms (2000)", "click", "done"),
    v("disabled_then_enabled", 1, "Submit disabled, enabled after delay_ms (3000)", "enabled", "click"),
    v("error_retry", 1, "simulated network error with Retry (fail_times extra)", "click", "content_shown"),
    v("pull_refresh", 1, "SwipeRefreshLayout over 30 rows", "refresh", "click"),
    v("ui_hang", 1, "tap sleeps hang_ms (10000) on the main thread", "click", "unhung"),
    v("crash", 1, "tap throws a RuntimeException", "click"),
    v("slow_a11y", 1, "AccessibilityNodeProvider that sleeps sleep_ms (5000) per node", "click"),
    v("recreate", 1, "Recreate / Rotate buttons recreate the activity", "click"),

    // 2. visibility, security, occlusion
    v("flag_secure", 2, "FLAG_SECURE window", "click"),
    v("sensitive", 2, "accessibilityDataSensitive views (API 34+; no-op below)", "click"),
    v("hidden_a11y", 2, "noHideDescendants subtree containing a button", "click"),
    v("password", 2, "password field (DTA logs len and value)", "text", "click"),
    v("overlay_blocker", 2, "transparent full-screen view over a Buy button", "click", "blocked"),
    v("partial", 2, "buttons 5%, 100%, 30% visible and one offscreen in a 400dp viewport", "click"),
    v("under_keyboard", 2, "Submit at the bottom, keyboard open, adjustNothing", "text", "click"),
    v("alpha_zero", 2, "alpha-0, INVISIBLE and GONE buttons next to a visible one", "click"),
    v("zero_size", 2, "0x0 button and a 1px clickable view", "click"),
    v("system_bars", 2, "edge-to-edge rows under translucent status/nav bars", "click"),

    // 3. controls and click semantics
    v("buttons", 3, "text Button, labelled ImageButton, unlabeled ImageButton", "click"),
    c("buttons_compose", 3, "Compose Button, labelled and unlabeled IconButton", "click"),
    v("toggle", 3, "switch, checkbox, radio group", "click"),
    c("toggle_compose", 3, "Compose Switch, Checkbox, radio rows", "click"),
    v("counter", 3, "increments on every click", "click"),
    c("counter_compose", 3, "Compose counter", "click"),
    v("row_nested", 3, "clickable rows each with a clickable star", "click"),
    c("row_nested_compose", 3, "Compose clickable rows with a star IconButton", "click"),
    v("touch_only", 3, "onTouchListener only; ACTION_CLICK is a silent no-op", "tap"),
    v("click_no_event", 3, "handles ACTION_CLICK but never sends TYPE_VIEW_CLICKED or changes", "click"),
    v("long_press", 3, "long-press opens a popup menu; tap does nothing", "long_press", "menu"),
    v("double_tap", 3, "double-tap to like", "double_tap", "single_tap"),
    v("custom_actions", 3, "rows with Archive/Delete accessibility custom actions", "action", "click"),
    c("custom_actions_compose", 3, "Compose rows with Archive/Delete custom actions", "action", "click"),
    v("swipe_only_delete", 3, "swipe-left to delete, no a11y action", "swipe_delete", "click"),
    v("slider", 3, "SeekBar 3/10 and a RangeSlider", "change"),
    c("slider_compose", 3, "Compose Slider 3/10", "change"),
    v("spinner_dropdown", 3, "Spinner, PopupMenu, AutoCompleteTextView", "select", "menu", "click"),
    v("tabs_pager", 3, "TabLayout + ViewPager2 with three pages", "page"),
    v("bottom_nav_drawer", 3, "toolbar with hamburger and overflow, drawer, bottom nav", "click", "nav", "menu"),
    v("canvas", 3, "Canvas-drawn buttons A/B/C with no accessibility info", "tap"),
    v("virtual_views", 3, "calendar exposed as virtual nodes (ExploreByTouchHelper)", "click"),
    v("drag_reorder", 3, "drag handles reorder a list", "reorder"),

    // 4. text input
    v("form", 4, "hint, setError, multiline, number-only, maxLength 5", "text", "submit", "click"),
    c("form_compose", 4, "Compose text fields with label, error, password", "text", "submit"),
    v("unicode", 4, "plain field for Unicode round trips", "text"),
    v("formatter", 4, "phone-number formatting TextWatcher", "text"),
    v("reject_set_text", 4, "EditText that refuses ACTION_SET_TEXT", "text"),
    v("otp", 4, "six single-digit boxes with auto-advance; a whole code spreads", "text", "otp"),
    v("search_enter", 4, "SearchView that acts on IME enter", "text", "search"),
    v("webview_form", 4, "WebView with an input and a Send button", "text", "submit"),

    // 5. lists and scrolling
    v("long_list", 5, "RecyclerView of 1,000 rows (rows extra)", "click"),
    c("list_compose", 5, "LazyColumn of 1,000 rows", "click"),
    v("list_insert_top", 5, "a new row inserted at the top every interval_ms (3000)", "insert", "click"),
    v("duplicates", 5, "20 rows each with an identical Delete button", "delete"),
    c("duplicates_compose", 5, "Compose: 20 rows each with a Delete button", "delete"),
    v("lookalike_ok", 5, "screen A OK leads to screen B with an OK in the same place", "click"),
    v("nested_scroll", 5, "horizontal carousel inside a vertical list", "click", "scroll"),
    v("infinite", 5, "loads 20 more rows on reaching the end, forever", "load_more", "click"),
    v("huge_tree", 5, "~5,000 nodes, 100 levels deep (depth, per_level extras)"),

    // 6. windows, dialogs, system UI
    v("dialogs", 6, "AlertDialog, full-screen dialog, bottom sheet (open=alert|full|sheet)", "dialog_shown", "choice", "dismiss", "click"),
    v("snackbar_toast", 6, "Snackbar with Undo; Toast Saved!", "undo", "click"),
    v("permission", 6, "requests CAMERA (system permission dialog)", "permission", "click"),
    v("notification", 6, "posts a notification with a Mark read action", "posted", "action", "click"),
    v("back_confirm", 6, "back shows an Exit? dialog", "back", "choice"),
    v("deep_link", 6, "button opening droidctl-test://s/form", "click"),
    v("keyboard_toggle", 6, "input that shows the IME; Hide keyboard", "ime", "text", "click"),

    // 8. spatial layout
    v("cart", 8, "top bar, rows with −/qty/+, total and Checkout", "inc", "dec", "checkout", "click"),
    c("cart_compose", 8, "Compose cart", "inc", "dec", "checkout", "click"),
    v("calendar", 8, "October 2026 in a 7-column GridView", "click"),
    c("calendar_compose", 8, "October 2026 in a LazyVerticalGrid", "click"),
    v("keypad", 8, "3x4 PIN keypad from aligned rows (no collectionInfo)", "key", "pin"),
    v("photo_grid", 8, "12 image tiles in 3 columns; every fourth has no desc", "click"),
    v("unlabeled_icons", 8, "toolbar icons with ids but no descs", "click"),
    v("label_left_form", 8, "inputs without hints; labels left of or above them", "text", "click"),
    v("cards", 8, "four plan cards in a 2x2 grid, each with Buy", "buy"),
    v("fab_sheet_drawer", 8, "FAB over a list, peeking bottom sheet, nav drawer", "click", "nav"),
    v("rtl", 8, "the cart screen with RTL layout direction", "inc", "dec", "checkout", "click"),
    v("layout_bugs", 8, "overlapping buttons, ellipsized text, a 30dp touch target", "click"),
)

/** scenarios.json, generated from [SPECS] (hand-rolled so the JVM test needs no org.json). */
fun specsJson(): String {
    fun q(s: String) = "\"" + s.replace("\\", "\\\\").replace("\"", "\\\"") + "\""
    return SPECS.joinToString(",\n", "[\n", "\n]\n") { s ->
        "  {\"name\": ${q(s.name)}, \"group\": ${s.group}, \"toolkit\": ${q(s.toolkit)}, " +
            "\"events\": [${s.events.joinToString(", ") { q(it) }}], \"desc\": ${q(s.desc)}}"
    }
}
