#!/bin/zsh
# Build + run S1 on the simulator, capturing the test app's DTA log as ground truth.
D=${0:a:h}; U=6B7DC9FE-933D-4544-ADF8-12F3F2C1643A
export DEVELOPER_DIR=/Users/enesb/Downloads/Xcode.app/Contents/Developer
cd $D
xcodebuild build-for-testing -project S1.xcodeproj -scheme Runner -destination "platform=iOS Simulator,id=$U" -derivedDataPath $D/dd-sim CODE_SIGNING_ALLOWED=NO > $D/build-sim.log 2>&1 || { grep error: $D/build-sim.log; exit 1; }
xcrun simctl boot $U 2>/dev/null; xcrun simctl bootstatus $U >/dev/null
xcrun simctl spawn $U log stream --style compact --predicate 'subsystem == "dev.droidctl.testapp"' > $D/sim-dta.log 2>&1 &
LOGPID=$!; sleep 2
xcodebuild test-without-building -project S1.xcodeproj -scheme Runner -destination "platform=iOS Simulator,id=$U" -derivedDataPath $D/dd-sim > $D/test-sim.log 2>&1
echo "xcodebuild exit=$?"; sleep 1; kill $LOGPID
grep " S1 " $D/test-sim.log | cut -c12-400
grep -E "error:|Executed" $D/test-sim.log | head -3
echo ---- DTA; grep -o "[0-9:.]* .*DTA .*" $D/sim-dta.log | sed -E 's/^([0-9:.]+) .*(DTA .*)/\1 \2/'
