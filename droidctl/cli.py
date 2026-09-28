"""
droidctl - an agent-first Android automation CLI.

Quick start:
    droidctl setup          # install the agent APK and enable its accessibility service
    droidctl ping           # round trip to the on-device agent
    droidctl doctor         # what is (not) working, and why
    droidctl cheat          # every command on one screen

The first command starts a background daemon (idle-exit 30m): `droidctl daemon stop`
stops it, DROIDCTL_NO_DAEMON=1 never starts one.
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
            row["backend"] = dev.backend_for(serial)
        rows.append(row)
    return {"ok": True, "devices": rows}


def render_devices(p):
    if not p["devices"]:
        console.print("[dim]no devices attached[/dim]")
    for r in p["devices"]:
        extra = f"  {r.get('model', '')}" + ("  [green]setup[/green]" if r.get("setup") else "") \
            + (f"  backend {r['backend']}" if r.get("backend", "a11y") != "a11y" else "")
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


def _wait_for_windows(port, timeout=3.0):
    """A freshly bound service answers ping before it can see the app's windows
    (measured on the SM-N950F: the first command after a re-enable found nothing).
    Wait until the tree has an application window with a root; never fail setup."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with dev.AgentClient(port, timeout=3.0) as c:
                tree = c.call("tree", {}, timeout=3.0)
            if any(w.get("type") == "application" and w.get("root") for w in tree.get("windows", ())):
                return True
        except UserError:
            pass
        time.sleep(0.2)
    return False


def cmd_setup(a):
    if not os.path.exists(dev.APK_PATH):
        raise UserError(f"the agent APK is not bundled ({dev.APK_PATH})", "missing-dep",
                        hint="run: make apk")
    serial = dev.resolve_serial(a.device)
    d = dev.adb_device(serial)
    # an explicit --backend switches; without it, keep what this phone was set up with
    backend = getattr(a, "backend", None) or dev.device_state(serial).get("backend") or "a11y"
    if backend == "uiautomation":
        return _setup_ua(serial, d)
    if dev.ua_pids(d):
        dev.stop_ua(serial)                   # chose a11y: don't leave backend B holding UiAutomation

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

    # 4. forward and ping. Listed but not answering: Android stops restarting a
    #    service that keeps crashing (measured on the SM-N950F after the agent's
    #    event race: enabled in settings, no process, until the setting changed), so
    #    rebind it by writing the list without ours, then with ours appended again.
    #    Every other entry keeps its exact text and order.
    port = dev.ensure_forward(serial)
    rebound = False
    try:
        info = _wait_for_agent(port, timeout=8.0 if (installed or added) else 3.0)
    except UserError as e:
        if e.kind != "connection" or added:
            raise
        kept, _ = dev.remove_service(dev.get_services(d))
        if kept:
            _put_setting(d, "enabled_accessibility_services", dev.join_services(kept))
        else:
            dev.sh(d, ["settings", "delete", "secure", "enabled_accessibility_services"])
        time.sleep(0.3)
        _put_setting(d, "enabled_accessibility_services", dev.join_services(dev.add_service(kept)[0]))
        _put_setting(d, "accessibility_enabled", "1")
        rebound = True
        info = _wait_for_agent(port)
    if installed or added or rebound:
        _wait_for_windows(port)
    dev.update_device_state(serial, port=port, version_code=info.get("versionCode"), backend="a11y")
    return {"ok": True, "serial": serial, "backend": "a11y", "installed": installed, "previous_version": before_version,
            "service_added": added, "service_rebound": rebound, "services_before": before, "services_after": dev.get_services(d),
            "port": port, "agent": info}


def _setup_ua(serial, d):
    """Backend B: push the agent (not installed) and start it; nothing is enabled."""
    client, info = dev._connect_ua(serial, 5.0)
    port = client.port
    client.close()
    dev.update_device_state(serial, backend="uiautomation", ua_port=port)
    a11y_on = any(dev.is_ours(e) for e in dev.get_services(d))
    out = {"ok": True, "serial": serial, "backend": "uiautomation", "port": port, "agent": info,
           "a11y_service_enabled": a11y_on}
    if a11y_on:
        out["warning"] = ("droidctl's accessibility service is still enabled. Apps that hide their UI while "
                          "an accessibility service is on stay hidden; to switch it off: "
                          "droidctl teardown --backend a11y")
    return out


