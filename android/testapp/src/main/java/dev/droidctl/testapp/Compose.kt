package dev.droidctl.testapp

import android.view.View
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Slider
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.ExperimentalComposeUiApi
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.ComposeView
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.semantics.CustomAccessibilityAction
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.customActions
import androidx.compose.ui.semantics.error
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.testTagsAsResourceId
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp

/** Compose variants: their accessibility trees differ from Views (merged semantics, desc-on-child). */
object Cmp {
    @OptIn(ExperimentalComposeUiApi::class)
    private fun host(s: Sc, content: @Composable ColumnScope.() -> Unit): View = ComposeView(s.act).apply {
        setContent {
            MaterialTheme {
                // testTagsAsResourceId: tags show up as viewIdResourceName, like View ids
                Column(Modifier.fillMaxSize().semantics { testTagsAsResourceId = true }.padding(16.dp)) { content() }
            }
        }
    }

    @Composable private fun Title(t: String) =
        Text(t, style = MaterialTheme.typography.headlineSmall, modifier = Modifier.semantics { heading() }.padding(bottom = 8.dp))

    fun buttons(s: Sc) = host(s) {
        Title("Buttons")
        Button(onClick = { s.dta("click", "save") }, Modifier.testTag("save")) { Text("Save") }
        IconButton(onClick = { s.dta("click", "settings") }, Modifier.testTag("settings")) {
            Icon(painterResource(android.R.drawable.ic_menu_preferences), contentDescription = "Settings")
        }
        IconButton(onClick = { s.dta("click", "mystery") }, Modifier.testTag("mystery")) {
            Icon(painterResource(android.R.drawable.ic_menu_help), contentDescription = null)
        }
    }

