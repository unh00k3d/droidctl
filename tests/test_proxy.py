"""`droidctl proxy`: proxy-app driven over `am broadcast`, through the real CLI, the real
adbutils and a fake adb SERVER (FakeAdb + FakeProxyApp), with every way of reaching an
`adb` binary or a subprocess booby-trapped (as in test_no_adb_binary)."""
import json
import shlex
import shutil
import subprocess

import pytest

from droidctl import act, cli, proxy
from droidctl import device as dev
from droidctl.core import UserError
from tests.fakes import FakeAdb, FakeAgent, FakeProxyApp, broadcast_reply

SERIAL = "PROXY1"
USER = {"id": "u-1", "name": "Work", "type": "socks5", "host": "10.0.0.2", "port": 1080, "mode": "all"}
USER2 = {"id": "u-2", "name": "Home", "type": "http", "host": "10.0.0.3", "port": 3128, "mode": "all"}
PREFIX = ("am broadcast -a com.enesb.proxyapp.CMD -n "
          "com.proxyapp.mobile/com.enesb.proxyapp.adb.AdbReceiver --es cmd ")


@pytest.fixture
def phone(tmp_path, monkeypatch):
    agent = FakeAgent()
    app = FakeProxyApp(profiles=[USER, USER2], selected_id="u-1", active_id="u-1")
    adb = FakeAdb({SERIAL: agent}, shell=app)
    monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", str(adb.port))
    monkeypatch.setenv("DROIDCTL_HOME", str(tmp_path))
    monkeypatch.setenv("ANDROID_SERIAL", SERIAL)
    monkeypatch.setenv("DROIDCTL_BACKEND", "a11y")
    monkeypatch.delenv(proxy.URL_ENV, raising=False)
    monkeypatch.setattr(proxy, "POLL_S", 0.01)

    def trap(name):
        def f(*a, **kw):
            raise AssertionError(f"{name} called (an adb binary?): {a!r}")
        return f
    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, trap(f"subprocess.{name}"))
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda cmd, *a, **kw: trap("shutil.which(adb)")(cmd)
                        if "adb" in str(cmd) else real_which(cmd, *a, **kw))
    for name in ("adb_path", "_bundled_adb", "start_adb_server"):
        monkeypatch.setattr(dev, name, trap(f"device.{name}"))
    act.close_sessions()
    yield adb, app
    act.close_sessions()
    adb.close()
    agent.close()


def run(capsys, *argv, ok=True):
    capsys.readouterr()
    try:
        cli.main([*argv, "--json"])
    except SystemExit as e:
        if ok:
            assert not e.code, capsys.readouterr()
    return json.loads(capsys.readouterr().out)


# --- the reply and the line (pure) ------------------------------------------------
def test_reply_json_is_everything_between_data_and_the_last_quote():
    """proxy-app does not escape the data: quotes inside the JSON stay as they are."""
    data = {"error": 'can\'t parse proxy "x"', "consent": "requested"}
    out = broadcast_reply(1, data)
    assert 'data="{"error":"' in out                    # the JSON's own quotes, not re-escaped
    assert proxy.parse_reply(out) == (1, data)
    assert proxy.parse_reply(broadcast_reply(0, [{"target": 'a "b"'}])) == (0, [{"target": 'a "b"'}])


@pytest.mark.parametrize("out", ["Broadcasting: Intent { }\nBroadcast completed: result=0\n", "", "Error: boom\n"])
def test_no_data_means_no_receiver_ran(out):
    with pytest.raises(UserError) as e:
        proxy.parse_reply(out)
    assert e.value.kind == "proxy" and e.value.data["reason"] == "no-receiver"


def test_the_url_is_single_quoted_and_flags_are_bare():
    line = proxy.broadcast_line("add", ("--es", "url", "http://phone:pt@127.0.0.1:8080"),
                                ("--es", "name", "it's"), ("--ez", "select", True), ("--es", "limit", 5))
    assert line == (PREFIX + "add --es url 'http://phone:pt@127.0.0.1:8080' "
                    "--es name 'it'\"'\"'s' --ez select true --es limit '5'")
    # the device shell sees each value as one word, unquoted
    assert shlex.split(line)[-9:] == ["--es", "name", "it's", "--ez", "select", "true", "--es", "limit", "5"]


@pytest.mark.parametrize("url", ["http://u:p+q@h:1", "http://u:p%41@h:1", "http://u:p@ss@h:1", "ftp://h:1",
                                 "http://h", "nonsense"])
