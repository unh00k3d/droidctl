package dev.droidctl.agent

import android.content.ComponentName
import android.os.SystemClock
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityWindowInfo
import org.json.JSONObject
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

/**
 * The event ring, the idle clock and the push subscriptions.
 *
 * Fed on the main thread by the service; read by connection threads. Every
 * mutation happens under `lock` and ends in notifyAll(), so waiters (settle,
 * wait_idle, wait_for) are woken by events instead of polling.
 */
class Events(private val svc: AgentService) {
    companion object {
        const val CAP = 500
        /** Bursts of the same noisy event from one package within this window are compacted. */
        const val COMPACT_MS = 250L

        /** Event types that mean "the screen may have changed" (they drive settle/idle). */
        val CHANGE_TYPES = setOf("window_content", "window_state", "windows_changed", "scrolled", "text_changed")
        private val COMPACTABLE = setOf("window_content", "windows_changed", "scrolled")
    }

    class Ev(val seq: Long, val t: Long, val type: String, val json: JSONObject,
             /** the event's source node, kept only for clicks (to match them to an `act`) */
             val source: AccessibilityNodeInfo?)

    /** A subscription: events are queued here and written by the subscription's own thread. */
    class Sub(val conn: Conn, val types: Set<String>?) {
        val queue = LinkedBlockingQueue<JSONObject>(2000)
        @Volatile var alive = true
    }

    val lock = Object()
    private val ring = ArrayDeque<Ev>()
    private var seq = 0L
    /** Bumped on every add *or* compaction: what waiters watch for "something happened". */
    var ticks = 0L
        private set
    /** Uptime of the last screen-changing event (see CHANGE_TYPES). */
    var lastChange = 0L
        private set
    /** Uptime of the last event of any type (the pipeline is alive and delivering). */
    var lastEvent = 0L
        private set
    private val subs = CopyOnWriteArrayList<Sub>()

    @Volatile var imeShown = false
        private set
    @Volatile var curPkg: String? = null
        private set
    @Volatile var curActivity: String? = null
        private set
    private val isActivity = HashMap<String, Boolean>()
    /**
     * window id -> activity class. Returning to an existing activity (back) sends no
     * window-state event on Samsung API 28, so "the last window-state event" goes stale;
     * the active window's id still tells us which activity is in front.
     */
    private val windowActivity = object : LinkedHashMap<Int, String>(64, 0.75f, true) {
        override fun removeEldestEntry(eldest: MutableMap.MutableEntry<Int, String>?) = size > 64
    }

    /** The activity owning window `id`, if a window-state event ever told us. */
    fun activityOf(id: Int): String? = synchronized(windowActivity) { windowActivity[id] }

    val nextSeq: Long get() = synchronized(lock) { seq + 1 }

    // ---------------------------------------------------------------- feeding
    fun onEvent(e: AccessibilityEvent, gen: Long) {
        val pkg = e.packageName?.toString()
        val cls = e.className?.toString()
        val o = JSONObject()
        if (pkg != null) o.put("pkg", pkg)
        var source: AccessibilityNodeInfo? = null
        val type = when (e.eventType) {
            AccessibilityEvent.TYPE_VIEW_CLICKED -> { source = src(o, e); node(o, cls, e); "clicked" }
            AccessibilityEvent.TYPE_VIEW_LONG_CLICKED -> { source = src(o, e); node(o, cls, e); "long_clicked" }
            AccessibilityEvent.TYPE_VIEW_FOCUSED -> { node(o, cls, e); "focused" }
            AccessibilityEvent.TYPE_VIEW_SELECTED -> { node(o, cls, e); "selected" }
            AccessibilityEvent.TYPE_VIEW_TEXT_CHANGED -> {
                if (cls != null) o.put("class", cls)
                if (!e.isPassword) text(e)?.let { o.put("text", it) }
                o.put("added", e.addedCount).put("removed", e.removedCount)
                "text_changed"
            }
            AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED -> {
                if (cls != null) o.put("class", cls)
                text(e)?.let { o.put("title", it) }
                if (e.contentChangeTypes != 0) o.put("changes", e.contentChangeTypes)
                trackActivity(pkg, cls, e.windowId)
                "window_state"
            }
            AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED -> "window_content"
            AccessibilityEvent.TYPE_WINDOWS_CHANGED -> { checkIme(gen); "windows_changed" }
            AccessibilityEvent.TYPE_VIEW_SCROLLED -> { if (cls != null) o.put("class", cls); "scrolled" }
            AccessibilityEvent.TYPE_NOTIFICATION_STATE_CHANGED -> {
                text(e)?.let { o.put("text", it) }
                if (cls != null && cls.contains("Toast")) "toast" else "notification"
            }
            AccessibilityEvent.TYPE_ANNOUNCEMENT -> { text(e)?.let { o.put("text", it) }; "announcement" }
            else -> return
        }
        add(type, o, gen, source)
        // the IME window can appear without a WINDOWS_CHANGED on some ROMs
        if (type == "window_state") checkIme(gen)
    }

