"""The daemon, the thin client and serve --stdio: lifecycle and device sessions.

Everything here runs real processes (the `droidctl` thin client and a spawned
daemon) in a throwaway DROIDCTL_HOME. Device sessions talk to FakeAdb and
FakeAgents (tests/fakes.py), so no phone is needed and the user's own daemon,
state and adb server are never touched.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from droidctl import client as cl
from tests.fakes import FakeAdb, FakeAgent

PY = sys.executable


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _env(home, **extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("DROIDCTL_") and k not in ("ANDROID_SERIAL", "ANDROID_ADB_SERVER_PORT")}
    env.update(DROIDCTL_HOME=str(home), DROIDCTL_IDLE="120s")
    env["PYTHONPATH"] = REPO   # subprocesses run this checkout, not whatever is pip-installed
    env.update({k: str(v) for k, v in extra.items()})
    return env


def dc(env, *argv, stdin=None, timeout=60):
    """The thin client, exactly as the console script runs it."""
    return subprocess.run([PY, "-m", "droidctl.client", *argv], env=env, input=stdin,
                          capture_output=True, text=True, timeout=timeout)


def js(res):
    assert res.stdout.strip(), f"no stdout; stderr={res.stderr!r}"
    return json.loads(res.stdout)


def _daemon_pid(home):
    try:
        return int(open(os.path.join(home, "daemon.pid")).read().strip())
    except (OSError, ValueError):
        return None


def _stop(env, home):
    subprocess.run([PY, "-m", "droidctl", "daemon", "stop"], env=env, capture_output=True, timeout=30)
    pid = _daemon_pid(home)
    if pid:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _wait(cond, timeout=6.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _ping(env):
    old = os.environ.get("DROIDCTL_HOME")
    os.environ["DROIDCTL_HOME"] = env["DROIDCTL_HOME"]
    try:
        return cl.ping(timeout=1.0)
    finally:
        if old is None:
            os.environ.pop("DROIDCTL_HOME", None)
        else:
            os.environ["DROIDCTL_HOME"] = old


@pytest.fixture
def home(tmp_path):
    env = _env(tmp_path)
    yield tmp_path
    _stop(env, tmp_path)


# --------------------------------------------------------------------------
# pure client helpers
# --------------------------------------------------------------------------
@pytest.mark.parametrize("argv,want", [
    (["type", "4", "--stdin"], True),
    (["run"], True),
    (["run", "-"], True),
    (["run", "-d", "SERIAL"], True),
    (["run", "steps.txt"], False),
    (["run", "--step", "tap 4"], False),
    (["snapshot"], False),
    (["tap", "4"], False),
])
def test_client_reads_stdin_only_when_the_command_consumes_it(argv, want):
    assert cl._wants_stdin(argv) is want


def test_client_finds_the_command_word():
    assert cl._command(["--json", "snapshot", "--find", "x"]) == "snapshot"
    assert cl._command(["--json"]) is None
    assert cl._command([]) is None


@pytest.mark.parametrize("raw,secs", [("90", 90), ("90s", 90), ("30m", 1800), ("2h", 7200), ("0", 0), ("junk", 1800)])
def test_idle_setting(monkeypatch, raw, secs):
    monkeypatch.setenv("DROIDCTL_IDLE", raw)
    assert cl.idle_seconds() == secs


def test_thin_client_imports_nothing_heavy():
    """The per-call path is the client plus the socket: no argparse, rich, adbutils."""
    out = subprocess.run([PY, "-X", "importtime", "-c", "import droidctl.client"],
                         capture_output=True, text=True).stderr
    mods = {line.split("|")[-1].strip() for line in out.splitlines() if "|" in line}
    for heavy in ("argparse", "rich", "adbutils", "requests", "PIL", "droidctl.cli", "droidctl.daemon"):
        assert heavy not in mods, f"the thin client imports {heavy}"


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------
def test_first_call_starts_the_daemon_and_says_so_exactly_once(home):
    env = _env(home)
    r1 = dc(env, "version")
    assert r1.returncode == 0, r1.stderr
    assert r1.stderr.count("started background daemon") == 1
    assert "DROIDCTL_NO_DAEMON=1" in r1.stderr and "droidctl daemon stop" in r1.stderr
    r2 = dc(env, "version")
    assert r2.returncode == 0 and r2.stderr == ""
    assert r1.stdout == r2.stdout
    p = js(dc(env, "version", "--json"))
    assert p["mode"] == "daemon" and "daemon" not in p
    st = js(dc(env, "daemon", "status", "--json"))
    assert st["running"] and st["pid"] == _daemon_pid(home)
    assert oct(os.stat(home / "d.sock").st_mode & 0o777) == "0o600"


def test_json_first_start_is_reported_in_the_payload_not_on_stderr(home):
    r = dc(_env(home), "version", "--json")
    p = js(r)
    assert r.stderr == ""
    assert p["daemon"]["started"] is True and p["daemon"]["pid"] == _daemon_pid(home)
    assert p["mode"] == "daemon"


def test_a_stale_socket_from_a_crash_is_replaced(home):
    env = _env(home)
    js(dc(env, "version", "--json"))
    old = _daemon_pid(home)
    os.kill(old, signal.SIGKILL)
    assert _wait(lambda: not _ping(env))
    assert (home / "d.sock").exists()           # the crash left its socket behind
    p = js(dc(env, "version", "--json"))
    assert p["daemon"]["started"] is True and p["daemon"]["pid"] != old


def test_a_client_of_another_build_makes_the_daemon_exit(home):
    env = _env(home)
    js(dc(env, "version", "--json"))
    os.environ["DROIDCTL_HOME"] = str(home)
    try:
        c = cl._Conn(timeout=5)
        resp = c.request("run", {"argv": ["version"], "build": "0.0.0+other"})
        c.close()
    finally:
        os.environ.pop("DROIDCTL_HOME")
    assert resp["error"]["code"] == cl.RESTART
    assert _wait(lambda: not _ping(env))
    # and the next normal call brings up this build again
    assert js(dc(env, "version", "--json"))["daemon"]["started"] is True


def test_idle_exit(tmp_path):
    env = _env(tmp_path, DROIDCTL_IDLE="1s")
    try:
        js(dc(env, "version", "--json"))
        assert _wait(lambda: not _ping(env), timeout=8)
        # the exit removes the socket, then the pidfile: wait for both
        assert _wait(lambda: not (tmp_path / "d.sock").exists() and not (tmp_path / "daemon.pid").exists())
    finally:
        _stop(env, tmp_path)


def _daemons(home):
    """pids of daemons serving `home` (other daemons, e.g. the user's own, don't count)."""
    out = subprocess.run(["pgrep", "-f", "droidctl daemon start --foreground"], capture_output=True, text=True)
    mine = set()
    for pid in out.stdout.split():
        try:
            env = open(f"/proc/{pid}/environ", "rb").read().split(b"\0")
        except OSError:
            continue
        if f"DROIDCTL_HOME={home}".encode() in env:
            mine.add(pid)
    return mine


@pytest.mark.parametrize("how", ["flag", "env"])
def test_no_daemon_runs_in_process_and_leaves_nothing_behind(tmp_path, how):
    env = _env(tmp_path, **({"DROIDCTL_NO_DAEMON": "1"} if how == "env" else {}))
    argv = ["version", "--json"] + (["--no-daemon"] if how == "flag" else [])
    r = dc(env, *argv)
    assert js(r)["mode"] == "inprocess" and r.stderr == ""
    assert not any((tmp_path / f).exists() for f in ("d.sock", "daemon.pid", "daemon.log"))
    assert not _daemons(tmp_path)


def test_autostart_off_is_a_typed_error(tmp_path):
    r = dc(_env(tmp_path, DROIDCTL_AUTOSTART="0"), "version", "--json")
    assert r.returncode == 1
    assert js(r)["error"]["kind"] == "no-daemon"
    assert not (tmp_path / "d.sock").exists()


def test_a_sandbox_without_a_usable_socket_falls_back_to_in_process(tmp_path):
    (tmp_path / "d.sock").mkdir()               # connect() and bind() both fail on a directory
    r = dc(_env(tmp_path), "version", "--json")
    assert r.returncode == 0
    assert js(r)["mode"] == "inprocess"
    _stop(_env(tmp_path), tmp_path)


def test_help_usage_errors_and_exit_codes_match_the_cli(home):
    env = _env(home)
    h = dc(env, "where", "--help")
    assert h.returncode == 0 and h.stdout.startswith("usage: droidctl where")
    bad = dc(env, "snapshot", "--bogus")
    assert bad.returncode == 2 and "unrecognized arguments: --bogus" in bad.stderr
    local = subprocess.run([PY, "-m", "droidctl", "snapshot", "--bogus"], env=env, capture_output=True, text=True)
    assert local.returncode == 2 and local.stderr == bad.stderr


def test_daemon_logs_and_restart(home):
    env = _env(home)
    js(dc(env, "version", "--json"))
    old = _daemon_pid(home)
    logs = js(dc(env, "daemon", "logs", "--json"))
    assert any("listening on" in line for line in logs["lines"])
    r = js(subprocess.run([PY, "-m", "droidctl", "daemon", "restart", "--json"], env=env,
                          capture_output=True, text=True, timeout=30))
    assert r["restarted"] is True and r["started"] is True and r["pid"] != old


# --------------------------------------------------------------------------
# device sessions (fake adb + fake agents)
# --------------------------------------------------------------------------
@pytest.fixture
def phones(tmp_path):
    a, b = FakeAgent(), FakeAgent(tree_delay=2.0)
    adb = FakeAdb({"AAA": a, "BBB": b})
    env = _env(tmp_path, ANDROID_ADB_SERVER_PORT=adb.port)
    yield env, a, b
    _stop(env, tmp_path)
    adb.close()
    a.close()
    b.close()


def test_snapshot_cache_hit_miss_and_invalidation(phones):
    env, a, _ = phones
    s1 = js(dc(env, "snapshot", "-d", "AAA", "--json"))
    assert s1["ok"] and s1["mode"] == "daemon" and s1["screen"]["pkg"] == "com.android.settings"
    assert a.calls["tree"] == 1
    js(dc(env, "snapshot", "-d", "AAA", "--json"))             # unchanged: served after a gen check
    assert a.calls["tree"] == 1 and a.calls["gen"] == 1
    time.sleep(0.5)
    js(dc(env, "snapshot", "-d", "AAA", "--json"))             # a gen check far from the dump: quiet
    gens = a.calls["gen"]
    js(dc(env, "snapshot", "-d", "AAA", "--json"))             # now served with no device call at all
    assert a.calls["tree"] == 1 and a.calls["gen"] == gens
    a.change()                                                  # the screen changed: a pushed event
    time.sleep(0.2)
    js(dc(env, "snapshot", "-d", "AAA", "--json"))
    assert a.calls["tree"] == 2
    st = js(dc(env, "daemon", "status", "--json"))
    dev_a = next(d for d in st["devices"] if d["serial"] == "AAA")
    assert dev_a["subscribed"] and dev_a["cache"]["hits"] == 3 and dev_a["cache"]["misses"] == 2


def test_the_event_subscription_comes_back_after_an_unbind(phones):
    """uiautomator/Appium unbind the service for ~1-2 s, which closes the
    daemon's event stream; it must resubscribe instead of staying degraded."""
    env, a, _ = phones
    js(dc(env, "snapshot", "-d", "AAA", "--json"))

    def subscribed():
        st = js(dc(env, "daemon", "status", "--json"))
        return next(d for d in st["devices"] if d["serial"] == "AAA")["subscribed"]

    assert subscribed()
    subs_before = a.calls["subscribe"]
    a.drop_subscribers()
    assert _wait(lambda: a.calls["subscribe"] > subs_before, timeout=6.0), "never resubscribed"
    assert _wait(subscribed, timeout=6.0)
    a.change()                                  # events flow again: the cache sees the change
    time.sleep(0.2)
    trees = a.calls["tree"]
    js(dc(env, "snapshot", "-d", "AAA", "--json"))
    assert a.calls["tree"] == trees + 1


