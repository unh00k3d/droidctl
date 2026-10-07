"""`droidctl mcp`: a real MCP server (stdio), built on the official `mcp` SDK.

It is a client of the daemon, so an MCP session, bash CLI calls and other
agents share one warm device connection, tree cache, refs and locks.

The tools are generated from the command registry (the argparse parser), the
same source as `cheat`: the schema can never drift from the CLI. The set is
small on purpose (every tool definition costs context on every turn); the
details live in one `droidctl://guide` resource. Results are the CLI's compact
text; `structuredContent` carries the --json payload; a failure is
`isError` with "kind: message" text; `shot` returns an image block.
"""
import base64
import json
import os
import shutil
import sys
import time

# (tool, command, description override). The description defaults to the
# command's own help line; `key` is the one tool that maps to several commands.
TOOLS = [
    ("snapshot", "snapshot", "The screen as a compact list of elements with [refs]. Call it first, and "
                             "after anything that may have changed the screen; act on refs, not coordinates."),
    ("tap", "tap", None),
    ("type", "type", None),
    ("scroll", "scroll", None),
    ("swipe", "swipe", None),
    ("action", "action", None),
    ("key", None, "Press a key: back, home, recents, notifications, quick-settings (system actions), "
                  "or enter, tab, del, search, KEYCODE_* / a number (adb keyevent)."),
    ("wait", "wait", None),
    ("launch", "launch", None),
    ("shot", "shot", "A screenshot of the screen as a JPEG image (downscaled), optionally with ref marks."),
    ("logs", "logs", None),
    ("devices", "devices", None),
]
GLOBAL_KEYS = ["back", "home", "recents", "notifications", "quick-settings"]
# options that make no sense for a tool call (shell quoting helpers, output modes)
SKIP_DESTS = {"help", "json", "stdin", "file", "fixture", "raw", "out", "base64", "no_daemon"}

GUIDE = """# droidctl (MCP)

Drive an Android phone through its accessibility tree.

1. Call `snapshot` first. Each line is one element with a [ref]:
   `[4] row "Ada Lovelace · Lunch?"  actions=[Archive, Delete]`
   Regions (top bar / content / bottom bar / dialog / keyboard) group them.
2. Act on refs: `tap {ref: 4}`, `type {ref: 2, content: "hello"}`, `action {ref: 4, name: "Delete"}`,
   `scroll {direction: "down"}`, `key {key: "back"}`. Locators work too (text, id, desc).
   Every action reports what changed; you rarely need another snapshot right away.
3. Refs are re-resolved against the live screen and never guessed by position. A ref that
   no longer resolves fails with a typed error instead of tapping the wrong thing:
   {kinds}. Re-run `snapshot` on `stale-ref`; use a more specific locator on `ambiguous`.
4. Copy labels verbatim from the snapshot; never invent them from a screenshot.
5. `shot` is for pixels only (colours, images, visual bugs); prefer `snapshot`.
6. Events that happened between your calls (toasts, new windows) are reported on the next
   result under `between_calls`.
7. Empty screen (a header but no elements, or a `no tree for app window` warning)? The app
   likely hides its UI from accessibility services (common in banking/finance). Tell the
   user to run `droidctl diagnose --fix` in a shell (switches to the uiautomation backend
   and disables the service), then relaunch the app; MCP cannot change the backend itself.
"""


# --------------------------------------------------------------------------
# the registry -> tool schemas
# --------------------------------------------------------------------------
def _subparsers():
    import argparse
    from droidctl import cli
    parser = cli.build_parser()
    act = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    helps = {ca.dest: (ca.help or "") for ca in act._choices_actions}
    return act.choices, helps


def _prop(action):
    import argparse
    if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
        p = {"type": "boolean"}
    elif action.choices:
        p = {"type": "string", "enum": [str(c) for c in action.choices]}
    elif action.type is int:
        p = {"type": "integer"}
    elif action.type is float:
        p = {"type": "number"}
    else:
        p = {"type": "string"}
    if action.nargs in ("+", "*") or isinstance(action, argparse._AppendAction):
        p = {"type": "array", "items": p}
    if action.help:
        p["description"] = action.help.replace("%%", "%")
    return p


def _fields(sp):
    """[(dest, action, positional?)] of a subparser, minus what tools don't take."""
    out = []
    for a in sp._actions:
        if a.dest in SKIP_DESTS:
            continue
        out.append((a.dest, a, not a.option_strings))
    return out


