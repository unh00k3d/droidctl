"""Signing without Xcode: the Apple sign-in's arithmetic and parsing, the prebuilt artifact,
re-signing helpers, secret storage. Nothing here talks to Apple or a phone; the live checks
are `research/ios-signing.md` §D.4 (M1–M8)."""
import hashlib
import os
import plistlib
import secrets as pysecrets
import zipfile

import pytest

from droidctl.core import UserError
from droidctl.ios import prebuilt, provision, sign
from droidctl.ios.apple import anisette, devservices, gsa
from droidctl.ios.apple import secrets as store


# -- SRP: a server written from the same equations must agree on M1, M2 and the key
def _server(username, password_key, salt, A):
    N, g, width = gsa.N, gsa.G, len(gsa._b(gsa.N))
    x = gsa._i(gsa._h(salt, gsa._h(b":" + password_key)))
    v = pow(g, x, N)
    b = gsa._i(pysecrets.token_bytes(32))
    k = gsa._i(gsa._h(N, g, width=width))
    B = (k * v + pow(g, b, N)) % N
    u = gsa._i(gsa._h(A, B, width=width))
    S = pow(A * pow(v, u, N) % N, b, N)
    return B, hashlib.sha256(gsa._b(S)).digest()


@pytest.mark.parametrize("protocol", ["s2k", "s2k_fo"])
def test_srp_client_and_server_agree(protocol):
    salt = os.urandom(16)
    pk = gsa.password_key("correct horse", salt, 1000, protocol)
    c = gsa.SrpClient("User@Example.com".lower())
    B, K = _server("user@example.com", pk, salt, c.A)
    c.process(salt, gsa._b(B), pk)
    assert c.K == K and len(c.M1) == 32 and c.M2 == gsa._h(c.A, c.M1, c.K)


def test_password_key_differs_by_protocol():
    salt = b"\x01" * 16
    assert gsa.password_key("pw", salt, 10, "s2k") != gsa.password_key("pw", salt, 10, "s2k_fo")


def test_srp_refuses_a_zero_challenge():
    c = gsa.SrpClient("a@b.c")
    with pytest.raises(UserError):
        c.process(b"s" * 16, gsa._b(gsa.N), b"k")


def test_spd_and_app_token_decrypt():
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    c = gsa.SrpClient("a@b.c")
    c.K = os.urandom(32)
    spd = {"adsid": "000123-08-abc", "GsIdmsToken": "tok", "sk": os.urandom(32), "c": b"cookie"}
    p = padding.PKCS7(128).padder()
    raw = p.update(plistlib.dumps(spd)) + p.finalize()
    e = Cipher(algorithms.AES(c.session_key("extra data key:")), modes.CBC(c.session_key("extra data iv:")[:16])).encryptor()
    assert gsa.decrypt_spd(c, e.update(raw) + e.finalize()) == spd
    iv = os.urandom(16)
    tok = plistlib.dumps({"t": {gsa.XCODE_APP: {"token": "xcode-token", "expiry": 1, "duration": 2}}})
    et = b"XYZ" + iv + AESGCM(spd["sk"]).encrypt(iv, tok, b"XYZ")
    assert gsa.decrypt_app_token(spd["sk"], et)["t"][gsa.XCODE_APP]["token"] == "xcode-token"
    bare = tok[tok.index(b"<plist"):]                      # as Apple sends it: no XML header
    et2 = b"XYZ" + iv + AESGCM(spd["sk"]).encrypt(iv, bare, b"XYZ")
    assert gsa.decrypt_app_token(spd["sk"], et2)["t"][gsa.XCODE_APP]["token"] == "xcode-token"
    with pytest.raises(UserError):
        gsa.decrypt_app_token(spd["sk"], b"ABC" + et[3:])


def test_gsa_status_errors_are_typed():
    assert gsa.status_error({"Status": {"ec": 0}}) is None
    assert gsa.status_error({"Status": {"ec": -22406, "em": "bad"}}) == (-22406, "bad")
    with pytest.raises(UserError) as e:
        gsa._check({"Status": {"ec": -22406, "em": "Your Apple ID or password was entered incorrectly."}})
    assert e.value.kind == "signing" and "password" in e.value.hint


