#!/bin/zsh
# Build the runner with a fresh build ID, start it, and verify ping reports that ID; reinstall once on mismatch.
D=${0:a:h}; cd $D; U=6B7DC9FE-933D-4544-ADF8-12F3F2C1643A
export DEVELOPER_DIR=/Users/enesb/Downloads/Xcode.app/Contents/Developer
ID=${1:-$(python3 -c "import secrets;print(secrets.token_hex(6))")}
xcrun simctl boot $U 2>/dev/null; xcrun simctl bootstatus $U >/dev/null
xcodebuild build-for-testing -project S1.xcodeproj -scheme Runner -destination "platform=iOS Simulator,id=$U" -derivedDataPath $D/dd-sim CODE_SIGNING_ALLOWED=NO "GCC_PREPROCESSOR_DEFINITIONS=\$(inherited) S2_BUILD_ID=\\\"$ID\\\"" > $D/build-sim.log 2>&1 || { grep error: $D/build-sim.log; exit 1; }
start() {
  TEST_RUNNER_S2_TOKEN=$(cat .token) nohup xcodebuild test-without-building -project S1.xcodeproj -scheme Runner -destination "platform=iOS Simulator,id=$U" -derivedDataPath $D/dd-sim -only-testing:Runner/S2Server/testServe > $D/serve-sim.log 2>&1 &
  for i in $(seq 1 180); do python3 s2.py 127.0.0.1:18100 ping >/dev/null 2>&1 && break; sleep 1; done
  python3 s2.py 127.0.0.1:18100 ping | python3 -c "import json,sys;print(json.load(sys.stdin).get('build'))"
}
GOT=$(start); echo "expected=$ID got=$GOT"
if [ "$GOT" != "$ID" ]; then
  echo "stale runner: reinstalling"; python3 s2.py 127.0.0.1:18100 quit >/dev/null; sleep 3
  xcrun simctl uninstall $U dev.droidctl.s1.runner.xctrunner; GOT=$(start); echo "after reinstall got=$GOT"
fi
