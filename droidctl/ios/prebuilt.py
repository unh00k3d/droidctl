"""The prebuilt runner: built once without signing, stripped of Apple's XCTest frameworks (the
Xcode licence forbids redistributing them; the phone loads its own from the developer disk
image, iOS 17+), then signed per team on the user's machine (`sign.py`).

Where it comes from, in order:
1. `DROIDCTL_IOS_RUNNER` = a path to a runner zip or an unpacked artifact directory;
2. the zip shipped inside the package (`droidctl/assets/droidctl-runner.zip`, built by
   `make runner` and committed like the agent APK), when it was built from these sources;
3. on a Mac with Xcode: built here, unsigned (14 s measured 2026-11-09).

An artifact directory holds `Debug-iphoneos/DroidctlRunner-Runner.app`, `runner.xctestrun` and
`manifest.json` ({build, xcode, stripped}).
"""
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import tempfile
import zipfile

from droidctl.core import UserError
from droidctl.ios import devices, runner

PLACEHOLDER_PREFIX = "dev.droidctl.prebuilt"      # rewritten per team when signing
APP = "DroidctlRunner-Runner.app"
PLUGIN = "PlugIns/DroidctlRunner.xctest"
BUNDLED = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets",
                       "droidctl-runner.zip")
ZIP_TIME = (1980, 1, 1, 0, 0, 0)                   # fixed, so a rebuild of the same bytes zips the same


def build_id():
    """The prebuilt runner's build id: the sources' hash (no team: one artifact for everyone)."""
    return hashlib.sha256(f"{runner.source_hash()}:prebuilt".encode()).hexdigest()[:12]


def root():
    return os.path.join(runner.ios_home(), "runner")


def artifact_dir(bid=None):
    return os.path.join(root(), bid or build_id())


