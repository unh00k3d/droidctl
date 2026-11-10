"""Guided setup for an iPhone (IOS-INTEGRATION.md §4a).

An ordered checklist. Each step checks first and skips when the phone is already fine, so
re-running `setup` resumes where it stopped. When the user is needed it says what to do
and where, then (interactive) waits and carries on by itself once the condition holds,
or (non-interactive: --json, MCP, the daemon) stops with a typed error whose `data`
carries the step, the same instructions and the command to re-run.
"""
import os
import re
import subprocess
import time

from droidctl import device as dev
from droidctl.core import UserError
from droidctl.ios import devices, runner, transport

POLL_S = 1.0
STEP_TIMEOUT_S = 300.0


class Need(Exception):
    """The user has to do something before this step can pass."""

    def __init__(self, kind, message, instructions, reason):
        super().__init__(message)
        self.kind, self.instructions, self.reason = kind, instructions, reason


# --------------------------------------------------------------------------
# signing teams from the keychain (PURE parsers + one subprocess each)
# --------------------------------------------------------------------------
IDENTITY = re.compile(r'^\s*\d+\)\s+([0-9A-F]{40})\s+"((?:Apple Development|iPhone Developer)[^"]*)"', re.M)


def parse_identities(text):
    """`security find-identity -v -p codesigning` -> {sha1: name} of development identities."""
    return {m.group(1): m.group(2) for m in IDENTITY.finditer(text)}


def parse_cert_subjects(text):
    """`security find-certificate -a -Z -p …` piped per certificate through openssl -> handled by
    `teams()`; here: the (sha1, pem) pairs of the listing."""
    out, sha, pem = [], None, []
    for line in text.splitlines():
        if line.startswith("SHA-1 hash:"):
            sha = line.split(":", 1)[1].strip()
        elif line.startswith("-----BEGIN CERTIFICATE-----"):
            pem = [line]
        elif pem:
            pem.append(line)
            if line.startswith("-----END CERTIFICATE-----"):
                out.append((sha, "\n".join(pem) + "\n"))
                pem = []
    return out


def parse_subject(subject):
    """openssl's `subject=UID=…, CN=…, OU=TEAMID, O=Team Name, C=US` -> (team id, team name)."""
    ou = re.search(r"\bOU\s*=\s*([A-Z0-9]{10})\b", subject)
    o = re.search(r"\bO\s*=\s*([^,/]+)", subject)
    return (ou.group(1) if ou else None), (o.group(1).strip() if o else None)


def parse_xcode_teams(prefs):
    """Xcode's preferences (com.apple.dt.Xcode as a dict) -> [{team, name, free}] of the signed-in
    accounts' teams. Xcode records them at sign-in, before any certificate exists. PURE."""
    # Xcode can keep the team record while its account list is empty; xcodebuild then says "No Accounts"
    # (seen 2026-10-22, from Claude's shell and the user's Terminal alike, although Xcode's Settings still
    # showed the Apple ID). None = not recorded.
    lists = prefs.get("DVTDeveloperAccountManagerAppleIDLists")
    signed_in = None if lists is None else any(bool(v) for v in lists.values())
    out = {}
    for teams_ in (prefs.get("IDEProvisioningTeamByIdentifier") or {}).values():
        for t in teams_ or []:
            tid = t.get("teamID")
            if tid and tid not in out:
                out[tid] = {"team": tid, "name": t.get("teamName"), "free": bool(t.get("isFreeProvisioningTeam")),
                            "signed_in": signed_in}
    return list(out.values())


def xcode_teams():
    import plistlib
    try:
        raw = subprocess.run(["defaults", "export", "com.apple.dt.Xcode", "-"], capture_output=True, timeout=20).stdout
        return parse_xcode_teams(plistlib.loads(raw)) if raw else []
    except (OSError, subprocess.SubprocessError, ValueError):
        return []


def all_teams():
    """The teams of the accounts signed in to Xcode, plus any keychain-only ones (older setups)."""
    found = {t["team"]: dict(t) for t in xcode_teams()}
    for t in teams():
        found.setdefault(t["team"], {"team": t["team"], "name": t["name"], "free": None})
    return list(found.values())