    fun toggle(s: Sc) = host(s) {
        Title("Toggles")
        var wifi by remember { mutableStateOf(false) }
        var remember by remember { mutableStateOf(false) }
        var size by remember { mutableStateOf("") }
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("Wi-Fi", Modifier.weight(1f))
            Switch(wifi, { wifi = it; s.dta("click", "wifi", "state" to it) }, Modifier.testTag("wifi"))
        }
        Row(verticalAlignment = Alignment.CenterVertically) {
            Checkbox(remember, { remember = it; s.dta("click", "remember", "state" to it) }, Modifier.testTag("remember"))
            Text("Remember me")
        }
        for (opt in listOf("Small", "Medium", "Large")) {
            Row(Modifier.clickable { size = opt; s.dta("click", opt.lowercase(), "state" to true) }.testTag(opt.lowercase()),
                verticalAlignment = Alignment.CenterVertically) {
                RadioButton(size == opt, null); Text(opt)
            }
        }
    }

    fun counter(s: Sc) = host(s) {
        Title("Counter")
        var n by remember { mutableIntStateOf(0) }
        Text("Count: $n", Modifier.testTag("count"))
        Button(onClick = { n++; s.dta("click", "inc") }, Modifier.testTag("inc")) { Text("Increment") }
    }

    fun rowNested(s: Sc) = host(s) {
        Title("Inbox")
        val people = listOf("Ada Lovelace" to "Lunch tomorrow?", "Alan Turing" to "Re: the paper", "Grace Hopper" to "Compiler notes")
        people.forEachIndexed { i, (who, subject) ->
            Row(Modifier.fillMaxWidth().clickable { s.dta("click", "row$i") }.testTag("row").padding(8.dp),
                verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) { Text(who); Text(subject, style = MaterialTheme.typography.bodySmall) }
                IconButton(onClick = { s.dta("click", "star$i") }, Modifier.testTag("star")) {
                    Icon(painterResource(android.R.drawable.btn_star_big_off), contentDescription = "Star")
                }
            }
        }
    }

    fun customActions(s: Sc) = host(s) {
        Title("Messages")
        val msgs = remember { mutableStateListOf(1, 2, 3, 4, 5) }
        for (i in msgs.toList()) {
            Text("Message $i", Modifier.fillMaxWidth().testTag("message")
                .semantics {
                    customActions = listOf(
                        CustomAccessibilityAction("Archive") { s.dta("action", "message$i", "name" to "Archive"); msgs.remove(i); true },
                        CustomAccessibilityAction("Delete") { s.dta("action", "message$i", "name" to "Delete"); msgs.remove(i); true })
                }
                .clickable { s.dta("click", "message$i") }.padding(14.dp))
        }
    }

    fun form(s: Sc) = host(s) {
        Title("Form")
        var name by remember { mutableStateOf("") }
        var email by remember { mutableStateOf("not-an-email") }
        var pw by remember { mutableStateOf("") }
        OutlinedTextField(name, { name = it; s.dta("text", "name", "value" to it) }, Modifier.testTag("name"), label = { Text("Name") })
        OutlinedTextField(email, { email = it; s.dta("text", "email", "value" to it) },
            Modifier.testTag("email").semantics { if (!email.contains("@")) error("Invalid email") },
            label = { Text("Email") }, isError = !email.contains("@"))
        OutlinedTextField(pw, { pw = it; s.dta("text", "password", "len" to it.length, "value" to it) }, Modifier.testTag("password"),
            label = { Text("Password") }, visualTransformation = PasswordVisualTransformation())
        Button(onClick = { s.dta("submit", "submit", "name" to name, "email" to email) }, Modifier.testTag("submit")) { Text("Submit") }
    }

    fun list(s: Sc) = host(s) {
        Title("Long list")
        val n = s.int("rows", 1000)
        LazyColumn(Modifier.fillMaxSize().testTag("row_list")) {
            items((1..n).toList()) { i ->
                Text("Row $i", Modifier.fillMaxWidth().clickable { s.dta("click", "Row $i") }.testTag("row").padding(14.dp))
            }
        }
    }

    fun duplicates(s: Sc) = host(s) {
        Title("Items")
        Column(Modifier.verticalScroll(rememberScrollState())) {
            for (i in 1..20) Row(verticalAlignment = Alignment.CenterVertically) {
                Text("Item $i", Modifier.weight(1f))
                Button(onClick = { s.dta("delete", "delete", "row" to i) }, Modifier.testTag("delete")) { Text("Delete") }
            }
        }
    }

    fun slider(s: Sc) = host(s) {
        Title("Sliders")
        var v by remember { mutableStateOf(3f) }
        Text("Volume ${v.toInt()}/10")
        Slider(v, { v = it; s.dta("change", "volume", "value" to it.toInt()) }, Modifier.testTag("volume").semantics { contentDescription = "Volume" },
            valueRange = 0f..10f, steps = 9)
    }

    fun cart(s: Sc) = host(s) {
        val names = listOf("Wireless Mouse" to 2499, "USB-C Cable" to 999, "Laptop Stand" to 3999)
        val qty = remember { mutableStateListOf(1, 2, 1) }
        Row(verticalAlignment = Alignment.CenterVertically) {
            IconButton(onClick = { s.dta("click", "back") }, Modifier.testTag("back")) {
                Icon(painterResource(android.R.drawable.ic_menu_revert), contentDescription = "Back")
            }
            Text("Cart (${qty.sum()})", Modifier.weight(1f).semantics { heading() }, style = MaterialTheme.typography.headlineSmall)
            IconButton(onClick = { s.dta("click", "ic_share") }, Modifier.testTag("ic_share")) {
                Icon(painterResource(android.R.drawable.ic_menu_share), contentDescription = null)
            }
        }
        Column(Modifier.weight(1f)) {
            names.forEachIndexed { i, (name, cents) ->
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) { Text(name); Text("$%.2f".format(cents / 100.0)) }
                    Button(onClick = { if (qty[i] > 0) qty[i]--; s.dta("dec", name, "qty" to qty[i]) }, Modifier.testTag("minus")) { Text("−") }
                    Text("${qty[i]}", Modifier.width(40.dp).testTag("qty"))
                    Button(onClick = { qty[i]++; s.dta("inc", name, "qty" to qty[i]) }, Modifier.testTag("plus")) { Text("+") }
                }
            }
        }
        val total = names.indices.sumOf { names[it].second * qty[it] } / 100.0
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("Total $%.2f".format(total), Modifier.weight(1f).testTag("total"))
            Button(onClick = { s.dta("checkout", null, "total" to total) }, Modifier.testTag("checkout")) { Text("Checkout") }
        }
    }

    fun calendar(s: Sc) = host(s) {
        Title("October 2026")
        val cells = listOf("S", "M", "T", "W", "T", "F", "S") + List(4) { "" } + (1..31).map { "$it" }
        LazyVerticalGrid(GridCells.Fixed(7), Modifier.testTag("month_grid"),
            horizontalArrangement = Arrangement.spacedBy(2.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
            items(cells.size) { p ->
                val c = cells[p]
                val m = if (p >= 11) Modifier.clickable { s.dta("click", "day$c", "day" to c.toInt()) }.background(Color(0xFFEEEEEE))
                    else Modifier
                Text(c, m.padding(vertical = 12.dp), textAlign = androidx.compose.ui.text.style.TextAlign.Center)
            }
        }
    }
}
