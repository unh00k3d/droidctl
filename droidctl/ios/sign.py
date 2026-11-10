"""Sign the prebuilt runner for one team: per-team bundle ids, the team's profile embedded, the
profile's entitlements, then the plugin and the app signed inside-out.

The output goes where Xcode's own build went (`runner.derived_data(team)/Build/Products`), so
starting, renewal checks, doctor and teardown read it the same way.

- With a keychain identity (macOS): `/usr/bin/codesign` (part of macOS, not Xcode), the identity
  whose certificate the profile lists (on a Mac that used Xcode, the one Xcode created).
- With droidctl's own key (Linux, or a Mac without such an identity): go-ios `sign app`
  (go-codesign, MIT); it takes the certificate from the profile (`identity.py`).
"""
import hashlib
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile

from droidctl.core import UserError
from droidctl.ios import prebuilt, runner


def decode_profile(data):
    """A .mobileprovision (CMS) -> its plist. Reads the embedded XML; no signature check (Apple
    sent it over TLS, and the phone verifies it). PURE."""
    start, end = data.find(b"<?xml"), data.find(b"</plist>")
    if start < 0 or end < 0:
        raise UserError("the provisioning profile is unreadable", "signing")
    return plistlib.loads(data[start:end + len(b"</plist>")])


def cert_sha1s(prof):
    """The SHA-1s of the development certificates a profile lists. PURE."""
    return [hashlib.sha1(c).hexdigest().upper() for c in prof.get("DeveloperCertificates", [])]


def entitlements(prof):
    """The app's entitlements: the profile's, minus what only the profile itself may carry. PURE."""
    ent = dict(prof.get("Entitlements") or {})
    ent.pop("com.apple.developer.icloud-container-development-container-identifiers", None)
    return ent


def keychain_identities():
    """SHA-1s of valid code-signing identities in the user's keychains (macOS)."""
    p = subprocess.run(["security", "find-identity", "-v", "-p", "codesigning"], capture_output=True, text=True,
                       timeout=30)
    out = []
    for line in p.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0].endswith(")") and len(parts[1]) == 40:
            out.append(parts[1].upper())
    return out


def identity_for(prof):
    """The keychain identity that matches one of the profile's certificates, or None."""
    listed = set(cert_sha1s(prof))
    return next((i for i in keychain_identities() if i in listed), None)


def rewrite_ids(app, prefix):
    """Per-team bundle ids in the app's and the plugin's Info.plist; returns (app id, plugin id)."""
    app_id, plugin_id = prefix + ".runner.xctrunner", prefix + ".runner"
    for plist, new in ((os.path.join(app, "Info.plist"), app_id),
                       (os.path.join(app, prebuilt.PLUGIN, "Info.plist"), plugin_id)):
        with open(plist, "rb") as f:
            raw = f.read()
        fmt = plistlib.FMT_BINARY if raw.startswith(b"bplist") else plistlib.FMT_XML
        d = plistlib.loads(raw)
        d["CFBundleIdentifier"] = new
        with open(plist, "wb") as f:
            plistlib.dump(d, f, fmt=fmt)
    return app_id, plugin_id


def rewrite_xctestrun(src, dst, app_id, plugin_id):
    with open(src, "rb") as f:
        x = plistlib.load(f)
    for key, val in x.items():
        if key.startswith("__") or not isinstance(val, dict):
            continue
        val["TestHostBundleIdentifier"] = app_id
        val["BundleIdentifiersForCrashReportEmphasis"] = [plugin_id]
    with open(dst, "wb") as f:
        plistlib.dump(x, f)


def codesign(path, identity, ents=None):
    cmd = ["codesign", "--force", "--sign", identity, "--timestamp=none", "--generate-entitlement-der"]
    with tempfile.NamedTemporaryFile(suffix=".plist", delete=False) as f:
        if ents is not None:
            plistlib.dump(ents, f)
    try:
        if ents is not None:
            cmd += ["--entitlements", f.name]
        p = subprocess.run(cmd + [path], capture_output=True, text=True, timeout=120)
    finally:
        os.remove(f.name)
    if p.returncode != 0:
        msg = (p.stderr or p.stdout).strip().splitlines()[-1:] or ["codesign failed"]
        hint = None
        if "errSecInternalComponent" in p.stderr or "user interaction" in p.stderr.lower():
            hint = "the keychain is locked: unlock it (log in at the Mac's screen, or `security unlock-keychain`)"
        raise UserError(f"signing the runner failed: {msg[0]}", "signing", hint=hint)


