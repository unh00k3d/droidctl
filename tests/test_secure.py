"""FLAG_SECURE detection for the screencap path (API < 30), on real dumpsys captures."""
import pathlib

from droidctl import device as dev

D = pathlib.Path(__file__).parent / "fixtures" / "dumpsys"


def test_a_visible_secure_window_is_found():
    wins = dev.secure_windows((D / "window-secure.txt").read_text())
    assert wins == ["dev.droidctl.testapp/dev.droidctl.testapp.Main"]


def test_no_secure_window_on_a_plain_screen():
    assert dev.secure_windows((D / "window-plain.txt").read_text()) == []
