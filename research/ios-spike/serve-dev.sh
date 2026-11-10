#!/bin/zsh
# Start the S2 server on the iPhone (xcodebuild keeps the test session) and wait until it pings over usbmux.
D=${0:a:h}; cd $D
export DEVELOPER_DIR=/Users/enesb/Downloads/Xcode.app/Contents/Developer
pgrep -f "[i]proxy 28100" >/dev/null || (iproxy 28100:18100 28101:18101 -u ${IOS_UDID:?set IOS_UDID to the hardware UDID} > iproxy.log 2>&1 &)
T0=$(date +%s)
TEST_RUNNER_S2_TOKEN=$(cat .token) nohup xcodebuild test-without-building -project S1.xcodeproj -scheme Runner -destination "platform=iOS,id=${IOS_UDID:?set IOS_UDID to the hardware UDID}" -derivedDataPath $D/dd-dev -only-testing:Runner/S2Server/testServe > $D/serve-dev.log 2>&1 &
for i in $(seq 1 240); do python3 s2.py 127.0.0.1:28100 ping >/dev/null 2>&1 && { echo "ready after $(( $(date +%s)-T0 ))s"; exit 0; }; grep -q -E "Unlock|\*\* TEST" serve-dev.log && break; sleep 1; done
grep -E "Unlock|error|TEST" serve-dev.log | head -5; exit 1
