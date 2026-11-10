"""iOS I3: teardown's plan, doctor's parsers and checks, profile renewal (real host output in
tests/fixtures/ios-host, captured with `pmset -g` / `pmset -g therm` on the dev Mac)."""
import datetime
import os

import pytest

from droidctl.ios import manage, runner
from droidctl.ios.setup import is_old_runner

HOST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "ios-host")
NOW = datetime.datetime(2026, 10, 21, 12, 0, tzinfo=datetime.timezone.utc)


def read(name):
    with open(os.path.join(HOST, name)) as f:
        return f.read()


def test_teardown_removes_only_droidctls_runners():
    apps = [{"bundle": "dev.droidctl.rae309727.runner.xctrunner", "developer": True},
            {"bundle": runner.runner_bundle("OTHERTEAM1"), "developer": True},
            {"bundle": "dev.droidctl.s1.runner.xctrunner", "developer": True},   # research build: the user's
            {"bundle": "dev.droidctl.s1.testapp", "developer": True},
            {"bundle": "com.example.app", "developer": True}]
    assert manage.plan(apps) == ["dev.droidctl.rae309727.runner.xctrunner", runner.runner_bundle("OTHERTEAM1")]


def test_setup_frees_slots_only_from_other_teams_product_runners():
    team = "TEAMID0000"
    assert is_old_runner(runner.runner_bundle("OTHERTEAM1"), team)
    assert not is_old_runner(runner.runner_bundle(team), team)
    assert not is_old_runner("dev.droidctl.s1.runner.xctrunner", team)
    assert not is_old_runner("dev.droidctl.jl3.runner.xctrunner", team)


def test_pmset_reads_sleep_and_who_prevents_it():
    pm = manage.parse_pmset(read("pmset.txt"))
    assert pm["sleep"] == "1" and pm["displaysleep"] == "10"
    assert "xcodebuild" in pm["sleep_prevented_by"]
    assert manage.parse_therm(read("pmset_therm.txt")) == 100


@pytest.mark.parametrize("expires,ok,word", [
    (None, None, "unknown"),
    ("2026-10-20T10:28:00Z", False, "expired"),
    ("2026-10-22T02:00:00Z", True, "re-signs"),
    ("2026-10-27T10:28:00Z", True, "days"),
])
def test_expiry_state(expires, ok, word):
    got, detail = manage.expiry_state(expires, now=NOW)
    assert got is ok and word in detail


def test_renewal_is_due_within_a_day_or_after_expiry():
    assert manage.renew_due("2026-10-22T02:00:00Z", now=NOW)
    assert manage.renew_due("2026-10-20T10:28:00Z", now=NOW)
    assert not manage.renew_due("2026-10-27T10:28:00Z", now=NOW)
    assert not manage.renew_due(None, now=NOW)


def test_the_tested_xcode_ios_pairs():
    assert manage.pair_tested("Xcode 26.2", "18.7.8")
    assert not manage.pair_tested("Xcode 27.0", "18.7.8")
    assert not manage.pair_tested("Xcode 26.2", "26.0")


def test_teardown_discloses_what_it_does_not_undo():
    lines = manage.disclosures(manage.TRUST_NOTE)
    text = " ".join(lines)
    for what in ("Developer Mode", "disk image", "Trust", "certificate"):
        assert what in text


class Profiles:
    """Stands in for the built runner's profile; `build(resign=True)` gets `issued` from Xcode."""

    def __init__(self, monkeypatch, current, issued):
        from droidctl.ios import setup
        self.current, self.issued, self.builds = current, issued, []
        monkeypatch.setattr(setup, "profile", lambda t: self.current)
        monkeypatch.setattr(runner, "xctestrun", lambda t: "/x.xctestrun")

        def build(u, t, resign=False):
            self.builds.append(resign)
            self.current = self.issued
        monkeypatch.setattr(runner, "build", build)


SOON = {"uuid": "old", "expires": "2026-10-22T02:00:00Z"}           # within a day of NOW
GONE = {"uuid": "old", "expires": "2000-01-01T00:00:00Z"}
FRESH = {"uuid": "new", "expires": "2999-01-01T00:00:00Z"}
UDID = "00008101-00AAAAAAAAAAAAAA"


