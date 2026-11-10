"""The parts of the easier setup that can run on an iPhone that is already set up.
Run with DROIDCTL_IOS_UDID=<hardware UDID>, the phone unlocked and set up once.

Not here (need a phone that was never set up, or another Mac): the Trust prompt raised by
`devicectl manage pair`, Xcode creating the first certificate, the interactive team pick."""
import json
import os
import subprocess
import sys

from droidctl.ios import manage, runner, setup
from tests.e2e.ios.test_i2 import ROOT, UDID, dc

TEAM = os.environ.get("DROIDCTL_IOS_TEAM", "")


def team():
    from droidctl import device as dev
    return TEAM or dev.device_state(UDID).get("team")


def test_the_developer_mode_reveal_is_accepted():
    """AMFI action 0 over lockdown's TLS session; it only shows the menu (already shown here)."""
    setup.reveal_developer_mode(UDID)


def test_a_resign_reports_honestly_and_the_runner_still_starts():
    """Xcode keeps a still-valid cached profile (2026-10-22: same UUID 6 days before expiry), so
    a re-sign must not claim a renewal it didn't get, and must leave a working runner."""
    t = team()
    old, new = runner.resign(UDID, t)
    assert new and not manage.expired(new["expires"])
    if runner.renewed(old, new):
        assert new["expires"] > old["expires"]
    else:
        assert new["uuid"] == old["uuid"]
    assert dc("snapshot")["ok"]


def test_setup_from_an_empty_droidctl_home(tmp_path):
    """A Mac droidctl has never run on: no state, no build. Setup builds from scratch, signs,
    installs over the existing runner and starts it; then a command works in that home."""
    env = dict(os.environ, DROIDCTL_HOME=str(tmp_path))

    def run(*args, timeout=600):
        p = subprocess.run([sys.executable, "-m", "droidctl", *args, "-d", UDID, "--json"], env=env, cwd=ROOT,
                           capture_output=True, text=True, timeout=timeout)
        return json.loads(p.stdout)
    try:
        s = run("setup")
        assert s["ok"], s
        assert s["steps"] == [k for k, _, _ in setup.STEPS] and s["team"] == team()
        assert s["profile_expires"] and not manage.expired(s["profile_expires"])
        assert run("snapshot")["ok"]
    finally:
        run("teardown", "--keep-apk", timeout=120)        # stop it; the runner stays installed
    assert dc("snapshot")["ok"]                             # the usual home starts it again


def test_interactive_setup_in_a_terminal():
    """`droidctl setup` typed by a person: the intro, numbered steps, and a ✓ for each."""
    import pty
    import select
    import time
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(ROOT)
        os.execv(sys.executable, [sys.executable, "-m", "droidctl", "setup", "-d", UDID])
    out, end = b"", time.monotonic() + 300
    while time.monotonic() < end:
        r, _, _ = select.select([fd], [], [], 1.0)
        if r:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
    _, status = os.waitpid(pid, 0)
    text = out.decode("utf-8", "replace")
    assert os.waitstatus_to_exitcode(status) == 0, text
    assert "Setting up droidctl for the iPhone" in text
    for n, (_, title, _) in enumerate(setup.STEPS, 1):
        assert f"✓ [{n}/{len(setup.STEPS)}] {title}" in text, text