def teams():
    """[{team, name, identity}] for every valid development identity in the keychain."""
    def run(cmd, inp=None):
        try:
            return subprocess.run(cmd, input=inp, capture_output=True, text=True, timeout=20).stdout
        except (OSError, subprocess.SubprocessError):
            return ""
    valid = parse_identities(run(["security", "find-identity", "-v", "-p", "codesigning"]))
    found = {}
    for sha, pem in parse_cert_subjects(run(["security", "find-certificate", "-a", "-Z", "-p"])):
        if sha not in valid:
            continue
        team, name = parse_subject(run(["openssl", "x509", "-noout", "-subject"], pem))
        if team and team not in found:
            found[team] = {"team": team, "name": name, "identity": valid[sha]}
    return list(found.values())


# --------------------------------------------------------------------------
# the steps: each returns a short "done" text or raises Need
# --------------------------------------------------------------------------
class Ctx:
    def __init__(self, udid, team, say, ask=None, secret=None, anisette=None):
        self.udid, self.team, self.say = udid, team, say
        self.anisette = anisette              # an anisette-v3 server URL (Linux)
        self.ask = ask                        # interactive only: ask(question) -> the user's answer
        self.secret = secret                  # interactive only: secret(question) -> typed without echo
        self.cdid = None
        self.info = {}
        self.helped = set()                   # things done once for the user (opened Xcode, a prompt, …)
        self.warnings = []                    # fine for now, but the user should act soon

    def once(self, key, fn):
        """Do `fn` the first time a step needs it (it is retried every second while waiting)."""
        if key in self.helped:
            return
        self.helped.add(key)
        try:
            fn()
        except Exception:                     # a convenience only: the instructions still say what to do
            pass


def step_tools(ctx):
    """Xcode on a Mac that has it; otherwise usbmuxd and go-ios (Linux, a Mac without Xcode)."""
    if devices.goios():
        return step_goios(ctx)
    return step_xcode(ctx)


PACKAGE_MANAGERS = [("apt-get", ["apt-get", "install", "-y", "usbmuxd"]),
                    ("dnf", ["dnf", "install", "-y", "usbmuxd"]),
                    ("pacman", ["pacman", "-S", "--needed", "--noconfirm", "usbmuxd"]),
                    ("zypper", ["zypper", "--non-interactive", "install", "usbmuxd"])]
# re-run udev's "add" for attached Apple devices: usbmuxd's rule then starts it, no replug needed
UDEV_TRIGGER = ["udevadm", "trigger", "--action=add", "--subsystem-match=usb", "--attr-match=idVendor=05ac"]


def usbmuxd_commands(which, root=False, installed=False):
    """The commands that install and start usbmuxd here (sudo unless root), or [] when droidctl
    doesn't know the package manager. PURE given `which`."""
    cmds = [] if installed else [c for name, c in PACKAGE_MANAGERS if which(name)][:1]
    if not installed and not cmds:
        return []
    cmds.append(UDEV_TRIGGER)
    return [c if root else ["sudo"] + c for c in cmds]


def _offer_usbmuxd(ctx):
    """Linux, in a terminal: offer to install/start usbmuxd with the user's sudo (asked once; the
    commands are shown first). droidctl itself never needs root: this is the one-time system part."""
    import shutil
    import sys
    if "usbmuxd" in ctx.helped or ctx.ask is None or not sys.platform.startswith("linux"):
        return
    ctx.helped.add("usbmuxd")
    installed = bool(shutil.which("usbmuxd") or os.path.exists("/usr/sbin/usbmuxd"))
    cmds = usbmuxd_commands(shutil.which, os.geteuid() == 0, installed)
    if not cmds:
        return
    what = "start usbmuxd" if installed else "install usbmuxd (the system service that connects iPhones over USB)"
    ctx.say(f"droidctl needs to {what}. It would run:")
    for c in cmds:
        ctx.say("    " + " ".join(c))
    if (ctx.ask("Run that now? (sudo may ask for your password) [Y/n] ") or "y").strip().lower() not in ("y", "yes"):
        return
    for c in cmds:
        if subprocess.run(c).returncode != 0:
            ctx.say("that failed; run the commands yourself, then setup carries on")
            return
    for _ in range(10):                       # udev starts usbmuxd a moment after the trigger
        if _usbmux_ok():
            return
        time.sleep(0.5)


