"""`teardown` and `doctor` for an iPhone (IOS.md §5.5, IOS-INTEGRATION.md §6).

teardown: stop the runner, uninstall droidctl's runner apps (never anything else), forget the
phone's state, and say plainly what it does not undo. doctor: the setup checklist read-only,
plus the runner, the profile's expiry, the Xcode/iOS pair and the Mac's sleep and thermal state.
"""
import datetime
import re
import subprocess
import time

from droidctl import device as dev
from droidctl.core import UserError
from droidctl.ios import devices, runner, setup
from droidctl.ios.client import IosClient

# droidctl's product runners: "dev.droidctl.r" + 8 hex (runner.bundle_prefix) + ".runner.xctrunner"
RUNNER_BUNDLE = re.compile(r"^dev\.droidctl\.r[0-9a-f]{8}\.runner\.xctrunner$")
RENEW_WITHIN = datetime.timedelta(hours=24)
# Xcode / iOS pairs droidctl's private calls were verified on (IOS.md §19)
TESTED = {("26.2", "18")}


def ours(bundle):
    """A droidctl runner (any team): teardown's to remove. PURE."""
    return bool(RUNNER_BUNDLE.match(bundle or ""))


def disclosures(trust_note):
    return [
        "Developer Mode stays on (Settings > Privacy & Security > Developer Mode to turn it off; the iPhone restarts).",
        "The developer disk image Xcode mounted stays mounted until the iPhone restarts.",
        trust_note,
        "Your Apple Development certificate and the runner's provisioning profile stay in your Apple account and in Xcode.",
        "No setting on the iPhone was changed by droidctl (no keyboard or accessibility preferences).",
    ]


TRUST_NOTE = ("With a free Apple team, removing the last app signed by your team also removes the Trust for it "
              "(Settings > General > VPN & Device Management): the next `droidctl setup` asks for the Trust tap again.")


# --------------------------------------------------------------------------
# teardown
# --------------------------------------------------------------------------
def plan(apps):
    """The installed bundles teardown removes: droidctl's runners only. PURE."""
    return [a["bundle"] for a in apps if ours(a.get("bundle"))]


