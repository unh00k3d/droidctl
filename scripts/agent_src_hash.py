"""Hash of everything the agent APK is built from. `make apk` writes it next to the
committed APK (droidctl/assets/droidctl-agent.src.sha256); tests/test_packaging.py
fails when the sources changed but the APK was not rebuilt."""
import hashlib
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
INPUTS = ["android/agent/src", "android/agent/build.gradle.kts", "android/agent/proguard-rules.pro",
          "android/build.gradle.kts", "android/settings.gradle.kts", "android/gradle.properties"]


def agent_src_hash(root=ROOT):
    h = hashlib.sha256()
    for rel in INPUTS:
        p = root / rel
        files = sorted(f for f in p.rglob("*") if f.is_file()) if p.is_dir() else [p]
        for f in files:
            h.update(f.relative_to(root).as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


if __name__ == "__main__":
    sys.stdout.write(agent_src_hash() + "\n")
