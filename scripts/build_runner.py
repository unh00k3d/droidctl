"""Build the prebuilt iOS runner (unsigned, XCTest stripped) into droidctl/assets/droidctl-runner.zip.

Needs a Mac with Xcode (DEVELOPER_DIR if Xcode isn't in /Applications). `make runner` runs it;
commit the zip. tests/test_packaging.py fails when the runner sources change without it.
CI (.github/workflows/ci.yml) runs the same script on a GitHub macOS runner."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from droidctl.ios import prebuilt  # noqa: E402


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else prebuilt.BUNDLED
    with tempfile.TemporaryDirectory(prefix="droidctl-runner-") as d:
        log = os.path.join(tempfile.gettempdir(), "droidctl-runner-build.log")     # kept for a failed build
        art = prebuilt.build_unsigned(os.path.join(d, "artifact"), log=log)
        sha = prebuilt.make_zip(art, out)
        m = prebuilt.manifest(art)
    print(f"{out}  build {m['build']}  {m['xcode']}  {os.path.getsize(out)} bytes  sha256 {sha}")


if __name__ == "__main__":
    main()