def tool_specs():
    """[{name, command, description, inputSchema}] generated from the parser."""
    choices, helps = _subparsers()
    specs = []
    for name, command, desc in TOOLS:
        if name == "key":
            if not any(k in choices for k in GLOBAL_KEYS + ["press"]):
                continue
            schema = {"type": "object", "properties": {
                "key": {"type": "string", "description": "back, home, recents, notifications, "
                                                         "quick-settings, enter, tab, del, search, KEYCODE_* or a number"},
                "device": {"type": "string", "description": "device serial (default: the only attached device)"}},
                "required": ["key"]}
            specs.append({"name": name, "command": None, "description": desc, "inputSchema": schema})
            continue
        sp = choices.get(command)
        if sp is None:
            continue                          # not in this build's registry
        props, required = {}, []
        for dest, action, positional in _fields(sp):
            props[dest] = _prop(action)
            if positional and action.nargs not in ("?", "*"):
                required.append(dest)
        schema = {"type": "object", "properties": props}
        if required:
            schema["required"] = required
        specs.append({"name": name, "command": command,
                      "description": desc or (helps.get(command) or "").capitalize(),
                      "inputSchema": schema})
    return specs


def first_snapshot_full(argv, seen):
    """The first snapshot of a device in this MCP session prints everything.

    "unchanged" is relative to the daemon's saved state, which the CLI and other
    clients share: an agent that never saw the screen must not be told it is
    unchanged. Later snapshots keep the token-saving form."""
    if not argv or argv[0] != "snapshot":
        return argv
    dev = argv[argv.index("-d") + 1] if "-d" in argv else ""
    if dev in seen or "--full" in argv or "--diff" in argv:
        seen.add(dev)
        return argv
    seen.add(dev)
    return argv[:1] + ["--full"] + argv[1:]


def build_argv(spec, args):
    """Tool arguments -> a droidctl argv (always --json)."""
    from droidctl.core import UserError
    args = dict(args or {})
    if spec["name"] == "key":
        key = str(args.pop("key", "")).strip()
        if not key:
            raise UserError("key: which key?", "bad-args")
        choices, _ = _subparsers()
        argv = [key] if key in GLOBAL_KEYS and key in choices else ["press", key]
        if args.get("device"):
            argv += ["-d", str(args["device"])]
        return argv + ["--json"]
    choices, _ = _subparsers()
    sp = choices[spec["command"]]
    known = {dest: (action, positional) for dest, action, positional in _fields(sp)}
    unknown = set(args) - set(known)
    if unknown:
        raise UserError(f"{spec['name']}: unknown argument(s) {', '.join(sorted(unknown))}", "bad-args")
    import argparse
    positionals, options = [], []
    for dest, action, positional in _fields(sp):
        if dest not in args or args[dest] is None:
            continue
        v = args[dest]
        if positional:
            positionals += [str(x) for x in v] if isinstance(v, list) else [str(v)]
            continue
        flag = max(action.option_strings, key=len)
        if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
            if bool(v) != bool(action.default):
                options.append(flag)
        elif isinstance(v, list):
            if isinstance(action, argparse._AppendAction):
                for x in v:
                    options += [flag, str(x)]
            else:
                options += [flag] + [str(x) for x in v]
        else:
            options += [flag, str(v)]
    return [spec["command"]] + positionals + options + ["--json"]


# --------------------------------------------------------------------------
# running a tool: through the daemon (or, if that is impossible, a subprocess)
# --------------------------------------------------------------------------
class Backend:
    def __init__(self):
        self._client = None

    def run(self, argv):
        """-> (payload or None, text, code)."""
        from droidctl.client import Client, DaemonUnavailable
        for attempt in (0, 1):
            try:
                if self._client is None:
                    self._client = Client()
                res = self._client.run(argv, both=True)
                return res.get("payload"), res.get("text") or "", res.get("code", 0)
            except (DaemonUnavailable, OSError, ConnectionError):
                if self._client is not None:
                    self._client.close()
                self._client = None
                if attempt == 1:
                    return self._subprocess(argv)
        return self._subprocess(argv)

    @staticmethod
    def _subprocess(argv):
        """Sandboxed without a daemon: run the in-process CLI in a child."""
        import subprocess
        proc = subprocess.run([sys.executable, "-m", "droidctl", *argv], capture_output=True, text=True,
                              env=dict(os.environ, DROIDCTL_NO_DAEMON="1"), stdin=subprocess.DEVNULL)
        try:
            payload = json.loads(proc.stdout)
        except ValueError:
            return None, (proc.stderr or proc.stdout).strip(), proc.returncode
        text = payload.get("text") if isinstance(payload, dict) else None
        return payload, text or json.dumps(payload, indent=1), proc.returncode