# -- anisette flavours
class FakeProvider:
    def base(self):
        return {"X-Apple-I-MD": "otp", "X-Apple-I-MD-M": "mid", "X-Apple-Locale": "en_US",
                "X-Mme-Client-Info": "<MacBookPro15,1> <macOS;15.7.9;24G830> <com.apple.AuthKit/1 (org.python.python/3.13)>"}


def test_anisette_header_flavours():
    p = FakeProvider()
    login = anisette.headers(p, cpd=True)
    assert "X-Mme-Client-Info" not in login and login["bootstrap"] == "true" and login["X-Apple-I-MD"] == "otp"
    xcode = anisette.headers(p, client_info=True, app_info=True)
    assert xcode["X-Mme-Client-Info"].endswith("<com.apple.AuthKit/1 (com.apple.akd/1.0)>")
    assert xcode["X-Mme-Client-Info"].startswith("<MacBookPro15,1> <macOS;15.7.9;24G830> ")
    assert xcode["X-Apple-App-Info"] == "com.apple.gs.xcode.auth" and "bootstrap" not in xcode
    assert anisette.client_info(p).endswith("(org.python.python/3.13)>")


# -- developer services replies
def test_dev_replies_type_their_errors():
    assert devservices.parse_reply(plistlib.dumps({"resultCode": 0, "teams": []})) == {"resultCode": 0, "teams": []}
    with pytest.raises(UserError) as e:
        devservices.parse_reply(plistlib.dumps({"resultCode": 7460, "userString": "Maximum certificates"}))
    assert e.value.kind == "signing" and e.value.data["reason"] == "cert-limit"
    with pytest.raises(UserError) as e:
        devservices.parse_reply(plistlib.dumps({"resultCode": 1100, "resultString": "session expired"}))
    assert e.value.data["reason"] == "token-expired" and "droidctl login" in e.value.hint
    with pytest.raises(UserError):
        devservices.parse_reply(b"<html>")


# -- profiles and re-signing helpers
def _profile(team="TEAMID0000", bundle="dev.droidctl.rabc.runner.xctrunner", certs=(b"cert-a",)):
    body = plistlib.dumps({"UUID": "U-1", "Entitlements": {"application-identifier": f"{team}.{bundle}",
                                                           "get-task-allow": True,
                                                           "com.apple.developer.team-identifier": team},
                           "DeveloperCertificates": list(certs)})
    return b"0\x82CMS-header" + body + b"\x00signature"


def test_decode_profile_and_cert_hashes():
    prof = sign.decode_profile(_profile())
    assert prof["UUID"] == "U-1"
    assert sign.cert_sha1s(prof) == [hashlib.sha1(b"cert-a").hexdigest().upper()]
    assert sign.entitlements(prof)["get-task-allow"] is True
    with pytest.raises(UserError):
        sign.decode_profile(b"garbage")


def _fake_artifact(path):
    app = os.path.join(path, "Debug-iphoneos", prebuilt.APP)
    os.makedirs(os.path.join(app, prebuilt.PLUGIN))
    with open(os.path.join(app, "Info.plist"), "wb") as f:
        plistlib.dump({"CFBundleIdentifier": "dev.droidctl.prebuilt.runner.xctrunner"}, f, fmt=plistlib.FMT_BINARY)
    with open(os.path.join(app, prebuilt.PLUGIN, "Info.plist"), "wb") as f:
        plistlib.dump({"CFBundleIdentifier": "dev.droidctl.prebuilt.runner"}, f)
    with open(os.path.join(app, "DroidctlRunner-Runner"), "wb") as f:
        f.write(b"\xcf\xfa\xed\xfe" + b"\0" * 28)
    with open(os.path.join(path, "runner.xctestrun"), "wb") as f:
        plistlib.dump({"__xctestrun_metadata__": {"FormatVersion": 1},
                       "DroidctlRunner": {"TestHostBundleIdentifier": "dev.droidctl.prebuilt.runner.xctrunner",
                                          "BundleIdentifiersForCrashReportEmphasis": ["dev.droidctl.prebuilt.runner"]}}, f)
    with open(os.path.join(path, "manifest.json"), "w") as f:
        f.write('{"build": "abc123", "stripped": true}')
    return app


