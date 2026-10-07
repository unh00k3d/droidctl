"""The shared core, ported from chromectl: output, errors and the error vocabulary.

Commands return payload dicts and never print; `emit` runs once, at the edge
(the CLI's dispatch today, the daemon's reply later), so `--json` means the same
thing everywhere: one JSON value on stdout, nothing else.
"""
import json
import sys
import time
from datetime import datetime, timezone


# Lazy rich. Importing rich costs ~30-40 ms (measured in chromectl) and is only
# needed to render for a human. Agents run with --json and never touch it, so
# `console`/`err` build the real Console on first use.
class _LazyConsole:
    def __init__(self, **kw):
        self._kw = kw
        self._real = None

    def __getattr__(self, name):
        if self._real is None:
            from rich.console import Console
            self._real = Console(**self._kw)
        return getattr(self._real, name)


console = _LazyConsole()
err = _LazyConsole(stderr=True, style="red")


def out_json(obj):
    """Print plain (pipeable) JSON to stdout."""
    print(json.dumps(obj, indent=2, default=str))


class UserError(RuntimeError):
    """A failure caused by the request or the device state, not by a bug.

    Carries a machine-readable `kind` (one of ERROR_KINDS) so --json callers can
    branch on it instead of scraping prose, and an optional `hint` telling a
    human or agent what to do next. Raised instead of exiting so a `run` step
    can fail alone.
    """

    def __init__(self, message, kind="error", hint="", data=None):
        super().__init__(message)
        self.kind = kind
        self.hint = hint
        self.data = data          # structured detail for --json (candidates, what is there now, ...)


# The whole `error.kind` vocabulary, in one place. Docs are generated from it
# and a unit test greps the source to prove nothing new slipped in unlisted.
ERROR_KINDS = {
    # generic (from chromectl)
    "error": "unclassified failure (the default)",
    "bad-args": "the arguments contradict each other or are missing",
    "not-found": "the thing named does not exist (a file, a binary, an element)",
    "timeout": "gave up waiting",
    "missing-dep": "an external tool this command needs is not installed",
    "connection": "the device agent's socket is unreachable or was closed (forward lost, service not running, or unbound by Appium/uiautomator); with data.maybe_performed the action may already have run: snapshot before repeating it",
    # device and setup
    "no-device": "no device attached, or none matches -d / ANDROID_SERIAL",
    "adb": "an adb command failed (its own message is passed through)",
    "not-installed": "the droidctl agent is not installed or its service is not enabled (run: droidctl setup)",
    "device": "the device agent rejected the request (its own message is passed through)",
    "suppressed": "another UiAutomation client (Appium/uiautomator2) is suppressing accessibility services, or (uiautomation backend) holds the one UiAutomation",
    "screen-off": "the screen is off or locked",
    "secure-window": "the window is FLAG_SECURE, so it cannot be captured",
    # resolving and acting
    "stale-ref": "the ref no longer resolves (gone, shifted or occupied); re-run snapshot",
    "ambiguous": "the locator matched more than one element",
    "occluded": "the element is covered (by another window or the keyboard)",
    "offscreen": "the element is outside the visible area; scroll to it first",
    "disabled": "the element is disabled",
    "no-change": "the action had no visible effect (with --expect-change)",
    "unsupported": "this device (API level) or element does not support the operation",
    # host daemon
    "no-daemon": "the daemon is not running and auto-start is disabled",
}


def _utc(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Clock:
    """When a command ran, on the host's clock: `t_start`/`t_end` (UTC ISO-8601)
    and `elapsed_ms` go into every --json result and error, so actions line up
    with a proxy's history or a capture without bracketing each call with `date`."""

    def __init__(self):
        self.wall, self.mono = time.time(), time.monotonic()

    def stamp(self):
        ms = round((time.monotonic() - self.mono) * 1000)
        return {"t_start": _utc(self.wall), "t_end": _utc(self.wall + ms / 1000), "elapsed_ms": ms}


def emit(a, payload, render=None):
    """Agent mode prints the payload as JSON; human mode runs the renderer."""
    if getattr(a, "json", False):
        out_json(payload)
    elif render is not None:
        render(payload)
    return payload


def die(args, kind, message, hint="", data=None, mode=None, clock=None):
    """Report a fatal error the way the caller asked for it, then exit 1.

    With --json the error is a JSON object on stdout, so an agent parsing stdout
    gets a value either way instead of an empty string plus red prose on stderr.
    """
    if getattr(args, "json", False):
        error = {"kind": kind, "message": str(message)}
        if hint:
            error["hint"] = hint
        if data:
            error["data"] = data
        out_json({"ok": False, "error": error, **({"mode": mode} if mode else {}),
                  **(clock.stamp() if clock else {})})
    else:
        err.print(f"{kind}: {message}" + (f"\n{hint}" if hint else ""), markup=False)
    sys.exit(1)
