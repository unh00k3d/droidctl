"""Apple's services for signing without Xcode (`research/ios-signing.md`, IOS-INTEGRATION.md §9).

- `anisette`: the machine headers Apple's login expects (AOSKit on macOS; an anisette-v3 server on Linux).
- `gsa`: the GrandSlam login (SRP; 2FA) and the Xcode app token.
- `devservices`: Xcode's developer services (teams, devices, App IDs, certificates, team profiles).
- `asc`: the App Store Connect API with a team key (paid teams).
- `secrets`: where the token lives (the OS keychain; a 0600 file as the fallback).

Protocol details follow PlumeImpactor (MIT, © 2025 Samara M), xtool (MIT) and isideload (MIT).
"""
import os
import ssl
import urllib.error
import urllib.request

from droidctl.core import UserError

ROOT_PEM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apple_root.pem")


def tls_context():
    """The usual public roots plus the Apple Root CA: gsa.apple.com chains to Apple's own root
    (which Python's default store lacks; sha256 B0:B1:73:0E…F0:24, the copy in macOS's
    SystemRootCertificates keychain), developerservices2 to DigiCert Global Root G2 (2026-11-09)."""
    ctx = ssl.create_default_context()
    try:
        import certifi                     # a python.org build on macOS has no default store
        ctx.load_verify_locations(cafile=certifi.where())
    except ImportError:
        pass
    ctx.load_verify_locations(cafile=ROOT_PEM)
    return ctx


def gunzip(data):
    """Developer services gzip their replies unasked (measured 2026-11-09). PURE."""
    if data[:2] == b"\x1f\x8b":
        import gzip
        try:
            return gzip.decompress(data)
        except (OSError, EOFError):
            return data
    return data


def request(url, body=None, headers=None, method=None, timeout=30.0):
    """(status, body bytes, decompressed). HTTP errors come back as their status, not as exceptions."""
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, context=tls_context(), timeout=timeout) as r:
            return r.status, gunzip(r.read())
    except urllib.error.HTTPError as e:
        return e.code, gunzip(e.read())
    except (urllib.error.URLError, OSError) as e:
        raise UserError(f"can't reach Apple ({url.split('/')[2]}): {getattr(e, 'reason', e)}", "connection",
                        hint="check the internet connection") from None
