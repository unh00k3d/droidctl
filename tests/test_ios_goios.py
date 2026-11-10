"""Linux / no-Xcode: go-ios as the device side, droidctl's own certificate, anisette v3.

Nothing here runs go-ios or talks to Apple: go-ios's output shapes come from its v1.3.2 source
(research/ios-linux.md), the anisette exchange from research/ios-anisette-v3.md."""
import base64
import hashlib
import io
import json
import os
import socket
import threading
import zipfile

import pytest

from droidctl.core import UserError
from droidctl.ios import goios, identity, runner, sign

UDID = "00008101-00AAAAAAAAAAAAAA"


# -- the go-ios binary
def test_the_release_matches_the_os_and_cpu():
    rel, member = goios.release_for("linux", "x86_64")
    assert rel["asset"] == "go-ios-linux.zip" and member == "ios-amd64"
    assert goios.release_for("linux", "aarch64")[1] == "ios-arm64"
    assert goios.release_for("darwin", "arm64")[1] == "ios"
    assert goios.release_for("win32", "x86_64") is None


def _fake_release(tmp_path, monkeypatch, content=b"\x7fELFgo-ios"):
    z = tmp_path / "go-ios-linux.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("ios-amd64", content)
    sha = hashlib.sha256(z.read_bytes()).hexdigest()
    monkeypatch.setattr(goios, "URL", "file://" + str(tmp_path) + "/{asset}")
    monkeypatch.setattr(goios, "release_for", lambda *a: ({"asset": "go-ios-linux.zip", "size": 1, "sha256": sha,
                                                           "member": {}}, "ios-amd64"))
    monkeypatch.setattr("droidctl.ios.apple.tls_context", lambda: None)
    monkeypatch.delenv("DROIDCTL_GO_IOS", raising=False)
    return sha


def test_go_ios_is_downloaded_once_checked_and_made_executable(tmp_path, monkeypatch):
    _fake_release(tmp_path, monkeypatch)
    assert goios.binary() is None
    path = goios.fetch()
    assert os.access(path, os.X_OK) and open(path, "rb").read() == b"\x7fELFgo-ios"
    assert goios.binary() == path


def test_a_go_ios_download_with_the_wrong_checksum_is_refused(tmp_path, monkeypatch):
    _fake_release(tmp_path, monkeypatch)
    rel, member = goios.release_for()
    monkeypatch.setattr(goios, "release_for", lambda *a: (dict(rel, sha256="0" * 64), member))
    with pytest.raises(UserError) as e:
        goios.fetch()
    assert e.value.data["reason"] == "checksum" and goios.binary() is None


def test_drOIDCTL_GO_IOS_must_be_an_executable(tmp_path, monkeypatch):
    monkeypatch.setenv("DROIDCTL_GO_IOS", str(tmp_path / "nope"))
    with pytest.raises(UserError):
        goios.binary()


def test_the_ios_range_without_xcode():
    assert goios.version_ok("17.4.0") == (False, True)
    assert goios.version_ok("17.4.1") == (True, True)
    assert goios.version_ok("18.7.8") == (True, True)
    assert goios.version_ok("26.1") == (True, False)


# -- go-ios's output
def test_go_ios_errors_come_from_its_json_log_lines():
    err = "\n".join([json.dumps({"level": "INFO", "msg": "installing"}),
                     json.dumps({"level": "ERROR", "msg": "failed writing", "err": "ApplicationVerificationFailed"}),
                     json.dumps({"level": "INFO", "msg": "Failed running Xcuitest", "error": "pidFromResponse: could "
                                 "not get pid from response"}), "not json"])
    assert goios.log_errors(err) == ["failed writing: ApplicationVerificationFailed",
                                     "Failed running Xcuitest: pidFromResponse: could not get pid from response"]


