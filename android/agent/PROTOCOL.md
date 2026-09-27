# droidctl agent protocol (v1)

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
- Server-pushed notifications (events) are reserved for a later version (`subscribe`).

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

## Methods

### `ping`
Params: none (ignored). Result:

| field | type | meaning |
|---|---|---|
| `protocol` | int | protocol version; this document is `1` |
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
← {"jsonrpc":"2.0","id":1,"result":{"protocol":1,"version":"0.1.0","versionCode":1,"sdk":28,"release":"9","manufacturer":"samsung","model":"SM-N950F","device":"greatlte","screen":{"w":1080,"h":2220,"density":420,"rotation":0},"service":{"connected":true},"gen":0,"peer_uid":2000,"uptime_ms":123456789}}
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

Result:

| field | meaning |
|---|---|
| `gen` | the `gen` value read **before** the dump started |
| `dump` | the dump id; it increases by one per successful dump. Handles belong to it |
| `degraded` | `true` if the dump is not a complete, fresh read (see `reason`) |
| `reason` | only when degraded: `timeout` (2 s budget exceeded), `truncated` (over 10,000 nodes or 120 levels), `no-root` (no window root, even after one retry 50 ms later) |
| `ms` | device time spent on this dump |
| `nodes` | number of nodes (= handles) in the dump |
| `screen` | as in `ping` |
| `windows` | window objects, in `getWindows()` order (top of the z-order first on most devices; use `layer`) |

**Degraded dumps.** If the 2 s budget runs out (or no root is found) and a previous
complete dump exists, the agent returns **that previous tree** (with its own `dump` id
and handles, which stay valid) plus `degraded:true`, `reason` and the new `ms`. A
degraded result can therefore describe *the previous screen*: never treat it as the
current one without checking `gen`/`reason`. Without a previous tree, the partial dump
is returned with `degraded:true`.

**Window object:** `id`, `type` (`application`, `input_method`, `system`,
`accessibility_overlay`, `split_screen_divider`, `magnification_overlay`, or
`type_<n>` for OEM types: Samsung's Edge panel is `type_-1`), `layer`, `bounds`,
`title` (if any), `pkg` (the root's package), `active` / `focused` (only when true),
`root` (a node; absent if the window had no root).

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
**latest** dump only; the previous dump's nodes are released when a new complete dump
succeeds. Methods that act on a node (from M5) take `{dump, handle}` and fail with
`-32002` if `dump` is not the latest.

Measured (SM-N950F, API 28): 40–70 ms device time for 50–240 nodes steady state; the
first dump after the service binds or after an app transition is slower (100–900 ms);
a Settings app list that was still loading exceeded the 2 s budget once.

## Versioning
- Additive changes (new methods, new result fields) keep `protocol` unchanged; clients
  must ignore unknown fields.
- Incompatible changes bump `protocol`. The host checks `protocol` and `versionCode`
  from `ping` before anything else.
