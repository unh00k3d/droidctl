# droidctl — instructions for agents working on this repo

An agent-first Android automation CLI: a compact UI snapshot with refs, element-based actions through our own accessibility-service APK, a resident daemon, a CLI and an MCP server. It is a sibling of chromectl (`~/Documents/chrome-debugging`), whose conventions and code we port.

## Read first
- `PLAN.md`: **the source of truth.** It holds the decisions, architecture, device API, snapshot/resolver/actions design, daemon, MCP, milestones, open questions and verification. Start with "Decisions at a glance" and "Milestones".
- `TESTAPP.md`: the edge-case test app spec (scenarios, expected behaviour, logcat ground truth).
- `research/`: prior-art reports (Artemis, droidrun/mobilerun, mobile-use, mobile-mcp, uiautomator2, android_world, Maestro, agent-device, …) with file:line references. Consult them before re-inventing a heuristic.

## Status
- Planning is complete; **no code yet.**
- Next up: **Milestone 1, the walking skeleton**: the agent APK with socket, `ping` and the peer-UID check; the chromectl core port; `setup`/`teardown`/`doctor`/`ping`; the round-trip measurement; `android/agent/PROTOCOL.md`.
- Work milestone by milestone. When a milestone is done, update PLAN.md with measured numbers and resolved open questions.

## Environment (set up 2026-09-27)
- JDK 21 (`javac`), Go, Python 3.13 (Debian system Python is externally managed, so **use a venv**: `python3 -m venv .venv`).
- Android SDK at `~/Android/Sdk` (`ANDROID_HOME`, set in `~/.zshrc`): `platforms;android-35`, `build-tools;35.0.0`, `platform-tools` (adb 37, first on PATH; Debian's `/usr/bin/adb` 34 also exists, so don't mix the two servers). The SDK's `sdkmanager` now prints a deprecation notice pointing at the new `android` CLI; it still works.
- Physical Android phone over USB (check with `adb devices`). Ask the user for the model and Android version if it isn't recorded here yet.

## Non-negotiables
- **Measure, don't assume.** Latency, settle defaults, the spatial-layer defaults and the Go-client question are all decided by measurements and benchmarks (see "Open questions and decision gates" in PLAN.md). Label estimates as estimates.
- **Fixtures come from real devices.** Never hand-write UI trees for tests; capture them with `dump-fixture` / `make fixtures`. Hand-written fixtures hid real bugs in Artemis and mobile-use.
- **Never break the user's phone.** Append to `enabled_accessibility_services`, never overwrite it. Never switch or leave behind an IME. Never uninstall other tools. `teardown` restores everything.
- **Never guess by position.** Refs resolve uniquely or fail with a typed error (`stale-ref`, `ambiguous`, `occluded`, `offscreen`). No coordinate taps unless explicit (`--point`) or through the event-gated single fallback.
- **The APK stays tiny:** Kotlin, no AndroidX or other dependencies, **no INTERNET permission**, auth by peer UID (shell = 2000).
- **Test ground truth comes from the test app's `DTA` logcat events,** never from droidctl's own output.
- **Licensing:** Artemis, mobile-use and mobile-mcp are Apache-2.0 (port with NOTICE attribution). uiautomator2 is MIT. **droidrun/mobilerun Portal is AGPL-3 and mobilecli's license is unclear: ideas only, never copy their code.** Our license is MIT.
- **Follow chromectl conventions:** `--json` on every command, one `ERROR_KINDS` registry, docs generated from the parser, and drift tests.
