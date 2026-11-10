"""go-ios (MIT, danielpaulus/go-ios), the iPhone's device side where there is no Xcode: Linux, or
a Mac without Xcode (`research/ios-linux.md`, IOS-INTEGRATION.md §9).

droidctl uses it only for what needs Apple's iOS 17+ tunnel or a developer disk image: the
tunnel agent, mounting the image, installing, starting the runner (`runtest`), and listing or
ending processes. Everything else goes over lockdown in our own Python (`lockdown.py`).

- **The binary:** `DROIDCTL_GO_IOS`, else a pinned release downloaded once into
  `~/.droidctl/bin/go-ios-<version>/` and checked against the sha256 below (go-ios publishes no
  checksums; measured 2026-11-10). Pinned because its CLI is the contract and it moves (§2.4).
- **The agent:** one per phone, droidctl's own, on a private port (never the default 28100, so a
  user's own go-ios agent is left alone), userspace (no root), with its files under our home.
- **Exit codes lie** (`runtest` and `image auto` exit 0 on failure), so outcomes are checked
  ourselves: the runner's ping, lockdown's LookupImage, installation_proxy.
"""
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

from droidctl import device as dev
from droidctl.core import UserError

VERSION = "1.3.2"
URL = "https://github.com/danielpaulus/go-ios/releases/download/v{version}/{asset}"
# sha256 of the release zips, measured 2026-11-10 (they match research/ios-linux.md §5)
RELEASES = {
    "linux": {"asset": "go-ios-linux.zip", "size": 16638255,
              "sha256": "a55fdb4c507391c0548252e01d1deb6ae3c7fd99cfbce842c6f80569cc125604",
              "member": {"x86_64": "ios-amd64", "amd64": "ios-amd64", "aarch64": "ios-arm64", "arm64": "ios-arm64"}},
    "darwin": {"asset": "go-ios-mac.zip", "size": 16821315,
               "sha256": "100f225bfdd039081bcbdaf45029df3e67032673f3e0ca1fa3c050898c230c57",
               "member": {"x86_64": "ios", "arm64": "ios"}},
}
AGENT_PORT_BASE = 28150                       # + a per-phone offset; go-ios's own default is 28100
AGENT_PORTS = 40
TESTED_IOS = ((17, 4, 1), (18, 99))           # the userspace tunnel needs > 17.4.0 (§2.1); 26.x untested


def home():
    return os.path.join(dev.home(), "ios", "go-ios")


def install_dir(version=VERSION):
    return os.path.join(dev.home(), "bin", f"go-ios-{version}")


# --------------------------------------------------------------------------
# the binary
# --------------------------------------------------------------------------
def release_for(system=None, machine=None):
    """(release, member) for this OS/CPU, or None. PURE."""
    system = (system or sys.platform).lower()
    system = "darwin" if system.startswith("darwin") else "linux" if system.startswith("linux") else system
    rel = RELEASES.get(system)
    if not rel:
        return None
    member = rel["member"].get((machine or platform.machine()).lower())
    return (rel, member) if member else None


def binary():
    """The go-ios binary to use, or None when it isn't there yet."""
    env = os.environ.get("DROIDCTL_GO_IOS")
    if env:
        if not (os.path.isfile(env) and os.access(env, os.X_OK)):
            raise UserError(f"DROIDCTL_GO_IOS is not an executable file: {env}", "bad-args")
        return env
    path = os.path.join(install_dir(), "ios")
    return path if os.path.isfile(path) and os.access(path, os.X_OK) else None


def fetch(say=lambda line: None):
    """Download the pinned go-ios once, check its sha256, unpack the binary for this CPU."""
    got = release_for()
    if not got:
        raise UserError(f"go-ios has no release for {sys.platform}/{platform.machine()}", "unsupported",
                        hint="build go-ios yourself and set DROIDCTL_GO_IOS to its `ios` binary")
    rel, member = got
    url = URL.format(version=VERSION, asset=rel["asset"])
    say(f"downloading go-ios {VERSION} ({rel['size'] // 1_000_000} MB, MIT; the iPhone's device side without Xcode)…")
    os.makedirs(os.path.dirname(install_dir()), exist_ok=True)
    with tempfile.TemporaryDirectory(dir=os.path.dirname(install_dir())) as d:
        zpath = os.path.join(d, rel["asset"])
        try:
            from droidctl.ios.apple import tls_context
            with urllib.request.urlopen(url, timeout=300, context=tls_context()) as r, open(zpath, "wb") as f:
                shutil.copyfileobj(r, f)
        except (urllib.error.URLError, OSError) as e:
            raise UserError(f"can't download go-ios: {getattr(e, 'reason', e)}", "connection",
                            hint=f"check the internet connection, or download {url} yourself, unzip it and set "
                                 "DROIDCTL_GO_IOS to the binary") from None
        sha = _sha256(zpath)
        if sha != rel["sha256"]:
            raise UserError("the downloaded go-ios doesn't match its pinned checksum", "device",
                            data={"reason": "checksum", "expected": rel["sha256"], "got": sha})
        out = os.path.join(d, "out")
        os.makedirs(out)
        with zipfile.ZipFile(zpath) as z:
            with z.open(member) as src, open(os.path.join(out, "ios"), "wb") as dst:
                shutil.copyfileobj(src, dst)
        os.chmod(os.path.join(out, "ios"), 0o755)
        shutil.rmtree(install_dir(), ignore_errors=True)
        os.replace(out, install_dir())
    return os.path.join(install_dir(), "ios")