def render_setup(p):
    if p.get("backend") == "uiautomation":
        console.print(f"[green]ready[/green] {p['serial']}  agent {p['agent'].get('version')} over uiautomation "
                      f"(sdk {p['agent'].get('sdk')}, {p['agent'].get('model')})  tcp:{p['port']}")
        console.print("  nothing installed or enabled; the agent runs until idle "
                      f"{dev.UA_IDLE_MS // 60000} min, a reboot, or: droidctl teardown")
        if p.get("warning"):
            console.print(f"  [yellow]{p['warning']}[/yellow]")
        return
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
    which = getattr(a, "backend", None) or "all"
    ua = dev.stop_ua(serial) if which in ("all", "uiautomation") else None
    if which == "uiautomation":
        if dev.device_state(serial).get("backend") == "uiautomation":
            dev.update_device_state(serial, backend="a11y", ua_port=None)
        return {"ok": True, "serial": serial, "backend": which, "uiautomation": ua}
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
    if which == "all":
        dev.clear_device_state(serial)
    else:                                     # a11y only: keep backend B's choice
        st = dev.device_state(serial)
        dev.clear_device_state(serial)
        if st.get("backend") == "uiautomation":
            dev.update_device_state(serial, backend="uiautomation")
    return {"ok": True, "serial": serial, "backend": which, "service_removed": removed, "services_after": kept,
            "forward_removed": forward, "uninstalled": uninstalled, "uiautomation": ua}


def render_teardown(p):
    ua = p.get("uiautomation")
    if ua is not None:
        console.print(f"[green]done[/green] {p['serial']}: uiautomation agent "
                      f"{'stopped' if ua['stopped'] else 'was not running'}"
                      f"{', files removed' if ua['removed'] else ''}")
    if p.get("backend") == "uiautomation":
        return
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

    # daemon health first: it is a host concern, independent of the device, and
    # not-running is a fine state (the first command auto-starts it). Never a
    # failure on its own; a stale/broken socket is the only FAIL here.
    def daemon_check():
        from droidctl import daemon as _d
        st = _d.status()
        if not st.get("running"):
            return True, "not running (starts automatically on the first command)"
        cache = st.get("cache") or {}
        hits, misses = cache.get("hits", 0), cache.get("misses", 0)
        rate = cache.get("hit_rate")
        rate = f"{rate * 100:.0f}%" if rate is not None else "n/a"
        secs = st.get("uptime_s", 0)
        up = f"{secs / 60:.0f}m" if secs >= 60 else f"{secs:.0f}s"
        return True, (f"running pid {st.get('pid')}  up {up}  v{st.get('version')}"
                      f"  devices {len(st.get('devices') or [])}"
                      f"  cache {hits}/{hits + misses} ({rate})")
    check("daemon", daemon_check)

    later = ["backend", "apk", "service", "uiautomation", "socket", "peer-uid", "protocol", "rtt"]
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

    def backend():
        ctx["backend"] = b = dev.backend_for(ctx["serial"])
        src = "DROIDCTL_BACKEND" if os.environ.get("DROIDCTL_BACKEND") else \
              "setup" if dev.device_state(ctx["serial"]).get("backend") else "default"
        pids = dev.ua_pids(d)
        ua = f"; uiautomation agent running (pid {', '.join(map(str, pids))})" if pids else ""
        return True, f"{b} ({src}){ua}"
    if not check("backend", backend):
        skip(*later[1:])
        return {"ok": False, "serial": ctx["serial"], "checks": checks}
    if ctx["backend"] == "uiautomation":
        return _doctor_ua(ctx, d, check, skip, checks)

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