def manifest(path):
    try:
        with open(os.path.join(path, "manifest.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def valid(path):
    m = manifest(path)
    return bool(m) and os.path.isdir(os.path.join(path, "Debug-iphoneos", APP)) and \
        os.path.exists(os.path.join(path, "runner.xctestrun"))


# --------------------------------------------------------------------------
# making one (CI, or a Mac with Xcode)
# --------------------------------------------------------------------------
def strip(app):
    """Drop what must not ship (Apple's frameworks) and what isn't needed (debug symbols)."""
    shutil.rmtree(os.path.join(app, "Frameworks"), ignore_errors=True)
    for d in os.listdir(os.path.join(app, "PlugIns")):
        if d.endswith(".dSYM"):
            shutil.rmtree(os.path.join(app, "PlugIns", d), ignore_errors=True)
    for f in ("_CodeSignature", "embedded.mobileprovision"):
        p = os.path.join(app, f)
        shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else (os.path.exists(p) and os.remove(p))
    # debug-map entries carry the build's temp path and object mtimes: drop them (same source,
    # same bytes; nothing of the building machine in what ships)
    for exe in (os.path.join(app, APP[:-len(".app")]), os.path.join(app, PLUGIN, "DroidctlRunner")):
        if os.path.exists(exe):
            subprocess.run(["xcrun", "strip", "-S", exe], env=devices.xcode_env(), check=True,
                           capture_output=True, timeout=60)


def build_unsigned(out, log=None):
    """xcodebuild build-for-testing for any iPhone, unsigned; the stripped artifact goes to `out`."""
    if not devices.developer_dir():
        raise UserError("building the runner needs Xcode", "missing-dep",
                        hint="install Xcode, or set DROIDCTL_IOS_RUNNER to a prebuilt runner zip")
    bid = build_id()
    with tempfile.TemporaryDirectory(prefix="droidctl-runner-") as dd:
        logpath = log or os.path.join(runner.ios_home(), "build-unsigned.log")
        os.makedirs(os.path.dirname(logpath), exist_ok=True)
        cmd = ["xcodebuild", "build-for-testing", "-project", runner.PROJECT, "-scheme", runner.SCHEME,
               "-destination", "generic/platform=iOS", "-derivedDataPath", dd, "CODE_SIGNING_ALLOWED=NO",
               # no path from the building machine (its user name) in the binaries that ship
               f"OTHER_CFLAGS=$(inherited) -ffile-prefix-map={runner.XCODE_DIR}=droidctl-runner",
               "DROIDCTL_TEAM=", f"DROIDCTL_BUNDLE_PREFIX={PLACEHOLDER_PREFIX}", f"DROIDCTL_BUILD_ID={bid}"]
        with open(logpath, "w") as f:
            p = subprocess.run(cmd, env=devices.xcode_env(), stdout=f, stderr=subprocess.STDOUT)
        if p.returncode != 0:
            raise UserError("building the runner failed", "device", hint=f"full log: {logpath}")
        products = os.path.join(dd, "Build", "Products")
        xr = next(f for f in os.listdir(products) if f.endswith(".xctestrun"))
        tmp = out + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(os.path.join(tmp, "Debug-iphoneos"))
        shutil.copytree(os.path.join(products, "Debug-iphoneos", APP), os.path.join(tmp, "Debug-iphoneos", APP),
                        symlinks=True)
        shutil.copy(os.path.join(products, xr), os.path.join(tmp, "runner.xctestrun"))
    strip(os.path.join(tmp, "Debug-iphoneos", APP))
    xcode = subprocess.run(["xcodebuild", "-version"], env=devices.xcode_env(), capture_output=True,
                           text=True).stdout.split("\n")[0].strip()
    with open(os.path.join(tmp, "manifest.json"), "w") as f:
        json.dump({"build": bid, "xcode": xcode, "stripped": True, "prefix": PLACEHOLDER_PREFIX}, f, indent=1)
    shutil.rmtree(out, ignore_errors=True)
    os.replace(tmp, out)
    return out


def make_zip(artifact, zip_path):
    """Pack an artifact directory (what ships in the package). Returns its sha256."""
    entries = []
    for base, dirs, files in os.walk(artifact):
        dirs.sort()
        entries += [os.path.join(base, n) for n in sorted(files)]
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for full in entries:
            info = zipfile.ZipInfo(os.path.relpath(full, artifact).replace(os.sep, "/"), ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if os.access(full, os.X_OK) else 0o644) << 16
            with open(full, "rb") as f:
                z.writestr(info, f.read())
    with open(zip_path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# --------------------------------------------------------------------------
# getting one
# --------------------------------------------------------------------------
def _unzip(zip_path, out):
    tmp = out + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    with zipfile.ZipFile(zip_path) as z:
        for n in z.namelist():
            if n.startswith("/") or ".." in n.split("/"):
                raise UserError(f"the runner zip has an unsafe path: {n}", "device")
        z.extractall(tmp)
    for base, _, files in os.walk(tmp):                     # zips drop the executable bit
        for name in files:
            p = os.path.join(base, name)
            with open(p, "rb") as f:
                if f.read(4) in (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"):
                    os.chmod(p, 0o755)
    if not valid(tmp):
        shutil.rmtree(tmp, ignore_errors=True)
        raise UserError("the runner zip is not a droidctl runner artifact", "device")
    shutil.rmtree(out, ignore_errors=True)
    os.replace(tmp, out)


def bundled_build(path=None):
    """The build id of the runner zip shipped in the package, or None. PURE-ish (reads the zip)."""
    try:
        with zipfile.ZipFile(path or BUNDLED) as z:
            return json.loads(z.read("manifest.json")).get("build")
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None


def ensure(say=lambda line: None):
    """The path of a usable artifact for this droidctl version."""
    src = os.environ.get("DROIDCTL_IOS_RUNNER")
    if src:
        if os.path.isdir(src):
            if not valid(src):
                raise UserError(f"DROIDCTL_IOS_RUNNER is not a runner artifact: {src}", "bad-args")
            return src
        out = artifact_dir("custom-" + hashlib.sha256(os.path.abspath(src).encode()).hexdigest()[:8])
        if not valid(out) or os.path.getmtime(src) > os.path.getmtime(out):
            _unzip(src, out)
        return out
    out = artifact_dir()
    if valid(out):
        return out
    if bundled_build() == build_id():
        _unzip(BUNDLED, out)
        return out
    if not devices.developer_dir():
        raise UserError("this droidctl has no runner built from its own sources", "missing-dep",
                        hint="reinstall droidctl, or on a Mac with Xcode run `make runner` in the repo; "
                             "DROIDCTL_IOS_RUNNER can point at a runner zip",
                        data={"reason": "no-runner", "shipped": bundled_build(), "wanted": build_id()})
    say("building the runner, unsigned (about 15 s)…")
    return build_unsigned(out)


def placeholder_ids(path):
    """(app bundle id, plugin bundle id) as built. PURE-ish (reads the plists)."""
    app = os.path.join(path, "Debug-iphoneos", APP)
    with open(os.path.join(app, "Info.plist"), "rb") as f:
        a = plistlib.load(f)["CFBundleIdentifier"]
    with open(os.path.join(app, PLUGIN, "Info.plist"), "rb") as f:
        p = plistlib.load(f)["CFBundleIdentifier"]
    return a, p
