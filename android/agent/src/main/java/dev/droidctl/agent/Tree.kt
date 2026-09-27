package dev.droidctl.agent

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.AccessibilityServiceInfo
import android.graphics.Rect
import android.os.Build
import android.os.SystemClock
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityNodeInfo.AccessibilityAction
import android.view.accessibility.AccessibilityWindowInfo
import org.json.JSONArray
import org.json.JSONObject

/**
 * Full-fidelity dumps of every window, plus the handle table for the latest dump.
 *
 * The host does all pruning and formatting; this only has to be complete, compact
 * (absent = default) and fast. See PROTOCOL.md "tree" for the schema.
 */
class Tree(private val svc: AccessibilityService) {
    companion object {
        const val BUDGET_MS = 2000L
        const val MAX_NODES = 10000
        const val MAX_DEPTH = 120

        /** Standard action ids -> names. Anything not here with a label is a custom action. */
        private val STANDARD: Map<Int, String> = buildMap {
            put(AccessibilityNodeInfo.ACTION_FOCUS, "focus")
            put(AccessibilityNodeInfo.ACTION_CLEAR_FOCUS, "clear_focus")
            put(AccessibilityNodeInfo.ACTION_SELECT, "select")
            put(AccessibilityNodeInfo.ACTION_CLEAR_SELECTION, "clear_selection")
            put(AccessibilityNodeInfo.ACTION_CLICK, "click")
            put(AccessibilityNodeInfo.ACTION_LONG_CLICK, "long_click")
            put(AccessibilityNodeInfo.ACTION_ACCESSIBILITY_FOCUS, "accessibility_focus")
            put(AccessibilityNodeInfo.ACTION_CLEAR_ACCESSIBILITY_FOCUS, "clear_accessibility_focus")
            put(AccessibilityNodeInfo.ACTION_NEXT_AT_MOVEMENT_GRANULARITY, "next_at_movement_granularity")
            put(AccessibilityNodeInfo.ACTION_PREVIOUS_AT_MOVEMENT_GRANULARITY, "previous_at_movement_granularity")
            put(AccessibilityNodeInfo.ACTION_NEXT_HTML_ELEMENT, "next_html_element")
            put(AccessibilityNodeInfo.ACTION_PREVIOUS_HTML_ELEMENT, "previous_html_element")
            put(AccessibilityNodeInfo.ACTION_SCROLL_FORWARD, "scroll_forward")
            put(AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD, "scroll_backward")
            put(AccessibilityNodeInfo.ACTION_COPY, "copy")
            put(AccessibilityNodeInfo.ACTION_PASTE, "paste")
            put(AccessibilityNodeInfo.ACTION_CUT, "cut")
            put(AccessibilityNodeInfo.ACTION_SET_SELECTION, "set_selection")
            put(AccessibilityNodeInfo.ACTION_EXPAND, "expand")
            put(AccessibilityNodeInfo.ACTION_COLLAPSE, "collapse")
            put(AccessibilityNodeInfo.ACTION_DISMISS, "dismiss")
            put(AccessibilityNodeInfo.ACTION_SET_TEXT, "set_text")
            put(AccessibilityAction.ACTION_SHOW_ON_SCREEN.id, "show_on_screen")
            put(AccessibilityAction.ACTION_SCROLL_TO_POSITION.id, "scroll_to_position")
            put(AccessibilityAction.ACTION_SCROLL_UP.id, "scroll_up")
            put(AccessibilityAction.ACTION_SCROLL_LEFT.id, "scroll_left")
            put(AccessibilityAction.ACTION_SCROLL_DOWN.id, "scroll_down")
            put(AccessibilityAction.ACTION_SCROLL_RIGHT.id, "scroll_right")
            put(AccessibilityAction.ACTION_CONTEXT_CLICK.id, "context_click")
            put(AccessibilityAction.ACTION_SET_PROGRESS.id, "set_progress")
            put(AccessibilityAction.ACTION_MOVE_WINDOW.id, "move_window")
            if (Build.VERSION.SDK_INT >= 28) {
                put(AccessibilityAction.ACTION_SHOW_TOOLTIP.id, "show_tooltip")
                put(AccessibilityAction.ACTION_HIDE_TOOLTIP.id, "hide_tooltip")
            }
            if (Build.VERSION.SDK_INT >= 29) {
                put(AccessibilityAction.ACTION_PAGE_UP.id, "page_up")
                put(AccessibilityAction.ACTION_PAGE_DOWN.id, "page_down")
                put(AccessibilityAction.ACTION_PAGE_LEFT.id, "page_left")
                put(AccessibilityAction.ACTION_PAGE_RIGHT.id, "page_right")
            }
            if (Build.VERSION.SDK_INT >= 30) {
                put(AccessibilityAction.ACTION_PRESS_AND_HOLD.id, "press_and_hold")
                put(AccessibilityAction.ACTION_IME_ENTER.id, "ime_enter")
            }
            if (Build.VERSION.SDK_INT >= 32) {
                put(AccessibilityAction.ACTION_DRAG_START.id, "drag_start")
                put(AccessibilityAction.ACTION_DRAG_DROP.id, "drag_drop")
                put(AccessibilityAction.ACTION_DRAG_CANCEL.id, "drag_cancel")
            }
            if (Build.VERSION.SDK_INT >= 33) {
                put(AccessibilityAction.ACTION_SHOW_TEXT_SUGGESTIONS.id, "show_text_suggestions")
            }
            if (Build.VERSION.SDK_INT >= 34) {
                put(AccessibilityAction.ACTION_SCROLL_IN_DIRECTION.id, "scroll_in_direction")
            }
        }

        /** Present on nearly every node and useless to the host: never reported. */
        private val NOISE = setOf(
            AccessibilityNodeInfo.ACTION_ACCESSIBILITY_FOCUS,
            AccessibilityNodeInfo.ACTION_CLEAR_ACCESSIBILITY_FOCUS,
            AccessibilityNodeInfo.ACTION_NEXT_AT_MOVEMENT_GRANULARITY,
            AccessibilityNodeInfo.ACTION_PREVIOUS_AT_MOVEMENT_GRANULARITY,
            AccessibilityNodeInfo.ACTION_NEXT_HTML_ELEMENT,
            AccessibilityNodeInfo.ACTION_PREVIOUS_HTML_ELEMENT,
        )

        fun windowType(t: Int): String = when (t) {
            AccessibilityWindowInfo.TYPE_APPLICATION -> "application"
            AccessibilityWindowInfo.TYPE_INPUT_METHOD -> "input_method"
            AccessibilityWindowInfo.TYPE_SYSTEM -> "system"
            AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY -> "accessibility_overlay"
            AccessibilityWindowInfo.TYPE_SPLIT_SCREEN_DIVIDER -> "split_screen_divider"
            6 -> "magnification_overlay"  // TYPE_MAGNIFICATION_OVERLAY (API 33)
            else -> "type_$t"
        }

        @Suppress("DEPRECATION")
        fun recycle(n: AccessibilityNodeInfo) {
            // a no-op from API 33; before that it returns the node to the pool
            if (Build.VERSION.SDK_INT < 33) try { n.recycle() } catch (_: Exception) {}
        }
    }