def _doctor_ua(ctx, d, check, skip, checks):
    """Doctor for backend B: nothing is installed, so check what it needs instead."""
    def service():
        on = any(dev.is_ours(e) for e in dev.get_services(d))
        return True, ("droidctl's accessibility service is ALSO enabled: apps that hide their UI while "
                      "one is on stay hidden (droidctl teardown --backend a11y)") if on else \
            "droidctl's accessibility service is not enabled (as intended for this backend)"
    check("service", service)

    def uiautomation():
        procs = dev.sh(d, ["ps", "-A", "-o", "NAME"])
        hits = sorted({p for p in procs.split() if "uiautomator" in p or "appium" in p})
        if hits:
            return False, f"{', '.join(hits)} running: it holds the one UiAutomation, so this backend cannot start"
        return True, "no uiautomator/appium process"
    check("uiautomation", uiautomation)

    def sock():
        ctx["client"], ctx["ping"] = dev.connect(ctx["serial"])      # starts the agent if needed
        return True, (f"tcp:{ctx['client'].port} -> {dev.REMOTES['uiautomation']}, agent "
                      f"{ctx['ping'].get('version')} (pid {', '.join(map(str, dev.ua_pids(d))) or '?'}, "
                      f"exits after {dev.UA_IDLE_MS // 60000} min idle)")
    if not check("socket", sock):
        skip("peer-uid", "protocol", "rtt")
        return {"ok": False, "serial": ctx["serial"], "backend": "uiautomation", "checks": checks}
    info, client = ctx["ping"], ctx["client"]
    try:
        uid = info.get("peer_uid")
        checks.append({"name": "peer-uid", "ok": uid in (2000, 0),
                       "detail": f"{uid} ({ {2000: 'shell', 0: 'root'}.get(uid, 'unexpected')})"})
        proto = info.get("protocol")
        checks.append({"name": "protocol", "ok": proto == dev.PROTOCOL and info.get("backend") == "uiautomation",
                       "detail": f"agent {proto} ({info.get('backend')}), host {dev.PROTOCOL}"})
        check("rtt", lambda: (True, dev.rtt_stats(dev.time_pings(client, 20))))
    finally:
        client.close()
    return {"ok": all(c["ok"] is not False for c in checks), "serial": ctx["serial"],
            "backend": "uiautomation", "agent": info, "checks": checks}


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
        # a degraded dump is a partial read (agent protocol 3): a fixture should be
        # complete, so retry, and only keep a partial one when explicitly asked to
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
# --------------------------------------------------------------------------
# look: snapshot and where
# --------------------------------------------------------------------------
LAYOUTS = ("spatial", "flat")


def _snap_opts(a):
    from droidctl import snapshot as snap_mod
    try:     # $DROIDCTL_LAYOUT is the base; flags given on the command line win
        return snap_mod.Opts.from_env(
            strict=True,
            layout=a.layout, regions=False if a.no_regions else None, rows=False if a.no_rows else None,
            grids=False if a.no_grids else None, infer=False if a.no_infer else None,
            geo=a.geo or None, map=a.map or None, bounds=a.bounds, max=a.max, find=a.find, within=a.within)
    except ValueError as e:
        raise UserError(str(e), "bad-args")


def _recent_toast(client, prev_state):
    """(toast text or None, event seq now) for the snapshot header: toasts since the
    previous snapshot or action, else ones fired in the last few seconds. Never fails
    the snapshot: an agent without events just gets no toast."""
    from droidctl import snapshot as snap_mod
    prev_seq = (prev_state or {}).get("evseq")
    try:
        r = client.call("events", {"since": prev_seq or 0}, timeout=5)
        evs = r.get("events") or []
        now = None
        if prev_seq is None and any(e.get("type") == "toast" for e in evs) \
                and not all("age_ms" in e for e in evs):
            now = client.call("ping", {}, timeout=5).get("uptime_ms")   # only to age a toast
    except UserError:
        return None, prev_seq
    return snap_mod.pick_toast(r.get("events"), prev_seq, now), r.get("next", prev_seq)


