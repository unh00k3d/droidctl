package dev.droidctl.agent

import android.accessibilityservice.AccessibilityServiceInfo
import android.annotation.SuppressLint
import android.app.UiAutomation
import android.content.ClipboardManager
import android.content.Context
import android.content.ContextWrapper
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import android.view.InputDevice
import android.view.MotionEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityWindowInfo
import java.lang.reflect.InvocationTargetException
import kotlin.system.exitProcess

/**
 * Backend B (PLAN.md "Device backends"): the same agent, driven by a UiAutomation
 * instead of an enabled accessibility service. Nothing is installed or enabled and
 * no setting changes: the host pushes the agent APK to /data/local/tmp and runs
 *
 *     CLASSPATH=/data/local/tmp/droidctl-agent.apk app_process /system/bin dev.droidctl.agent.ShellMain
 *
 * as the shell user (uid 2000). A UiAutomation can only be had that way (over adb),
 * so this needs exactly the trust droidctl already has. It serves the same
 * PROTOCOL.md on its own socket name (@droidctl_ua) and exits when it has had no
 * client for `--idle-ms` (default 10 min), when the host sends `shutdown`, or when
 * another UiAutomation client (Appium, uiautomator2) already holds the one slot.
 *
 * Framework internals (ActivityThread's system context, the UiAutomation
 * constructors, connect(flags)) are reached by reflection; app_process code is not
 * subject to the hidden-API policy that apps are.
 */
object ShellMain {
    const val EXIT_BUSY = 3         // another UiAutomation is registered
    const val EXIT_SOCKET = 4       // another instance owns @droidctl_ua
    const val EXIT_FAILED = 5
    private const val DEFAULT_IDLE_MS = 600_000L

    @JvmStatic
    fun main(args: Array<String>) {
        var idleMs = DEFAULT_IDLE_MS
        var suppress = false
        var i = 0
        while (i < args.size) {
            when (args[i]) {
                "--idle-ms" -> { idleMs = args.getOrNull(i + 1)?.toLongOrNull() ?: idleMs; i++ }
                // default: leave other accessibility services (TalkBack, backend A) running
                "--suppress" -> suppress = true
            }
            i++
        }
        guardBackgroundThreads()
        Looper.prepareMainLooper()
        val ctx = try { systemContext() } catch (e: Throwable) { fail("no system context", e) }
        // UiAutomation's callbacks run on a looper; set up off the main thread, which
        // must keep looping (API 34+ builds the UiAutomation on the main looper, and
        // the clipboard is used there)
        Thread({ start(ctx, idleMs, suppress) }, "droidctl-ua-init").start()
        Looper.loop()
    }

    private fun start(ctx: Context, idleMs: Long, suppress: Boolean) {
        val looper = HandlerThread("droidctl-ua").apply { start() }.looper
        val ua = try { connect(ctx, looper, suppress) } catch (e: Throwable) {
            val msg = e.message ?: e.toString()
            if (msg.contains("already registered")) {
                say("E busy: another UiAutomation client (uiautomator/Appium) is connected: $msg")
                exitProcess(EXIT_BUSY)
            }
            fail("UiAutomation connect failed", e)
        }
        val host = ShellHost(ua, ctx)
        host.configure()
        val core = Core(host)
        ua.setOnAccessibilityEventListener { core.onEvent(it) }
        lateinit var server: Server
        val stop = {
            say("I stopping")
            server.stop()
            try { UiAutomation::class.java.getDeclaredMethod("disconnect").invoke(ua) } catch (_: Throwable) {}
            exitProcess(0)
        }
        server = Server(Rpc(host, core.tree, core.events, stop), Server.SHELL_SOCKET_NAME)
        server.onBindFailed = { say("E socket: @${Server.SHELL_SOCKET_NAME} is taken (another instance?)"); exitProcess(EXIT_SOCKET) }
        server.start()
        say("I ready pid=${android.os.Process.myPid()} sdk=${Build.VERSION.SDK_INT} idle_ms=$idleMs suppress=$suppress")
        // polite by default: release the UiAutomation once nobody has used it for a while
        Thread({
            while (true) {
                SystemClock.sleep(minOf(5000L, idleMs))
                val since = server.idleSince
                if (since > 0 && SystemClock.uptimeMillis() - since >= idleMs) {
                    say("I idle for ${idleMs} ms")
                    stop()
                }
            }
        }, "droidctl-ua-idle").apply { isDaemon = true }.start()
    }

