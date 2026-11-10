"""Unit tests never see a real iPhone or the user's own state.

- usbmuxd lookups go to a closed port unless a test installs its own FakeUsbmux (it sets
  USBMUXD_SOCKET_ADDRESS itself). Set at import time so the daemon subprocesses the tests
  start inherit it.
- Every test gets a fresh, short DROIDCTL_HOME (the daemon's unix socket lives there), so no
  test reads the real Apple sign-in record, writes into ~/.droidctl or sees another test's state.
- The iPhone backend is Xcode's unless a test picks go-ios (so Linux CI runs the same tests).

e2e runs (DROIDCTL_IOS_UDID / DROIDCTL_SERIAL) keep the real usbmuxd and home."""
import os
import shutil
import tempfile

import pytest

UNIT = not os.environ.get("DROIDCTL_IOS_UDID") and not os.environ.get("DROIDCTL_SERIAL")

if UNIT:
    os.environ["USBMUXD_SOCKET_ADDRESS"] = "127.0.0.1:1"


@pytest.fixture(autouse=True)
def _private_droidctl_home(monkeypatch):
    if not UNIT:
        yield
        return
    d = tempfile.mkdtemp(prefix="dct-", dir="/tmp")
    monkeypatch.setenv("DROIDCTL_HOME", d)
    # the iPhone tests fake Xcode's tools; go-ios tests ask for that backend themselves
    monkeypatch.setenv("DROIDCTL_IOS_BACKEND", "xcode")
    yield d
    shutil.rmtree(d, ignore_errors=True)