def test_bad_urls_are_refused(url):
    with pytest.raises(UserError) as e:
        proxy.check_url(url)
    assert e.value.kind == "bad-args"


def test_mask_hides_the_password_everywhere():
    assert proxy.mask("http://phone:pt@127.0.0.1:8080") == "http://phone:***@127.0.0.1:8080"
    assert proxy.mask('can\'t parse proxy "socks5://a:secret9@h:1"') == 'can\'t parse proxy "socks5://a:***@h:1"'
    assert proxy.mask("http://127.0.0.1:8080") == "http://127.0.0.1:8080"


# --- setup: install only when missing or older -----------------------------------
def test_setup_is_a_noop_on_the_same_version(phone, capsys):
    adb, app = phone
    r = run(capsys, "proxy", "setup")
    assert r["installed"] is False and r["version"] == 1 and app.installs == 0
    assert not adb.pushed


def test_setup_never_downgrades(phone, capsys):
    _, app = phone
    app.installed = 7
    r = run(capsys, "proxy", "setup")
    assert r["installed"] is False and r["version"] == 7 and app.installs == 0


@pytest.mark.parametrize("have", [None, 0])
def test_setup_installs_when_missing_or_older(phone, capsys, monkeypatch, tmp_path, have):
    adb, app = phone
    apk = tmp_path / "p.apk"
    apk.write_bytes(b"PK\x03\x04proxy-app")
    import hashlib
    man = tmp_path / "p.json"
    man.write_text(json.dumps({"package": proxy.PROXY_PKG, "versionCode": 1,
                               "sha256": hashlib.sha256(apk.read_bytes()).hexdigest()}))
    monkeypatch.setattr(proxy, "APK_PATH", str(apk))
    monkeypatch.setattr(proxy, "MANIFEST_PATH", str(man))
    app.installed = have
    r = run(capsys, "proxy", "setup")
    assert r["installed"] is True and r["previous_version"] == have and r["version"] == 1
    (remote, data), = adb.pushed.items()
    assert data == apk.read_bytes()
    assert f"pm install -r {remote}" in adb.shell_lines        # push + pm install -r, like `droidctl install`


def test_setup_refuses_an_apk_that_does_not_match_its_manifest(phone, capsys, monkeypatch, tmp_path):
    _, app = phone
    apk = tmp_path / "p.apk"
    apk.write_bytes(b"tampered")
    monkeypatch.setattr(proxy, "APK_PATH", str(apk))
    app.installed = None
    r = run(capsys, "proxy", "setup", ok=False)
    assert r["error"]["kind"] == "missing-dep" and "sha256" in r["error"]["message"] and app.installs == 0


def test_the_bundled_manifest_matches_the_bundled_apk():
    man = proxy.bundled()
    assert man["package"] == "com.proxyapp.mobile" and man["versionCode"] >= 1
    proxy._verify_apk(man)


# --- on / off: the user's own state is put back ------------------------------------
def test_on_routes_the_app_and_off_restores_the_users_connected_profile(phone, capsys):
    adb, app = phone
    before = app.snapshot()
    r = run(capsys, "proxy", "on", "--app", "com.android.chrome")
    assert r["state"] == "connected" and r["profile"] == "droidctl-pt" and r["app"] == "com.android.chrome"
    assert r["to"] == "http://phone:***@127.0.0.1:8080" and r["dns"] == "direct"
    assert r["previous"] == {"state": "connected", "profile": "Work"}
    assert "pt@" not in json.dumps(r)                       # the password never shows
    add = next(l for l in app.broadcasts if "--es cmd add" in l)
    assert add == (PREFIX + "add --es name 'droidctl-pt' --es url 'http://phone:pt@127.0.0.1:8080' "
                   "--es apps 'com.android.chrome' --es mode 'only' --es dns 'direct' "
                   "--ez blockquic true --ez select true")
    ours = next(p for p in app.profiles if p["name"] == "droidctl-pt")
    assert app.active_id == ours["id"] and ours["packages"] == ["com.android.chrome"]
    assert app.profiles[:2] == [USER, USER2]                # the user's profiles untouched
    assert dev.device_state(SERIAL)["proxy_restore"]["profile_id"] == "u-1"

    r = run(capsys, "proxy", "off")
    assert r["restored"] == {"state": "connected", "profile": "Work"}
    assert r["state"] == "connected" and r["profile"] == "Work" and r["removed"]
    assert app.snapshot() == before
    assert "proxy_restore" not in dev.device_state(SERIAL)


