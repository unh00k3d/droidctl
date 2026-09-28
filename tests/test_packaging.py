"""The committed agent APK is what `pipx install git+…` ships (then `droidctl setup`
puts it on the phone), so it must be present and built from the current sources."""
import pathlib
import subprocess
import sys

import pytest

from droidctl import device as dev

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_the_agent_apk_is_bundled():
    apk = pathlib.Path(dev.APK_PATH)
    assert apk.is_file() and apk.stat().st_size > 10_000, "run: make apk (and commit the APK)"


def test_the_bundled_apk_was_built_from_the_current_sources():
    if not (ROOT / "android").is_dir():
        pytest.skip("android/ is not part of the installed package")
    sys.path.insert(0, str(ROOT / "scripts"))
    from agent_src_hash import agent_src_hash
    recorded = (ROOT / "droidctl" / "assets" / "droidctl-agent.src.sha256").read_text().strip()
    assert recorded == agent_src_hash(), "the agent sources changed: run `make apk` and commit the APK"


def test_the_bundled_apk_has_the_expected_version_code():
    aapt = sorted((pathlib.Path.home() / "Android/Sdk/build-tools").glob("*/aapt"))
    if not aapt:
        pytest.skip("no aapt (Android build-tools) to read the APK")
    out = subprocess.run([str(aapt[-1]), "dump", "badging", dev.APK_PATH], capture_output=True, text=True).stdout
    assert f"versionCode='{dev.AGENT_VERSION_CODE}'" in out, out.splitlines()[:1]


def test_the_wheel_config_ships_the_apk_and_skill():
    text = (ROOT / "pyproject.toml").read_text()
    assert '"droidctl.assets" = ["*.apk"]' in text and 'droidctl = ["SKILL.md"]' in text
    assert "droidctl/assets/*.apk" in (ROOT / "MANIFEST.in").read_text()


def test_adb_falls_back_to_the_binary_adbutils_ships(monkeypatch, tmp_path):
    monkeypatch.setenv("ANDROID_HOME", str(tmp_path))          # no SDK adb
    monkeypatch.setenv("PATH", str(tmp_path))                   # no adb on PATH
    bundled = dev._bundled_adb()
    if bundled is None:
        pytest.skip("this adbutils build ships no adb binary")
    assert dev.adb_path() == bundled
