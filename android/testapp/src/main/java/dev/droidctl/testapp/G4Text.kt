package dev.droidctl.testapp

import android.annotation.SuppressLint
import android.content.Context
import android.os.Bundle
import android.telephony.PhoneNumberFormattingTextWatcher
import android.text.Editable
import android.text.InputFilter
import android.text.InputType
import android.text.TextWatcher
import android.view.View
import android.view.accessibility.AccessibilityNodeInfo
import android.view.inputmethod.EditorInfo
import android.webkit.JavascriptInterface
import android.webkit.WebView
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.SearchView

object G4 {
    fun form(s: Sc): View = with(s) {
        val name = edit("Name", "name")
        val email = edit("Email", "email").apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_EMAIL_ADDRESS
            setText("not-an-email")
            error = "Invalid email"
            addTextChangedListener(object : TextWatcher {
                override fun beforeTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
                override fun onTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
                override fun afterTextChanged(e: Editable?) { error = if (e.toString().contains("@")) null else "Invalid email" }
            })
        }
        val notes = edit("Notes", "notes").apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_MULTI_LINE
            minLines = 3; isSingleLine = false
        }
        val age = edit("Age", "age").apply { inputType = InputType.TYPE_CLASS_NUMBER }
        val code = edit("Code (max 5)", "code").apply { filters = arrayOf(InputFilter.LengthFilter(5)) }
        val submit = button("Submit", "submit")
        submit.setOnClickListener {
            dta("submit", "submit", "name" to name.text.toString(), "email" to email.text.toString(),
                "notes" to notes.text.toString(), "age" to age.text.toString(), "code" to code.text.toString())
        }
        scroll(col(title("Form"), name, email, notes, age, code, submit))
    }

    fun unicode(s: Sc): View = with(s) { col(title("Unicode"), edit("Type here", "field")) }

    fun formatter(s: Sc): View = with(s) {
        val phone = edit("Phone number", "phone").apply {
            inputType = InputType.TYPE_CLASS_PHONE
            addTextChangedListener(PhoneNumberFormattingTextWatcher("US"))
        }
        col(title("Phone"), phone)
    }

    /** An EditText that refuses ACTION_SET_TEXT (paste and typing still work). */
    @SuppressLint("AppCompatCustomView", "ViewConstructor")
    class NoSetTextEdit(ctx: Context) : EditText(ctx) {
        override fun performAccessibilityAction(action: Int, args: Bundle?): Boolean =
            if (action == AccessibilityNodeInfo.ACTION_SET_TEXT) false else super.performAccessibilityAction(action, args)
    }

    fun rejectSetText(s: Sc): View = with(s) {
        val e = NoSetTextEdit(act).apply { hint = "Message"; withId("message") }
        watch(e, "message")
        col(title("Rejects set text"), e)
    }

    fun otp(s: Sc): View = with(s) {
        val boxes = (1..6).map { i ->
            EditText(act).apply {
                withId("otp$i")
                inputType = InputType.TYPE_CLASS_NUMBER
                textAlignment = View.TEXT_ALIGNMENT_CENTER
                hint = "•"
                layoutParams = lp(0, WRAP, 1f)
            }
        }
        var busy = false
        boxes.forEachIndexed { i, box ->
            box.addTextChangedListener(object : TextWatcher {
                override fun beforeTextChanged(t: CharSequence?, a: Int, b: Int, c: Int) {}
                override fun onTextChanged(t: CharSequence?, a: Int, b: Int, c: Int) {}
                override fun afterTextChanged(e: Editable?) {
                    if (busy) return
                    val v = e.toString().filter { it.isDigit() }
                    busy = true
                    if (v.length > 1) {
                        // a paste/set of the whole code into one box: spread it like real OTP screens
                        v.take(6 - i).forEachIndexed { k, ch -> boxes[i + k].setText(ch.toString()) }
                        boxes[minOf(5, i + v.length - 1)].requestFocus()
                    } else if (v.length == 1 && i < 5) {
                        boxes[i + 1].requestFocus()
                    }
                    busy = false
                    val code = boxes.joinToString("") { it.text.toString() }
                    dta("text", "otp${i + 1}", "value" to box.text.toString())
                    if (code.length == 6) dta("otp", "otp", "value" to code)
                }
            })
        }
        col(title("Enter code"), row(*boxes.toTypedArray()))
    }

    fun searchEnter(s: Sc): View = with(s) {
        val result = text("No search yet", "result")
        val sv = SearchView(act).apply {
            withId("search"); isIconifiedByDefault = false; queryHint = "Search"
            imeOptions = EditorInfo.IME_ACTION_SEARCH
            setOnQueryTextListener(object : SearchView.OnQueryTextListener {
                override fun onQueryTextSubmit(q: String): Boolean { dta("search", "search", "value" to q); result.text = "Results for \"$q\""; return true }
                override fun onQueryTextChange(q: String): Boolean { dta("text", "search", "value" to q); return false }
            })
        }
        col(title("Search"), sv, result)
    }

    @SuppressLint("SetJavaScriptEnabled", "JavascriptInterface", "AddJavascriptInterface")
    fun webviewForm(s: Sc): View = with(s) {
        val wv = WebView(act).apply {
            withId("web")
            settings.javaScriptEnabled = true
            addJavascriptInterface(object {
                @JavascriptInterface fun ev(name: String, value: String) { dta(name, "web", "value" to value) }
            }, "dta")
            layoutParams = LinearLayout.LayoutParams(MATCH, MATCH)
        }
        val html = """<!doctype html><html><head><meta name="viewport" content="width=device-width">
            <title>Web form</title></head><body style="font-family:sans-serif;padding:16px">
            <h2>Web form</h2>
            <label for="city">City</label><br>
            <input id="city" name="city" placeholder="City" oninput="dta.ev('text', this.value)"><br><br>
            <button id="go" onclick="dta.ev('submit', document.getElementById('city').value);document.getElementById('out').textContent='Sent: '+document.getElementById('city').value">Send</button>
            <p id="out"></p></body></html>"""
        wv.loadDataWithBaseURL("https://droidctl.test/", html, "text/html", "utf-8", null)
        col(title("WebView"), wv, pad = 8)
    }
}