def ensure(say=lambda line: None):
    return binary() or fetch(say)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def version_ok(ios):
    """Whether go-ios's userspace tunnel is expected to work on this iOS ("x.y.z"). PURE.
    Returns (ok, tested)."""
    v = tuple(int(x) for x in (ios or "0").split(".")[:3] if x.isdigit())
    return v >= TESTED_IOS[0], v <= TESTED_IOS[1]


# --------------------------------------------------------------------------
# running it
# --------------------------------------------------------------------------
def log_errors(text):
    """go-ios logs JSON lines on stderr ({"level", "msg", "err", …}): its ERROR lines (and the
    "Failed running Xcuitest" Info line, the only trace of a failed runtest) as text. PURE."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("level") == "ERROR" or rec.get("msg") == "Failed running Xcuitest":
            err = rec.get("err") or rec.get("error")
            out.append(f"{rec.get('msg')}: {err}" if err else str(rec.get("msg")))
    return out


def json_out(text):
    """The last JSON value go-ios printed on stdout, or None. PURE."""
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if line.startswith(("{", "[")):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def run(udid, *args, tunnel=True, timeout=120):
    """`ios ARGS --udid=U [--tunnel-info-port=P]` -> (stdout, stderr). A non-zero exit raises
    `device` with go-ios's own error lines."""
    cmd = [ensure(), *args, f"--udid={udid}"]
    if tunnel:
        cmd.append(f"--tunnel-info-port={agent_port(udid)}")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=_cwd())
    except subprocess.TimeoutExpired:
        raise UserError(f"go-ios {args[0]} did not finish within {timeout}s", "timeout")
    except OSError as e:
        raise UserError(f"cannot run go-ios: {e}", "missing-dep")
    if p.returncode != 0:
        errs = log_errors(p.stderr) or [(p.stderr or p.stdout).strip()[-300:]]
        from droidctl.ios import runner
        runner.raise_for_output(p.stderr + p.stdout)
        raise UserError(f"go-ios {args[0]} failed: {'; '.join(errs)}", "device", data={"errors": errs})
    return p.stdout, p.stderr


def _cwd():
    """go-ios drops files in its working directory (`selfIdentity.plist`, `devimages/`): keep
    them in our home, never in the user's current directory."""
    d = home()
    os.makedirs(d, exist_ok=True)
    return d


# --------------------------------------------------------------------------
# the tunnel agent (iOS 17+): one per phone, ours
# --------------------------------------------------------------------------
def agent_port(udid):
    st = dev.device_state(udid)
    if st.get("goios_port"):
        return st["goios_port"]
    return AGENT_PORT_BASE + int(hashlib.sha1(udid.encode()).hexdigest(), 16) % AGENT_PORTS


def _get(port, path, timeout=2.0):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
            body = r.read()
            return r.status, json.loads(body) if body.strip().startswith((b"{", b"[")) else body
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, OSError, ValueError):
        return None, None


def tunnel(udid):
    """The agent's tunnel to the phone ({address, rsdPort, userspaceTunPort, …}), or None."""
    status, body = _get(agent_port(udid), f"/tunnel/{udid}")
    return body if status == 200 and isinstance(body, dict) else None


