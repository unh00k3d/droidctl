package dev.droidctl.agent

import android.content.Context
import android.hardware.display.DisplayManager
import android.os.Build
import android.os.SystemClock
import android.util.DisplayMetrics
import android.view.Display
import org.json.JSONObject

/** The method table. Handlers run on the connection's thread and return a result object. */
class Rpc(private val ctx: Context) {
    class Ctx(val peerUid: Int)

    companion object {
        const val PROTOCOL = 1
    }

    fun call(method: String, params: JSONObject, c: Ctx): JSONObject = when (method) {
        "ping" -> ping(c)
        else -> throw RpcError(Codes.METHOD_NOT_FOUND, "method not found: $method")
    }

    private fun ping(c: Ctx): JSONObject {
        val pi = ctx.packageManager.getPackageInfo(ctx.packageName, 0)
        @Suppress("DEPRECATION")
        val versionCode = if (Build.VERSION.SDK_INT >= 28) pi.longVersionCode else pi.versionCode.toLong()
        return JSONObject()
            .put("protocol", PROTOCOL)
            .put("version", pi.versionName)
            .put("versionCode", versionCode)
            .put("sdk", Build.VERSION.SDK_INT)
            .put("release", Build.VERSION.RELEASE)
            .put("manufacturer", Build.MANUFACTURER)
            .put("model", Build.MODEL)
            .put("device", Build.DEVICE)
            .put("screen", screen())
            .put("service", JSONObject().put("connected", true))
            .put("gen", 0)  // content-generation counter: a placeholder until `tree` lands (M2)
            .put("peer_uid", c.peerUid)
            .put("uptime_ms", SystemClock.uptimeMillis())
    }

    /** Real (logical) display size in px: reflects `wm size`/`wm density` overrides. */
    private fun screen(): JSONObject {
        val dm = ctx.getSystemService(DisplayManager::class.java)
        val display = dm.getDisplay(Display.DEFAULT_DISPLAY)
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
