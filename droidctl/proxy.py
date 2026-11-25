"""`droidctl proxy`: route one app's traffic through an HTTP/SOCKS proxy with proxy-app.

proxy-app (`com.proxyapp.mobile`, bundled in droidctl/assets) is a per-app VPN that
forwards the chosen apps to a proxy. Its adb interface is a broadcast receiver:

    am broadcast -a com.enesb.proxyapp.CMD -n com.proxyapp.mobile/com.enesb.proxyapp.adb.AdbReceiver
                 --es cmd <cmd> [extras]
    -> Broadcast completed: result=0|1, data="<json>"

The JSON in data="..." is NOT escaped: it is everything between `data="` and the
LAST `"`. No data means no receiver ran. result=1 carries {"error": ...}.

The phone's owner uses proxy-app for their own work, so droidctl owns exactly one
profile (`droidctl-pt`), records what was selected and whether it was connected
before `proxy on`, and `proxy off` puts that back. The record lives ON THE PHONE, in
droidctl-pt's own `tag` (`droidctl-restore:v1:<profile id or ->:<connected|off>`), so it
survives a new host or a recreated container (pt runs droidctl in a disposable one);
~/.droidctl/state.json keeps a copy as the fallback. (`tag`, not `note`: the receiver
stores a note but `list` never returns it.) Other profiles are never edited or removed. Everything goes over the adb server's
protocol (Session.shell / Session.install): no `adb` binary.
"""
import hashlib
import json
import os
import re
import time

from droidctl import device as dev
from droidctl.core import UserError

PROXY_PKG = "com.proxyapp.mobile"
RECEIVER = f"{PROXY_PKG}/com.enesb.proxyapp.adb.AdbReceiver"
ACTION = "com.enesb.proxyapp.CMD"
PROFILE = "droidctl-pt"                      # the only profile droidctl creates, edits or removes
DEFAULT_URL = "http://phone:pt@127.0.0.1:8080"
URL_ENV = "DROIDCTL_PROXY_URL"
DNS_CHOICES = ("auto", "proxy", "direct")
DEFAULT_DNS = "direct"                       # DNS stays off the proxy: no DNS-over-CONNECT noise in a capture
ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
APK_PATH = os.path.join(ASSETS, "proxy-app.apk")
MANIFEST_PATH = os.path.join(ASSETS, "proxy-app.json")
# the system's VPN consent dialog (Android 9: "Connection request", OK / Cancel)
CONSENT_PKGS = ("com.android.vpndialogs",)
CONSENT_LABELS = ("ok", "allow", "tamam", "izin ver")
POLL_S = 0.5
TAG_PREFIX = "droidctl-restore:v1:"          # droidctl-pt's tag: the user's pre-`on` state
ACTIVE = ("connected", "connecting")
_PKG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
_SECRET_RE = re.compile(r"(://[^:/@\s\"']*:)[^@\s\"']*@")


# --------------------------------------------------------------------------
# pure helpers: quoting, the reply, masking, the URL
# --------------------------------------------------------------------------
def squote(s):
    """Single-quote for the device shell, always: it re-parses the line adb sends."""
    return "'" + str(s).replace("'", "'\"'\"'") + "'"


def broadcast_line(cmd, *extras):
    """The `am broadcast` line for one receiver command. `extras` are (flag, key, value):
    ("--es", "url", "http://…") is quoted; --ez/--ei values are bare."""
    words = ["am", "broadcast", "-a", ACTION, "-n", RECEIVER, "--es", "cmd", cmd]
    for flag, key, value in extras:
        if flag == "--ez":
            value = "true" if value else "false"
        elif flag == "--es":
            value = squote(value)
        words += [flag, key, str(value)]
    return " ".join(words)


def mask(text):
    """Hide a URL's password (`user:***@`) in anything shown or raised."""
    return _SECRET_RE.sub(r"\1***@", str(text))


def parse_reply(out):
    """`am broadcast` output -> (result code, parsed JSON). The data is not escaped,
    so it is everything between `data="` and the last `"`. No data: no receiver ran."""
    m = re.search(r"Broadcast completed: result=(-?\d+)", out)
    start = out.find('data="')
    end = out.rfind('"')
    if not m or start < 0 or end < start + len('data="'):
        raise UserError(f"proxy-app did not answer the broadcast: {mask(out.strip())[:300] or '(no output)'}",
                        "proxy", hint="install it: droidctl proxy setup",
                        data={"reason": "no-receiver"})
    raw = out[start + len('data="'):end]
    try:
        return int(m.group(1)), json.loads(raw)
    except ValueError:
        raise UserError(f"proxy-app's reply is not JSON: {mask(raw)[:300]}", "proxy",
                        data={"reason": "bad-reply"})


