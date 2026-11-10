# droidctl — agent reference

A single CLI that drives a real Android phone or iPhone through its accessibility tree
(iPhone specifics: the "iPhone" section). **This file is
the complete interface — you do not need to run `--help` per command.** Re-fetch the
surface as data anytime: `droidctl cheat --json`. (Working *on* droidctl itself? Read
`CLAUDE.md` and `PLAN.md` instead.)

## The rules

1. **Add `--json` to every command.** One JSON value on stdout, always; failures are
   `{"ok": false, "error": {"kind", "message", "hint"?, "data"?}}` with exit code 1.
   Branch on `error.kind` (table at the end), never on the message.
2. **Look, then act on refs.** `snapshot` gives each element a `[ref]`; actions take the ref.
   Refs are locators, re-resolved against the live screen — never coordinates, never guessed
   by position. When a ref no longer resolves you get a typed error instead of a wrong tap.
   A `connection` error with `data.maybe_performed` means the action may already have run
   (Appium/`uiautomator` briefly unbinds the agent): `snapshot` before repeating it.
3. **Copy labels verbatim** from the snapshot for `--text`/`--desc`. Don't invent them from a
   screenshot. Prefer refs.
4. **Batch with `run`**: a multi-step flow in one `run --json` is one process and one device
   session.

## Setup and devices

```bash
droidctl devices                 # attached phones and whether droidctl is set up
droidctl setup                   # install the agent APK, append its accessibility service
droidctl doctor                  # adb, APK, service, socket, peer UID, round-trip latency
droidctl teardown                # remove only our service, restore settings, uninstall
```

- Target a phone with `-d SERIAL` (else `ANDROID_SERIAL`, else the only attached one).
- Commands run `setup` automatically when the agent is missing; `--no-auto-setup` makes them
  fail with `not-installed` instead.
- `setup` **appends** to `enabled_accessibility_services` (TalkBack, password managers etc.
  stay on); `teardown` removes only our entry and puts the original values back. droidctl
  never switches the keyboard (IME) and never touches other apps' settings.
- If Appium/uiautomator2 is attached, Android suppresses accessibility services:
  `doctor` reports it and commands fail with `suppressed`.
- **Screen off or locked:** accessibility actions don't count as user activity, so a long
  session can let the screen time out. The snapshot header then says `screen=off` or
  `screen=locked`, and actions fail with `screen-off` (wake it: `droidctl press KEYCODE_WAKEUP`).

**Two backends.** By default droidctl runs as an accessibility service (`setup` installs
and enables it). `droidctl setup --backend uiautomation` runs the same agent over adb
instead: nothing is installed or enabled, the agent is pushed to `/data/local/tmp` and
started with `app_process`, holding a UiAutomation. Use it where an accessibility service
can't be enabled, or for apps that hide their UI while one is on. The choice is remembered
per phone (`DROIDCTL_BACKEND` overrides it per call) and never switched silently. It stops
after 10 min idle, on reboot or on `teardown`, and the next command starts it again. Only
one UiAutomation client can run at a time: with uiautomator/Appium attached it fails with
`suppressed`. Screenshots use `screencap`. `doctor` shows which backend answers.

## Look: `snapshot`

```
screen dev.droidctl.testapp/.Main  sig=9b86  keyboard=hidden  dialog=no  1080x2220
-- top bar
[1] button "Back"   [2] heading "Cart (4)"   [3] button share? (unlabeled, right of "Cart (4)") #ic_share
-- content
[4] text "Wireless Mouse"   [5] button "−"   [6] text "1"   [7] button "+"
-- bottom bar
[19] text "Total $84.96"   [20] button "Checkout"
```

- Only actionable or labelled elements are listed; a row's passive texts merge into it
  (`row "Connections · Wi-Fi, Bluetooth"`). Elements whose vertical spans overlap share a line,
  left to right; `--` lines are regions (top bar, content, bottom bar, dialog, sheet, keyboard…).
- Annotations show only non-default state: `hint=`, `error=`, `empty`, `password`, `checked`,
  `off`, `disabled`, `focused`, `selected`, `heading`, `range=3/10`, `actions=[Archive, Delete]`
  (custom actions), `list 8/25 more↓` (scrollable, with position), `covered` (behind the
  keyboard or another window), `#id`. `share?` marks a label *inferred* from a resource id.
