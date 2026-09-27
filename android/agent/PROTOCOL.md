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
| `gen` | int | content-generation counter. **Placeholder: always `0` in v1** until `tree` lands |
| `peer_uid` | int | the uid this connection was accepted as (2000 or 0) |
| `uptime_ms` | int | device `SystemClock.uptimeMillis()` |

Example (illustrative values):
```
→ {"jsonrpc":"2.0","id":1,"method":"ping"}
← {"jsonrpc":"2.0","id":1,"result":{"protocol":1,"version":"0.1.0","versionCode":1,"sdk":28,"release":"9","manufacturer":"samsung","model":"SM-N950F","device":"greatlte","screen":{"w":1080,"h":2220,"density":420,"rotation":0},"service":{"connected":true},"gen":0,"peer_uid":2000,"uptime_ms":123456789}}
```

## Versioning
- Additive changes (new methods, new result fields) keep `protocol` unchanged; clients
  must ignore unknown fields.
- Incompatible changes bump `protocol`. The host checks `protocol` and `versionCode`
  from `ping` before anything else.