def test_ids_are_rewritten_per_team_keeping_the_plist_format(tmp_path):
    app = _fake_artifact(str(tmp_path / "a"))
    assert prebuilt.placeholder_ids(str(tmp_path / "a")) == ("dev.droidctl.prebuilt.runner.xctrunner",
                                                             "dev.droidctl.prebuilt.runner")
    ids = sign.rewrite_ids(app, "dev.droidctl.rabc")
    assert ids == ("dev.droidctl.rabc.runner.xctrunner", "dev.droidctl.rabc.runner")
    with open(os.path.join(app, "Info.plist"), "rb") as f:
        raw = f.read()
    assert raw.startswith(b"bplist") and plistlib.loads(raw)["CFBundleIdentifier"] == ids[0]
    out = str(tmp_path / "x.xctestrun")
    sign.rewrite_xctestrun(str(tmp_path / "a" / "runner.xctestrun"), out, *ids)
    with open(out, "rb") as f:
        x = plistlib.load(f)
    assert x["DroidctlRunner"]["TestHostBundleIdentifier"] == ids[0]
    assert x["DroidctlRunner"]["BundleIdentifiersForCrashReportEmphasis"] == [ids[1]]


def test_a_runner_zip_unpacks_with_executables_and_refuses_unsafe_paths(tmp_path):
    _fake_artifact(str(tmp_path / "a"))
    z = str(tmp_path / "r.zip")
    prebuilt.make_zip(str(tmp_path / "a"), z)
    out = str(tmp_path / "out")
    prebuilt._unzip(z, out)
    assert prebuilt.valid(out)
    assert os.access(os.path.join(out, "Debug-iphoneos", prebuilt.APP, "DroidctlRunner-Runner"), os.X_OK)
    bad = str(tmp_path / "bad.zip")
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("../evil", "x")
    with pytest.raises(UserError):
        prebuilt._unzip(bad, str(tmp_path / "out2"))


def test_the_same_artifact_zips_to_the_same_bytes(tmp_path):
    _fake_artifact(str(tmp_path / "a"))
    first = prebuilt.make_zip(str(tmp_path / "a"), str(tmp_path / "1.zip"))
    os.utime(os.path.join(str(tmp_path / "a"), "manifest.json"), (1, 1))     # mtimes don't matter
    assert prebuilt.make_zip(str(tmp_path / "a"), str(tmp_path / "2.zip")) == first


def test_the_shipped_zip_is_used_when_it_matches_the_sources(tmp_path, monkeypatch):
    _fake_artifact(str(tmp_path / "a"))
    z = str(tmp_path / "r.zip")
    prebuilt.make_zip(str(tmp_path / "a"), z)
    monkeypatch.delenv("DROIDCTL_IOS_RUNNER", raising=False)
    monkeypatch.setattr(prebuilt, "BUNDLED", z)
    monkeypatch.setattr(prebuilt, "build_id", lambda: "abc123")
    out = prebuilt.ensure()
    assert prebuilt.valid(out) and out.endswith("abc123")


def test_a_stale_shipped_zip_without_xcode_is_a_typed_error(tmp_path, monkeypatch):
    _fake_artifact(str(tmp_path / "a"))
    z = str(tmp_path / "r.zip")
    prebuilt.make_zip(str(tmp_path / "a"), z)
    monkeypatch.delenv("DROIDCTL_IOS_RUNNER", raising=False)
    monkeypatch.setattr(prebuilt, "BUNDLED", z)
    monkeypatch.setattr(prebuilt, "build_id", lambda: "newer0")
    monkeypatch.setattr(prebuilt.devices, "developer_dir", lambda: None)
    with pytest.raises(UserError) as e:
        prebuilt.ensure()
    assert e.value.data == {"reason": "no-runner", "shipped": "abc123", "wanted": "newer0"}