- The header: package/activity, a screen signature `sig=` (changes when the screen's
  structure does), `keyboard=`, `dialog=`, the screen size, and `toast="…"` for a toast
  that fired since your last snapshot or action (also `toast` in `--json`). Action results
  carry their own `toast`, and the daemon reports events between calls (`between_calls`).
- Options: `--diff` (only what changed since the last snapshot; `unchanged` if nothing),
  `--find TEXT`, `--in REF` (one subtree), `--bounds`, `--geo` (`@x,y wxh` as screen %),
  `--map` (ASCII wireframe), `--layout flat` (one element per line), `--system` (include the
  status/navigation bars), `--max N`, `--raw` (the full raw tree as JSON).
- `where REF`: one element's box in device px, its region and its neighbours.
- `shot [PATH] [--marks] [--crop REF] [--scale 0.5] [--out F]`: a JPEG screenshot, with ref boxes drawn
  by `--marks`. Use it for pixels only (colours, images, visual bugs); prefer `snapshot`.
  `FLAG_SECURE` windows (banking apps, password screens) fail with `secure-window`.
- `current`: the foreground package/activity and whether the keyboard is shown.

## Act

Every element action takes a positional `REF` or a **locator**: `--ref N`, `--id ID`,
`--text TEXT` (text, hint or desc, case-insensitive), `--desc`, `--class`, `--role`
(+ `--index I`), or spatially `--right-of/--left-of/--above/--below/--near ANCHOR`
(`tap --text "+" --right-of "Wireless Mouse"`). A locator that matches more than one element
fails with `ambiguous` (the candidates, each with its region and position, are in the message
and in `error.data`); it never picks one for you. A locator that matches nothing fails with
`not-found` naming the screen it looked at; `tap/long-press --scroll` scrolls a locator's target
into view first. Across a screen change or a scroll, prefer a locator to an old ref number:
it is resolved against the live screen. `--point X,Y` (device pixels) is the explicit
coordinate escape hatch.

```bash
droidctl tap 7                                   # or tap --text "Checkout"
droidctl tap 7 --double | long-press 7
droidctl type 2 "Merhaba dünya 😀" --enter        # --append, --clear
droidctl type 2 --stdin < message.txt            # quotes/$/newlines: --stdin or --file F
droidctl scroll down                             # the main list; scroll up 4 for a specific one
droidctl scroll-to --text "Privacy"              # scroll its own list (or tab strip) until it is on screen
droidctl tap --text "Privacy" --scroll           # the same, then tap it
droidctl swipe left 4                            # finger direction; the screen if no element
droidctl action 4 "Delete"                       # a custom accessibility action
droidctl set 9 7                                 # a slider/range value
droidctl focus 2 | expand 5 | collapse 5 | dismiss 3
droidctl back | home | recents | notifications | quick-settings
droidctl press enter                             # adb keyevent: enter, tab, del, search, KEYCODE_*
droidctl gesture --path '540,1800 540,600' --ms 400   # raw finger path (escape hatch)
```

**Every action verifies itself.** The result has `changed`, `new_screen`, `diff` (the changed
snapshot lines, ≤80), `toast`, `events`, `method`, the new `screen` header, and `text` (the diff,
or the whole new screen when `new_screen`), so you can usually act again without another
`snapshot`. **Refs are stable on one screen:** an element keeps its number across snapshots
and results (a scroll, a deleted or inserted row, a changed counter); new elements get
numbers not used before on that screen, so numbers can be out of order after a scroll; a
number whose element went away is never reused and fails as `offscreen`/`stale-ref` (or acts
again if the element came back). A different screen starts again at 1. `--expect-change` turns "nothing
changed" into the `no-change` error; `--settle MS` sets the quiet window (default 150 ms,
`0` = don't wait). `scroll` on a screen where nothing scrolls fails (`not-found`, reason
`no-scrollable`) rather than swiping blindly; use `swipe` for content that moves by touch only.
`wait --idle` blocks until the screen stops changing, for slow work outside an action.
Every `--json` result and error has `t_start`/`t_end` (host UTC, ISO-8601) and `elapsed_ms`;
`mark LABEL` writes a line to the phone's logcat (tag `droidctl-mark`) to cut captures into steps;
`logs --marks` (or `--tag TAG`) reads them back.