@pytest.fixture
def state(monkeypatch, tmp_path):
    from droidctl import device as dev
    monkeypatch.setenv("DROIDCTL_HOME", str(tmp_path))
    monkeypatch.setattr(manage, "renew_due", lambda e, now=None, f=manage.renew_due: f(e, now or NOW))
    monkeypatch.setattr(manage, "expired", lambda e, now=None, f=manage.expired: f(e, now or NOW))
    dev.update_device_state(UDID, backend="ios", team="TEAMID0000", built=runner.build_id("TEAMID0000"))
    return dev


def test_setup_resigns_and_reports_a_new_profile(monkeypatch, state):
    from droidctl.ios import setup
    p = Profiles(monkeypatch, SOON, FRESH)
    ctx = setup.Ctx(UDID, "TEAMID0000", lambda line: None)
    assert "re-signed (new profile expires 2999" in setup.step_build(ctx) and ctx.info["renewed"]
    assert setup.step_build(ctx) == "already built" and p.builds == [True]


def test_xcode_keeping_a_valid_profile_is_reported_and_tried_once(monkeypatch, state):
    """Measured 2026-10-22: Xcode re-signed with the same cached profile 6 days before expiry."""
    from droidctl.ios import setup
    p = Profiles(monkeypatch, SOON, SOON)
    ctx = setup.Ctx(UDID, "TEAMID0000", lambda line: None)
    assert "Xcode kept the current profile" in setup.step_build(ctx) and "renewed" not in ctx.info
    assert setup.step_build(ctx) == "already built"                 # not again for the same profile
    runner.renew_if_due(UDID, "TEAMID0000")
    assert p.builds == [True]


def test_an_expired_profile_is_retried_until_xcode_issues_a_new_one(monkeypatch, state):
    from droidctl.core import UserError
    p = Profiles(monkeypatch, GONE, GONE)
    for _ in range(2):
        with pytest.raises(UserError) as e:
            runner.renew_if_due(UDID, "TEAMID0000")
        assert e.value.kind == "profile-expired" and "Accounts" in e.value.hint
    p.issued = FRESH
    old, new = runner.renew_if_due(UDID, "TEAMID0000")
    assert runner.renewed(old, new) and p.builds == [True, True, True]
    assert runner.renew_if_due(UDID, "TEAMID0000") is None


def test_a_profile_far_from_expiry_is_left_alone(monkeypatch, state):
    p = Profiles(monkeypatch, FRESH, FRESH)
    assert runner.renew_if_due(UDID, "TEAMID0000") is None and p.builds == []


# -- easier setup (2026-10-22)
def test_teams_come_from_xcodes_signed_in_accounts():
    import json
    from droidctl.ios import setup
    with open(os.path.join(HOST, "xcode_teams.json")) as f:
        prefs = json.load(f)                        # scrubbed copy of Xcode's own record
    assert setup.parse_xcode_teams(prefs) == [{"team": "TEAMID0000", "name": "Test Name 0 (Personal Team)", "free": True, "signed_in": None}]
    assert setup.parse_xcode_teams({}) == []


def _ctx(monkeypatch, found, answer=None):
    from droidctl.ios import setup
    monkeypatch.setattr(setup, "all_teams", lambda: found)
    opened = []
    monkeypatch.setattr(setup.subprocess, "Popen", lambda *a, **k: opened.append(a[0]))
    monkeypatch.setattr(setup.devices, "developer_dir", lambda: "/Applications/Xcode.app/Contents/Developer")
    ctx = setup.Ctx("U", None, lambda line: None, (lambda q: answer) if answer is not None else None)
    return setup, ctx, opened


def test_no_team_opens_xcode_once_and_says_to_sign_in(monkeypatch):
    setup, ctx, opened = _ctx(monkeypatch, [])
    for _ in range(3):                               # the step is retried every second while waiting
        with pytest.raises(setup.Need) as e:
            setup.step_team(ctx)
    assert opened == [["open", "-a", "/Applications/Xcode.app"]]
    assert "Apple ID" in " ".join(e.value.instructions)


