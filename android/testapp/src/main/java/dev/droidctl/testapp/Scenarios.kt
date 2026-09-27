package dev.droidctl.testapp

import android.net.LocalSocket
import android.net.LocalSocketAddress
import android.view.View

/** Scenario name -> screen builder. [SPECS] and this map must name the same set (checked at startup and in tests). */
object Scenarios {
    val specs: List<Spec> = SPECS
    val byName: Map<String, Spec> = SPECS.associateBy { it.name }

    val builders: Map<String, (Sc) -> View> = mapOf(
        "peer_uid_probe" to ::peerUidProbe,
        "splash" to G1::splash, "delayed" to G1::delayed, "spinner_forever" to G1::spinnerForever,
        "skeleton" to G1::skeleton, "ticker" to G1::ticker, "slow_click" to G1::slowClick,
        "disabled_then_enabled" to G1::disabledThenEnabled, "error_retry" to G1::errorRetry,
        "pull_refresh" to G1::pullRefresh, "ui_hang" to G1::uiHang, "crash" to G1::crash,
        "slow_a11y" to G1::slowA11y, "recreate" to G1::recreate,
        "flag_secure" to G2::flagSecure, "sensitive" to G2::sensitive, "hidden_a11y" to G2::hiddenA11y,
        "password" to G2::password, "overlay_blocker" to G2::overlayBlocker, "partial" to G2::partial,
        "under_keyboard" to G2::underKeyboard, "alpha_zero" to G2::alphaZero, "zero_size" to G2::zeroSize,
        "system_bars" to G2::systemBars,
        "buttons" to G3::buttons, "buttons_compose" to Cmp::buttons, "toggle" to G3::toggle,
        "toggle_compose" to Cmp::toggle, "counter" to G3::counter, "counter_compose" to Cmp::counter,
        "row_nested" to G3::rowNested, "row_nested_compose" to Cmp::rowNested, "touch_only" to G3::touchOnly,
        "click_no_event" to G3::clickNoEvent, "long_press" to G3::longPress, "double_tap" to G3::doubleTap,
        "custom_actions" to G3::customActions, "custom_actions_compose" to Cmp::customActions,
        "swipe_only_delete" to G3::swipeOnlyDelete, "slider" to G3::slider, "slider_compose" to Cmp::slider,
        "spinner_dropdown" to G3::spinnerDropdown, "tabs_pager" to G3::tabsPager,
        "bottom_nav_drawer" to G3::bottomNavDrawer, "canvas" to G3::canvas, "virtual_views" to G3::virtualViews,
        "drag_reorder" to G3::dragReorder,
        "form" to G4::form, "form_compose" to Cmp::form, "unicode" to G4::unicode, "formatter" to G4::formatter,
        "reject_set_text" to G4::rejectSetText, "otp" to G4::otp, "search_enter" to G4::searchEnter,
        "webview_form" to G4::webviewForm,
        "long_list" to G5::longList, "list_compose" to Cmp::list, "list_insert_top" to G5::listInsertTop,
        "duplicates" to G5::duplicates, "duplicates_compose" to Cmp::duplicates, "lookalike_ok" to G5::lookalikeOk,
        "nested_scroll" to G5::nestedScroll, "infinite" to G5::infinite, "huge_tree" to G5::hugeTree,
        "dialogs" to G6::dialogs, "snackbar_toast" to G6::snackbarToast, "permission" to G6::permission,
        "notification" to G6::notification, "back_confirm" to G6::backConfirm, "deep_link" to G6::deepLink,
        "keyboard_toggle" to G6::keyboardToggle,
        "cart" to { s: Sc -> G8.cart(s) }, "cart_compose" to Cmp::cart, "calendar" to G8::calendar,
        "calendar_compose" to Cmp::calendar, "keypad" to G8::keypad, "photo_grid" to G8::photoGrid,
        "unlabeled_icons" to G8::unlabeledIcons, "label_left_form" to G8::labelLeftForm, "cards" to G8::cards,
        "fab_sheet_drawer" to G8::fabSheetDrawer, "rtl" to G8::rtl, "layout_bugs" to G8::layoutBugs,
    )

    init {
        val missing = byName.keys - builders.keys
        val extra = builders.keys - byName.keys
        check(missing.isEmpty() && extra.isEmpty()) { "registry mismatch: no builder for $missing, no spec for $extra" }
    }

    fun build(name: String, s: Sc): View = builders.getValue(name)(s)

    /** Connect to the agent's socket as this app's uid: the agent must answer -32001 and close. */
    private fun peerUidProbe(s: Sc): View = with(s) {
        val status = text("probing @droidctl…", "probe_status")
        Thread {
            val reply = try {
                LocalSocket().use { sock ->
                    sock.connect(LocalSocketAddress("droidctl", LocalSocketAddress.Namespace.ABSTRACT))
                    sock.soTimeout = 3000
                    sock.outputStream.write("{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"ping\"}\n".toByteArray())
                    sock.inputStream.bufferedReader().readLine() ?: "<eof>"
                }
            } catch (e: Exception) {
                "<error ${e.javaClass.simpleName}: ${e.message}>"
            }
            dta("probe", null, "uid" to android.os.Process.myUid(), "reply" to reply)
            act.runOnUiThread { status.text = "uid ${android.os.Process.myUid()}: $reply" }
        }.start()
        col(title("Peer UID probe"), status)
    }
}
