"""The iOS runner's lifecycle: build (signed with the user's team), start, adopt, stop.

The runner is an XCUITest bundle; `xcodebuild test-without-building` installs it and keeps
it alive, so a detached xcodebuild process (the "session holder") is what keeps the
runner running (IOS.md §5). Without Xcode the holder is go-ios `runtest`, after droidctl
installs the signed runner itself (`goios.py`). Everything here is driven by `droidctl setup` and by
`connect()`, which adopts a running runner or starts an installed one.
"""
import glob
import hashlib
import os
import re
import shutil
import signal
import subprocess
import time

from droidctl import device as dev
from droidctl.core import UserError
from droidctl.ios import devices
from droidctl.ios.client import DEFAULT_PORT, IosClient

XCODE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xcode")
PROJECT = os.path.join(XCODE_DIR, "DroidctlRunner.xcodeproj")
SCHEME = "DroidctlRunner"
TEST = "DroidctlRunner/DroidctlRunner/testServe"
START_TIMEOUT = 120.0


def ios_home():
    return os.path.join(dev.home(), "ios")


def source_hash():
    """sha256 over the runner's sources (project + .m files), so a changed runner is rebuilt."""
    h = hashlib.sha256()
    files = sorted(glob.glob(os.path.join(XCODE_DIR, "**", "*"), recursive=True))
    for f in files:
        if os.path.isfile(f) and (f.endswith(".m") or f.endswith(".pbxproj") or f.endswith(".xcscheme")):
            h.update(os.path.relpath(f, XCODE_DIR).encode())
            with open(f, "rb") as fh:
                h.update(fh.read())
    return h.hexdigest()


def build_id(team):
    from droidctl.ios import prebuilt, provision
    if provision.mode(team) == "apple-id":
        return prebuilt.build_id()
    return hashlib.sha256(f"{source_hash()}:{team}".encode()).hexdigest()[:12]


def bundle_prefix(team):
    """Bundle ids must be unique per team (a generic one fails on personal teams)."""
    return "dev.droidctl.r" + hashlib.sha1(team.encode()).hexdigest()[:8]


def runner_bundle(team):
    return bundle_prefix(team) + ".runner.xctrunner"


def derived_data(team):
    return os.path.join(ios_home(), "build", team)


def session_dir(udid):
    return os.path.join(ios_home(), udid)


def xctestrun(team):
    found = sorted(glob.glob(os.path.join(derived_data(team), "Build", "Products", "*.xctestrun")))
    return found[0] if found else None


# --------------------------------------------------------------------------
# errors from xcodebuild's output (PURE)
# --------------------------------------------------------------------------
TRUST_HINT = ("on the iPhone: Settings > General > VPN & Device Management > your Apple Development "
              "profile > Trust")


def classify_output(text):
    """The first recognised failure in xcodebuild's build or session output, as
    (kind, message, hint, reason), or None. Wording verified on Xcode 26.2 / iOS 18.7.8
    where noted; the rest follows the reports in research/ios*.md."""
    rules = [
        # verified 2026-10-21: the phone sat on a passcode prompt
        (r"Timed out while enabling automation mode", "screen-off",
         "the iPhone didn't let the runner start (it is locked or waiting for its passcode)",
         "unlock the iPhone, enter the passcode if it asks, and keep the screen on", "locked-at-start"),
        (r"Unlock .{0,40} to Continue|is locked|passcode protected", "screen-off",
         "the iPhone is locked", "unlock the iPhone and keep it unlocked while the runner starts", "locked-at-start"),
        (r"not been explicitly trusted|Untrusted Developer|not trusted", "not-trusted",
         "the developer profile is not trusted on the iPhone", TRUST_HINT, "untrusted"),
        (r"profile has expired|provisioning profile .{0,80}expired|certificate has expired", "profile-expired",
         "the runner's provisioning profile has expired", "run: droidctl setup (it re-signs the runner)", "expired"),
        (r"[Dd]eveloper [Mm]ode .{0,40}(disabled|not enabled|turned off)", "device",
         "Developer Mode is off on the iPhone",
         "on the iPhone: Settings > Privacy & Security > Developer Mode > On, then restart it", "developer-mode"),
        # verified 2026-10-22: Xcode's account list was empty (its team record stayed), in any shell
        (r"error: No Accounts", "signing", "Xcode's command-line builds see no Apple ID, so they can't sign the runner",
         "open Xcode > Settings > Accounts: sign in again, or remove and re-add the Apple ID; then run: droidctl setup",
         "signed-out"),
        (r"No Account for Team|No signing certificate|requires a development team|No profiles for",
         "signing", "Xcode can't sign the runner for this team",
         "open Xcode > Settings > Accounts, sign in with your Apple ID, then run: droidctl setup", "signing"),
        (r"(?:Failed Registering Bundle Identifier|is not available|cannot be registered)", "signing",
         "the runner's bundle identifier can't be registered for this team", "run: droidctl setup", "bundle-id"),
        # verified 2026-10-21 (iOS 18.7.8): a free team can have 3 apps installed on a device
        (r"maximum number of installed apps using a free developer profile|maximum number of apps|App ID limit",
         "signing", "the iPhone already has 3 apps from this free Apple team (the limit)",
         "remove one of them from the iPhone (touch and hold its icon > Remove App), then run: droidctl setup",
         "app-limit"),
    ]
    from droidctl.ios.goios import RULES
    for pat, kind, msg, hint, reason in RULES + rules:
        m = re.search(pat, text)
        if m:
            return kind, msg, hint, reason
    m = re.search(r"encountered an error \((.+)\)", text)
    if m:
        return "device", f"the runner failed to start: {m.group(1)}", "", "runner-error"
    return None