    /** Content-generation counter, bumped by the service on content/window events. */
    @Volatile var gen: Long = 0

    private val lock = Any()
    private var dump = 0
    private var handles: HashMap<Int, AccessibilityNodeInfo> = HashMap()
    private var lastGood: JSONObject? = null

    /** The node for a handle of the latest dump, or an RpcError (`stale`) for any other dump. */
    fun node(dumpId: Int, handle: Int): AccessibilityNodeInfo = synchronized(lock) {
        if (dumpId != dump) throw RpcError(Codes.STALE, "stale dump $dumpId (latest is $dump)")
        handles[handle] ?: throw RpcError(Codes.INVALID_PARAMS, "no handle $handle in dump $dumpId")
    }

    fun dump(notImportant: Boolean, allWindows: Boolean, screen: JSONObject): JSONObject = synchronized(lock) {
        setNotImportant(notImportant)
        val t0 = SystemClock.uptimeMillis()
        val deadline = t0 + BUDGET_MS
        val startGen = gen
        val walk = Walk(deadline)
        val windows = JSONArray()
        var roots = collect(allWindows, walk, windows)
        if (roots == 0) {  // a null root right after a transition is common: retry once
            SystemClock.sleep(50)
            walk.release()
            val w2 = Walk(deadline)
            windows.clear()
            roots = collect(allWindows, w2, windows)
            return@synchronized finish(w2, windows, startGen, t0, screen, roots)
        }
        finish(walk, windows, startGen, t0, screen, roots)
    }

