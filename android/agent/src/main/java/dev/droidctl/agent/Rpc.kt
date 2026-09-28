package dev.droidctl.agent

import android.hardware.display.DisplayManager
import android.os.Build
import android.os.SystemClock
import android.util.DisplayMetrics
import android.view.Display
import org.json.JSONArray
import org.json.JSONObject

/** The method table. Handlers run on the connection's thread and return a result object. */
class Rpc(private val host: Host, private val tree: Tree, private val events: Events,
          /** backend B only: ends the process (the host's `teardown`, or a clean restart) */
          private val onShutdown: (() -> Unit)? = null) {
    private val actions = Actions(host, tree, events) { screen() }

    companion object {
        const val PROTOCOL = 3
    }

    fun call(method: String, params: JSONObject, c: Conn): JSONObject = when (method) {
        "ping" -> ping(c)
        "echo" -> params  // transport-only round trip: no device work (benchmarks, health)
        "gen" -> JSONObject().put("gen", tree.gen)
        "tree" -> tree.dump(params.optBoolean("not_important", false),
                            params.optBoolean("windows", true), screen(),
                            params.optLong("budget_ms", Tree.BUDGET_MS).coerceIn(100, 30000),
                            params.optInt("max_nodes", Tree.MAX_NODES).coerceIn(1, Tree.MAX_NODES))
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
        "shutdown" -> {
            val stop = onShutdown ?: throw RpcError(Codes.UNSUPPORTED, "the ${host.backend} backend is stopped by disabling its service")
            Thread({ SystemClock.sleep(100); stop() }, "droidctl-shutdown").start()   // after the reply is written
            JSONObject().put("stopping", true)
        }
        else -> throw RpcError(Codes.METHOD_NOT_FOUND, "method not found: $method")
    }

    /** The connection closed: drop its subscriptions. */
    fun closed(c: Conn) { events.unsubscribe(c) }

    // Fixed for the life of the process, and looking it up costs binder calls:
    // measured ~5 ms per ping (the version, ~3 ms, is cached by the Host).
    private val display: Display by lazy {
        host.context.getSystemService(DisplayManager::class.java).getDisplay(Display.DEFAULT_DISPLAY)
    }

    private fun ping(c: Conn): JSONObject {
        val (versionName, versionCode) = host.version()
        return JSONObject()
            .put("protocol", PROTOCOL)
            .put("version", versionName)
            .put("versionCode", versionCode)
            .put("backend", host.backend)
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

    private val power by lazy { host.context.getSystemService(android.os.PowerManager::class.java) }
    private val keyguard by lazy { host.context.getSystemService(android.app.KeyguardManager::class.java) }

    /**
     * Real (logical) display size in px: reflects `wm size`/`wm density` overrides.
     * `on`/`locked`: accessibility actions are not user activity, so a session of node
     * taps lets the screen time out (measured on the SM-N950F: off after its 10 min
     * timeout mid-benchmark, then every locator failed as not-found).
     */
    private fun screen(): JSONObject {
        val m = DisplayMetrics()
        @Suppress("DEPRECATION")
        display.getRealMetrics(m)
        val o = JSONObject()
            .put("w", m.widthPixels)
            .put("h", m.heightPixels)
            .put("density", m.densityDpi)
            .put("rotation", display.rotation)
        try { o.put("on", power.isInteractive) } catch (_: Exception) {}
        try { o.put("locked", keyguard.isKeyguardLocked) } catch (_: Exception) {}
        return o
    }
}
