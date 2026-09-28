package dev.droidctl.agent

import android.accessibilityservice.AccessibilityService
import android.annotation.TargetApi
import android.content.ClipData
import android.graphics.Bitmap
import android.graphics.Rect
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Base64
import android.view.Display
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityWindowInfo
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

/**
 * Everything that changes the screen or waits for it: act, gesture, global,
 * wait_idle, wait_for, plus current, screenshot and clipboard.
 * Waits are event-driven (Events.lock), never polling sleeps.
 */
class Actions(private val svc: Host, private val tree: Tree, private val events: Events,
              private val screen: () -> JSONObject) {
    companion object {
        const val DEFAULT_QUIET_MS = 150L
        const val DEFAULT_SETTLE_TIMEOUT_MS = 2000L
        // measured click-event latency on the SM-N950F: 80-300 ms (bench/results/m5-device.json);
        // both waits end as soon as the event arrives, so a generous cap only costs when nothing happens
        const val DEFAULT_EVENT_MS = 600L
        const val DEFAULT_FIRST_MS = 600L
        const val MAX_EVENTS = 50

        private val BY_NAME: Map<String, Int> = Tree.STANDARD.entries.associate { (id, name) -> name to id }

        /** Actions that make sense on a node that is not (fully) on screen. */
        private val OFFSCREEN_OK = setOf("show_on_screen", "scroll_forward", "scroll_backward",
            "scroll_up", "scroll_down", "scroll_left", "scroll_right", "scroll_to_position", "focus", "clear_focus")

        val GLOBALS: Map<String, Int> = mapOf(
            "back" to AccessibilityService.GLOBAL_ACTION_BACK,
            "home" to AccessibilityService.GLOBAL_ACTION_HOME,
            "recents" to AccessibilityService.GLOBAL_ACTION_RECENTS,
            "notifications" to AccessibilityService.GLOBAL_ACTION_NOTIFICATIONS,
            "quick_settings" to AccessibilityService.GLOBAL_ACTION_QUICK_SETTINGS,
            "power_dialog" to AccessibilityService.GLOBAL_ACTION_POWER_DIALOG,
            "split" to AccessibilityService.GLOBAL_ACTION_TOGGLE_SPLIT_SCREEN,
            "lock" to 8,        // GLOBAL_ACTION_LOCK_SCREEN, API 28
            "screenshot" to 9,  // GLOBAL_ACTION_TAKE_SCREENSHOT, API 28
        )
        private val GLOBAL_MIN_SDK = mapOf("lock" to 28, "screenshot" to 28)
    }

    // ------------------------------------------------------------------ act
    fun act(p: JSONObject): JSONObject {
        val dumpId = p.optInt("dump", -1)
        val handle = p.optInt("handle", -1)
        if (dumpId < 0 || handle < 0) throw RpcError(Codes.INVALID_PARAMS, "act needs dump and handle")
        val args = p.optJSONObject("args") ?: JSONObject()
        val node = tree.node(dumpId, handle)
        if (!node.refresh()) throw RpcError(Codes.GONE, "the node for handle $handle is gone")

        val (actionId, label) = resolveAction(p, node)
        if (!p.optBoolean("force", false)) {
            if (!node.isEnabled) throw RpcError(Codes.DISABLED, "the node is disabled")
            if (!node.isVisibleToUser && label !in OFFSCREEN_OK)
                throw RpcError(Codes.NOT_VISIBLE, "the node is not visible on screen")
        }
        val bundle = bundle(label, args)
        val startSeq = events.nextSeq - 1
        val t0 = SystemClock.uptimeMillis()
        val performed = node.performAction(actionId, bundle)
        val out = JSONObject().put("performed", performed).put("action", label)
            .put("perform_ms", SystemClock.uptimeMillis() - t0).put("t0", t0)
        if (!performed) {
            out.put("available", availableActions(node))
            return out
        }
        val isClick = label == "click" || label == "long_click"
        // settle dumps a new tree, which recycles this dump's nodes (API < 33 clears them):
        // compare click events against a private copy
        val ref = if (isClick) copy(node) else null
        val settle = p.optJSONObject("settle")
        if (settle == null && isClick) {
            // wait (event-driven) just long enough to see whether the view handled it
            val eventMs = p.optLong("event_ms", DEFAULT_EVENT_MS)
            events.awaitEvent(startSeq, t0 + eventMs) { it.clickedBy(ref!!, label) }
        }
        settleInto(out, settle, t0)
        val evs = events.since(startSeq, MAX_EVENTS)
        if (ref != null) {
            out.put("clicked_event", evs.any { it.clickedBy(ref, label) })
            Tree.recycle(ref)
        }
        out.put("events", toJson(evs))
        return out
    }

    private fun Events.Ev.clickedBy(node: AccessibilityNodeInfo, label: String): Boolean {
        val want = if (label == "long_click") "long_clicked" else "clicked"
        if (type != want) return false
        if (source != null && source == node) return true
        // equals() compares internal ids that do not always survive refresh(): fall back to window + bounds
        val b = json.optJSONArray("bounds") ?: return false
        if (json.optInt("window", -1) != node.windowId) return false
        val r = Rect()
        node.getBoundsInScreen(r)
        return b.optInt(0) == r.left && b.optInt(1) == r.top && b.optInt(2) == r.right && b.optInt(3) == r.bottom
    }

    @Suppress("DEPRECATION")
    private fun copy(n: AccessibilityNodeInfo): AccessibilityNodeInfo =
        if (Build.VERSION.SDK_INT >= 33) AccessibilityNodeInfo(n) else AccessibilityNodeInfo.obtain(n)

    /** The front activity: the one owning the active application window, else the last window-state event's. */
    private fun frontActivity(): String? {
        val ws = try { svc.windows() } catch (_: Exception) { emptyList() }
        val active = ws.firstOrNull { it.isActive && it.type == AccessibilityWindowInfo.TYPE_APPLICATION }
            ?: ws.firstOrNull { it.isFocused && it.type == AccessibilityWindowInfo.TYPE_APPLICATION }
        return active?.let { events.activityOf(it.id) } ?: events.curActivity
    }

    /** -> (action id, readable label). A standard name, or a custom action by label or id. */
    private fun resolveAction(p: JSONObject, node: AccessibilityNodeInfo): Pair<Int, String> {
        val custom = p.opt("custom")
        if (custom != null && custom != JSONObject.NULL) {
            val list = node.actionList ?: emptyList()
            val hit = when (custom) {
                is Number -> list.firstOrNull { it.id == custom.toInt() }
                else -> list.firstOrNull { it.label?.toString()?.equals(custom.toString(), ignoreCase = true) == true }
            } ?: throw RpcError(Codes.UNSUPPORTED, "no custom action '$custom' on this node; it has ${availableActions(node)}")
            return hit.id to (hit.label?.toString() ?: "custom:${hit.id}")
        }
        val name = p.optString("action", "")
        if (name.isEmpty()) throw RpcError(Codes.INVALID_PARAMS, "act needs action (a standard name) or custom (label or id)")
        if (name == "ime_enter" && Build.VERSION.SDK_INT < 30)
            throw RpcError(Codes.UNSUPPORTED, "ime_enter needs API 30 (this is ${Build.VERSION.SDK_INT}); use keyevent 66")
        val id = BY_NAME[name] ?: throw RpcError(Codes.UNSUPPORTED, "unknown action '$name' on API ${Build.VERSION.SDK_INT}")
        return id to name
    }

    private fun bundle(label: String, a: JSONObject): Bundle? = when (label) {
        "set_text" -> Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, a.optString("text", ""))
        }
        "set_selection" -> Bundle().apply {
            putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT, a.optInt("start", 0))
            putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT, a.optInt("end", a.optInt("start", 0)))
        }
        "set_progress" -> Bundle().apply {
            if (!a.has("value")) throw RpcError(Codes.INVALID_PARAMS, "set_progress needs args.value")
            putFloat(AccessibilityNodeInfo.ACTION_ARGUMENT_PROGRESS_VALUE, a.getDouble("value").toFloat())
        }
        "scroll_to_position" -> Bundle().apply {
            putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_ROW_INT, a.optInt("row", 0))
            putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_COLUMN_INT, a.optInt("col", 0))
        }
        else -> null
    }

    private fun availableActions(node: AccessibilityNodeInfo): JSONArray {
        val arr = JSONArray()
        for (a in node.actionList ?: emptyList()) {
            val n = Tree.STANDARD[a.id]
            when {
                n != null -> arr.put(n)
                a.label != null -> arr.put(a.label.toString())
            }
        }
        return arr
    }

    /**
     * With `settle`, waits for quiet and puts {idle, settle_ms, tree} into `out`.
     *
     * Quiet alone is not enough: mid-transition Android can go quiet for longer
     * than the quiet window while an app window has no root yet (a dialog being
     * added after `back`: WINDOWS_CHANGED, ~190 ms of nothing, then the dialog's
     * WINDOW_STATE) or a root with no children (a window not drawn yet, e.g. a
     * cold launch). So the settled tree is checked too: while it shows such a
     * window, wait for the next event (up to first_ms) and quiet again, then
     * re-dump, all within timeout_ms. A settled normal screen pays nothing extra.
     */
    private fun settleInto(out: JSONObject, settle: JSONObject?, t0: Long) {
        if (settle == null) return
        val quiet = settle.optLong("quiet_ms", DEFAULT_QUIET_MS)
        val timeout = settle.optLong("timeout_ms", DEFAULT_SETTLE_TIMEOUT_MS)
        val first = settle.optLong("first_ms", DEFAULT_FIRST_MS)
        var idle = if (quiet <= 0) true else events.waitSettled(t0, quiet, first, timeout)
        if (!settle.optBoolean("tree", true)) {
            out.put("idle", idle).put("settle_ms", SystemClock.uptimeMillis() - t0).put("t0", t0)
            return
        }
        val ni = settle.optBoolean("not_important", false)
        var settledAt = SystemClock.uptimeMillis()     // settle_ms excludes the dump itself
        var t = tree.dump(ni, true, screen())
        var redumps = 0
        while (quiet > 0 && transitional(t)) {
            val left = t0 + timeout - SystemClock.uptimeMillis()
            if (left <= 0) { idle = false; break }
            val mark = SystemClock.uptimeMillis()
            idle = events.waitSettled(mark, quiet, minOf(first, left), left)
            settledAt = SystemClock.uptimeMillis()
            t = tree.dump(ni, true, screen())
            redumps += 1
        }
        out.put("idle", idle).put("settle_ms", settledAt - t0).put("t0", t0)
        if (redumps > 0) out.put("redumps", redumps)
        out.put("tree", t)
    }

    /** An application window with no root, or a root with no children: not drawn yet. */
    private fun transitional(t: JSONObject): Boolean {
        val ws = t.optJSONArray("windows") ?: return false
        for (i in 0 until ws.length()) {
            val w = ws.getJSONObject(i)
            if (w.optString("type") != "application") continue
            if (w.optBoolean("no_root")) return true
            val root = w.optJSONObject("root") ?: continue
            if (!root.has("children") && !root.optBoolean("truncated")) return true
        }
        return false
    }

    private fun toJson(evs: List<Events.Ev>): JSONArray {
        val arr = JSONArray()
        for (e in evs) arr.put(e.json)
        return arr
    }

    // ------------------------------------------------------------------ gesture
    fun gesture(p: JSONObject): JSONObject {
        val type = p.optString("type", "tap")
        val pts = points(p.optJSONArray("points"))
        fun need(n: Int) { if (pts.size < n) throw RpcError(Codes.INVALID_PARAMS, "$type needs at least $n points") }
        val strokes = when (type) {
            "tap", "long" -> {
                need(1)
                listOf(Stroke(pts.subList(0, 1), 0, p.optLong("ms", if (type == "tap") 60 else 800)))
            }
            "double" -> {
                need(1)
                val ms = p.optLong("ms", 50)
                listOf(Stroke(pts.subList(0, 1), 0, ms), Stroke(pts.subList(0, 1), ms + 100, ms))
            }
            "swipe", "path" -> {
                need(2)
                listOf(Stroke(pts, 0, p.optLong("ms", if (type == "swipe") 300 else 500)))
            }
            "pinch" -> {
                need(4)
                val ms = p.optLong("ms", 400)
                listOf(Stroke(pts.subList(0, 2), 0, ms), Stroke(pts.subList(2, 4), 0, ms))
            }
            else -> throw RpcError(Codes.INVALID_PARAMS, "unknown gesture type '$type' (tap|long|double|swipe|path|pinch)")
        }
        val total = strokes.maxOf { it.startMs + it.durationMs }
        val startSeq = events.nextSeq - 1
        val t0 = SystemClock.uptimeMillis()
        if (!svc.runGesture(strokes, total + 3000)) throw RpcError(Codes.CANCELLED, "the gesture was cancelled (another gesture or a touch interrupted it)")
        val out = JSONObject().put("performed", true).put("type", type).put("ms", SystemClock.uptimeMillis() - t0)
        settleInto(out, p.optJSONObject("settle"), t0)
        out.put("events", toJson(events.since(startSeq, MAX_EVENTS)))
        return out
    }

    private fun points(arr: JSONArray?): List<FloatArray> {
        if (arr == null) return emptyList()
        return (0 until arr.length()).map { i ->
            val pt = arr.optJSONArray(i) ?: throw RpcError(Codes.INVALID_PARAMS, "points must be [[x,y],…]")
            val x = pt.optDouble(0, Double.NaN).toFloat()
            val y = pt.optDouble(1, Double.NaN).toFloat()
            if (x.isNaN() || y.isNaN() || x < 0 || y < 0)
                throw RpcError(Codes.INVALID_PARAMS, "bad point ${pt}: device px, non-negative")
            floatArrayOf(x, y)
        }
    }

    // ------------------------------------------------------------------ global
    fun global(p: JSONObject): JSONObject {
        val name = p.optString("name", "")
        val id = GLOBALS[name] ?: throw RpcError(Codes.INVALID_PARAMS, "unknown global '$name' (${GLOBALS.keys.joinToString("|")})")
        val min = GLOBAL_MIN_SDK[name] ?: 0
        if (Build.VERSION.SDK_INT < min) throw RpcError(Codes.UNSUPPORTED, "global $name needs API $min")
        val startSeq = events.nextSeq - 1
        val t0 = SystemClock.uptimeMillis()
        val performed = svc.global(id)
        val out = JSONObject().put("performed", performed).put("name", name)
        if (performed) settleInto(out, p.optJSONObject("settle"), t0)
        out.put("events", toJson(events.since(startSeq, MAX_EVENTS)))
        return out
    }

    // ------------------------------------------------------------------ waits
    fun waitIdle(p: JSONObject): JSONObject {
        val startSeq = events.nextSeq - 1
        val t0 = SystemClock.uptimeMillis()
        val quiet = p.optLong("quiet_ms", DEFAULT_QUIET_MS)
        // quiet since the last change, even one before this call: "is it idle now?"
        val idle = events.waitQuiet(0, quiet, p.optLong("timeout_ms", DEFAULT_SETTLE_TIMEOUT_MS))
        return JSONObject().put("idle", idle).put("ms", SystemClock.uptimeMillis() - t0)
            .put("events", toJson(events.since(startSeq, MAX_EVENTS)))
    }

    /**
     * Blocks on the device until the condition holds. Node conditions (text/id/desc,
     * optionally `gone`) are re-checked when events arrive, at most every 100 ms.
     */
    fun waitFor(p: JSONObject): JSONObject {
        val t0 = SystemClock.uptimeMillis()
        val deadline = t0 + p.optLong("timeout_ms", 5000)
        val since = if (p.has("since")) p.getLong("since") else events.nextSeq - 1
        val text = p.optString("text").ifEmpty { null }
        val id = p.optString("id").ifEmpty { null }
        val desc = p.optString("desc").ifEmpty { null }
        val gone = p.optBoolean("gone", false)
        val exact = p.optBoolean("exact", false)
        val activity = p.optString("activity").ifEmpty { null }
        val toast = if (p.has("toast")) p.optString("toast") else null
        val window = p.optString("window").ifEmpty { null }
        val pkg = p.optString("pkg").ifEmpty { null }
        val nodeCond = text != null || id != null || desc != null
        if (!nodeCond && activity == null && toast == null && window == null)
            throw RpcError(Codes.INVALID_PARAMS, "wait_for needs text, id, desc, activity, toast or window")

        var lastCheck = 0L
        while (true) {
            val seen = events.ticks        // read before checking, so no event slips between
            val now = SystemClock.uptimeMillis()
            val match = JSONObject()
            var ok = true
            if (nodeCond) {
                val hit = findNode(text, id, desc, exact, pkg)
                ok = if (gone) hit == null else hit != null
                if (hit != null && !gone) match.put("node", hit)
                lastCheck = now
            }
            if (activity != null && ok) {
                val cur = frontActivity()
                ok = cur != null && (cur == activity || cur.endsWith(activity))
                if (ok) match.put("activity", cur)
            }
            if (toast != null && ok) {
                val ev = events.since(since).lastOrNull { it.type == "toast" &&
                        (toast.isEmpty() || it.json.optString("text").contains(toast, ignoreCase = true)) }
                ok = ev != null
                if (ev != null) match.put("toast", ev.json)
            }
            if (window != null && ok) {
                val w = findWindow(window)
                ok = w != null
                if (w != null) match.put("window", w)
            }
            if (ok) return match.put("matched", true).put("ms", now - t0)
            if (now >= deadline) throw RpcError(Codes.TIMEOUT, "wait_for: condition not met within ${deadline - t0} ms")
            // block until an event arrives; re-check at least every second as a safety net
            events.awaitTick(seen, minOf(deadline, now + 1000))
            // a busy screen emits events constantly: walk the tree at most every 100 ms
            if (nodeCond) {
                val wait = minOf(lastCheck + 100, deadline) - SystemClock.uptimeMillis()
                if (wait > 0) SystemClock.sleep(wait)
            }
        }
    }

    /** First visible node matching all given criteria, as a small summary, or null. */
    private fun findNode(text: String?, id: String?, desc: String?, exact: Boolean, pkg: String?): JSONObject? {
        fun m(v: CharSequence?, want: String?): Boolean {
            if (want == null) return true
            val s = v?.toString() ?: return false
            return if (exact) s == want else s.contains(want, ignoreCase = true)
        }
        fun idOk(v: String?): Boolean {
            if (id == null) return true
            v ?: return false
            return v == id || v.endsWith(":id/$id")
        }
        val rect = Rect()
        var budget = 5000
        fun walk(n: AccessibilityNodeInfo, depth: Int): JSONObject? {
            if (--budget < 0 || depth > 120) return null
            if (n.isVisibleToUser && m(n.text, text) && m(n.contentDescription, desc) && idOk(n.viewIdResourceName)
                && (text != null || desc != null || id != null)) {
                n.getBoundsInScreen(rect)
                val o = JSONObject()
                n.text?.let { o.put("text", it.toString()) }
                n.contentDescription?.let { o.put("desc", it.toString()) }
                n.viewIdResourceName?.let { o.put("id", it) }
                n.className?.let { o.put("class", it.toString()) }
                return o.put("bounds", JSONArray().put(rect.left).put(rect.top).put(rect.right).put(rect.bottom))
            }
            for (i in 0 until n.childCount) {
                val c = try { n.getChild(i) } catch (_: Exception) { null } ?: continue
                walk(c, depth + 1)?.let { return it }
            }
            return null
        }
        for (root in roots()) {
            if (pkg != null && root.packageName?.toString() != pkg) continue
            walk(root, 0)?.let { return it }
        }
        return null
    }

    private fun roots(): List<AccessibilityNodeInfo> {
        val ws = try { svc.windows() } catch (_: Exception) { emptyList() }
        val out = ws.mapNotNull { try { it.root } catch (_: Exception) { null } }
        if (out.isNotEmpty()) return out
        return listOfNotNull(svc.activeRoot())
    }

    private fun findWindow(want: String): JSONObject? {
        for (w in try { svc.windows() } catch (_: Exception) { emptyList() }) {
            val title = if (Build.VERSION.SDK_INT >= 24) w.title?.toString() else null
            val pkg = try { w.root?.packageName?.toString() } catch (_: Exception) { null }
            if ((title != null && title.contains(want, ignoreCase = true)) || pkg == want)
                return windowSummary(w, pkg)
        }
        return null
    }

    private fun windowSummary(w: AccessibilityWindowInfo, pkg: String?): JSONObject {
        val o = JSONObject().put("id", w.id).put("type", Tree.windowType(w.type)).put("layer", w.layer)
        if (Build.VERSION.SDK_INT >= 24) w.title?.let { o.put("title", it.toString()) }
        if (pkg != null) o.put("pkg", pkg)
        if (w.isActive) o.put("active", true)
        if (w.isFocused) o.put("focused", true)
        return o
    }

    // ------------------------------------------------------------------ current
    fun current(): JSONObject {
        val ws = try { svc.windows() } catch (_: Exception) { emptyList() }
        val arr = JSONArray()
        var activePkg: String? = null
        var keyboard = false
        for (w in ws) {
            val pkg = try { w.root?.packageName?.toString() } catch (_: Exception) { null }
            if (w.type == AccessibilityWindowInfo.TYPE_INPUT_METHOD) keyboard = true
            if (w.isActive && w.type == AccessibilityWindowInfo.TYPE_APPLICATION) activePkg = pkg
            arr.put(windowSummary(w, pkg))
        }
        if (activePkg == null) activePkg = svc.activeRoot()?.packageName?.toString()
        val o = JSONObject().put("keyboard", keyboard).put("windows", arr).put("gen", tree.gen)
        activePkg?.let { o.put("pkg", it) }
        // the activity is only known from window-state events; report it only if it belongs to the front package
        frontActivity()?.let { if (events.curPkg == activePkg || activePkg == null || it.startsWith(activePkg)) o.put("activity", it) }
        return o
    }

    // ------------------------------------------------------------------ screenshot
    fun screenshot(p: JSONObject): JSONObject {
        if (Build.VERSION.SDK_INT < 30)
            throw RpcError(Codes.UNSUPPORTED, "takeScreenshot needs API 30 (this is ${Build.VERSION.SDK_INT}); use screencap")
        // UiAutomation's capture differs in how it treats FLAG_SECURE windows; the host's
        // screencap path detects those, so backend B uses it rather than guessing
        val a11y = (svc as? ServiceHost)?.service
            ?: throw RpcError(Codes.UNSUPPORTED, "the ${svc.backend} backend has no screenshot; use screencap")
        return screenshot30(a11y, p)
    }

    @TargetApi(30)
    private fun screenshot30(svc: AgentService, p: JSONObject): JSONObject {
        val scale = p.optDouble("scale", 0.5).coerceIn(0.05, 1.0)
        val quality = p.optInt("quality", 70).coerceIn(1, 100)
        var lastErr = 0
        for (attempt in 0..2) {
            val done = CountDownLatch(1)
            val shot = AtomicReference<AccessibilityService.ScreenshotResult?>(null)
            svc.takeScreenshot(Display.DEFAULT_DISPLAY, { it.run() }, object : AccessibilityService.TakeScreenshotCallback {
                override fun onSuccess(r: AccessibilityService.ScreenshotResult) { shot.set(r); done.countDown() }
                override fun onFailure(code: Int) { lastErr = code; done.countDown() }
            })
            if (!done.await(5, TimeUnit.SECONDS)) throw RpcError(Codes.TIMEOUT, "takeScreenshot did not answer")
            val r = shot.get()
            if (r != null) return encode(r, p.optJSONArray("crop"), scale, quality)
            when (lastErr) {
                AccessibilityService.ERROR_TAKE_SCREENSHOT_SECURE_WINDOW ->
                    throw RpcError(Codes.SECURE_WINDOW, "a secure (FLAG_SECURE) window is on screen")
                AccessibilityService.ERROR_TAKE_SCREENSHOT_INTERVAL_TIME_SHORT -> SystemClock.sleep(350)
                else -> throw RpcError(Codes.INTERNAL, "takeScreenshot failed with code $lastErr")
            }
        }
        throw RpcError(Codes.INTERNAL, "takeScreenshot kept failing (code $lastErr)")
    }

    @TargetApi(30)
    private fun encode(r: AccessibilityService.ScreenshotResult, crop: JSONArray?, scale: Double, quality: Int): JSONObject {
        val hw = Bitmap.wrapHardwareBuffer(r.hardwareBuffer, r.colorSpace)
            ?: throw RpcError(Codes.INTERNAL, "could not wrap the screenshot buffer")
        var bmp = hw.copy(Bitmap.Config.ARGB_8888, false)
        hw.recycle()
        r.hardwareBuffer.close()
        if (crop != null && crop.length() == 4) {
            val l = crop.getInt(0).coerceIn(0, bmp.width - 1)
            val t = crop.getInt(1).coerceIn(0, bmp.height - 1)
            val rr = crop.getInt(2).coerceIn(l + 1, bmp.width)
            val b = crop.getInt(3).coerceIn(t + 1, bmp.height)
            bmp = Bitmap.createBitmap(bmp, l, t, rr - l, b - t)
        }
        val w = (bmp.width * scale).toInt().coerceAtLeast(1)
        val h = (bmp.height * scale).toInt().coerceAtLeast(1)
        if (w != bmp.width) bmp = Bitmap.createScaledBitmap(bmp, w, h, true)
        val buf = ByteArrayOutputStream()
        bmp.compress(Bitmap.CompressFormat.JPEG, quality, buf)
        return JSONObject().put("format", "jpeg").put("w", bmp.width).put("h", bmp.height)
            .put("scale", scale).put("data", Base64.encodeToString(buf.toByteArray(), Base64.NO_WRAP))
    }

    // ------------------------------------------------------------------ clipboard
    /** {set?: text} -> {previous?, set}. Reading may be denied on API 29+ (background clipboard rules). */
    fun clipboard(p: JSONObject): JSONObject {
        val out = JSONObject()
        val done = CountDownLatch(1)
        val err = AtomicReference<Exception?>(null)
        Handler(Looper.getMainLooper()).post {
            try {
                val cm = svc.clipboard()
                val prev = try { cm.primaryClip?.takeIf { it.itemCount > 0 }?.getItemAt(0)?.coerceToText(svc.context)?.toString() }
                           catch (_: Exception) { null }
                if (prev != null) out.put("previous", prev)
                if (p.has("set")) {
                    cm.setPrimaryClip(ClipData.newPlainText("droidctl", p.optString("set")))
                    out.put("set", true)
                } else out.put("set", false)
            } catch (e: Exception) { err.set(e) } finally { done.countDown() }
        }
        if (!done.await(3, TimeUnit.SECONDS)) throw RpcError(Codes.TIMEOUT, "clipboard: main thread busy")
        err.get()?.let { throw RpcError(Codes.INTERNAL, "clipboard: $it") }
        return out
    }
}
