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

    override fun onServiceConnected() {
        Log.i(TAG, "service connected")
        stopServer()
        server = Server(Rpc(this)).also { it.start() }
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {}

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