    private fun finish(walk: Walk, windows: JSONArray, startGen: Long, t0: Long,
                       screen: JSONObject, roots: Int): JSONObject {
        val ms = SystemClock.uptimeMillis() - t0
        val prev = lastGood
        if ((walk.timedOut || roots == 0) && prev != null) {
            // keep the previous handles valid: the host may still act on them
            walk.release()
            return JSONObject(prev.toString()).put("degraded", true).put("ms", ms)
                .put("reason", if (walk.timedOut) "timeout" else "no-root")
        }
        for (n in handles.values) recycle(n)
        handles = walk.handles
        dump += 1
        val out = JSONObject()
            .put("gen", startGen)
            .put("dump", dump)
            .put("degraded", walk.timedOut || walk.truncated || roots == 0)
            .put("ms", ms)
            .put("nodes", walk.handles.size)
            .put("screen", screen)
            .put("windows", windows)
        if (walk.timedOut) out.put("reason", "timeout")
        else if (walk.truncated) out.put("reason", "truncated")
        else if (roots == 0) out.put("reason", "no-root")
        if (!walk.timedOut && !walk.truncated && roots > 0) lastGood = out
        return out
    }

    private fun JSONArray.clear() { while (length() > 0) remove(length() - 1) }

    private fun setNotImportant(on: Boolean) {
        val info = svc.serviceInfo ?: return
        val flag = AccessibilityServiceInfo.FLAG_INCLUDE_NOT_IMPORTANT_VIEWS
        val want = if (on) info.flags or flag else info.flags and flag.inv()
        if (want != info.flags) {
            info.flags = want
            svc.serviceInfo = info
        }
    }

    /** Fills `out` with one entry per window; returns how many roots were dumped. */
    private fun collect(allWindows: Boolean, walk: Walk, out: JSONArray): Int {
        var roots = 0
        val list = if (allWindows) try { svc.windows } catch (_: Exception) { emptyList() } else emptyList()
        if (list.isNotEmpty()) {
            for (w in list) {
                val root = try { w.root } catch (_: Exception) { null }
                val jw = windowJson(w, root)
                if (root != null) {
                    jw.put("root", walk.node(root, null, 0))
                    roots += 1
                }
                out.put(jw)
            }
            @Suppress("DEPRECATION")
            if (Build.VERSION.SDK_INT < 33) for (w in list) try { w.recycle() } catch (_: Exception) {}
        } else {
            val root = svc.rootInActiveWindow ?: return 0
            val jw = JSONObject().put("id", root.windowId).put("type", "application").put("active", true)
            root.packageName?.let { jw.put("pkg", it.toString()) }
            jw.put("root", walk.node(root, null, 0))
            out.put(jw)
            roots = 1
        }
        return roots
    }

    private fun windowJson(w: AccessibilityWindowInfo, root: AccessibilityNodeInfo?): JSONObject {
        val r = Rect()
        w.getBoundsInScreen(r)
        val o = JSONObject()
            .put("id", w.id)
            .put("type", windowType(w.type))
            .put("layer", w.layer)
            .put("bounds", JSONArray().put(r.left).put(r.top).put(r.right).put(r.bottom))
        if (Build.VERSION.SDK_INT >= 24) w.title?.let { o.put("title", it.toString()) }
        root?.packageName?.let { o.put("pkg", it.toString()) }
        if (w.isActive) o.put("active", true)
        if (w.isFocused) o.put("focused", true)
        return o
    }