def test_off_restores_an_unselected_state_without_connecting_anything(phone, capsys):
    """No explicit selection (proxy-app then shows its first profile as selected): removing
    ours clears the selection again; nothing of the user's is connected."""
    _, app = phone
    app.selected_id, app.active_id = None, None
    before = app.snapshot()
    run(capsys, "proxy", "on", "--app", "com.android.chrome")
    app.connects.clear()
    r = run(capsys, "proxy", "off")
    assert r["state"] == "off" and app.connects == [] and "warning" not in r
    assert app.snapshot() == before and app.selected_id is None


def test_off_reselects_a_profile_that_was_selected_but_off(phone, capsys):
    _, app = phone
    app.selected_id, app.active_id = "u-2", None         # Home selected, not the first profile
    before = app.snapshot()
    run(capsys, "proxy", "on", "--app", "com.android.chrome")
    r = run(capsys, "proxy", "off")
    assert app.snapshot() == before and r["state"] == "off" and r["profile"] == "Home"
    assert "connected it for a moment" in r["warning"]     # proxy-app has no select-only command


def test_a_second_on_keeps_the_first_record(phone, capsys):
    _, app = phone
    before = app.snapshot()
    run(capsys, "proxy", "on", "--app", "com.android.chrome")
    run(capsys, "proxy", "on", "--app", "com.android.chrome", "--to", "socks5://127.0.0.1:1080", "--dns", "proxy")
    assert dev.device_state(SERIAL)["proxy_restore"]["profile"] == "Work"
    assert [p["name"] for p in app.profiles].count("droidctl-pt") == 1
    run(capsys, "proxy", "off")
    assert app.snapshot() == before


def test_off_leaves_a_vpn_the_user_switched_to_alone(phone, capsys):
    _, app = phone
    run(capsys, "proxy", "on", "--app", "com.android.chrome")
    app.active_id = app.selected_id = "u-2"                # the user connected Home meanwhile
    r = run(capsys, "proxy", "off")
    assert r["disconnected"] is False and app.active_id == "u-2" and "left as is" in r["warning"]
    assert "droidctl-pt" not in [p["name"] for p in app.profiles]


def test_off_without_on_changes_nothing(phone, capsys):
    adb, app = phone
    before = app.snapshot()
    r = run(capsys, "proxy", "off")
    assert r["changed"] is False and app.snapshot() == before
    assert not [l for l in app.broadcasts if "disconnect" in l or "remove" in l]


def test_on_uses_the_env_url(phone, capsys, monkeypatch):
    _, app = phone
    monkeypatch.setenv(proxy.URL_ENV, "socks5://127.0.0.1:1090")
    r = run(capsys, "proxy", "on", "--app", "com.android.chrome", "--dns", "auto")
    assert r["to"] == "socks5://127.0.0.1:1090"
    assert "--es url 'socks5://127.0.0.1:1090'" in next(l for l in app.broadcasts if "cmd add" in l)


def test_on_needs_an_installed_app(phone, capsys):
    r = run(capsys, "proxy", "on", "--app", "com.not.there", ok=False)
    assert r["error"]["kind"] == "not-found"
    assert run(capsys, "proxy", "on", ok=False)["error"]["kind"] == "bad-args"


def test_connect_timeout_is_typed_and_keeps_the_record(phone, capsys, monkeypatch):
    _, app = phone
    app.connect_state = "connecting"
    r = run(capsys, "proxy", "on", "--app", "com.android.chrome", "--timeout", "0.05", ok=False)
    assert r["error"]["kind"] == "timeout" and "proxy off" in r["error"]["hint"]
    assert dev.device_state(SERIAL)["proxy_restore"]["profile"] == "Work"


def test_connect_error_fails_fast_with_kind_proxy(phone, capsys):
    _, app = phone
    app.connect_state = "error"
    r = run(capsys, "proxy", "on", "--app", "com.android.chrome", ok=False)
    assert r["error"]["kind"] == "proxy" and r["error"]["data"]["reason"] == "rejected"


def test_not_installed_receiver_is_kind_proxy(phone, capsys):
    _, app = phone
    app.installed = None
    r = run(capsys, "proxy", "status", ok=False)
    assert r["error"]["kind"] == "proxy" and r["error"]["data"]["reason"] == "no-receiver"
    r = run(capsys, "proxy", "on", "--app", "com.android.chrome", "--no-auto-setup", ok=False)
    assert r["error"]["kind"] == "not-installed"


