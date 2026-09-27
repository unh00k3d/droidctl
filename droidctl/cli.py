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
import time

from droidctl import __version__
from droidctl import device as dev
from droidctl.core import UserError, console, die, emit


# --------------------------------------------------------------------------
# device commands: devices, setup, teardown, doctor, ping
# --------------------------------------------------------------------------
def cmd_devices(a):
    rows = []
    for serial, state in dev.list_devices():
        row = {"serial": serial, "state": state}
        if state == "device":
            row["model"] = dev.sh(dev.adb_device(serial), ["getprop", "ro.product.model"])
            row["setup"] = bool(dev.device_state(serial))
        rows.append(row)
    return {"ok": True, "devices": rows}


def render_devices(p):
    if not p["devices"]:
        console.print("[dim]no devices attached[/dim]")
    for r in p["devices"]:
        extra = f"  {r.get('model', '')}" + ("  [green]setup[/green]" if r.get("setup") else "")
        console.print(f"{r['serial']}  {r['state']}{extra}")


def _put_setting(d, key, value):
    dev.sh(d, ["settings", "put", "secure", key, value])


def _wait_for_agent(port, timeout=8.0):
    """Poll ping until Android binds the service and the socket answers."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            with dev.AgentClient(port, timeout=2.0) as c:
                return c.call("ping", timeout=2.0)
        except UserError as e:
            if e.kind != "connection" or time.monotonic() > deadline:
                raise
            time.sleep(0.2)


def cmd_setup(a):
    if not os.path.exists(dev.APK_PATH):
        raise UserError(f"the agent APK is not bundled ({dev.APK_PATH})", "missing-dep",
                        hint="run: make apk")
    serial = dev.resolve_serial(a.device)
    d = dev.adb_device(serial)

    # 1. install, unless the phone already has this exact versionCode
    before_version = dev.installed_version(d)
    installed = a.reinstall or before_version != dev.AGENT_VERSION_CODE
    if installed:
        d.sync.push(dev.APK_PATH, dev.DEVICE_TMP_APK)
        out = dev.sh(d, ["pm", "install", "-r", "-g", dev.DEVICE_TMP_APK], timeout=120)
        dev.sh(d, ["rm", "-f", dev.DEVICE_TMP_APK])
        if "Success" not in out:
            raise UserError(f"pm install failed: {out}", "adb")

    # 2. append our service; never overwrite what is there. The first setup
    #    records the original values so teardown can restore them exactly.
    raw_services = dev.sh(d, ["settings", "get", "secure", "enabled_accessibility_services"])
    raw_enabled = dev.sh(d, ["settings", "get", "secure", "accessibility_enabled"])
    before = dev.parse_services(raw_services)
    after, added = dev.add_service(before)
    if added:
        if "restore" not in dev.device_state(serial):
            dev.update_device_state(serial, restore={"services": raw_services,
                                                     "a11y_enabled": raw_enabled})
        _put_setting(d, "enabled_accessibility_services", dev.join_services(after))

    # 3. accessibility_enabled=1, re-checked (some ROMs reset it right away)
    for _ in range(5):
        if dev.a11y_enabled(d) and any(dev.is_ours(e) for e in dev.get_services(d)):
            break
        _put_setting(d, "accessibility_enabled", "1")
        time.sleep(0.2)
    else:
        raise UserError("the phone did not keep our accessibility service enabled", "not-installed",
                        hint="enable 'droidctl agent' in Settings > Accessibility, then run: droidctl doctor")

    # 4. forward and ping
    port = dev.ensure_forward(serial)
    info = _wait_for_agent(port)
    dev.update_device_state(serial, port=port, version_code=info.get("versionCode"))
    return {"ok": True, "serial": serial, "installed": installed, "previous_version": before_version,
            "service_added": added, "services_before": before, "services_after": dev.get_services(d),
            "port": port, "agent": info}


def render_setup(p):
    console.print(f"[green]ready[/green] {p['serial']}  agent {p['agent'].get('version')} "
                  f"(sdk {p['agent'].get('sdk')}, {p['agent'].get('model')})  tcp:{p['port']}")
    console.print(f"  apk {'installed' if p['installed'] else 'already up to date'}; "
                  f"service {'enabled' if p['service_added'] else 'was already enabled'}")
    others = [s for s in p["services_after"] if not dev.is_ours(s)]
    if others:
        console.print(f"  [dim]other accessibility services kept: {', '.join(others)}[/dim]")


def cmd_teardown(a):
    serial = dev.resolve_serial(a.device)
    d = dev.adb_device(serial)
    restore = dev.device_state(serial).get("restore", {})
    kept, removed = dev.remove_service(dev.get_services(d))
    if removed:
        if kept:
            _put_setting(d, "enabled_accessibility_services", dev.join_services(kept))
        elif restore.get("services", "null") == "null":
            dev.sh(d, ["settings", "delete", "secure", "enabled_accessibility_services"])
        else:
            _put_setting(d, "enabled_accessibility_services", "")
    if removed and not kept:
        # we were the last service: back to what it was before setup (never
        # touch the setting if we had not enabled anything)
        original = restore.get("a11y_enabled", "0")
        if original == "null":
            dev.sh(d, ["settings", "delete", "secure", "accessibility_enabled"])
        else:
            _put_setting(d, "accessibility_enabled", original)
    forward = dev.remove_forward(serial)
    uninstalled = False
    if not a.keep_apk and dev.installed_version(d) is not None:
        out = dev.sh(d, ["pm", "uninstall", dev.PKG])
        if "Success" not in out:
            raise UserError(f"pm uninstall failed: {out}", "adb")
        uninstalled = True
    dev.clear_device_state(serial)
    return {"ok": True, "serial": serial, "service_removed": removed, "services_after": kept,
            "forward_removed": forward, "uninstalled": uninstalled}


def render_teardown(p):
    console.print(f"[green]done[/green] {p['serial']}: service "
                  f"{'removed' if p['service_removed'] else 'was not enabled'}, "
                  f"apk {'uninstalled' if p['uninstalled'] else 'kept'}")
    if p["services_after"]:
        console.print(f"  [dim]other accessibility services kept: {', '.join(p['services_after'])}[/dim]")


def cmd_doctor(a):
    """Run every check; a failed check is data, not an exception. ok=false exits 1."""
    checks = []

    def check(name, fn):
        try:
            ok, detail = fn()
        except UserError as e:
            ok, detail = False, f"{e.kind}: {e}" + (f" ({e.hint})" if e.hint else "")
        checks.append({"name": name, "ok": ok, "detail": detail})
        return ok

    def skip(*names):
        checks.extend({"name": n, "ok": None, "detail": "skipped"} for n in names)

    later = ["apk", "service", "uiautomation", "socket", "peer-uid", "protocol", "rtt"]
    if not check("adb", lambda: (True, f"server version {int(dev._adb_host_query('host:version'), 16)}")):
        skip("device", *later)
        return {"ok": False, "checks": checks}
    ctx = {}

    def pick():
        ctx["serial"] = dev.resolve_serial(a.device)
        ctx["d"] = dev.adb_device(ctx["serial"])
        return True, ctx["serial"]
    if not check("device", pick):
        skip(*later)
        return {"ok": False, "checks": checks}
    d = ctx["d"]

    def apk():
        v = dev.installed_version(d)
        if v is None:
            return False, "not installed (run: droidctl setup)"
        if v != dev.AGENT_VERSION_CODE:
            return False, f"versionCode {v}, bundled {dev.AGENT_VERSION_CODE} (run: droidctl setup)"
        return True, f"versionCode {v}"
    apk_ok = check("apk", apk)

    def service():
        services = dev.get_services(d)
        ours = any(dev.is_ours(e) for e in services)
        others = [e for e in services if not dev.is_ours(e)]
        detail = f"others: {', '.join(others) or 'none'}"
        if not ours:
            return False, "not in enabled_accessibility_services; " + detail
        if not dev.a11y_enabled(d):
            return False, "accessibility_enabled is 0; " + detail
        return True, "enabled; " + detail
    service_ok = check("service", service)

    def uiautomation():
        procs = dev.sh(d, ["ps", "-A", "-o", "NAME"])
        hits = sorted({p for p in procs.split() if "uiautomator" in p or "appium" in p})
        if hits:
            return False, f"{', '.join(hits)} running; its UiAutomation suppresses accessibility services"
        return True, "no uiautomator/appium process"
    check("uiautomation", uiautomation)

    if not (apk_ok and service_ok):
        skip("socket", "peer-uid", "protocol", "rtt")
        return {"ok": False, "serial": ctx["serial"], "checks": checks}

    def sock():
        ctx["client"], ctx["ping"] = dev.connect(ctx["serial"])
        return True, f"tcp:{ctx['client'].port} -> {dev.REMOTE}, agent {ctx['ping'].get('version')}"
    if not check("socket", sock):
        skip("peer-uid", "protocol", "rtt")
        return {"ok": False, "checks": checks}
    info, client = ctx["ping"], ctx["client"]
    try:
        uid = info.get("peer_uid")
        checks.append({"name": "peer-uid", "ok": uid in (2000, 0),
                       "detail": f"{uid} ({ {2000: 'shell', 0: 'root'}.get(uid, 'unexpected')})"})
        proto = info.get("protocol")
        checks.append({"name": "protocol", "ok": proto == dev.PROTOCOL,
                       "detail": f"agent {proto}, host {dev.PROTOCOL}"})
        check("rtt", lambda: (True, dev.rtt_stats(dev.time_pings(client, 20))))
    finally:
        client.close()
    return {"ok": all(c["ok"] is not False for c in checks), "serial": ctx["serial"],
            "agent": info, "checks": checks}


def render_doctor(p):
    marks = {True: "[green]ok[/green]  ", False: "[red]FAIL[/red]", None: "[dim]skip[/dim]"}
    for c in p["checks"]:
        detail = c["detail"]
        if isinstance(detail, dict):
            detail = "  ".join(f"{k}={v}" for k, v in detail.items()) + " ms"
        console.print(f"{marks[c['ok']]} {c['name']:<13} {detail}", highlight=False)


def cmd_ping(a):
    serial = dev.resolve_serial(a.device)
    t0 = time.perf_counter()
    client, info = dev.connect(serial)
    connect_ms = (time.perf_counter() - t0) * 1000
    try:
        samples = dev.time_pings(client, max(1, a.count))
    finally:
        client.close()
    payload = {"ok": True, "serial": serial, **info,
               "connect_ms": round(connect_ms, 3), "rtt_ms": round(samples[0], 3)}
    if a.count > 1:
        payload["rtt"] = dev.rtt_stats(samples)
    return payload


def render_ping(p):
    line = (f"pong {p['serial']}  agent {p.get('version')} protocol {p.get('protocol')}  "
            f"sdk {p.get('sdk')}  {p.get('manufacturer')} {p.get('model')}  "
            f"rtt {p['rtt_ms']:.2f} ms (connect {p['connect_ms']:.1f} ms)")
    console.print(line, highlight=False)
    if "rtt" in p:
        console.print("  " + "  ".join(f"{k}={v}" for k, v in p["rtt"].items()), highlight=False)


# --------------------------------------------------------------------------
# dev: dump-fixture (real raw trees for the offline tests)
# --------------------------------------------------------------------------
FIXTURE_DIR = os.path.join("tests", "fixtures", "trees")


def _wait_quiet(client, quiet_ms=500, cap_ms=4000):
    """Poll the device's content-generation counter until it holds still for quiet_ms."""
    deadline = time.monotonic() + cap_ms / 1000
    last, since = client.call("gen")["gen"], time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(0.1)
        g = client.call("gen")["gen"]
        if g != last:
            last, since = g, time.monotonic()
        elif (time.monotonic() - since) * 1000 >= quiet_ms:
            return True
    return False