    /** A Context for system services without an installed package (as scrcpy's server does). */
    // runs only under app_process as shell, where the hidden-API policy does not apply
    @SuppressLint("BlockedPrivateApi")
    private fun systemContext(): Context {
        val at = Class.forName("android.app.ActivityThread")
        val thread = at.getDeclaredConstructor().apply { isAccessible = true }.newInstance()
        at.getDeclaredField("sCurrentActivityThread").apply { isAccessible = true }.set(null, thread)
        // Android 12 moved the configuration into a ConfigurationController that only
        // attach() creates, and getSystemContext() reads it: on an unattached thread it
        // threw an NPE (ConfigurationController.getConfiguration() on null; reported on
        // a Galaxy S25, API 35). Give the thread its controller, nothing more:
        // systemMain()/attach(true) would also set this process up as system_server.
        if (Build.VERSION.SDK_INT >= 31) {
            val internal = Class.forName("android.app.ActivityThreadInternal")
            val controller = Class.forName("android.app.ConfigurationController")
                .getDeclaredConstructor(internal).apply { isAccessible = true }.newInstance(thread)
            at.getDeclaredField("mConfigurationController").apply { isAccessible = true }.set(thread, controller)
        }
        return at.getDeclaredMethod("getSystemContext").invoke(thread) as Context
    }

    private fun connect(ctx: Context, looper: Looper, suppress: Boolean): UiAutomation {
        val conn = Class.forName("android.app.UiAutomationConnection")
            .getDeclaredConstructor().apply { isAccessible = true }.newInstance()
        val iface = Class.forName("android.app.IUiAutomationConnection")
        val cls = UiAutomation::class.java
        val ua = try {
            cls.getDeclaredConstructor(Looper::class.java, iface).apply { isAccessible = true }.newInstance(looper, conn)
        } catch (_: NoSuchMethodException) {
            cls.getDeclaredConstructor(Context::class.java, iface).apply { isAccessible = true }.newInstance(ctx, conn)
        }
        val flags = if (suppress) 0 else UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES
        try {
            cls.getDeclaredMethod("connect", Int::class.javaPrimitiveType).apply { isAccessible = true }.invoke(ua, flags)
        } catch (e: InvocationTargetException) {
            throw e.targetException
        }
        return ua
    }

    fun say(line: String) {
        System.out.println(line)
        System.out.flush()
        Log.i(TAG, "ua: $line")
    }

    private fun fail(what: String, e: Throwable): Nothing {
        val cause = (e as? InvocationTargetException)?.targetException ?: e
        say("E failed: $what: $cause")
        Log.e(TAG, what, cause)
        exitProcess(EXIT_FAILED)
    }
}

/** Backend B's Host: a UiAutomation, plus input injection for gestures. */
class ShellHost(private val ua: UiAutomation, private val ctx: Context) : Host {
    override val backend = "uiautomation"
    override val context: Context get() = ctx
    override fun windows(): List<AccessibilityWindowInfo> = ua.windows
    override fun activeRoot(): AccessibilityNodeInfo? = ua.rootInActiveWindow
    override fun getInfo(): AccessibilityServiceInfo? = ua.serviceInfo
    override fun setInfo(info: AccessibilityServiceInfo) { ua.serviceInfo = info }
    override fun global(id: Int) = ua.performGlobalAction(id)

    /** The flags backend A gets from res/xml/accessibility_service.xml. */
    fun configure() {
        val info = ua.serviceInfo ?: AccessibilityServiceInfo()
        info.eventTypes = android.view.accessibility.AccessibilityEvent.TYPES_ALL_MASK
        info.flags = info.flags or AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS or
            AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS
        info.notificationTimeout = 50
        ua.serviceInfo = info
    }

    private val ver by lazy {
        val apk = System.getProperty("java.class.path") ?: ""
        val pi = ctx.packageManager.getPackageArchiveInfo(apk, 0)
        @Suppress("DEPRECATION")
        (pi?.versionName ?: "?") to (pi?.let { if (Build.VERSION.SDK_INT >= 28) it.longVersionCode else it.versionCode.toLong() } ?: -1L)
    }
    override fun version() = ver

    /** The shell package's clipboard (the system context would claim to be "android", uid 1000). */
    private val cm by lazy {
        val shell = object : ContextWrapper(ctx) {
            override fun getPackageName() = "com.android.shell"
            override fun getOpPackageName() = "com.android.shell"
        }
        ClipboardManager::class.java.getDeclaredConstructor(Context::class.java, Handler::class.java)
            .apply { isAccessible = true }.newInstance(shell, Handler(Looper.getMainLooper()))
    }
    override fun clipboard(): ClipboardManager = cm