    /** The click's source node, plus its window and bounds so `act` can match it even when equals() can't. */
    private fun src(o: JSONObject, e: AccessibilityEvent): AccessibilityNodeInfo? {
        o.put("window", e.windowId)
        val n = try { e.source } catch (_: Exception) { null } ?: return null
        val r = android.graphics.Rect()
        n.getBoundsInScreen(r)
        o.put("bounds", org.json.JSONArray().put(r.left).put(r.top).put(r.right).put(r.bottom))
        return n
    }

    private fun node(o: JSONObject, cls: String?, e: AccessibilityEvent) {
        if (cls != null) o.put("class", cls)
        text(e)?.let { o.put("text", it) }
        e.contentDescription?.let { if (it.isNotEmpty()) o.put("desc", it.toString()) }
    }

    private fun text(e: AccessibilityEvent): String? {
        val parts = e.text ?: return null
        val s = parts.filterNotNull().joinToString(" ").trim()
        return if (s.isEmpty()) null else if (s.length > 300) s.substring(0, 300) + "…" else s
    }

    private fun trackActivity(pkg: String?, cls: String?, windowId: Int) {
        if (pkg == null) return
        curPkg = pkg
        if (cls == null) return
        val key = "$pkg/$cls"
        val act = isActivity.getOrPut(key) {
            try { svc.packageManager.getActivityInfo(ComponentName(pkg, cls), 0); true } catch (_: Exception) { false }
        }
        if (act) {
            curActivity = cls
            if (windowId >= 0) synchronized(windowActivity) { windowActivity[windowId] = cls }
        }
    }

    /** Emits an `ime` event when the input-method window appears or disappears. */
    private fun checkIme(gen: Long) {
        val shown = imeWindowPresent()
        if (shown != imeShown) {
            imeShown = shown
            add("ime", JSONObject().put("shown", shown), gen, null)
        }
    }

    fun imeWindowPresent(): Boolean = try {
        svc.windows.any { it.type == AccessibilityWindowInfo.TYPE_INPUT_METHOD }
    } catch (_: Exception) { false }

    private fun add(type: String, o: JSONObject, gen: Long, source: AccessibilityNodeInfo?) {
        val now = SystemClock.uptimeMillis()
        var fresh: Ev? = null
        synchronized(lock) {
            ticks++
            lastEvent = now
            if (type in CHANGE_TYPES) lastChange = now
            val last = ring.lastOrNull()
            if (type in COMPACTABLE && last != null && last.type == type &&
                last.json.optString("pkg") == o.optString("pkg") &&
                now - last.json.optLong("t_last", last.t) <= COMPACT_MS) {
                last.json.put("n", last.json.optInt("n", 1) + 1).put("t_last", now).put("gen", gen)
            } else {
                seq++
                o.put("seq", seq).put("t", now).put("gen", gen).put("type", type)
                fresh = Ev(seq, now, type, o, source)
                ring.addLast(fresh!!)
                while (ring.size > CAP) ring.removeFirst()
            }
            lock.notifyAll()
        }
        val ev = fresh ?: return
        for (s in subs) {
            if (!s.alive) continue
            if (s.types == null || type in s.types) {
                if (!s.queue.offer(ev.json)) Log.w(TAG, "subscriber queue full; dropping ${ev.type}")
            }
        }
    }

