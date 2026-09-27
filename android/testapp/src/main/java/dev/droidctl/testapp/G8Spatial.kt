package dev.droidctl.testapp

import android.graphics.Color
import android.text.TextUtils
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.BaseAdapter
import android.widget.FrameLayout
import android.widget.GridView
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import androidx.coordinatorlayout.widget.CoordinatorLayout
import androidx.core.view.GravityCompat
import androidx.core.view.ViewCompat
import androidx.drawerlayout.widget.DrawerLayout
import androidx.recyclerview.widget.GridLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.google.android.material.bottomsheet.BottomSheetBehavior
import com.google.android.material.floatingactionbutton.FloatingActionButton
import com.google.android.material.navigation.NavigationView

object G8 {
    data class Item(val name: String, val cents: Int, var qty: Int)

    fun cart(s: Sc, rtl: Boolean = false): View = with(s) {
        val items = listOf(Item("Wireless Mouse", 2499, 1), Item("USB-C Cable", 999, 2), Item("Laptop Stand", 3999, 1))
        val total = text("", "total", 20f)
        val heading = title("")
        fun refresh() {
            heading.text = "Cart (${items.sumOf { it.qty }})"
            total.text = "Total $%.2f".format(items.sumOf { it.cents * it.qty } / 100.0)
        }
        val top = row(icon(android.R.drawable.ic_menu_revert, "Back", "back"),
            heading.apply { layoutParams = lp(0, WRAP, 1f) },
            icon(android.R.drawable.ic_menu_share, null, "ic_share")).apply { withId("top_bar") }
        val rows = col(pad = 0).apply { withId("cart_items") }
        for (item in items) {
            val qty = text("${item.qty}", "qty", 18f).apply { gravity = Gravity.CENTER; layoutParams = lp(dp(40), WRAP) }
            val label = col(text(item.name, "item_name"), text("$%.2f".format(item.cents / 100.0), "item_price", 14f), pad = 0)
                .apply { layoutParams = lp(0, WRAP, 1f) }
            val minus = button("−", "minus")
            val plus = button("+", "plus")
            minus.setOnClickListener { _ -> dta("dec", item.name, "qty" to (item.qty - 1).coerceAtLeast(0)); if (item.qty > 0) item.qty--; qty.text = "${item.qty}"; refresh() }
            plus.setOnClickListener { _ -> dta("inc", item.name, "qty" to item.qty + 1); item.qty++; qty.text = "${item.qty}"; refresh() }
            rows.addView(row(label, minus, qty, plus).apply { setPadding(dp(16), dp(4), dp(8), dp(4)) })
        }
        val checkout = button("Checkout", "checkout") { dta("checkout", null, "total" to total.text.toString()) }
        val bottom = row(total.apply { layoutParams = lp(0, WRAP, 1f) }, checkout).apply {
            withId("bottom_bar"); setPadding(dp(16), dp(8), dp(16), dp(8))
        }
        refresh()
        col(top, rows.apply { layoutParams = lp(MATCH, 0, 1f) }, bottom, pad = 0).apply {
            if (rtl) layoutDirection = View.LAYOUT_DIRECTION_RTL
        }
    }

    fun rtl(s: Sc): View {
        s.act.window.decorView.layoutDirection = View.LAYOUT_DIRECTION_RTL
        return cart(s, rtl = true)
    }