def app_limit_bundles(text):
    """The bundle ids a free-team app-limit error lists ("TEAMID.bundle.id"). PURE."""
    seen = []
    for b in re.findall(r'"[A-Z0-9]{10}\.([A-Za-z0-9._-]+)"', text):
        if b not in seen:
            seen.append(b)
    return seen


def raise_for_output(text, default=None):
    got = classify_output(text)
    if got:
        kind, msg, hint, reason = got
        data = {"reason": reason}
        if reason == "app-limit":
            data["apps"] = app_limit_bundles(text)
        raise UserError(msg, kind, hint=hint, data=data)
    if default:
        raise UserError(default, "device")


# --------------------------------------------------------------------------
# build / start / stop
# --------------------------------------------------------------------------
def build(udid, team, log=None, resign=False):
    """xcodebuild build-for-testing, signed with `team`. Returns the build id. `resign` drops the
    built products first, so the app is signed again (expiry renewal; Xcode keeps a still-valid
    cached profile, see renewal_due). With droidctl's own Apple sign-in for the team, the
    prebuilt runner is signed instead (no Xcode account involved; a fresh profile every time)."""
    from droidctl.ios import provision
    if provision.mode(team) == "apple-id":
        name = None
        try:
            from droidctl.ios import lockdown
            name = lockdown.info(udid).get("name")
        except UserError:
            pass
        return provision.provision_and_sign(udid, team, name)["build"]
    if devices.goios():
        raise UserError("without Xcode, droidctl signs the runner with its own Apple sign-in", "signing",
                        hint="run: droidctl setup --ios (in a terminal: it asks for your Apple ID once)",
                        data={"reason": "no-login"})
    env = devices.xcode_env()
    bid = build_id(team)
    os.makedirs(ios_home(), exist_ok=True)
    if resign:
        shutil.rmtree(os.path.join(derived_data(team), "Build", "Products"), ignore_errors=True)
    cmd = ["xcodebuild", "build-for-testing", "-project", PROJECT, "-scheme", SCHEME,
           "-destination", f"platform=iOS,id={udid}", "-derivedDataPath", derived_data(team),
           "-allowProvisioningUpdates", f"DROIDCTL_TEAM={team}", f"DROIDCTL_BUNDLE_PREFIX={bundle_prefix(team)}",
           f"DROIDCTL_BUILD_ID={bid}"]
    logpath = os.path.join(ios_home(), "build.log")
    from droidctl.ios import syslog
    with syslog.xcode_busy(udid), open(logpath, "w") as out:
        p = subprocess.run(cmd, env=env, stdout=out, stderr=subprocess.STDOUT)
    if p.returncode != 0:
        with open(logpath, errors="replace") as f:
            text = f.read()
        raise_for_output(text)
        errors = [ln.strip() for ln in text.splitlines() if "error:" in ln][:3]
        raise UserError("building the runner failed: " + ("; ".join(errors) or f"see {logpath}"), "signing"
                        if any("sign" in e.lower() or "provision" in e.lower() for e in errors) else "device",
                        hint=f"full log: {logpath}")
    return bid


