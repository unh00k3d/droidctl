---
name: droidctl
description: Drive a real Android phone from the shell through its accessibility tree — read the screen as a compact list of elements with refs, tap/type/scroll by element (not coordinates), wait for UI conditions, launch apps, take screenshots, and batch steps. Use when a task needs an Android device or emulator: testing an app, reproducing a UI bug, automating a flow, or checking what a screen shows. Requires the `droidctl` CLI on PATH and a phone connected over adb.
---

# droidctl

One CLI over an Android phone's accessibility tree. Everything below is the whole
interface; you never need `--help` per command (`droidctl cheat --json` has it as data).

## The rules

1. **Add `--json` to every command.** Output becomes one JSON value on stdout.
   Failures become `{"ok": false, "error": {"kind": "...", "message": "...", "hint": "..."}}`
   on stdout too, with a non-zero exit. Branch on `error.kind`: <!-- ERROR-KINDS -->.
2. **Look, then act on refs.** `droidctl snapshot` prints one line per element:
   `[4] row "Ada Lovelace · Lunch?"  actions=[Archive, Delete]`. Act with the ref
   (`droidctl tap 4`). Refs are re-resolved against the live screen, never guessed by
   position: if the screen changed they fail with `stale-ref` (re-run `snapshot`),
   `ambiguous` (use a more specific locator), `occluded` or `offscreen` (`scroll-to` it).
   `connection` with `data.maybe_performed`: the action may already have run, so
   `snapshot` before repeating it.
3. **Copy labels verbatim** from the snapshot when you use `--text`/`--desc`; never
   invent them from a screenshot. Prefer refs over text.
4. **Every action reports what changed** (`changed`, `diff`, `new_screen`, `toast`), so
   you rarely need another snapshot right after it.
5. **Batch with `run`**: steps in one `run --json` share one process and one device session.

```bash
droidctl snapshot --json                    # the screen, with refs
droidctl tap 7 --json                        # or: tap --text "Checkout"
droidctl type 2 "Merhaba dünya" --enter --json
printf 'it'"'"'s $5\n' | droidctl type 2 --stdin --json   # anything shell-hostile: --stdin / --file
droidctl scroll-to --text "Privacy" --json
droidctl action 4 "Delete" --json            # custom accessibility actions (swipe-to-delete)
droidctl wait --text "Saved" --timeout 5 --json
droidctl launch com.android.settings --json
droidctl run --json --step 'launch com.android.settings' --step 'tap --text Display' --step 'snapshot'
```

## Tapping: what the result means

- `method:"action"` — the element's `ACTION_CLICK` ran (the normal case).
- A `warning` saying the click was performed but nothing happened: droidctl does **not**
  tap a second time, because the app may already have handled it (a second tap could
  submit twice). Check the screen; if nothing happened, retry with `--method gesture`.
- `method:"gesture-fallback"` — the element refused `ACTION_CLICK`, so it was tapped
  once at the centre of its visible area.
- `--point X,Y` taps device pixels: an explicit escape hatch, not the default.

## Setup, daemon, devices

- `droidctl setup` installs a tiny agent APK and **appends** its accessibility service
  (other services are kept); `droidctl teardown` removes only ours and restores the
  settings. Commands run setup automatically if needed (`--no-auto-setup` to refuse).
- The first call starts a background **daemon** (idle-exit 30 min) that keeps the phone
  connection warm and caches the screen; `--json` results say `"mode":"daemon"`. Events
  that happened between your calls (toasts, new windows) arrive on the next result under
  `between_calls`. `DROIDCTL_NO_DAEMON=1` (or `--no-daemon`) runs in-process instead.
- Several phones: `-d SERIAL` (or `ANDROID_SERIAL`).
- Banking and other secure apps: screenshots of `FLAG_SECURE` windows fail with
  `secure-window` (the snapshot still works); some apps refuse to run on rooted phones.

## All commands

Every device command also takes `--json`, `-d SERIAL` and `--no-auto-setup`. `LOCATOR` is
`--ref N | --id ID | --text TEXT | --desc DESC | --class CLASS | --role ROLE [--index I]`,
spatial `--right-of/--left-of/--above/--below/--near ANCHOR`, or `--point X,Y`.

<!-- COMMANDS -->
