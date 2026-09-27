"""TESTAPP group 0: the agent's socket auth, from an app uid (not adb)."""
import time

from tests.e2e.conftest import PKG, Scenario, adb, dc


def test_an_app_uid_cannot_talk_to_the_agent():
    """TESTAPP expects -32001 (our peer-UID check). On the SM-N950F (SELinux
    enforcing) the connect itself is refused with EACCES before our check runs, so
    either answer proves an app can't drive the phone through the agent."""
    # launched here, not via the fixture: the probe answers within milliseconds and
    # the fixture's post-launch `logcat -c` would erase it
    adb("logcat", "-c")
    adb("shell", "am", "start", "-n", f"{PKG}/.Main", "--es", "s", "peer_uid_probe", "--ez", "reset", "true")
    sc = Scenario("peer_uid_probe")
    deadline = time.time() + 8
    while not sc.dta("probe") and time.time() < deadline:
        time.sleep(0.2)
    ev = sc.dta("probe")
    assert ev, "the probe never reported"
    reply, uid = ev[0]["reply"], ev[0]["uid"]
    assert uid >= 10000                                      # an app uid, not shell/root
    assert "-32001" in reply or "EACCES" in reply or "Permission denied" in reply, reply
    assert '"result"' not in reply
    assert dc("ping")["peer_uid"] == 2000                   # while adb still gets through

