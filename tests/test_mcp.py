"""`droidctl mcp`: tools generated from the registry, a real stdio session, --install."""
import argparse
import json
import os
import subprocess
import sys

import pytest

from droidctl import cli, mcp
from tests.fakes import FakeAdb, FakeAgent

pytest.importorskip("mcp.server.lowlevel")


def _choices():
    parser = cli.build_parser()
    act = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return act.choices


# --- the tool list is the command registry --------------------------------
def test_tool_list_matches_the_command_registry():
    choices = _choices()
    specs = {s["name"]: s for s in mcp.tool_specs()}
    expected = [name for name, command, _ in mcp.TOOLS
                if (command in choices) or (command is None and "press" in choices)]
    assert list(specs) == expected
    for s in specs.values():
        if s["command"] is None:
            continue
        fields = {dest for dest, _, _ in mcp._fields(choices[s["command"]])}
        assert set(s["inputSchema"]["properties"]) == fields, s["name"]
        assert "json" not in fields and "help" not in fields


def test_the_small_tool_set_is_all_there():
    """PLAN.md: snapshot, tap, type, scroll, swipe, action, key, wait, launch, shot, logs, devices."""
    names = [s["name"] for s in mcp.tool_specs()]
    assert names == ["snapshot", "tap", "type", "scroll", "swipe", "action", "key", "wait",
                     "launch", "shot", "logs", "devices"]


def test_descriptions_are_short():
    for s in mcp.tool_specs():
        assert s["description"] and len(s["description"]) <= 220, s["name"]
        assert s["description"].count(". ") <= 2, s["name"]


def test_tool_arguments_become_an_argv_the_cli_parses():
    parser = cli.build_parser()
    specs = {s["name"]: s for s in mcp.tool_specs()}
    cases = {
        "tap": {"ref": 4, "device": "X", "settle": 0},
        "type": {"target": "2", "content": "héllo \"quoted\" $HOME", "enter": True},
        "snapshot": {"find": "Wi-Fi", "layout": "flat", "max": 20},
        "scroll": {"direction": "down"},
        "wait": {"text": "Done", "timeout": 3},
        "launch": {"pkg": "com.android.settings"},
    }
    for name, args in cases.items():
        argv = mcp.build_argv(specs[name], args)
        ns = parser.parse_args(argv)
        assert ns.json is True
        for k, v in args.items():
            assert getattr(ns, k) == v, (name, k)


def test_key_maps_to_a_global_action_or_press():
    key = next(s for s in mcp.tool_specs() if s["name"] == "key")
    assert mcp.build_argv(key, {"key": "back"}) == ["back", "--json"]
    assert mcp.build_argv(key, {"key": "enter", "device": "X"}) == ["press", "enter", "-d", "X", "--json"]


def test_unknown_arguments_are_a_typed_error():
    from droidctl.core import UserError
    tap = next(s for s in mcp.tool_specs() if s["name"] == "tap")
    with pytest.raises(UserError) as e:
        mcp.build_argv(tap, {"bogus": 1})
    assert e.value.kind == "bad-args"


def test_errors_are_is_error_with_kind_text_and_shot_is_an_image(tmp_path):
    specs = {s["name"]: s for s in mcp.tool_specs()}
    blocks, structured, is_error = mcp.result_for(
        specs["tap"], {"ok": False, "error": {"kind": "stale-ref", "message": "gone", "hint": "re-run snapshot"}}, "", 1)
    assert is_error and blocks == [{"type": "text", "text": "stale-ref: gone\nre-run snapshot"}]
    img = tmp_path / "s.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
    blocks, structured, is_error = mcp.result_for(specs["shot"], {"ok": True, "path": str(img)}, "shot", 0)
    assert not is_error and [b["type"] for b in blocks] == ["text", "image"]
    assert blocks[1]["mimeType"] == "image/jpeg"


def test_the_guide_names_the_error_kinds():
    g = mcp.guide()
    assert "`stale-ref`" in g and "`ambiguous`" in g and "{kinds}" not in g