def step_goios(ctx):
    from droidctl.ios import goios
    if not _usbmux_ok():
        _offer_usbmuxd(ctx)
    if not _usbmux_ok():
        raise Need("missing-dep", "usbmuxd is not running (it connects iPhones over USB)", [
            "Install it: Debian/Ubuntu: sudo apt install usbmuxd; Fedora: sudo dnf install usbmuxd; "
            "Arch: sudo pacman -S usbmuxd.",
            "Then reconnect the iPhone (its udev rule starts usbmuxd when an iPhone is plugged in)."], "usbmuxd")
    if goios.binary():
        return f"go-ios {goios.VERSION} (no Xcode needed)"
    if ctx.ask is None:
        raise Need("missing-dep", "go-ios is not installed (droidctl uses it for the iPhone without Xcode)", [
            "Run `droidctl setup --ios` in a terminal: it offers to download go-ios (MIT, about 17 MB, "
            "checksum-verified) into ~/.droidctl/bin.",
            "Or point DROIDCTL_GO_IOS at an `ios` binary you have (go-ios " + goios.VERSION + ")."], "go-ios")
    answer = (ctx.ask(f"iPhone support needs go-ios {goios.VERSION} (MIT, about 17 MB from GitHub, checksum-"
                      "verified, kept in ~/.droidctl/bin). Download it now? [Y/n] ") or "y").strip().lower()
    if answer not in ("y", "yes"):
        raise UserError("iPhone support needs go-ios", "missing-dep",
                        hint="run droidctl setup --ios again and accept the download, or set DROIDCTL_GO_IOS",
                        data={"reason": "go-ios"})
    goios.fetch(ctx.say)
    return f"go-ios {goios.VERSION} downloaded"


