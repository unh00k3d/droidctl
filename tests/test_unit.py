"""Tests that need no device: parsing, the surface, output and the error vocabulary."""
import argparse
import json
import pathlib
import re

import pytest

from droidctl import cli, core

PKG = pathlib.Path(cli.__file__).resolve().parent


# --- the command surface --------------------------------------------------
def test_parser_builds_and_every_command_has_a_handler():
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    for name, sp in sub.choices.items():
        assert sp.get_default("fn") is not None, f"{name} has no fn"


def test_every_command_has_help_and_json():
    """--json is the agent contract: it has to exist everywhere, uniformly."""
    for r in cli._surface():
        assert r["help"], f"{r['command']} needs a help string"
        assert "--json" in r["options"], f"{r['command']} is missing --json"


# --- output and errors ----------------------------------------------------
def test_user_error_carries_a_machine_readable_kind_and_hint():
    e = core.UserError("nope", "timeout", hint="try again")
    assert (str(e), e.kind, e.hint) == ("nope", "timeout", "try again")


def test_emit_prints_json_when_asked(capsys):
    core.emit(argparse.Namespace(json=True), {"ok": True, "n": 1}, lambda p: print("human"))
    assert json.loads(capsys.readouterr().out) == {"ok": True, "n": 1}


def test_emit_renders_for_humans_otherwise(capsys):
    core.emit(argparse.Namespace(json=False), {"ok": True}, lambda p: print("human", p["ok"]))
    assert capsys.readouterr().out.strip() == "human True"


def test_die_emits_a_json_envelope(capsys):
    with pytest.raises(SystemExit) as exc:
        core.die(argparse.Namespace(json=True), "timeout", "waited too long", "hint here")
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False, "error": {"kind": "timeout", "message": "waited too long", "hint": "hint here"}}


def test_dispatch_maps_user_errors_to_the_envelope(capsys):
    def boom(a):
        raise core.UserError("no phone", "no-device")
    with pytest.raises(SystemExit):
        cli.dispatch(argparse.Namespace(json=True, fn=boom))
    assert json.loads(capsys.readouterr().out)["error"]["kind"] == "no-device"


def test_version_and_cheat_run_end_to_end(capsys):
    cli.main(["version", "--json"])
    assert json.loads(capsys.readouterr().out)["ok"] is True
    cli.main(["cheat", "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert {"version", "cheat"} <= {r["command"] for r in rows}


# --- the error-kind vocabulary --------------------------------------------
def test_every_error_kind_used_in_the_source_is_documented():
    """ERROR_KINDS is the single source; this keeps new kinds from slipping in unlisted."""
    used = set()
    for f in PKG.glob("*.py"):
        src = f.read_text()
        used |= set(re.findall(r'UserError\([^)]*?,\s*"([a-z-]+)"', src, re.S))
        used |= set(re.findall(r'\bdie\(\w+,\s*"([a-z-]+)"', src))
        used |= set(re.findall(r'\bkind="([a-z-]+)"', src))
    undocumented = used - set(core.ERROR_KINDS)
    assert not undocumented, f"add to ERROR_KINDS: {sorted(undocumented)}"