def test_several_teams_are_picked_interactively_or_named_in_the_error(monkeypatch):
    two = [{"team": "TEAMA00000", "name": "A", "free": True}, {"team": "TEAMB00000", "name": "B", "free": False}]
    setup, ctx, _ = _ctx(monkeypatch, two, answer="2")
    assert "TEAMB00000" in setup.step_team(ctx) and ctx.team == "TEAMB00000"
    setup, ctx, _ = _ctx(monkeypatch, two)
    from droidctl.core import UserError
    with pytest.raises(UserError) as e:
        setup.step_team(ctx)
    assert "--team" in e.value.hint and "paid team" in e.value.hint


# -- an Apple ID signed out of Xcode (2026-10-22: the team record stays, xcodebuild says "No Accounts")
def test_a_signed_out_account_is_told_apart_from_a_signed_in_one():
    import json
    from droidctl.ios import setup
    with open(os.path.join(HOST, "xcode_teams_signed_out.json")) as f:
        out = setup.parse_xcode_teams(json.load(f))
    assert [t["signed_in"] for t in out] == [False]
    with open(os.path.join(HOST, "xcode_teams.json")) as f:
        assert setup.parse_xcode_teams(json.load(f))[0]["signed_in"] is None   # not recorded: not judged


def test_signed_out_keeps_a_working_runner_with_a_warning(monkeypatch, state):
    setup, ctx, opened = _ctx(monkeypatch, [{"team": "TEAMID0000", "name": "A", "free": True, "signed_in": False}])
    ctx.udid = UDID
    monkeypatch.setattr(runner, "xctestrun", lambda t: "/x.xctestrun")
    monkeypatch.setattr(setup, "profile_expiry", lambda t: "2026-10-27T10:28:00Z")
    assert "sees no Apple ID" in setup.step_team(ctx) and "sign in again" in ctx.warnings[0] and opened == []


def test_signed_out_needs_a_sign_in_when_a_profile_is_needed(monkeypatch, state):
    setup, ctx, opened = _ctx(monkeypatch, [{"team": "TEAMID0000", "name": "A", "free": True, "signed_in": False}])
    ctx.udid = UDID
    monkeypatch.setattr(runner, "xctestrun", lambda t: "/x.xctestrun")
    monkeypatch.setattr(setup, "profile_expiry", lambda t: "2026-10-22T02:00:00Z")    # renewal due
    monkeypatch.setattr(setup, "cached_profile", lambda t: None)
    with pytest.raises(setup.Need) as e:
        setup.step_team(ctx)
    assert e.value.reason == "signed-out" and opened == [["open", "-a", "/Applications/Xcode.app"]]


@pytest.mark.parametrize("built,cached,ok", [("2026-10-27T10:28:00Z", None, True), (None, "2026-10-27T10:28:00Z", True),
                                              (None, None, False), (None, "2026-10-22T02:00:00Z", False)])
def test_signed_out_after_teardown_uses_a_team_with_a_valid_profile(monkeypatch, state, built, cached, ok):
    """`teardown` forgets the phone's team, and a new droidctl home has no build: a signed-out team whose
    profile (the built runner's, else Xcode's cached one) is valid and not due still signs."""
    setup, ctx, opened = _ctx(monkeypatch, [{"team": "TEAMID0000", "name": "A", "free": True, "signed_in": False}])
    ctx.udid = UDID
    state.update_device_state(UDID, team=None)
    monkeypatch.setattr(setup, "profile_expiry", lambda t: built)
    monkeypatch.setattr(setup, "cached_profile", lambda t: cached and {"uuid": "u", "expires": cached})
    if ok:
        assert "sees no Apple ID" in setup.step_team(ctx) and ctx.team == "TEAMID0000" and opened == []
    else:
        with pytest.raises(setup.Need) as e:
            setup.step_team(ctx)
        assert e.value.reason == "no-team"


def test_xcodebuilds_no_accounts_is_a_sign_in_hint():
    kind, msg, hint, reason = runner.classify_output(
        "DroidctlRunner.xcodeproj: error: No Accounts: Add a new account in Accounts settings. (in target 'DroidctlRunner')")
    assert (kind, reason) == ("signing", "signed-out") and "Accounts" in hint
