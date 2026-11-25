"""No `adb` executable: every device command speaks only the adb server's protocol.

pt runs droidctl in a container whose adb server is a relay to the host's, with no
`adb` binary on purpose (a client of another version sends `host:kill`). Each
command here runs through the real CLI, the real adbutils and a fake adb SERVER
(tests/fakes.FakeAdb), with every way of finding or spawning a binary booby-trapped.
"""
import io
import json
import shlex
import shutil
import subprocess

import pytest

from droidctl import act, cli
from droidctl import device as dev
from tests.fakes import FakeAdb, FakeAgent, _RpcError

SERIAL = "NOADB1"


class Agent(FakeAgent):
    """FakeAgent on API 28: no device screenshot (the screencap path), and the
    launched app comes to the front at once."""

    def _call(self, method, p, f):
        if method == "screenshot":
            self.calls[method] += 1
            raise _RpcError(-32006, "unsupported")
        if method == "wait_for":
            self.calls[method] += 1
            return {"ms": 1, "found": True}
        return super()._call(method, p, f)


def _png():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (108, 222), (40, 90, 160)).save(buf, "PNG")
    return buf.getvalue()


PNG = _png()
LOGCAT = (b"--------- beginning of main\n"
          b"11-25 10:00:00.000  100  100 I droidctl-mark: step one\n"
          b"11-25 10:00:01.000  200  200 W Other: noise\n")


def device_shell(line):
    """What the phone's shell prints for each command line droidctl sends."""
    words = line.split()
    if line.startswith("pm path "):
        return b"package:/system/priv-app/SecSettings/SecSettings.apk\n", b"", 0
    if line.startswith("cmd package resolve-activity"):
        return b"priority=0 preferredOrder=0\ncom.android.settings/.Settings\n", b"", 0
    if line.startswith("am start"):
        return b"Starting: Intent { ... }\n", b"", 0
    if line.startswith("pm list packages"):
        extra = b"" if "-3" in words else b"package:android\n"
        return b"package:com.b.app\npackage:com.a.app\n" + extra, b"", 0
    if line.startswith("pm install"):
        return b"Success\n", b"", 0
    if line.startswith("screencap"):
        return PNG, b"", 0
    if "exec logcat" in line:
        return LOGCAT, b"", 0
    if words[0] in ("input", "log", "am", "rm", "dumpsys", "pidof"):
        return b"", b"", 0
    raise AssertionError(f"unexpected device command: {line}")


@pytest.fixture
def phone(tmp_path, monkeypatch):
    """A fake adb server + agent, and no way to reach an adb binary."""
    agent = Agent()
    adb = FakeAdb({SERIAL: agent}, shell=device_shell)
    monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", str(adb.port))
    monkeypatch.setenv("DROIDCTL_HOME", str(tmp_path))
    monkeypatch.setenv("ANDROID_SERIAL", SERIAL)
    monkeypatch.setenv("DROIDCTL_BACKEND", "a11y")

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
    yield adb, agent
    act.close_sessions()
    adb.close()
    agent.close()


def run(capsys, *argv):
    capsys.readouterr()
    try:
        cli.main([*argv, "--json", "--no-auto-setup"])
    except SystemExit as e:
        assert not e.code, capsys.readouterr()
    return json.loads(capsys.readouterr().out)


def test_apps(phone, capsys):
    adb, _ = phone
    r = run(capsys, "apps")
    assert r["ok"] and r["apps"] == ["com.a.app", "com.b.app"] and r["count"] == 2
    assert adb.shell_lines == ["pm list packages -3"]
    assert run(capsys, "apps", "--all")["count"] == 3


def test_launch(phone, capsys):
    adb, agent = phone
    r = run(capsys, "launch", "com.android.settings")
    assert r["ok"] and r["pkg"] == "com.android.settings"
    assert "am start -n com.android.settings/.Settings" in adb.shell_lines
    assert agent.calls["wait_for"] == 1


def test_open_url_quotes_the_url_once(phone, capsys):
    adb, _ = phone
    url = "https://example.com/a?b=1&c=2"
    assert run(capsys, "open-url", url)["ok"]
    # the shell must see the URL as one word: quoted once, as `adb shell` sent it
    assert adb.shell_lines[-1] == f"am start -a android.intent.action.VIEW -d {shlex.quote(url)}"


def test_stop_app(phone, capsys):
    adb, _ = phone
    assert run(capsys, "stop-app", "com.android.settings")["text"] == "stopped com.android.settings"
    assert adb.shell_lines == ["am force-stop com.android.settings"]


def test_press(phone, capsys):
    adb, _ = phone
    assert run(capsys, "press", "home")["ok"]
    assert adb.shell_lines[-1].startswith("input keyevent ")