def cmd_snapshot(a):
    import json
    from droidctl import snapshot as snap_mod
    opts = _snap_opts(a)
    serial, activity = None, None
    if a.fixture:
        try:
            with open(a.fixture, encoding="utf-8") as f:
                doc = json.load(f)
        except OSError as e:
            raise UserError(f"cannot read {a.fixture}: {e.strerror}", "not-found")
        tree = doc.get("tree", doc)
        activity = (doc.get("meta") or {}).get("activity")
    else:
        serial = dev.resolve_serial(a.device)
        prev_state = snap_mod.load_state(serial)
        client, _info = dev.connect(serial)
        try:
            tree = client.call("tree", {}, timeout=15)
            # never a frame mid-activity-transition (two screens at once); bounded
            deadline = time.monotonic() + 1.0
            while snap_mod.leaving_windows(tree) and time.monotonic() < deadline:
                time.sleep(0.1)
                tree = client.call("tree", {}, timeout=15)
            if not a.raw:
                toast, evseq = _recent_toast(client, prev_state)
        finally:
            client.close()
    if a.raw:
        return {"ok": True, "raw": tree}
    snap = snap_mod.build(tree, activity=activity, system=a.system)
    if serial:
        snap.toast, snap.evseq = toast, evseq
        snap_mod.carry_refs(prev_state, snap)   # before rendering: the numbers it shows are kept
    if a.within is not None and not any(e.ref == a.within for e in snap.elements):
        raise UserError(f"no element [{a.within}] on this screen", "not-found", hint="run: droidctl snapshot")
    prev = snap_mod.load_state(serial) if serial else None
    text = snap_mod.render(snap, opts)
    changes, unchanged = None, False
    if a.diff and prev:
        if prev.get("sig") == snap.sig:
            changes = snap_mod.diff(prev, snap)
            unchanged = not changes
            text = "\n".join([snap_mod.header(snap, opts)] + (changes or ["unchanged"]))
        else:
            text += f"\n(new screen: sig was {prev.get('sig')}; full snapshot shown instead of a diff)"
    elif (prev and not (a.full or a.find or a.within is not None or a.map)
          and prev.get("sig") == snap.sig and prev.get("lines") == snap_mod.flat_lines(snap)):
        unchanged = True
        text = (snap_mod.header(snap, opts)
                + f"\nunchanged ({len(snap.elements)} elements; --full to print them again)")
    if serial:
        snap_mod.save_state(serial, snap_mod.to_state(snap, serial))
    shown = snap_mod.select(snap, opts)[:opts.max]
    return {
        "ok": True,
        "screen": {"pkg": snap.pkg, "activity": snap.activity or None, "title": snap.title or None,
                   "sig": snap.sig, "keyboard": bool(snap.keyboard), "dialog": snap.dialog,
                   "size": [snap.screen[2], snap.screen[3]], "degraded": snap.degraded,
                   "dump": snap.dump, "gen": snap.gen},
        "unchanged": unchanged, "diff": changes,
        "elements": [snap_mod.element_json(e) for e in shown],
        "total": len(snap.elements), "warnings": snap.warnings, "toast": snap.toast,
        "text": text, "tokens_est": snap_mod.est_tokens(text),
    }


def render_snapshot(p):
    import json
    if "raw" in p:
        print(json.dumps(p["raw"], ensure_ascii=False, indent=1))
    else:
        print(p["text"])


def cmd_where(a):
    from droidctl import snapshot as snap_mod
    serial = dev.resolve_serial(a.device)
    state = snap_mod.load_state(serial)
    if not state:
        raise UserError(f"no saved snapshot for {serial}", "not-found", hint="run: droidctl snapshot")
    info = snap_mod.where(state, a.ref)
    if info is None:
        raise UserError(f"no element [{a.ref}] in the last snapshot", "not-found", hint="run: droidctl snapshot")
    return {"ok": True, "sig": state.get("sig"), **info}


def render_where(p):
    lab = f' "{p["label"]}"' if p.get("label") else ""
    parent = ("  inside " + " ".join(f"[{r}]" for r in p["inside"])) if p.get("inside") else ""
    print(f"[{p['ref']}] {p['role']}{lab}  region={p['region']}{parent}")
    if p.get("bounds"):
        b, (w, h) = p["bounds"], p["size"]
        print(f"  box [{b[0]},{b[1]},{b[2]},{b[3]}]  {w}x{h} px  {p['geo']}  tap {p['tap'][0]},{p['tap'][1]}")
    for d in ("left", "right", "above", "below"):
        n = p["neighbours"].get(d)
        if n:
            lab = f' "{n["label"]}"' if n.get("label") else ""
            print(f"  {d:<6} [{n['ref']}] {n['role']}{lab}  gap {n['gap']} px")


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
# agent skill and the generated docs (AGENTS.md table, SKILL.md)
# --------------------------------------------------------------------------
SKILL_DEFAULT_DIR = os.path.expanduser("~/.claude/skills")
# options every device command takes; the docs state them once instead of per row
_UNIVERSAL_OPTS = ("--json", "--device SERIAL", "--no-auto-setup")
# the shared locator group (commands.py `_locators`), shown as one LOCATOR token
_LOCATOR_OPTS = ("--ref N", "--id ID", "--text TEXT", "--desc DESC", "--class CLASS",
                 "--role ROLE", "--index I", "--right-of ANCHOR", "--left-of ANCHOR",
                 "--above ANCHOR", "--below ANCHOR", "--near ANCHOR", "--point X,Y")


