"""GrandSlam (gsa.apple.com): the Apple ID login Xcode uses, then the Xcode app token.

The password never goes on the wire: SRP-6a (RFC 5054 group 2048, SHA-256), with Apple's
twists: x leaves the user name out, and the password enters as PBKDF2-SHA256(sha256(password)),
hex-encoded first for the "s2k_fo" protocol. The server's reply carries an encrypted "spd"
(AES-CBC under keys derived from the SRP session key) holding the account id (adsid) and the
IdMS token; those buy the Xcode app token (`com.apple.gs.xcode.auth`), which developer services
take. 2FA: a code pushed to the user's devices (or an SMS), then the login again.

Ported from PlumeImpactor (MIT, © 2025 Samara M) `crates/plume_core/src/auth/` and checked
against pysrp's rfc5054 + no_username_in_x arithmetic (the variant pypush used with Apple).
"""
import base64
import hashlib
import hmac
import json
import plistlib
import secrets
import uuid

from droidctl.core import UserError
from droidctl.ios import apple
from droidctl.ios.apple import anisette

GSA = "https://gsa.apple.com/grandslam/GsService2"
XCODE_APP = "com.apple.gs.xcode.auth"
GSA_AGENT = "akd/1.0 CFNetwork/978.0.7 Darwin/18.7.0"

# RFC 5054 Appendix A, the 2048-bit group, g = 2
N = int("AC6BDB41324A9A9BF166DE5E1389582FAF72B6651987EE07FC3192943DB56050A37329CBB4A099ED8193E0757767A13DD52312AB4B03310D"
        "CD7F48A9DA04FD50E8083969EDB767B0CF6095179A163AB3661A05FBD5FAAAE82918A9962F0B93B855F97993EC975EEAA80D740ADBF4FF"
        "747359D041D5C33EA71D281E446B14773BCA97B43A23FB801676BD207A436C6481F1D2B9078717461A5B9D32E688F87748544523B524B0"
        "D57D5EA77A2775D2ECFA032CFBDBF52FB3786160279004E57AE6AF874E7303CE53299CCC041C7BC308D82A5698F3A8D0C38271AE35F8E9"
        "DBFBB694B5C803D89F7AE435DE236D525F54759B65E372FCD68EF20FA7111F9E4AFF73", 16)
G = 2


