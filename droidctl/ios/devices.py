"""The iPhone's device side: Xcode's `xcrun devicectl`, or, without Xcode, lockdown (our own
Python) plus go-ios (`goios.py`). Callers don't care which: `backend()` picks it.

An iPhone has two identifiers: the hardware UDID (usbmuxd, `xcodebuild -destination
id=`) and the CoreDevice identifier, a UUID (`devicectl --device`). droidctl names
phones by UDID everywhere and maps to the CoreDevice id here (IOS.md §1); with go-ios the
"cdid" is the UDID itself. devicectl calls read its JSON output (`-j`), its only supported
script interface.
"""
import glob
import json
import os
import subprocess
import tempfile

from droidctl.core import UserError

XCODE_GLOBS = ("/Applications/Xcode*.app", os.path.expanduser("~/Applications/Xcode*.app"),
               os.path.expanduser("~/Downloads/Xcode*.app"))


def developer_dir():
    """The Xcode developer directory: $DROIDCTL_DEVELOPER_DIR, $DEVELOPER_DIR,
    `xcode-select -p` when it points into an Xcode (not the Command Line Tools),
    else the newest-looking Xcode*.app in the usual places. None when there is none."""
    for var in ("DROIDCTL_DEVELOPER_DIR", "DEVELOPER_DIR"):
        if os.environ.get(var):
            return os.environ[var]
    try:
        p = subprocess.run(["xcode-select", "-p"], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        p = ""
    if p and ".app/" in p:
        return p
    apps = sorted(a for g in XCODE_GLOBS for a in glob.glob(g))
    return os.path.join(apps[-1], "Contents", "Developer") if apps else None


def backend():
    """"xcode" (devicectl, xcodebuild) on a Mac with Xcode, else "goios" (Linux, a Mac without
    Xcode). DROIDCTL_IOS_BACKEND=xcode|goios overrides it."""
    forced = os.environ.get("DROIDCTL_IOS_BACKEND")
    if forced in ("xcode", "goios"):
        return forced
    return "xcode" if developer_dir() else "goios"


def goios():
    return backend() == "goios"


def xcode_env():
    d = developer_dir()
    if not d:
        raise UserError("Xcode was not found", "missing-dep",
                        hint="install Xcode from the App Store (or set DROIDCTL_DEVELOPER_DIR "
                             "to its Contents/Developer folder)")
    return dict(os.environ, DEVELOPER_DIR=d)


def error_text(doc):
    """devicectl's error as one line: description, failure reason (if any)."""
    e = (doc or {}).get("error") or {}
    info = e.get("userInfo") or {}

    def s(key):
        v = info.get(key) or {}
        return v.get("string") if isinstance(v, dict) else v
    parts = [s("NSLocalizedDescription"), s("NSLocalizedFailureReason")]
    text = " ".join(p for p in parts if p) or "devicectl failed"
    return f"{text} ({e.get('domain', '?')} {e.get('code', '?')})"


def devicectl(*args, timeout=120):
    """Run `xcrun devicectl ARGS -j FILE -q` and return the parsed `result`.
    A failed outcome raises `device` with devicectl's own message in data.error."""
    env = xcode_env()
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "out.json")
        try:
            p = subprocess.run(["xcrun", "devicectl", *args, "-j", out, "-q"], env=env,
                               capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise UserError(f"devicectl {' '.join(args[:3])} did not finish within {timeout}s", "timeout")
        except OSError as e:
            raise UserError(f"cannot run xcrun: {e}", "missing-dep")
        try:
            with open(out) as f:
                doc = json.load(f)
        except (OSError, ValueError):
            raise UserError(f"devicectl {' '.join(args[:3])} failed: {(p.stderr or p.stdout).strip()[:300]}",
                            "device")
    return result_of(doc, args)


def result_of(doc, args=()):
    if (doc.get("info") or {}).get("outcome") != "success":
        e = doc.get("error") or {}
        raise UserError(f"devicectl {' '.join(args[:3])}: {error_text(doc)}", "device",
                        data={"domain": e.get("domain"), "code": e.get("code")})
    return doc.get("result") or {}


def parse_devices(result):
    """devicectl `list devices` → [{udid, cdid, name, model, product, os, paired,
    developer_mode, connection}] for physical iPhones/iPads."""
    out = []
    for d in result.get("devices", []):
        hw, props, conn = d.get("hardwareProperties", {}), d.get("deviceProperties", {}), d.get("connectionProperties", {})
        if hw.get("reality") != "physical" or hw.get("platform") != "iOS":
            continue
        out.append({
            "udid": hw.get("udid"), "cdid": d.get("identifier"), "name": props.get("name"),
            "model": hw.get("marketingName"), "product": hw.get("productType"),
            "os": props.get("osVersionNumber"), "paired": conn.get("pairingState") == "paired",
            "developer_mode": props.get("developerModeStatus"),
            # "connected" while the phone is attached and reachable; "unavailable" otherwise
            "connection": conn.get("tunnelState"), "transport": conn.get("transportType"),
        })
    return out


def list_devices():
    if goios():
        return _lockdown_devices()
    return parse_devices(devicectl("list", "devices", timeout=60))


def _lockdown_devices():
    """The same rows as parse_devices, from usbmux + lockdown (no Xcode). `model` is the product
    type ("iPhone13,2"): only Xcode knows the marketing name."""
    from droidctl.ios import lockdown, transport
    out = []
    for d in transport.list_devices(timeout=3.0):
        row = {"udid": d["udid"], "cdid": d["udid"], "name": None, "model": None, "product": None, "os": None,
               "paired": transport.is_paired(d["udid"]), "developer_mode": None, "connection": "connected",
               "transport": "wired"}
        if row["paired"]:
            try:
                i = lockdown.info(d["udid"])
                row.update(name=i.get("name"), model=i.get("product"), product=i.get("product"), os=i.get("os"),
                           developer_mode={True: "enabled", False: "disabled"}.get(i.get("developer_mode")))
            except UserError:
                pass
        out.append(row)
    return out


def find(udid):
    """The devicectl entry for a hardware UDID."""
    for d in list_devices():
        if d["udid"] == udid:
            return d
    raise UserError(f"the iPhone {udid} is not attached or not reachable", "no-device",
                    hint="connect it with a cable, unlock it and tap Trust; then run: droidctl setup -d " + udid)


def parse_lock_state(result):
    """{locked, unlocked_since_boot}. `passcodeRequired` true means the phone is locked now."""
    return {"locked": bool(result.get("passcodeRequired")), "unlocked_since_boot": bool(result.get("unlockedSinceBoot"))}


def lock_state(cdid):
    if goios():
        from droidctl.ios import lockdown
        return {"locked": lockdown.locked(cdid), "unlocked_since_boot": None}
    return parse_lock_state(devicectl("device", "info", "lockState", "--device", cdid, timeout=30))


def parse_apps(result):
    return [{"bundle": a.get("bundleIdentifier"), "name": a.get("name"), "version": a.get("version"),
             "build": a.get("bundleVersion"), "developer": a.get("builtByDeveloper", False),
             "system": bool(a.get("defaultApp") or not a.get("removable", True)), "url": a.get("url")}
            for a in result.get("apps", [])]


def apps(cdid, all_apps=False):
    """Installed apps: the user's (removable) ones, or with all_apps the system ones too."""
    if goios():
        from droidctl.ios import lockdown
        return lockdown.apps(cdid, all_apps)
    args = ["device", "info", "apps", "--device", cdid] + (["--include-all-apps"] if all_apps else [])
    return parse_apps(devicectl(*args, timeout=60))


def _path(url):
    """file:///private/var/x/ and /var/x/ are the same place on iOS. PURE."""
    p = (url or "").replace("file://", "")
    return p[len("/private"):] if p.startswith("/private/") else p


def parse_processes(result):
    return [{"pid": p.get("processIdentifier"), "path": _path(p.get("executable"))}
            for p in result.get("runningProcesses", [])]


def pids_of(app, procs):
    """The pids running out of an app's bundle (its main executable and extensions). PURE."""
    root = _path(app.get("url")).rstrip("/") + "/"
    return [p["pid"] for p in procs if p["path"].startswith(root)]


def processes(cdid):
    if goios():
        from droidctl.ios import goios as g
        return g.processes(cdid)
    return parse_processes(devicectl("device", "info", "processes", "--device", cdid, timeout=60))


def terminate(cdid, pid):
    if goios():
        from droidctl.ios import goios as g
        return g.kill(cdid, pid)
    devicectl("device", "process", "terminate", "--device", cdid, "--pid", str(pid), timeout=60)


def app(cdid, bundle):
    """The installed app with this bundle id (system apps included), or None."""
    if goios():
        from droidctl.ios import lockdown
        return lockdown.app(cdid, bundle)
    found = parse_apps(devicectl("device", "info", "apps", "--device", cdid, "--bundle-id", bundle,
                                 "--include-all-apps", timeout=60))
    return found[0] if found else None


def install(cdid, app_path):
    if goios():
        from droidctl.ios import goios as g
        return g.install(cdid, app_path)
    devicectl("device", "install", "app", "--device", cdid, app_path, timeout=300)


def uninstall(cdid, bundle):
    if goios():
        from droidctl.ios import lockdown
        return lockdown.uninstall(cdid, bundle)
    devicectl("device", "uninstall", "app", "--device", cdid, bundle, timeout=120)


def launch(cdid, bundle, terminate_existing=False):
    if goios():
        from droidctl.ios import goios as g
        return g.run(cdid, "launch", bundle, *(["--kill-existing"] if terminate_existing else []), timeout=60)
    args = ["device", "process", "launch", "--device", cdid]
    if terminate_existing:
        args.append("--terminate-existing")
    return devicectl(*args, bundle, timeout=60)