def sign(artifact, team, profile_bytes, ident=None, udid=None):
    """Sign `artifact` for `team` with this profile. Returns the signed app's path. `ident` is
    identity.ensure()'s answer; without one, a keychain identity the profile lists (macOS)."""
    prof = decode_profile(profile_bytes)
    if ident and ident["kind"] == "key":
        if ident["sha1"] not in cert_sha1s(prof):
            raise UserError("the team's profile doesn't list droidctl's certificate yet", "signing",
                            hint="run droidctl setup again in a minute", data={"reason": "cert-not-in-profile"})
        identity = None
    else:
        if sys.platform != "darwin":
            raise UserError("no signing key for this team", "signing", hint="run: droidctl setup --ios",
                            data={"reason": "no-identity"})
        identity = ident["sha1"] if ident else identity_for(prof)
        if not identity:
            raise UserError("no signing certificate in this Mac's keychain matches the team's profile", "signing",
                            hint="run: droidctl setup --ios (droidctl creates its own certificate when signed in)",
                            data={"reason": "no-identity"})
    prefix = runner.bundle_prefix(team)
    products = os.path.join(runner.derived_data(team), "Build", "Products")
    tmp = products + ".signing"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(os.path.join(tmp, "Debug-iphoneos"))
    app = os.path.join(tmp, "Debug-iphoneos", prebuilt.APP)
    shutil.copytree(os.path.join(artifact, "Debug-iphoneos", prebuilt.APP), app, symlinks=True)
    app_id, plugin_id = rewrite_ids(app, prefix)
    want = f"{team}.{app_id}"
    got = (prof.get("Entitlements") or {}).get("application-identifier", "")
    if got not in (want, f"{team}.*"):
        raise UserError(f"the profile is for {got}, not {want}", "signing")
    if identity:
        ents = entitlements(prof)
        ents["application-identifier"] = want
        if "keychain-access-groups" in ents:
            ents["keychain-access-groups"] = [want]
        with open(os.path.join(app, "embedded.mobileprovision"), "wb") as f:
            f.write(profile_bytes)
        codesign(os.path.join(app, prebuilt.PLUGIN), identity)
        codesign(app, identity, ents)
    else:
        goios_sign(app, app_id, profile_bytes, ident["key_pem"], udid)
    rewrite_xctestrun(os.path.join(artifact, "runner.xctestrun"), os.path.join(tmp, "DroidctlRunner.xctestrun"),
                      app_id, plugin_id)
    shutil.rmtree(products, ignore_errors=True)
    os.makedirs(os.path.dirname(products), exist_ok=True)
    os.replace(tmp, products)
    return os.path.join(products, "Debug-iphoneos", prebuilt.APP)


def goios_sign(app, app_id, profile_bytes, key_pem, udid):
    """go-ios `sign app` (go-codesign): nested bundles first, the profile's entitlements, the
    profile embedded. The key is written to a private temp dir for the call only."""
    from droidctl.ios import goios
    with tempfile.TemporaryDirectory(prefix="droidctl-sign-") as d:
        os.chmod(d, 0o700)
        key, prof, out = os.path.join(d, "key.pem"), os.path.join(d, "runner.mobileprovision"), os.path.join(d, "out.app")
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key_pem)
        with open(prof, "wb") as f:
            f.write(profile_bytes)
        goios.run(udid, "sign", "app", f"--path={app}", f"--p12file={key}", f"--profile={prof}", f"--output={out}",
                  f"--bundleid={app_id}", tunnel=False, timeout=120)
        if not os.path.isdir(out):
            raise UserError("go-ios did not write the signed runner", "signing")
        shutil.rmtree(app)
        shutil.move(out, app)