    /** One traversal: assigns handles and enforces the time and size budgets. */
    private inner class Walk(val deadline: Long) {
        val handles = HashMap<Int, AccessibilityNodeInfo>()
        var timedOut = false
        var truncated = false
        private var next = 1
        private val rect = Rect()

        fun release() { for (n in handles.values) recycle(n); handles.clear() }

        fun node(n: AccessibilityNodeInfo, parentPkg: String?, depth: Int): JSONObject {
            val h = next++
            handles[h] = n
            val o = JSONObject().put("handle", h)
            n.className?.let { o.put("class", it.toString()) }
            val pkg = n.packageName?.toString()
            if (pkg != null && pkg != parentPkg) o.put("pkg", pkg)
            n.viewIdResourceName?.let { o.put("id", it) }
            if (Build.VERSION.SDK_INT >= 33) n.uniqueId?.let { o.put("uid", it) }
            str(o, "text", n.text)
            str(o, "desc", n.contentDescription)
            if (Build.VERSION.SDK_INT >= 26) str(o, "hint", n.hintText)
            str(o, "error", n.error)
            if (Build.VERSION.SDK_INT >= 30) str(o, "state", n.stateDescription)
            if (Build.VERSION.SDK_INT >= 28) {
                str(o, "tooltip", n.tooltipText)
                str(o, "pane", n.paneTitle)
            }
            n.getBoundsInScreen(rect)
            o.put("bounds", JSONArray().put(rect.left).put(rect.top).put(rect.right).put(rect.bottom))
            if (!n.isVisibleToUser) o.put("visible", false)
            if (Build.VERSION.SDK_INT >= 24 && n.drawingOrder != 0) o.put("drawingOrder", n.drawingOrder)
            flags(o, n)
            actions(o, n)
            n.collectionInfo?.let { o.put("collection", JSONObject().put("rows", it.rowCount).put("cols", it.columnCount)) }
            n.collectionItemInfo?.let { o.put("item", JSONObject().put("row", it.rowIndex).put("col", it.columnIndex)) }
            n.rangeInfo?.let { o.put("range", JSONObject().put("min", it.min.toDouble()).put("max", it.max.toDouble()).put("cur", it.current.toDouble())) }
            if (n.inputType != 0) o.put("inputType", n.inputType)

            val count = n.childCount
            if (count > 0) {
                val kids = JSONArray()
                for (i in 0 until count) {
                    if (SystemClock.uptimeMillis() > deadline) { timedOut = true; break }
                    if (handles.size >= MAX_NODES || depth >= MAX_DEPTH) { truncated = true; break }
                    val c = try { n.getChild(i) } catch (_: Exception) { null } ?: continue
                    kids.put(node(c, pkg, depth + 1))
                }
                if (kids.length() > 0) o.put("children", kids)
            }
            return o
        }

        private fun str(o: JSONObject, key: String, v: CharSequence?) {
            if (!v.isNullOrEmpty()) o.put(key, v.toString())
        }

        private fun flags(o: JSONObject, n: AccessibilityNodeInfo) {
            val f = JSONArray()
            if (n.isClickable) f.put("clickable")
            if (n.isLongClickable) f.put("longClickable")
            if (n.isCheckable) f.put("checkable")
            if (n.isChecked) f.put("checked")
            if (n.isFocusable) f.put("focusable")
            if (n.isFocused) f.put("focused")
            if (n.isSelected) f.put("selected")
            if (n.isEnabled) f.put("enabled")
            if (n.isEditable) f.put("editable")
            if (n.isPassword) f.put("password")
            if (n.isScrollable) f.put("scrollable")
            if (Build.VERSION.SDK_INT >= 28 && n.isHeading) f.put("heading")
            if (Build.VERSION.SDK_INT >= 26 && n.isShowingHintText) f.put("showingHint")
            if (f.length() > 0) o.put("flags", f)
        }

        private fun actions(o: JSONObject, n: AccessibilityNodeInfo) {
            val list = n.actionList ?: return
            val a = JSONArray()
            for (act in list) {
                if (act.id in NOISE) continue
                val name = STANDARD[act.id]
                when {
                    name != null -> a.put(name)
                    act.label != null -> a.put(JSONObject().put("id", act.id).put("label", act.label.toString()))
                    else -> a.put(JSONObject().put("id", act.id))
                }
            }
            if (a.length() > 0) o.put("actions", a)
        }
    }
}
