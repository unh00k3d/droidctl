package dev.droidctl.agent

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.AccessibilityServiceInfo
import android.accessibilityservice.GestureDescription
import android.content.Context
import android.graphics.Path
import android.os.Build
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityWindowInfo
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

/**
 * What the agent needs from whatever gives it accessibility access (PLAN.md
 * "Device backends"). Tree, Events, Actions and Rpc only talk to this, so the
 * same code serves both backends:
 *  - [ServiceHost]: our AccessibilityService, enabled in settings (backend A, the default);
 *  - [ShellHost]: a UiAutomation held by an `app_process` started over adb (backend B).
 */
/** One finger: a polyline in screen px (one point = hold still), from startMs for durationMs. */
class Stroke(val points: List<FloatArray>, val startMs: Long, val durationMs: Long)

interface Host {
    /** "a11y" or "uiautomation": reported by ping, so the host can show which one answers. */
    val backend: String
    val context: Context
    fun windows(): List<AccessibilityWindowInfo>
    fun activeRoot(): AccessibilityNodeInfo?
    fun getInfo(): AccessibilityServiceInfo?
    fun setInfo(info: AccessibilityServiceInfo)
    fun global(id: Int): Boolean
    /** Runs the gesture to its end: true = completed, false = cancelled. Throws RpcError if refused. */
    fun runGesture(strokes: List<Stroke>, timeoutMs: Long): Boolean
    /** (versionName, versionCode) of this agent build. */
    fun version(): Pair<String, Long>
    /** Must be called on the main looper. */
    fun clipboard(): android.content.ClipboardManager =
        context.getSystemService(android.content.ClipboardManager::class.java)
}

/**
 * The event entry point shared by both backends: bump the content generation,
 * then record the event. Cheap and allocation-free for the common types.
 */
class Core(host: Host) {
    val tree = Tree(host)
    val events = Events(host)

    fun onEvent(event: AccessibilityEvent) {
        when (event.eventType) {
            AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED,
            AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED,
            AccessibilityEvent.TYPE_WINDOWS_CHANGED,
            AccessibilityEvent.TYPE_VIEW_SCROLLED,
            AccessibilityEvent.TYPE_VIEW_TEXT_CHANGED -> tree.gen++
        }
        try { events.onEvent(event, tree.gen) } catch (e: Exception) { android.util.Log.w(TAG, "event handling failed", e) }
    }
}

/**
 * An exception on one of the agent's own background threads (connections, subscriptions,
 * idle watchdog) must never take the process down: for backend A that is the user's
 * accessibility service, and Android puts a "keeps stopping" dialog over their app.
 * Such a thread just ends (its connection closes); the main thread keeps the default.
 */
fun guardBackgroundThreads() {
    val default = Thread.getDefaultUncaughtExceptionHandler()
    Thread.setDefaultUncaughtExceptionHandler { t, e ->
        if (t.name.startsWith("droidctl-")) android.util.Log.e(TAG, "thread ${t.name} died", e)
        else default?.uncaughtException(t, e)
    }
}

/** Backend A: the enabled accessibility service. */
class ServiceHost(private val svc: AgentService) : Host {
    val service get() = svc
    override val backend = "a11y"
    override val context: Context get() = svc
    override fun windows(): List<AccessibilityWindowInfo> = svc.windows
    override fun activeRoot(): AccessibilityNodeInfo? = svc.rootInActiveWindow
    override fun getInfo(): AccessibilityServiceInfo? = svc.serviceInfo
    override fun setInfo(info: AccessibilityServiceInfo) { svc.serviceInfo = info }
    override fun global(id: Int) = svc.performGlobalAction(id)

    override fun runGesture(strokes: List<Stroke>, timeoutMs: Long): Boolean {
        val b = GestureDescription.Builder()
        for (s in strokes) {
            val path = Path().apply {
                moveTo(s.points[0][0], s.points[0][1])
                for (i in 1 until s.points.size) lineTo(s.points[i][0], s.points[i][1])
            }
            b.addStroke(GestureDescription.StrokeDescription(path, s.startMs, s.durationMs))
        }
        val g = b.build()
        val done = CountDownLatch(1)
        val result = AtomicReference<Boolean>(null)
        val cb = object : AccessibilityService.GestureResultCallback() {
            override fun onCompleted(d: GestureDescription?) { result.set(true); done.countDown() }
            override fun onCancelled(d: GestureDescription?) { result.set(false); done.countDown() }
        }
        if (!svc.dispatchGesture(g, cb, null))
            throw RpcError(Codes.CANCELLED, "the system refused the gesture")
        if (!done.await(timeoutMs, TimeUnit.MILLISECONDS))
            throw RpcError(Codes.TIMEOUT, "no gesture completion callback within $timeoutMs ms")
        return result.get() == true
    }

    // fixed for the life of the process (an APK upgrade restarts it); the lookup costs ~3 ms of binder
    private val ver by lazy {
        val pkg = svc.packageManager.getPackageInfo(svc.packageName, 0)
        @Suppress("DEPRECATION")
        (pkg.versionName ?: "?") to (if (Build.VERSION.SDK_INT >= 28) pkg.longVersionCode else pkg.versionCode.toLong())
    }
    override fun version() = ver
}
