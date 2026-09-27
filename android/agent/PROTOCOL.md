# droidctl agent protocol (v3)

The language-neutral contract between a host and the on-device agent
(`dev.droidctl.agent`). Any client (the Python host, a future Go client, a test)
that follows this document can drive the agent.

## Transport
- The agent's accessibility service listens on the **abstract unix socket `droidctl`**
  (`LocalServerSocket`), only while the service is enabled and bound. Disabling the
  service closes the listener and every open connection.
- The host reaches it through adb: `adb forward tcp:N localabstract:droidctl`, then a
  plain TCP connection to `127.0.0.1:N`.
- Multiple connections may be open at once. Requests on one connection are handled
  **in order**, one at a time; each reply is written before the next request is read.
- If the name is still held by a previous instance, the service retries the bind 5
  times with a growing backoff (200 ms × attempt), then gives up and logs.

## Authentication
- On accept the agent reads the peer's credentials (`SO_PEERCRED`). Only **uid 2000**
  (shell, i.e. adbd on production builds) and **uid 0** (root: adbd after `adb root`,
  rooted or custom-ROM devices) are accepted.
- Any other peer (another app on the phone) receives one line and is disconnected:
  `{"jsonrpc":"2.0","id":null,"error":{"code":-32001,"message":"unauthorized uid <N>"}}`
- Rejections are logged under logcat tag `droidctl`. There is no token.

## Framing
- **NDJSON**: UTF-8, one JSON object per line, terminated by `\n`, in both directions.
  Objects must not contain raw newlines (standard JSON encoders escape them).
- A request line longer than **1 MiB** is discarded whole and answered with
  `-32600` (`id` null); the connection stays usable.
- Blank lines are ignored.

## Messages (JSON-RPC 2.0)
- Request: `{"jsonrpc":"2.0","id":<number|string>,"method":"<name>","params":{...}}`.
  `params` is optional; if present it must be an object.
- Response: `{"jsonrpc":"2.0","id":<same>,"result":{...}}` or
  `{"jsonrpc":"2.0","id":<same>,"error":{"code":<int>,"message":"<text>"}}`.
- A request without `id` is a **notification** and gets no reply (not even an error).
- Server-pushed notifications: after `subscribe`, the agent writes
  `{"jsonrpc":"2.0","method":"event","params":<event>}` lines on that connection,
  interleaved with replies (never inside one: writes are serialized per connection).
  A client must accept a notification while waiting for a reply.

### Error codes
| code | meaning |
|---|---|
| -32700 | parse error (the line is not JSON); `id` null |
| -32600 | invalid request (no `method`, `jsonrpc` is not `"2.0"`, or the line is too long) |
| -32601 | method not found |
| -32602 | invalid params (`params` is not an object, or a bad parameter) |
| -32603 | internal error; `message` is the exception text. The service itself never crashes |
| -32001 | unauthorized peer uid |
| -32002 | stale: the request names a dump that is no longer the latest (handles are only valid for the latest dump) |
| -32003 | gone: the handle's node no longer exists (`refresh()` failed) |
| -32004 | disabled: the node is not enabled (bypass with `force`) |
| -32005 | not visible: the node is not visible to the user (bypass with `force`; scroll/show_on_screen/focus are exempt) |
| -32006 | unsupported: not available on this API level, or no such (custom) action on the node |
| -32007 | timeout: a wait (`wait_for`, gesture completion, screenshot) ran out |
| -32008 | secure window: a FLAG_SECURE window blocks the screenshot |
| -32009 | cancelled: the system refused or cancelled a gesture |

Host mapping (droidctl `error.kind`): -32002/-32003 → `stale-ref`, -32004 → `disabled`,
-32005 → `offscreen`, -32006 → `unsupported`, -32007 → `timeout`, -32008 →
`secure-window`, everything else → `device`.

## Methods

### `ping`
Params: none (ignored). Result:

| field | type | meaning |
|---|---|---|
| `protocol` | int | protocol version; this document is `3` |
| `version` | string | agent `versionName` |
| `versionCode` | int | agent `versionCode`; the host upgrades the APK when it differs from the bundled one |
| `sdk` | int | `Build.VERSION.SDK_INT` |
| `release` | string | `Build.VERSION.RELEASE` |
| `manufacturer`, `model`, `device` | string | `Build.*` |
| `screen` | object | `{w, h, density, rotation}`: the real logical display size in px (it reflects `wm size` / `wm density` overrides), `densityDpi`, and `Surface.ROTATION_*` (0–3) |
| `service` | object | `{connected: true}` |
| `gen` | int | content-generation counter (see `gen`) |
| `peer_uid` | int | the uid this connection was accepted as (2000 or 0) |
| `uptime_ms` | int | device `SystemClock.uptimeMillis()` |