def _focused_activity(serial):
    """`pkg/.Activity` of the focused window, from dumpsys (dev path, not the hot path)."""
    import re
    out = dev.sh(dev.adb_device(serial), "dumpsys window windows | grep -E 'mCurrentFocus|mFocusedApp'")
    m = re.search(r"mCurrentFocus=Window\{\S+ \S+ ([^\s}]+)\}", out)
    return m.group(1) if m else None


def cmd_dump_fixture(a):
    import datetime
    import json
    import re
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", a.name):
        raise UserError(f"fixture name {a.name!r}: use lowercase letters, digits, '.', '_' and '-'", "bad-args")
    serial = dev.resolve_serial(a.device)
    client, info = dev.connect(serial)
    try:
        focus = None
        if a.pkg:
            deadline = time.monotonic() + a.timeout
            while True:
                focus = _focused_activity(serial)
                if focus and focus.split("/")[0] == a.pkg:
                    break
                if time.monotonic() > deadline:
                    raise UserError(f"{a.pkg} is not in the foreground (focus: {focus})", "timeout")
                time.sleep(0.3)
        settled = _wait_quiet(client)
        # a degraded dump may be the *previous* screen's last good tree: never
        # save one under this screen's name unless explicitly asked to
        for _ in range(3):
            tree = client.call("tree", {"not_important": a.not_important}, timeout=15)
            if not tree.get("degraded"):
                break
            time.sleep(1.0)
        if tree.get("degraded") and not a.allow_degraded:
            raise UserError(f"the tree stayed degraded ({tree.get('reason')}, {tree.get('ms')} ms) "
                            "after 3 tries; not saved", "timeout", hint="retry, or pass --allow-degraded")
        focus = _focused_activity(serial) or focus
    finally:
        client.close()
    if a.pkg and (not focus or focus.split("/")[0] != a.pkg):
        raise UserError(f"the foreground changed while dumping (focus: {focus})", "error")
    apps = [w for w in tree["windows"] if w.get("type") == "application"]
    meta = {
        "name": a.name,
        "captured": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "manufacturer": info.get("manufacturer"), "model": info.get("model"),
        "sdk": info.get("sdk"), "release": info.get("release"),
        "screen": info.get("screen"),
        "agent": {"version": info.get("version"), "versionCode": info.get("versionCode")},
        "package": (focus or "").split("/")[0] or (apps[0].get("pkg") if apps else None),
        "activity": focus,
        "not_important": a.not_important,
        "settled": settled,
        "dump_ms": tree.get("ms"), "nodes": tree.get("nodes"), "degraded": tree.get("degraded"),
    }
    os.makedirs(a.dir, exist_ok=True)
    path = os.path.join(a.dir, a.name + ".json")
    text = json.dumps({"meta": meta, "tree": tree}, ensure_ascii=False, indent=1) + "\n"
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(path + ".tmp", path)
    return {"ok": True, "path": path, "bytes": len(text.encode()), **{k: meta[k] for k in
            ("package", "activity", "nodes", "dump_ms", "degraded", "settled")}}


