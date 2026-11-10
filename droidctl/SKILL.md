---
name: droidctl
description: Drive a real Android phone or iPhone from the shell through its accessibility tree — read the screen as a compact list of elements with refs, tap/type/scroll by element (not coordinates), wait for UI conditions, launch apps, take screenshots, and batch steps. Use when a task needs an Android device, an emulator or an iPhone: testing an app, reproducing a UI bug, automating a flow, or checking what a screen shows. Requires the `droidctl` CLI on PATH and a phone connected over adb, or an iPhone over USB (a Mac with Xcode, or Linux/macOS with usbmuxd and go-ios, which setup offers to download).
---

# droidctl

One CLI over an Android phone's or an iPhone's accessibility tree. Everything below is the whole
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
   `snapshot` before repeating it. **On one screen a ref keeps its number** across
   snapshots and action results (after a scroll, a deleted row, a changed counter);
   new elements get new numbers, and a number whose element went away is never
   reused. On a new screen numbering starts again at 1.
3. **Refs for the screen you just saw, locators across changes.** A ref from the latest
   `snapshot` or action result is the precise choice on that screen. After a screen
   change, a scroll, or in a script written ahead of time, use a **locator** instead
   (`--text`/`--desc`/`--role`, spatial `--right-of "Total"`): it is resolved against the
   live screen every time. Don't grep ref numbers out of old output. **Copy labels
   verbatim** from the snapshot; never invent them from a screenshot. Many hardened apps
   strip resource ids (`id` is null), so `--id` may not exist there. If the target may be
   scrolled out of view, `tap --text X --scroll` scrolls it in, then taps. A locator that
   matches several elements is `ambiguous`, never a pick: the error lists each candidate
   with its region and position, so add a spatial locator or `--index N`.
4. **Every action waits for the screen to settle, then reports what changed** (`changed`,
   `diff`, `new_screen`, `toast`), so you rarely need another snapshot or a `sleep` after
   it. Settling is on by default (`--settle MS` to lengthen it, `--settle 0` to skip).
   Something slow outside an action (a load after login)? `wait --text X`, `wait --role
   progress --gone`, or `wait --idle` (until the screen stops changing).
5. **Batch with `run`**: steps in one `run --json` share one process and one device session.
6. **Timing for captures**: every `--json` result and error carries `t_start`/`t_end`
   (host clock, UTC ISO-8601) and `elapsed_ms`, to line actions up with a proxy history.
   `droidctl mark "LABEL"` writes a marker line to the phone's logcat (tag `droidctl-mark`);
   `droidctl logs --marks` reads them back (`--tag TAG` for any other tag).

```bash
droidctl snapshot --json                    # the screen, with refs
droidctl tap 7 --json                        # or: tap --text "Checkout"
droidctl type 2 "Merhaba dünya" --enter --json
printf 'it'"'"'s $5\n' | droidctl type 2 --stdin --json   # anything shell-hostile: --stdin / --file
droidctl scroll-to --text "Privacy" --json
droidctl action 4 "Delete" --json            # custom accessibility actions (swipe-to-delete)
droidctl wait --text "Saved" --timeout 5 --json
droidctl wait --role progress --gone --json      # after a submit: until the spinner is gone
droidctl tap --text "Privacy" --scroll --json   # scroll it into view (its own list/tab strip), then tap
droidctl mark "step 3: login" --json              # a logcat marker between capture steps
droidctl launch com.android.settings --json
droidctl run --json --step 'launch com.android.settings' --step 'tap --text Display' --step 'snapshot'
```

## Tapping: what the result means

- `method:"action"` — the element's `ACTION_CLICK` ran (the normal case).
- `handled:true` / "unchanged, but the app received the click" — the tap **worked**; the
  app just shows nothing for it on this screen. **Don't tap again** (not even with
  `--method gesture`): that clicks twice.
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
- `screen-off` (header `screen=off|locked`): the phone slept; node taps don't keep it
  awake. `droidctl press KEYCODE_WAKEUP`, then carry on.
- A phone set up with `setup --backend uiautomation` (Backend B) runs the agent over adb
  with nothing installed or enabled; everything else works the same (`doctor` shows the
  backend). Switching to B turns our own accessibility service **off** by default (so the
  apps below can be read); `--keep-a11y` keeps it on.
- **Blank or partial screen?** When `snapshot` shows a header but no elements, warns
  `no tree for app window ...`, or is `degraded=no-root`, run **`droidctl diagnose`**: it
  names the cause and the fix. The commonest is a **hardened app** (banking/finance) that
  hides its whole UI whenever an accessibility service is enabled — Backend A can't read
  it. Fix: **`droidctl diagnose --fix`** (switches to Backend B and disables our service),
  **then relaunch the app** (`stop-app` + `launch`): a window drawn while a service was on
  stays blocked until it is recreated. `diagnose` statuses: `hardened-a11y`,
  `hardened-ua-service-on` (B selected but our service still on), `hardened-other-service`,
  `suppressed-ua`, `latched-or-slow`, `degraded`, `opaque`, `transition`, `ok`.
