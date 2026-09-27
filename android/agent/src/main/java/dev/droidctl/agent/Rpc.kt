package dev.droidctl.agent

import android.hardware.display.DisplayManager
import android.os.Build
import android.os.SystemClock
import android.util.DisplayMetrics
import android.view.Display
import org.json.JSONArray
import org.json.JSONObject

/** The method table. Handlers run on the connection's thread and return a result object. */
class Rpc(private val ctx: AgentService, private val tree: Tree, private val events: Events) {
    private val actions = Actions(ctx, tree, events) { screen() }

    companion object {
        const val PROTOCOL = 2
    }

    fun call(method: String, params: JSONObject, c: Conn): JSONObject = when (method) {
        "ping" -> ping(c)
        "echo" -> params  // transport-only round trip: no device work (benchmarks, health)
        "gen" -> JSONObject().put("gen", tree.gen)
        "tree" -> tree.dump(params.optBoolean("not_important", false),
                            params.optBoolean("windows", true), screen())
        "act" -> actions.act(params)
        "gesture" -> actions.gesture(params)
        "global" -> actions.global(params)
        "wait_idle" -> actions.waitIdle(params)
        "wait_for" -> actions.waitFor(params)
        "current" -> actions.current()
        "screenshot" -> actions.screenshot(params)
        "clipboard" -> actions.clipboard(params)
        "events" -> {
            val since = params.optLong("since", 0)
            val arr = JSONArray()
            for (e in events.since(since, params.optInt("limit", Events.CAP))) arr.put(e.json)
            JSONObject().put("events", arr).put("next", events.nextSeq - 1)
        }
        "subscribe" -> {
            val types = params.optJSONArray("events")?.let { a -> (0 until a.length()).map { a.getString(it) }.toSet() }
            events.subscribe(c, types?.ifEmpty { null })
            JSONObject().put("subscribed", types?.let { JSONArray(it.toList()) } ?: "all").put("next", events.nextSeq - 1)
        }
        "unsubscribe" -> JSONObject().put("unsubscribed", events.unsubscribe(c))
        else -> throw RpcError(Codes.METHOD_NOT_FOUND, "method not found: $method")
    }

    /** The connection closed: drop its subscriptions. */
    fun closed(c: Conn) { events.unsubscribe(c) }

    // Both are fixed for the life of the process (an APK upgrade restarts it), and
    // looking them up costs binder calls: measured ~3 ms and ~5 ms per ping.
    private val pkg by lazy { ctx.packageManager.getPackageInfo(ctx.packageName, 0) }
    private val display: Display by lazy {
        ctx.getSystemService(DisplayManager::class.java).getDisplay(Display.DEFAULT_DISPLAY)
    }

    private fun ping(c: Conn): JSONObject {
        @Suppress("DEPRECATION")
        val versionCode = if (Build.VERSION.SDK_INT >= 28) pkg.longVersionCode else pkg.versionCode.toLong()
        return JSONObject()
            .put("protocol", PROTOCOL)
            .put("version", pkg.versionName)
            .put("versionCode", versionCode)
            .put("sdk", Build.VERSION.SDK_INT)
            .put("release", Build.VERSION.RELEASE)
            .put("manufacturer", Build.MANUFACTURER)
            .put("model", Build.MODEL)
            .put("device", Build.DEVICE)
            .put("screen", screen())
            .put("service", JSONObject().put("connected", true))
            .put("gen", tree.gen)
            .put("peer_uid", c.uid)
            .put("uptime_ms", SystemClock.uptimeMillis())
    }

    /** Real (logical) display size in px: reflects `wm size`/`wm density` overrides. */
    private fun screen(): JSONObject {
        val m = DisplayMetrics()
        @Suppress("DEPRECATION")
        display.getRealMetrics(m)
        return JSONObject()
            .put("w", m.widthPixels)
            .put("h", m.heightPixels)
            .put("density", m.densityDpi)
            .put("rotation", display.rotation)
    }
}