    /** October 2026 (starts on a Thursday) in a 7-column GridView (it reports collectionInfo). */
    fun calendar(s: Sc): View = with(s) {
        val cells = listOf("S", "M", "T", "W", "T", "F", "S") + List(4) { "" } + (1..31).map { "$it" }
        val grid = GridView(act).apply {
            withId("month_grid"); numColumns = 7
            adapter = object : BaseAdapter() {
                override fun getCount() = cells.size
                override fun getItem(p: Int) = cells[p]
                override fun getItemId(p: Int) = p.toLong()
                override fun isEnabled(p: Int) = p >= 11
                override fun getView(p: Int, cv: View?, parent: ViewGroup?): View = TextView(act).apply {
                    text = cells[p]; gravity = Gravity.CENTER; textSize = 16f
                    setPadding(0, dp(12), 0, dp(12))
                    if (p < 7) ViewCompat.setAccessibilityHeading(this, true)
                }
            }
            setOnItemClickListener { _, _, p, _ -> if (p >= 11) dta("click", "day${cells[p]}", "day" to cells[p].toInt()) }
            layoutParams = lp(MATCH, MATCH)
        }
        col(title("October 2026", "month_title"), grid)
    }

    fun keypad(s: Sc): View = with(s) {
        val pin = StringBuilder()
        val display = text("PIN: ", "pin_display", 24f)
        fun key(label: String, id: String, desc: String? = null) = button(label, id).apply {
            if (desc != null) contentDescription = desc
            layoutParams = lp(0, dp(72), 1f)
            setOnClickListener {
                when (label) {
                    "⌫" -> if (pin.isNotEmpty()) pin.setLength(pin.length - 1)
                    "OK" -> dta("pin", null, "value" to pin.toString())
                    else -> { pin.append(label); dta("key", null, "digit" to label) }
                }
                display.text = "PIN: " + "•".repeat(pin.length)
            }
        }
        val rows = listOf(listOf("1", "2", "3"), listOf("4", "5", "6"), listOf("7", "8", "9"))
            .map { r -> row(*r.map { key(it, "key_$it") }.toTypedArray()) }
        val last = row(key("⌫", "key_del", "Delete"), key("0", "key_0"), key("OK", "key_ok"))
        col(title("Enter PIN"), display, *rows.toTypedArray(), last)
    }

    fun photoGrid(s: Sc): View = with(s) {
        val colors = listOf(Color.rgb(200, 80, 80), Color.rgb(80, 160, 90), Color.rgb(70, 110, 200), Color.rgb(220, 180, 60))
        val rv = RecyclerView(act).apply {
            withId("photos")
            layoutManager = GridLayoutManager(act, 3)
            adapter = object : RecyclerView.Adapter<RecyclerView.ViewHolder>() {
                override fun getItemCount() = 12
                override fun onCreateViewHolder(p: ViewGroup, t: Int) = object : RecyclerView.ViewHolder(ImageView(act).apply {
                    isClickable = true; withId("photo")
                    layoutParams = RecyclerView.LayoutParams(MATCH, dp(120)).apply { setMargins(dp(2), dp(2), dp(2), dp(2)) }
                }) {}
                override fun onBindViewHolder(h: RecyclerView.ViewHolder, pos: Int) {
                    val iv = h.itemView as ImageView
                    iv.setBackgroundColor(colors[pos % 4])
                    // every fourth tile has no label at all
                    iv.contentDescription = if (pos % 4 == 3) null else "Photo ${pos + 1}"
                    iv.setOnClickListener { dta("click", "photo${pos + 1}", "row" to pos / 3 + 1, "col" to pos % 3 + 1) }
                }
            }
            layoutParams = lp(MATCH, MATCH)
        }
        col(title("Photos"), rv, pad = 4)
    }

    fun unlabeledIcons(s: Sc): View = with(s) {
        col(row(title("Document").apply { layoutParams = lp(0, WRAP, 1f) },
            icon(android.R.drawable.ic_menu_share, null, "ic_share"),
            icon(android.R.drawable.ic_menu_delete, null, "ic_delete"),
            icon(android.R.drawable.ic_menu_edit, null, "ic_edit"),
            icon(android.R.drawable.ic_menu_more, null, "ic_more")),
            text("Quarterly report.pdf"))
    }