def check_url(url):
    """A proxy URL proxy-app can take: scheme://[user:pass@]host:port. The receiver
    percent-decodes the password and turns `+` into a space, so the credentials must
    be alphanumeric."""
    m = re.match(r"^(https?|socks5h?)://(?:([^:@/]*)(?::([^@/]*))?@)?([^:@/]+):(\d+)/?$", url or "")
    if not m:
        raise UserError(f"not a proxy URL: {mask(url)!r}", "bad-args",
                        hint="e.g. http://user:pass@127.0.0.1:8080 or socks5://127.0.0.1:1080")
    for part in (m.group(2), m.group(3)):
        if part and not part.isalnum():
            raise UserError("the proxy user and password must be letters and digits only "
                            "(proxy-app percent-decodes them and turns + into a space)", "bad-args")
    return url


def bundled():
    """The manifest of the bundled APK: {package, versionCode, versionName, sha256}."""
    try:
        with open(MANIFEST_PATH) as f:
            man = json.load(f)
    except (OSError, ValueError) as e:
        raise UserError(f"proxy-app's manifest is missing or broken ({MANIFEST_PATH}: {e})", "missing-dep")
    if man.get("package") != PROXY_PKG:
        raise UserError(f"the bundled proxy-app is {man.get('package')!r}, not {PROXY_PKG}", "missing-dep")
    return man