**How `tap` decides** (`--method auto`, the default):
- `ACTION_CLICK` on the element (or its clickable ancestor) → `method:"action"`.
- If the element refuses the click, one gesture tap at the centre of its visible area →
  `method:"gesture-fallback"` with a `warning`.
- If the app received the click (a click event) but nothing on screen changed, the result
  says so (`handled:true`, text "unchanged, but the app received the click"). The tap
  **worked**; don't repeat it, not even with `--method gesture` (that clicks twice).
- If the click was performed but produced no click event and no visible change, droidctl
  does **not** tap again — the app may already have handled it, and a second tap could submit
  twice. You get `method:"action"` plus a `warning`; if nothing happened, retry with
  `--method gesture`.
- `--method action|gesture` forces one path.

**Typing** tries `ACTION_SET_TEXT` (Unicode, no tap), then a clipboard paste (the clipboard is
restored, or cleared if it was empty), then `adb input text` (ASCII only), and reads the value
back (`value` in the result). Password fields never go through the clipboard, and their value is
never printed: `verified` compares the length only. A field counts as a password when the app says
so *or* its input type is a password variation (some apps mask on their own and expose the clear
text); snapshots show such values as bullets. A formatter's punctuation is accepted
(`5551234567` shown as `(555) 123 45 67` is verified). `--enter` presses the keyboard's action key.
Digits after a locator are text: `type --id otp1 123456`.

**Swipes** drag 0.4 of the box up/down and 0.7 left/right: pages and swipe-to-dismiss rows need
more than half the width, because accessibility gestures don't register as flings.

## iPhone (iOS 17+)

The same commands drive an iPhone over USB: from a Mac with Xcode, or without Xcode (Linux, or a Mac
without it) through usbmuxd and go-ios. Everything above applies;
this section is what differs.

**Setup and targeting**
- `droidctl devices` lists an iPhone by its hardware UDID (`00008101-…`, `backend ios`); pass it
  as `-d UDID`. `droidctl setup -d UDID` is a guided checklist: Trust this computer, Developer
  Mode, an **Apple sign-in**, then it signs, installs and starts droidctl's runner (an XCUITest).
  Keep the phone **unlocked**. It says what to do on the phone, waits, and carries on by itself.
- **The Apple sign-in is the user's, in a terminal.** The first `setup` asks for their Apple ID and
  its password (a free Apple ID works; 2FA only if Apple asks; on a Mac already signed in to that
  Apple ID, usually none). droidctl keeps Apple's token in the keychain, never the password, and
  from then on gets and renews the signing profile itself. An agent can't type the password:
  when `setup` (with `--json`, or over MCP) fails with `signing` and `data.reason` `no-login`, ask
  the user to run `droidctl setup --ios` (or `droidctl login`) in their terminal.
- A free Apple team's profile lasts 7 days; droidctl renews it by itself before it runs out (a
  few seconds, no prompt). If Apple stops accepting the saved sign-in, commands fail with
  `signing` and the hint `droidctl login`: ask the user to run it. `signing` / `not-trusted` /
  `profile-expired` errors carry the fix in `hint`.
