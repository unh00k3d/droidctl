# Build helpers. The Android build needs ANDROID_HOME (or android/local.properties).
ANDROID_HOME ?= $(HOME)/Android/Sdk
export ANDROID_HOME

PYTHON ?= .venv/bin/python
APK_OUT := android/agent/build/outputs/apk/release/agent-release.apk
APK_ASSET := droidctl/assets/droidctl-agent.apk

.PHONY: apk clean fixtures dist

# The APK is committed (pipx/pip installs from git get it without an Android SDK);
# the source hash next to it lets tests/test_packaging.py catch a stale APK.
apk:
	cd android && ./gradlew -q :agent:assembleRelease
	cp $(APK_OUT) $(APK_ASSET)
	$(PYTHON) scripts/agent_src_hash.py > $(APK_ASSET:.apk=.src.sha256)
	@ls -l $(APK_ASSET)

clean:
	cd android && ./gradlew -q clean
	rm -f $(APK_ASSET)

# Re-capture the test-app scenario fixtures (every screen scenario in scenarios.json, plus
# keyboard and after-action variants) from a real device; needs the test app installed and
# `droidctl setup` done.
fixtures:
	$(PYTHON) scripts/capture_fixtures.py
	$(PYTHON) scripts/make_goldens.py
	@echo 'review: git diff tests/fixtures/snap'

# Wheel + sdist with the agent APK inside. Fails if the APK is missing from the wheel.
dist: apk
	rm -rf dist
	$(PYTHON) -m build --outdir dist .
	$(PYTHON) -c "import zipfile,glob,sys; n=zipfile.ZipFile(glob.glob('dist/*.whl')[0]).namelist(); sys.exit(0 if {'droidctl/assets/droidctl-agent.apk','droidctl/SKILL.md'} <= set(n) else 'APK or SKILL.md missing from the wheel')"
	@ls -l dist
