# iOS spikes (2026-10-19 … 21): throwaway code, kept for reference

The results are in `../ios-decision.md` §8–§14, and the design that came out of them is in `../../IOS.md`. This is spike code: not product code, with no tests. The handoff is `../../IOS-HANDOFF.md`.

**Project:**
- `S1.xcodeproj` (scheme `Runner`; build settings `S1_TEAM`, `S1_BUNDLE_PREFIX`) builds two things:
  - `TestApp/App.swift`: the ground-truth app. Every input path logs a DTA event to os_log and `Documents/dta.log`.
  - `Runner/`: the XCUITest runner.
- `Runner/S2Server.m`: the NDJSON runner. It's the reference for every private call in `IOS.md` §7, and covers:
  - reading: tree, snapfast, hit, fetch, sig;
  - acting: act, fasttap, swipe, pinch, sendstring, setattr, dlaunch;
  - state and probes: lock guard, axreg/axevents, idle, watch, sweep, methods/protos.
  - It also has XCTest-waiting commands (`launch`, `activate`, `type`, `tap`), kept only to measure them. **Never port those.**
- `Runner/S1Tests.m`: the three AX-action routes.

**Scripts:**
- Client and measurement rounds:
  - `s2.py`: the client (`python3 s2.py HOST:PORT CMD k=v…`; the token is read from `.token` here or from `S2_TOKEN`).
  - `bench.py`, `exp2.py`…`exp6.py`: measurement rounds.
  - `soak.py`: long runs.
  - `cycles.py`: lock-guard cycles.
- Settle research:
  - `settle1.py`: simulator.
  - `settle2.py`, `settle3.py`: device; mode E (events + idle) and mode P (polling with diffs).
  - `settle_eval.py`: rule scoring.
  - `fling.py`: run-loop idle against the DTA scroll end.
- Open items of the handoff (`../ios-decision.md` §15): `exp7.py` (sections `names`, `focus`, `hang`, `long`, `web`; needs `.cdid`, the git-ignored CoreDevice ID), `exp8.py` + `settle_eval2.py` (the runner's `settle` command against polled truth), `exp9.py` (AX page scroll against DTA).
- Activation research: `sweep2.py`, the AX action-number sweep with liveness checks. It needs `IOS_CDID`.
- Third-party apps: their helper scripts and trees stay out of the repo (`qa/` is git-ignored). **Never commit them.**
- Mapping:
  - `runner2raw.py` (throwaway) and `ios_map.py` (the prototype for `droidctl/ios/mapping.py`, rules R1–R8) convert runner trees into droidctl raw trees.
  - `map_compare.py` renders both.
  - `ios2raw.py` converts WDA JSON.
- Other: `simax.m` (S5, the in-simulator AX helper; needs simulator automation mode, which must be restored afterwards).
- Start scripts:
  - `serve-dev.sh` (iPhone; needs `IOS_UDID`, the hardware UDID);
  - `sim-build-start.sh` (simulator, with the build-ID check);
  - `run-sim.sh` (S1 on the simulator).
- `results/`: raw data behind `ios-decision.md` §12 (settle and fling).

**Before running:**
- Create `.token` (git-ignored): `python3 -c "import secrets;print(secrets.token_hex(16))" > .token`.
- Build for a device with `-allowProvisioningUpdates DEVELOPMENT_TEAM=<team>`. The phone must be unlocked and must trust the developer.
- The simulator UDID in the `sim-*.sh`/`run-sim.sh` scripts is the iPhone 17 simulator used at the time; replace it with yours.