- **Without Xcode** (Linux, or `DROIDCTL_IOS_BACKEND=goios` on a Mac): setup checks usbmuxd (on
  Linux it offers to install the `usbmuxd` package with the user's own sudo, showing the commands
  first; the only root step, and the user's), offers to download a pinned go-ios
  (MIT, checksum-verified, `~/.droidctl/bin`; `DROIDCTL_GO_IOS` points at your own), starts
  droidctl's own go-ios tunnel agent (userspace, no root), mounts the developer disk image (the
  first time Apple personalizes it: internet needed; `DROIDCTL_IOS_DDI` = a copy of a Mac's
  `iOS_DDI` folder) and installs the runner itself. Apple's sign-in on Linux needs an anisette
  server: setup offers SideStore's public one (Enter) or takes the user's own (`--anisette URL`,
  `DROIDCTL_ANISETTE`); it never sees the Apple ID or password. Apple refusing a server's data
  is `signing` with `data.reason` `anisette-rejected` (-80045): setup then tries the other public server. droidctl creates its own
  development certificate when no usable one exists and never revokes one (`signing`,
  `data.reason` `cert-limit` lists them). iOS 17.4.1 or later. On `not-trusted` setup opens
  Settings at its main screen there (go-ios can't open a page): relay the hint's Settings path.
- `doctor -d UDID` checks Xcode (or go-ios), Developer Mode, the team, the runner and the profile's expiry.

**Reading the screen**
- Sizes and coordinates are in **points** (the header says e.g. `390x844`), not pixels: `--point`,
  `gesture --path`, `where` and `--bounds` all use points. A `shot` image is pixels; to turn an image
  position into points, scale by header width / image width (`shot` prints the image size).
- A button's title is its label (`button "Login"`); `--text` matches it. Apps often name icon
  buttons after their image (`button "arrowBack"`, `"searchNewIcon"`, `"info20"`): those labels
  are real, use them as shown.
- Pop-ups are `-- dialog` regions: UIKit alerts, an app's own floating cards, and system prompts
  (location, notifications, microphone) over the app. Tap the button you want by its text
  (`tap --text "Allow Once"`). Permission prompts are the user's decision: ask them unless told.
- **Some apps draw pop-ups that accessibility can't see.** The snapshot then says `unchanged` (or
  shows the old screen) although something appeared. If an action's result doesn't match what
  should have happened (a login that "did nothing"), take a `shot` and look. To dismiss such a
  pop-up, `tap --point X,Y` on its button, converting the image position to points as above.

**Acting**
- `tap` runs the element's accessibility action; standard controls refuse it, so droidctl then
  touches it **once**, at a point it has checked shows that element (`via: "touch"`; `occluded`
  when something else is there). `handled: true` means the app got it: don't tap again.
- Every action waits for what it started, in the same command: a spinner it brought up (up to 8 s),
  a splash (3 s), a push still sliding in, or a screen that appears up to 1.2 s after the tap.
  `loading_ms` in the result says how long. A `warning` "still loading after 8 s" means a slow
  server: `wait --role progress --gone --timeout 20`, then carry on. A real screen change takes
  about 1–2 s per action on network-backed apps; `snapshot` takes 70–350 ms.
- `back` taps the screen's own back button (the navigation bar's, or an app's `arrowBack`-style
  button). iOS has no system Back: on a screen without one, or with a dialog open, `back` fails
  with `unsupported` (the hint lists the dialog's buttons; tap one).
- `home` goes to the Home screen; app icons there are buttons (`tap --text "App Name"`).

**Typing**
- `type` types real keystrokes into the field (first deleting what it held), so the app's own
  input handling and formatters run; it falls back to setting the value. The keyboard stays up.
- Passwords: the value is never printed; `verified` compares the length only.
- **A field that submits itself** (a 6-digit PIN/OTP/password box) closes as the last key goes in.
  The result is then `ok` with `verified: false` and a `warning` "the field left the screen once
  the keys were in": the keys were taken. **Don't type again**; read the new screen in `text`.
- `hide-keyboard` presses the keyboard's Return/Done key. A number pad has neither: it fails with
  `unsupported`; tap a label or another field to end editing. `press enter` types a newline.

**Apps, logs, screenshots**
- `launch BUNDLE_ID` (e.g. `com.apple.Preferences`), `stop-app`, `apps`, `install` (`.app`/`.ipa`),
  `open-url`, `shot` (with `--marks`), `mark` and `logs` work. An iPhone's log is a live stream: the
  daemon keeps the lines from while droidctl is in use; without the daemon `logs` listens for 2 s.
- **Xcode can't connect to the iPhone while droidctl reads its log** (up to 60 s after the last
  droidctl command); `droidctl daemon stop` releases it at once.
- `unsupported` on iOS: `press` keys other than enter, `--toast`, `--activity`, `launch --clear`,
  `install --grant`, `recents`, `notifications`, `quick-settings`, `expand`/`collapse`/`dismiss`.

**When things go wrong**
- `no-device` right after the cable moved: the phone dropped off USB. Retry in a few seconds
  (`droidctl devices`). The first command after a reconnect can take ~45 s while the runner restarts.
