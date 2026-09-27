"""TESTAPP group 7: device-level cases that are safe to run on the user's phone.

Not run here, on purpose (each would need the user or risk the phone):
- screen off / locked: the phone's lock screen needs the user to unlock it;
- uiautomator2/Appium attached: starting UiAutomation suppresses a11y services
  until they are rebound; exercised offline via doctor's process check;
- APK older than the host: needs an older signed APK build on the phone;
- two devices attached: only one phone here (select_serial is unit-tested).
"""
import json
import os
import subprocess
import sys
import time

import anyio
import pytest

from tests.e2e.conftest import SERIAL, adb, dc

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def env_for(home, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("DROIDCTL_")}
    env.update(DROIDCTL_HOME=str(home), ANDROID_SERIAL=SERIAL, DROIDCTL_IDLE="120s", PYTHONPATH=REPO)
    env.update(extra)
    return env


def client(env, *argv):
    out = subprocess.run([sys.executable, "-m", "droidctl.client", *argv], env=env,
                         capture_output=True, text=True, timeout=90)
    return out


def daemons():
    out = subprocess.run(["pgrep", "-f", "droidctl daemon start"], capture_output=True, text=True).stdout
    return set(out.split())


def stop(env):
    subprocess.run([sys.executable, "-m", "droidctl", "daemon", "stop"], env=env, capture_output=True, timeout=30)


def test_no_daemon_runs_in_process_and_leaves_nothing(tmp_path):
    before = daemons()
    out = client(env_for(tmp_path, DROIDCTL_NO_DAEMON="1"), "ping", "--json")
    assert json.loads(out.stdout)["mode"] == "inprocess"
    assert daemons() == before


def test_autostart_off_is_a_typed_error(tmp_path):
    out = client(env_for(tmp_path, DROIDCTL_AUTOSTART="0"), "ping", "--json")
    assert json.loads(out.stdout)["error"]["kind"] == "no-daemon"


def test_first_start_notice_exactly_once_then_daemon_mode(tmp_path):
    env = env_for(tmp_path)
    try:
        a = client(env, "ping")
        b = client(env, "ping", "--json")
        assert a.stderr.count("started background daemon") == 1, a.stderr
        assert "started background daemon" not in b.stderr
        assert json.loads(b.stdout)["mode"] == "daemon"
    finally:
        stop(env)


def test_api_28_fallbacks_are_reported_not_silent(tmp_path, scenario):
    from droidctl import device as dev
    scenario("buttons")
    c, info = dev.connect(SERIAL)
    try:
        assert info["sdk"] < 30
        with pytest.raises(dev.UserError) as e:
            c.call("screenshot", {"scale": 0.5})
        assert e.value.kind == "unsupported"
    finally:
        c.close()
    r = dc("shot", "--out", tmp_path / "s.jpg")
    assert r["source"] == "screencap" and (tmp_path / "s.jpg").stat().st_size > 1000


def test_a_disabled_service_is_re_enabled_by_auto_setup(scenario):
    """The service disappears mid-run (only our entry is removed); the next command
    re-enables it (auto-setup) and succeeds. Other entries are never touched."""
    sc = scenario("buttons")
    before = adb("shell", "settings", "get", "secure", "enabled_accessibility_services").strip()
    entries = [e for e in before.split(":") if e and e != "null"]
    ours = [e for e in entries if e.lower().startswith("dev.droidctl.agent/")]
    assert ours, before
    others = [e for e in entries if e not in ours]
    adb("shell", "settings", "put", "secure", "enabled_accessibility_services", ":".join(others) or "''")
    time.sleep(1.0)
    r = dc("tap", "--text", "Save")
    assert r["ok"] and len(sc.dta("click", "save")) == 1
    after = adb("shell", "settings", "get", "secure", "enabled_accessibility_services").strip()
    assert any(e.lower().startswith("dev.droidctl.agent/") for e in after.split(":"))
    assert all(o in after.split(":") for o in others)


def test_mcp_stdio_snapshot_and_tap_on_cart(tmp_path, scenario):
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    sc = scenario("cart")
    env = env_for(tmp_path)

    async def session():
        params = StdioServerParameters(command=sys.executable, args=["-m", "droidctl", "mcp"], env=env)
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as s:
                await s.initialize()
                tools = await s.list_tools()
                snap = await s.call_tool("snapshot", {"device": SERIAL})
                tap = await s.call_tool("tap", {"device": SERIAL, "text": "+", "right_of": "Laptop Stand"})
                return tools, snap, tap
    try:
        tools, snap, tap = anyio.run(session)
    finally:
        stop(env)
    from droidctl import mcp
    assert [t.name for t in tools.tools] == [t["name"] for t in mcp.tool_specs()]
    assert not snap.is_error and "Wireless Mouse" in snap.content[0].text
    assert not tap.is_error, tap.content[0].text
    ev = sc.dta("inc")
    assert len(ev) == 1 and ev[0]["id"] == "Laptop Stand"
