"""Where droidctl keeps Apple credentials it was given: the OS keychain, never argv or logs.

- macOS: the login keychain, through `security -i` (commands on stdin, so the secret never
  appears in a process list).
- Linux: the Secret Service, through `secret-tool store` (the secret on stdin).
- Otherwise (no keychain, a headless box): a 0600 JSON file under droidctl's home, said so.

What is stored: the account id, the Xcode app token and its expiry, the Apple ID's address and
the team chosen. Never the password.
"""
import base64
import json
import os
import shutil
import subprocess
import sys

from droidctl import device as dev

SERVICE = "dev.droidctl.apple"


def _file(account):
    d = os.path.join(dev.home(), "ios", "apple")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return os.path.join(d, account.replace("/", "_") + ".json")


def backend():
    if sys.platform == "darwin" and shutil.which("security"):
        return "keychain"
    if sys.platform.startswith("linux") and shutil.which("secret-tool") and os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return "secret-service"
    return "file"


def _quote(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save(account, value):
    """Store `value` (a JSON-able dict) under `account`. Returns where it went."""
    blob = base64.b64encode(json.dumps(value).encode()).decode()
    where = backend()
    if where == "keychain":
        cmd = f"add-generic-password -U -s {SERVICE} -a {_quote(account)} -l {_quote('droidctl Apple sign-in')} -w {blob}\n"
        p = subprocess.run(["security", "-i"], input=cmd, text=True, capture_output=True, timeout=30)
        if p.returncode == 0 and "error" not in (p.stderr or "").lower():
            return where
    elif where == "secret-service":
        p = subprocess.run(["secret-tool", "store", "--label=droidctl Apple sign-in", "service", SERVICE,
                            "account", account], input=blob, text=True, capture_output=True, timeout=30)
        if p.returncode == 0:
            return where
    path = _file(account)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(blob)
    return "file"


def load(account):
    blob = None
    where = backend()
    if where == "keychain":
        p = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-a", account, "-w"],
                           capture_output=True, text=True, timeout=30)
        blob = p.stdout.strip() if p.returncode == 0 else None
    elif where == "secret-service":
        p = subprocess.run(["secret-tool", "lookup", "service", SERVICE, "account", account],
                           capture_output=True, text=True, timeout=30)
        blob = p.stdout.strip() if p.returncode == 0 else None
    if not blob and os.path.exists(_file(account)):
        with open(_file(account)) as f:
            blob = f.read().strip()
    if not blob:
        return None
    try:
        return json.loads(base64.b64decode(blob))
    except ValueError:
        return None


def delete(account):
    where = backend()
    if where == "keychain":
        subprocess.run(["security", "delete-generic-password", "-s", SERVICE, "-a", account],
                       capture_output=True, timeout=30)
    elif where == "secret-service":
        subprocess.run(["secret-tool", "clear", "service", SERVICE, "account", account], capture_output=True, timeout=30)
    try:
        os.remove(_file(account))
    except FileNotFoundError:
        pass