- `launch`/`apps`/`install` failing with `timeout` "devicectl … did not finish": Apple's
  CoreDeviceService on the Mac is stuck (check: it sits near 100% CPU). Taps, typing and snapshots
  still work, so open the app from the Home screen instead (`home`, then `tap --text "App Name"`),
  and tell the user; restarting that service is their call.
- The app shows its login again after a while: the app's own session expired, not droidctl.
- A command you didn't expect to change the screen did, or the screen is not what you left: the
  user may be using the phone. Check with `snapshot` before acting.

## Wait and observe

```bash
droidctl wait --text "Saved" --timeout 5         # --id, --desc, --gone, --activity, --toast, --window
droidctl wait --role progress --gone --timeout 20 # a bare loading spinner (no text/id) is over
droidctl watch --max 20 --timeout 10             # pushed events: clicks, toasts, windows, IME
droidctl logs --max 50 --pkg com.example --level W
```

Waits block on the phone and return the moment the condition holds (no polling).

## Apps

```bash
droidctl launch com.android.settings             # starts it, waits for its window, prints the screen
droidctl launch com.example --stop               # force-stop first; --clear wipes its data (destructive)
droidctl stop-app com.example | apps --filter bank | install app.apk | open-url https://example.com
```

## Prefer `run` for multi-step tasks

```bash
droidctl run --json \
  --step 'launch com.android.settings' \
  --step 'tap --text Display' \
  --step 'snapshot'
droidctl run --json flow.txt                     # one step per line (# comments); or stdin
```

Steps stop at the first failure unless `--keep-going`. With `--json` you get
`{"ok", "steps", "ran", "failed", "results": [{"step", "cmd", "ok", "result"|"error"}]}`.

## Daemon (warm connection, cached screen)

- The first droidctl call starts a **background daemon** (you'll see one line on stderr; in
  `--json` mode the result carries `"daemon": {"started": true, "pid": N}` instead). Later
  calls reuse its warm phone connection and screen cache, so a repeated `snapshot` with no
  change on screen costs ~40 ms from the shell instead of a new tree dump.
- Every `--json` result says where it ran: `"mode": "daemon"` or `"inprocess"`.
- Events that happened **between** your calls (toasts, new windows, crash dialogs) come back
  once, on the next result, under `between_calls`.
- It exits after 30 min idle (`DROIDCTL_IDLE`), on `droidctl daemon stop`, or when a different
  droidctl version connects. `droidctl daemon status|logs` shows it.
- Opt out: `--no-daemon` or `DROIDCTL_NO_DAEMON=1` runs in-process (slower, no cache or event
  history); `DROIDCTL_AUTOSTART=0` fails with `no-daemon` instead of starting one. In a sandbox
  that forbids it, droidctl falls back to in-process automatically.

## MCP

`droidctl mcp` is a real MCP server (stdio) over the same daemon, with a small tool set
(`snapshot`, `tap`, `type`, `scroll`, `swipe`, `action`, `key`, `wait`, `launch`, `shot`, `logs`,
`devices`). `droidctl mcp --install claude|codex|cursor|all` adds it to a client's config.
For shell-capable agents the CLI + `droidctl skill install` is the leaner default.

## Teaching another agent this CLI

`droidctl skill install` writes `~/.claude/skills/droidctl/SKILL.md` (a short version of this
file with the same generated tables); `droidctl skill print` prints it.

## Error kinds

