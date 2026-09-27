package dev.droidctl.testapp

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Rect
import android.os.Bundle
import android.view.GestureDetector
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.ViewGroup
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.widget.ArrayAdapter
import android.widget.AutoCompleteTextView
import android.widget.CheckBox
import android.widget.FrameLayout
import android.widget.ImageButton
import android.widget.LinearLayout
import android.widget.PopupMenu
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.SeekBar
import android.widget.Spinner
import android.widget.AdapterView
import android.widget.TextView
import androidx.appcompat.widget.SwitchCompat
import androidx.appcompat.widget.Toolbar
import androidx.core.view.GravityCompat
import androidx.core.view.accessibility.AccessibilityNodeInfoCompat
import androidx.customview.widget.ExploreByTouchHelper
import androidx.core.view.ViewCompat
import androidx.drawerlayout.widget.DrawerLayout
import androidx.recyclerview.widget.ItemTouchHelper
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import androidx.viewpager2.widget.ViewPager2
import com.google.android.material.bottomnavigation.BottomNavigationView
import com.google.android.material.navigation.NavigationView
import com.google.android.material.slider.RangeSlider
import com.google.android.material.tabs.TabLayout
import com.google.android.material.tabs.TabLayoutMediator

object G3 {
    fun buttons(s: Sc): View = with(s) {
        col(
            title("Buttons"),
            button("Save", "save"),
            icon(android.R.drawable.ic_menu_preferences, "Settings", "settings"),
            icon(android.R.drawable.ic_menu_help, null, "mystery"),
        )
    }

    fun toggle(s: Sc): View = with(s) {
        val wifi = SwitchCompat(act).apply {
            text = "Wi-Fi"; withId("wifi")
            setOnCheckedChangeListener { _, on -> dta("click", "wifi", "state" to on) }
        }
        val remember = CheckBox(act).apply {
            text = "Remember me"; withId("remember")
            setOnCheckedChangeListener { _, on -> dta("click", "remember", "state" to on) }
        }
        val group = RadioGroup(act).apply { withId("size") }
        for ((label, id) in listOf("Small" to "small", "Medium" to "medium", "Large" to "large")) {
            group.addView(RadioButton(act).apply { text = label; withId(id) })
        }
        group.setOnCheckedChangeListener { g, checked ->
            val name = res.getResourceEntryName(checked)
            dta("click", name, "state" to true)
        }
        col(title("Toggles"), wifi, remember, text("Size"), group)
    }

    fun counter(s: Sc): View = with(s) {
        var n = 0
        val count = text("Count: 0", "count", 20f)
        col(title("Counter"), count, button("Increment", "inc") { n++; count.text = "Count: $n" })
    }

    fun rowNested(s: Sc): View = with(s) {
        val people = listOf("Ada Lovelace" to "Lunch tomorrow?", "Alan Turing" to "Re: the paper",
            "Grace Hopper" to "Compiler notes", "Edsger Dijkstra" to "GOTO considered", "Barbara Liskov" to "Substitution")
        val list = col(title("Inbox"), pad = 0)
        people.forEachIndexed { i, (who, subject) ->
            val star = ImageButton(act).apply {
                setImageResource(android.R.drawable.btn_star_big_off)
                contentDescription = "Star"; withId("star")
                setBackgroundColor(Color.TRANSPARENT)
                setOnClickListener { dta("click", "star$i") }
            }
            val texts = LinearLayout(act).apply {
                orientation = LinearLayout.VERTICAL
                addView(text(who)); addView(text(subject, size = 14f))
                layoutParams = lp(0, WRAP, 1f)
            }
            list.addView(row(texts, star).apply {
                withId("row"); isClickable = true; isFocusable = true
                setPadding(dp(16), dp(8), dp(16), dp(8))
                setOnClickListener { dta("click", "row$i") }
            })
        }
        list
    }

