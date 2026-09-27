package dev.droidctl.testapp

import android.annotation.SuppressLint
import android.graphics.Color
import android.os.Build
import android.text.InputType
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.view.inputmethod.InputMethodManager
import android.widget.Button
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView

object G2 {
    fun flagSecure(s: Sc): View = with(s) {
        act.window.addFlags(WindowManager.LayoutParams.FLAG_SECURE)
        col(title("Secure"), text("Balance: $1,234.56", "balance", 20f), button("Reveal", "reveal"))
    }

    fun sensitive(s: Sc): View = with(s) {
        val card = text("Card 4111 1111 1111 1111", "card", 18f)
        val pay = button("Pay", "pay")
        if (Build.VERSION.SDK_INT >= 34) {
            card.setAccessibilityDataSensitive(View.ACCESSIBILITY_DATA_SENSITIVE_YES)
            pay.setAccessibilityDataSensitive(View.ACCESSIBILITY_DATA_SENSITIVE_YES)
        }
        col(title("Sensitive"), card, pay, text("sdk ${Build.VERSION.SDK_INT}: " +
            if (Build.VERSION.SDK_INT >= 34) "marked data-sensitive" else "accessibilityDataSensitive needs API 34"))
    }

    fun hiddenA11y(s: Sc): View = with(s) {
        val hidden = col(text("Hidden section"), button("Hidden action", "hidden"), pad = 0).apply {
            importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO_HIDE_DESCENDANTS
        }
        col(title("Hidden from a11y"), button("Visible", "visible"), hidden)
    }

    fun password(s: Sc): View = with(s) {
        val user = edit("Username", "username")
        val pw = android.widget.EditText(act).apply {
            hint = "Password"; withId("password")
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
        }
        watch(pw, "password", secret = true)
        col(title("Sign in"), user, pw, button("Log in", "login"))
    }

    @SuppressLint("ClickableViewAccessibility")
    fun overlayBlocker(s: Sc): View = with(s) {
        val frame = FrameLayout(act)
        frame.addView(col(title("Shop"), button("Buy", "buy")))
        frame.addView(View(act).apply {
            withId("blocker")
            setBackgroundColor(Color.argb(1, 0, 0, 0))
            isClickable = true
            setOnTouchListener { _, e -> if (e.action == MotionEvent.ACTION_UP) dta("blocked", "blocker"); true }
            layoutParams = FrameLayout.LayoutParams(MATCH, MATCH)
        })
        frame
    }

    /** In a 400dp viewport: "Five" 5% visible at the top, "Full", "Thirty" 30% at the bottom, "Offscreen" far below. */
    fun partial(s: Sc): View = with(s) {
        fun btn(label: String, id: String, top: Int) = Button(act).apply {
            text = label; isAllCaps = false; withId(id)
            setOnClickListener { dta("click", id) }
            layoutParams = FrameLayout.LayoutParams(MATCH, dp(100)).apply { topMargin = dp(top) }
        }
        val inner = FrameLayout(act).apply {
            addView(btn("Five", "five", 0))
            addView(btn("Full", "full", 150))
            addView(btn("Thirty", "thirty", 465))
            addView(btn("Offscreen", "offscreen", 1500))
            addView(View(act).apply { layoutParams = FrameLayout.LayoutParams(1, dp(1700)) })
        }
        val sv = ScrollView(act).apply {
            withId("viewport"); addView(inner)
            layoutParams = LinearLayout.LayoutParams(MATCH, dp(400))
            setBackgroundColor(Color.rgb(245, 245, 245))
        }
        sv.post { sv.scrollTo(0, dp(95)) }
        col(title("Partial visibility"), sv)
    }

    fun underKeyboard(s: Sc): View = with(s) {
        act.window.setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_NOTHING or
            WindowManager.LayoutParams.SOFT_INPUT_STATE_ALWAYS_VISIBLE)
        val msg = edit("Message", "message")
        val submit = button("Submit", "submit")
        val frame = FrameLayout(act)
        frame.addView(col(title("Compose"), msg))
        frame.addView(submit, FrameLayout.LayoutParams(MATCH, dp(64), Gravity.BOTTOM))
        msg.post {
            msg.requestFocus()
            (act.getSystemService(InputMethodManager::class.java)).showSoftInput(msg, InputMethodManager.SHOW_IMPLICIT)
        }
        frame
    }

    fun alphaZero(s: Sc): View = with(s) {
        col(title("Alpha"),
            button("Ghost", "ghost").apply { alpha = 0f },
            button("Invisible", "invisible").apply { visibility = View.INVISIBLE },
            button("Gone", "gone").apply { visibility = View.GONE },
            button("Visible", "visible"))
    }

    fun zeroSize(s: Sc): View = with(s) {
        col(title("Zero size"),
            button("Zero", "zero").apply { layoutParams = LinearLayout.LayoutParams(0, 0) },
            View(act).apply {
                withId("onepx"); isClickable = true; contentDescription = "One pixel"
                setOnClickListener { dta("click", "onepx") }
                layoutParams = LinearLayout.LayoutParams(1, 1)
            },
            button("Normal", "normal"))
    }

    @Suppress("DEPRECATION")
    fun systemBars(s: Sc): View = with(s) {
        act.window.decorView.systemUiVisibility = View.SYSTEM_UI_FLAG_LAYOUT_STABLE or
            View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN or View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
        act.window.statusBarColor = Color.argb(128, 0, 0, 0)
        act.window.navigationBarColor = Color.argb(128, 0, 0, 0)
        val list = LinearLayout(act).apply { orientation = LinearLayout.VERTICAL }
        for (i in 1..40) list.addView(TextView(act).apply {
            text = "Edge row $i"; textSize = 18f; isClickable = true; withId("edge_row")
            setPadding(dp(16), dp(12), dp(16), dp(12))
            setOnClickListener { dta("click", "edge_row", "row" to i) }
        })
        ScrollView(act).apply { addView(list); clipToPadding = false }
    }
}