<!-- generated from droidctl.core.ERROR_KINDS; a test checks it -->
| kind | meaning |
|---|---|
| `error` | unclassified failure (the default) |
| `bad-args` | the arguments contradict each other or are missing |
| `not-found` | the thing named does not exist (a file, a binary, an element) |
| `timeout` | gave up waiting |
| `missing-dep` | an external tool this command needs is not installed |
| `connection` | the device agent's socket is unreachable or was closed (forward lost, service not running, or unbound by Appium/uiautomator); with data.maybe_performed the action may already have run: snapshot before repeating it |
| `no-device` | no device attached, or none matches -d / ANDROID_SERIAL |
| `adb` | an adb command failed (its own message is passed through) |
| `not-installed` | the droidctl agent is not installed or its service is not enabled (run: droidctl setup) |
| `device` | the device agent rejected the request (its own message is passed through) |
| `suppressed` | another UiAutomation client (Appium/uiautomator2) is suppressing accessibility services, or (uiautomation backend) holds the one UiAutomation |
| `screen-off` | the screen is off or locked |
| `secure-window` | the window is FLAG_SECURE, so it cannot be captured |
| `signing` | the iPhone runner can't be signed or provisioned (no Apple sign-in, an expired one, no team, a free team's 3-app or certificate limit) |
| `not-trusted` | the iPhone doesn't trust the developer certificate yet (Settings > General > VPN & Device Management) |
| `profile-expired` | the iPhone runner's provisioning profile has expired and no new one could be issued (run: droidctl login) |
| `stale-ref` | the ref no longer resolves (gone, shifted or occupied); re-run snapshot |
| `ambiguous` | the locator matched more than one element |
| `occluded` | the element is covered (by another window or the keyboard) |
| `offscreen` | the element is outside the visible area; scroll to it first |
| `disabled` | the element is disabled |
| `not-focused` | no input field has the keyboard focus, so typed keys would go nowhere (tap the field first) |
| `no-change` | the action had no visible effect (with --expect-change) |
| `unsupported` | this device (API level) or element does not support the operation |
| `no-daemon` | the daemon is not running and auto-start is disabled |

## All commands

Every device command also takes `--json`, `-d SERIAL` and `--no-auto-setup`. `LOCATOR` is
`--ref N | --id ID | --text TEXT | --desc DESC | --class CLASS | --role ROLE [--index I]`,
spatial `--right-of/--left-of/--above/--below/--near ANCHOR`, or `--point X,Y`.
Regenerate with: `python -c 'from droidctl.cli import _command_table; print(_command_table())'`