def test_ps_output_becomes_the_devicectl_shape():
    out = 'some log\n[{"IsApplication":true,"Name":"Runner","Pid":42,"RealAppName":' \
          '"/private/var/containers/Bundle/Application/X/R.app/R","StartDate":"2026-11-10T00:00:00Z"}]\n'
    procs = goios.parse_ps(goios.json_out(out))
    assert procs == [{"pid": 42, "path": "/var/containers/Bundle/Application/X/R.app/R"}]
    from droidctl.ios import devices
    assert devices.pids_of({"url": "file:///private/var/containers/Bundle/Application/X/R.app/"}, procs) == [42]


def test_go_ios_start_failures_are_typed_before_xcodes_rules():
    assert runner.classify_output("Did not find test app for 'x' on device. Is it installed?")[3] == "not-installed"
    # "is not available" is also in an Xcode rule (bundle ids): the go-ios tunnel rule must win
    assert runner.classify_output("service 'com.apple.dt.testmanagerd' is not available in RSD")[3] == "tunnel"
    assert runner.classify_output("pidFromResponse: could not get pid from response")[:1] == ("not-trusted",)


def test_the_runtest_command_names_every_id(monkeypatch):
    monkeypatch.setattr(goios, "ensure", lambda say=None: "/bin/ios")
    cmd = goios.runtest_cmd(UDID, "dev.droidctl.rabc.runner.xctrunner", 27100, "/tmp/r.log")
    assert cmd[:2] == ["/bin/ios", "runtest"]
    assert {f"--udid={UDID}", "--test-runner-bundle-id=dev.droidctl.rabc.runner.xctrunner",
            "--xctest-config=DroidctlRunner.xctest", "--env=DROIDCTL_PORT=27100", "--log-output=/tmp/r.log",
            f"--tunnel-info-port={goios.agent_port(UDID)}"} <= set(cmd)


def test_our_agent_port_is_private_and_stable():
    p = goios.agent_port(UDID)
    assert goios.AGENT_PORT_BASE <= p < goios.AGENT_PORT_BASE + goios.AGENT_PORTS and p != 28100
    assert goios.agent_port(UDID) == p


def test_a_mounted_image_is_kept(monkeypatch):
    from droidctl.ios import lockdown
    monkeypatch.setattr(lockdown, "developer_image_mounted", lambda udid: True)
    monkeypatch.setattr(goios, "run", lambda *a, **k: pytest.fail("must not mount over an existing image"))
    assert goios.ensure_ddi(UDID) == "already mounted"


def test_an_image_go_ios_failed_to_mount_is_a_typed_error(monkeypatch):
    from droidctl.ios import lockdown
    monkeypatch.setattr(lockdown, "developer_image_mounted", lambda udid: False)
    monkeypatch.setattr(goios, "run", lambda *a, **k: ("", json.dumps({"level": "ERROR", "msg": "TSS failed"})))
    with pytest.raises(UserError) as e:
        goios.ensure_ddi(UDID)                     # `image auto` exits 0 on failure: checked through lockdown
    assert e.value.data["reason"] == "ddi" and e.value.data["errors"] == ["TSS failed"]


# -- the backend seam
def test_without_xcode_the_device_calls_go_through_lockdown_and_go_ios(monkeypatch):
    from droidctl.ios import devices, lockdown
    monkeypatch.setenv("DROIDCTL_IOS_BACKEND", "goios")
    calls = []
    monkeypatch.setattr(lockdown, "locked", lambda udid: calls.append(("locked", udid)) or False)
    monkeypatch.setattr(lockdown, "uninstall", lambda udid, b: calls.append(("uninstall", b)))
    monkeypatch.setattr(goios, "kill", lambda udid, pid: calls.append(("kill", pid)))
    monkeypatch.setattr(devices, "devicectl", lambda *a, **k: pytest.fail("no devicectl without Xcode"))
    assert devices.lock_state(UDID) == {"locked": False, "unlocked_since_boot": None}
    devices.uninstall(UDID, "dev.droidctl.rx.runner.xctrunner")
    devices.terminate(UDID, 7)
    assert calls == [("locked", UDID), ("uninstall", "dev.droidctl.rx.runner.xctrunner"), ("kill", 7)]