    /** Only a touch listener. ACTION_CLICK "succeeds" but does nothing and sends no event. */
    @SuppressLint("ClickableViewAccessibility", "ViewConstructor")
    class TouchOnlyView(ctx: Context, val s: Sc) : TextView(ctx) {
        var n = 0
        init {
            text = "Touch me"; textSize = 20f; gravity = Gravity.CENTER
            setBackgroundColor(Color.rgb(200, 220, 255))
            setOnTouchListener { _, e ->
                if (e.action == MotionEvent.ACTION_UP) { n++; text = "Touched $n"; s.dta("tap", "touch") }
                true
            }
        }
        override fun onInitializeAccessibilityNodeInfo(info: AccessibilityNodeInfo) {
            super.onInitializeAccessibilityNodeInfo(info)
            info.isClickable = true
            info.addAction(AccessibilityNodeInfo.AccessibilityAction.ACTION_CLICK)
        }
        override fun performAccessibilityAction(action: Int, args: Bundle?): Boolean =
            if (action == AccessibilityNodeInfo.ACTION_CLICK) true else super.performAccessibilityAction(action, args)
    }

    fun touchOnly(s: Sc): View = with(s) {
        col(title("Touch only"), TouchOnlyView(act, s).apply { withId("touch"); layoutParams = lp(MATCH, dp(120)) })
    }

    /** Handles ACTION_CLICK (and touch) but never sends TYPE_VIEW_CLICKED and never changes. */
    @SuppressLint("ClickableViewAccessibility", "ViewConstructor")
    class ClickNoEventView(ctx: Context, val s: Sc) : TextView(ctx) {
        init {
            text = "Silent"; textSize = 20f; gravity = Gravity.CENTER
            setBackgroundColor(Color.rgb(255, 230, 200))
            setOnTouchListener { _, e -> if (e.action == MotionEvent.ACTION_UP) s.dta("click", "silent", "via" to "touch"); true }
        }
        override fun onInitializeAccessibilityNodeInfo(info: AccessibilityNodeInfo) {
            super.onInitializeAccessibilityNodeInfo(info)
            info.isClickable = true
            info.addAction(AccessibilityNodeInfo.AccessibilityAction.ACTION_CLICK)
        }
        override fun performAccessibilityAction(action: Int, args: Bundle?): Boolean {
            if (action == AccessibilityNodeInfo.ACTION_CLICK) { s.dta("click", "silent", "via" to "a11y"); return true }
            return super.performAccessibilityAction(action, args)
        }
        override fun sendAccessibilityEvent(eventType: Int) {
            if (eventType != AccessibilityEvent.TYPE_VIEW_CLICKED) super.sendAccessibilityEvent(eventType)
        }
    }

    fun clickNoEvent(s: Sc): View = with(s) {
        col(title("Click without event"), ClickNoEventView(act, s).apply { withId("silent"); layoutParams = lp(MATCH, dp(120)) })
    }

    fun longPress(s: Sc): View = with(s) {
        val target = text("Hold me", "hold", 20f).apply {
            setPadding(dp(24), dp(24), dp(24), dp(24))
            setBackgroundColor(Color.rgb(230, 230, 230))
            setOnLongClickListener { v ->
                dta("long_press", "hold")
                PopupMenu(act, v).apply {
                    menu.add("Copy"); menu.add("Share")
                    setOnMenuItemClickListener { dta("menu", "hold", "item" to it.title.toString()); true }
                }.show()
                true
            }
        }
        col(title("Long press"), target)
    }

    @SuppressLint("ClickableViewAccessibility")
    fun doubleTap(s: Sc): View = with(s) {
        var liked = false
        val photo = text("Photo (double-tap to like)", "photo", 18f).apply {
            gravity = Gravity.CENTER
            setBackgroundColor(Color.rgb(220, 240, 220))
            layoutParams = lp(MATCH, dp(240))
        }
        val gd = GestureDetector(act, object : GestureDetector.SimpleOnGestureListener() {
            override fun onDown(e: MotionEvent) = true
            override fun onSingleTapConfirmed(e: MotionEvent): Boolean { dta("single_tap", "photo"); return true }
            override fun onDoubleTap(e: MotionEvent): Boolean {
                liked = !liked
                photo.text = if (liked) "Liked ♥" else "Photo (double-tap to like)"
                dta("double_tap", "photo", "liked" to liked); return true
            }
        })
        photo.setOnTouchListener { _, e -> gd.onTouchEvent(e) }
        col(title("Double tap"), photo)
    }