# --- a real stdio session (the official SDK's client) ---------------------
def test_stdio_session_against_fake_phones(tmp_path):
    import anyio
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    agent = FakeAgent()
    adb = FakeAdb({"AAA": agent})
    env = {k: v for k, v in os.environ.items() if not k.startswith("DROIDCTL_") and k != "ANDROID_SERIAL"}
    env.update(DROIDCTL_HOME=str(tmp_path), ANDROID_ADB_SERVER_PORT=str(adb.port), DROIDCTL_IDLE="60s")
    env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    async def session():
        params = StdioServerParameters(command=sys.executable, args=["-m", "droidctl", "mcp"], env=env)
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as s:
                init = await s.initialize()
                tools = await s.list_tools()
                guide = await s.read_resource("droidctl://guide")
                snap = await s.call_tool("snapshot", {"device": "AAA"})
                back = await s.call_tool("key", {"key": "back", "device": "AAA"})
                missing = await s.call_tool("snapshot", {"device": "NOPE"})
                return init, tools, guide, snap, back, missing

    try:
        init, tools, guide, snap, back, missing = anyio.run(session)
    finally:
        subprocess.run([sys.executable, "-m", "droidctl", "daemon", "stop"], env=env, capture_output=True)
        adb.close()
        agent.close()
    assert init.server_info.name == "droidctl"
    assert [t.name for t in tools.tools] == [s["name"] for s in mcp.tool_specs()]
    assert "snapshot" in guide.contents[0].text
    assert not snap.is_error
    assert snap.content[0].text.startswith("screen com.android.settings")
    assert snap.structured_content["ok"] is True and snap.structured_content["mode"] == "daemon"
    assert not back.is_error and back.structured_content["method"] == "back"
    assert missing.is_error and missing.content[0].text.startswith("no-device:")


# --- mcp --install ----------------------------------------------------------
@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(mcp.shutil, "which",
                        lambda name: "/opt/bin/droidctl" if name == "droidctl" else None)
    return tmp_path


def test_install_cursor_keeps_other_servers_and_backs_up(fake_home):
    path = fake_home / ".cursor" / "mcp.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "keep": 1}))
    dry = mcp.install("cursor", dry_run=True)
    assert dry["clients"][0]["action"] == "would add"
    assert "droidctl" not in json.loads(path.read_text())["mcpServers"]
    r = mcp.install("cursor")["clients"][0]
    assert r["action"] == "added" and r["kept"] == ["other"] and os.path.exists(r["backup"])
    doc = json.loads(path.read_text())
    assert doc["keep"] == 1 and doc["mcpServers"]["other"] == {"command": "x"}
    assert doc["mcpServers"]["droidctl"] == {"command": "/opt/bin/droidctl", "args": ["mcp"]}
    assert mcp.install("cursor")["clients"][0]["action"] == "already configured"


def test_install_never_clobbers_a_different_entry_without_force(fake_home):
    path = fake_home / ".claude.json"
    path.write_text(json.dumps({"mcpServers": {"droidctl": {"command": "old"}}, "projects": {}}))
    assert mcp.install("claude")["clients"][0]["action"] == "skipped"
    assert json.loads(path.read_text())["mcpServers"]["droidctl"] == {"command": "old"}
    assert mcp.install("claude", force=True)["clients"][0]["action"] == "added"
    assert json.loads(path.read_text())["mcpServers"]["droidctl"]["command"] == "/opt/bin/droidctl"


def test_install_codex_appends_a_toml_section_once(fake_home):
    path = fake_home / ".codex" / "config.toml"
    path.parent.mkdir()
    path.write_text('model = "o4"\n\n[mcp_servers.other]\ncommand = "x"\n')
    assert mcp.install("codex")["clients"][0]["action"] == "added"
    text = path.read_text()
    assert text.startswith('model = "o4"') and "[mcp_servers.other]" in text
    assert '[mcp_servers.droidctl]\ncommand = "/opt/bin/droidctl"\nargs = ["mcp"]' in text
    assert mcp.install("codex")["clients"][0]["action"] == "already configured"
    assert path.read_text() == text


def test_install_all_reports_each_client(fake_home):
    r = mcp.install("all", dry_run=True)
    assert [c["client"] for c in r["clients"]] == ["claude", "codex", "cursor"]
    assert not any((fake_home / p).exists() for p in (".claude.json", ".codex", ".cursor"))