| command | usage | what |
|---|---|---|
| `version` | – | print the droidctl version |
| `cheat` | – | every command and its options, on one screen |
| `skill` | `<{print,install}> --dir DIR --force` | print the agent skill (SKILL.md), or install it for Claude Code |
| `devices` | – | list attached devices and whether droidctl is set up |
| `setup` | `--ios --team ID --anisette URL --reinstall --backend {a11y,uiautomation} --keep-a11y` | install the agent APK and append its accessibility service (keeps the others); on an iPhone: a guided setup (Trust, Developer Mode, an Apple sign-in) that signs, installs and starts the runner |
| `login` | `--apple-id EMAIL --team ID --anisette URL --fresh` | (iPhone) sign in to Apple again (setup asks the first time), so droidctl signs and renews the runner itself; asks for the password, and a 2FA code if Apple wants one, in the terminal |
| `logout` | – | (iPhone) forget droidctl's Apple sign-in (the keychain entry) |
| `teardown` | `--keep-apk --backend {a11y,uiautomation,all}` | remove only our service, restore the a11y settings, uninstall the agent (iPhone: stop and uninstall the runner, and list what it does not undo) |
| `doctor` | – | check adb, APK, service, socket, peer UID and round-trip latency (iPhone: Xcode, Developer Mode, team, runner, profile expiry, the Mac's sleep) |
| `diagnose` | `--fix` | explain why the current screen has no readable UI, and what to do (e.g. apps that hide from accessibility services) |
| `ping` | `--count N` | round trip to the on-device agent |
| `dump-fixture` | `<name> --pkg PKG --not-important --dir DIR --timeout S --allow-degraded` | save the current screen's raw tree as a test fixture (dev) |
| `snapshot (snap)` | `--diff --find TEXT --in REF --raw --bounds --layout {spatial,flat} --no-regions --no-rows --no-grids --no-infer --geo --map --system --max N --full --fixture PATH` | the screen as a compact list of elements with refs |
| `where` | `<ref>` | an element's box, region and neighbours (from the last snapshot) |
| `tap` | `[REF] LOCATOR --scroll --double --method {auto,action,gesture} --settle MS --expect-change` | tap an element: ACTION_CLICK (iOS: its AX action, else one checked touch), verified by a diff |
| `long-press` | `[REF] LOCATOR --scroll --method {auto,action,gesture} --settle MS --expect-change` | long-press an element (ACTION_LONG_CLICK, else a long gesture) |
| `type` | `[REF] [TEXT] LOCATOR --append --clear --enter --stdin --file F --settle MS --expect-change` | set an input's text (Android: set_text, then paste, then adb input; iOS: keystrokes, then set_value) |
| `scroll` | `<{up,down,left,right}> [REF] LOCATOR --settle MS --expect-change` | scroll an element (default: the main list) up/down/left/right |
| `scroll-to` | `LOCATOR --direction {up,down,left,right} --one-way --max-scrolls N --settle MS --expect-change` | scroll until an element matching --text/--id/--desc is on screen |
| `swipe` | `<{up,down,left,right}> [REF] LOCATOR --distance F --ms MS --settle MS --expect-change` | swipe the screen or an element; DIR is the finger's direction |
| `action` | `[REF] <label> LOCATOR --settle MS --expect-change` | run an element's accessibility action by label (custom actions like Delete) |
| `set` | `[REF] <value> LOCATOR --settle MS --expect-change` | set a slider/range value (or an input's text) |
| `focus` | `[REF] LOCATOR --settle MS --expect-change` | give an element input focus |
| `expand` | `[REF] LOCATOR --settle MS --expect-change` | expand an element (ACTION_EXPAND) |
| `collapse` | `[REF] LOCATOR --settle MS --expect-change` | collapse an element (ACTION_COLLAPSE) |
| `dismiss` | `[REF] LOCATOR --settle MS --expect-change` | dismiss an element (ACTION_DISMISS) |
| `gesture` | `--path 'X,Y X,Y …' --ms MS --settle MS --expect-change` | a raw finger path in device px (escape hatch) |
| `back` | `--settle MS --expect-change` | the system Back (iOS: the screen's own back button) |
| `home` | `--settle MS --expect-change` | go to the home screen |
| `recents` | `--settle MS --expect-change` | open the recent apps |
| `notifications` | `--settle MS --expect-change` | open the notification shade |
| `quick-settings` | `--settle MS --expect-change` | open quick settings |
| `hide-keyboard` | `--settle MS --expect-change` | hide the on-screen keyboard (iOS: its Return/Done key; Android: Back) |
| `press` | `<key> --settle MS --expect-change` | press a key via adb (enter, tab, del, search, KEYCODE_*, or a number; iOS: enter only) |
| `wait` | `--stable --quiet MS --text TEXT --id ID --desc DESC --role ROLE --gone --exact --activity CLASS --toast TEXT --window TITLE\|PKG --pkg PKG --timeout S` | block on the device until a condition holds, or --idle (event-driven) |
| `mark` | `<label>` | write a marker line to the device's logcat (tag droidctl-mark), to cut captures into steps |
| `current` | – | the foreground app/activity and whether the keyboard is shown |
| `watch` | `--max N --timeout S --events TYPES --all` | pushed device events (clicks, toasts, windows, IME), bounded |
| `logs` | `--max N --pkg PKG --level {V,D,I,W,E,F} --tag TAG --marks` | recent logcat lines, bounded (iOS: the phone's log stream) |
| `shot` | `[PATH] --scale F --full --quality Q --marks --crop REF --out F --base64` | a screenshot (downscaled JPEG), optionally with ref marks |
| `launch` | `<pkg> --activity CLASS --stop --clear --timeout S --settle MS --expect-change` | start an app, wait for its window and settle; prints the new screen |
| `stop-app` | `<pkg>` | force-stop an app |
| `apps` | `--all --filter TEXT` | list installed apps (third-party by default) |
| `install` | `<apk> --grant` | install an APK (adb install -r; iOS: a .app or .ipa) |
| `open-url` | `<url> --settle MS --expect-change` | open a URL or deep link (ACTION_VIEW) and settle |
| `run` | `[file] --step CMD --keep-going` | run many steps in one process and one device session |
| `daemon` | `<{start,stop,restart,status,logs}> --foreground --lines N` | the background daemon: start, stop, restart, status, logs |
| `serve` | `--stdio` | bridge the daemon's JSON-RPC over stdin/stdout (not MCP: see `mcp`) |
| `mcp` | `--install {claude,codex,cursor,all} --dry-run --force` | run the MCP server on stdio, or --install it into an agent's config |