def _doc_usage(r):
    """One row's usage for the docs: universal options dropped, locators folded."""
    opts = [o for o in r["options"] if o not in _UNIVERSAL_OPTS]
    if all(o in opts for o in _LOCATOR_OPTS):
        first = opts.index(_LOCATOR_OPTS[0])
        opts = [o for o in opts if o not in _LOCATOR_OPTS]
        opts.insert(first, "LOCATOR")
    return " ".join(r["args"] + opts)


def _command_table():
    """The full command surface as a Markdown table, generated from the parser."""
    lines = ["| command | usage | what |", "|---|---|---|"]
    for r in _surface():
        alias = f" ({'/'.join(r['aliases'])})" if r["aliases"] else ""
        usage = _doc_usage(r).replace("|", "\\|")
        usage = f"`{usage}`" if usage else "–"
        lines.append(f"| `{r['command']}{alias}` | {usage} | {r['help']} |")
    return "\n".join(lines)


def _error_kind_list():
    """The `error.kind` vocabulary as one inline Markdown line, from ERROR_KINDS."""
    from droidctl.core import ERROR_KINDS
    return ", ".join(f"`{k}`" for k in ERROR_KINDS if k != "error")


def _error_kind_table():
    """The `error.kind` vocabulary with meanings, as a Markdown table (AGENTS.md)."""
    from droidctl.core import ERROR_KINDS
    return "\n".join(["| kind | meaning |", "|---|---|"]
                     + [f"| `{k}` | {v} |" for k, v in ERROR_KINDS.items()])


