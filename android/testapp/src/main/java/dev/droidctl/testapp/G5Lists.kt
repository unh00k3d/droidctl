package dev.droidctl.testapp

import android.graphics.Color
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView

object G5 {
    fun longList(s: Sc): View = with(s) {
        val n = int("rows", 1000)
        val rv = G3.recycler(s, (1..n).map { "Row $it" }, "row") { dta("click", it) }
        col(title("Long list"), rv.apply { layoutParams = lp(MATCH, MATCH) }, pad = 0)
    }

    fun listInsertTop(s: Sc): View = with(s) {
        val items = (1..10).map { "Item $it" }.toMutableList()
        val rv = G3.recycler(s, items, "item") { dta("click", it) }
        var k = 0
        every(int("interval_ms", 3000).toLong()) {
            k++
            items.add(0, "New $k")
            rv.adapter!!.notifyItemInserted(0)
            dta("insert", null, "item" to "New $k")
        }
        col(title("Feed"), rv.apply { layoutParams = lp(MATCH, MATCH) }, pad = 0)
    }

    fun duplicates(s: Sc): View = with(s) {
        val list = col(title("Items"), pad = 0)
        for (i in 1..20) {
            val label = text("Item $i", "label").apply { layoutParams = lp(0, WRAP, 1f) }
            list.addView(row(label, button("Delete", "delete").apply {
                setOnClickListener { dta("delete", "delete", "row" to i) }
            }).apply { setPadding(dp(16), 0, dp(8), 0) })
        }
        scroll(list)
    }

    fun lookalikeOk(s: Sc): View = with(s) {
        val frame = FrameLayout(act)
        fun screen(name: String): View = col(
            title("Screen $name", "screen_title"),
            text(if (name == "A") "Confirm your order?" else "Order placed. Close?"),
            button("OK", "ok").apply {
                setOnClickListener {
                    dta("click", "ok", "screen" to name)
                    if (name == "A") { frame.removeAllViews(); frame.addView(screen("B")) }
                }
            })
        frame.addView(screen("A"))
        frame
    }

    fun nestedScroll(s: Sc): View = with(s) {
        val feed = LinearLayout(act).apply { orientation = LinearLayout.VERTICAL; withId("feed") }
        for (i in 1..30) {
            if (i == 3) {
                val carousel = RecyclerView(act).apply {
                    withId("carousel")
                    contentDescription = "Carousel"
                    layoutManager = LinearLayoutManager(act, LinearLayoutManager.HORIZONTAL, false)
                    adapter = object : RecyclerView.Adapter<RecyclerView.ViewHolder>() {
                        override fun getItemCount() = 20
                        override fun onCreateViewHolder(p: ViewGroup, t: Int) = object : RecyclerView.ViewHolder(
                            TextView(act).apply {
                                textSize = 18f; gravity = Gravity.CENTER; isClickable = true
                                setBackgroundColor(Color.rgb(220, 230, 250))
                                layoutParams = RecyclerView.LayoutParams(dp(140), dp(120)).apply { setMargins(dp(8), dp(8), dp(8), dp(8)) }
                            }) {}
                        override fun onBindViewHolder(h: RecyclerView.ViewHolder, pos: Int) {
                            val label = "Card ${pos + 1}"
                            (h.itemView as TextView).text = label
                            h.itemView.setOnClickListener { dta("click", "card", "item" to label) }
                        }
                    }
                    addOnScrollListener(object : RecyclerView.OnScrollListener() {
                        override fun onScrollStateChanged(rv: RecyclerView, st: Int) {
                            if (st == RecyclerView.SCROLL_STATE_IDLE) dta("scroll", "carousel",
                                "first" to (rv.layoutManager as LinearLayoutManager).findFirstVisibleItemPosition())
                        }
                    })
                    layoutParams = LinearLayout.LayoutParams(MATCH, dp(136))
                }
                feed.addView(carousel)
            }
            feed.addView(text("Story $i", "story", 18f).apply {
                setPadding(dp(16), dp(16), dp(16), dp(16)); isClickable = true
                setOnClickListener { dta("click", "story", "item" to "Story $i") }
            })
        }
        scroll(feed)
    }

    fun infinite(s: Sc): View = with(s) {
        val items = (1..20).map { "Post $it" }.toMutableList()
        val rv = G3.recycler(s, items, "post") { dta("click", it) }
        var page = 1
        rv.addOnScrollListener(object : RecyclerView.OnScrollListener() {
            override fun onScrolled(r: RecyclerView, dx: Int, dy: Int) {
                val lm = r.layoutManager as LinearLayoutManager
                if (lm.findLastVisibleItemPosition() >= items.size - 2) {
                    page++
                    val start = items.size
                    items.addAll((start + 1..start + 20).map { "Post $it" })
                    r.post { r.adapter!!.notifyItemRangeInserted(start, 20) }
                    dta("load_more", null, "page" to page, "total" to items.size)
                }
            }
        })
        col(title("Infinite feed"), rv.apply { layoutParams = lp(MATCH, MATCH) }, pad = 0)
    }

    /** 100 nested levels, ~50 text nodes per level: about 5,000 nodes. */
    fun hugeTree(s: Sc): View = with(s) {
        val depth = int("depth", 100)
        val perLevel = int("per_level", 49)
        val root = LinearLayout(act).apply { orientation = LinearLayout.VERTICAL }
        var parent: LinearLayout = root
        for (d in 1..depth) {
            val level = LinearLayout(act).apply { orientation = LinearLayout.VERTICAL; setPadding(1, 0, 0, 0) }
            // explicit WRAP_CONTENT: a vertical LinearLayout's default MATCH_PARENT width
            // makes it measure children twice, which compounds over 100 levels (~17 s)
            for (k in 1..perLevel) level.addView(TextView(act).apply { text = "L$d.$k"; textSize = 10f }, lp(WRAP, WRAP))
            parent.addView(level, lp(WRAP, WRAP))
            parent = level
        }
        scroll(col(title("Huge tree"), root, pad = 4))
    }
}
