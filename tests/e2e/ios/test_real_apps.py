"""Real apps on a real iPhone (2026-10-22): Safari driven like an agent would (needs the
internet), and an MCP session against the iPhone. Run with DROIDCTL_IOS_UDID=<hardware UDID>."""
import asyncio
import os
import sys

from tests.e2e.ios.test_i2 import APP, UDID, dc


def test_safari_address_bar_link_back_and_scroll():
    dc("open-url", "https://example.com")
    # after a load Safari shows "Reader and Translation Available" where the address goes, for a few seconds
    assert dc("wait", "--id", "TabBarItemTitle", "--timeout", "10")["matched"]
    r = dc("tap", "--id", "TabBarItemTitle")
    assert r["screen"]["keyboard"]
    dc("type", "en.wikipedia.org/wiki/IPhone", "--enter")
    assert dc("wait", "--text", "smartphones", "--timeout", "15")["matched"]
    dc("tap", "--text", "Steve Jobs")
    assert dc("wait", "--text", "Steven Paul Jobs", "--timeout", "15")["matched"]
    back = dc("back")
    assert back["via"] in ("ax", "touch")
    assert dc("wait", "--text", "smartphones", "--timeout", "10")["matched"]
    s = dc("scroll", "down")
    assert s["changed"] and s["method"] == "gesture"          # web views scroll by a drag, not AX
    dc("launch", APP)


def test_an_mcp_session_drives_the_iphone():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def go():
        exe = os.path.join(os.path.dirname(sys.executable), "droidctl")
        async with stdio_client(StdioServerParameters(command=exe, args=["mcp"])) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                out = {}
                for name, args in (("launch", {"pkg": APP}), ("snapshot", {}), ("tap", {"id": "dta_custom"}),
                                   ("key", {"key": "hide-keyboard"}), ("shot", {})):
                    res = await s.call_tool(name, dict(args, device=UDID))
                    out[name] = res
                return out
    out = asyncio.run(go())
    assert not any(r.is_error for r in out.values())
    assert "Custom" in out["snapshot"].content[0].text
    assert any(c.type == "image" for c in out["shot"].content)
