"""Xcode's developer services (developerservices2.apple.com, "QH65B2"): XML-plist requests with
the account id + Xcode token and anisette. Free and paid teams alike.

Ported from PlumeImpactor (MIT, © 2025 Samara M) `crates/plume_core/src/developer/`.
Free-team limits (Apple's server rules, `research/ios-signing.md` C.2): 3 installed apps per
device, 10 new App IDs per 7 days, 7-day profiles, a small development-certificate cap (7460).
"""
import json
import plistlib
import uuid

from droidctl.core import UserError
from droidctl.ios import apple
from droidctl.ios.apple import anisette

BASE = "https://developerservices2.apple.com/services/QH65B2"
CLIENT_ID = "XABBG36SBA"
V1 = "https://developerservices2.apple.com/services/v1/"
NO_SUCH_CERT = 7252                     # "There is no 'ios' certificate with serial number …"
CERT_LIMIT = 7460
APP_ID_LIMIT = 9120


class DevError(UserError):
    pass


def _keep_unreadable(data):
    """Keep an unreadable reply for diagnosis: a private file (it holds account details)."""
    import os
    from droidctl import device as dev
    d = os.path.join(dev.home(), "ios", "apple")
    os.makedirs(d, mode=0o700, exist_ok=True)
    path = os.path.join(d, "last-unreadable-reply")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data or b"")
    return path


def parse_reply(data, status=200, keep=True):
    """A QH reply -> its dict, or DevError for a non-zero resultCode. PURE unless unreadable."""
    try:
        r = plistlib.loads(data)
    except Exception as e:
        from droidctl.ios.apple import gsa
        try:
            r = gsa.parse_spd(data or b"")
        except UserError:
            path = _keep_unreadable(data) if keep else None
            raise DevError(f"Apple's developer services sent an unreadable reply (HTTP {status}, {len(data or b'')} "
                           f"bytes; {type(e).__name__}: {str(e)[:120]})", "device",
                           hint=f"the reply was kept for diagnosis in {path} (private; it holds account details)"
                           if path else None, data={"reason": "unreadable", "status": status}) from None
    code = r.get("resultCode", 0)
    if code:
        msg = r.get("userString") or r.get("resultString") or "unknown error"
        kind, hint, reason = "signing", None, "apple-error"
        if code == CERT_LIMIT:
            hint, reason = "the team already has its maximum of development certificates", "cert-limit"
        elif code == APP_ID_LIMIT:
            hint, reason = "a free team may create 10 App IDs per 7 days; wait, or reuse an existing one", "app-id-limit"
        elif code in (1100, 1101):              # "session expired" / "not signed in"
            kind, hint, reason = "signing", "run: droidctl login", "token-expired"
        raise DevError(f"Apple: {msg} ({code})", kind, hint=hint, data={"reason": reason, "code": code})
    return r


def is_free(team):
    """A free (personal) team: Apple keeps `xcodeFreeOnly` False for one and says it in the
    membership, "Xcode Free Provisioning Program" (measured 2026-11-09). PURE."""
    return bool(team.get("xcodeFreeOnly")) or any(
        "free" in (m.get("name") or "").lower() for m in team.get("memberships") or [])


