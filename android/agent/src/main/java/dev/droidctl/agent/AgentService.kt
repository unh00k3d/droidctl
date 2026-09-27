package dev.droidctl.agent

import android.accessibilityservice.AccessibilityService
import android.content.Intent
import android.util.Log
import android.view.accessibility.AccessibilityEvent

const val TAG = "droidctl"

/**
 * The accessibility service. It owns the socket server's lifetime: the server
 * runs exactly while the system has the service connected.
 */
class AgentService : AccessibilityService() {
    private var server: Server? = null
    val tree = Tree(this)
    val events = Events(this)

    override fun onServiceConnected() {
        Log.i(TAG, "service connected")
        stopServer()
        server = Server(Rpc(this, tree, events)).also { it.start() }
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        event ?: return
        // the content-generation counter: cheap, allocation-free, main thread only
        when (event.eventType) {
            AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED,
            AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED,
            AccessibilityEvent.TYPE_WINDOWS_CHANGED,
            AccessibilityEvent.TYPE_VIEW_SCROLLED,
            AccessibilityEvent.TYPE_VIEW_TEXT_CHANGED -> tree.gen++
        }
        try { events.onEvent(event, tree.gen) } catch (e: Exception) { Log.w(TAG, "event handling failed", e) }
    }

    override fun onInterrupt() {}

    override fun onUnbind(intent: Intent?): Boolean {
        Log.i(TAG, "service unbound")
        stopServer()
        return super.onUnbind(intent)
    }

    override fun onDestroy() {
        stopServer()
        super.onDestroy()
    }

    private fun stopServer() {
        server?.stop()
        server = null
    }
}