def test_the_backend_follows_xcode_unless_forced(monkeypatch):
    from droidctl.ios import devices
    monkeypatch.delenv("DROIDCTL_IOS_BACKEND")
    monkeypatch.setattr(devices, "developer_dir", lambda: None)
    assert devices.backend() == "goios"
    monkeypatch.setattr(devices, "developer_dir", lambda: "/Applications/Xcode.app/Contents/Developer")
    assert devices.backend() == "xcode"
    monkeypatch.setenv("DROIDCTL_IOS_BACKEND", "goios")
    assert devices.backend() == "goios"


# -- setup without Xcode
def _ctx(answer=None):
    from droidctl.ios import setup
    return setup.Ctx(UDID, None, lambda line: None, (lambda q: answer) if answer is not None else None)


def test_setup_without_xcode_asks_before_downloading_go_ios(monkeypatch):
    from droidctl.ios import setup
    monkeypatch.setenv("DROIDCTL_IOS_BACKEND", "goios")
    monkeypatch.setattr(setup, "_usbmux_ok", lambda: True)
    monkeypatch.setattr(goios, "binary", lambda: None)
    fetched = []
    monkeypatch.setattr(goios, "fetch", lambda say: fetched.append(1))
    with pytest.raises(setup.Need) as e:           # not in a terminal: say how, download nothing
        setup.step_tools(_ctx())
    assert e.value.reason == "go-ios" and not fetched
    with pytest.raises(UserError):
        setup.step_tools(_ctx("n"))
    assert not fetched
    assert "downloaded" in setup.step_tools(_ctx("")) and fetched == [1]     # Enter = yes


def test_setup_without_xcode_needs_usbmuxd(monkeypatch):
    from droidctl.ios import setup
    monkeypatch.setenv("DROIDCTL_IOS_BACKEND", "goios")
    monkeypatch.setattr(setup, "_usbmux_ok", lambda: False)
    with pytest.raises(setup.Need) as e:
        setup.step_tools(_ctx())
    assert e.value.reason == "usbmuxd" and "apt install usbmuxd" in " ".join(e.value.instructions)


def test_without_xcode_the_team_comes_only_from_droidctls_sign_in(monkeypatch):
    from droidctl.ios import provision, setup
    monkeypatch.setenv("DROIDCTL_IOS_BACKEND", "goios")
    monkeypatch.setattr(provision, "signed_in_team", lambda: None)
    monkeypatch.setattr(setup, "all_teams", lambda: pytest.fail("no Xcode teams without Xcode"))
    with pytest.raises(setup.Need) as e:
        setup.step_team(_ctx())
    assert e.value.reason == "no-login"


# -- the certificate
class FakeDev:
    def __init__(self, certs, limit=False):
        self._certs, self.limit, self.csrs = certs, limit, []

    def certs(self, team):
        return list(self._certs)

    def submit_csr(self, team, csr, machine):
        if self.limit:
            raise UserError("limit", "signing", data={"reason": "cert-limit", "code": 7460})
        self.csrs.append(csr)
        self._certs.append({"id": "NEW1", "der": b"new-cert", "name": "droidctl", "machine": machine})
        return {"certificateId": "NEW1"}


@pytest.fixture
def keyring(monkeypatch):
    from droidctl.ios.apple import secrets
    store = {}
    monkeypatch.setattr(secrets, "load", lambda acct: store.get(acct))
    monkeypatch.setattr(secrets, "save", lambda acct, v: store.__setitem__(acct, v) or "test")
    monkeypatch.setattr(identity, "make_csr", lambda: ("-----BEGIN PRIVATE KEY-----\nk\n", "CSR"))
    return store


def test_a_keychain_identity_the_team_lists_is_used_first(keyring):
    ds = FakeDev([{"id": "X", "der": b"xcode-cert"}])
    got = identity.ensure(ds, "TEAM", keychain=[identity.sha1(b"xcode-cert")])
    assert got == {"kind": "keychain", "sha1": identity.sha1(b"xcode-cert")} and not ds.csrs