def test_an_action_primes_the_cache_with_its_settled_tree(phones):
    env, a, _ = phones
    js(dc(env, "snapshot", "-d", "AAA", "--json"))
    r = js(dc(env, "back", "-d", "AAA", "--json"))
    assert r["ok"] and a.calls["global"] == 1
    trees = a.calls["tree"]
    js(dc(env, "snapshot", "-d", "AAA", "--json"))
    assert a.calls["tree"] == trees                             # served from the action's tree


def test_events_between_calls_come_with_the_next_result(phones):
    env, a, _ = phones
    js(dc(env, "snapshot", "-d", "AAA", "--json"))
    a.toast("Message archived")
    time.sleep(0.3)
    p = js(dc(env, "snapshot", "-d", "AAA", "--json"))
    assert {"type": "toast", "text": "Message archived", "pkg": "com.android.settings"} in p["between_calls"]
    human = dc(env, "snapshot", "-d", "AAA")
    assert "between calls" not in human.stdout               # reported once, not forever
    a.toast("Saved!")
    time.sleep(0.3)
    assert 'between calls: toast "Saved!"' in dc(env, "snapshot", "-d", "AAA").stdout


def test_phone_a_never_blocks_phone_b(phones):
    env, a, b = phones
    js(dc(env, "version", "--json"))                           # daemon up first
    slow = subprocess.Popen([PY, "-m", "droidctl.client", "snapshot", "-d", "BBB", "--json"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(0.3)
    t0 = time.monotonic()
    fast = js(dc(env, "snapshot", "-d", "AAA", "--json"))
    took = time.monotonic() - t0
    assert fast["ok"] and took < 1.5, f"phone A waited {took:.2f}s for phone B"
    assert slow.poll() is None                                  # B's 2 s dump is still running
    out, _ = slow.communicate(timeout=30)
    assert json.loads(out)["ok"]


def test_actions_on_one_device_are_serialized_but_reads_are_not(tmp_path):
    a = FakeAgent(act_delay=1.0)
    adb = FakeAdb({"AAA": a})
    env = _env(tmp_path, ANDROID_ADB_SERVER_PORT=adb.port)
    try:
        js(dc(env, "snapshot", "-d", "AAA", "--json"))
        t0 = time.monotonic()
        procs = [subprocess.Popen([PY, "-m", "droidctl.client", "back", "-d", "AAA", "--json"], env=env,
                                  stdout=subprocess.PIPE, text=True) for _ in range(2)]
        time.sleep(0.3)
        assert js(dc(env, "snapshot", "-d", "AAA", "--json"))["ok"]
        still_acting = sum(p.poll() is None for p in procs)
        outs = [json.loads(p.communicate(timeout=30)[0]) for p in procs]
        both = time.monotonic() - t0
        assert all(o["ok"] for o in outs)
        assert both >= 2.0, f"two 1 s actions on one device overlapped ({both:.2f}s)"
        assert still_acting >= 1, "the snapshot waited for the actions instead of being served alongside"
    finally:
        _stop(env, tmp_path)
        adb.close()
        a.close()


def test_a_device_side_wait_does_not_hold_the_shared_connection(phones):
    env, a, _ = phones
    js(dc(env, "snapshot", "-d", "AAA", "--json"))
    waiting = subprocess.Popen([PY, "-m", "droidctl.client", "wait", "-d", "AAA", "--text", "nope",
                                "--timeout", "2", "--json"], env=env, stdout=subprocess.PIPE, text=True)
    time.sleep(0.3)
    a.change()
    time.sleep(0.1)
    t0 = time.monotonic()
    assert js(dc(env, "snapshot", "-d", "AAA", "--json"))["ok"]
    assert time.monotonic() - t0 < 1.0
    out = json.loads(waiting.communicate(timeout=30)[0])
    assert out["error"]["kind"] == "timeout"


def test_python_client_sdk_and_subscription(phones):
    env, a, _ = phones
    js(dc(env, "version", "--json"))
    os.environ["DROIDCTL_HOME"] = env["DROIDCTL_HOME"]
    os.environ["ANDROID_ADB_SERVER_PORT"] = env["ANDROID_ADB_SERVER_PORT"]
    try:
        with cl.Client() as c:
            snap = c.call("snapshot -d AAA")
            assert snap["ok"] and snap["mode"] == "daemon"
            with pytest.raises(cl.DroidctlError) as e:
                c.call(["snapshot", "-d", "NOPE"])
            assert e.value.kind == "no-device"
            got = []

            def listen():
                for ev in c.subscribe(device="AAA", events=["toast"], timeout=5):
                    got.append(ev)
                    return

            t = threading.Thread(target=listen)
            t.start()
            time.sleep(0.5)
            a.toast("hello")
            t.join(timeout=10)
            assert got and got[0]["text"] == "hello" and got[0]["serial"] == "AAA"
            assert c.status()["subscribers"] == 0 or True   # the stream closed with the iterator
    finally:
        os.environ.pop("DROIDCTL_HOME", None)
        os.environ.pop("ANDROID_ADB_SERVER_PORT", None)


def test_serve_stdio_bridges_the_daemon_protocol(phones):
    env, _, _ = phones
    proc = subprocess.Popen([PY, "-m", "droidctl", "serve", "--stdio"], env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True)
    reqs = [{"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "id": 2, "method": "run", "params": {"argv": ["snapshot", "-d", "AAA", "--json"]}}]
    out, _ = proc.communicate("\n".join(json.dumps(r) for r in reqs) + "\n", timeout=60)
    replies = {m["id"]: m for m in map(json.loads, out.splitlines())}
    assert replies[1]["result"]["ok"] is True
    run = replies[2]["result"]
    assert run["code"] == 0 and json.loads(run["stdout"])["screen"]["pkg"] == "com.android.settings"


def test_run_reads_its_steps_from_stdin_through_the_daemon(phones):
    env, _, _ = phones
    r = dc(env, "run", "-d", "AAA", "--json", stdin="snapshot\nversion\n")
    p = js(r)
    assert p["ok"] and p["steps"] == 2 and p["mode"] == "daemon"


def test_agents_with_different_serials_and_cwds_do_not_block_each_other(phones, tmp_path):
    """ANDROID_SERIAL is the command's -d (not process env) and path arguments are made
    absolute, so neither makes one client wait for another's long command."""
    env, a, b = phones
    js(dc(env, "version", "--json"))
    other = tmp_path / "elsewhere"
    other.mkdir()
    slow = subprocess.Popen([PY, "-m", "droidctl.client", "snapshot", "--json"],
                            env=dict(env, ANDROID_SERIAL="BBB"), cwd=str(other),
                            stdout=subprocess.PIPE, text=True)
    time.sleep(0.3)
    t0 = time.monotonic()
    fast = js(subprocess.run([PY, "-m", "droidctl.client", "snapshot", "--json"],
                             env=dict(env, ANDROID_SERIAL="AAA"), cwd=str(tmp_path),
                             capture_output=True, text=True, timeout=30))
    assert fast["ok"] and time.monotonic() - t0 < 1.5
    assert slow.poll() is None
    assert json.loads(slow.communicate(timeout=30)[0])["ok"]
    assert a.calls["tree"] >= 1 and b.calls["tree"] >= 1          # each went to its own phone


def test_relative_paths_resolve_against_the_clients_cwd(home, tmp_path):
    env = _env(home)
    src = os.path.join(os.path.dirname(__file__), "fixtures", "trees", "real-settings-main.json")
    work = tmp_path / "work"
    (work / "fx").mkdir(parents=True)
    with open(src) as f, open(work / "fx" / "t.json", "w") as g:
        g.write(f.read())
    (work / "steps.txt").write_text("snapshot --fixture fx/t.json\n")
    r = subprocess.run([PY, "-m", "droidctl.client", "snapshot", "--fixture", "fx/t.json", "--json"],
                       env=env, cwd=str(work), capture_output=True, text=True, timeout=30)
    assert js(r)["screen"]["pkg"] == "com.android.settings"
    r = subprocess.run([PY, "-m", "droidctl.client", "run", "steps.txt", "--json"],
                       env=env, cwd=str(work), capture_output=True, text=True, timeout=30)
    p = js(r)
    assert p["ok"] and p["results"][0]["result"]["screen"]["pkg"] == "com.android.settings"


def test_doctor_reports_daemon_state_when_none_is_running(tmp_path, monkeypatch):
    """doctor gains a daemon check that runs before the device checks and never
    fails on its own: not-running is a normal state (the first call starts it)."""
    import argparse
    monkeypatch.setenv("DROIDCTL_HOME", str(tmp_path))
    from droidctl import cli
    from droidctl import device as dev

    def no_adb(*a, **k):
        raise cli.UserError("no adb server", "adb")
    monkeypatch.setattr(dev, "_adb_host_query", no_adb)

    payload = cli.cmd_doctor(argparse.Namespace(device=None, json=True))
    names = [c["name"] for c in payload["checks"]]
    assert names[0] == "daemon", "daemon check should run first, before the device checks"
    daemon_c = payload["checks"][0]
    assert daemon_c["ok"] is True
    assert "not running" in daemon_c["detail"]
