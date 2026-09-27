"""M1 round-trip benchmark: transport, agent, cold CLI and service-bind latency.

Needs a phone with the agent set up (`droidctl setup`). Usage:
    .venv/bin/python bench/rtt.py --count 300 --json [--raw] [--bind 3] [--out FILE]

  --raw     also measure a toybox `nc` echo over its own adb forward: the adb
            transport alone, with no agent in the path
  --bind N  N times: drop our service from enabled_accessibility_services, re-add
            it, and time until the socket answers (only our entry is touched)
"""
import argparse
import datetime
import json
import os
import platform
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from droidctl import device as dev  # noqa: E402

PY = sys.executable
RAW_PORT = 18765


def ms(t0):
    return (time.perf_counter() - t0) * 1000


def timed_calls(client, method, params, n):
    out = []
    for _ in range(n):
        t0 = time.perf_counter()
        client.call(method, params)
        out.append(ms(t0))
    return out


def fresh_connections(port, n):
    """Connect + first echo on a new connection, n times."""
    out = []
    for _ in range(n):
        t0 = time.perf_counter()
        with dev.AgentClient(port) as c:
            c.call("echo", {})
        out.append(ms(t0))
    return out


def raw_echo(serial, n):
    """The adb forward alone: toybox nc runs `cat` per connection on the phone."""
    adb = ["adb", "-s", serial]
    srv = subprocess.Popen(adb + ["shell", f"toybox nc -s 127.0.0.1 -p {RAW_PORT} -L cat"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    port = None
    try:
        time.sleep(1.0)
        port = int(subprocess.check_output(adb + ["forward", "tcp:0", f"tcp:{RAW_PORT}"]).strip())
        s = socket.create_connection(("127.0.0.1", port))
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        msg = b'{"jsonrpc":"2.0","id":1,"method":"echo","params":{}}\n'
        out = []
        for _ in range(n + 1):
            t0 = time.perf_counter()
            s.sendall(msg)
            got = b""
            while len(got) < len(msg):
                got += s.recv(65536)
            out.append(ms(t0))
        s.close()
        return out[1:]          # the first includes nc forking `cat`
    finally:
        if port:
            subprocess.call(adb + ["forward", "--remove", f"tcp:{port}"])
        subprocess.call(adb + ["shell", f"pkill -f 'nc -s 127.0.0.1 -p {RAW_PORT}'"])
        srv.kill()


def run_wall(argv, n):
    out = []
    for _ in range(n):
        t0 = time.perf_counter()
        subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        out.append(ms(t0))
    return out


def bind_latency(serial, port, n):
    """Time from re-adding our service to the first successful echo."""
    d = dev.adb_device(serial)
    out = []
    for _ in range(n):
        kept, _ = dev.remove_service(dev.get_services(d))
        dev.sh(d, ["settings", "put", "secure", "enabled_accessibility_services", dev.join_services(kept)])
        time.sleep(1.5)                                   # let Android unbind us
        after, _ = dev.add_service(dev.get_services(d))
        t0 = time.perf_counter()
        dev.sh(d, ["settings", "put", "secure", "enabled_accessibility_services", dev.join_services(after)])
        while True:
            try:
                with dev.AgentClient(port, timeout=1.0) as c:
                    c.call("echo", {}, timeout=1.0)
                break
            except dev.UserError:
                if ms(t0) > 10000:
                    raise
                time.sleep(0.01)
        out.append(ms(t0))
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-d", "--device", default=os.environ.get("ANDROID_SERIAL"))
    p.add_argument("--count", type=int, default=300)
    p.add_argument("--cold", type=int, default=25, help="cold CLI runs")
    p.add_argument("--raw", action="store_true")
    p.add_argument("--bind", type=int, default=0)
    p.add_argument("--json", action="store_true")
    p.add_argument("--out")
    a = p.parse_args()

    serial = dev.resolve_serial(a.device)
    client, info = dev.connect(serial)
    port = client.port
    stats = dev.rtt_stats
    res = {}
    try:
        timed_calls(client, "echo", {}, 20)                       # warm up
        res["echo"] = stats(timed_calls(client, "echo", {}, a.count))
        res["ping"] = stats(timed_calls(client, "ping", None, a.count))
        big = {"blob": "x" * 65536}
        res["echo_64k"] = stats(timed_calls(client, "echo", big, max(20, a.count // 10)))
    finally:
        client.close()
    res["fresh_connection"] = stats(fresh_connections(port, max(20, a.count // 10)))
    if a.raw:
        res["raw_adb_echo"] = stats(raw_echo(serial, a.count))
    res["cold_python_pass"] = stats(run_wall([PY, "-c", "pass"], a.cold))
    res["cold_import_cli"] = stats(run_wall([PY, "-c", "import droidctl.cli"], a.cold))
    res["cold_cli_version"] = stats(run_wall([PY, "-m", "droidctl", "version", "--json"], a.cold))
    res["cold_cli_ping"] = stats(run_wall([PY, "-m", "droidctl", "ping", "--json", "-d", serial], a.cold))
    if a.bind:
        res["service_bind"] = stats(bind_latency(serial, port, a.bind))

    report = {
        "date": datetime.date.today().isoformat(),
        "device": {k: info.get(k) for k in ("manufacturer", "model", "device", "sdk", "release", "screen")},
        "agent": {"version": info.get("version"), "versionCode": info.get("versionCode")},
        "host": {"python": platform.python_version(), "machine": platform.machine(),
                 "kernel": platform.release()},
        "transport": "usb-passthrough-vm",
        "units": "ms",
        "results": res,
    }
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w") as f:
            json.dump(report, f, indent=2)
            f.write("\n")
    if a.json:
        print(json.dumps(report, indent=2))
    else:
        for k, v in res.items():
            print(f"{k:18} " + "  ".join(f"{kk}={vv}" for kk, vv in v.items()))


if __name__ == "__main__":
    main()
