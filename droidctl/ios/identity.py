"""The development certificate that signs the runner, when droidctl provisions it itself.

In order:
1. **macOS:** a keychain identity whose certificate the team lists (the one Xcode created on
   this Mac): `/usr/bin/codesign` signs with it.
2. **droidctl's own key** for the team, saved in the OS keyring by an earlier run, while Apple
   still lists its certificate: go-ios (go-codesign) signs with it; the certificate itself comes
   from the provisioning profile, so only the private key is kept.
3. **A new certificate:** an RSA key and CSR made here with `openssl`, sent to Apple
   (`submitDevelopmentCSR`); the key goes to the keyring.

droidctl never revokes a certificate: a full team (Apple's limit, error 7460) is a typed error
that lists what is there, so the user decides (research/ios-signing.md §C.2: revoking Xcode's
certificate would break their own Xcode signing).
"""
import hashlib
import os
import socket
import subprocess
import sys
import tempfile

from droidctl.core import UserError


def account(team):
    return f"cert-{team}"


def sha1(der):
    return hashlib.sha1(der).hexdigest().upper()


def make_csr():
    """(private key PEM, CSR PEM): RSA 2048, made by the `openssl` CLI (macOS and every Linux
    distro have one; the key never leaves this machine except into the keyring)."""
    with tempfile.TemporaryDirectory(prefix="droidctl-csr-") as d:
        os.chmod(d, 0o700)
        key, csr = os.path.join(d, "key.pem"), os.path.join(d, "csr.pem")
        try:
            p = subprocess.run(["openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", key,
                                "-out", csr, "-subj", "/CN=droidctl/C=US"], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            raise UserError(f"cannot run openssl: {e}", "missing-dep",
                            hint="install openssl (Debian/Ubuntu: sudo apt install openssl)")
        if p.returncode != 0:
            raise UserError(f"openssl could not make a signing key: {p.stderr.strip()[-200:]}", "signing")
        with open(key) as f, open(csr) as g:
            return f.read(), g.read()


def machine_name():
    return ("droidctl " + socket.gethostname().split(".")[0])[:40]


def listed(certs):
    """sha1 -> cert entry, for Apple's listAllDevelopmentCerts rows. PURE."""
    return {sha1(c["der"]): c for c in certs if c.get("der")}


def cert_limit_error(certs):
    rows = [f"{c.get('name') or '?'} on {c.get('machine') or '?'} (expires {c.get('expires') or '?'})" for c in certs]
    return UserError("this Apple team already has as many development certificates as Apple allows", "signing",
                     hint="droidctl won't revoke one for you (one may be your Xcode's or a sideloading tool's; "
                          "apps signed with it stop opening). Revoke one you no longer use (developer.apple.com "
                          "for a paid team; a free team's Xcode has no Revoke button: ask your agent, which can "
                          "revoke the one you pick), then run droidctl setup again. Certificates: " + "; ".join(rows),
                     data={"reason": "cert-limit", "certificates": [
                         {k: c.get(k) for k in ("id", "name", "machine", "expires")} for c in certs]})


def ensure(ds, team, say=lambda line: None, keychain=None):
    """{"kind": "keychain", "sha1"} or {"kind": "key", "sha1", "key_pem"} for signing for `team`."""
    from droidctl.ios.apple import secrets
    from droidctl.ios.apple.devservices import CERT_LIMIT
    have = listed(ds.certs(team))
    if keychain is None and sys.platform == "darwin":
        from droidctl.ios import sign
        keychain = sign.keychain_identities()
    for sha in keychain or []:
        if sha in have:
            return {"kind": "keychain", "sha1": sha}
    saved = secrets.load(account(team))
    if saved and saved.get("sha1") in have and saved.get("key_pem"):
        return {"kind": "key", "sha1": saved["sha1"], "key_pem": saved["key_pem"]}
    say("creating a development certificate for droidctl (once per Apple team and machine)…")
    key_pem, csr_pem = make_csr()
    try:
        req = ds.submit_csr(team, csr_pem, machine_name())
    except UserError as e:
        if (e.data or {}).get("code") == CERT_LIMIT or (e.data or {}).get("reason") == "cert-limit":
            raise cert_limit_error(list(have.values())) from None
        raise
    after = listed(ds.certs(team))
    new = [s for s, c in after.items() if s not in have and
           (not req.get("certificateId") or c.get("id") == req.get("certificateId"))]
    if not new:
        raise UserError("Apple accepted the certificate request but doesn't list the new certificate yet", "signing",
                        hint="run droidctl setup again in a minute")
    secrets.save(account(team), {"key_pem": key_pem, "sha1": new[0], "cert_id": req.get("certificateId")})
    return {"kind": "key", "sha1": new[0], "key_pem": key_pem}


def forget(team):
    from droidctl.ios.apple import secrets
    secrets.delete(account(team))