    fun customActions(s: Sc): View = with(s) {
        val list = col(title("Messages"), pad = 0)
        for (i in 1..5) {
            val label = "Message $i"
            val rowView = text(label, "message", 18f).apply {
                setPadding(dp(16), dp(14), dp(16), dp(14))
                isClickable = true
                setOnClickListener { dta("click", "message$i") }
            }
            ViewCompat.addAccessibilityAction(rowView, "Archive") { v, _ ->
                dta("action", "message$i", "name" to "Archive"); list.removeView(v); true
            }
            ViewCompat.addAccessibilityAction(rowView, "Delete") { v, _ ->
                dta("action", "message$i", "name" to "Delete"); list.removeView(v); true
            }
            list.addView(rowView)
        }
        list
    }

    fun swipeOnlyDelete(s: Sc): View = with(s) {
        val items = (1..8).map { "Note $it" }.toMutableList()
        val rv = recycler(s, items, "note") { dta("click", it) }
        ItemTouchHelper(object : ItemTouchHelper.SimpleCallback(0, ItemTouchHelper.LEFT) {
            override fun onMove(r: RecyclerView, a: RecyclerView.ViewHolder, b: RecyclerView.ViewHolder) = false
            override fun onSwiped(vh: RecyclerView.ViewHolder, dir: Int) {
                val pos = vh.bindingAdapterPosition
                val gone = items.removeAt(pos)
                rv.adapter!!.notifyItemRemoved(pos)
                dta("swipe_delete", null, "item" to gone)
            }
        }).attachToRecyclerView(rv)
        col(title("Swipe to delete"), rv.apply { layoutParams = lp(MATCH, MATCH) })
    }