def test_mark_label_reaches_the_shell_as_one_word(phone, capsys):
    adb, _ = phone
    r = run(capsys, "mark", "step 3; login 'ok'")
    assert r["label"] == "step 3; login 'ok'"
    assert adb.shell_lines == ["log -t droidctl-mark " + shlex.quote("step 3; login 'ok'")]


def test_logs_marks(phone, capsys):
    adb, _ = phone
    r = run(capsys, "logs", "--marks")
    assert r["lines"] == ["11-25 10:00:00.000  100  100 I droidctl-mark: step one",
                          "11-25 10:00:01.000  200  200 W Other: noise"]
    # what `adb logcat` sends: its own args quoted (`*:S` is not globbed)
    assert adb.shell_lines == ['export ANDROID_LOG_TAGS=""; exec logcat -d -v threadtime '
                               "droidctl-mark:V '*:S'"]


def test_logs_pkg_reads_the_crash_buffer(phone, capsys):
    adb, _ = phone
    r = run(capsys, "logs", "--pkg", "com.x")
    assert r["pid"] is None and r["crash"] == []
    assert adb.shell_lines[0] == "pidof com.x"
    assert "exec logcat -d -b crash -v threadtime" in adb.shell_lines[1]


def test_shot_uses_screencap_over_the_protocol(phone, capsys, tmp_path):
    adb, agent = phone
    out = tmp_path / "s.jpg"
    r = run(capsys, "shot", str(out), "--scale", "1")
    assert r["ok"] and (r["w"], r["h"]) == (108, 222)
    assert out.read_bytes()[:3] == b"\xff\xd8\xff"            # a JPEG
    assert agent.calls["screenshot"] == 1
    assert adb.shell_lines == ["dumpsys window windows", "screencap -p"]


def test_shot_refuses_output_that_is_not_a_png(phone, capsys, monkeypatch):
    adb, _ = phone
    adb.shell = lambda line: (b"", b"", 0) if line.startswith("dumpsys") else (b"garbage", b"", 0)
    capsys.readouterr()
    with pytest.raises(SystemExit):
        cli.main(["shot", "--json", "--no-auto-setup"])
    err = json.loads(capsys.readouterr().out)["error"]
    assert err["kind"] == "adb" and "screencap failed" in err["message"]


def test_install_pushes_then_pm_installs(phone, capsys, tmp_path):
    adb, _ = phone
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"PK\x03\x04fake apk")
    r = run(capsys, "install", str(apk), "--grant")
    assert r["ok"] and r["text"] == "installed app.apk"
    (remote, data), = adb.pushed.items()
    assert data == apk.read_bytes() and remote.startswith("/data/local/tmp/")
    assert adb.shell_lines == [f"pm install -r -g {remote}", f"rm -f {remote}"]


def test_install_failure_keeps_its_error_kind(phone, capsys, tmp_path):
    adb, _ = phone
    adb.shell = lambda line: ((b"", b"Failure [INSTALL_FAILED_OLDER_SDK]\n", 1)
                              if line.startswith("pm install") else (b"", b"", 0))
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"PK")
    capsys.readouterr()
    with pytest.raises(SystemExit):
        cli.main(["install", str(apk), "--json", "--no-auto-setup"])
    err = json.loads(capsys.readouterr().out)["error"]
    assert err["kind"] == "adb" and "INSTALL_FAILED_OLDER_SDK" in err["message"]
    assert adb.shell_lines[-1].startswith("rm -f /data/local/tmp/")      # the copy is removed


def test_failed_shell_command_is_kind_adb(phone):
    adb, _ = phone
    adb.shell = lambda line: (b"", b"Error: boom\n", 1)
    sess = act.get_session(SERIAL, auto_setup=False)
    with pytest.raises(act.UserError) as e:
        sess.shell("input", "keyevent", "3")
    assert e.value.kind == "adb" and e.value.args[0] == "adb shell input keyevent failed: Error: boom"


def test_a_running_server_never_starts_or_looks_for_a_binary(phone):
    """start_adb_server (trapped) is the one binary use: only when nothing answers."""
    assert dev.list_devices() == [(SERIAL, "device")]


def test_no_server_and_no_binary_degrades_to_a_typed_error(monkeypatch):
    """Nothing on the port and no binary: one failed start attempt, then `adb`."""
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]                     # nothing listens here
    monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", str(port))
    tried = []

    def no_binary(argv, **kw):                        # never start a real server from a test
        tried.append(argv)
        raise FileNotFoundError(2, "No such file or directory", argv[0])
    monkeypatch.setattr(subprocess, "run", no_binary)
    with pytest.raises(dev.UserError) as e:
        dev.list_devices()
    assert e.value.kind == "adb" and "cannot reach the adb server" in str(e.value)
    assert len(tried) == 1 and tried[0][1:] == ["start-server"]