def result_for(spec, payload, text, code):
    """-> (content blocks as dicts, structured payload, is_error)."""
    if isinstance(payload, dict) and payload.get("ok") is False and "error" in payload:
        e = payload["error"]
        msg = f"{e.get('kind', 'error')}: {e.get('message', '')}" + (f"\n{e['hint']}" if e.get("hint") else "")
        return [{"type": "text", "text": msg}], payload, True
    if payload is None and code:
        return [{"type": "text", "text": text or "error"}], None, True
    blocks = []
    if spec["name"] == "shot" and isinstance(payload, dict):
        data = payload.get("data")
        if not data and payload.get("path") and os.path.exists(payload["path"]):
            with open(payload["path"], "rb") as f:
                data = base64.b64encode(f.read()).decode()
        if data:
            blocks.append({"type": "image", "data": data, "mimeType": "image/jpeg"})
            payload = {k: v for k, v in payload.items() if k != "data"}
    blocks.insert(0, {"type": "text", "text": text or "ok"})
    return blocks, payload if isinstance(payload, dict) else {"result": payload}, bool(code)


def guide():
    from droidctl.core import ERROR_KINDS
    kinds = ", ".join(f"`{k}`" for k in ("stale-ref", "ambiguous", "occluded", "offscreen", "disabled",
                                          "no-change", "not-installed", "no-device") if k in ERROR_KINDS)
    return GUIDE.replace("{kinds}", kinds)


# --------------------------------------------------------------------------
# the MCP server (official SDK, stdio)
# --------------------------------------------------------------------------
def build_server(backend=None):
    import anyio
    import mcp_types as types
    from mcp.server.lowlevel import Server
    from droidctl import __version__
    from droidctl.core import UserError

    backend = backend or Backend()
    specs = tool_specs()
    by_name = {s["name"]: s for s in specs}
    seen = set()                # devices this MCP session has had a full snapshot of
    output_schema = {"type": "object", "additionalProperties": True}

    async def list_tools(ctx, params):
        return types.ListToolsResult(tools=[
            types.Tool(name=s["name"], description=s["description"], input_schema=s["inputSchema"],
                       output_schema=output_schema) for s in specs])

    async def call_tool(ctx, params):
        spec = by_name.get(params.name)
        if spec is None:
            return types.CallToolResult(content=[types.TextContent(text=f"bad-args: no tool {params.name!r}")],
                                        is_error=True)
        try:
            argv = build_argv(spec, params.arguments)
        except UserError as e:
            return types.CallToolResult(content=[types.TextContent(text=f"{e.kind}: {e}")], is_error=True)
        argv = first_snapshot_full(argv, seen)
        payload, text, code = await anyio.to_thread.run_sync(backend.run, argv)
        blocks, structured, is_error = result_for(spec, payload, text, code)
        content = [types.ImageContent(data=b["data"], mime_type=b["mimeType"]) if b["type"] == "image"
                   else types.TextContent(text=b["text"]) for b in blocks]
        return types.CallToolResult(content=content, structured_content=structured, is_error=is_error)

    async def list_resources(ctx, params):
        return types.ListResourcesResult(resources=[
            types.Resource(uri="droidctl://guide", name="guide", mime_type="text/markdown",
                           description="How to drive a phone with droidctl's tools (read once).")])

    async def read_resource(ctx, params):
        if str(params.uri) != "droidctl://guide":
            raise ValueError(f"no resource {params.uri}")
        return types.ReadResourceResult(contents=[
            types.TextResourceContents(uri="droidctl://guide", mime_type="text/markdown", text=guide())])

    return Server("droidctl", version=__version__,
                  instructions="Android phone automation. Read droidctl://guide once; start with `snapshot`, "
                               "then act on its [refs].",
                  on_list_tools=list_tools, on_call_tool=call_tool,
                  on_list_resources=list_resources, on_read_resource=read_resource)


def run_stdio():
    try:
        import anyio
        from mcp.server.stdio import stdio_server
    except ImportError:
        from droidctl.core import UserError
        raise UserError("the MCP server needs the official SDK", "missing-dep",
                        hint="pip install 'droidctl[mcp]'")
    server = build_server()

    async def main():
        async with stdio_server() as (r, w):
            await server.run(r, w, server.create_initialization_options())

    anyio.run(main)