    // ---------------------------------------------------------------- reading
    /** Events with seq > since (oldest first), at most `limit` of the newest. */
    fun since(since: Long, limit: Int = CAP): List<Ev> = synchronized(lock) {
        val out = ring.filter { it.seq > since }
        if (out.size > limit) out.subList(out.size - limit, out.size).toList() else out
    }

    /**
     * Settle after an action taken at `from`. Accessibility events reach us with a lag
     * (150-250 ms after a click on the SM-N950F, measured), so a quiet window counted
     * from the action ends before anything is reported. Instead: wait up to `firstMs`
     * for the first screen-changing event after `from` (none -> nothing happened, idle;
     * a clicked/focus event alone doesn't count), then until no event has arrived for
     * `quietMs`. False if `timeoutMs` (from now) ran out first.
     */
    fun waitSettled(from: Long, quietMs: Long, firstMs: Long, timeoutMs: Long): Boolean {
        val deadline = SystemClock.uptimeMillis() + timeoutMs
        synchronized(lock) {
            while (true) {
                val now = SystemClock.uptimeMillis()
                // the first *change* opens the quiet window: a TYPE_VIEW_CLICKED alone
                // says the click was handled, not that the screen is done changing
                // (a banking QA app: Login -> clicked, then the next activity ~0.5 s later)
                val target = if (lastChange <= from) from + firstMs
                             else maxOf(lastEvent, lastChange) + quietMs
                if (now >= target) return true
                if (now >= deadline) return false
                lock.wait(minOf(target, deadline) - now)
            }
        }
    }

    /**
     * Blocks until nothing screen-changing has happened for `quietMs`, counting from
     * max(last change, `from`). Returns false if `timeoutMs` (from now) ran out first.
     */
    fun waitQuiet(from: Long, quietMs: Long, timeoutMs: Long): Boolean {
        val deadline = SystemClock.uptimeMillis() + timeoutMs
        synchronized(lock) {
            while (true) {
                val now = SystemClock.uptimeMillis()
                val quietAt = maxOf(lastChange, from) + quietMs
                if (now >= quietAt) return true
                if (now >= deadline) return false
                lock.wait(minOf(quietAt, deadline) - now)
            }
        }
    }

    /** Blocks until `ticks` moves past `seen` or `until` (uptime) passes; returns the new ticks. */
    fun awaitTick(seen: Long, until: Long): Long {
        synchronized(lock) {
            while (ticks == seen) {
                val left = until - SystemClock.uptimeMillis()
                if (left <= 0) break
                lock.wait(left)
            }
            return ticks
        }
    }

    /** Blocks until an event matching `pred` with seq > since exists, or `until`; returns it or null. */
    fun awaitEvent(since: Long, until: Long, pred: (Ev) -> Boolean): Ev? {
        synchronized(lock) {
            var cursor = since
            while (true) {
                for (ev in ring) if (ev.seq > cursor && pred(ev)) return ev
                cursor = seq
                val left = until - SystemClock.uptimeMillis()
                if (left <= 0) return null
                lock.wait(left)
            }
        }
    }

    // ---------------------------------------------------------------- subscriptions
    fun subscribe(conn: Conn, types: Set<String>?): Sub {
        unsubscribe(conn)
        val s = Sub(conn, types)
        subs.add(s)
        Thread({
            try {
                while (s.alive) {
                    val ev = s.queue.poll(500, TimeUnit.MILLISECONDS) ?: continue
                    if (!conn.notify("event", ev)) break
                }
            } catch (_: InterruptedException) {
            } finally {
                s.alive = false
                subs.remove(s)
            }
        }, "droidctl-sub").apply { isDaemon = true }.start()
        return s
    }

    fun unsubscribe(conn: Conn): Boolean {
        var any = false
        for (s in subs) if (s.conn === conn) { s.alive = false; subs.remove(s); any = true }
        return any
    }
}