def ensure_agent(udid, timeout=30.0, say=lambda line: None):
    """Our agent is up and has a tunnel to this phone."""
    if tunnel(udid):
        return agent_port(udid)
    port = agent_port(udid)
    status, _ = _get(port, "/health")
    if status is not None:
        stop_agent(udid)                       # ours, but without a tunnel to this phone: start over
        time.sleep(1.5)
    from droidctl.ios import runner
    os.makedirs(runner.session_dir(udid), exist_ok=True)
    logpath = os.path.join(runner.session_dir(udid), "go-ios-agent.log")
    pairs = os.path.join(_cwd(), "pair-records")
    os.makedirs(pairs, mode=0o700, exist_ok=True)
    say("starting the iOS tunnel (go-ios, userspace)…")
    with open(logpath, "w") as out:
        p = subprocess.Popen([ensure(), "tunnel", "start", "--userspace", f"--udid={udid}",
                              f"--tunnel-info-port={port}", f"--pair-record-path={pairs}"],
                             stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=_cwd(),
                             start_new_session=True)
    dev.update_device_state(udid, goios_port=port, goios_agent_pid=p.pid)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if tunnel(udid):
            return port
        if p.poll() is not None:
            break
        time.sleep(0.5)
    with open(logpath, errors="replace") as f:
        errs = log_errors(f.read())
    if p.poll() is None:
        stop_agent(udid)
    raise UserError("go-ios could not open a tunnel to the iPhone", "device",
                    hint="keep it connected and unlocked; go-ios's tunnel needs iOS 17.4.1 or later. Log: " + logpath,
                    data={"reason": "tunnel", "errors": errs[-3:]})


def stop_agent(udid):
    """Shut our agent down (its /shutdown; then the pid we started)."""
    st = dev.device_state(udid)
    port = st.get("goios_port")
    if port:
        _get(port, "/shutdown", timeout=2.0)
    pid = st.get("goios_agent_pid")
    if pid:
        time.sleep(1.2)
        try:
            os.killpg(pid, 15)
        except OSError:
            pass
        dev.update_device_state(udid, goios_agent_pid=None)


# --------------------------------------------------------------------------
# developer disk image, install, processes
# --------------------------------------------------------------------------
def ddi_dir():
    """Where `image auto` keeps the image: DROIDCTL_IOS_DDI (a copy of a Mac's
    /Library/Developer/DeveloperDiskImages/iOS_DDI), else go-ios's download under our home."""
    return os.environ.get("DROIDCTL_IOS_DDI") or os.path.join(_cwd(), "ddi")


def ensure_ddi(udid, say=lambda line: None):
    """The developer disk image is mounted: an image already there is kept as it is (Xcode's,
    say); otherwise go-ios mounts one (the first time it asks Apple to personalize it)."""
    from droidctl.ios import lockdown
    if lockdown.developer_image_mounted(udid):
        return "already mounted"
    say("mounting the developer disk image (the first time, Apple personalizes it; needs the internet)…")
    _, err = run(udid, "image", "auto", f"--basedir={ddi_dir()}", timeout=600)
    if not lockdown.developer_image_mounted(udid):
        errs = log_errors(err)
        raise UserError("the developer disk image could not be mounted", "device",
                        hint="check the internet connection (Apple personalizes the image), or copy a Mac's "
                             "/Library/Developer/DeveloperDiskImages/iOS_DDI and set DROIDCTL_IOS_DDI to it",
                        data={"reason": "ddi", "errors": errs[-3:]})
    return "mounted"


def install(udid, app_path):
    run(udid, "install", f"--path={app_path}", tunnel=False, timeout=300)


def parse_ps(doc):
    """`ios ps --apps` JSON -> [{pid, path}], the shape devices.parse_processes gives. PURE."""
    from droidctl.ios.devices import _path
    return [{"pid": p.get("Pid"), "path": _path(p.get("RealAppName"))} for p in (doc or []) if isinstance(p, dict)]


def processes(udid):
    out, _ = run(udid, "ps", timeout=60)
    return parse_ps(json_out(out))


def kill(udid, pid):
    run(udid, "kill", f"--pid={pid}", timeout=60)


def runtest_cmd(udid, bundle, port, log):
    """The session holder: go-ios starts the installed runner through testmanagerd and keeps the
    session open, like `xcodebuild test-without-building`."""
    return [ensure(), "runtest", f"--udid={udid}", f"--tunnel-info-port={agent_port(udid)}",
            f"--test-runner-bundle-id={bundle}", "--xctest-config=DroidctlRunner.xctest",
            f"--env=DROIDCTL_PORT={port}", f"--log-output={log}"]


# go-ios's wording for a failed start (research/ios-linux.md §3.4), PURE rules for runner.classify_output
RULES = [
    (r"Did not find test app", "not-installed", "the runner is not installed on the iPhone",
     "run: droidctl setup", "not-installed"),
    (r"not available in RSD|missing tunnel address|go-ios agent is not running", "device",
     "go-ios has no tunnel to the iPhone", "run: droidctl setup (it starts the tunnel)", "tunnel"),
    (r"could not get pid from response", "not-trusted",
     "the iPhone refused to start the runner (it is locked, or the developer profile isn't trusted)",
     "unlock the iPhone; if it is unlocked: Settings > General > VPN & Device Management > your Apple "
     "Development profile > Trust", "launch-refused"),
    (r"lost connection to testmanagerd", "device", "the runner's test session ended",
     "run the command again (droidctl restarts the runner)", "session-lost"),
]