Example (illustrative values):
```
→ {"jsonrpc":"2.0","id":1,"method":"ping"}
← {"jsonrpc":"2.0","id":1,"result":{"protocol":3,"version":"0.3.0","versionCode":3,"sdk":28,"release":"9","manufacturer":"samsung","model":"SM-N950F","device":"greatlte","screen":{"w":1080,"h":2220,"density":420,"rotation":0},"service":{"connected":true},"gen":0,"peer_uid":2000,"uptime_ms":123456789}}
```

### `echo`
Params: any object. Result: the same object, verbatim. It does no device work, so it
measures the transport and framing cost alone (`bench/rtt.py` compares it with `ping`).

```
→ {"jsonrpc":"2.0","id":2,"method":"echo","params":{"x":1}}
← {"jsonrpc":"2.0","id":2,"result":{"x":1}}
```

### `gen`
Params: none. Result: `{"gen": <int>}`. The **content-generation counter**: it goes up
on every `TYPE_WINDOW_CONTENT_CHANGED`, `TYPE_WINDOW_STATE_CHANGED`, `TYPE_WINDOWS_CHANGED`,
`TYPE_VIEW_SCROLLED` and `TYPE_VIEW_TEXT_CHANGED` event the service receives (throttled
by `notificationTimeout`, 50 ms). It never goes down while the service is bound; it
restarts at 0 when the service rebinds. Equal values ⇒ nothing reported a change in
between: a cheap cache-validity and "has it settled?" check.

### `tree`
Params (all optional):

