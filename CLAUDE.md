# droidctl — instructions for agents working on this repo

An agent-first Android automation CLI: a compact UI snapshot with refs, element-based actions through our own accessibility-service APK, a resident daemon, a CLI and an MCP server. It is a sibling of chromectl (`~/Documents/chrome-debugging`), whose conventions and code we port.

## Read first
- `PLAN.md`: **the source of truth.** It holds the decisions, architecture, device API, snapshot/resolver/actions design, daemon, MCP, milestones, open questions and verification. Start with "Decisions at a glance" and "Milestones".
- `TESTAPP.md`: the edge-case test app spec (scenarios, expected behaviour, logcat ground truth).
- `research/`: prior-art reports (Artemis, droidrun/mobilerun, mobile-use, mobile-mcp, uiautomator2, android_world, Maestro, agent-device, …) with file:line references. Consult them before re-inventing a heuristic.

## Status
- **M1 (walking skeleton) done 2026-09-27**; numbers in PLAN.md. Next: M2 (raw tree, test app, fixtures).
- Build the APK with `make apk` (Gradle 9.8 wrapper, AGP 9.4.1; build-tools 36.0.0 was auto-installed by AGP). Python: `.venv/bin/pytest`; on-device e2e with `DROIDCTL_SERIAL=<serial>`.
- The Bash tool's shell does not source `~/.zshrc`: put `~/Android/Sdk/platform-tools` first on PATH yourself, or you get Debian's adb 34.
- Work milestone by milestone. When a milestone is done, update PLAN.md with measured numbers and resolved open questions.

## Environment (set up 2026-09-27)
- JDK 21 (`javac`), Go, Python 3.13 (Debian system Python is externally managed, so **use a venv**: `python3 -m venv .venv`).
- Android SDK at `~/Android/Sdk` (`ANDROID_HOME`, set in `~/.zshrc`): `platforms;android-35`, `build-tools;35.0.0`, `platform-tools` (adb 37, first on PATH; Debian's `/usr/bin/adb` 34 also exists, so don't mix the two servers). The SDK's `sdkmanager` now prints a deprecation notice pointing at the new `android` CLI; it still works.
- **Dev phone (recorded 2026-09-27):** Samsung Galaxy Note 8 **SM-N950F** (`greatlte`), serial `<serial>`, **Android 9 / API 28**, stock Samsung firmware `N950FXXSGDUG6` (user build, release-keys, verified boot green, SELinux enforcing), rooted with **Magisk**. adbd runs as shell (uid 2000). The screen is 1440x2960 physical but has a **resolution override of 1080x2220** (density 420). The keyboard is SamsungKeypad. No accessibility services were enabled before droidctl.
- **Connection:** this dev machine is a KVM VM with the phone passed through over USB. If `adb devices` is empty: the host's adb server or GNOME MTP (`gvfsd-mtp`) may have grabbed the phone, so stop them on the host, set the phone to "Charging only", and re-attach the USB device to the VM. Check with `lsusb` (expect `04e8:6860`) and `/sys/bus/usb/devices/*/bConfigurationValue` (must not be empty).
- **The dev phone is rooted.**
  - droidctl must **never depend on root**: no `su` in features or tests.
  - Don't use `adb root` for normal development; keep adbd as shell (uid 2000) so we exercise the real path. Peer-UID auth also accepts uid 0 for rooted/custom-ROM setups.
  - Results from the M1 on-device checks (restricted settings, peer UID, a11y behaviour) must be re-verified on a stock phone before v1.0.
  - Minimum Android 8 (API 26); `takeScreenshot` needs 11+, stable node `uid` needs 13+.
- **API 28 consequences (important):**
  - The dev phone exercises the *fallback* paths: no `takeScreenshot` (use host `screencap`), no `ACTION_IME_ENTER` (use `input keyevent 66`), no `stateDescription`, no `getUniqueId` (resolver tiers 2–4 only).
  - Android 13+ restricted settings and API 34 `accessibilityDataSensitive` **cannot be verified on it**.
  - The resolution override (1080x2220 over 1440x2960) is a real-world scaling edge case: a11y bounds, gesture coordinates, `screencap` pixels and `--marks` must all agree. Verify this explicitly.
  - **Second target:** an API 35 emulator. Nested KVM is available (`/dev/kvm`) and the SDK can install `emulator` + a `system-images;android-35;google_apis;x86_64` image. Use it for the API 30+ paths.

## Non-negotiables
- **Measure, don't assume.** Latency, settle defaults, the spatial-layer defaults and the Go-client question are all decided by measurements and benchmarks (see "Open questions and decision gates" in PLAN.md). Label estimates as estimates.
- **Fixtures come from real devices.** Never hand-write UI trees for tests; capture them with `dump-fixture` / `make fixtures`. Hand-written fixtures hid real bugs in Artemis and mobile-use.
- **Never break the user's phone.** Append to `enabled_accessibility_services`, never overwrite it. Never switch or leave behind an IME. Never uninstall other tools. `teardown` restores everything.
- **Never guess by position.** Refs resolve uniquely or fail with a typed error (`stale-ref`, `ambiguous`, `occluded`, `offscreen`). No coordinate taps unless explicit (`--point`) or through the event-gated single fallback.
- **The APK stays tiny:** Kotlin, no AndroidX or other dependencies, **no INTERNET permission**, auth by peer UID (shell 2000, or root 0).
- **Test ground truth comes from the test app's `DTA` logcat events,** never from droidctl's own output.
- **Licensing:** Artemis, mobile-use and mobile-mcp are Apache-2.0 (port with NOTICE attribution). uiautomator2 is MIT. **droidrun/mobilerun Portal is AGPL-3 and mobilecli's license is unclear: ideas only, never copy their code.** Our license is MIT.
- **Follow chromectl conventions:** `--json` on every command, one `ERROR_KINDS` registry, docs generated from the parser, and drift tests.
