"""The act / observe / app commands' parsers. Stdlib only at import time.

The implementations live in act.py, which pulls in snapshot/resolve; each
command here is a lazy wrapper so `import droidctl.cli` stays cheap.
"""
import argparse


def _lazy(name):
    def fn(a):
        from droidctl import act
        return getattr(act, name)(a)
    fn.__name__ = name
    return fn


def render_text(p):
    text = p.get("text")
    if text:
        print(text)
    if p.get("warning"):
        import sys
        print(f"warning: {p['warning']}", file=sys.stderr)


def _locators(sp, positional=True):
    """The locator options every element action shares."""
    if positional:
        sp.add_argument("target", nargs="?", metavar="REF", help="a ref from `snapshot` (e.g. 4 or [4])")
    g = sp.add_argument_group("locators")
    g.add_argument("--ref", type=int, metavar="N", help="the element's ref (same as the positional REF)")
    g.add_argument("--id", metavar="ID", help="resource id (name, or pkg:id/name)")
    g.add_argument("--text", metavar="TEXT", help="visible text, hint or desc (case-insensitive)")
    g.add_argument("--desc", metavar="DESC", help="content description")
    g.add_argument("--class", dest="cls", metavar="CLASS", help="class, short (Button) or full")
    g.add_argument("--role", metavar="ROLE", help="snapshot role (button, input, switch, row, …)")
    g.add_argument("--index", type=int, metavar="I", help="pick the I-th (0-based) of several matches")
    g.add_argument("--right-of", metavar="ANCHOR", help="to the right of this text or ref")
    g.add_argument("--left-of", metavar="ANCHOR", help="to the left of this text or ref")
    g.add_argument("--above", metavar="ANCHOR", help="above this text or ref")
    g.add_argument("--below", metavar="ANCHOR", help="below this text or ref")
    g.add_argument("--near", metavar="ANCHOR", help="nearest to this text or ref")
    g.add_argument("--point", metavar="X,Y", help="device pixels: an explicit coordinate escape hatch")


def _act_opts(sp, method=True):
    if method:
        sp.add_argument("--method", choices=("auto", "action", "gesture"), default="auto",
                        help="auto: ACTION_CLICK, one event-gated gesture fallback; action/gesture: only that")
    sp.add_argument("--settle", type=int, metavar="MS",
                    help="quiet window before reading the result (default 150; 0 = don't wait)")
    sp.add_argument("--expect-change", action="store_true", help="fail with no-change if nothing changed")


