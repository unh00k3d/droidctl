# droidctl — agent reference

A single CLI that drives a real Android phone through its accessibility tree. **This file is
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
  structure does), `keyboard=`, `dialog=` and the screen size. Toasts are reported on action
  results (`toast`) and between calls (`between_calls`), not in the header.
- Options: `--diff` (only what changed since the last snapshot; `unchanged` if nothing),
  `--find TEXT`, `--in REF` (one subtree), `--bounds`, `--geo` (`@x,y wxh` as screen %),
  `--map` (ASCII wireframe), `--layout flat` (one element per line), `--system` (include the
  status/navigation bars), `--max N`, `--raw` (the full raw tree as JSON).
- `where REF`: one element's box in device px, its region and its neighbours.
- `shot [--marks] [--crop REF] [--scale 0.5] [--out F]`: a JPEG screenshot, with ref boxes drawn
  by `--marks`. Use it for pixels only (colours, images, visual bugs); prefer `snapshot`.
  `FLAG_SECURE` windows (banking apps, password screens) fail with `secure-window`.
- `current`: the foreground package/activity and whether the keyboard is shown.

## Act

Every element action takes a positional `REF` or a **locator**: `--ref N`, `--id ID`,
`--text TEXT` (text, hint or desc, case-insensitive), `--desc`, `--class`, `--role`
(+ `--index I`), or spatially `--right-of/--left-of/--above/--below/--near ANCHOR`
(`tap --text "+" --right-of "Wireless Mouse"`). A locator that matches more than one element
fails with `ambiguous` (the candidates are in `error.data`); it never picks one for you.
`--point X,Y` (device pixels) is the explicit coordinate escape hatch.

