package dev.droidctl.testapp

import android.content.Intent
import android.graphics.Color
import android.os.Bundle
import android.view.View
import android.view.WindowManager
import android.widget.LinearLayout
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity

/**
 * The only activity. It reads the scenario from `--es s NAME` or from the deep
 * link `droidctl-test://s/NAME`, builds that screen and logs `shown`. With no
 * scenario it lists them all; `s=__list__` dumps the registry to logcat.
 */
class Main : AppCompatActivity() {
    private var sc: Sc? = null
    /** Set by the `permission` scenario. */
    var permissionListener: ((Boolean) -> Unit)? = null
    private var baseSoftInput = 0

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        baseSoftInput = window.attributes.softInputMode
        show(intent, recreated = savedInstanceState != null)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        if (intent.getBooleanExtra("reset", false)) {
            // a hard reset: a fresh activity in a fresh task, so no dialog, popup
            // or view state from the previous scenario can survive
            sc?.dispose()
            startActivity(Intent(intent).setClass(this, Main::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK))
            overridePendingTransition(0, 0)
            return
        }
        setIntent(intent)
        show(intent, recreated = false)
    }

    override fun onRequestPermissionsResult(code: Int, perms: Array<out String>, results: IntArray) {
        super.onRequestPermissionsResult(code, perms, results)
        permissionListener?.invoke(results.isNotEmpty() && results[0] == android.content.pm.PackageManager.PERMISSION_GRANTED)
    }

    override fun onDestroy() {
        sc?.dispose()
        super.onDestroy()
    }

    private fun scenarioOf(intent: Intent): String? =
        intent.getStringExtra("s") ?: intent.data?.takeIf { it.scheme == "droidctl-test" }?.pathSegments?.firstOrNull()

    private fun show(intent: Intent, recreated: Boolean) {
        sc?.dispose()
        resetWindow()
        val name = scenarioOf(intent)
        if (name == "__list__") {
            dumpRegistry()
            finish()
            return
        }
        if (intent.getBooleanExtra("reset", false)) {
            getSharedPreferences("state", MODE_PRIVATE).edit().clear().apply()
            dtaLine(name ?: "", "reset", null, null, emptyArray())
        }
        val spec = name?.let { Scenarios.byName[it] }
        if (name != null && spec == null) {
            dtaLine(name, "unknown_scenario", null, null, emptyArray())
            setContentView(TextView(this).apply { text = "unknown scenario: $name" })
            return
        }
        // capture check: the a11y window title is "s:<scenario>" (Activity title -> window title)
        title = "s:" + (spec?.name ?: "__index__")
        val s = Sc(this, spec?.name ?: "__index__", intent.extras)
        sc = s
        val view = if (spec == null) index(s) else Scenarios.build(spec.name, s)
        setContentView(view)
        view.post { if (!s.disposed) s.dta("shown", null, "recreated" to recreated) }
    }

    /** Undo whatever the previous scenario did to the window. */
    private fun resetWindow() {
        window.clearFlags(WindowManager.LayoutParams.FLAG_SECURE)
        window.setSoftInputMode(baseSoftInput)
        @Suppress("DEPRECATION")
        window.decorView.systemUiVisibility = 0
        window.decorView.layoutDirection = View.LAYOUT_DIRECTION_INHERIT
        window.statusBarColor = Color.BLACK
        window.navigationBarColor = Color.BLACK
    }

    private fun index(s: Sc): View {
        val list = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        with(s) {
            list.addView(title("droidctl test app"))
            for (spec in Scenarios.specs) {
                list.addView(TextView(this@Main).apply {
                    text = "${spec.group}. ${spec.name}  (${spec.toolkit})"
                    textSize = 16f
                    setPadding(dp(16), dp(10), dp(16), dp(10))
                    setOnClickListener {
                        startActivity(Intent(this@Main, Main::class.java).putExtra("s", spec.name))
                    }
                })
            }
            return scroll(list)
        }
    }

    private fun dumpRegistry() {
        for (spec in Scenarios.specs) {
            dtaLine("__list__", "scenario", null, null, arrayOf(
                "name" to spec.name, "group" to spec.group, "toolkit" to spec.toolkit,
                "events" to spec.events, "desc" to spec.desc))
        }
        dtaLine("__list__", "list_end", null, null, arrayOf("count" to Scenarios.specs.size))
    }
}