def add_parsers(sub, jsonopt, devopt):
    actopt = argparse.ArgumentParser(add_help=False)
    actopt.add_argument("--no-auto-setup", action="store_true",
                        help="fail with not-installed instead of running setup automatically")
    common = [jsonopt, devopt, actopt]

    def add(name, help, fn, aliases=()):
        sp = sub.add_parser(name, parents=common, help=help, aliases=list(aliases))
        sp.set_defaults(fn=_lazy(fn), render=render_text)
        return sp

    # -- act on elements
    sp = add("tap", "tap an element: ACTION_CLICK, verified by the click event and a diff", "cmd_tap")
    _locators(sp)
    sp.add_argument("--double", action="store_true", help="double-tap (a gesture)")
    _act_opts(sp)

    sp = add("long-press", "long-press an element (ACTION_LONG_CLICK, else a long gesture)", "cmd_long_press")
    _locators(sp)
    _act_opts(sp)

    sp = add("type", "set an input's text (Unicode; set_text, then paste, then adb input)", "cmd_type")
    sp.add_argument("target", nargs="?", metavar="REF", help="the input's ref (default: the focused input)")
    sp.add_argument("content", nargs="?", metavar="TEXT", help="the text (or --stdin / --file to avoid shell quoting)")
    _locators(sp, positional=False)
    sp.add_argument("--append", action="store_true", help="add to the current text instead of replacing it")
    sp.add_argument("--clear", action="store_true", help="clear the field (with no TEXT)")
    sp.add_argument("--enter", action="store_true", help="press the IME action / Enter afterwards")
    sp.add_argument("--stdin", action="store_true", help="read the text from stdin")
    sp.add_argument("--file", metavar="F", help="read the text from a file")
    _act_opts(sp, method=False)

    sp = add("scroll", "scroll an element (default: the main list) up/down/left/right", "cmd_scroll")
    sp.add_argument("direction", choices=("up", "down", "left", "right"), help="where the content should go")
    sp.add_argument("target", nargs="?", metavar="REF", help="the scrollable's ref")
    _locators(sp, positional=False)
    _act_opts(sp, method=False)

    sp = add("scroll-to", "scroll until an element matching --text/--id/--desc is on screen", "cmd_scroll_to")
    _locators(sp, positional=False)
    sp.add_argument("--direction", choices=("up", "down", "left", "right"), default="down",
                    help="scroll this way first (default down), then back the other way")
    sp.add_argument("--one-way", action="store_true", help="don't try the other direction at the end")
    sp.add_argument("--max-scrolls", type=int, default=15, metavar="N", help="give up after N scrolls (15)")
    _act_opts(sp, method=False)

    sp = add("swipe", "swipe the screen or an element; DIR is the finger's direction", "cmd_swipe")
    sp.add_argument("direction", choices=("up", "down", "left", "right"), help="finger direction")
    sp.add_argument("target", nargs="?", metavar="REF", help="swipe inside this element")
    _locators(sp, positional=False)
    sp.add_argument("--distance", type=float, default=0.4, metavar="F", help="fraction of the box (0.4)")
    sp.add_argument("--ms", type=int, default=400, metavar="MS", help="swipe duration (400)")
    _act_opts(sp, method=False)

    sp = add("action", "run an element's accessibility action by label (custom actions like Delete)", "cmd_action")
    sp.add_argument("target", nargs="?", metavar="REF", help="the element's ref")
    sp.add_argument("label", help="the action's label, e.g. Delete or Archive")
    _locators(sp, positional=False)
    _act_opts(sp, method=False)

    sp = add("set", "set a slider/range value (or an input's text)", "cmd_set")
    sp.add_argument("target", nargs="?", metavar="REF", help="the element's ref")
    sp.add_argument("value", help="the value (a number for a range)")
    _locators(sp, positional=False)
    _act_opts(sp, method=False)

    for name, fn, what in (("focus", "cmd_focus", "give an element input focus"),
                           ("expand", "cmd_expand", "expand an element (ACTION_EXPAND)"),
                           ("collapse", "cmd_collapse", "collapse an element (ACTION_COLLAPSE)"),
                           ("dismiss", "cmd_dismiss", "dismiss an element (ACTION_DISMISS)")):
        sp = add(name, what, fn)
        _locators(sp)
        _act_opts(sp, method=False)

    sp = add("gesture", "a raw finger path in device px (escape hatch)", "cmd_gesture")
    sp.add_argument("--path", required=True, metavar="'X,Y X,Y …'", help="two or more points")
    sp.add_argument("--ms", type=int, default=500, metavar="MS", help="duration (500)")
    _act_opts(sp, method=False)

    for name, fn, what in (("back", "cmd_back", "the system Back"),
                           ("home", "cmd_home", "go to the home screen"),
                           ("recents", "cmd_recents", "open the recent apps"),
                           ("notifications", "cmd_notifications", "open the notification shade"),
                           ("quick-settings", "cmd_quick_settings", "open quick settings")):
        sp = add(name, what, fn)
        _act_opts(sp, method=False)

    sp = add("press", "press a key via adb (enter, tab, del, search, KEYCODE_*, or a number)", "cmd_press")
    sp.add_argument("key", help="key name or code")
    _act_opts(sp, method=False)

    # -- wait / observe
    sp = add("wait", "block on the device until a condition holds (event-driven)", "cmd_wait")
    sp.add_argument("--text", metavar="TEXT", help="a visible node whose text/desc contains TEXT")
    sp.add_argument("--id", metavar="ID", help="a visible node with this resource id")
    sp.add_argument("--desc", metavar="DESC", help="a visible node whose desc contains DESC")
    sp.add_argument("--gone", action="store_true", help="wait until the node condition no longer holds")
    sp.add_argument("--exact", action="store_true", help="text/desc must be equal, not contained")
    sp.add_argument("--activity", metavar="CLASS", help="the front activity (class or suffix)")
    sp.add_argument("--toast", nargs="?", const="", metavar="TEXT", help="a toast (containing TEXT)")
    sp.add_argument("--window", metavar="TITLE|PKG", help="a window with this title or package")
    sp.add_argument("--pkg", metavar="PKG", help="an app window of this package in front")
    sp.add_argument("--timeout", type=float, default=10.0, metavar="S", help="give up after S seconds (10)")

    sp = add("current", "the foreground app/activity and whether the keyboard is shown", "cmd_current")

    sp = add("watch", "pushed device events (clicks, toasts, windows, IME), bounded", "cmd_watch")
    sp.add_argument("--max", type=int, default=20, metavar="N", help="stop after N events (20)")
    sp.add_argument("--timeout", type=float, default=10.0, metavar="S", help="stop after S seconds (10)")
    sp.add_argument("--events", metavar="TYPES", help="comma-separated event types (default all)")
    sp.add_argument("--all", action="store_true", help="include content/scroll churn")

    sp = add("logs", "recent logcat lines, bounded", "cmd_logs")
    sp.add_argument("--max", type=int, default=100, metavar="N", help="at most N lines (100)")
    sp.add_argument("--pkg", metavar="PKG", help="only this app's process")
    sp.add_argument("--level", choices=("V", "D", "I", "W", "E", "F"), help="minimum level")

    sp = add("shot", "a screenshot (downscaled JPEG), optionally with ref marks", "cmd_shot")
    sp.add_argument("--scale", type=float, default=0.5, metavar="F", help="downscale factor (0.5)")
    sp.add_argument("--full", action="store_true", help="full resolution")
    sp.add_argument("--quality", type=int, default=70, metavar="Q", help="JPEG quality (70)")
    sp.add_argument("--marks", action="store_true", help="draw ref-numbered boxes (takes a fresh snapshot)")
    sp.add_argument("--crop", type=int, metavar="REF", help="only this element's box")
    sp.add_argument("--out", metavar="F", help="output path (default ~/.droidctl/shots/…)")
    sp.add_argument("--base64", action="store_true", help="include the JPEG as base64 in --json")

    # -- apps
    sp = add("launch", "start an app, wait for its window and settle; prints the new screen", "cmd_launch")
    sp.add_argument("pkg", help="package name")
    sp.add_argument("--activity", metavar="CLASS", help="a specific activity (default: the launcher one)")
    sp.add_argument("--stop", action="store_true", help="force-stop it first (a cold start)")
    sp.add_argument("--clear", action="store_true", help="clear its data first (pm clear: destructive)")
    sp.add_argument("--timeout", type=float, default=10.0, metavar="S", help="wait for its window (10 s)")
    _act_opts(sp, method=False)

    sp = add("stop-app", "force-stop an app", "cmd_stop_app")
    sp.add_argument("pkg", help="package name")

    sp = add("apps", "list installed apps (third-party by default)", "cmd_apps")
    sp.add_argument("--all", action="store_true", help="include system apps")
    sp.add_argument("--filter", metavar="TEXT", help="only packages containing TEXT")

    sp = add("install", "install an APK (adb install -r)", "cmd_install")
    sp.add_argument("apk", help="path to the .apk")
    sp.add_argument("--grant", action="store_true", help="grant all runtime permissions (-g)")

    sp = add("open-url", "open a URL or deep link (ACTION_VIEW) and settle", "cmd_open_url")
    sp.add_argument("url", help="the URL")
    _act_opts(sp, method=False)

    # -- batching
    sp = add("run", "run many steps in one process and one device session", "cmd_run")
    sp.add_argument("file", nargs="?", help="a file with one step per line ('-' = stdin)")
    sp.add_argument("--step", action="append", metavar="CMD", help="a step (repeatable), e.g. 'tap 4'")
    sp.add_argument("--keep-going", action="store_true", help="run the remaining steps after a failure")
