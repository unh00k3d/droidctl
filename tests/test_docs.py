"""Drift tests: the docs are generated from (or checked against) the code."""
import pathlib
import re

import pytest

from droidctl import cli, core

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _read(name):
    path = ROOT / name
    if not path.exists():
        pytest.skip(f"{name} is not part of the installed package")
    return path.read_text()


# --- AGENTS.md -------------------------------------------------------------
def test_agents_md_command_table_matches_the_parser():
    """AGENTS.md pastes the generated table. Regenerate with:
    python -c 'from droidctl.cli import _command_table; print(_command_table())'"""
    _, sep, rest = _read("AGENTS.md").partition("## All commands\n")
    assert sep, "AGENTS.md lost its '## All commands' heading"
    table = "\n".join(l for l in rest.splitlines() if l.startswith("|"))
    assert table == cli._command_table(), "AGENTS.md command table is stale"


def test_agents_md_error_kinds_match_the_registry():
    """Regenerate with: python -c 'from droidctl.cli import _error_kind_table; print(_error_kind_table())'"""
    _, sep, rest = _read("AGENTS.md").partition("## Error kinds\n")
    assert sep, "AGENTS.md lost its '## Error kinds' heading"
    section = rest.split("\n## ", 1)[0]
    table = "\n".join(l for l in section.splitlines() if l.startswith("|"))
    assert table == cli._error_kind_table(), "AGENTS.md error-kind table is stale"


def test_every_command_is_in_the_docs_table():
    table = cli._command_table()
    for r in cli._surface():
        assert f"| `{r['command']}" in table


def test_locator_folding_only_hides_what_the_docs_explain():
    """The table folds the shared locators into LOCATOR; the folded set must be exactly
    the locator group commands.py defines, or the docs would hide a real option."""
    tap = next(r for r in cli._surface() if r["command"] == "tap")
    assert all(o in tap["options"] for o in cli._LOCATOR_OPTS)
    assert "LOCATOR" in cli._doc_usage(tap)
    for o in tap["options"]:
        if o not in cli._LOCATOR_OPTS and o not in cli._UNIVERSAL_OPTS:
            assert o in cli._doc_usage(tap)


# --- SKILL.md --------------------------------------------------------------
def test_skill_has_frontmatter_with_a_description():
    text = cli._skill_text()
    m = re.match(r"---\nname: droidctl\ndescription: (.+?)\n---\n", text, re.S)
    assert m and len(m.group(1)) > 80


def test_skill_has_no_unfilled_placeholders_and_lists_every_error_kind():
    text = cli._skill_text()
    assert "<!--" not in text, "a generated placeholder was left unsubstituted"
    for kind in core.ERROR_KINDS:
        if kind != "error":
            assert f"`{kind}`" in text, f"the skill does not mention error kind {kind}"
    assert cli._command_table() in text


def test_skill_install_and_refuse_to_overwrite(tmp_path):
    import argparse
    out = cli.cmd_skill(argparse.Namespace(action="install", dir=str(tmp_path), force=False))
    dest = tmp_path / "droidctl" / "SKILL.md"
    assert out["path"] == str(dest) and dest.read_text() == cli._skill_text()
    with pytest.raises(core.UserError) as e:
        cli.cmd_skill(argparse.Namespace(action="install", dir=str(tmp_path), force=False))
    assert e.value.kind == "bad-args"
    cli.cmd_skill(argparse.Namespace(action="install", dir=str(tmp_path), force=True))


# --- the daemon must never be a surprise (PLAN "Making the daemon visible") --
@pytest.mark.parametrize("doc,heading", [("README.md", "## Background daemon"),
                                         ("AGENTS.md", "## Daemon")])
def test_daemon_sections_exist_and_name_the_opt_outs(doc, heading):
    text = _read(doc)
    assert heading in text, f"{doc} lost its daemon section"
    section = text.split(heading, 1)[1].split("\n## ", 1)[0]
    for env in ("DROIDCTL_NO_DAEMON", "DROIDCTL_AUTOSTART", "DROIDCTL_IDLE", "--no-daemon"):
        assert env in section, f"{doc} daemon section does not mention {env}"


def test_help_epilog_mentions_the_daemon():
    epilog = cli.build_parser().epilog
    assert "daemon" in epilog and "DROIDCTL_NO_DAEMON" in epilog


# --- security wording (PLAN: never "no permissions") -----------------------
def test_readme_states_the_permission_model_exactly():
    text = _read("README.md")
    assert ("requests no Android permissions (not even INTERNET); its capabilities come\n"
            "  from being enabled as an accessibility service, which setup does and teardown undoes"
            ) in text


def test_manifest_really_requests_no_permissions():
    manifest = ROOT / "android/agent/src/main/AndroidManifest.xml"
    if not manifest.exists():
        pytest.skip("android sources are not part of the installed package")
    import xml.etree.ElementTree as ET
    root = ET.parse(manifest).getroot()          # comments are dropped, only elements count
    assert not [e for e in root.iter() if e.tag.startswith("uses-permission")]


# --- licensing ---------------------------------------------------------------
def test_notice_credits_what_the_source_credits():
    notice = _read("NOTICE")
    src = "\n".join(p.read_text() for p in (ROOT / "droidctl").glob("*.py"))
    for project in ("Artemis", "Maestro", "appium-mcp", "android_world", "chromectl"):
        if project in src:
            assert project in notice, f"the source credits {project} but NOTICE does not"
    assert "AGPL" in notice and "not included" in notice.lower()