def step_xcode(ctx):
    d = devices.developer_dir()
    if not d:
        raise Need("missing-dep", "Xcode was not found", [
            "Install Xcode from the Mac App Store (free; about 10 GB), open it once and let it finish installing components.",
            "If it is installed somewhere unusual, set DROIDCTL_DEVELOPER_DIR to its Contents/Developer folder."],
            "no-xcode")
    try:
        p = subprocess.run(["xcodebuild", "-version"], env=devices.xcode_env(), capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise Need("missing-dep", f"xcodebuild does not run: {e}", ["Reinstall Xcode."], "xcodebuild")
    text = (p.stdout + p.stderr).strip()
    if p.returncode != 0:
        if "license" in text.lower():
            raise Need("missing-dep", "the Xcode license has not been accepted",
                       ["In a terminal, run: sudo xcodebuild -license accept"], "xcode-license")
        raise Need("missing-dep", f"xcodebuild fails: {text[:200]}",
                   ["In a terminal, run: xcodebuild -runFirstLaunch", "Then run droidctl setup again."], "xcode-first-launch")
    return text.splitlines()[0] if text else d


def step_attached(ctx):
    phones = [d["udid"] for d in transport.list_devices(timeout=3.0)] if _usbmux_ok() else []
    if ctx.udid is None:
        if len(phones) == 1:
            ctx.udid = phones[0]
        elif len(phones) > 1:
            raise UserError(f"{len(phones)} iPhones attached: {', '.join(phones)}", "no-device",
                            hint="pick one: droidctl setup -d <UDID>")
    if ctx.udid is None or ctx.udid not in phones:
        raise Need("no-device", "no iPhone is attached", [
            "Connect the iPhone to this computer with a USB cable.",
            "Unlock it. If it asks \"Trust This Computer?\", tap Trust and enter the passcode."], "not-attached")
    if not transport.is_paired(ctx.udid):
        d = None if devices.goios() else next((x for x in devices.list_devices() if x["udid"] == ctx.udid), None)
        if d is not None:                     # ask the iPhone to show "Trust This Computer?" now
            ctx.once("pair", lambda: subprocess.Popen(
                ["xcrun", "devicectl", "manage", "pair", "--device", d["cdid"], "-q"], env=devices.xcode_env(),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True))
        raise Need("no-device", "the iPhone has not trusted this computer yet", [
            "Unlock the iPhone and tap Trust on \"Trust This Computer?\", then enter the passcode.",
            "No prompt? Unplug and reconnect the cable while the iPhone is unlocked."], "not-paired")
    return ctx.udid


def _usbmux_ok():
    try:
        transport.list_devices(timeout=3.0)
        return True
    except UserError:
        return False


def step_developer_mode(ctx):
    d = next((x for x in devices.list_devices() if x["udid"] == ctx.udid), None)
    if d is None and devices.goios():
        raise Need("no-device", "the iPhone is not reachable", [
            "Keep it connected and unlocked; if it stays like this, unplug and reconnect the cable."], "not-reachable")
    if d is None:
        raise Need("no-device", "Xcode doesn't see the iPhone yet", [
            "Keep the iPhone unlocked and connected; Xcode prepares it the first time (this can take a minute).",
            "If it stays like this: open Xcode > Window > Devices and Simulators and select the iPhone."], "not-in-xcode")
    ctx.cdid = d["cdid"]
    ctx.info = d
    ver = d.get("os") or "0"
    if int(ver.split(".")[0] or 0) < 17:
        raise UserError(f"iOS {ver} is not supported (droidctl needs iOS 17 or later)", "unsupported")
    if devices.goios():
        from droidctl.ios import goios, lockdown
        ok, tested = goios.version_ok(ver)
        if not ok:
            raise UserError(f"iOS {ver} is not supported without Xcode (go-ios's tunnel needs iOS 17.4.1 or later)",
                            "unsupported", hint="update the iPhone, or use a Mac with Xcode")
        if not tested:
            ctx.warnings.append(f"iOS {ver} is newer than what droidctl has tested with go-ios {goios.VERSION}")
        if d.get("developer_mode") is None:
            d["developer_mode"] = "enabled" if lockdown.developer_mode(ctx.udid) else "disabled"
    if d.get("developer_mode") != "enabled":
        # the toggle is hidden until a Mac asks for it: show it (AMFI "reveal"; changes nothing else)
        ctx.once("reveal", lambda: reveal_developer_mode(ctx.udid))
        raise Need("device", "Developer Mode is off on the iPhone", [
            "On the iPhone: Settings > Privacy & Security > Developer Mode (at the bottom) > turn it On.",
            "The iPhone restarts. After the restart, unlock it and tap Turn On, then enter the passcode."],
            "developer-mode")
    return f"{d.get('model')} on iOS {ver}"


def _label(t):
    kind = {True: "free personal team", False: "paid team"}.get(t.get("free"), "team")
    return f"{t['team']} ({t.get('name') or '?'}, {kind})"


SIGN_IN = ("In Xcode (opened for you): Xcode menu > Settings… > Accounts > + > Apple ID, and sign in. "
           "Any Apple ID works; it doesn't need a paid developer membership.")
LOGIN_INSTEAD = ("Or skip Xcode's account: run `droidctl login` in a terminal (your Apple ID, its password and a 2FA "
                 "code, once); droidctl then gets and renews the signing profile itself.")
RESIGN_IN = ("In Xcode (opened for you): Xcode menu > Settings… > Accounts. Select your Apple ID: if it asks you to "
             "sign in, do; if it is listed but signing still fails, remove it (−) and add it again (+ > Apple ID).")


def _open_xcode(ctx):
    ctx.once("open-xcode", lambda: subprocess.Popen(["open", "-a", devices.developer_dir().split("/Contents/")[0]],
                                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))


def _profile_usable(team):
    """The runner built for the team has a profile that is valid and not yet due for renewal."""
    from droidctl.ios.manage import renew_due
    expires = profile_expiry(team) or (cached_profile(team) or {}).get("expires")
    return bool(expires) and not renew_due(expires)


def _signed_out(ctx, team):
    """Xcode's command-line builds see no Apple ID for the team. While the runner's profile is valid
    and not due, builds still sign with Xcode's cached copy of it (2026-10-26: a rebuild after a
    source change); anything that needs a new profile (a first build, a renewal) can't."""
    expires = profile_expiry(team) or (cached_profile(team) or {}).get("expires")
    if _profile_usable(team):
        ctx.warnings.append(f"Xcode's command-line builds see no Apple ID, so the runner can't be renewed: fix it "
                            f"before {expires} (Xcode > Settings > Accounts: sign in again, or remove and re-add "
                            "the Apple ID)")
        return f"{team} (Xcode sees no Apple ID; the runner works until {expires})"
    _open_xcode(ctx)
    raise Need("signing", "Xcode's command-line builds see no Apple ID, so they can't sign the runner", [
        RESIGN_IN, "Then droidctl renews the signing profile itself.", LOGIN_INSTEAD], "signed-out")


WRONG_PASSWORD = -22406


def step_apple(ctx):
    """droidctl's own Apple sign-in: it gets and renews the runner's profile without Xcode's
    account. Asked once, in the terminal; a saved sign-in that still works passes silently."""
    from droidctl.ios import provision
    if os.environ.get("DROIDCTL_IOS_SIGNING") == "xcode":
        return "skipped (DROIDCTL_IOS_SIGNING=xcode: Xcode's account signs)"
    rec = provision.record()
    if rec and rec.get("team") and (not ctx.team or ctx.team == rec["team"]):
        try:
            provision.session()[0].teams()
            return f"{rec.get('apple_id')}, team {rec.get('team_name') or rec['team']}"
        except UserError as e:
            if (e.data or {}).get("reason") not in ("token-expired", "no-login"):
                raise
            ctx.say("the saved Apple sign-in has expired: signing in again")
    if ctx.ask is None or ctx.secret is None:
        raise Need("signing", "droidctl is not signed in to Apple", [
            "Run `droidctl setup --ios` in a terminal: it asks for your Apple ID and its password once "
            "(a free Apple ID works). The password is used only in Apple's SRP sign-in and never stored."], "no-login")
    xcode = not devices.goios()
    ctx.say("droidctl signs and renews its iPhone runner itself, with your Apple ID (a free one works; a "
            "secondary Apple ID is wiser). The password goes only into Apple's SRP sign-in; droidctl keeps "
            "Apple's token in the keyring, never the password."
            + (" Leave the Apple ID empty to use Xcode's account instead." if xcode else ""))
    url = _anisette_url(ctx, rec)
    known = (rec or {}).get("apple_id")
    apple_id = (ctx.ask(f"Apple ID{f' [{known}]' if known else ''}: ") or "").strip() or known
    if not apple_id:
        if xcode:
            return "skipped: Xcode's account signs the runner"
        raise Need("signing", "without Xcode, droidctl needs your Apple ID to sign its runner", [
            "Enter the Apple ID (and its password) when asked."], "no-login")
    for attempt in range(3):
        try:
            out = provision.login(apple_id, lambda: ctx.secret("Password (not shown): "), ask_code=ctx.ask,
                                  team=ctx.team, ask=ctx.ask, say=ctx.say, anisette_url=url, fresh=attempt > 0)
            break
        except UserError as e:
            from droidctl.ios.apple import anisette
            other = {anisette.PUBLIC: anisette.ALTERNATE, anisette.ALTERNATE: anisette.PUBLIC}.get(url)
            if (e.data or {}).get("reason") == "anisette-rejected" and other and attempt < 2:
                # Apple refused a public server's data before checking the password: try the other one
                ctx.say(f"Apple refused the anisette server {url}; trying {other}.")
                url = ctx.anisette = other
                continue
            if (e.data or {}).get("code") != WRONG_PASSWORD or attempt == 2:
                raise
            ctx.say("Apple says the Apple ID or password is wrong; try again.")
    ctx.team = out["team"]
    return f"{out['apple_id']}, team {out['team_name']} ({out['team']})"


def _anisette_url(ctx, rec):
    """The anisette server for Apple's sign-in: --anisette, DROIDCTL_ANISETTE, last time's; on a Mac
    none (the Mac's own). On Linux, asked (a self-hosted one by default) and checked."""
    import sys
    from droidctl.ios.apple import anisette
    url = anisette.current(ctx.anisette or os.environ.get("DROIDCTL_ANISETTE") or (rec or {}).get("anisette"))
    if not url and sys.platform == "darwin":
        return None
    if not url:
        url = anisette.choose_server(ctx.ask, ctx.say)
    try:
        anisette.V3Anisette(url).client_info()
    except UserError as e:
        raise Need("signing", f"no anisette server answers at {url}", [
            f"Pass another: droidctl setup --ios --anisette <URL> (public: {anisette.PUBLIC} or "
            f"{anisette.ALTERNATE}; or your own anisette-v3 server).", f"Detail: {e}"],
            "no-anisette")
    ctx.anisette = url
    return url


def step_team(ctx):
    from droidctl.ios import provision
    own = provision.signed_in_team()
    if own and (not ctx.team or ctx.team == own):
        ctx.team = own
        rec = provision.record() or {}
        return f"{rec.get('team_name') or own} ({own}), droidctl's Apple sign-in"
    if devices.goios():
        raise Need("signing", "droidctl is not signed in to Apple (without Xcode it signs the runner itself)", [
            "Run `droidctl setup --ios` in a terminal and enter your Apple ID when asked."], "no-login")
    found = all_teams()
    if ctx.team:
        if found and ctx.team not in {t["team"] for t in found}:
            raise UserError(f"team {ctx.team} is not signed in to Xcode on this Mac", "signing",
                            hint="teams found: " + ", ".join(_label(t) for t in found))
        if any(t["team"] == ctx.team and t.get("signed_in") is False for t in found):
            return _signed_out(ctx, ctx.team)
        return ctx.team
    # all signed out: still fine for a team whose cached profile is valid (`teardown` forgets the phone's team,
    # 2026-10-26); _signed_out below says so with a warning
    if not found or all(t.get("signed_in") is False and not _profile_usable(t["team"]) for t in found) \
            and not dev.device_state(ctx.udid).get("team"):
        _open_xcode(ctx)
        raise Need("signing", "no Apple ID is signed in to Xcode on this Mac", [
            SIGN_IN, "That's all: droidctl creates the signing certificate and profile itself.", LOGIN_INSTEAD],
            "no-team")
    if len(found) > 1:
        if ctx.ask:
            ctx.say("Xcode has more than one team; which one signs droidctl's runner?")
            for i, t in enumerate(found, 1):
                ctx.say(f"    {i}. {_label(t)}")
            pick = (ctx.ask(f"team [1-{len(found)}]: ") or "").strip()
            if pick.isdigit() and 1 <= int(pick) <= len(found):
                ctx.team = found[int(pick) - 1]["team"]
                return _label(found[int(pick) - 1])
        raise UserError("more than one signing team on this Mac", "bad-args",
                        hint="pick one: droidctl setup --team <ID>  (" + ", ".join(_label(t) for t in found) + ")",
                        data={"teams": found})
    ctx.team = found[0]["team"]
    if found[0].get("signed_in") is False:
        return _signed_out(ctx, ctx.team)
    return _label(found[0])


def step_unlocked(ctx):
    if devices.goios():
        # lockdown's PasswordProtected may mean "a passcode is set" rather than "locked now" (unverified):
        # don't wait on it; a locked phone shows up when the runner starts (runner._explain_refused)
        return "keep the iPhone unlocked until setup finishes"
    st = devices.lock_state(ctx.cdid)
    if st["locked"]:
        raise Need("screen-off", "the iPhone is locked", [
            "Unlock the iPhone and keep it unlocked until setup finishes (it can't start the runner while locked)."],
            "locked")
    return "unlocked"


def step_build(ctx):
    want = runner.build_id(ctx.team)
    st = dev.device_state(ctx.udid)
    renew = runner.renewal_due(ctx.udid, ctx.team)
    if st.get("built") == want and runner.xctestrun(ctx.team) and not renew:
        return "already built"
    if renew:
        ctx.say(f"the runner's profile expires {profile_expiry(ctx.team)}: re-signing it…")
    elif provision_mode(ctx.team) == "apple-id":
        ctx.say("preparing the runner: profile from Apple, then signing…")
    else:
        ctx.say("building the runner (the first build takes about a minute)…")
    try:
        if renew:
            old, new = runner.resign(ctx.udid, ctx.team)
        else:
            runner.build(ctx.udid, ctx.team)
    except UserError as e:
        if e.kind == "screen-off":
            raise Need("screen-off", str(e), [e.hint], (e.data or {}).get("reason", "locked"))
        if e.kind == "signing":
            raise Need("signing", str(e), [e.hint or "Open Xcode > Settings > Accounts and check the Apple ID."],
                       (e.data or {}).get("reason", "signing"))
        raise
    dev.update_device_state(ctx.udid, backend="ios", team=ctx.team, built=want)
    if renew:
        if runner.renewed(old, new):
            ctx.info["renewed"] = True
            return f"build {want}, re-signed (new profile expires {new['expires']})"
        return (f"build {want}; Xcode kept the current profile (expires {(old or {}).get('expires')}): "
                "it issues a new one only after that, and droidctl re-signs then")
    return f"build {want}"


def provision_mode(team):
    from droidctl.ios import provision
    return provision.mode(team)


def is_old_runner(bundle, team):
    """An earlier droidctl product runner (another team): ours to remove. PURE. Only the product's
    own bundle scheme: research builds (dev.droidctl.s1.*) are the user's to keep."""
    from droidctl.ios.manage import ours
    return ours(bundle) and bundle != runner.runner_bundle(team)


def step_start(ctx):
    try:
        try:
            info = runner.start(ctx.udid, ctx.team)
        except UserError as e:
            old = [b for b in (e.data or {}).get("apps", []) if is_old_runner(b, ctx.team)]
            if (e.data or {}).get("reason") != "app-limit" or not old:
                raise
            for b in old:                      # only droidctl's own earlier runners, never other apps
                ctx.say(f"removing an earlier droidctl runner to free a slot: {b}")
                devices.uninstall(ctx.cdid, b)
            info = runner.start(ctx.udid, ctx.team)
    except UserError as e:
        if (e.data or {}).get("reason") == "app-limit":
            apps = (e.data or {}).get("apps", [])
            raise Need("signing", str(e), [
                "A free Apple team can have only 3 apps installed on one iPhone. Installed now: " + ", ".join(apps) + ".",
                "Remove one you no longer need from the iPhone (touch and hold its icon > Remove App)."], "app-limit")
        if e.kind == "not-trusted":
            ctx.once("trust-page", lambda: open_settings(ctx.cdid, TRUST_PAGE))
            raise Need("not-trusted", "the iPhone doesn't trust your developer certificate yet", [
                ("On the iPhone (Settings is opened there for you): General > VPN & Device Management."
                 if devices.goios() else
                 "On the iPhone (Settings is opened there for you): Settings > General > VPN & Device Management."),
                "Under Developer App, tap your Apple ID, then Trust \"Apple Development: …\" and confirm."], "untrusted")
        if e.kind == "screen-off":
            raise Need("screen-off", str(e), [e.hint], (e.data or {}).get("reason", "locked-at-start"))
        raise
    ctx.info["runner"] = info
    return f"runner {info.get('version')} answering (build {info.get('build')})"


TRUST_PAGE = "prefs:root=General&path=ManagedConfigurationList"


def open_settings(cdid, url):
    """Open the iPhone's Settings app (needs Developer Mode, which is on by then). With Xcode, at
    the page `url` names; go-ios can't pass a URL to an app it launches, so there Settings opens at
    its main screen (`--kill-existing`: not on a page left open earlier)."""
    devices.launch(cdid, "com.apple.Preferences", terminate_existing=True) if devices.goios() else \
        devices.devicectl("device", "process", "launch", "--device", cdid, "--terminate-existing",
                          "--payload-url", url, "com.apple.Preferences", timeout=60)


def reveal_developer_mode(udid):
    """Make Settings > Privacy & Security show the Developer Mode switch (lockdown's AMFI service,
    action 0 = reveal; it turns nothing on). One short lockdown session."""
    from droidctl.ios import lockdown
    lockdown.reveal_developer_mode(udid)


STEPS = [("tools", "Xcode, or go-ios without it", step_tools), ("attached", "iPhone connected and trusted", step_attached),
         ("developer-mode", "Developer Mode", step_developer_mode), ("apple", "Apple sign-in", step_apple),
         ("team", "signing team", step_team),
         ("unlocked", "iPhone unlocked", step_unlocked), ("build", "build and sign the runner", step_build),
         ("start", "start the runner (installs it)", step_start)]


def run(udid=None, team=None, interactive=False, say=print, rerun=None, ask=None, secret=None, anisette=None):
    """Run the checklist. Returns a summary payload; raises UserError with data.step/instructions."""
    ctx = Ctx(udid, team, say, ask if interactive else None, secret if interactive else None, anisette)
    done = []
    if interactive:
        tools = "go-ios (it offers to download it)" if devices.goios() else "Xcode installed"
        say(f"Setting up droidctl for the iPhone. The first time it needs, from you: {tools}, your Apple ID "
            "and its password once (a free one works), and a few taps on the iPhone. It says when, and carries on "
            "by itself; signing, renewing, installing and starting are done for you.")
    for n, (key, title, fn) in enumerate(STEPS, 1):
        title = f"[{n}/{len(STEPS)}] {title}"
        t0 = time.monotonic()
        announced = False
        while True:
            try:
                detail = fn(ctx)
                break
            except Need as n:
                if not interactive:
                    raise UserError(f"setup needs you: {n}", n.kind,
                                    hint=" ".join(n.instructions) + f"  Then run: {rerun or 'droidctl setup'}",
                                    data={"step": key, "reason": n.reason, "instructions": n.instructions,
                                          "rerun": rerun or "droidctl setup", "done": done})
                if not announced:
                    say(f"→ {title}: {n}")
                    for i, line in enumerate(n.instructions, 1):
                        say(f"    {i}. {line}")
                    say("    (waiting; this continues by itself. Ctrl-C to stop)")
                    announced = True
                if time.monotonic() - t0 > STEP_TIMEOUT_S:
                    raise UserError(f"setup gave up waiting for: {title}", "timeout",
                                    hint=f"run {rerun or 'droidctl setup'} again when it's done",
                                    data={"step": key, "reason": n.reason, "instructions": n.instructions})
                time.sleep(POLL_S)
        say(f"✓ {title}: {detail}")
        done.append(key)
    for w in ctx.warnings:
        say(f"! {w}")
    expiry = profile_expiry(ctx.team)
    return {"ok": True, "udid": ctx.udid, "team": ctx.team, "model": ctx.info.get("model"), "ios": ctx.info.get("os"),
            "runner": ctx.info.get("runner", {}).get("build"), "profile_expires": expiry,
            "profile_renewed": bool(ctx.info.get("renewed")), "steps": done, "warnings": ctx.warnings,
            "next": f"droidctl snapshot -d {ctx.udid}"}


def profile(team):
    """The built runner's provisioning profile as {uuid, expires (ISO)}, or None."""
    import glob
    from droidctl.ios import sign
    apps = glob.glob(f"{runner.derived_data(team)}/Build/Products/*-iphoneos/*-Runner.app/embedded.mobileprovision")
    if not apps:
        return None
    try:
        with open(apps[0], "rb") as f:
            doc = sign.decode_profile(f.read())
    except (OSError, ValueError, UserError):
        return None
    exp = doc.get("ExpirationDate")
    return {"uuid": doc.get("UUID"), "expires": exp.isoformat() + "Z" if exp else None}


XCODE_PROFILES = ("~/Library/Developer/Xcode/UserData/Provisioning Profiles", "~/Library/MobileDevice/Provisioning Profiles")


def cached_profile(team):
    """The latest profile in Xcode's own cache for the team's runner, as {uuid, expires}, or None: a build
    in a new droidctl home signs with it while Xcode's command-line builds see no Apple ID."""
    import glob
    import plistlib
    want = {f"{team}.{runner.runner_bundle(team)}", f"{team}.*"}
    best = None
    for d in XCODE_PROFILES:
        for f in glob.glob(os.path.join(os.path.expanduser(d), "*.mobileprovision")):
            try:
                doc = plistlib.loads(subprocess.run(["security", "cms", "-D", "-i", f], capture_output=True,
                                                    timeout=20).stdout)
            except Exception:
                continue
            exp = doc.get("ExpirationDate")
            if exp and (doc.get("Entitlements") or {}).get("application-identifier") in want and \
                    (best is None or exp.isoformat() + "Z" > best["expires"]):
                best = {"uuid": doc.get("UUID"), "expires": exp.isoformat() + "Z"}
    return best


def profile_expiry(team):
    """The runner's provisioning profile expiry (ISO), from the built app, or None."""
    return (profile(team) or {}).get("expires")