def test_the_env_artifact_is_used_as_is(tmp_path, monkeypatch):
    _fake_artifact(str(tmp_path / "a"))
    monkeypatch.setenv("DROIDCTL_IOS_RUNNER", str(tmp_path / "a"))
    assert prebuilt.ensure() == str(tmp_path / "a")


# -- the account record, the signing mode, secret storage
def test_mode_follows_droidctls_own_sign_in(tmp_path, monkeypatch):
    monkeypatch.setenv("DROIDCTL_HOME", str(tmp_path))
    monkeypatch.delenv("DROIDCTL_IOS_SIGNING", raising=False)
    assert provision.mode("TEAMID0000") == "xcode"
    provision._save_record({"account": "a", "team": "TEAMID0000"})
    assert provision.mode("TEAMID0000") == "apple-id" and provision.mode("OTHER00000") == "xcode"
    monkeypatch.setenv("DROIDCTL_IOS_SIGNING", "xcode")
    assert provision.mode("TEAMID0000") == "xcode"


def test_the_runner_build_id_has_no_team_when_prebuilt(tmp_path, monkeypatch):
    from droidctl.ios import runner
    monkeypatch.setenv("DROIDCTL_HOME", str(tmp_path))
    monkeypatch.setenv("DROIDCTL_IOS_SIGNING", "apple-id")
    assert runner.build_id("TEAMID0000") == runner.build_id("OTHER00000") == prebuilt.build_id()


def test_secrets_file_fallback_is_private_and_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("DROIDCTL_HOME", str(tmp_path))
    monkeypatch.setattr(store, "backend", lambda: "file")
    assert store.save("acct", {"token": "t"}) == "file"
    path = store._file("acct")
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert store.load("acct") == {"token": "t"}
    store.delete("acct")
    assert store.load("acct") is None


def test_logout_without_a_sign_in(tmp_path, monkeypatch):
    monkeypatch.setenv("DROIDCTL_HOME", str(tmp_path))
    assert provision.logout() == {"ok": True, "signed_out": False}
    with pytest.raises(UserError) as e:
        provision.session()
    assert e.value.kind == "signing" and e.value.data["reason"] == "no-login"


def test_spd_without_its_xml_header_or_wrapper_is_read():
    full = plistlib.dumps({"adsid": "x", "sk": b"k"})
    bare = full[full.index(b"<plist"):]
    assert gsa.parse_spd(bare)["adsid"] == "x"
    inner = bare[bare.index(b"<dict>"):bare.rindex(b"</plist>")]
    assert gsa.parse_spd(inner + b"\n")["adsid"] == "x"
    with pytest.raises(UserError) as e:
        gsa.parse_spd(b"\x07garbage")
    assert e.value.data["reason"] == "spd-format" and "garbage" not in str(e.value)


def test_login_reuses_a_saved_token_without_the_password(tmp_path, monkeypatch):
    """Apple answers 429 to repeated sign-ins: a rerun after a later failure must not sign in again."""
    from droidctl.ios.apple import anisette as an
    monkeypatch.setenv("DROIDCTL_HOME", str(tmp_path))
    monkeypatch.setattr(store, "backend", lambda: "file")
    monkeypatch.setattr(an, "default_provider", lambda url=None: FakeProvider())
    store.save("ADSID", {"adsid": "ADSID", "token": "tok"})
    provision._save_record({"apple_id": "me@example.com", "account": "ADSID", "team": None})
    monkeypatch.setattr(devservices.Session, "teams",
                        lambda self: [{"id": "TEAMID0000", "name": "Me", "free": True, "type": "", "status": ""}])

    def no_password():
        raise AssertionError("asked for the password")
    out = provision.login("me@example.com", no_password, ask_code=None, say=lambda line: None)
    assert out["team"] == "TEAMID0000" and provision.signed_in_team() == "TEAMID0000"