def render_dump_fixture(p):
    console.print(f"[green]saved[/green] {p['path']}  {p['nodes']} nodes, {p['bytes'] // 1024} KB, "
                  f"dump {p['dump_ms']} ms  [dim]{p['activity']}[/dim]"
                  + ("  [yellow]degraded[/yellow]" if p["degraded"] else "")
                  + ("" if p["settled"] else "  [yellow]never settled[/yellow]"), highlight=False)


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

    sp = sub.add_parser("devices", parents=[jsonopt], help="list attached devices and whether droidctl is set up")
    sp.set_defaults(fn=cmd_devices, render=render_devices)

    sp = sub.add_parser("setup", parents=[jsonopt, devopt],
                        help="install the agent APK and append its accessibility service (keeps the others)")
    sp.add_argument("--reinstall", action="store_true", help="install even if the same version is present")
    sp.set_defaults(fn=cmd_setup, render=render_setup)

    sp = sub.add_parser("teardown", parents=[jsonopt, devopt],
                        help="remove only our service, restore the a11y settings, uninstall the agent")
    sp.add_argument("--keep-apk", action="store_true", help="disable the service but keep the APK installed")
    sp.set_defaults(fn=cmd_teardown, render=render_teardown)

    sp = sub.add_parser("doctor", parents=[jsonopt, devopt],
                        help="check adb, APK, service, socket, peer UID and round-trip latency")
    sp.set_defaults(fn=cmd_doctor, render=render_doctor)

    sp = sub.add_parser("ping", parents=[jsonopt, devopt], help="round trip to the on-device agent")
    sp.add_argument("--count", type=int, default=1, metavar="N", help="ping N times and report min/median/p95")
    sp.set_defaults(fn=cmd_ping, render=render_ping)

    sp = sub.add_parser("dump-fixture", parents=[jsonopt, devopt],
                        help="save the current screen's raw tree as a test fixture (dev)")
    sp.add_argument("name", help="fixture name, e.g. real-settings-main")
    sp.add_argument("--pkg", metavar="PKG", help="wait until PKG is in the foreground, and fail if it leaves")
    sp.add_argument("--not-important", action="store_true", help="include views not important for accessibility")
    sp.add_argument("--dir", default=FIXTURE_DIR, metavar="DIR", help=f"output directory (default {FIXTURE_DIR})")
    sp.add_argument("--timeout", type=float, default=10.0, metavar="S", help="how long to wait for --pkg")
    sp.add_argument("--allow-degraded", action="store_true", help="save even a degraded (partial or stale) dump")
    sp.set_defaults(fn=cmd_dump_fixture, render=render_dump_fixture)
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
    emit(args, payload, getattr(args, "render", None))
    # a payload that reports ok=false (doctor with a failed check) still printed
    # in full, but the exit code has to tell a script something is wrong
    if isinstance(payload, dict) and payload.get("ok") is False:
        sys.exit(1)
    return payload


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        dispatch(args)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