    fun slider(s: Sc): View = with(s) {
        val volLabel = text("Volume 3/10", "volume_label")
        val seek = SeekBar(act).apply {
            max = 10; progress = 3; withId("volume"); contentDescription = "Volume"
            setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
                override fun onProgressChanged(sb: SeekBar, p: Int, fromUser: Boolean) {
                    volLabel.text = "Volume $p/10"; dta("change", "volume", "value" to p)
                }
                override fun onStartTrackingTouch(sb: SeekBar) {}
                override fun onStopTrackingTouch(sb: SeekBar) {}
            })
        }
        val range = RangeSlider(act).apply {
            valueFrom = 0f; valueTo = 100f; stepSize = 1f; values = listOf(20f, 80f)
            withId("price"); contentDescription = "Price range"
            addOnChangeListener { sl, _, _ -> dta("change", "price", "values" to sl.values.map { it.toInt() }) }
        }
        col(title("Sliders"), volLabel, seek, text("Price"), range)
    }

    fun spinnerDropdown(s: Sc): View = with(s) {
        val fruits = listOf("Apple", "Banana", "Cherry")
        val spinner = Spinner(act).apply {
            withId("fruit")
            adapter = ArrayAdapter(act, android.R.layout.simple_spinner_dropdown_item, fruits)
            onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
                override fun onItemSelected(p: AdapterView<*>?, v: View?, pos: Int, id: Long) { dta("select", "fruit", "value" to fruits[pos]) }
                override fun onNothingSelected(p: AdapterView<*>?) {}
            }
        }
        val menuBtn = button("Menu", "menu")
        menuBtn.setOnClickListener { v ->
            dta("click", "menu")
            PopupMenu(act, v).apply {
                listOf("Rename", "Duplicate", "Remove").forEach { menu.add(it) }
                setOnMenuItemClickListener { dta("menu", "menu", "item" to it.title.toString()); true }
            }.show()
        }
        val countries = listOf("Turkey", "Tunisia", "Tuvalu", "Germany", "Georgia", "Ghana")
        val ac = AutoCompleteTextView(act).apply {
            hint = "Country"; withId("country"); threshold = 1
            setAdapter(ArrayAdapter(act, android.R.layout.simple_dropdown_item_1line, countries))
            setOnItemClickListener { p, _, pos, _ -> dta("select", "country", "value" to p.getItemAtPosition(pos).toString()) }
        }
        col(title("Dropdowns"), text("Fruit"), spinner, menuBtn, ac)
    }

    fun tabsPager(s: Sc): View = with(s) {
        val pages = listOf("Alpha", "Beta", "Gamma")
        val tabs = TabLayout(act).apply { withId("tabs") }
        val pager = ViewPager2(act).apply {
            withId("pager")
            adapter = object : RecyclerView.Adapter<RecyclerView.ViewHolder>() {
                override fun getItemCount() = pages.size
                override fun onCreateViewHolder(p: ViewGroup, t: Int) = object : RecyclerView.ViewHolder(
                    TextView(act).apply { textSize = 24f; gravity = Gravity.CENTER; layoutParams = ViewGroup.LayoutParams(MATCH, MATCH) }) {}
                override fun onBindViewHolder(h: RecyclerView.ViewHolder, pos: Int) { (h.itemView as TextView).text = "Page ${pages[pos]} content" }
            }
            registerOnPageChangeCallback(object : ViewPager2.OnPageChangeCallback() {
                override fun onPageSelected(pos: Int) { dta("page", "pager", "index" to pos, "name" to pages[pos]) }
            })
            layoutParams = lp(MATCH, 0, 1f)
        }
        TabLayoutMediator(tabs, pager) { tab, pos -> tab.text = pages[pos] }.attach()
        col(tabs, pager, pad = 0)
    }

    fun bottomNavDrawer(s: Sc): View = with(s) {
        val drawer = DrawerLayout(act).apply { withId("drawer") }
        val content = text("Home", "section", 24f).apply { gravity = Gravity.CENTER; layoutParams = lp(MATCH, 0, 1f) }
        val toolbar = Toolbar(act).apply {
            title = "Mail"; withId("toolbar")
            setNavigationIcon(android.R.drawable.ic_menu_sort_by_size)
            navigationContentDescription = "Open navigation drawer"
            setNavigationOnClickListener { dta("click", "hamburger"); drawer.openDrawer(GravityCompat.START) }
            menu.add("Settings").setOnMenuItemClickListener { dta("menu", "overflow", "item" to "Settings"); true }
            menu.add("Help").setOnMenuItemClickListener { dta("menu", "overflow", "item" to "Help"); true }
        }
        val bottom = BottomNavigationView(act).apply {
            withId("bottom_nav")
            listOf("Home" to android.R.drawable.ic_menu_view, "Search" to android.R.drawable.ic_menu_search,
                "Profile" to android.R.drawable.ic_menu_myplaces).forEachIndexed { i, (t, ic) -> menu.add(0, i + 1, i, t).setIcon(ic) }
            setOnItemSelectedListener { dta("nav", "bottom_nav", "item" to it.title.toString()); content.text = it.title; true }
        }
        val main = col(toolbar, content, bottom, pad = 0)
        val nav = NavigationView(act).apply {
            withId("nav_view")
            menu.add("Inbox"); menu.add("Sent"); menu.add("Trash")
            setNavigationItemSelectedListener { dta("nav", "drawer", "item" to it.title.toString()); drawer.closeDrawers(); true }
        }
        drawer.addView(main, DrawerLayout.LayoutParams(MATCH, MATCH))
        drawer.addView(nav, DrawerLayout.LayoutParams(dp(280), MATCH, GravityCompat.START))
        drawer
    }

    /** A game-like canvas: three drawn buttons, no accessibility info at all. */
    @SuppressLint("ClickableViewAccessibility", "ViewConstructor")
    class GameView(ctx: Context, val s: Sc) : View(ctx) {
        private val paint = Paint(Paint.ANTI_ALIAS_FLAG)
        private val names = listOf("A", "B", "C")
        private fun rects(): List<Rect> {
            val w = width / 3
            return names.indices.map { Rect(it * w + 20, height / 3, (it + 1) * w - 20, height / 3 + w - 40) }
        }
        init {
            importantForAccessibility = IMPORTANT_FOR_ACCESSIBILITY_NO
            setOnTouchListener { _, e ->
                if (e.action == MotionEvent.ACTION_UP) {
                    val hit = rects().indexOfFirst { it.contains(e.x.toInt(), e.y.toInt()) }
                    val loc = IntArray(2).also { getLocationOnScreen(it) }
                    s.dta("tap", if (hit >= 0) names[hit] else "none",
                        "x" to (e.x.toInt() + loc[0]), "y" to (e.y.toInt() + loc[1]))
                }
                true
            }
        }
        override fun onDraw(c: Canvas) {
            c.drawColor(Color.rgb(30, 30, 50))
            rects().forEachIndexed { i, r ->
                paint.color = listOf(Color.RED, Color.GREEN, Color.BLUE)[i]
                c.drawRect(r, paint)
                paint.color = Color.WHITE; paint.textSize = 64f
                c.drawText(names[i], r.exactCenterX() - 18, r.exactCenterY() + 22, paint)
            }
        }
    }

    fun canvas(s: Sc): View = with(s) { FrameLayout(act).apply { addView(GameView(act, s).apply { withId("game") }) } }

    /** A calendar drawn on one view, exposed as virtual a11y nodes (ExploreByTouchHelper). */
    @SuppressLint("ViewConstructor")
    class VirtualCalendar(ctx: Context, val s: Sc) : View(ctx) {
        private val paint = Paint(Paint.ANTI_ALIAS_FLAG).apply { textSize = 40f }
        private val days = 31
        private fun cell(d: Int): Rect {
            val w = width / 7; val h = w
            val i = d - 1 + 3 // October 2026 starts on a Thursday
            return Rect((i % 7) * w, (i / 7) * h, (i % 7 + 1) * w, (i / 7 + 1) * h)
        }
        private val helper = object : ExploreByTouchHelper(this) {
            override fun getVirtualViewAt(x: Float, y: Float): Int =
                (1..days).firstOrNull { cell(it).contains(x.toInt(), y.toInt()) } ?: INVALID_ID
            override fun getVisibleVirtualViews(ids: MutableList<Int>) { ids.addAll(1..days) }
            override fun onPopulateNodeForVirtualView(id: Int, node: AccessibilityNodeInfoCompat) {
                node.contentDescription = "October $id"
                @Suppress("DEPRECATION") node.setBoundsInParent(cell(id))
                node.addAction(AccessibilityNodeInfoCompat.ACTION_CLICK)
                node.isClickable = true
            }
            override fun onPerformActionForVirtualView(id: Int, action: Int, args: Bundle?): Boolean {
                if (action != AccessibilityNodeInfoCompat.ACTION_CLICK) return false
                pick(id, "a11y"); return true
            }
        }
        var picked = 0
        fun pick(d: Int, via: String) {
            picked = d; invalidate()
            s.dta("click", "day$d", "via" to via)
            helper.invalidateVirtualView(d)
            helper.sendEventForVirtualView(d, AccessibilityEvent.TYPE_VIEW_CLICKED)
        }
        init {
            ViewCompat.setAccessibilityDelegate(this, helper)
            setOnTouchListener { _, e ->
                if (e.action == MotionEvent.ACTION_UP) {
                    val d = helper.let { (1..days).firstOrNull { cell(it).contains(e.x.toInt(), e.y.toInt()) } }
                    if (d != null) pick(d, "touch")
                }
                true
            }
        }
        override fun dispatchHoverEvent(e: MotionEvent) = helper.dispatchHoverEvent(e) || super.dispatchHoverEvent(e)
        override fun onDraw(c: Canvas) {
            for (d in 1..days) {
                val r = cell(d)
                paint.color = if (d == picked) Color.rgb(120, 170, 255) else Color.rgb(235, 235, 235)
                c.drawRect(r.left + 4f, r.top + 4f, r.right - 4f, r.bottom - 4f, paint)
                paint.color = Color.BLACK
                c.drawText("$d", r.left + 20f, r.exactCenterY() + 14, paint)
            }
        }
    }

    @SuppressLint("ClickableViewAccessibility")
    fun virtualViews(s: Sc): View = with(s) {
        col(title("October 2026"), VirtualCalendar(act, s).apply { withId("month"); layoutParams = lp(MATCH, dp(360)) })
    }

    fun dragReorder(s: Sc): View = with(s) {
        val items = (1..8).map { "Task $it" }.toMutableList()
        lateinit var touch: ItemTouchHelper
        val rv = RecyclerView(act).apply {
            withId("tasks")
            layoutManager = LinearLayoutManager(act)
            layoutParams = lp(MATCH, MATCH)
        }
        rv.adapter = object : RecyclerView.Adapter<RecyclerView.ViewHolder>() {
            override fun getItemCount() = items.size
            @SuppressLint("ClickableViewAccessibility")
            override fun onCreateViewHolder(p: ViewGroup, t: Int): RecyclerView.ViewHolder {
                val handle = ImageButton(act).apply {
                    setImageResource(android.R.drawable.ic_menu_sort_by_size); contentDescription = "Drag handle"; withId("handle")
                    setBackgroundColor(Color.TRANSPARENT)
                }
                val label = text("", "task", 18f).apply { layoutParams = lp(0, WRAP, 1f) }
                val r = row(label, handle).apply { setPadding(dp(16), dp(10), dp(8), dp(10)) }
                val vh = object : RecyclerView.ViewHolder(r) {}
                handle.setOnTouchListener { _, e -> if (e.actionMasked == MotionEvent.ACTION_DOWN) touch.startDrag(vh); false }
                return vh
            }
            override fun onBindViewHolder(h: RecyclerView.ViewHolder, pos: Int) {
                ((h.itemView as LinearLayout).getChildAt(0) as TextView).text = items[pos]
            }
        }
        touch = ItemTouchHelper(object : ItemTouchHelper.SimpleCallback(ItemTouchHelper.UP or ItemTouchHelper.DOWN, 0) {
            override fun isLongPressDragEnabled() = false
            override fun onMove(r: RecyclerView, a: RecyclerView.ViewHolder, b: RecyclerView.ViewHolder): Boolean {
                val from = a.bindingAdapterPosition; val to = b.bindingAdapterPosition
                items.add(to, items.removeAt(from)); r.adapter!!.notifyItemMoved(from, to)
                return true
            }
            override fun clearView(r: RecyclerView, vh: RecyclerView.ViewHolder) {
                super.clearView(r, vh); dta("reorder", "tasks", "order" to items.toList())
            }
            override fun onSwiped(vh: RecyclerView.ViewHolder, dir: Int) {}
        })
        touch.attachToRecyclerView(rv)
        col(title("Reorder"), rv, pad = 8)
    }

    /** A simple RecyclerView of clickable text rows sharing resource id [id]. */
    fun recycler(s: Sc, items: List<String>, id: String, onClick: (String) -> Unit): RecyclerView = with(s) {
        RecyclerView(act).apply {
            withId("${id}_list")
            layoutManager = LinearLayoutManager(act)
            adapter = object : RecyclerView.Adapter<RecyclerView.ViewHolder>() {
                override fun getItemCount() = items.size
                override fun onCreateViewHolder(p: ViewGroup, t: Int) = object : RecyclerView.ViewHolder(
                    text("", id, 18f).apply {
                        setPadding(dp(16), dp(14), dp(16), dp(14)); isClickable = true
                        layoutParams = RecyclerView.LayoutParams(MATCH, WRAP)
                    }) {}
                override fun onBindViewHolder(h: RecyclerView.ViewHolder, pos: Int) {
                    val label = items[pos]
                    (h.itemView as TextView).text = label
                    h.itemView.setOnClickListener { onClick(label) }
                }
            }
        }
    }

}