    fun labelLeftForm(s: Sc): View = with(s) {
        fun left(label: String, id: String) = row(text(label).apply { layoutParams = lp(dp(100), WRAP) },
            edit(null, id).apply { layoutParams = lp(0, WRAP, 1f) })
        col(title("Contact"), left("Email", "f_email"), text("Password"), edit(null, "f_password").apply {
            inputType = android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_VARIATION_PASSWORD
        }, left("Phone", "f_phone"), button("Save", "save"))
    }

    fun cards(s: Sc): View = with(s) {
        fun card(plan: String, price: String) = col(title(plan), text(price), button("Buy", "buy").apply {
            setOnClickListener { dta("buy", null, "plan" to plan) }
        }, pad = 12).apply {
            setBackgroundColor(Color.rgb(240, 240, 250))
            layoutParams = lp(0, WRAP, 1f).apply { setMargins(dp(6), dp(6), dp(6), dp(6)) }
        }
        col(title("Plans"), row(card("Basic", "$5/mo"), card("Standard", "$10/mo")),
            row(card("Premium", "$20/mo"), card("Enterprise", "$50/mo")))
    }

    fun fabSheetDrawer(s: Sc): View = with(s) {
        val drawer = DrawerLayout(act).apply { withId("drawer") }
        val coord = CoordinatorLayout(act)
        val list = G3.recycler(s, (1..25).map { "Contact $it" }, "contact") { dta("click", it) }
        coord.addView(col(row(icon(android.R.drawable.ic_menu_sort_by_size, "Open navigation drawer", "hamburger") {
            drawer.openDrawer(GravityCompat.START) }, title("Contacts")), list, pad = 0), CoordinatorLayout.LayoutParams(MATCH, MATCH))
        val sheet = col(title("Filters"), button("Favorites only", "filter_fav"), button("Recent", "filter_recent"), pad = 12).apply {
            withId("sheet"); setBackgroundColor(Color.rgb(235, 235, 245))
        }
        coord.addView(sheet, CoordinatorLayout.LayoutParams(MATCH, WRAP).apply {
            behavior = BottomSheetBehavior<View>().apply { peekHeight = dp(72) }
        })
        val fab = FloatingActionButton(act).apply {
            setImageResource(android.R.drawable.ic_input_add); contentDescription = "Add"; withId("fab")
            setOnClickListener { dta("click", "fab") }
        }
        coord.addView(fab, CoordinatorLayout.LayoutParams(WRAP, WRAP).apply {
            gravity = Gravity.END or Gravity.BOTTOM; setMargins(dp(16), dp(16), dp(16), dp(96))
        })
        val nav = NavigationView(act).apply {
            withId("nav_view"); menu.add("All contacts"); menu.add("Groups")
            setNavigationItemSelectedListener { dta("nav", "drawer", "item" to it.title.toString()); drawer.closeDrawers(); true }
        }
        drawer.addView(coord, DrawerLayout.LayoutParams(MATCH, MATCH))
        drawer.addView(nav, DrawerLayout.LayoutParams(dp(280), MATCH, GravityCompat.START))
        drawer
    }

    fun layoutBugs(s: Sc): View = with(s) {
        val overlap = FrameLayout(act).apply {
            addView(button("Left", "left"), FrameLayout.LayoutParams(dp(160), dp(56)))
            addView(button("Right", "right"), FrameLayout.LayoutParams(dp(160), dp(56)).apply { leftMargin = dp(100) })
        }
        val ellipsized = text("This is a very long product title that will never fit in the space it was given", "long_title").apply {
            maxLines = 1; ellipsize = TextUtils.TruncateAt.END; layoutParams = lp(dp(160), WRAP)
        }
        val tiny = button("x", "tiny").apply { minWidth = 0; minHeight = 0; minimumWidth = 0; minimumHeight = 0; layoutParams = lp(dp(30), dp(30)); setPadding(0, 0, 0, 0) }
        col(title("Layout bugs"), overlap, ellipsized, tiny)
    }
}