class Session:
    def __init__(self, adsid, token, provider):
        self.adsid, self.token, self.provider = adsid, token, provider

    def call(self, action, platform="ios", **body):
        url = f"{BASE}/{platform + '/' if platform else ''}{action}.action?clientId={CLIENT_ID}"
        h = anisette.headers(self.provider, client_info=True, app_info=True)
        h.update({"Content-Type": "text/x-xml-plist", "Accept": "text/x-xml-plist", "Accept-Language": "en-us",
                  "User-Agent": "Xcode", "X-Apple-I-Identity-Id": self.adsid, "X-Apple-GS-Token": self.token})
        payload = dict(body, clientId=CLIENT_ID, protocolVersion="QH65B2", requestId=str(uuid.uuid4()).upper())
        status, data = apple.request(url, plistlib.dumps(payload), h, method="POST")
        if status == 401 or status == 403:
            raise DevError("Apple's developer services no longer accept the saved sign-in", "signing",
                           hint="run: droidctl login", data={"reason": "token-expired"})
        return parse_reply(data, status)

    # -- teams
    def teams(self):
        return [{"id": t.get("teamId"), "name": t.get("name"), "type": t.get("type"),
                 "free": is_free(t), "status": t.get("status")}
                for t in self.call("listTeams", platform=None).get("teams", [])]

    # -- devices
    def devices(self, team):
        return [{"id": d.get("deviceId"), "udid": d.get("deviceNumber"), "name": d.get("name")}
                for d in self.call("listDevices", teamId=team).get("devices", [])]

    def ensure_device(self, team, udid, name):
        for d in self.devices(team):
            if (d["udid"] or "").upper() == udid.upper():
                return d
        d = self.call("addDevice", teamId=team, deviceNumber=udid, name=name or "iPhone").get("device", {})
        return {"id": d.get("deviceId"), "udid": d.get("deviceNumber"), "name": d.get("name")}

    # -- App IDs
    def app_ids(self, team):
        return [{"id": a.get("appIdId"), "identifier": a.get("identifier"), "name": a.get("name"),
                 "expires": a.get("expirationDate")} for a in self.call("listAppIds", teamId=team).get("appIds", [])]

    def ensure_app_id(self, team, identifier, name):
        for a in self.app_ids(team):
            if a["identifier"] == identifier:
                return a
        name = "".join(c for c in name if c.isascii() and c.isalnum()) or "droidctl"
        a = self.call("addAppId", teamId=team, identifier=identifier, name=name).get("appId", {})
        return {"id": a.get("appIdId"), "identifier": a.get("identifier"), "name": a.get("name")}

    # -- certificates
    def certs(self, team):
        return [{"id": c.get("certificateId"), "serial": c.get("serialNumber") or c.get("serialNum"),
                 "name": c.get("name"), "machine": c.get("machineName"), "status": c.get("status"),
                 "expires": c.get("expirationDate"), "der": c.get("certContent")}
                for c in self.call("listAllDevelopmentCerts", teamId=team).get("certificates", [])]

    def submit_csr(self, team, csr_pem, machine_name):
        r = self.call("submitDevelopmentCSR", teamId=team, csrContent=csr_pem,
                      machineId=str(uuid.uuid4()).upper(), machineName=machine_name)
        return r.get("certRequest", {})

    def revoke_cert(self, team, cert):
        """Revoke one development certificate (a `certs()` row). droidctl never calls this on its
        own: only on the user's explicit choice of that certificate (apps signed with it stop
        launching). Older "iOS Development" certificates go through the classic action; Xcode's
        unified "Apple Development" ones aren't there (7252, "no 'ios' certificate") but in the
        v1 API, deleted by id (measured 2026-11-10: HTTP 204)."""
        try:
            return self.call("revokeDevelopmentCert", teamId=team, serialNumber=cert["serial"])
        except DevError as e:
            if (e.data or {}).get("code") != NO_SUCH_CERT or not cert.get("id"):
                raise
        status, data = self.v1(f"certificates/{cert['id']}", {"teamId": team}, "DELETE")
        if status not in (200, 204):
            raise DevError(f"Apple refused to revoke certificate {cert['id']} (HTTP {status})", "signing",
                           data={"reason": "revoke-failed", "status": status})
        return {}

    def v1(self, path, body, method):
        """Apple's v1 (JSON:API) developer services, as Xcode calls them: POST with the real
        method in X-HTTP-Method-Override."""
        h = anisette.headers(self.provider, client_info=True, app_info=True)
        h.update({"Content-Type": "application/vnd.api+json", "Accept": "application/vnd.api+json",
                  "Accept-Language": "en-us", "User-Agent": "Xcode", "X-Apple-I-Identity-Id": self.adsid,
                  "X-Apple-GS-Token": self.token, "X-HTTP-Method-Override": method})
        return apple.request(V1 + path, json.dumps(body).encode(), h, method="POST")

    # -- profiles
    def team_profile(self, team, app_id_id):
        """The team provisioning profile for one App ID (Xcode's "automatic" profile): it lists
        the team's registered devices and development certificates."""
        p = self.call("downloadTeamProvisioningProfile", teamId=team, appIdId=app_id_id).get("provisioningProfile", {})
        if not p.get("encodedProfile"):
            raise DevError("Apple sent no provisioning profile", "signing")
        return {"uuid": p.get("UUID"), "name": p.get("name"), "expires": p.get("dateExpire"),
                "data": p["encodedProfile"]}