def test_a_rate_limited_sign_in_is_typed(monkeypatch):
    from droidctl.ios import apple
    monkeypatch.setattr(apple, "request", lambda *a, **k: (429, b""))
    with pytest.raises(UserError) as e:
        gsa.gsa_post(FakeProvider(), {"o": "init"})
    assert e.value.data["reason"] == "rate-limited" and "wait" in e.value.hint


# -- setup's Apple sign-in step
def _ctx(ask=None, secret=None):
    from droidctl.ios import setup
    return setup.Ctx("00008101-00AAAAAAAAAAAAAA", None, lambda line: None, ask, secret)


@pytest.fixture
def no_signing_override(tmp_path, monkeypatch):
    monkeypatch.setenv("DROIDCTL_HOME", str(tmp_path))
    monkeypatch.delenv("DROIDCTL_IOS_SIGNING", raising=False)


def test_setup_sign_in_passes_silently_with_a_working_saved_sign_in(no_signing_override, monkeypatch):
    from droidctl.ios import setup
    provision._save_record({"apple_id": "me@example.com", "account": "A", "team": "TEAMID0000", "team_name": "Me"})
    monkeypatch.setattr(provision, "session", lambda: (type("S", (), {"teams": lambda self: []})(), {}))
    ctx = _ctx()
    assert "me@example.com" in setup.step_apple(ctx)


def test_setup_sign_in_without_a_terminal_says_where_to_run_it(no_signing_override):
    from droidctl.ios import setup
    with pytest.raises(setup.Need) as e:
        setup.step_apple(_ctx())
    assert e.value.reason == "no-login" and "terminal" in e.value.instructions[0]


def test_setup_sign_in_asks_retries_a_wrong_password_and_sets_the_team(no_signing_override, monkeypatch):
    from droidctl.ios import setup
    calls = []

    def fake_login(apple_id, get_password, **kw):
        calls.append((apple_id, get_password(), kw["fresh"]))
        if len(calls) == 1:
            raise UserError("wrong", "signing", data={"code": setup.WRONG_PASSWORD})
        return {"apple_id": apple_id, "team": "TEAMID0000", "team_name": "Me"}
    monkeypatch.setattr(provision, "login", fake_login)
    monkeypatch.setattr(setup, "_anisette_url", lambda ctx, rec: None)   # Linux would ask for a server first
    ctx = _ctx(ask=lambda q: "me@example.com", secret=lambda q: "pw")
    assert "TEAMID0000" in setup.step_apple(ctx) and ctx.team == "TEAMID0000"
    assert calls == [("me@example.com", "pw", False), ("me@example.com", "pw", True)]


def test_setup_sign_in_left_blank_keeps_xcode(no_signing_override, monkeypatch):
    from droidctl.ios import setup
    monkeypatch.setattr(provision, "login", lambda *a, **k: pytest.fail("signed in"))
    assert "Xcode" in setup.step_apple(_ctx(ask=lambda q: "", secret=lambda q: "x"))


def test_gzipped_replies_are_read():
    """Developer services gzip their replies without being asked (2026-11-09)."""
    import gzip
    from droidctl.ios import apple
    body = plistlib.dumps({"resultCode": 0, "teams": []})
    assert apple.gunzip(gzip.compress(body)) == body and apple.gunzip(body) == body
    assert devservices.parse_reply(apple.gunzip(gzip.compress(body)))["teams"] == []


def test_free_team_is_read_from_the_membership():
    from droidctl.ios.apple.devservices import is_free
    assert is_free({"type": "Individual", "xcodeFreeOnly": False,
                    "memberships": [{"name": "Xcode Free Provisioning Program", "platform": "ios"}]})
    assert not is_free({"type": "Individual", "xcodeFreeOnly": False,
                        "memberships": [{"name": "Apple Developer Program", "platform": "ios"}]})
    assert is_free({"xcodeFreeOnly": True})
