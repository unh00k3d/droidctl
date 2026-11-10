# Anisette v3 client: the wire protocol, for a pure-stdlib Python client

What droidctl needs to get anisette headers on Linux from a user-run `anisette-v3-server`. On macOS we already get them natively (`droidctl/ios/apple/anisette.py`).

_2026-11-10. Only source code was read. Nothing ran against Apple, against any anisette server, or with any Apple ID. Claims are cited; anything marked *inferred* is a reading of the code that has not been tested._

**Sources** (shallow clones in the session scratchpad; commit and date of HEAD):

| prefix | repo | HEAD | license | use |
|---|---|---|---|---|
| `XT:` | xtool-org/xtool | `a2875dd` 2026-11-07 | **MIT** (`LICENSE.md`) | the reference client. You may port it with attribution. |
| `AV:` | Dadoum/anisette-v3-server | `6848957` 2026-09-23 | **no license file** | Read only to learn the protocol. Never copy its code. |
| `VD:` | vibe-d/vibe.d `v0.9.8` (the server's HTTP/WebSocket stack, `AV:dub.selections.json`) | tag `v0.9.8` | MIT (`LICENSE.txt`) | handshake and framing rules |
| `SS:` | SideStore/SideStore | `4f3d18d` 2026-11-06 | **AGPL-3.0** | Ideas only. |
| `SG:` | SideStore/SideSign (SideStore's v3 client) | `752d1ef` 2026-10-13 | **GPL-3.0** (`README.md:156-158`) | Ideas only. |
| `AK:` | mahee96/AnisetteKit (SideSign's provisioning flow) | `db8b410` 2026-09-18 | **AGPL-3.0** (`LICENSE`) | Ideas only. |

There is no repo named `SideStore/omnisette`. xtool points at `SideStore/omnisette-server` as the v3 spec (`XT:Sources/XKit/GrandSlam/Anisette/OmnisetteADIProvider.swift:7`), but that repo was not read. Everything below comes from `AV:source/app.d`, the only file of the server, which is what users would run.

**Bottom line:** port xtool. `OmnisetteADIProvider.swift` (187 lines) and `ADIDataProvider.swift` (312 lines) cover the whole protocol, and both are MIT. The SideStore stack is (A)GPL, so use it only to cross-check.

---

## 1. Endpoints of anisette-v3-server

All routes are registered on one `AnisetteService` class (`AV:source/app.d:168-170,188-443`). The default bind is `0.0.0.0:6969` (`AV:app.d:56-57`; Docker `EXPOSE 6969`, `AV:Dockerfile:36`). Every HTTP response carries `Implementation-Version: anisette-v3-server v<provision version>` (`AV:app.d:38,223,238,283,291`). No other routes exist.

| method + path | request | success response | error response |
|---|---|---|---|
| `GET /` (**v1**) | none | 200 `application/json`. See §5. | (none written; an exception goes to vibe.d's default error page, *inferred*) |
| `GET /v3/client_info` | none | 200 `application/json`: `{"client_info": "<MacBookPro13,2> <macOS;13.1;22C65> <com.apple.AuthKit/1 (com.apple.akd/1.0)>", "user_agent": "akd/1.0 CFNetwork/808.1.4"}` (`AV:app.d:39,228-240`) | none |
| `POST /v3/get_headers` | JSON `{"identifier": b64(16 bytes), "adi_pb": b64(adi.pb)}` (`AV:app.d:251-253`; xtool sends `Content-Type: application/json`, `XT:OmnisetteADIProvider.swift:79-85`) | 200: `{"result":"Headers","X-Apple-I-MD": b64,"X-Apple-I-MD-M": b64,"X-Apple-I-MD-RINFO":"17106176"}` (`AV:app.d:277-284`) | **Still HTTP 200**, body `{"result":"GetHeadersError","message":"<D exception type>: <msg>"}` (`AV:app.d:286-293`; no status code is set, so vibe.d sends 200, *inferred*). **Check `result`, not the status code.** |
| `GET /v3/provisioning_session` (WebSocket) | an RFC 6455 upgrade | the message exchange in §2 | messages with `result` set to `Timeout`, `InvalidIdentifier`, `StartProvisioningError` or `EndProvisioningError` (§2) |

Notes on `get_headers`:
- It does not record the result. The server writes `adi_pb` into a temporary directory `<runtime>/anisette-v3/provisioning/<uuid>`, asks the library for an OTP, then deletes the directory (`AV:app.d:254-275,294-298`). **The server keeps no per-client state between calls, so the client owns its identity.**
- The server derives the ADI identifier from the client's 16 bytes: `UUID(bytes[0..16]).toString()`, uppercased, first 16 characters (`AV:app.d:254,272`). This is the same rule as in provisioning (`AV:app.d:331,358`).
- **Persist and reuse one identifier.** The provisioning and every later `get_headers` call must send the same identifier: the server feeds it to the ADI as its identity in both places (`AV:app.d:272,358`). That a different identifier would make `requestOTP` fail is *inferred*.
- `X-Apple-I-MD-RINFO` is always the constant `"17106176"` (`AV:app.d:281`). xtool overwrites its stored routing info with this value whenever it parses as a number (`XT:OmnisetteADIProvider.swift:88-90`).
- All binary fields use standard base64 with padding. xtool's encoder uses `.base64` (`XT:OmnisetteADIProvider.swift:16-28`), and the server decodes with D's `std.base64.Base64`. That unpadded input would be rejected is *inferred*: send padded base64.
- JSON keys are snake_case: `client_info`, `user_agent`, `adi_pb` (xtool uses `convertToSnakeCase`/`convertFromSnakeCase`, `XT:OmnisetteADIProvider.swift:18,25`).

## 2. The provisioning sequence

This runs once per identity, or again after the user resets it. The client is the only party that talks to Apple. The server only runs the ADI library.

```
client                                   anisette server (ws)              gsa.apple.com
  |-- GET /grandslam/GsService2/lookup -------------------------------------->|  (A0) gets urls
  |-- POST urls.midStartProvisioning  {Header:{},Request:{}} ---------------->|  (A1)
  |<------------------------------------------------- Response.spim (b64) ----|
  |== WS connect /v3/provisioning_session ==>|
  |<== {"result":"GiveIdentifier"}           |
  |==> {"identifier": b64(16 bytes)}         |
  |<== {"result":"GiveStartProvisioningData"}|
  |==> {"spim": b64}                         |
  |<== {"result":"GiveEndProvisioningData","cpim": b64}
  |-- POST urls.midFinishProvisioning {Header:{},Request:{cpim:b64}} -------->|  (A2) must finish within the server's 3 s
  |<-------------------------- Response.ptm, Response.tk, Response.X-Apple-I-MD-RINFO
  |==> {"ptm": b64, "tk": b64}               |
  |<== {"result":"ProvisioningSuccess","adi_pb": b64}   then the server closes
```

### Server side, step by step (`AV:app.d:301-442`)

| # | server sends | client must reply | server failure → `result` |
|---|---|---|---|
| 1 | `{"result":"GiveIdentifier"}` (`:310-313`) | `{"identifier": "<b64 of 16 raw bytes>"}` (`:327-331`; xtool: the raw `uuid_t` bytes of a UUID, `XT:OmnisetteADIProvider.swift:115-119,183-186`) | no reply within the timeout → `Timeout` (`:316-322`). Unparseable JSON or bad base64 → `InvalidIdentifier` (`:332-339`). It is *inferred* that fewer than 16 decoded bytes raises a D `RangeError`, which this `catch (Exception)` does not catch. |
| 2 | `{"result":"GiveStartProvisioningData"}` (`:360-365`) | `{"spim": "<b64 spim from Apple>"}` (`:378-382`) | `Timeout` (`:367-373`); `{"result":"StartProvisioningError","message":"… (request id: <uuid>)"}` (`:393-400`) |
| 3 | `{"result":"GiveEndProvisioningData","cpim":"<b64>"}` (`:386-392`) | `{"ptm":"<b64>","tk":"<b64>"}` (`:414-419`) | `Timeout` (`:404-410`) |
| 4 | `{"result":"ProvisioningSuccess","adi_pb":"<b64 adi.pb>"}` (`:424-441`) | nothing; the socket closes (`:305`) | `{"result":"EndProvisioningError","message":"…"}` (`:430-437`) |

After every error the server sends that one message and returns, which closes the socket (`:305`).

**Timeout gotcha:** each `waitForData` allows `--timeout` ms, default **3000** (`AV:app.d:64,73,79`). The Docker image passes no flags (`AV:Dockerfile:37`). Between steps 3 and 4 the client makes the Apple call (A2) itself, so **the `midFinishProvisioning` round-trip must finish in under 3 s** or the server sends `Timeout`. To fit:
- Do the lookup (A0) and `midStartProvisioning` (A1) **before** opening the WebSocket, as xtool does (`XT:ADIDataProvider.swift:207-214`).
- Keep the HTTPS connection to gsa.apple.com warm, or at least don't add retries inside that window (*inferred* advice).

xtool's client side (`XT:OmnisetteADIProvider.swift:110-154,160-180`):
- It reads each message, decodes `result` and **fails if `result` is not the one expected** (`:156-175`). It accepts text or binary frames (`:162-166`).
- It sends every message as a **text** frame (`:177-180`).

### Apple call A0: lookup (`XT:Sources/XKit/GrandSlam/Lookup/GrandSlamLookupManager.swift:15,29-48`)
- `GET https://gsa.apple.com/grandslam/GsService2/lookup`, no body. AnisetteKit uses the same URL (`AK:Sources/Constants.swift:23`).
- Headers:
  - `User-Agent`: the `user_agent` from `/v3/client_info`
  - `X-Apple-I-Locale`: e.g. `en_US`
  - `X-Apple-I-TimeZone`: the time zone identifier, e.g. `Europe/Istanbul`
  - `X-Apple-I-TimeZone-Offset`: seconds from GMT
  - `X-MMe-Country`: the region code
  - `X-MMe-Client-Info`: `client_info` from `/v3/client_info`
  - `X-Mme-Device-Id`: §3
- Response: an XML plist whose root dict has `urls` → {`gsService`, `secondaryAuth`, `trustedDeviceSecondaryAuth`, `validateCode`, `midStartProvisioning`, `midFinishProvisioning`, …} (`XT:Lookup/GrandSlamEndpoints.swift:11-18`; AnisetteKit reads `plist["urls"]["midStartProvisioning"/"midFinishProvisioning"]`, `AK:Sources/AnisetteClient.swift:243-250`). xtool caches the result for the life of the process (`XT:GrandSlamLookupManager.swift:50-55`).

### Apple calls A1 and A2 (`XT:Sources/XKit/GrandSlam/Anisette/ADIDataProvider.swift:117-215`)
Both are `POST <url from lookup>`, with an **XML plist** body and these headers (`:160-179`):

| header | value |
|---|---|
| `Content-Type` | `text/x-xml-plist` |
| `Accept-Language` | `en_US` |
| `X-Apple-I-MD-LU` | the local user ID (§3) |
| `X-MMe-Country` | the region code, if known |
| `X-MMe-Client-Info` | `client_info` from `/v3/client_info` (via `DeviceInfo.dictionary`, `XT:Anisette/DeviceInfo.swift:10,15-19`) |
| `X-Mme-Device-Id` | the device ID (§3) |

AnisetteKit sends more headers on these calls: `User-Agent`, `X-Apple-I-MD-RINFO` `17106176`, `X-Apple-I-SRL-NO` `0`, `X-Apple-Locale`, `X-Apple-I-TimeZone`, `X-Apple-I-Client-Time`, `Accept: */*` (`AK:Sources/AnisetteClient.swift:322-349`, `AK:Sources/AnisetteRequestHeaders.swift:34-46`). xtool shows the smaller set above is enough, at least in xtool's own use.

| call | body (plist) | response `Response` dict |
|---|---|---|
| A1 `midStartProvisioning` | `{"Header": {}, "Request": {}}` (`XT:ADIDataProvider.swift:117-127,208-211`; `AK:AnisetteClient.swift:354`) | `spim` (base64 string) (`XT:ADIDataProvider.swift:128-130,212`) |
| A2 `midFinishProvisioning` | `{"Header": {}, "Request": {"cpim": "<b64 cpim>"}}` (`XT:ADIDataProvider.swift:132-134,190-193`; `AK:AnisetteClient.swift:373`) | `ptm` (b64), `tk` (b64), `X-Apple-I-MD-RINFO` (decimal u64 as a string) (`XT:ADIDataProvider.swift:135-145,195-198`) |

- **Error shape:** the plist is `{"Response": {"Status": {"ec": int, "em": str}, …}}`, and `ec != 0` means an error (`XT:Requests/GrandSlamOperationRequest.swift:20-55`).
- **Decoding:** A1's `spim` is decoded from base64 and sent on to the WebSocket, base64-encoded again. A2's `ptm`/`tk` are decoded and sent back to the server the same way (`XT:ADIDataProvider.swift:195-201`, `XT:OmnisetteADIProvider.swift:138-146`).
- **Connections:** xtool uses an ephemeral (fresh) HTTP connection per GSA request (`XT:ADIDataProvider.swift:181`). For the auth calls it says GSA dislikes reused keep-alive connections (`XT:GrandSlamClient.swift:43-45`).

## 3. What to persist, and how the IDs are derived

xtool (`XT:ADIDataProvider.swift:84-115,238-280`):

| item | how it is made | persisted? |
|---|---|---|
| **local user UID** | a random UUID (v4), stored as its uppercase string (`:93-100,112`) | yes (`XTLLocalUserUID`) |
| **identifier sent to the server** | the UID's **16 raw bytes**, base64-encoded (`XT:OmnisetteADIProvider.swift:82-84,119,183-186`) | derived from the UID |
| **`X-Mme-Device-Id`** | the UID string, uppercase, e.g. `1B4E28BA-2FA1-11D2-883F-0016D3CCA427` (`:285`; Foundation's `uuidString` is uppercase) | derived from the UID |
| **`X-Apple-I-MD-LU`** | `SHA256(utf8(uid_string_uppercase))` as **uppercase hex**, 64 characters (`:101-104`) | derived from the UID |
| **adi.pb** | the bytes from `ProvisioningSuccess.adi_pb` | yes (`XTLProvisioningInfo`, `:114,273`) |
| **routing info** | `X-Apple-I-MD-RINFO` from A2, then overwritten by `get_headers` (constant `17106176`) | yes (`XTLRoutingInfo`, `:115,274`) |
| **client_info / user_agent** | `GET /v3/client_info` | cached in memory only (`:88,153-158`); constant for this server (`AV:app.d:39,233-236`) |
| provider ID | `"mac-omnisette"`. If it changes, xtool drops the provisioning data (`:106-109,243-245`) and logs out (`XT:AnisetteDataProvider.swift:13-19`) | yes |

On each run: if adi.pb and the routing info are stored, call `get_headers` and nothing else. Otherwise provision as in §2, store, then call `get_headers` (`XT:ADIDataProvider.swift:261-280`). A reset clears adi.pb and the routing info but keeps the UID (`:238-241`).

The UID plus adi.pb **are the machine identity** that Apple's "trusted machine" logic remembers. If they are lost, a new provisioning is needed, and probably a new 2FA (`research/ios-signing.md` line 43, *inferred*). Store them next to the GSA token (for example `~/.droidctl/ios/anisette.json`, mode 0600; an *inferred* recommendation).

Divergences in the other clients (ideas only):
- SideSign sends `identifier` as **32 lowercase hex characters** instead of base64 bytes (`SG:Sources/Anisette/RemoteAnisetteDataProvider.swift:59,72,127,138`). The server base64-decodes that to 24 bytes and uses the first 16 (`AV:app.d:328-331`), so it works, but it is a different identity. Use xtool's form.
- AnisetteKit uses the UUID uppercased as `X-Mme-Device-Id` (`AK:AnisetteClient.swift:161,338`), as xtool does. Its **default `X-Apple-I-MD-LU`** is the constant `000…0001` (64 hex characters) (`AK:Sources/Constants.swift:15`). xtool's SHA-256 derivation is the better-founded choice.

## 4. The final header set for GSA

xtool, `AnisetteData.dictionary` (`XT:Sources/XKit/GrandSlam/Anisette/AnisetteData.swift:54-72`) merged with `DeviceInfo.dictionary` (`XT:Anisette/DeviceInfo.swift:15-20`):

| header | value | source |
|---|---|---|
| `X-Apple-I-MD` | base64 OTP | `get_headers` → `X-Apple-I-MD` (`XT:ADIDataProvider.swift:223-233`) |
| `X-Apple-I-MD-M` | base64 machine ID | `get_headers` → `X-Apple-I-MD-M` |
| `X-Apple-I-MD-RINFO` | `17106176` (decimal string) | `get_headers` (`AV:app.d:281`), with the stored A2 value as fallback |
| `X-Apple-I-MD-LU` | SHA-256 uppercase hex of the UID string | §3 |
| `X-Mme-Device-Id` | the UID, uppercase | §3 |
| `X-MMe-Client-Info` | `client_info` from `/v3/client_info` | `XT:DeviceInfo.swift:10` |
| `X-Apple-I-Client-Time` | UTC, ISO 8601 without fractions, e.g. `2024-01-02T03:04:05Z` | the request time (`XT:AnisetteData.swift:13-20,65`; test at `XT:Tests/XToolTests/AnisetteDataTests.swift:6-26`) |
| `X-Apple-I-TimeZone` | time zone **abbreviation**, e.g. `UTC`, `CET` (falls back to `UTC`) | `XT:AnisetteData.swift:64` |
| `X-Apple-Locale` | `ll_RR` with no `@rg=` suffix; `en_US` if unknown | `XT:AnisetteData.swift:55-63` |
| `X-Apple-I-SRL-NO` | **not sent by xtool.** AnisetteKit sends `"0"` (`AK:Sources/Constants.swift:16,38`), and so do the server's own v1 output (`AV:app.d:216`) and droidctl's macOS provider (`droidctl/ios/apple/anisette.py:83`, using `"0"` as fallback) | optional; send `"0"` to match v1 and macOS (*inferred* harmless) |

How xtool uses the set:
- **Login operations (SRP init/complete):** the set goes **in the body** as `cpd`, merged with `bootstrap`, `icscrec`, `pbe`, `prkgen`, `svct`, `loc` (`XT:Requests/GrandSlamOperationRequest.swift:77-91`). The HTTP headers carry only `Content-Type: text/x-xml-plist`, `X-MMe-Client-Info`, `Accept: */*` and `User-Agent: <user_agent>` (`XT:GrandSlamClient.swift:27-41`, `XT:GrandSlamOperationRequest.swift:72-75`).
- **2FA requests:** the same set goes in as **HTTP headers**, plus `X-Apple-App-Info`/`X-Apple-Identity-Token` (`XT:Requests/GrandSlamTwoFactorRequest.swift:20-27`).
- **Freshness:** the OTP is short-lived. droidctl's macOS provider refreshes it after 60 s (`droidctl/ios/apple/anisette.py:20`). One `get_headers` per GSA request, or per 60 s, fits that (*inferred*).

droidctl's existing `headers()`/`client_info_for_authkit()` (`droidctl/ios/apple/anisette.py:96-125`) take a provider whose `base()` returns exactly this dict. A Linux provider only needs a `base()` that returns these keys.

## 5. Does `GET /` (v1) exist? Yes.

`AV:app.d:189-226` answers `GET /` with `application/json`:

```json
{"X-Apple-I-Client-Time": "2026-11-10T12:00:00Z", "X-Apple-I-MD": "<b64>", "X-Apple-I-MD-M": "<b64>",
 "X-Apple-I-MD-RINFO": "17106176", "X-Apple-I-MD-LU": "<16 uppercase hex chars>", "X-Apple-I-SRL-NO": "0",
 "X-MMe-Client-Info": "<MacBookPro13,2> <macOS;13.1;22C65> <com.apple.AuthKit/1 (com.apple.akd/1.0)>",
 "X-Apple-I-TimeZone": "<server tz dstName>", "X-Apple-Locale": "en_US", "X-Mme-Device-Id": "<UPPERCASE UUID>"}
```

The README also says the server supports both "anisette-v1 and anisette-v3" (`AV:README.md:5-6`).

How v1 works:
- **The identity is one server-global machine.** `v1Device`/`v1Adi` are created once (random UUID device ID, an 8-byte hex `localUserUUID`, a 2-byte hex ADI identifier) and provisioned **by the server itself** with Apple, at startup or on the first request (`AV:app.d:42-43,134-160,199-203`).
- **Everyone who uses that server shares the identity.** SideSign calls this "outdated V1 mode (shared device identity)" and warns before using it (`SG:Sources/Anisette/AnisetteDataManager.swift:188,223-227`; `SS:SideStore/Core/Anisette/AnisetteProvider.swift:45-57`).

What that means for droidctl:
- **v1 is enough for a minimal client on a single-user, self-hosted server.** It is one plain `urllib` GET, with no WebSocket, no Apple calls and no local state. The headers come back ready to use.
- **Caveat, *inferred*:** the README's Docker volume mounts only `…/.config/anisette-v3/lib/` (`AV:README.md:11`). The v1 `device.json` and adi.pb live one level up, in `~/.config/anisette-v3` (`AV:app.d:59,95,134-136`). So **recreating the container probably mints a new v1 machine identity**, which can trigger a fresh 2FA.
- With v3 the identity lives on the client, so it survives server rebuilds and server switches.
- Recommendation (*inferred*): implement v3 as the main path. v1 could be a fallback, but don't build it unless needed.
- SideSign's order is: v3 `get_headers` when it has adi.pb, else v1 `GET /` (`SG:RemoteAnisetteDataProvider.swift:62-105`). Its server probe is `GET /v3/client_info`, then `GET /` (`SG:AnisetteDataManager.swift:314-349`).

## 6. Minimal WebSocket client (stdlib `socket` + `ssl` + `base64` + `hashlib` + `os`)

The server's handshake check is vibe.d's `handleWebSocket` (`VD:http/vibe/http/websockets.d:183-232`; the `WebSocket` parameter makes the route a WS endpoint, `VD:web/vibe/web/web.d:132-135,1035-1039`).

- **Required request headers:**
  - `Connection` containing `upgrade` (case-insensitive, comma list)
  - `Upgrade: websocket`
  - `Sec-WebSocket-Version: 13`
  - `Sec-WebSocket-Key` present
  - plus `Host` for HTTP/1.1 (*inferred*)
  - Otherwise the server answers **400** with a plain-text reason (`:193-214`).
- **No subprotocol:** the server never reads `Sec-WebSocket-Protocol` (that line is commented out, `:190`), and nothing else is negotiated. No auth, cookies or origin check are involved (`AV:app.d:301-303`).
- **Response:** `101` with `Sec-WebSocket-Accept = base64(sha1(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"))` (`:216-219`). Verify it, or skip the check (*inferred* acceptable for a localhost server).
- **Frames the server sends:** **text** frames (`socket.send(string)` → `FrameOpcode.text`, `VD:websockets.d:660-668`), **unmasked** (the server-side socket has no RNG, so it writes no mask: `:577-580,953-969`). Every message is small and fits one frame (*inferred*, from the payload sizes). The 16-bit and 64-bit extended length forms must still be parsed, because adi.pb can exceed 125 bytes.
- **Frames the server accepts:** it reads masked or unmasked frames (`:1157-1199`). Send client frames **masked**, as RFC 6455 requires.
  - The server calls `receiveText()` with `strict = true`, which **rejects binary frames** with a `WebSocketException` (`:774-779`). So send **text frames** (opcode 0x1, FIN set). A rejected frame shows up as `InvalidIdentifier` or `…ProvisioningError`.
  - The server answers pings with pongs (`:822-824`) and closes with a close frame (`:837-850`, `AV:app.d:305`). The client can just read until `ProvisioningSuccess` (or an error) and then close.
  - Continuation frames are not handled properly on the server (`VD:websockets.d:855`, a FIXME), so **never fragment** what you send.
- **URL scheme:** `http://` → `ws://`, `https://` → `wss://` on the same host, port and path prefix (`XT:OmnisetteADIProvider.swift:45-49`; `SG:RemoteAnisetteDataProvider.swift:114-122`).

The client side is about 60 lines of stdlib code: the handshake, a framed text send with a 4-byte mask, and a receive that handles 7/16/64-bit lengths plus ping/close (an estimate).

## Other notes

- **Public server:** SideStore's default is `https://ani.sidestore.io`, and its server list is `https://servers.sidestore.io/servers.json` (`SS:SideStore/AppConstants.swift:98-99`). xtool defaults to the third-party `https://mac-ani.jkcoxson.com` (`XT:OmnisetteADIProvider.swift:10-12`).
  - With v3, a public server sees your identifier, adi.pb and request timing, but never the Apple ID password (*inferred* from the protocol: no credentials cross it).
  - Keep it opt-in, as already decided.
- **Server HTTPS flags look swapped:** `--private-key` writes to `certificateChainPath`, and `--cert-chain` writes to `privateKeyPath` (`AV:app.d:74-75`). The usual setup is plain HTTP on localhost, so this rarely matters.
- **What the server downloads:** it fetches Apple Music for Android's `libCoreADI.so`/`libstoreservicescore.so` from `apps.mzstatic.com` on first start (`AV:app.d:102-131`).
