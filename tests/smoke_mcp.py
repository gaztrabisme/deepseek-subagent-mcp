"""End-to-end smoke test: drive the real MCP server over stdio.

Run directly (not under pytest):  uv run python tests/smoke_mcp.py

Without DEEPSEEK_API_KEY it still exercises everything except the model call:
runtime boot, the JSON-RPC handshake, run plumbing, and error reporting.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def show(label: str, result) -> dict:
    payload = result.structured_content or {}
    print(f"\n--- {label} ---")
    print(json.dumps(payload, indent=2)[:1400])
    return payload


async def main() -> int:
    workspace = tempfile.mkdtemp(prefix="dsa-mcp-")
    env = {
        **os.environ,
        "DSA_WORKSPACE": workspace,
        "DSA_SESSION_ROOT": f"{workspace}/.dsh-sessions",
        "DSA_MAX_AGENTS": "2",
    }
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "deepseek_subagent_mcp"], env=env
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        print("server:", init.server_info.name, init.server_info.version)

        tools = await session.list_tools()
        names = sorted(t.name for t in tools.tools)
        print("tools :", names)
        assert names == [
            "dsh_await",
            "dsh_cancel",
            "dsh_continue",
            "dsh_delegate",
            "dsh_list",
            "dsh_transcript",
        ], names

        show("dsh_list (empty)", await session.call_tool("dsh_list", {}))

        started = show(
            "dsh_delegate",
            await session.call_tool(
                "dsh_delegate",
                {
                    "task": "Reply with the single word: ready",
                    "name": "smoke",
                    "verification": "true",
                    "wait_seconds": 120,
                },
            ),
        )
        run_id = started["run_id"]
        agent_id = started["agent_id"]

        show("dsh_await", await session.call_tool("dsh_await", {"run_id": run_id,
                                                                "wait_seconds": 60}))
        show("dsh_transcript", await session.call_tool("dsh_transcript",
                                                       {"run_id": run_id, "limit": 12}))
        show("dsh_list", await session.call_tool("dsh_list", {}))
        show("dsh_cancel", await session.call_tool("dsh_cancel", {"agent_id": agent_id}))

        err = await session.call_tool("dsh_await", {"run_id": "run-nope"})
        print("\n--- unknown run_id ---")
        print("is_error:", err.is_error, "|", err.content[0].text[:200])
        assert err.is_error
    print("\nsmoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