def teardown(udid, keep_app=False, say=lambda line: None):
    st = dev.device_state(udid)
    stopped = runner.stop(udid, quiet=True).get("stopped", False)
    removed, left_dev = [], []
    d = devices.find(udid)
    apps = devices.apps(d["cdid"])
    if not keep_app:
        for b in plan(apps):
            say(f"uninstalling {b}")
            devices.uninstall(d["cdid"], b)
            removed.append(b)
    left_dev = [a["bundle"] for a in apps if a.get("developer") and a["bundle"] not in removed]
    goios_files = None
    if devices.goios():                        # our tunnel agent goes; the disk image stays mounted until a reboot
        from droidctl.ios import goios
        goios.stop_agent(udid)
        goios_files = goios.home()
    dev.clear_device_state(udid)
    if not removed:
        trust = "Nothing was uninstalled, so the Trust for your developer certificate is unchanged."
    elif not left_dev:
        trust = ("No other developer app is left on the iPhone, so the Trust for your developer certificate is gone "
                 "too: the next `droidctl setup` asks for the Trust tap again (Settings > General > VPN & Device "
                 "Management).")
    else:
        trust = TRUST_NOTE + f" Other developer apps remain ({len(left_dev)}); if one is from your team, the Trust stays."
    return {"ok": True, "serial": udid, "platform": "ios", "stopped": stopped, "uninstalled": removed,
            "kept": keep_app, "build_cache": runner.derived_data(st["team"]) if st.get("team") else None,
            "not_undone": disclosures(trust) + ([
                "The developer disk image go-ios mounted stays mounted until the iPhone restarts.",
                f"go-ios's files (its pair record for the tunnel, the downloaded disk image) are in {goios_files}; "
                "usbmuxd's pairing record for this computer is in /var/lib/lockdown (Linux)."] if goios_files else [])}


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------
def parse_pmset(text):
    """`pmset -g` -> {setting: value} for the active power source, plus `sleep_prevented_by`
    (the processes holding a sleep assertion, from "sleep 1 (sleep prevented by a, b)"). PURE."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^\s+(\w+)\s+(\S+)(?:\s+\((.*)\))?", line)
        if m:
            out[m.group(1)] = m.group(2)
            if m.group(1) == "sleep" and m.group(3) and "prevented by" in m.group(3):
                out["sleep_prevented_by"] = sorted({x.strip() for x in m.group(3).split("by", 1)[1].split(",")})
    return out


def parse_therm(text):
    """`pmset -g therm` -> CPU speed limit percent (100 = not throttled), or None. PURE."""
    m = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", text)
    return int(m.group(1)) if m else None


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def expiry_state(expires, now=None):
    """(ok, detail) for a profile expiry ISO string. PURE."""
    if not expires:
        return None, "unknown (no built runner)"
    exp = datetime.datetime.fromisoformat(expires.replace("Z", "+00:00"))
    now = now or datetime.datetime.now(datetime.timezone.utc)
    left = exp - now
    if left <= datetime.timedelta(0):
        return False, f"expired {expires} (run: droidctl setup, it re-signs the runner)"
    days = left.total_seconds() / 86400
    when = f"{days:.1f} days" if days >= 1 else f"{left.total_seconds() / 3600:.0f} h"
    if left < RENEW_WITHIN:
        return True, (f"expires {expires} (in {when}); droidctl re-signs the runner before its next start "
                      "(Xcode may keep this profile until it has expired)")
    return True, f"expires {expires} (in {when})"


def expired(expires, now=None):
    """Whether a profile expiry ISO string is in the past. PURE."""
    if not expires:
        return False
    exp = datetime.datetime.fromisoformat(expires.replace("Z", "+00:00"))
    return exp <= (now or datetime.datetime.now(datetime.timezone.utc))


def renew_due(expires, now=None):
    """Whether setup should re-sign now: expired or expiring within 24 h. PURE."""
    if not expires:
        return False
    exp = datetime.datetime.fromisoformat(expires.replace("Z", "+00:00"))
    return exp - (now or datetime.datetime.now(datetime.timezone.utc)) < RENEW_WITHIN


def pair_tested(xcode, ios):
    """Whether this Xcode / iOS major pair is one droidctl was verified on. PURE."""
    xv = (re.search(r"(\d+\.\d+)", xcode or "") or [None, None])[1]
    return (xv, (ios or "").split(".")[0]) in TESTED


def doctor(udid, check, skip):
    """The iPhone checks, appended through doctor's own `check`/`skip`. Returns extra result fields."""
    ctx = setup.Ctx(udid, dev.device_state(udid).get("team"), lambda line: None)
    later = ["developer-mode", "team", "unlocked", "runner-build", "profile", "runner", "pair"]
    xcode = {}

    def xc():
        xcode["version"] = setup.step_tools(ctx)
        if devices.goios():
            from droidctl.ios import goios
            return True, f"{xcode['version']} at {goios.binary()} (no Xcode)"
        return True, f"{xcode['version']} at {devices.developer_dir()}"
    if not _need(check, "xcode", xc):
        skip("device", *later)
        return {}

    def attached():
        setup.step_attached(ctx)
        return True, f"{udid} (USB, paired)"
    if not _need(check, "device", attached):
        skip(*later)
        return {}
    if not _need(check, "developer-mode", lambda: (True, setup.step_developer_mode(ctx))):
        skip(*later[1:])
        return {}
    team_ok = _need(check, "team", lambda: (True, setup.step_team(ctx)))
    _need(check, "unlocked", lambda: (True, setup.step_unlocked(ctx)))

    def build():
        if not team_ok:
            return False, "no signing team"
        built, want = dev.device_state(udid).get("built"), runner.build_id(ctx.team)
        if not built:
            return False, f"droidctl is not set up on this iPhone (run: droidctl setup -d {udid})"
        if not runner.xctestrun(ctx.team):
            return False, "not built (run: droidctl setup)"
        if built != want:
            return False, f"built {built}, sources are {want} (run: droidctl setup)"
        return True, f"build {want} in {runner.derived_data(ctx.team)}"
    check("runner-build", build)
    check("profile", lambda: expiry_state(setup.profile_expiry(ctx.team) if ctx.team else None))

    def answering():
        if not dev.device_state(udid).get("built"):
            return None, "not set up"
        try:
            c = IosClient(udid, dev.device_state(udid).get("port", runner.DEFAULT_PORT), timeout=2.0)
        except UserError as e:
            if e.kind != "connection":
                raise
            return True, "not running (the next command starts it); the Trust is checked then"
        try:
            info = c.call("ping", timeout=5.0)
            samples = []
            for _ in range(10):                 # `gen`: the round trip alone (ping hit-tests the screen)
                t0 = time.perf_counter()
                c.call("gen", timeout=5.0)
                samples.append((time.perf_counter() - t0) * 1000)
            ms = dev.rtt_stats(samples)
        finally:
            c.close()
        want = dev.device_state(udid).get("build_id")
        if want and info.get("build") != want:
            return False, f"runner build {info.get('build')} answers, expected {want} (the next command restarts it)"
        screen = (info.get("screen") or {}).get("state")
        return True, (f"runner {info.get('version')} build {info.get('build')} answering (so the profile is trusted); "
                      f"screen {screen}; round trip median {ms['median']:.1f} ms")
    check("runner", answering)
    ios = ctx.info.get("os")
    if devices.goios():
        from droidctl.ios import goios
        ok, tested = goios.version_ok(ios)
        check("pair", lambda: (True, f"go-ios {goios.VERSION} with iOS {ios}: "
                                     + ("in the range droidctl expects to work (17.4.1–18.x); unverified on Linux"
                                        if tested else "untested")))
        check("host", lambda: (True, "no Xcode: go-ios tunnel agent on port "
                                     f"{dev.device_state(udid).get('goios_port') or goios.agent_port(udid)}"))
        return {"platform": "ios", "not_undone_by_teardown": disclosures(TRUST_NOTE)}
    check("pair", lambda: (True, f"{xcode.get('version')} with iOS {ios}: verified")
          if pair_tested(xcode.get("version"), ios) else
          (True, f"{xcode.get('version')} with iOS {ios} is untested: droidctl's private XCTest calls may "
                 "have changed (every one is checked before use; failures say `unsupported`)"))

    def host():
        pm = parse_pmset(_run(["pmset", "-g"]))
        cpu = parse_therm(_run(["pmset", "-g", "therm"]))
        sleep = pm.get("sleep")
        parts = [f"system sleep {'never' if sleep == '0' else (sleep + ' min') if sleep else 'unknown'}"]
        if cpu is not None:
            parts.append(f"CPU speed limit {cpu}%" + (" (thermal throttling)" if cpu < 100 else ""))
        if sleep and sleep != "0":
            if "xcodebuild" in (pm.get("sleep_prevented_by") or []):
                parts.append("held awake while the runner runs (its xcodebuild session prevents sleep)")
            else:
                parts.append("if the Mac sleeps, the runner's session can end (not measured); the next command "
                             "restarts it")
        return True, "; ".join(parts)
    check("host", host)
    return {"platform": "ios", "not_undone_by_teardown": disclosures(TRUST_NOTE)}


def _need(check, name, fn):
    """check() for setup steps: their Need (the user has to act) is a failed check with its instructions."""
    def go():
        try:
            return fn()
        except setup.Need as n:
            return False, f"{n} — " + " ".join(n.instructions)
    return check(name, go)

