"""
droidctl - an agent-first Android automation CLI.

Quick start:
    droidctl setup          # install the agent APK and enable its accessibility service
    droidctl ping           # round trip to the on-device agent
    droidctl doctor         # what is (not) working, and why
    droidctl cheat          # every command on one screen
"""
import argparse
import os
import sys

from droidctl import __version__
from droidctl.core import UserError, console, die, emit


# --------------------------------------------------------------------------
# meta commands
# --------------------------------------------------------------------------
def cmd_version(a):
    return {"ok": True, "version": __version__, "python": sys.version.split()[0]}


def render_version(p):
    console.print(f"droidctl {p['version']} [dim](python {p['python']})[/dim]")


def _surface():
    """Introspect the parser into a compact, structured command list (self-maintaining)."""
    parser = build_parser()
    subact = next(x for x in parser._actions if isinstance(x, argparse._SubParsersAction))
    help_map = {ca.dest: (ca.help or "") for ca in subact._choices_actions}
    by_parser, order = {}, []
    for name, sp in subact.choices.items():
        if id(sp) in by_parser:
            by_parser[id(sp)]["aliases"].append(name)
            continue
        by_parser[id(sp)] = {"command": name, "aliases": [], "sp": sp}
        order.append(id(sp))
    rows = []
    for key in order:
        rec = by_parser[key]
        sp = rec["sp"]
        pos, opts = [], []
        for act in sp._actions:
            if act.dest == "help":
                continue
            if not act.option_strings:
                mv = act.metavar or (("{" + ",".join(map(str, act.choices)) + "}")
                                     if act.choices else act.dest)
                if act.nargs == "?":
                    pos.append(f"[{mv}]")
                elif act.nargs in ("+", "*"):
                    pos.append(f"{mv}...")
                else:
                    pos.append(f"<{mv}>")
            else:
                flag = act.option_strings[-1]
                if isinstance(act, (argparse._StoreTrueAction, argparse._StoreFalseAction)) or act.nargs == 0:
                    opts.append(flag)
                elif act.choices:
                    opts.append(f"{flag} {{{','.join(map(str, act.choices))}}}")
                else:
                    opts.append(f"{flag} {act.metavar or act.dest.upper()}")
        rows.append({"command": rec["command"], "aliases": rec["aliases"],
                     "args": pos, "options": opts, "help": help_map.get(rec["command"], "")})
    return rows


def cmd_cheat(a):
    return _surface()


def render_cheat(rows):
    from rich.markup import escape
    for r in rows:
        alias = f" ({'/'.join(r['aliases'])})" if r["aliases"] else ""
        usage = escape(" ".join(r["args"] + r["options"]))
        console.print(f"[bold cyan]{r['command']}[/bold cyan][dim]{alias}[/dim] {usage}")
        if r["help"]:
            console.print(f"    [dim]{escape(r['help'])}[/dim]")


# --------------------------------------------------------------------------
# parser and dispatch
# --------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        prog="droidctl", description="Agent-first Android automation CLI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Quick start:")[1])
    sub = p.add_subparsers(dest="cmd", required=True)

    jsonopt = argparse.ArgumentParser(add_help=False)
    jsonopt.add_argument("--json", action="store_true", help="machine-readable JSON output")

    # every command that talks to a phone takes -d; it falls back to
    # ANDROID_SERIAL, then to the single attached device
    devopt = argparse.ArgumentParser(add_help=False)
    devopt.add_argument("-d", "--device", metavar="SERIAL",
                        default=os.environ.get("ANDROID_SERIAL") or None,
                        help="device serial (default: $ANDROID_SERIAL, else the only attached device)")

    sp = sub.add_parser("version", parents=[jsonopt], help="print the droidctl version")
    sp.set_defaults(fn=cmd_version, render=render_version)

    sp = sub.add_parser("cheat", parents=[jsonopt], help="every command and its options, on one screen")
    sp.set_defaults(fn=cmd_cheat, render=render_cheat)
    return p


def dispatch(args):
    """Run one parsed command with the shared error mapping, then emit its payload.

    Separate from main() so a future daemon or `run` can go through the exact
    same path and error vocabulary. Errors funnel through die(), which prints
    per --json and raises SystemExit.
    """
    try:
        payload = args.fn(args)
    except UserError as e:
        die(args, e.kind, e, e.hint)
    return emit(args, payload, getattr(args, "render", None))


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        dispatch(args)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