# --------------------------------------------------------------------------
# SRP (PURE)
# --------------------------------------------------------------------------
def _b(n):
    return n.to_bytes((n.bit_length() + 7) // 8 or 1, "big")


def _i(b):
    return int.from_bytes(b, "big")


def _h(*parts, width=None):
    h = hashlib.sha256()
    for p in parts:
        data = _b(p) if isinstance(p, int) else p
        if width is not None:
            h.update(bytes(width - len(data)))
        h.update(data)
    return h.digest()


def password_key(password, salt, iterations, protocol):
    """The SRP "password": PBKDF2-SHA256 over sha256(password) (its hex for s2k_fo)."""
    p = hashlib.sha256(password.encode()).digest()
    if protocol == "s2k_fo":
        p = p.hex().encode()
    return hashlib.pbkdf2_hmac("sha256", p, salt, iterations, 32)


class SrpClient:
    """The client side, pysrp-compatible (rfc5054 padding, no user name in x)."""

    def __init__(self, username, a=None):
        self.I = username.encode()
        self.a = a if a is not None else _i(secrets.token_bytes(32))
        self.A = pow(G, self.a, N)
        self.width = len(_b(N))

    def process(self, salt, B_bytes, password):
        """M1 for the server's challenge; keeps K and the expected M2."""
        B = _i(B_bytes)
        if B % N == 0:
            raise UserError("Apple's login sent an invalid SRP challenge", "device")
        k = _i(_h(N, G, width=self.width))
        u = _i(_h(self.A, B, width=self.width))
        x = _i(_h(salt, _h(b":" + password)))
        S = pow((B - k * pow(G, x, N)) % N, self.a + u * x, N)
        self.K = hashlib.sha256(_b(S)).digest()
        hn, hg = hashlib.sha256(_b(N)).digest(), hashlib.sha256(bytes(self.width - len(_b(G))) + _b(G)).digest()
        m = hashlib.sha256()
        for part in (bytes(x ^ y for x, y in zip(hn, hg)), hashlib.sha256(self.I).digest(), salt,
                     _b(self.A), _b(B), self.K):
            m.update(part)
        self.M1 = m.digest()
        self.M2 = _h(self.A, self.M1, self.K)
        return self.M1

    def session_key(self, name):
        return hmac.new(self.K, name.encode(), hashlib.sha256).digest()


def decrypt_spd(srp, data):
    """The login's encrypted "spd": AES-256-CBC, key/iv from the session key."""
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    d = Cipher(algorithms.AES(srp.session_key("extra data key:")), modes.CBC(srp.session_key("extra data iv:")[:16])).decryptor()
    raw = d.update(data) + d.finalize()
    try:
        u = padding.PKCS7(128).unpadder()
        raw = u.update(raw) + u.finalize()
    except ValueError:
        pass
    return parse_spd(raw)


PLIST_HEADER = (b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                b'"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n')


def parse_spd(raw):
    """The decrypted spd: Apple sends a plist without its XML header (pypush prepends one), in
    some replies without the <plist> wrapper either. PURE."""
    body = raw.rstrip(b"\x00").strip()
    tries = [body, PLIST_HEADER + body]
    if body.startswith(b"<dict"):
        tries.append(PLIST_HEADER + b'<plist version="1.0">' + body + b"</plist>")
    for t in tries:
        try:
            d = plistlib.loads(t)
        except Exception:
            continue
        if isinstance(d, dict):
            return d
    shape = "XML" if body[:1] == b"<" else "binary" if body[:6] == b"bplist" else "unknown"
    raise UserError(f"Apple's sign-in reply could not be read ({shape} data, {len(body)} bytes)", "signing",
                    hint="please report this; nothing was stored", data={"reason": "spd-format"})


def decrypt_app_token(sk, et):
    """An app-token reply's "et": b"XYZ" + 16-byte IV + AES-256-GCM (AAD "XYZ")."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if len(et) < 35 or et[:3] != b"XYZ":
        raise UserError("Apple's app token is in an unknown format", "device")
    return parse_spd(AESGCM(sk).decrypt(et[3:19], et[19:], b"XYZ"))


# --------------------------------------------------------------------------
# requests
# --------------------------------------------------------------------------
class AuthError(UserError):
    pass


def status_error(resp):
    """(code, message) of a GrandSlam reply's Status, or None when ec == 0. PURE."""
    st = resp.get("Status", resp) if isinstance(resp.get("Status", resp), dict) else resp
    ec = st.get("ec", 0)
    return None if not ec else (ec, st.get("em") or "")


def _check(resp):
    err = status_error(resp)
    if err:
        code, msg = err
        hint = None
        if code == -22406:
            hint = "check the Apple ID and password"
        if code == anisette.REJECTED:
            hint = ("Apple refused this anisette server's machine data (before checking the password). Try "
                    f"another: --anisette {anisette.ALTERNATE} (or {anisette.PUBLIC}), or your own server")
            raise AuthError(f"Apple refused the sign-in ({code}): {msg}".rstrip(": "), "signing", hint=hint,
                            data={"reason": "anisette-rejected", "code": code})
        raise AuthError(f"Apple refused the sign-in ({code}): {msg}".rstrip(": "), "signing", hint=hint,
                        data={"reason": "apple-error", "code": code})


def gsa_post(provider, body):
    headers = {"Content-Type": "text/x-xml-plist", "Accept": "*/*", "User-Agent": anisette.user_agent(provider, GSA_AGENT),
               "X-MMe-Client-Info": anisette.client_info(provider)}
    req = {"Header": {"Version": "1.0.1"}, "Request": dict(body, cpd=anisette.headers(provider, cpd=True))}
    status, data = apple.request(GSA, plistlib.dumps(req), headers, method="POST")
    if status != 200:
        if status == 429:
            raise UserError("Apple is refusing sign-ins for now: too many in a short time (HTTP 429)", "signing",
                            hint="wait 15–30 minutes, then retry; a saved sign-in is reused without signing in again",
                            data={"reason": "rate-limited"})
        raise UserError(f"Apple's sign-in service answered HTTP {status}", "connection")
    return plistlib.loads(data)["Response"]


def auth_headers(provider, spd, json_body=False):
    """Headers for the 2FA endpoints: Xcode's identity plus anisette."""
    ident = base64.b64encode(f"{spd['adsid']}:{spd['GsIdmsToken']}".encode()).decode()
    h = anisette.headers(provider, client_info=True, app_info=True)
    kind = "application/json" if json_body else "text/x-xml-plist"
    h.update({"Content-Type": kind, "Accept": kind, "User-Agent": "Xcode", "Accept-Language": "en-us",
              "X-Apple-Identity-Token": ident})
    if h.get("X-Apple-Locale"):
        h["Loc"] = h["X-Apple-Locale"]
    return h


def srp_login(provider, username, password):
    """One SRP round trip pair. Returns (state, spd): state is "ok", "device-2fa", "sms-2fa",
    "verify-2fa" or another step's name."""
    user = username.strip().lower()
    srp = SrpClient(user)
    r = gsa_post(provider, {"A2k": _b(srp.A), "ps": ["s2k", "s2k_fo"], "u": user, "o": "init"})
    _check(r)
    if r.get("sp") not in ("s2k", "s2k_fo"):
        raise UserError(f"Apple asked for an unknown password protocol: {r.get('sp')}", "unsupported")
    m1 = srp.process(r["s"], r["B"], password_key(password, r["s"], r["i"], r["sp"]))
    r2 = gsa_post(provider, {"M1": m1, "c": r["c"], "u": user, "o": "complete"})
    _check(r2)
    if not hmac.compare_digest(r2.get("M2", b""), srp.M2):
        raise UserError("Apple's sign-in reply failed verification", "device")
    spd = decrypt_spd(srp, r2["spd"])
    spd.setdefault("appleId", user)
    st = r2.get("Status") or {}
    au = st.get("au")
    if au == "trustedDeviceSecondaryAuth":
        return "device-2fa", spd
    if au == "secondaryAuth":
        return "sms-2fa", spd
    if au:
        return au, spd
    if st.get("hsc") == 409:
        return "verify-2fa", spd
    return "ok", spd


def login(provider, username, password, ask_code, say=print, max_rounds=4):
    """The whole login, 2FA included. `ask_code(prompt)` returns what the user typed. Returns
    the spd (adsid, GsIdmsToken, sk, c, …)."""
    for _ in range(max_rounds):
        state, spd = srp_login(provider, username, password)
        if state == "ok":
            return spd
        if state in ("device-2fa", "verify-2fa"):
            if state == "device-2fa":
                apple.request("https://gsa.apple.com/auth/verify/trusteddevice", headers=auth_headers(provider, spd))
            code = ask_code("Enter the 6-digit code shown on your Apple devices (or \"sms\" to get a text): ").strip()
            if code.lower() == "sms":
                _sms(provider, spd, ask_code, say)
                continue
            h = dict(auth_headers(provider, spd), **{"security-code": code})
            _, data = apple.request(GSA + "/validate", headers=h)
            _check(plistlib.loads(data) if data.startswith(b"<?xml") or data.startswith(b"bplist") else {})
            continue                                     # a verified code: sign in again
        if state == "sms-2fa":
            _sms(provider, spd, ask_code, say)
            continue
        raise UserError(f"Apple wants an extra sign-in step droidctl can't do ({state})", "signing",
                        hint="sign in once at https://appleid.apple.com to finish it, then retry",
                        data={"reason": "extra-step", "step": state})
    raise UserError("Apple kept asking for verification", "signing", hint="retry: droidctl login")


def _sms(provider, spd, ask_code, say):
    h = auth_headers(provider, spd, json_body=True)
    _, data = apple.request("https://gsa.apple.com/auth", headers=h)
    phones = (json.loads(data or b"{}").get("trustedPhoneNumbers") or [{"id": 1}])
    phone = phones[0]
    body = {"phoneNumber": {"id": phone["id"]}, "mode": "sms"}
    st, _ = apple.request("https://gsa.apple.com/auth/verify/phone", json.dumps(body).encode(), h, method="PUT")
    if st >= 300:
        raise UserError(f"Apple could not send the SMS (HTTP {st})", "signing")
    say(f"A code was texted to the number ending in {phone.get('lastTwoDigits', '??')}.")
    code = ask_code("Enter the code from the text message: ").strip()
    body["securityCode"] = {"code": code}
    st, _ = apple.request("https://gsa.apple.com/auth/verify/phone/securitycode", json.dumps(body).encode(), h,
                          method="POST")
    if st >= 300:
        raise UserError("Apple rejected the code", "signing", hint="retry: droidctl login")


def app_token(provider, spd, app=XCODE_APP):
    """{token, expiry, duration} of an app token (Xcode's by default)."""
    checksum = hmac.new(spd["sk"], b"apptokens" + spd["adsid"].encode() + app.encode(), hashlib.sha256).digest()
    r = gsa_post(provider, {"app": [app], "c": spd["c"], "checksum": checksum, "o": "apptokens",
                            "t": spd["GsIdmsToken"], "u": spd["adsid"]})
    _check(r)
    tok = (decrypt_app_token(spd["sk"], r["et"]).get("t") or {}).get(app) or {}
    if not tok.get("token"):
        raise UserError("Apple gave no Xcode token", "signing")
    return {"token": tok["token"], "expiry": tok.get("expiry"), "duration": tok.get("duration")}


def new_request_id():
    return str(uuid.uuid4()).upper()
