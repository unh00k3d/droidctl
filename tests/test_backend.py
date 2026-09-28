"""Device backends without a phone: selection, backend B's launch line, log parsing and
the relaunch policy (PLAN.md "Device backends")."""
import pytest

from droidctl import device as dev
from droidctl.core import UserError


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DROIDCTL_HOME", str(tmp_path))
    monkeypatch.delenv("DROIDCTL_BACKEND", raising=False)
    return tmp_path


# --- selection: explicit, per phone, never guessed --------------------------
def test_default_backend_is_the_a11y_service(home):
    assert dev.backend_for("X") == "a11y"


def test_setup_choice_is_remembered_per_phone(home):
    dev.update_device_state("X", backend="uiautomation")
    assert dev.backend_for("X") == "uiautomation"
    assert dev.backend_for("Y") == "a11y"


def test_env_overrides_the_recorded_choice(home, monkeypatch):
    dev.update_device_state("X", backend="uiautomation")
    monkeypatch.setenv("DROIDCTL_BACKEND", "a11y")
    assert dev.backend_for("X") == "a11y"
    monkeypatch.setenv("DROIDCTL_BACKEND", "appium")
    with pytest.raises(UserError) as e:
        dev.backend_for("X")
    assert e.value.kind == "bad-args"


# --- forwards: each backend has its own abstract socket --------------------
FORWARDS = ("S tcp:40001 localabstract:droidctl\n"
            "S tcp:40002 localabstract:droidctl_ua\n"
            "T tcp:40003 localabstract:droidctl_ua\n")


def test_forwards_are_found_per_backend():
    assert dev.find_forward("S", FORWARDS) == 40001
    assert dev.find_forward("S", FORWARDS, remote=dev.REMOTES["uiautomation"]) == 40002
    assert dev.find_forward("T", FORWARDS) is None
    assert dev.REMOTES["a11y"] == dev.REMOTE and len(set(dev.REMOTES.values())) == 2


# --- launching backend B -----------------------------------------------------
def test_launch_line_runs_the_pushed_apk_detached():
    line = dev.ua_command(1234)
    assert line.startswith(f"CLASSPATH={dev.UA_APK} setsid sh -c 'app_process /system/bin {dev.UA_MAIN} ")
    assert "--idle-ms 1234" in line
    assert line.endswith(f"</dev/null >{dev.UA_LOG} 2>&1 &'")       # backgrounded inside the new session
    # adbutils appends `; echo X4EXIT:$?`: `&;` would be a shell syntax error
    import subprocess
    assert subprocess.run(["sh", "-n", "-c", line + "; echo X4EXIT:$?"]).returncode == 0
    assert "su " not in line and "pm install" not in line and "settings" not in line


@pytest.mark.parametrize("log,state", [
    ("", None),
    ("I ready pid=4242 sdk=28 idle_ms=600000 suppress=false\n", "ready"),
    ("E busy: another UiAutomation client (uiautomator/Appium) is connected: UiAutomationService x already registered!",
     "busy"),
    ("E socket: @droidctl_ua is taken (another instance?)", "socket"),
    ("E failed: UiAutomation connect failed: java.lang.SecurityException", "failed"),
    ("WARNING: linker: something\nI ready pid=1\n", "ready"),
])
def test_status_line_parsing(log, state):
    assert dev.parse_ua_log(log)[0] == state


def _fake_link(monkeypatch, pings, launch_error=None):
    """_connect_remote answers from `pings` (None = nothing listening); records launches/stops."""
    calls = {"launch": 0, "stop": 0}
    seq = list(pings)

    class C:
        port = 1

        def close(self):
            pass

    def connect_remote(serial, remote, timeout):
        assert remote == dev.REMOTES["uiautomation"]
        p = seq.pop(0)
        return None if p is None else (C(), p)

    def launch(serial, *a, **k):
        calls["launch"] += 1
        if launch_error:
            raise launch_error

    def stop(serial, remove=True):
        calls["stop"] += 1
        assert remove is False          # a relaunch keeps the pushed files
        return {"stopped": True, "removed": False}

    monkeypatch.setattr(dev, "_connect_remote", connect_remote)
    monkeypatch.setattr(dev, "launch_ua", launch)
    monkeypatch.setattr(dev, "stop_ua", stop)
    return calls


def test_a_running_current_agent_is_reused(monkeypatch):
    calls = _fake_link(monkeypatch, [{"versionCode": dev.AGENT_VERSION_CODE, "backend": "uiautomation"}])
    _, info = dev._connect_ua("S", 1.0)
    assert info["backend"] == "uiautomation" and calls == {"launch": 0, "stop": 0}


def test_a_dead_agent_is_relaunched(monkeypatch):
    """Hard part 1: backend B dies on reboot, when killed and after its idle timeout."""
    calls = _fake_link(monkeypatch, [None, {"versionCode": dev.AGENT_VERSION_CODE}])
    dev._connect_ua("S", 1.0)
    assert calls == {"launch": 1, "stop": 0}


def test_an_older_agent_is_replaced(monkeypatch):
    calls = _fake_link(monkeypatch, [{"versionCode": dev.AGENT_VERSION_CODE - 1}, {"versionCode": dev.AGENT_VERSION_CODE}])
    dev._connect_ua("S", 1.0)
    assert calls == {"launch": 1, "stop": 1}