| param | default | meaning |
|---|---|---|
| `not_important` | `false` | include views not important for accessibility (`FLAG_INCLUDE_NOT_IMPORTANT_VIEWS`). Switching it costs one slow dump (the system's node cache is rebuilt: measured 200–450 ms on the SM-N950F vs 40–70 ms steady), so keep it constant |
| `windows` | `true` | dump every window from `getWindows()`; `false` dumps only `rootInActiveWindow` |
| `budget_ms` | `2000` | time budget for the read (clamped to 100–30,000). `dump-fixture` may ask for more; agents should not |
| `max_nodes` | `10000` | node cap (1–10,000) |

Result:

| field | meaning |
|---|---|
| `gen` | the `gen` value read **before** the dump started |
| `dump` | the dump id; it increases by one per successful dump. Handles belong to it |
| `degraded` | `true` if the dump is not a complete read of the current windows (see `reason`) |
| `reason` | only when degraded, comma-separated: `timeout` (budget exceeded), `truncated` (over `max_nodes` or 120 levels), `no-root` (an application window has no root) |
| `unread` | only when degraded: how many nodes carry `truncated` (subtrees not read) |
| `ms` | device time spent on this dump |
| `nodes` | number of nodes (= handles) in the dump |
| `screen` | as in `ping` |
| `windows` | window objects, in `getWindows()` order (top of the z-order first on most devices; use `layer`) |

**Degraded dumps (changed in v3).** A dump is always a read of the **current**
windows; the agent never substitutes an older tree (v2 returned the previous complete
tree when the budget ran out, which could be a screen that was gone: measured on
TESTAPP `huge_tree`, every v2 dump returned the previous scenario's tree).
- The read is **breadth-first across all windows**, so when the budget or node cap
  runs out every window has its top levels and the deep parts are what is missing.
  A node whose children were not read carries `truncated:true`; `unread` counts them.
- Window roots are fetched in parallel and waited for at most ¾ of the budget. An app
  whose accessibility provider blocks its UI thread (TESTAPP `slow_a11y`) would
  otherwise hold the dump for the system's 5 s interaction timeout. Such a window is
  reported with `no_root:true`, and a late root is discarded. A root that came back
  **null** (common right after a transition) is retried once after 50 ms; one still
  pending is not, because asking again would queue another request on the busy app.
- A degraded dump is a real dump: `dump` increases and its handles are valid, so an
  element that was read can still be acted on by handle. What a partial read can't
  do is prove that a match is **unique** (the real one may be in the unread part); the
  host resolver therefore re-matches refs only inside a window that was read in full.

**Window object:** `id`, `type` (`application`, `input_method`, `system`,
`accessibility_overlay`, `split_screen_divider`, `magnification_overlay`, or
`type_<n>` for OEM types: Samsung's Edge panel is `type_-1`), `layer`, `bounds`,
`title` (if any), `pkg` (the root's package), `active` / `focused` (only when true),
`root` (a node; absent if the window had no root), `no_root` (`true` for an
application window whose root could not be read in time: see Degraded dumps).

**Node object.** Fields that are null, empty, `false` or default are **omitted**:

| field | meaning |
|---|---|
| `handle` | int, unique within the dump |
| `class` | `className` |
| `pkg` | `packageName`, only on a root and where it differs from the parent's |
| `id` | `viewIdResourceName` (`pkg:id/name`) |
| `uid` | `getUniqueId()` (API 33+) |
| `text`, `desc`, `hint` (26+), `error`, `state` (30+), `tooltip` (28+), `pane` (28+) | strings |
| `bounds` | `[left, top, right, bottom]` in screen px (logical: they follow `wm size` overrides). **Raw, as Android reports them**: clipped to the window, so a node wholly outside it comes back *inverted* (`left > right` or `top > bottom`) and must be read as empty. Every inverted node in the captured fixtures is also `visible:false` |
| `size` | `[w, h]`: the View's full, unclipped size (`boundsInParent`, which `View` fills from its drawing rect), present only when it is larger than `bounds`, i.e. the node is clipped. Lets the host apply "under 10% visible" to a button scrolled 5% into view (a 13 px strip of a 262 px button on TESTAPP `partial`) |
| `truncated` | `true` when the node has children that were not read (degraded dumps only) |
| `visible` | present only as `false` (`isVisibleToUser`) |
| `drawingOrder` | int (24+), omitted when 0 |
| `flags` | the true ones among `clickable`, `longClickable`, `checkable`, `checked`, `focusable`, `focused`, `selected`, `enabled`, `editable`, `password`, `scrollable`, `heading` (28+), `showingHint` (26+) |
| `actions` | standard actions by name (`click`, `long_click`, `scroll_forward`, `scroll_backward`, `set_text`, `expand`, `dismiss`, `show_on_screen`, `scroll_down`, `set_progress`, `ime_enter`, …), custom actions as `{"id": <int>, "label": "<text>"}`. Never listed (present on almost every node): accessibility focus, movement-granularity and HTML-element navigation |
| `collection` | `{rows, cols}` (`collectionInfo`) |
| `item` | `{row, col}` (`collectionItemInfo`) |
| `range` | `{min, max, cur}` (`rangeInfo`, floats) |
| `inputType` | int, omitted when 0 |
| `children` | nodes, in child order |

Example (trimmed):
```
→ {"jsonrpc":"2.0","id":3,"method":"tree"}
← {"jsonrpc":"2.0","id":3,"result":{"gen":12,"dump":4,"degraded":false,"ms":58,"nodes":98,
   "screen":{"w":1080,"h":2220,"density":420,"rotation":0},
   "windows":[{"id":2,"type":"application","layer":1,"bounds":[0,0,1080,2220],
     "title":"Settings","pkg":"com.android.settings","active":true,"focused":true,
     "root":{"handle":1,"class":"android.widget.FrameLayout","pkg":"com.android.settings",
       "bounds":[0,0,1080,2220],"flags":["enabled"],"children":[…]}}]}}
```

**Handles.** Handles map to live `AccessibilityNodeInfo`s held by the agent for the
**latest** dump only; the previous dump's nodes are released when a new dump (complete
or degraded) is made. Methods that act on a node (from M5) take `{dump, handle}` and fail with
`-32002` if `dump` is not the latest.

Measured (SM-N950F, API 28): 40–70 ms device time for 50–240 nodes steady state; the
first dump after the service binds or after an app transition is slower (100–900 ms);
a Settings app list that was still loading exceeded the 2 s budget once. TESTAPP
`huge_tree` (5,000 nodes, 100 levels) is app-bound: ~150 nodes/s on a cold read (each
fetch walks 100 levels on the app's UI thread; breadth- vs depth-first made no
difference), ~700–1,400 nodes in the 2 s budget as the system cache warms. TESTAPP
`slow_a11y`: 1.5–1.9 s (v2: 5 s, the provider's full sleep).


### Settle (shared by `act`, `gesture`, `global`)
`settle: {quiet_ms=150, first_ms=600, timeout_ms=2000, tree=true, not_important=false}`.
Accessibility events reach the agent with a lag, so a quiet window counted from the
action would end before anything is reported. The agent therefore waits up to
`first_ms` for the **first event** after the action (none → nothing happened, idle),
then until no event has arrived for `quiet_ms`; `timeout_ms` caps the whole wait.
Result fields: `idle` (false = the cap was hit while the screen kept changing),
`settle_ms` (until the screen went quiet; excludes the final dump), `t0` (uptime ms
when the action started; compare with event `t`), `redumps` (when the check below
fired), and, unless `tree:false`, `tree` (the same object `tree` returns; it is the new
latest dump, so the previous dump's handles become stale).

**The settled tree is checked too (v3).** Quiet alone is not proof: mid-transition
Android can stay quiet for longer than `quiet_ms` while an application window has no
root yet or a root with no children (not drawn yet). After `back` on TESTAPP
`back_confirm`, `WINDOWS_CHANGED` arrives at ~290 ms and the dialog's `WINDOW_STATE`
only ~190 ms later, so a 150 ms quiet window could return the frame without the
dialog. While the settled tree shows such a window, the agent waits for the next event
(up to `first_ms`) and quiet again, then re-dumps, all within `timeout_ms`. Measured:
with `quiet_ms=60`, 15/15 `back`s first hit the transitional frame and all 15 were
recovered (settle median 588 ms, max 1,157); at the default 150 ms, 20/20 settled
trees contained the dialog. A screen that is already complete pays nothing (tap +
settle on a static target: 308.7 ms median, 301.5 before). The trade-off: a screen
that is *really* an empty application window (a root with no children) is only
reported after `first_ms` without further events, or at `timeout_ms`.

Measured on the SM-N950F (API 28, `bench/results/m5-device.json`): the first event
after a click arrives 75–100 ms later for an in-place change and ~300 ms later for an
activity launch; a toast-showing button ~200 ms.

### `act`
`{dump, handle, action | custom, args?, settle?, force?, event_ms=600}` →
`{performed, action, perform_ms, t0, clicked_event?, available?, idle?, settle_ms?, tree?, events}`

- `action`: a standard name as reported in `tree` (`click`, `long_click`, `focus`,
  `clear_focus`, `select`, `clear_selection`, `scroll_forward`, `scroll_backward`,
  `scroll_up/down/left/right`, `scroll_to_position` (`args {row, col}`), `set_text`
  (`args {text}`), `set_selection` (`args {start, end}`), `set_progress`
  (`args {value}`), `expand`, `collapse`, `dismiss`, `copy`, `paste`, `cut`,
  `show_on_screen`, `context_click`, `ime_enter` (API 30+; else -32006), …).
- `custom`: a custom action's label (case-insensitive) or id.
- The node is `refresh()`ed first (-32003 if gone), then checked enabled (-32004) and
  visible (-32005) unless `force`.
- `performed:false` is a result, not an error, and comes with `available` (the node's
  action names/labels). No settle happens then.
- `clicked_event` (click/long_click only): whether a `TYPE_VIEW_CLICKED`
  (`TYPE_VIEW_LONG_CLICKED`) from **this node** arrived. Without `settle` the agent waits
  up to `event_ms` for it, returning as soon as it arrives. The host's tap fallback
  depends on this, so it is matched by node identity, falling back to window id + bounds.
- `events`: the events seen from the action until the reply (≤ 50).

### `gesture`
`{type, points:[[x,y],…], ms?, settle?}` → `{performed, type, ms, idle?, settle_ms?, tree?, events}`

Device pixels (the same space as `bounds`, verified under a `wm size` override).
`tap` (1 point, 60 ms), `long` (1 point, 800 ms), `double` (1 point, 2×50 ms, 100 ms
apart), `swipe` (2 points, 300 ms), `path` (≥2 points, 500 ms), `pinch` (4 points: two
simultaneous strokes p0→p1 and p2→p3, 400 ms). Blocks until the system's completion
callback; cancelled → -32009.

### `global`
`{name, settle?}` → `{performed, name, idle?, settle_ms?, tree?, events}`. Names:
`back`, `home`, `recents`, `notifications`, `quick_settings`, `power_dialog`, `split`,
`lock` (28+), `screenshot` (28+).

### Events
Every event: `{seq, t (uptime ms), gen, type, pkg?, …}`. Types:

| type | extra fields |
|---|---|
| `clicked`, `long_clicked` | `class`, `text`, `desc`, `window`, `bounds` |
| `focused`, `selected` | `class`, `text`, `desc` |
| `text_changed` | `class`, `text` (omitted for passwords), `added`, `removed` |
| `window_state` | `class` (activity or dialog class), `title`, `changes` |
| `window_content`, `windows_changed`, `scrolled` | compacted: bursts from one package within 250 ms become one event with `n` and `t_last` |
| `toast` | `text` |
| `notification` | `text` |
| `announcement` | `text` |
| `ime` | `shown` (the input-method window appeared/disappeared) |

The ring keeps the last 500. Compaction mutates the newest entry in place, so a
client that already read it will not see later `n` increments.

### `events`
`{since=0, limit?}` → `{events:[…], next}`: events with `seq > since`, oldest first;
`next` is the latest seq (pass it as `since` next time).

### `subscribe` / `unsubscribe`
`{events?:[types]}` (absent or empty = all) → `{subscribed, next}`. Matching events are
pushed as notifications on this connection until `unsubscribe` or disconnect. One
subscription per connection (a new `subscribe` replaces it). Use a dedicated
connection for subscriptions so pushed events never delay replies.

### `wait_idle`
`{quiet_ms=150, timeout_ms=2000}` → `{idle, ms, events}`: returns once no screen-change
event (`window_content`, `window_state`, `windows_changed`, `scrolled`,
`text_changed`) has arrived for `quiet_ms`, counting from the last one even if it was
before the call. ~3 ms when the screen is already idle.

### `wait_for`
`{text? , id?, desc?, exact=false, gone=false, pkg?, activity?, toast?, window?, since?, timeout_ms=5000}`
→ `{matched:true, ms, node?, activity?, toast?, window?}`, or -32007.

All given conditions must hold at once. Node conditions: a visible node whose text/desc
contains the value (case-insensitive; `exact` for equality) and whose resource id equals
`id` or ends with `:id/<id>`; with `gone` the condition is that no such node exists.
`activity`: the front activity class equals or ends with the value. `toast`: a toast
event (text containing the value; `""` = any) with `seq > since` (default: from the
call). `window`: a window whose title contains the value or whose package equals it.
The current state is checked first; then the agent re-checks when events arrive (the
tree at most every 100 ms), and at least every second.

### `current`
`{}` → `{pkg?, activity?, keyboard, windows:[{id, type, layer, title?, pkg?, active?, focused?}], gen}`.
`activity` comes from window-state events, remembered per window id, so it stays right
after `back` (Samsung API 28 sends no window-state event when an existing activity
returns to the front). It is absent when unknown (e.g. before any event since the
service started); the host can fall back to `dumpsys activity`.

### `screenshot`
`{scale=0.5, quality=70, crop?:[l,t,r,b]}` → `{format:"jpeg", w, h, scale, data (base64)}`.
API 30+ only (`takeScreenshot`); below that -32006 and the host uses
`adb exec-out screencap -p`. A FLAG_SECURE window → -32008. Retries twice on the
system's 1-per-333 ms rate limit.

### `clipboard`
`{set?: text}` → `{previous?, set}`. `previous` is the current primary clip as text when
readable (API 29+ may deny background reads). Used by the host's paste fallback for
typing; the host restores `previous` afterwards.

## Versioning
- Additive changes (new methods, new result fields) keep `protocol` unchanged; clients
  must ignore unknown fields.
- Incompatible changes bump `protocol`. The host checks `protocol` and `versionCode`
  from `ping` before anything else.
- History: **1** (M1) `ping`, `echo`; `gen`, `tree` (M2). **2** (M5) `act`, `gesture`,
  `global`, events and `subscribe` notifications, `wait_idle`, `wait_for`, `current`,
  `screenshot`, `clipboard`. Bumped because the host now relies on these methods and
  on the connection carrying notifications.
  **3** `degraded` means a partial read of the *current* windows, never an older tree
  (v2 could return the previous screen); breadth-first reads with `truncated` nodes and
  `unread`; `no_root` windows and parallel, bounded root fetches; `tree {budget_ms,
  max_nodes}`; node `size` (unclipped) when clipped; settle re-checks transitional trees
  (`redumps`). Bumped because a host that trusted a v2 degraded tree's handles would
  act on the wrong screen.
