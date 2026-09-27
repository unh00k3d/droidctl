package dev.droidctl.testapp

import android.content.res.Resources
import android.graphics.Color
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.ImageButton
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.core.view.ViewCompat
import org.json.JSONArray
import org.json.JSONObject

/** Logcat tag for ground-truth events (TESTAPP.md: tests read these, never droidctl's output). */
const val DTA = "DTA"

/** Log one ground-truth line: `DTA {"s":..,"ev":..,"id":..,"n":..,...}`. */
fun dtaLine(s: String, ev: String, id: String?, n: Int?, fields: Array<out Pair<String, Any?>>) {
    val o = JSONObject().put("s", s).put("ev", ev)
    if (id != null) o.put("id", id)
    if (n != null) o.put("n", n)
    for ((k, v) in fields) o.put(k, when (v) {
        null -> JSONObject.NULL
        is Collection<*> -> JSONArray(v)
        else -> v
    })
    Log.i(DTA, o.toString())
}

/**
 * Everything a scenario needs while it is on screen: extras, the DTA logger
 * with per-(event, id) counters, timers and cleanup. A scenario switch
 * disposes the old context, so no timer or window flag leaks into the next.
 */
class Sc(val act: Main, val name: String, private val extras: Bundle?) {
    private val handler = Handler(Looper.getMainLooper())
    private val disposers = mutableListOf<() -> Unit>()
    private val counts = HashMap<String, Int>()
    @Volatile var disposed = false
        private set

    val res: Resources get() = act.resources

    fun int(key: String, def: Int): Int = extras?.let {
        if (it.containsKey(key)) (it.get(key) as? Number)?.toInt() ?: it.getString(key)?.toIntOrNull() ?: def else def
    } ?: def

    fun str(key: String, def: String? = null): String? = extras?.getString(key) ?: def

    /** Ground truth. `n` counts this (event, id) pair since the scenario was shown. */
    fun dta(ev: String, id: String? = null, vararg fields: Pair<String, Any?>) {
        val key = "$ev/${id ?: ""}"
        val n = synchronized(counts) { (counts[key] ?: 0) + 1 }.also { synchronized(counts) { counts[key] = it } }
        dtaLine(name, ev, id, n, fields)
    }

    fun later(ms: Long, fn: () -> Unit) {
        val r = Runnable { if (!disposed) fn() }
        handler.postDelayed(r, ms)
        disposers += { handler.removeCallbacks(r) }
    }

    fun every(ms: Long, fn: () -> Unit) {
        val r = object : Runnable {
            override fun run() {
                if (disposed) return
                fn()
                handler.postDelayed(this, ms)
            }
        }
        handler.postDelayed(r, ms)
        disposers += { handler.removeCallbacks(r) }
    }

    fun onDispose(fn: () -> Unit) { disposers += fn }

    fun dispose() {
        disposed = true
        for (d in disposers.asReversed()) try { d() } catch (_: Exception) {}
        disposers.clear()
        handler.removeCallbacksAndMessages(null)
    }

    /** A resource id by name, so the a11y tree reports `viewIdResourceName` (ids.xml). */
    fun rid(name: String): Int {
        val id = res.getIdentifier(name, "id", act.packageName)
        require(id != 0) { "id '$name' is missing from res/values/ids.xml" }
        return id
    }

    fun dp(v: Int): Int = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v.toFloat(), res.displayMetrics).toInt()

    // ---- view helpers ----------------------------------------------------
    fun <T : View> T.withId(name: String?): T { if (name != null) id = rid(name); return this }

    fun col(vararg children: View, pad: Int = 16): LinearLayout = LinearLayout(act).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(dp(pad), dp(pad), dp(pad), dp(pad))
        children.forEach { addView(it) }
    }

    fun row(vararg children: View): LinearLayout = LinearLayout(act).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        children.forEach { addView(it) }
    }

    fun scroll(child: View): ScrollView = ScrollView(act).apply { addView(child) }

    fun text(s: String, id: String? = null, size: Float = 16f): TextView = TextView(act).apply {
        text = s
        textSize = size
        setPadding(0, dp(4), 0, dp(4))
    }.withId(id)

    fun title(s: String, id: String? = null): TextView = text(s, id, 22f).apply {
        ViewCompat.setAccessibilityHeading(this, true)
    }

    /** A Button that logs `click` with its id and runs [then]. */
    fun button(label: String, id: String, then: (() -> Unit)? = null): Button = Button(act).apply {
        text = label
        isAllCaps = false
        withId(id)
        setOnClickListener { dta("click", id); then?.invoke() }
    }

    /** An icon button; [desc] null means deliberately unlabeled. */
    fun icon(res: Int, desc: String?, id: String, then: (() -> Unit)? = null): ImageButton = ImageButton(act).apply {
        setImageResource(res)
        contentDescription = desc
        withId(id)
        setOnClickListener { dta("click", id); then?.invoke() }
    }

    fun edit(hint: String?, id: String): EditText = EditText(act).apply {
        this.hint = hint
        withId(id)
        watch(this, id)
    }

    /** Log `text` events with the full value after every change. */
    fun watch(e: EditText, id: String, secret: Boolean = false) {
        e.addTextChangedListener(object : android.text.TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
            override fun onTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
            override fun afterTextChanged(s: android.text.Editable?) {
                val v = s?.toString() ?: ""
                if (secret) dta("text", id, "len" to v.length, "value" to v) else dta("text", id, "value" to v)
            }
        })
    }

    fun space(h: Int): View = View(act).apply { layoutParams = ViewGroup.LayoutParams(1, dp(h)) }

    fun box(color: Int = Color.LTGRAY, w: Int, h: Int): View = View(act).apply {
        setBackgroundColor(color)
        layoutParams = LinearLayout.LayoutParams(dp(w), dp(h))
    }

    fun lp(w: Int, h: Int, weight: Float = 0f) = LinearLayout.LayoutParams(w, h, weight)
    val MATCH get() = ViewGroup.LayoutParams.MATCH_PARENT
    val WRAP get() = ViewGroup.LayoutParams.WRAP_CONTENT
}