```bash
droidctl tap 7                                   # or tap --text "Checkout"
droidctl tap 7 --double | long-press 7
droidctl type 2 "Merhaba dünya 😀" --enter        # --append, --clear
droidctl type 2 --stdin < message.txt            # quotes/$/newlines: --stdin or --file F
droidctl scroll down                             # the main list; scroll up 4 for a specific one
droidctl scroll-to --text "Privacy"              # scroll until it is on screen
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
or the whole new screen when `new_screen`). The refs in it refer to the **updated** screen, so
you can usually act again without another `snapshot`. `--expect-change` turns "nothing
changed" into the `no-change` error; `--settle MS` sets the quiet window (default 150 ms,
`0` = don't wait).

**How `tap` decides** (`--method auto`, the default):
- `ACTION_CLICK` on the element (or its clickable ancestor) → `method:"action"`.
- If the element refuses the click, one gesture tap at the centre of its visible area →
  `method:"gesture-fallback"` with a `warning`.
- If the click was performed but produced no click event and no visible change, droidctl
  does **not** tap again — the app may already have handled it, and a second tap could submit
  twice. You get `method:"action"` plus a `warning`; if nothing happened, retry with
  `--method gesture`.
- `--method action|gesture` forces one path.

**Typing** tries `ACTION_SET_TEXT` (Unicode, no tap), then a clipboard paste (the clipboard is
restored), then `adb input text` (ASCII only), and reads the value back (`value` in the
result). `--enter` presses the keyboard's action key.

## Wait and observe

```bash
droidctl wait --text "Saved" --timeout 5         # --id, --desc, --gone, --activity, --toast, --window
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
| `connection` | the device agent's socket is unreachable (forward lost, service not running) |
| `no-device` | no device attached, or none matches -d / ANDROID_SERIAL |
| `adb` | an adb command failed (its own message is passed through) |
| `not-installed` | the droidctl agent is not installed or its service is not enabled (run: droidctl setup) |
| `device` | the device agent rejected the request (its own message is passed through) |
| `suppressed` | another UiAutomation client (Appium/uiautomator2) is suppressing accessibility services |
| `screen-off` | the screen is off or locked |
| `secure-window` | the window is FLAG_SECURE, so it cannot be captured |
| `stale-ref` | the ref no longer resolves (gone, shifted or occupied); re-run snapshot |
| `ambiguous` | the locator matched more than one element |
| `occluded` | the element is covered (by another window or the keyboard) |
| `offscreen` | the element is outside the visible area; scroll to it first |
| `disabled` | the element is disabled |
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
| `setup` | `--reinstall` | install the agent APK and append its accessibility service (keeps the others) |
| `teardown` | `--keep-apk` | remove only our service, restore the a11y settings, uninstall the agent |
| `doctor` | – | check adb, APK, service, socket, peer UID and round-trip latency |
| `ping` | `--count N` | round trip to the on-device agent |
| `dump-fixture` | `<name> --pkg PKG --not-important --dir DIR --timeout S --allow-degraded` | save the current screen's raw tree as a test fixture (dev) |
| `snapshot (snap)` | `--diff --find TEXT --in REF --raw --bounds --layout {spatial,flat} --no-regions --no-rows --no-grids --no-infer --geo --map --system --max N --full --fixture PATH` | the screen as a compact list of elements with refs |
| `where` | `<ref>` | an element's box, region and neighbours (from the last snapshot) |
| `tap` | `[REF] LOCATOR --double --method {auto,action,gesture} --settle MS --expect-change` | tap an element: ACTION_CLICK, verified by the click event and a diff |
| `long-press` | `[REF] LOCATOR --method {auto,action,gesture} --settle MS --expect-change` | long-press an element (ACTION_LONG_CLICK, else a long gesture) |
| `type` | `[REF] [TEXT] LOCATOR --append --clear --enter --stdin --file F --settle MS --expect-change` | set an input's text (Unicode; set_text, then paste, then adb input) |
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
| `back` | `--settle MS --expect-change` | the system Back |
| `home` | `--settle MS --expect-change` | go to the home screen |
| `recents` | `--settle MS --expect-change` | open the recent apps |
| `notifications` | `--settle MS --expect-change` | open the notification shade |
| `quick-settings` | `--settle MS --expect-change` | open quick settings |
| `press` | `<key> --settle MS --expect-change` | press a key via adb (enter, tab, del, search, KEYCODE_*, or a number) |
| `wait` | `--text TEXT --id ID --desc DESC --gone --exact --activity CLASS --toast TEXT --window TITLE\|PKG --pkg PKG --timeout S` | block on the device until a condition holds (event-driven) |
| `current` | – | the foreground app/activity and whether the keyboard is shown |
| `watch` | `--max N --timeout S --events TYPES --all` | pushed device events (clicks, toasts, windows, IME), bounded |
| `logs` | `--max N --pkg PKG --level {V,D,I,W,E,F}` | recent logcat lines, bounded |
| `shot` | `--scale F --full --quality Q --marks --crop REF --out F --base64` | a screenshot (downscaled JPEG), optionally with ref marks |
| `launch` | `<pkg> --activity CLASS --stop --clear --timeout S --settle MS --expect-change` | start an app, wait for its window and settle; prints the new screen |
| `stop-app` | `<pkg>` | force-stop an app |
| `apps` | `--all --filter TEXT` | list installed apps (third-party by default) |
| `install` | `<apk> --grant` | install an APK (adb install -r) |
| `open-url` | `<url> --settle MS --expect-change` | open a URL or deep link (ACTION_VIEW) and settle |
| `run` | `[file] --step CMD --keep-going` | run many steps in one process and one device session |
| `daemon` | `<{start,stop,restart,status,logs}> --foreground --lines N` | the background daemon: start, stop, restart, status, logs |
| `serve` | `--stdio` | bridge the daemon's JSON-RPC over stdin/stdout (not MCP: see `mcp`) |
| `mcp` | `--install {claude,codex,cursor,all} --dry-run --force` | run the MCP server on stdio, or --install it into an agent's config |