# --- consent ----------------------------------------------------------------------
def test_consent_dialog_is_accepted_then_status_polled(phone, capsys, monkeypatch):
    _, app = phone
    app.consent = True
    taps = []

    def accept(a, timeout=10.0):
        taps.append(a.device)
        app.accept()
        return {"tapped": "OK", "ref": 4, "method": "action"}
    monkeypatch.setattr(proxy, "accept_consent", accept)
    r = run(capsys, "proxy", "on", "--app", "com.android.chrome")
    assert taps == [SERIAL] and r["state"] == "connected" and r["consent"]["tapped"] == "OK"


def test_accept_consent_taps_the_dialogs_positive_button(phone, monkeypatch):
    """The real helper, on a real captured AlertDialog (its package stands in for the VPN dialog's)."""
    import argparse
    adb, _ = phone
    agent = adb.agents[SERIAL]
    with open(f"{__file__.rsplit('/', 1)[0]}/fixtures/trees/testapp-dialogs-alert.json") as f:
        agent.base = json.load(f)["tree"]
    monkeypatch.setattr(proxy, "CONSENT_PKGS", ("dev.droidctl.testapp",))
    r = proxy.accept_consent(argparse.Namespace(device=SERIAL, no_auto_setup=True))
    assert r["tapped"] == "DELETE" and r["ref"] == 4          # android:id/button1
    assert agent.calls["act"] == 1


def test_accept_consent_never_taps_another_apps_dialog(phone, monkeypatch):
    import argparse
    adb, _ = phone
    agent = adb.agents[SERIAL]
    with open(f"{__file__.rsplit('/', 1)[0]}/fixtures/trees/testapp-dialogs-alert.json") as f:
        agent.base = json.load(f)["tree"]
    with pytest.raises(UserError) as e:
        proxy.accept_consent(argparse.Namespace(device=SERIAL, no_auto_setup=True), timeout=0.05)
    assert e.value.kind == "proxy" and e.value.data["reason"] == "consent" and agent.calls["act"] == 0


# --- status / log pass proxy-app's JSON through ---------------------------------------
def test_status_and_log_pass_through(phone, capsys):
    _, app = phone
    r = run(capsys, "proxy", "status")
    assert r["status"] == app.status() and r["state"] == "connected"
    r = run(capsys, "proxy", "log", "--limit", "5", "--failed")
    assert r["entries"] == [] and r["count"] == 0
    assert app.broadcasts[-1] == PREFIX + "log --es limit '5' --ez failed true"
    assert run(capsys, "proxy", "log")["entries"] == app.log


def _older_installed(app, monkeypatch, tmp_path):
    """The phone has versionCode 0; the bundled (fake) APK is 1."""
    import hashlib
    apk = tmp_path / "p.apk"
    apk.write_bytes(b"PK\x03\x04proxy-app")
    man = tmp_path / "p.json"
    man.write_text(json.dumps({"package": proxy.PROXY_PKG, "versionCode": 1,
                               "sha256": hashlib.sha256(apk.read_bytes()).hexdigest()}))
    monkeypatch.setattr(proxy, "APK_PATH", str(apk))
    monkeypatch.setattr(proxy, "MANIFEST_PATH", str(man))
    app.installed = 0


def test_an_upgrade_during_on_still_restores_the_users_connected_profile(phone, capsys, monkeypatch, tmp_path):
    """pm install -r stops the user's running VPN: the record is read before it, so off reconnects."""
    _, app = phone
    _older_installed(app, monkeypatch, tmp_path)
    before = app.snapshot()
    r = run(capsys, "proxy", "on", "--app", "com.android.chrome")
    assert r["setup"]["installed"] and r["previous"] == {"state": "connected", "profile": "Work"}
    run(capsys, "proxy", "off")
    assert app.snapshot() == before


def test_setup_reconnects_a_profile_its_upgrade_stopped(phone, capsys, monkeypatch, tmp_path):
    _, app = phone
    _older_installed(app, monkeypatch, tmp_path)
    before = app.snapshot()
    r = run(capsys, "proxy", "setup")
    assert r["installed"] and r["reconnected"] == "Work" and app.snapshot() == before


def test_a_tunnel_that_fails_after_connect_fails_fast(phone, capsys):
    _, app = phone
    app.connect_state = "error-later"
    r = run(capsys, "proxy", "on", "--app", "com.android.chrome", "--timeout", "30", ok=False)
    assert r["error"]["kind"] == "proxy" and r["error"]["data"]["reason"] == "connect-failed"
    assert r["elapsed_ms"] < 5000
