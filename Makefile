# Build helpers. The Android build needs ANDROID_HOME (or android/local.properties).
ANDROID_HOME ?= $(HOME)/Android/Sdk
export ANDROID_HOME

APK_OUT := android/agent/build/outputs/apk/release/agent-release.apk
APK_ASSET := droidctl/assets/droidctl-agent.apk

.PHONY: apk clean

apk:
	cd android && ./gradlew -q :agent:assembleRelease
	cp $(APK_OUT) $(APK_ASSET)
	@ls -l $(APK_ASSET)

clean:
	cd android && ./gradlew -q clean
	rm -f $(APK_ASSET)