- Banking and other secure apps: screenshots of `FLAG_SECURE` windows fail with
  `secure-window` (the snapshot still works); some apps refuse to run on rooted phones.

## iPhone (iOS 17+; a Mac with Xcode, or Linux / a Mac without Xcode)

The same commands, with `-d UDID` (the hardware UDID `droidctl devices` shows). `droidctl setup
-d UDID` is a guided checklist (Trust, Developer Mode, an Apple sign-in) that signs, installs
and starts droidctl's runner; keep the phone unlocked.
- **Without Xcode** (Linux, or `DROIDCTL_IOS_BACKEND=goios`): one command, `droidctl setup --ios`,
  run by the user in their terminal. It offers to install usbmuxd (with the user's sudo), offers to
  download a pinned go-ios, and on Linux asks which anisette server Apple's sign-in uses (Enter =
  SideStore's public one; or `--anisette URL`). iOS 17.4.1 or later. On `not-trusted` setup opens Settings
  at its main screen (not the Trust page itself): tell the user the path from the hint.
- `signing` with `data.reason`:
  - `anisette-rejected` (Apple's -80045, before the password is checked): Apple refuses that
    anisette server's data; setup already tried the other public one. Suggest another server
    (`--anisette URL`); don't let the user retry the same one over and over.
  - `cert-limit`: the Apple team has as many development certificates as Apple allows (each
    Mac, Linux host and sideloading tool takes one). droidctl never revokes one by itself: show
    the user `data.certificates` and let them choose. A free team's Xcode has no Revoke button;
    revoking breaks apps signed with that certificate (AltStore's, Xcode's).
- **Signing is automatic after one sign-in, which only the user can do:** the first `setup` asks
  for their Apple ID and password in a terminal (droidctl keeps Apple's token, never the
  password) and then renews the 7-day profile by itself. If a command fails with `signing` and
  the hint says `droidctl login` (or `data.reason` is `no-login`), ask the user to run
  `droidctl setup --ios` or `droidctl login` in their terminal; you can't type the password.
- **Points, not pixels:** the header's size (`390x844`), `--point`, `gesture --path` and boxes are
  in points. A `shot` image is pixels: scale by header width / image width.
- Icon buttons are often labelled with their image name (`button "arrowBack"`): use it as shown.
  Pop-ups, including system permission prompts, are `-- dialog`: tap a button by its text.
  Permission choices are the user's: ask unless told.
- **Some app pop-ups are invisible to accessibility**: the result says `unchanged` although
  something appeared. When a result doesn't match what should have happened, `shot` and look;
  dismiss such a pop-up with `tap --point X,Y` (in points).
- Actions wait for what they started, in one command: a spinner they brought up (≤8 s), a splash,
  a push sliding in, a screen appearing ≤1.2 s after the tap (`loading_ms`). "still loading"
  warning: `wait --role progress --gone --timeout 20`. Expect ~1–2 s per screen change.
- `tap`: the element's accessibility action, else **one** checked touch (`via: "touch"`);
  `handled: true` means the app got it: don't tap again.
- `type` types real keystrokes (deleting the old value first). A field that submits itself (a
  6-digit code box) closes as the last key goes in: `ok`, `verified: false`, a warning "the field
  left the screen": the keys were taken, **don't type again**. `hide-keyboard` presses Return/Done;
  a number pad has neither (`unsupported`): tap a label or another field.
- `back` taps the screen's own back button; iOS has no system Back, so without one, or with a
  dialog open, it is `unsupported` (the hint lists the dialog's buttons). `home` shows the Home
  screen, where app icons are buttons (`tap --text "App Name"`).
- `launch BUNDLE_ID`, `apps`, `install .app/.ipa`, `open-url`, `shot`, `logs`, `mark` work.
  Unsupported: `press` keys other than enter, `--toast`, `--activity`, `launch --clear`,
  `recents`, `notifications`, `quick-settings`, `expand`/`collapse`/`dismiss`.
- `no-device` after the cable moved: retry in a few seconds; the first command after a reconnect
  can take ~45 s. `launch`/`apps` timing out on "devicectl": the Mac's CoreDeviceService is stuck;
  open the app from its Home-screen icon and tell the user (restarting it is their call).
- Xcode can't reach the iPhone while droidctl reads its log (≤60 s after the last command):
  `droidctl daemon stop` releases it.

## All commands

Every device command also takes `--json`, `-d SERIAL` and `--no-auto-setup`. `LOCATOR` is
`--ref N | --id ID | --text TEXT | --desc DESC | --class CLASS | --role ROLE [--index I]`,
spatial `--right-of/--left-of/--above/--below/--near ANCHOR`, or `--point X,Y`.

<!-- COMMANDS -->