def test_a_new_certificate_is_made_once_and_its_key_kept(keyring):
    ds = FakeDev([{"id": "X", "der": b"someone-elses"}])
    first = identity.ensure(ds, "TEAM", keychain=[])
    assert first["kind"] == "key" and first["sha1"] == identity.sha1(b"new-cert") and ds.csrs == ["CSR"]
    assert keyring["cert-TEAM"]["key_pem"].startswith("-----BEGIN PRIVATE KEY")
    again = identity.ensure(ds, "TEAM", keychain=[])
    assert again == first and ds.csrs == ["CSR"]


def test_a_full_team_is_a_typed_error_and_nothing_is_revoked(keyring):
    ds = FakeDev([{"id": "X", "der": b"a", "name": "iPhone Developer", "machine": "Mac", "expires": "2027"}], limit=True)
    with pytest.raises(UserError) as e:
        identity.ensure(ds, "TEAM", keychain=[])
    assert e.value.data["reason"] == "cert-limit" and e.value.data["certificates"][0]["machine"] == "Mac"
    assert "won't revoke" in e.value.hint


def test_signing_with_droidctls_key_goes_through_go_ios(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("sigtests", os.path.join(os.path.dirname(__file__), "test_ios_signing.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _fake_artifact = mod._fake_artifact
    _fake_artifact(str(tmp_path / "a"))
    team, key = "TEAMID0000", "-----BEGIN PRIVATE KEY-----\nk\n"
    app_id = runner.bundle_prefix(team) + ".runner.xctrunner"
    import plistlib
    prof_doc = {"UUID": "U1", "Entitlements": {"application-identifier": f"{team}.{app_id}"},
                "DeveloperCertificates": [b"new-cert"]}
    profile = b"CMS" + plistlib.dumps(prof_doc) + b"SIG"
    seen = {}

    def fake_run(udid, *args, **k):
        a = dict(x.split("=", 1) for x in args if x.startswith("--"))
        seen.update(a, key=open(a["--p12file"]).read(), mode=oct(os.stat(a["--p12file"]).st_mode & 0o777))
        import shutil
        shutil.copytree(a["--path"], a["--output"])
        return "", ""
    monkeypatch.setattr("droidctl.ios.goios.run", fake_run)
    out = sign.sign(str(tmp_path / "a"), team, profile, {"kind": "key", "sha1": identity.sha1(b"new-cert"),
                                                          "key_pem": key}, UDID)
    assert os.path.isdir(out) and seen["key"] == key and seen["mode"] == "0o600"
    assert seen["--bundleid"] == app_id and not os.path.exists(seen["--p12file"])   # the key file is gone
    assert os.path.exists(os.path.join(os.path.dirname(os.path.dirname(out)), "DroidctlRunner.xctestrun"))


def test_a_profile_without_droidctls_certificate_is_refused(tmp_path):
    import plistlib
    prof = b"CMS" + plistlib.dumps({"DeveloperCertificates": [b"other"]}) + b"SIG"
    with pytest.raises(UserError) as e:
        sign.sign(str(tmp_path), "T", prof, {"kind": "key", "sha1": "AB", "key_pem": "k"}, UDID)
    assert e.value.data["reason"] == "cert-not-in-profile"


# -- anisette v3
def test_anisette_ids_follow_xtool():
    from droidctl.ios.apple import anisette
    ident, device_id, lu = anisette.ids("1b4e28ba-2fa1-11d2-883f-0016d3cca427")
    assert base64.b64decode(ident) == bytes.fromhex("1b4e28ba2fa111d2883f0016d3cca427")
    assert device_id == "1B4E28BA-2FA1-11D2-883F-0016D3CCA427"
    assert lu == hashlib.sha256(device_id.encode()).hexdigest().upper()


def _ws_server(script):
    """A one-connection WebSocket server on localhost: replies to the upgrade, then for each
    (send, expect_key) sends `send` and reads one masked client frame."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    got = []

    def frame(obj):
        data = json.dumps(obj).encode()
        n = len(data)
        head = bytes([0x81]) + (bytes([n]) if n < 126 else bytes([126]) + n.to_bytes(2, "big"))
        return head + data

    def read_frame(c):
        b0, b1 = c.recv(2, socket.MSG_WAITALL)
        n = b1 & 0x7F
        if n == 126:
            n = int.from_bytes(c.recv(2, socket.MSG_WAITALL), "big")
        mask = c.recv(4, socket.MSG_WAITALL)
        data = c.recv(n, socket.MSG_WAITALL) if n else b""
        assert b1 & 0x80 and b0 == 0x81              # masked text frames only
        return json.loads(bytes(x ^ mask[i % 4] for i, x in enumerate(data)))

    def serve():
        c, _ = srv.accept()
        req = b""
        while b"\r\n\r\n" not in req:
            req += c.recv(4096)
        got.append(req.decode())
        c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
        for send, reply in script:
            c.sendall(frame(send))
            if reply:
                got.append(read_frame(c))
        c.close()
    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1], got


def test_the_provisioning_session_follows_the_v3_protocol(monkeypatch):
    from droidctl.ios.apple import anisette
    big = "A" * 300                                  # adi.pb is longer than 125 bytes: 16-bit lengths
    port, got = _ws_server([({"result": "GiveIdentifier"}, True), ({"result": "GiveStartProvisioningData"}, True),
                            ({"result": "GiveEndProvisioningData", "cpim": "CPIM"}, True),
                            ({"result": "ProvisioningSuccess", "adi_pb": big}, False)])
    p = anisette.V3Anisette(f"http://127.0.0.1:{port}")
    apple = []

    def fake_apple(url, body, uid):
        apple.append((url, body))
        if body is None:
            return {"urls": {"midStartProvisioning": "https://a/start", "midFinishProvisioning": "https://a/finish"}}
        if url.endswith("start"):
            return {"Response": {"spim": "SPIM"}}
        return {"Response": {"ptm": "PTM", "tk": "TK"}}
    monkeypatch.setattr(p, "_apple", fake_apple)
    uid = "1b4e28ba-2fa1-11d2-883f-0016d3cca427"
    assert p.provision(uid) == big
    assert "Upgrade: websocket" in got[0] and "Sec-WebSocket-Version: 13" in got[0]
    assert got[1:] == [{"identifier": anisette.ids(uid)[0]}, {"spim": "SPIM"}, {"ptm": "PTM", "tk": "TK"}]
    assert apple[2] == ("https://a/finish", {"Header": {}, "Request": {"cpim": "CPIM"}})


def test_a_failed_provisioning_step_is_typed():
    from droidctl.ios.apple import anisette
    port, _ = _ws_server([({"result": "Timeout"}, False)])
    ws = anisette.WebSocket(f"http://127.0.0.1:{port}/v3/provisioning_session")
    with pytest.raises(UserError) as e:
        ws.expect("GiveIdentifier")
    assert e.value.data["reason"] == "anisette-provisioning"
    ws.close()


def test_v3_headers_come_from_the_server_and_the_kept_identity(monkeypatch):
    from droidctl.ios.apple import anisette
    state = {"server": "http://x", "uid": "1B4E28BA-2FA1-11D2-883F-0016D3CCA427", "adi_pb": "ADI"}
    p = anisette.V3Anisette("http://x", store=(lambda: dict(state), lambda st: state.update(st)))
    sent = []

    def http(path, body=None):
        sent.append((path, body))
        if path == "/v3/client_info":
            return {"client_info": "<MacBookPro13,2> <macOS;13.1;22C65> <com.apple.AuthKit/1 (com.apple.akd/1.0)>",
                    "user_agent": "akd/1.0 CFNetwork/808.1.4"}
        return {"result": "Headers", "X-Apple-I-MD": "OTP", "X-Apple-I-MD-M": "MID", "X-Apple-I-MD-RINFO": "17106176"}
    monkeypatch.setattr(p, "_http", http)
    h = p.base()
    assert h["X-Apple-I-MD"] == "OTP" and h["X-Mme-Device-Id"] == state["uid"]
    assert h["X-Apple-I-MD-LU"] == anisette.ids(state["uid"])[2] and h["X-Apple-I-Client-Time"].endswith("Z")
    assert sent[0] == ("/v3/get_headers", {"identifier": anisette.ids(state["uid"])[0], "adi_pb": "ADI"})
    # it works with droidctl's header flavours (the login's cpd, AuthKit's client info)
    assert anisette.headers(p, cpd=True, client_info=True)["X-Mme-Client-Info"].endswith(
        "<com.apple.AuthKit/1 (com.apple.akd/1.0)>")


def test_a_get_headers_error_comes_with_http_200_and_is_typed(monkeypatch):
    from droidctl.ios.apple import anisette
    p = anisette.V3Anisette("http://x", store=(lambda: {"server": "http://x", "uid": "1B4E28BA-2FA1-11D2-883F-0016D3CCA427",
                                                        "adi_pb": "ADI"}, lambda st: None))
    monkeypatch.setattr(p, "_http", lambda path, body=None: {"client_info": "c", "user_agent": "u"}
                        if path == "/v3/client_info" else {"result": "GetHeadersError", "message": "boom"})
    with pytest.raises(UserError) as e:
        p.base()
    assert e.value.data["reason"] == "anisette"


def test_linux_without_an_anisette_server_says_how_to_run_one(monkeypatch):
    from droidctl.ios.apple import anisette
    monkeypatch.setattr(anisette.sys, "platform", "linux")
    monkeypatch.delenv("DROIDCTL_ANISETTE", raising=False)
    with pytest.raises(UserError) as e:
        anisette.default_provider()
    assert anisette.PUBLIC in e.value.hint and "--anisette" in e.value.hint
    monkeypatch.setenv("DROIDCTL_ANISETTE", "http://localhost:6969")
    assert isinstance(anisette.default_provider(), anisette.V3Anisette)


def test_linux_offers_the_public_anisette_server_by_default():
    from droidctl.ios.apple import anisette
    said = []
    assert anisette.choose_server(lambda q: "", said.append) == anisette.PUBLIC
    assert "never your Apple ID, password or token" in said[0]
    assert anisette.choose_server(lambda q: "http://box:6969", said.append) == "http://box:6969"
    answers = iter(["n", ""])
    assert anisette.choose_server(lambda q: next(answers), said.append) == "http://localhost:6969"


def test_usbmuxd_is_installed_and_started_with_the_users_sudo():
    from droidctl.ios import setup
    apt = lambda name: "/usr/bin/apt-get" if name == "apt-get" else None
    assert setup.usbmuxd_commands(apt) == [["sudo", "apt-get", "install", "-y", "usbmuxd"],
                                          ["sudo"] + setup.UDEV_TRIGGER]
    assert setup.usbmuxd_commands(apt, installed=True) == [["sudo"] + setup.UDEV_TRIGGER]   # only start it
    assert setup.usbmuxd_commands(apt, root=True)[0][0] == "apt-get"
    assert setup.usbmuxd_commands(lambda name: None) == []       # unknown distro: instructions only


def test_setup_offers_usbmuxd_once_and_runs_nothing_without_a_yes(monkeypatch):
    from droidctl.ios import setup
    monkeypatch.setenv("DROIDCTL_IOS_BACKEND", "goios")
    monkeypatch.setattr(setup.sys if hasattr(setup, "sys") else __import__("sys"), "platform", "linux")
    monkeypatch.setattr(setup, "_usbmux_ok", lambda: False)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/apt-get" if name == "apt-get" else None)
    ran, asked = [], []
    monkeypatch.setattr(setup.subprocess, "run", lambda c, **k: ran.append(c) or type("P", (), {"returncode": 0})())
    ctx = setup.Ctx(UDID, None, lambda line: None, lambda q: asked.append(q) or "n")
    for _ in range(3):                                             # the step is retried while waiting
        with pytest.raises(setup.Need):
            setup.step_tools(ctx)
    assert len(asked) == 1 and ran == []


def test_the_retired_public_anisette_server_is_replaced():
    """Apple refuses ani.sidestore.io's data (-80045) since 2026-10-12; a saved sign-in may name it."""
    from droidctl.ios.apple import anisette
    assert anisette.current("https://ani.sidestore.io/") == anisette.PUBLIC
    assert anisette.current("http://localhost:6969") == "http://localhost:6969"
    assert anisette.current(None) is None
    assert anisette.V3Anisette("https://ani.sidestore.io").url == anisette.PUBLIC


def test_a_new_anisette_server_gets_a_new_machine_identity(monkeypatch):
    from droidctl.ios.apple import anisette
    state = {"server": "https://old.example", "uid": "1B4E28BA-2FA1-11D2-883F-0016D3CCA427", "adi_pb": "old"}
    p = anisette.V3Anisette("http://x", store=(lambda: dict(state), lambda st: state.update(st) or state.clear() or state.update(st)))
    monkeypatch.setattr(p, "provision", lambda uid: "fresh-adi")
    monkeypatch.setattr(p, "_http", lambda path, body=None: {"client_info": "ci", "user_agent": "ua"} if body is None else
                        {"result": "Headers", "X-Apple-I-MD": "md", "X-Apple-I-MD-M": "m"})
    p._fetch()
    assert state["server"] == "http://x" and state["adi_pb"] == "fresh-adi"
    assert state["uid"] != "1B4E28BA-2FA1-11D2-883F-0016D3CCA427"


def test_grandslam_uses_the_anisette_servers_user_agent_and_types_a_rejection(monkeypatch):
    from droidctl.ios.apple import anisette, gsa
    p = anisette.V3Anisette("http://x")
    p._info = {"client_info": "<Mac> <macOS> <com.apple.AuthKit/1 (com.apple.akd/1.0)>", "user_agent": "akd/1.0 X"}
    assert anisette.user_agent(p, gsa.GSA_AGENT) == "akd/1.0 X"
    assert anisette.user_agent(object(), gsa.GSA_AGENT) == gsa.GSA_AGENT
    with pytest.raises(UserError) as e:
        gsa._check({"Status": {"ec": -80045, "em": "This action cannot be completed at this time."}})
    assert e.value.data["reason"] == "anisette-rejected" and anisette.ALTERNATE in e.value.hint


def test_revoking_an_apple_development_cert_falls_back_to_the_v1_api(monkeypatch):
    """Xcode's unified certificates aren't in the classic action (7252): delete by id in v1."""
    from droidctl.ios.apple import devservices
    ds = devservices.Session("adsid", "tok", None)
    calls = []

    def call(action, platform="ios", **body):
        calls.append(("action", action, body["serialNumber"]))
        raise devservices.DevError("no 'ios' certificate", "signing", data={"code": devservices.NO_SUCH_CERT})
    monkeypatch.setattr(ds, "call", call)
    monkeypatch.setattr(ds, "v1", lambda path, body, method: calls.append(("v1", path, method, body)) or (204, b""))
    ds.revoke_cert("TEAM", {"id": "ABC123", "serial": "7F14"})
    assert calls == [("action", "revokeDevelopmentCert", "7F14"),
                     ("v1", "certificates/ABC123", "DELETE", {"teamId": "TEAM"})]
    monkeypatch.setattr(ds, "v1", lambda path, body, method: (403, b""))
    with pytest.raises(UserError) as e:
        ds.revoke_cert("TEAM", {"id": "ABC123", "serial": "7F14"})
    assert e.value.data["reason"] == "revoke-failed"