def _skill_text():
    """SKILL.md with the generated bits filled in, so they can never drift."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "SKILL.md")
    if not os.path.exists(path):
        raise UserError("SKILL.md is missing from the installed package", "missing-dep")
    with open(path) as f:
        body = f.read()
    return (body.replace("<!-- COMMANDS -->", _command_table())
                .replace("<!-- ERROR-KINDS -->", _error_kind_list()))


def cmd_skill(a):
    """Print or install the agent skill (docs that ship with the binary)."""
    text = _skill_text()
    if a.action == "print":
        return {"ok": True, "text": text}
    root = os.path.expanduser(a.dir or SKILL_DEFAULT_DIR)
    dest = os.path.join(root, "droidctl", "SKILL.md")
    if os.path.exists(dest) and not a.force:
        raise UserError(f"{dest} already exists; pass --force to overwrite", "bad-args")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w") as f:
        f.write(text)
    return {"ok": True, "path": dest, "chars": len(text)}


def render_skill(p):
    if "text" in p:
        print(p["text"])
        return
    console.print(f"[green]installed[/green] skill → {p['path']}")
    console.print("[dim]agents that read this directory pick it up next session[/dim]")


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

    sp = sub.add_parser("skill", parents=[jsonopt],
                        help="print the agent skill (SKILL.md), or install it for Claude Code")
    sp.add_argument("action", choices=("print", "install"))
    sp.add_argument("--dir", metavar="DIR", help=f"skills directory (default {SKILL_DEFAULT_DIR})")
    sp.add_argument("--force", action="store_true", help="overwrite an existing SKILL.md")
    sp.set_defaults(fn=cmd_skill, render=render_skill)

    sp = sub.add_parser("devices", parents=[jsonopt], help="list attached devices and whether droidctl is set up")
    sp.set_defaults(fn=cmd_devices, render=render_devices)

    sp = sub.add_parser("setup", parents=[jsonopt, devopt],
                        help="install the agent APK and append its accessibility service (keeps the others)")
    sp.add_argument("--reinstall", action="store_true", help="install even if the same version is present")
    sp.add_argument("--backend", choices=dev.BACKENDS,
                    help="a11y: the accessibility service (default); uiautomation: run the agent over adb with "
                         "a UiAutomation, nothing installed or enabled. Remembered per phone")
    sp.set_defaults(fn=cmd_setup, render=render_setup)

    sp = sub.add_parser("teardown", parents=[jsonopt, devopt],
                        help="remove only our service, restore the a11y settings, uninstall the agent")
    sp.add_argument("--keep-apk", action="store_true", help="disable the service but keep the APK installed")
    sp.add_argument("--backend", choices=dev.BACKENDS + ("all",), default="all",
                    help="remove only this backend (default: all of droidctl)")
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
    sp.add_argument("--allow-degraded", action="store_true", help="save even a degraded (partial) dump")
    sp.set_defaults(fn=cmd_dump_fixture, render=render_dump_fixture)

    sp = sub.add_parser("snapshot", aliases=["snap"], parents=[jsonopt, devopt],
                        help="the screen as a compact list of elements with refs")
    sp.add_argument("--diff", action="store_true", help="only what changed since the last snapshot")
    sp.add_argument("--find", metavar="TEXT", help="only elements whose text, hint, desc, error or id contains TEXT")
    sp.add_argument("--in", dest="within", type=int, metavar="REF", help="only element REF and what is inside it")
    sp.add_argument("--raw", action="store_true", help="the raw device tree as JSON")
    sp.add_argument("--bounds", action="store_true", help="add each element's box in device px")
    sp.add_argument("--layout", choices=LAYOUTS, help="spatial (regions, rows, grids) or flat (default: $DROIDCTL_LAYOUT, e.g. 'spatial,no-rows,geo', else spatial)")
    sp.add_argument("--no-regions", action="store_true", help="spatial layout without region headers")
    sp.add_argument("--no-rows", action="store_true", help="spatial layout with one element per line")
    sp.add_argument("--no-grids", action="store_true", help="spatial layout without grid tables")
    sp.add_argument("--no-infer", action="store_true", help="don't guess labels for unlabeled controls")
    sp.add_argument("--geo", action="store_true", help="add each element's box as screen percentages (@x,y wxh)")
    sp.add_argument("--map", action="store_true", help="add an ASCII wireframe of the screen with refs")
    sp.add_argument("--system", action="store_true", help="include the status bar, navigation bar and other system windows")
    sp.add_argument("--max", type=int, default=150, metavar="N", help="print at most N elements (default 150)")
    sp.add_argument("--full", action="store_true", help="print everything even when the screen is unchanged")
    sp.add_argument("--fixture", metavar="PATH", help="render a saved fixture instead of the device (offline)")
    sp.set_defaults(fn=cmd_snapshot, render=render_snapshot)

    sp = sub.add_parser("where", parents=[jsonopt, devopt],
                        help="an element's box, region and neighbours (from the last snapshot)")
    sp.add_argument("ref", type=int, help="the element's ref")
    sp.set_defaults(fn=cmd_where, render=render_where)

    # act / observe / apps / run (implemented in act.py, registered in commands.py)
    from droidctl import commands
    commands.add_parsers(sub, jsonopt, devopt)

    # daemon / serve / mcp (implemented in daemon.py and mcp.py)
    from droidctl import daemon
    daemon.add_parsers(sub, jsonopt)
    return p


def dispatch(args, mode="inprocess"):
    """Run one parsed command with the shared error mapping, then emit its payload.

    The daemon runs the same parser and command functions (daemon.execute) but
    emits per request itself. Errors funnel through die(), which prints per
    --json and raises SystemExit. Every --json result says where it ran.
    """
    try:
        payload = args.fn(args)
    except UserError as e:
        die(args, e.kind, e, e.hint, getattr(e, "data", None), mode=mode)
    if isinstance(payload, dict):
        payload.setdefault("mode", mode)
    emit(args, payload, getattr(args, "render", None))
    # a payload that reports ok=false (doctor with a failed check) still printed
    # in full, but the exit code has to tell a script something is wrong
    if isinstance(payload, dict) and payload.get("ok") is False:
        sys.exit(1)
    return payload


def main(argv=None, mode="inprocess"):
    """The in-process CLI (`python -m droidctl`, --no-daemon, and the thin
    client's fallback). The `droidctl` script is droidctl.client:main."""
    argv = list(sys.argv[1:] if argv is None else argv)
    argv = [t for t in argv if t != "--no-daemon"]
    args = build_parser().parse_args(argv)
    try:
        dispatch(args, mode)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