def holder_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def start(udid, team, port=DEFAULT_PORT, timeout=START_TIMEOUT):
    """Start the runner with a detached xcodebuild session; wait for its ping with our build id.
    Log streams let go meanwhile: Xcode can't connect to the iPhone while one is open."""
    from droidctl.ios import syslog
    with syslog.xcode_busy(udid):
        return _start(udid, team, port, timeout)


def _start(udid, team, port, timeout):
    xr = xctestrun(team)
    if not xr:
        raise UserError("the runner is not built for this team", "not-installed", hint=f"run: droidctl setup -d {udid}")
    stop(udid, quiet=True)
    os.makedirs(session_dir(udid), exist_ok=True)
    logpath = os.path.join(session_dir(udid), "session.log")
    if devices.goios():
        cmd, env = _goios_holder(udid, team, port, logpath)
    else:
        cmd = ["xcodebuild", "test-without-building", "-xctestrun", xr, "-destination", f"platform=iOS,id={udid}",
               f"-only-testing:{TEST}"]
        env = dict(devices.xcode_env(), TEST_RUNNER_DROIDCTL_PORT=str(port))
    with open(logpath, "w") as out:
        p = subprocess.Popen(cmd, env=env, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=True)
    dev.update_device_state(udid, backend="ios", port=port, team=team, holder_pid=p.pid,
                            build_id=build_id(team), bundle=runner_bundle(team))
    try:
        return _wait_ready(udid, p, logpath, build_id(team), timeout)
    except UserError as e:
        if devices.goios() and (e.data or {}).get("reason") == "launch-refused":
            _explain_refused(udid, team)
        raise


def _goios_holder(udid, team, port, logpath):
    """go-ios: tunnel, developer disk image, the signed runner installed; then `runtest`."""
    from droidctl.ios import goios, lockdown
    goios.ensure_agent(udid)
    goios.ensure_ddi(udid)
    app = built_app(team)
    stamp = _install_stamp(team)
    if dev.device_state(udid).get("installed") != stamp or not lockdown.app(udid, runner_bundle(team)):
        goios.install(udid, app)
        dev.update_device_state(udid, installed=stamp)
    # go-ios's own log (the errors) goes to the session log; the runner's output to its own file
    return goios.runtest_cmd(udid, runner_bundle(team), port, os.path.join(session_dir(udid), "runner.log")), None


def built_app(team):
    found = sorted(glob.glob(os.path.join(derived_data(team), "Build", "Products", "*-iphoneos", "*-Runner.app")))
    return found[0] if found else None


def _install_stamp(team):
    """What is installed: the build and its profile (a re-sign installs again)."""
    from droidctl.ios import setup
    return f"{build_id(team)}:{(setup.profile(team) or {}).get('uuid')}"


def _explain_refused(udid, team):
    """go-ios drops the phone's reason for a refused launch: tell locked, expired and untrusted
    apart ourselves (research/ios-linux.md §3.4)."""
    from droidctl.ios import lockdown, manage, setup
    exp = setup.profile_expiry(team)
    if exp and manage.expired(exp):
        raise UserError("the runner's provisioning profile has expired", "profile-expired",
                        hint="run: droidctl setup (it re-signs the runner)", data={"reason": "expired"})
    # PasswordProtected false surely means "not locked"; true may only mean "has a passcode"
    # (unverified), so then the combined "locked or untrusted" message stands
    try:
        if lockdown.locked(udid) is False:
            raise UserError("the iPhone doesn't trust your developer certificate yet", "not-trusted",
                            hint=TRUST_HINT, data={"reason": "untrusted"})
    except UserError as e:
        if e.kind == "not-trusted":
            raise