    /**
     * Plays the strokes as touchscreen MotionEvents, sampled every [STEP_MS] on the
     * strokes' own timeline, moving along each polyline at constant speed (what
     * dispatchGesture does). Several strokes at once become a multi-pointer gesture.
     */
    override fun runGesture(strokes: List<Stroke>, timeoutMs: Long): Boolean {
        val end = strokes.maxOf { it.startMs + it.durationMs }
        val times = sortedSetOf<Long>()
        var t = 0L
        while (t < end) { times.add(t); t += STEP_MS }
        times.add(end)
        for (s in strokes) { times.add(s.startMs); times.add(s.startMs + s.durationMs) }
        val active = ArrayList<Int>()            // stroke indices, in pointer order
        val t0 = SystemClock.uptimeMillis()
        var downTime = t0
        for (tt in times) {
            val wait = t0 + tt - SystemClock.uptimeMillis()
            if (wait > 0) SystemClock.sleep(wait)
            val now = SystemClock.uptimeMillis()
            // new fingers first, then movement, then lifted fingers
            for ((i, s) in strokes.withIndex()) if (s.startMs == tt && i !in active) {
                active.add(i)
                if (active.size == 1) downTime = now
                inject(strokes, active, tt, downTime, now,
                       if (active.size == 1) MotionEvent.ACTION_DOWN else pointerAction(MotionEvent.ACTION_POINTER_DOWN, active.size - 1))
            }
            if (active.isNotEmpty()) inject(strokes, active, tt, downTime, now, MotionEvent.ACTION_MOVE)
            for (i in strokes.indices.filter { it in active && strokes[it].startMs + strokes[it].durationMs == tt }) {
                val idx = active.indexOf(i)
                inject(strokes, active, tt, downTime, now,
                       if (active.size == 1) MotionEvent.ACTION_UP else pointerAction(MotionEvent.ACTION_POINTER_UP, idx))
                active.remove(i)
            }
            if (SystemClock.uptimeMillis() - t0 > timeoutMs) throw RpcError(Codes.TIMEOUT, "gesture injection overran $timeoutMs ms")
        }
        return true
    }

    private fun pointerAction(action: Int, index: Int) = action or (index shl MotionEvent.ACTION_POINTER_INDEX_SHIFT)

    private fun inject(strokes: List<Stroke>, active: List<Int>, t: Long, downTime: Long, now: Long, action: Int) {
        val props = Array(active.size) { k -> MotionEvent.PointerProperties().apply { id = active[k]; toolType = MotionEvent.TOOL_TYPE_FINGER } }
        val coords = Array(active.size) { k ->
            val p = position(strokes[active[k]], t)
            MotionEvent.PointerCoords().apply { x = p[0]; y = p[1]; pressure = 1f; size = 1f }
        }
        val ev = MotionEvent.obtain(downTime, now, action, active.size, props, coords, 0, 0, 1f, 1f, 0, 0,
                                    InputDevice.SOURCE_TOUCHSCREEN, 0)
        try {
            if (!ua.injectInputEvent(ev, true)) throw RpcError(Codes.CANCELLED, "the system refused an injected touch")
        } finally { ev.recycle() }
    }

    companion object {
        const val STEP_MS = 10L

        /** Where a stroke's finger is at time t (clamped to the stroke). */
        fun position(s: Stroke, t: Long): FloatArray {
            val pts = s.points
            if (pts.size == 1 || s.durationMs <= 0) return if (t <= s.startMs) pts[0] else pts.last()
            val f = ((t - s.startMs).toFloat() / s.durationMs).coerceIn(0f, 1f)
            val seg = FloatArray(pts.size - 1) { i -> Math.hypot((pts[i + 1][0] - pts[i][0]).toDouble(), (pts[i + 1][1] - pts[i][1]).toDouble()).toFloat() }
            var left = seg.sum() * f
            for (i in seg.indices) {
                if (left <= seg[i] || i == seg.lastIndex) {
                    val r = if (seg[i] > 0) (left / seg[i]).coerceIn(0f, 1f) else 1f
                    return floatArrayOf(pts[i][0] + (pts[i + 1][0] - pts[i][0]) * r, pts[i][1] + (pts[i + 1][1] - pts[i][1]) * r)
                }
                left -= seg[i]
            }
            return pts.last()
        }
    }
}
