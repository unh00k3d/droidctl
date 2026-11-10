"""droidctl's own Apple sign-in and provisioning, without Xcode (IOS-INTEGRATION.md §9).

`droidctl login` signs in once (password -> SRP only; 2FA once per machine), picks the team and
keeps Apple's Xcode token in the OS keychain (`apple/secrets.py`); never the password. After
that, a profile for the runner comes with no prompt: register the iPhone, ensure the runner's
App ID, download the team profile (Xcode's "automatic" profile), sign (`sign.py`).

A small non-secret record (`account.json`: Apple ID, team, token expiry, where the secret is)
says which account is in use.
"""
import datetime
import json
import os
import time

from droidctl import device as dev
from droidctl.core import UserError
from droidctl.ios import runner

ACCOUNT_FILE = "account.json"


def _dir():
    d = os.path.join(runner.ios_home(), "apple")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def record():
    """The non-secret account record, or None."""
    try:
        with open(os.path.join(_dir(), ACCOUNT_FILE)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _save_record(rec):
    path = os.path.join(_dir(), ACCOUNT_FILE)
    with open(os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(rec, f, indent=1)
    os.replace(path + ".tmp", path)


def signed_in_team():
    """The team droidctl's own sign-in provisions for, or None."""
    rec = record()
    return rec.get("team") if rec else None


def mode(team):
    """How the runner gets signed for `team`: "apple-id" (droidctl's own sign-in) or "xcode"."""
    forced = os.environ.get("DROIDCTL_IOS_SIGNING")
    if forced in ("apple-id", "xcode"):
        return forced
    return "apple-id" if team and signed_in_team() == team else "xcode"


def _iso_ms(ms):
    if not ms:
        return None
    return datetime.datetime.fromtimestamp(int(ms) / 1000, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------
# login / logout
# --------------------------------------------------------------------------
def login(apple_id, get_password, ask_code, team=None, ask=None, say=print, anisette_url=None, fresh=False):
    """Sign in (or reuse the saved token), then pick the team. `get_password()` is called only
    when a new sign-in is needed. The token is saved as soon as Apple sends it, so a later step
    failing doesn't cost another sign-in (Apple answers 429 to repeated ones)."""
    from droidctl.ios.apple import anisette, devservices, gsa, secrets
    t0 = time.monotonic()
    rec, teams, ds = record(), None, None
    anisette_url = anisette.current(anisette_url or (rec or {}).get("anisette"))   # the same machine identity as last time
    provider = anisette.default_provider(anisette_url)
    sec = secrets.load(rec["account"]) if rec and rec.get("account") else None
    same = rec and (not apple_id or (rec.get("apple_id") or "").lower() == apple_id.strip().lower())
    if sec and same and not fresh:
        ds = devservices.Session(sec["adsid"], sec["token"], provider)
        try:
            teams = ds.teams()
            say("reusing the saved Apple sign-in")
        except UserError as e:
            if (e.data or {}).get("reason") != "token-expired":
                raise
            teams = None
    if teams is None:
        if not apple_id:
            raise UserError("no Apple ID given", "bad-args")
        spd = gsa.login(provider, apple_id, get_password(), ask_code, say=say)
        tok = gsa.app_token(provider, spd)
        where = secrets.save(spd["adsid"], {"adsid": spd["adsid"], "token": tok["token"], "expiry": tok.get("expiry")})
        rec = {"apple_id": spd.get("appleId", apple_id), "account": spd["adsid"], "team": None, "stored": where,
               "token_expires": _iso_ms(tok.get("expiry")), "token_duration_s": tok.get("duration"),
               "signed_in": _now(), "anisette": anisette_url}
        _save_record(rec)
        ds = devservices.Session(spd["adsid"], tok["token"], provider)
        teams = ds.teams()
    if not teams:
        raise UserError("this Apple ID has no developer team", "signing",
                        hint="sign in to https://developer.apple.com once with it to accept the terms, then retry")
    if team:
        pick = next((t for t in teams if t["id"] == team), None)
        if not pick:
            raise UserError(f"team {team} is not on this Apple ID", "bad-args",
                            hint="teams: " + ", ".join(f"{t['name']} ({t['id']})" for t in teams))
    elif len(teams) == 1:
        pick = teams[0]
    else:
        pick = None
        if ask:
            say("This Apple ID has several teams; which one signs droidctl's runner?")
            for i, t in enumerate(teams, 1):
                say(f"    {i}. {t['name']} ({t['id']}){' — free' if t['free'] else ''}")
            got = (ask(f"team [1-{len(teams)}]: ") or "").strip()
            if got.isdigit() and 1 <= int(got) <= len(teams):
                pick = teams[int(got) - 1]
        if not pick:
            raise UserError("this Apple ID has several teams", "bad-args",
                            hint="pick one: droidctl login --team <ID>  (" +
                                 ", ".join(f"{t['name']} ({t['id']})" for t in teams) + ")",
                            data={"teams": teams})
    rec.update(team=pick["id"], team_name=pick["name"], free=pick["free"])
    _save_record(rec)
    return dict(rec, ok=True, ms=round((time.monotonic() - t0) * 1000))


def logout():
    from droidctl.ios.apple import secrets
    rec = record()
    if not rec:
        return {"ok": True, "signed_out": False}
    secrets.delete(rec["account"])
    os.remove(os.path.join(_dir(), ACCOUNT_FILE))
    return {"ok": True, "signed_out": True, "apple_id": rec.get("apple_id")}


def _now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def session():
    """A developer-services session from the saved sign-in."""
    from droidctl.ios.apple import anisette, devservices, secrets
    rec = record()
    if not rec:
        raise UserError("droidctl is not signed in to Apple", "signing", hint="run: droidctl login",
                        data={"reason": "no-login"})
    sec = secrets.load(rec["account"])
    if not sec:
        raise UserError("droidctl's saved Apple sign-in is gone from the keychain", "signing",
                        hint="run: droidctl login", data={"reason": "no-login"})
    return devservices.Session(sec["adsid"], sec["token"], anisette.default_provider(rec.get("anisette"))), rec


# --------------------------------------------------------------------------
# a profile for the runner
# --------------------------------------------------------------------------
def runner_profile(udid, team, device_name=None, say=lambda line: None):
    """Register the iPhone, make sure there is a certificate to sign with, ensure the runner's App
    ID, download the team profile (it lists the team's certificates, so it comes last).
    Returns (profile bytes, identity, timings)."""
    from droidctl.ios import identity
    ds, rec = session()
    if rec["team"] != team:
        raise UserError(f"droidctl is signed in for team {rec['team']}, not {team}", "signing",
                        hint="run: droidctl login --team " + team)
    t = {}
    t0 = time.monotonic()
    ident = identity.ensure(ds, team, say)
    t["identity_ms"] = round((time.monotonic() - t0) * 1000)
    t0 = time.monotonic()
    ds.ensure_device(team, udid, device_name)
    t["device_ms"] = round((time.monotonic() - t0) * 1000)
    t0 = time.monotonic()
    app_id = ds.ensure_app_id(team, runner.runner_bundle(team), "droidctl runner")
    t["app_id_ms"] = round((time.monotonic() - t0) * 1000)
    t0 = time.monotonic()
    prof = ds.team_profile(team, app_id["id"])
    t["profile_ms"] = round((time.monotonic() - t0) * 1000)
    dev.update_device_state(udid, profile_source="apple-id")
    return prof["data"], ident, t


def provision_and_sign(udid, team, device_name=None, say=lambda line: None):
    """Everything up to an installable signed runner. Returns {profile, build, timings}."""
    from droidctl.ios import prebuilt, sign
    t0 = time.monotonic()
    art = prebuilt.ensure(say)
    timings = {"artifact_ms": round((time.monotonic() - t0) * 1000)}
    data, ident, t = runner_profile(udid, team, device_name, say)
    timings.update(t)
    t0 = time.monotonic()
    sign.sign(art, team, data, ident, udid)
    timings["sign_ms"] = round((time.monotonic() - t0) * 1000)
    prof = sign.decode_profile(data)
    exp = prof.get("ExpirationDate")
    return {"profile": {"uuid": prof.get("UUID"), "expires": exp.isoformat() + "Z" if exp else None},
            "build": prebuilt.manifest(art)["build"], "timings": timings}