# --------------------------------------------------------------------------
# mcp --install: add droidctl to an agent's config, keeping everything else
# --------------------------------------------------------------------------
def _command():
    exe = shutil.which("droidctl")
    if exe:
        return os.path.abspath(exe), ["mcp"]
    return sys.executable, ["-m", "droidctl", "mcp"]


def _backup(path):
    if not os.path.exists(path):
        return None
    dest = f"{path}.droidctl-bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, dest)
    return dest


def _json_install(client, path, key, force, dry_run):
    cmd, args = _command()
    entry = {"command": cmd, "args": args}
    try:
        with open(path) as f:
            doc = json.load(f)
    except FileNotFoundError:
        doc = {}
    except ValueError as e:
        return {"client": client, "path": path, "action": "skipped",
                "detail": f"not valid JSON ({e}); left untouched"}
    servers = doc.setdefault(key, {})
    if not isinstance(servers, dict):
        return {"client": client, "path": path, "action": "skipped", "detail": f"'{key}' is not an object"}
    if servers.get("droidctl") == entry:
        return {"client": client, "path": path, "action": "already configured"}
    if "droidctl" in servers and not force:
        return {"client": client, "path": path, "action": "skipped",
                "detail": "a different droidctl entry exists; pass --force to replace it"}
    others = sorted(k for k in servers if k != "droidctl")
    res = {"client": client, "path": path, "entry": entry, "kept": others,
           "action": "would add" if dry_run else "added"}
    if dry_run:
        return res
    res["backup"] = _backup(path)
    servers["droidctl"] = entry
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return res


def _install_claude(force, dry_run):
    cmd, args = _command()
    claude = shutil.which("claude")
    if claude:                                # the supported way; never races Claude Code's own writes
        argv = [claude, "mcp", "add", "--scope", "user", "droidctl", "--", cmd, *args]
        res = {"client": "claude", "path": "claude mcp (user scope)", "command": argv}
        if dry_run:
            return dict(res, action="would run", detail=" ".join(argv))
        import subprocess
        got = subprocess.run([claude, "mcp", "get", "droidctl"], capture_output=True, text=True)
        if got.returncode == 0:
            if not force:
                return dict(res, action="already configured", detail="pass --force to replace it")
            subprocess.run([claude, "mcp", "remove", "--scope", "user", "droidctl"], capture_output=True)
        out = subprocess.run(argv, capture_output=True, text=True)
        if out.returncode != 0:
            return dict(res, action="failed", detail=(out.stderr or out.stdout).strip())
        return dict(res, action="added")
    return _json_install("claude", os.path.expanduser("~/.claude.json"), "mcpServers", force, dry_run)


def _install_codex(force, dry_run):
    path = os.path.expanduser("~/.codex/config.toml")
    cmd, args = _command()
    block = (f'[mcp_servers.droidctl]\ncommand = {json.dumps(cmd)}\n'
             f'args = [{", ".join(json.dumps(a) for a in args)}]\n')
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        text = ""
    header = "[mcp_servers.droidctl]"
    res = {"client": "codex", "path": path}
    if header in text:
        start = text.index(header)
        nxt = text.find("\n[", start + len(header))
        end = len(text) if nxt < 0 else nxt + 1
        if text[start:end].strip() == block.strip():
            return dict(res, action="already configured")
        if not force:
            return dict(res, action="skipped", detail="a different droidctl entry exists; pass --force to replace it")
        new = text[:start] + block + ("" if end == len(text) else "\n") + text[end:]
    else:
        new = text + ("\n" if text and not text.endswith("\n\n") else "") + block
    if dry_run:
        return dict(res, action="would add", detail=block.strip())
    res["backup"] = _backup(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        f.write(new)
    os.replace(path + ".tmp", path)
    return dict(res, action="added")


def install(which, dry_run=False, force=False):
    targets = ["claude", "codex", "cursor"] if which == "all" else [which]
    out = []
    for t in targets:
        if t == "claude":
            out.append(_install_claude(force, dry_run))
        elif t == "codex":
            out.append(_install_codex(force, dry_run))
        elif t == "cursor":
            out.append(_json_install("cursor", os.path.expanduser("~/.cursor/mcp.json"), "mcpServers",
                                     force, dry_run))
    return {"ok": all(r["action"] != "failed" for r in out), "dry_run": dry_run, "clients": out}
