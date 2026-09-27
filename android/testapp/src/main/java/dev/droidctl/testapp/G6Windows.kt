package dev.droidctl.testapp

import android.Manifest
import android.app.Dialog
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.view.View
import android.view.inputmethod.InputMethodManager
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AlertDialog
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import com.google.android.material.bottomsheet.BottomSheetDialog
import com.google.android.material.snackbar.Snackbar

object G6 {
    fun dialogs(s: Sc): View = with(s) {
        fun alert() {
            dta("dialog_shown", "alert")
            val d = AlertDialog.Builder(act).setTitle("Delete file?").setMessage("This cannot be undone.")
                .setPositiveButton("Delete") { _, _ -> dta("choice", "alert", "button" to "Delete") }
                .setNegativeButton("Cancel") { _, _ -> dta("choice", "alert", "button" to "Cancel") }
                .setOnCancelListener { dta("dismiss", "alert") }
                .show()
            onDispose { d.dismiss() }
        }
        fun full() {
            dta("dialog_shown", "full")
            val d = Dialog(act, android.R.style.Theme_Material_Light_NoActionBar_Fullscreen)
            d.setContentView(col(title("Full screen dialog"), text("Edit your profile"),
                button("Save", "dialog_save") { d.dismiss() }))
            d.setOnCancelListener { dta("dismiss", "full") }
            d.show()
            onDispose { d.dismiss() }
        }
        fun sheet() {
            dta("dialog_shown", "sheet")
            val d = BottomSheetDialog(act)
            d.setContentView(col(title("Share via"),
                button("Copy link", "sheet_copy") { d.dismiss() },
                button("Email", "sheet_email") { d.dismiss() }))
            d.setOnCancelListener { dta("dismiss", "sheet") }
            d.show()
            onDispose { d.dismiss() }
        }
        val root = col(title("Dialogs"), button("Alert", "open_alert") { alert() },
            button("Full screen", "open_full") { full() }, button("Sheet", "open_sheet") { sheet() })
        when (str("open")) { "alert" -> root.post { alert() }; "full" -> root.post { full() }; "sheet" -> root.post { sheet() } }
        root
    }

    fun snackbarToast(s: Sc): View = with(s) {
        lateinit var root: View
        root = col(title("Messages"),
            button("Snackbar", "show_snackbar") {
                Snackbar.make(root, "Message archived", Snackbar.LENGTH_INDEFINITE)
                    .setAction("Undo") { dta("undo", "snackbar") }.show()
            },
            button("Toast", "show_toast") { Toast.makeText(act, "Saved!", Toast.LENGTH_SHORT).show() })
        root
    }

    const val REQ_CAMERA = 42

    fun permission(s: Sc): View = with(s) {
        val status = text(status(s), "perm_status")
        act.permissionListener = { granted -> dta("permission", "camera", "granted" to granted); status.text = status(s) }
        onDispose { act.permissionListener = null }
        col(title("Permission"), status, button("Request camera", "request") {
            act.requestPermissions(arrayOf(Manifest.permission.CAMERA), REQ_CAMERA)
        })
    }

    private fun status(s: Sc) = "Camera: " +
        if (s.act.checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) "granted" else "not granted"

    fun notification(s: Sc): View = with(s) {
        col(title("Notification"), button("Notify", "notify") {
            if (Build.VERSION.SDK_INT >= 33 &&
                act.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
                act.requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 43)
                return@button
            }
            val nm = act.getSystemService(NotificationManager::class.java)
            nm.createNotificationChannel(NotificationChannel("droidctl", "droidctl tests", NotificationManager.IMPORTANCE_DEFAULT))
            val pi = PendingIntent.getBroadcast(act, 0, Intent(act, NotificationActionReceiver::class.java),
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
            val n = android.app.Notification.Builder(act, "droidctl")
                .setSmallIcon(android.R.drawable.stat_notify_chat)
                .setContentTitle("droidctl test notification")
                .setContentText("Tap Mark read")
                .addAction(android.app.Notification.Action.Builder(null, "Mark read", pi).build())
                .setAutoCancel(true).build()
            nm.notify(1, n)
            dta("posted", "notification")
        })
    }

    fun backConfirm(s: Sc): View = with(s) {
        val cb = object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                dta("back", null)
                val d = AlertDialog.Builder(act).setTitle("Exit?")
                    .setPositiveButton("Exit") { _, _ -> dta("choice", "exit", "button" to "Exit"); isEnabled = false; act.finish() }
                    .setNegativeButton("Stay") { _, _ -> dta("choice", "exit", "button" to "Stay") }
                    .show()
                onDispose { d.dismiss() }
            }
        }
        act.onBackPressedDispatcher.addCallback(cb)
        onDispose { cb.remove() }
        col(title("Editor"), text("Press back to exit"), edit("Draft", "draft"))
    }

    fun deepLink(s: Sc): View = with(s) {
        col(title("Deep link"), text("This screen answers droidctl-test://s/<scenario>."),
            button("Open form", "open_form") { act.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse("droidctl-test://s/form"))) })
    }

    fun keyboardToggle(s: Sc): View = with(s) {
        val input = edit("Type something", "input")
        val root = col(title("Keyboard"), input, button("Hide keyboard", "hide") {
            act.getSystemService(InputMethodManager::class.java).hideSoftInputFromWindow(input.windowToken, 0)
            input.clearFocus()
        })
        var last: Boolean? = null
        ViewCompat.setOnApplyWindowInsetsListener(root) { v, insets ->
            val shown = insets.isVisible(WindowInsetsCompat.Type.ime())
            if (shown != last) { last = shown; dta("ime", null, "shown" to shown) }
            insets
        }
        root
    }
}

class NotificationActionReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        dtaLine("notification", "action", "mark_read", null, emptyArray())
        context.getSystemService(NotificationManager::class.java).cancel(1)
    }
}
