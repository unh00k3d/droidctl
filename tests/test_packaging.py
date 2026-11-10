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
    assert '"droidctl.assets" = ["*.apk", "*.zip"]' in text and 'droidctl = ["SKILL.md"]' in text
    manifest = (ROOT / "MANIFEST.in").read_text()
    assert "droidctl/assets/*.apk" in manifest and "droidctl/assets/*.zip" in manifest


def test_the_ios_runner_is_bundled_and_built_from_the_current_sources():
    """Linux and Macs without Xcode only sign the shipped runner, so it must match the sources."""
    import zipfile
    from droidctl.ios import prebuilt
    assert prebuilt.bundled_build() == prebuilt.build_id(), \
        "the iOS runner sources changed: run `make runner` on a Mac with Xcode and commit the zip"
    with zipfile.ZipFile(prebuilt.BUNDLED) as z:
        names = z.namelist()
    assert not [n for n in names if "/Frameworks/" in n or ".dSYM" in n], "Apple's frameworks must not ship"


def test_adb_falls_back_to_the_binary_adbutils_ships(monkeypatch, tmp_path):
    monkeypatch.setenv("ANDROID_HOME", str(tmp_path))          # no SDK adb
    monkeypatch.setenv("PATH", str(tmp_path))                   # no adb on PATH
    bundled = dev._bundled_adb()
    if bundled is None:
        pytest.skip("this adbutils build ships no adb binary")
    assert dev.adb_path() == bundled


def test_the_ios_runner_sources_ship_in_the_wheel():
    """`droidctl setup` builds the runner from the installed package: every source file of the
    Xcode project must be matched by pyproject's package-data globs."""
    import fnmatch
    import tomllib
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    globs = cfg["tool"]["setuptools"]["package-data"]["droidctl.ios"]
    base = ROOT / "droidctl" / "ios"
    files = [p.relative_to(base).as_posix() for p in (base / "xcode").rglob("*")
             if p.is_file() and "xcuserdata" not in p.parts and "project.xcworkspace" not in p.parts]
    assert files
    missing = [f for f in files if not any(fnmatch.fnmatch(f, g) for g in globs)]
    assert not missing, f"not in package-data: {missing}"


def test_every_package_and_data_file_ships_in_the_wheel():
    """pyproject lists packages explicitly: a new subpackage left off the list installs from a
    checkout (editable) but not from `pipx install git+…` (ModuleNotFoundError on Linux)."""
    import fnmatch
    import tomllib
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]
    tracked = subprocess.run(["git", "ls-files", "droidctl"], cwd=ROOT, capture_output=True, text=True,
                             check=True).stdout.split()
    packages = {str(pathlib.PurePosixPath(f).parent).replace("/", ".") for f in tracked if f.endswith("/__init__.py")}
    assert not packages - set(cfg["packages"]), f"not in [tool.setuptools] packages: {packages - set(cfg['packages'])}"
    data = cfg["package-data"]
    unshipped = []
    for f in tracked:
        if f.endswith(".py") or f.endswith(("/.gitkeep", ".src.sha256")):
            continue
        p = pathlib.PurePosixPath(f)
        owner = max((pkg for pkg in cfg["packages"] if str(p.parent).replace("/", ".").startswith(pkg)), key=len)
        rel = p.relative_to(owner.replace(".", "/")).as_posix()
        if not any(fnmatch.fnmatch(rel, g) for g in data.get(owner, [])):
            unshipped.append(f)
    assert not unshipped, f"not in package-data: {unshipped}"