def test_launched_but_silent_is_a_connection_error(monkeypatch):
    _fake_link(monkeypatch, [None, None])
    with pytest.raises(UserError) as e:
        dev._connect_ua("S", 1.0)
    assert e.value.kind == "connection" and dev.UA_LOG in e.value.hint


def test_another_uiautomation_holder_is_reported_not_fought(monkeypatch):
    busy = UserError("another UiAutomation client holds the device", "suppressed")
    calls = _fake_link(monkeypatch, [None], launch_error=busy)
    with pytest.raises(UserError) as e:
        dev._connect_ua("S", 1.0)
    assert e.value.kind == "suppressed" and calls["launch"] == 1


def test_connect_dispatches_on_the_backend(home, monkeypatch):
    monkeypatch.setattr(dev, "_connect_ua", lambda serial, timeout: ("ua", {}))
    monkeypatch.setattr(dev, "_connect_remote", lambda serial, remote, timeout: ("a11y", {}))
    assert dev._connect("S")[0] == "a11y"
    monkeypatch.setenv("DROIDCTL_BACKEND", "uiautomation")
    assert dev._connect("S")[0] == "ua"


def test_daemon_session_reopens_when_the_backend_changes(home, monkeypatch):
    from droidctl import daemon as d
    opened = []

    def fake_open(self):
        self.backend, self.alive = dev.backend_for(self.serial), True
        opened.append(self.backend)
    monkeypatch.setattr(d.Session, "open", fake_open)
    pool = d.Pool()
    pool.get("S")
    pool.get("S")
    monkeypatch.setenv("DROIDCTL_BACKEND", "uiautomation")
    pool.get("S")
    assert opened == ["a11y", "uiautomation"]


def test_process_pattern_cannot_match_the_shell_that_runs_it():
    """pgrep -f <name> matched its own `sh -c` line, so a stopped agent looked alive
    and pkill -f killed the adb shell itself (found on the SM-N950F)."""
    import re
    pat = dev.UA_PATTERN.strip("'")
    assert re.search(pat, f"app_process /system/bin {dev.UA_MAIN} --idle-ms 1")
    assert not re.search(pat, f"sh -c pgrep -f {dev.UA_PATTERN}")


# --- setup rebinds a service Android stopped restarting ---------------------
def _fake_phone(monkeypatch, services, answers_after_writes):
    """Settings in a dict; the agent answers once the service list was rewritten
    `answers_after_writes` times (Android rebinds on a settings change)."""
    import argparse
    from droidctl import cli
    st = {"enabled_accessibility_services": services, "accessibility_enabled": "1"}
    writes = []

    def sh(d, cmd, timeout=30):
        if isinstance(cmd, list) and cmd[:2] == ["settings", "get"]:
            return st.get(cmd[3], "null")
        if isinstance(cmd, list) and cmd[:2] == ["settings", "put"]:
            st[cmd[3]] = cmd[4]
            writes.append((cmd[3], cmd[4]))
            return ""
        if isinstance(cmd, list) and cmd[:2] == ["settings", "delete"]:
            st.pop(cmd[3], None)
            writes.append((cmd[3], None))
            return ""
        return ""

    def wait(port, timeout=8.0):
        if sum(k == "enabled_accessibility_services" for k, _ in writes) < answers_after_writes:
            raise UserError("the agent closed the connection before replying", "connection")
        return {"versionCode": dev.AGENT_VERSION_CODE, "version": "t"}

    monkeypatch.setattr(dev, "APK_PATH", __file__)
    monkeypatch.setattr(dev, "resolve_serial", lambda s: "S")
    monkeypatch.setattr(dev, "adb_device", lambda s: object())
    monkeypatch.setattr(dev, "installed_version", lambda d: dev.AGENT_VERSION_CODE)
    monkeypatch.setattr(dev, "ua_pids", lambda d: [])
    monkeypatch.setattr(dev, "ensure_forward", lambda s, remote=dev.REMOTE: 1)
    monkeypatch.setattr(dev, "sh", sh)
    monkeypatch.setattr(cli, "_wait_for_agent", wait)
    monkeypatch.setattr(cli, "_wait_for_windows", lambda port, timeout=3.0: True)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    args = argparse.Namespace(device=None, reinstall=False, json=True, backend=None)
    return cli, args, st, writes


def test_setup_rebinds_a_listed_service_that_does_not_answer(home, monkeypatch):
    talkback = "com.google.android.marvin.talkback/com.google.android.marvin.talkback.TalkBackService"
    original = f"{talkback}:{dev.COMPONENT}"
    cli, args, st, writes = _fake_phone(monkeypatch, original, answers_after_writes=2)
    out = cli.cmd_setup(args)
    assert out["service_rebound"] and not out["service_added"]
    assert writes[0] == ("enabled_accessibility_services", talkback)        # ours out, TalkBack kept
    assert st["enabled_accessibility_services"] == original                  # back exactly as it was


def test_setup_does_not_touch_a_service_that_answers(home, monkeypatch):
    cli, args, st, writes = _fake_phone(monkeypatch, dev.COMPONENT, answers_after_writes=0)
    out = cli.cmd_setup(args)
    assert not out["service_rebound"] and not writes