def _wait_ready(udid, p, logpath, want, timeout):
    """Poll the runner's ping until it answers with our build; read the holder's output meanwhile."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            c = IosClient(udid, dev.device_state(udid).get("port", DEFAULT_PORT), timeout=2.0)
            try:
                info = c.call("ping", timeout=3.0)
            finally:
                c.close()
            if info.get("build") == want:
                return info
            raise UserError(f"a stale runner answered (build {info.get('build')}, expected {want})", "device",
                            hint=f"run: droidctl setup -d {udid}", data={"reason": "stale-build"})
        except UserError as e:
            if e.kind != "connection":
                raise
        if p.poll() is not None:
            with open(logpath, errors="replace") as f:
                raise_for_output(f.read(), default=f"the runner exited during start (see {logpath})")
        with open(logpath, errors="replace") as f:
            got = classify_output(f.read())
        if got and got[0] != "device":
            stop(udid, quiet=True)
            with open(logpath, errors="replace") as f:
                raise_for_output(f.read())
        time.sleep(0.5)
    stop(udid, quiet=True)
    raise UserError(f"the runner did not answer within {timeout:g}s", "timeout", hint=f"see {logpath}")


def stop(udid, quiet=False):
    """Ask the runner to quit, then end its session holder."""
    st = dev.device_state(udid)
    try:
        c = IosClient(udid, st.get("port", DEFAULT_PORT), timeout=1.0)
        try:
            c.call("quit", timeout=2.0)
        finally:
            c.close()
    except UserError:
        pass
    pid = st.get("holder_pid")
    if holder_alive(pid):
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            pass
    if st:
        dev.update_device_state(udid, holder_pid=None)
    return {"stopped": True}


def renewed(old, new):
    """Whether a re-sign brought a new profile. PURE."""
    return bool(old and new and new.get("uuid") and new["uuid"] != old.get("uuid"))


def renewal_due(udid, team):
    """A free team's profile lasts 7 days. Re-sign when it ends within a day, but only once per
    profile while it is still valid: Xcode keeps a valid cached profile (measured 2026-10-22: a
    re-sign 6 days before expiry embedded the same profile), so retrying on every command would
    only rebuild for nothing. Once expired, every start re-signs until a new profile comes."""
    from droidctl.ios import manage, setup
    prof = setup.profile(team)
    if not prof or not manage.renew_due(prof.get("expires")):
        return False
    if manage.expired(prof["expires"]):
        return True
    return dev.device_state(udid).get("resign_tried") != prof.get("uuid")


def resign(udid, team):
    """Re-sign the runner: (old, new) profiles. An expired profile that Xcode didn't replace is
    `profile-expired` (the runner can't start with it)."""
    from droidctl.ios import manage, setup
    old = setup.profile(team)
    build(udid, team, resign=True)
    new = setup.profile(team)
    dev.update_device_state(udid, built=build_id(team), resign_tried=(old or {}).get("uuid"))
    if not renewed(old, new) and manage.expired((new or old or {}).get("expires")):
        raise UserError("the runner's provisioning profile has expired and Xcode did not issue a new one",
                        "profile-expired",
                        hint="run: droidctl login (droidctl then renews the profile itself, without Xcode); or open "
                             "Xcode > Settings > Accounts, sign in again, and run: droidctl setup",
                        data={"reason": "expired"})
    return old, new


def renew_if_due(udid, team):
    """Before starting the runner: re-sign it when its profile is (nearly) out, so nobody has to
    remember the weekly free-team renewal."""
    if renewal_due(udid, team):
        return resign(udid, team)
    return None


def connect(udid, timeout=5.0):
    """(IosClient, ping): adopt a running runner, or start the installed one."""
    st = dev.device_state(udid)
    port = st.get("port", DEFAULT_PORT)
    try:
        c = IosClient(udid, port, timeout=timeout)
        try:
            info = c.call("ping", timeout=timeout)
        except UserError:
            c.close()
            raise
        if st.get("build_id") and info.get("build") != st["build_id"]:
            c.close()
            if not st.get("team"):
                raise UserError("an unknown runner build is running", "device", hint=f"run: droidctl setup -d {udid}")
            start(udid, st["team"], port)
            return connect(udid, timeout)
        return c, info
    except UserError as e:
        if e.kind != "connection":
            raise
    if not st.get("team") or not xctestrun(st["team"]):
        raise UserError("the droidctl runner is not set up on this iPhone", "not-installed",
                        hint=f"run: droidctl setup -d {udid}")
    renew_if_due(udid, st["team"])
    start(udid, st["team"], port)
    c = IosClient(udid, port, timeout=timeout)
    return c, c.call("ping", timeout=timeout)