def _verify_apk(man):
    h = hashlib.sha256()
    try:
        with open(APK_PATH, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError as e:
        raise UserError(f"the bundled proxy-app APK is missing ({APK_PATH}: {e.strerror})", "missing-dep")
    if h.hexdigest() != man["sha256"]:
        raise UserError("the bundled proxy-app APK does not match its manifest's sha256", "missing-dep",
                        hint="reinstall droidctl")


# --------------------------------------------------------------------------
# talking to proxy-app
# --------------------------------------------------------------------------
def _session(a):
    from droidctl import act
    sess = act.session_for(a)
    if sess.ios:
        raise UserError("proxy is Android only (proxy-app is an Android VPN)", "unsupported")
    return sess


def call(sess, cmd, *extras, timeout=40):
    """One receiver command -> its JSON. result=1 raises kind `proxy` (data.reason
    `consent` when it opened the VPN consent dialog, else `rejected`)."""
    out = sess.shell(broadcast_line(cmd, *extras), timeout=timeout, check=False)
    code, obj = parse_reply(out)
    if code != 0:
        msg = obj.get("error") if isinstance(obj, dict) else None
        consent = isinstance(obj, dict) and obj.get("consent") == "requested"
        raise UserError(f"proxy-app {cmd}: {mask(msg or obj)}", "proxy",
                        data={"reason": "consent" if consent else "rejected", "cmd": cmd,
                              "reply": json.loads(mask(json.dumps(obj)))})
    return obj


def installed_version(sess, pkg=PROXY_PKG):
    """versionCode of an installed package, or None."""
    if not sess.shell("pm", "path", pkg, check=False).strip().startswith("package:"):
        return None
    m = re.search(r"versionCode=(\d+)", sess.shell("dumpsys", "package", pkg, check=False))
    return int(m.group(1)) if m else None


def ensure_installed(sess, install=True):
    """Install the bundled APK only when the phone has none or an older versionCode.
    The same version is never reinstalled: that would kill an active VPN of the user's."""
    man = bundled()
    have = installed_version(sess)
    out = {"package": PROXY_PKG, "bundled_version": man["versionCode"], "previous_version": have,
           "version": have, "installed": False}
    if have is not None and have >= man["versionCode"]:
        return out
    if not install:
        raise UserError(f"proxy-app is {'not installed' if have is None else f'older ({have})'}",
                        "not-installed", hint="run: droidctl proxy setup")
    _verify_apk(man)
    res = sess.install(APK_PATH, timeout=300)
    if "Success" not in res:
        raise UserError(f"installing proxy-app failed: {res.strip()[-300:]}", "adb",
                        hint=("the installed proxy-app is signed by another key; droidctl never uninstalls "
                              "it for you" if "UPDATE_INCOMPATIBLE" in res else ""))
    out.update(installed=True, version=installed_version(sess) or man["versionCode"])
    return out


def _ours(profiles):
    return next((p for p in profiles if str(p.get("name", "")).lower() == PROFILE.lower()), None)


def _wait_state(sess, want_profile, timeout):
    """Poll status until `want_profile` is connected; `error` fails at once."""
    deadline = time.monotonic() + timeout
    st = call(sess, "status")
    while True:
        if st.get("state") == "connected" and str(st.get("profile", "")).lower() == want_profile.lower():
            return st
        if st.get("state") == "error":
            raise UserError(f"proxy-app could not connect {want_profile}: {mask(st.get('error'))}", "proxy",
                            data={"reason": "connect-failed", "status": json.loads(mask(json.dumps(st)))})
        if time.monotonic() >= deadline:
            raise UserError(f"{want_profile} did not connect within {timeout:g} s (state {st.get('state')})",
                            "timeout", data={"status": json.loads(mask(json.dumps(st)))},
                            hint="see: droidctl proxy status / droidctl proxy log --failed")
        time.sleep(POLL_S)
        st = call(sess, "status")


def _wait_off(sess, timeout=10.0):
    deadline = time.monotonic() + timeout
    st = call(sess, "status")
    while st.get("state") in ACTIVE and time.monotonic() < deadline:
        time.sleep(POLL_S)
        st = call(sess, "status")
    return st


def accept_consent(a, timeout=10.0):
    """Tap OK on the system's VPN consent dialog with droidctl's own snapshot and tap.
    Only when the front window is the system's VPN dialog: never an OK in another app."""
    from droidctl import cli
    parser = cli.build_parser()

    def run(*argv):
        sa = parser.parse_args(list(argv))
        sa.device, sa.json = a.device, True
        sa.no_auto_setup = getattr(a, "no_auto_setup", False)
        return sa.fn(sa)

    deadline = time.monotonic() + timeout
    while True:
        snap = run("snapshot", "--full")
        if (snap.get("screen") or {}).get("pkg") in CONSENT_PKGS:
            buttons = [e for e in snap["elements"] if e.get("role") == "button"]
            pick = (next((e for e in buttons if (e.get("id") or "").endswith(":id/button1")), None)
                    or next((e for e in buttons if (e.get("label") or "").strip().lower() in CONSENT_LABELS), None))
            if pick is not None:
                res = run("tap", str(pick["ref"]))
                return {"tapped": pick.get("label"), "ref": pick["ref"], "method": res.get("method")}
        if time.monotonic() >= deadline:
            raise UserError("proxy-app asked for VPN permission but the consent dialog did not show up", "proxy",
                            data={"reason": "consent", "screen": snap.get("screen")},
                            hint="accept the 'Connection request' dialog on the phone, then: droidctl proxy status")
        time.sleep(POLL_S)


def _status_record(st, profiles):
    """What the user had before `proxy on`: the selected profile (by id) and its state."""
    sel = next((p for p in profiles if p.get("selected")), None)
    state = st.get("state")
    if sel is not None and str(sel.get("name", "")).lower() == PROFILE.lower():
        sel, state = None, "off"                # droidctl's own: nothing of the user's to put back
    return {"state": state, "profile_id": sel and sel.get("id"), "profile": sel and sel.get("name"),
            "recorded": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def _encode_record(rec):
    """The restore record as droidctl-pt's tag: only the id and on/off (names may hold `:`)."""
    return f"{TAG_PREFIX}{rec.get('profile_id') or '-'}:{'connected' if rec.get('state') in ACTIVE else 'off'}"


def _device_record(profiles):
    """The restore record kept in droidctl-pt's tag, or None (no droidctl-pt, no or a foreign
    tag, or one that points at droidctl-pt itself). The name is looked up by id."""
    ours = _ours(profiles)
    tag = str((ours or {}).get("tag") or "")
    if not tag.startswith(TAG_PREFIX):
        return None
    pid, _, state = tag[len(TAG_PREFIX):].rpartition(":")
    if not pid or state not in ("connected", "off"):
        return None
    pid = None if pid == "-" else pid
    if pid is not None and pid == ours.get("id"):
        return None
    prof = next((p for p in profiles if pid is not None and p.get("id") == pid), None)
    return {"state": state, "profile_id": pid, "profile": prof.get("name") if prof else None,
            "source": "device"}


def _local_record(serial, ours):
    """state.json's copy, unless it points at droidctl-pt itself."""
    rec = dev.device_state(serial).get("proxy_restore")
    if rec and ours is not None and rec.get("profile_id") == ours.get("id"):
        return None
    return rec


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_proxy(a):
    return {"setup": _setup, "on": _on, "off": _off, "status": _status, "log": _log}[a.action](a)


def _read_state(sess):
    """proxy-app's status and profiles, or (None, []) when no receiver answers (not
    installed: nothing of the user's to remember). Read BEFORE installing: an upgrade
    (`pm install -r`) stops a running VPN, so afterwards it would always read `off`."""
    try:
        return call(sess, "status"), call(sess, "list")
    except UserError as e:
        if (e.data or {}).get("reason") != "no-receiver":
            raise
        return None, []


def _install_keeping_state(sess, install=True, timeout=20.0):
    """ensure_installed, and if an upgrade stopped the user's running profile, connect it
    again. -> (setup result, the state read before installing, its profiles)."""
    st, profiles = _read_state(sess)
    r = ensure_installed(sess, install=install)
    if r["installed"] and st and st.get("state") in ACTIVE:
        rec = _status_record(st, profiles)
        if rec["profile_id"]:
            _restore(sess, rec, timeout)
            r["reconnected"] = rec["profile"]
    return r, st, profiles


def _setup(a):
    sess = _session(a)
    r, _, _ = _install_keeping_state(sess)
    r["ok"] = True
    r["text"] = (f"installed proxy-app {r['version']}" if r["installed"]
                 else f"proxy-app {r['version']} is already installed (bundled {r['bundled_version']})")
    return r


def _on(a):
    if not a.app:
        raise UserError("proxy on needs --app PKG (the app whose traffic goes to the proxy)", "bad-args")
    if not _PKG_RE.match(a.app):
        raise UserError(f"not a package name: {a.app!r}", "bad-args")
    url = check_url(a.to or os.environ.get(URL_ENV) or DEFAULT_URL)
    dns = a.dns or DEFAULT_DNS
    sess = _session(a)
    if not sess.shell("pm", "path", a.app, check=False).strip().startswith("package:"):
        raise UserError(f"{a.app} is not installed", "not-found", hint="see: droidctl apps")
    # what the user had, read before any install and recorded once: a second `on` (also
    # from a new host or container, with no state.json) must not overwrite it with ours.
    # The phone's copy (droidctl-pt's tag) wins, then state.json, then the state just read.
    setup, pre, profiles = _install_keeping_state(sess, install=not getattr(a, "no_auto_setup", False),
                                                  timeout=a.timeout)
    previous = (_device_record(profiles) or _local_record(sess.serial, _ours(profiles))
                or _status_record(pre or {"state": "off"}, profiles))
    previous = {k: v for k, v in previous.items() if k != "source"}
    dev.update_device_state(sess.serial, proxy_restore=previous)
    st = call(sess, "status")
    try:
        return _switch_on(a, sess, url, dns, st, setup, previous)
    except UserError as e:
        e.hint = (e.hint + "; " if e.hint else "") + "to put the user's previous proxy-app state back: droidctl proxy off"
        raise


def _switch_on(a, sess, url, dns, st, setup, previous):
    # the record goes on the phone in the same `add`: a failed connect still leaves it there
    saved = call(sess, "add", ("--es", "name", PROFILE), ("--es", "url", url), ("--es", "apps", a.app),
                 ("--es", "mode", "only"), ("--es", "dns", dns), ("--ez", "blockquic", True),
                 ("--es", "tag", _encode_record(previous)), ("--ez", "select", True))["saved"]
    # a running VPN (the user's, or ours with the old settings) is stopped first, so the new one applies
    if st.get("state") in ACTIVE:
        call(sess, "disconnect")
        _wait_off(sess)
    consent = None
    try:
        call(sess, "connect", ("--es", "profile", saved["id"]))
    except UserError as e:
        if (e.data or {}).get("reason") != "consent":
            raise
        consent = accept_consent(a)
    st = _wait_state(sess, saved["name"], a.timeout)
    out = {"ok": True, "state": st["state"], "profile": saved["name"], "app": a.app, "to": mask(url), "dns": dns,
           "setup": setup, "previous": {k: previous.get(k) for k in ("state", "profile")}}
    if consent:
        out["consent"] = consent
    out["text"] = f"{a.app} -> {mask(url)} via proxy-app ({st['state']}, dns {dns})"
    return out


def _off(a):
    sess = _session(a)
    st, profiles = call(sess, "status"), call(sess, "list")
    ours = _ours(profiles)
    # the phone's record first (it survives a recreated container), else this host's copy
    rec = _device_record(profiles) or _local_record(sess.serial, ours)
    if rec is None and ours is None:
        return {"ok": True, "changed": False, "state": st.get("state"), "profile": st.get("profile"),
                "text": "droidctl's proxy was not on; nothing changed"}
    active_ours = st.get("state") in ACTIVE and str(st.get("profile", "")).lower() == PROFILE.lower()
    took_over = st.get("state") in ACTIVE and not active_ours       # the user switched to their own profile
    out = {"ok": True, "changed": True, "disconnected": False, "removed": False}
    if active_ours:
        call(sess, "disconnect")
        _wait_off(sess)
        out["disconnected"] = True
    if ours is not None:
        call(sess, "remove", ("--es", "profile", ours["id"]))      # clears the selection if ours was selected
        out["removed"] = True
    warning = None
    if rec and took_over:
        warning = f"{st.get('profile')} is running (not droidctl's); the previous selection was left as is"
    elif rec and rec.get("profile_id"):
        warning = _restore(sess, rec, a.timeout)
    st = call(sess, "status")
    data = dev.load_state()                     # restored: forget the record
    data["devices"].get(sess.serial, {}).pop("proxy_restore", None)
    dev.save_state(data)
    out.update(state=st.get("state"), profile=st.get("profile"),
               restored={k: (rec or {}).get(k) for k in ("state", "profile")},
               restored_from=rec and rec.get("source", "local"))
    if warning:
        out["warning"] = warning
    out["text"] = (f"proxy off; restored {out['restored']['profile'] or 'no selection'} "
                   f"({out['restored']['state']}); now {st.get('state')} {st.get('profile') or ''}").rstrip()
    return out


def _restore(sess, rec, timeout):
    """Select (and if it was on, connect) the user's previous profile. -> a warning or None.
    proxy-app has no select-only command: `connect` selects and connects. So a profile that
    was selected but off, and is not what proxy-app falls back to (its first profile), is
    connected and at once disconnected again."""
    pid = rec["profile_id"]
    profiles = call(sess, "list")
    prof = next((p for p in profiles if p.get("id") == pid), None)
    if prof is None:
        return f"the previous profile {rec.get('profile') or pid!r} no longer exists; nothing selected"
    if rec.get("state") in ACTIVE:
        call(sess, "connect", ("--es", "profile", pid))
        _wait_state(sess, prof["name"], timeout)
        return None
    if prof.get("selected"):
        return None
    try:
        call(sess, "connect", ("--es", "profile", pid))
    finally:
        call(sess, "disconnect")
        _wait_off(sess)
    return f"re-selecting {prof['name']} connected it for a moment (proxy-app has no select-only command)"


def _status(a):
    st = call(_session(a), "status")
    text = f"{st.get('state')} {st.get('profile') or ''} mode={st.get('mode')} apps={','.join(st.get('apps') or [])}"
    return {"ok": True, "state": st.get("state"), "status": st, "text": text}


def _log(a):
    extras = [("--es", "limit", a.limit)]
    if a.failed:
        extras.append(("--ez", "failed", True))
    entries = call(_session(a), "log", *extras)
    lines = [f"{e.get('time')} {e.get('proto')} {e.get('app') or '-'} {e.get('target')} {e.get('status')}"
             + (f" {e['error']}" if e.get("error") else "") for e in entries]
    return {"ok": True, "entries": entries, "count": len(entries), "text": "\n".join(lines) or "(no entries)"}


def add_parsers(sub, jsonopt, devopt):
    import argparse
    from droidctl.commands import render_text
    actopt = argparse.ArgumentParser(add_help=False)
    actopt.add_argument("--no-auto-setup", action="store_true",
                        help="fail with not-installed instead of installing proxy-app (or the agent) automatically")
    sp = sub.add_parser("proxy", parents=[jsonopt, devopt, actopt],
                        help="route one app's traffic to a proxy with the bundled proxy-app (a per-app VPN): "
                             "setup installs it, on/off switch it and restore the user's own profile, "
                             "status/log pass its JSON through")
    sp.add_argument("action", choices=("setup", "on", "off", "status", "log"))
    sp.add_argument("--app", metavar="PKG", help="on: the app to route (only this one)")
    sp.add_argument("--to", metavar="URL",
                    help=f"on: the proxy (default ${URL_ENV}, else {DEFAULT_URL}); user/password letters and digits only")
    sp.add_argument("--dns", choices=DNS_CHOICES, help=f"on: DNS policy (default {DEFAULT_DNS}: DNS stays off the proxy)")
    sp.add_argument("--timeout", type=float, default=20.0, metavar="S", help="on/off: wait for connected (20 s)")
    sp.add_argument("--limit", type=int, default=50, metavar="N", help="log: at most N entries (50)")
    sp.add_argument("--failed", action="store_true", help="log: only failed connections")
    sp.set_defaults(fn=cmd_proxy, render=render_text)
