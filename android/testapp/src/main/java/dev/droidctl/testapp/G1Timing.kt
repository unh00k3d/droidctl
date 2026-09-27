package dev.droidctl.testapp

import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.ActivityInfo
import android.graphics.Color
import android.os.SystemClock
import android.view.View
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityNodeProvider
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ProgressBar
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout

object G1 {
    fun splash(s: Sc): View = with(s) {
        val frame = FrameLayout(act)
        frame.addView(ProgressBar(act))
        later(int("splash_ms", 1500).toLong()) {
            frame.removeAllViews()
            frame.addView(col(title("Home"), text("Welcome"), button("Start", "start")))
            dta("content_shown")
        }
        frame
    }

    fun delayed(s: Sc): View = with(s) {
        val box = col(title("Delayed"), text("Loading…", "status"))
        later(int("delay_ms", 3000).toLong()) {
            box.addView(text("Loaded", "loaded", 20f))
            box.addView(button("Continue", "continue_btn"))
            dta("content_shown")
        }
        box
    }

    fun spinnerForever(s: Sc): View = with(s) {
        col(title("Working"), ProgressBar(act).apply { isIndeterminate = true; withId("progress") }, button("Do it", "do_it"))
    }

    fun skeleton(s: Sc): View = with(s) {
        val body = col(pad = 0)
        val loading = col(pad = 8).apply {
            withId("loading"); contentDescription = "Loading"
            repeat(6) { addView(box(Color.rgb(225, 225, 225), 320, 48)); addView(space(8)) }
        }
        body.addView(loading)
        later(int("delay_ms", 2000).toLong()) {
            body.removeAllViews()
            for (i in 1..6) body.addView(button("Result $i", "result"))
            dta("loaded")
        }
        col(title("Results"), body)
    }

    fun ticker(s: Sc): View = with(s) {
        val clock = text("0.0 s", "clock", 28f)
        val t0 = SystemClock.uptimeMillis()
        every(int("interval_ms", 100).toLong()) { clock.text = "%.1f s".format((SystemClock.uptimeMillis() - t0) / 1000.0) }
        col(title("Ticker"), clock, button("Tap", "tap"))
    }

    fun slowClick(s: Sc): View = with(s) {
        val status = text("Idle", "status")
        col(title("Slow"), status, button("Slow", "slow") {
            status.text = "Working…"
            later(int("delay_ms", 2000).toLong()) { status.text = "Done"; dta("done", "slow") }
        })
    }

    fun disabledThenEnabled(s: Sc): View = with(s) {
        val b = button("Submit", "submit").apply { isEnabled = false }
        later(int("delay_ms", 3000).toLong()) { b.isEnabled = true; dta("enabled", "submit") }
        col(title("Please wait"), b)
    }

    fun errorRetry(s: Sc): View = with(s) {
        val box = col(pad = 0)
        var failures = int("fail_times", 0)
        fun error() {
            box.removeAllViews()
            box.addView(text("Network error. Please try again.", "error"))
            box.addView(button("Retry", "retry") {
                box.removeAllViews(); box.addView(text("Loading…"))
                later(500) {
                    if (failures-- > 0) error() else {
                        box.removeAllViews(); box.addView(text("Welcome back", "content", 20f)); dta("content_shown")
                    }
                }
            })
        }
        error()
        col(title("Feed"), box)
    }

    fun pullRefresh(s: Sc): View = with(s) {
        var refreshes = 0
        val rv = G3.recycler(s, (1..30).map { "Entry $it" }, "entry") { dta("click", it) }
        val srl = SwipeRefreshLayout(act).apply {
            withId("refresh")
            addView(rv)
            setOnRefreshListener {
                refreshes++
                dta("refresh", "refresh")
                later(1000) { isRefreshing = false }
            }
        }
        col(title("Pull to refresh"), srl.apply { layoutParams = lp(MATCH, MATCH) }, pad = 0)
    }

    fun uiHang(s: Sc): View = with(s) {
        col(title("Hang"), button("Hang", "hang") { SystemClock.sleep(int("hang_ms", 10000).toLong()); dta("unhung", "hang") })
    }

    fun crash(s: Sc): View = with(s) {
        col(title("Crash"), button("Crash", "crash") { throw RuntimeException("droidctl testapp: deliberate crash") })
    }

    /** A view whose AccessibilityNodeProvider sleeps before every answer. */
    @SuppressLint("ViewConstructor")
    class SlowA11yView(ctx: Context, private val sleepMs: Long) : View(ctx) {
        init { setBackgroundColor(Color.rgb(250, 220, 220)) }
        override fun getAccessibilityNodeProvider(): AccessibilityNodeProvider = object : AccessibilityNodeProvider() {
            override fun createAccessibilityNodeInfo(virtualViewId: Int): AccessibilityNodeInfo? {
                SystemClock.sleep(sleepMs)
                val info = AccessibilityNodeInfo.obtain(this@SlowA11yView)
                onInitializeAccessibilityNodeInfo(info)
                info.contentDescription = "Slow view"
                return info
            }
        }
    }

    fun slowA11y(s: Sc): View = with(s) {
        col(title("Slow accessibility"), button("Normal", "normal"),
            SlowA11yView(act, int("sleep_ms", 5000).toLong()).apply { withId("slow_view"); layoutParams = LinearLayout.LayoutParams(MATCH, dp(120)) })
    }

    fun recreate(s: Sc): View = with(s) {
        col(title("Recreate"), text("Orientation: " + (if (act.resources.configuration.orientation == 2) "landscape" else "portrait"), "orientation"),
            edit("Keep me", "keep"),
            button("Recreate", "recreate_btn") { act.recreate() },
            button("Rotate", "rotate") {
                act.requestedOrientation = if (act.resources.configuration.orientation == 2)
                    ActivityInfo.SCREEN_ORIENTATION_PORTRAIT else ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE
            }).also {
            onDispose { if (!act.isChangingConfigurations) act.requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED }
        }
    }
}
