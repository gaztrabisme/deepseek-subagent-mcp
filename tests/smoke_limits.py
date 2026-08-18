"""Live proof that the reaper and the run deadline work against a real runtime.

Run directly: DEEPSEEK_API_KEY=... uv run python tests/smoke_limits.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def child_processes() -> int:
    out = subprocess.run(["pgrep", "-f", "dsh-jsonrpc-agent"], capture_output=True, text=True)
    return len([line for line in out.stdout.splitlines() if line.strip()])


async def session_for(**env_overrides):
    ws = tempfile.mkdtemp(prefix="dsa-lim-")
    env = {**os.environ, "DSA_WORKSPACE": ws, **env_overrides}
    return StdioServerParameters(
        command=sys.executable, args=["-m", "deepseek_subagent_mcp"], env=env
    ), ws


async def reaper_proof() -> None:
    params, ws = await session_for(DSA_MAX_AGENTS="1", DSA_IDLE_TIMEOUT="5")
    print("\n=== reaper proof (DSA_MAX_AGENTS=1, DSA_IDLE_TIMEOUT=5) ===")
    async with stdio_client(params) as (read, write), ClientSession(read, write) as s:
        await s.initialize()
        a = (await s.call_tool("dsh_delegate", {"task": "Reply with: one", "verification": "true",
                                                "wait_seconds": 180})).structured_content
        print("first  :", a["state"], "|", a["result"][:40])
        await asyncio.sleep(12)  # idle timeout + one reaper interval
        listing = (await s.call_tool("dsh_list", {})).structured_content
        print("after idle: live =", listing["live"], "agents =", len(listing["agents"]))
        b = (await s.call_tool("dsh_delegate", {"task": "Reply with: two", "verification": "true",
                                                "wait_seconds": 180})).structured_content
        print("second :", b["state"], "|", b.get("result", "")[:40])
        assert b["state"] == "completed", "capacity did not recover after reaping"
        assert listing["live"] == 0, "idle agent was not reaped"
    print("reaper proof PASSED")


async def deadline_proof() -> None:
    # The gate is off for this leg on purpose. With it on, the supervisor denies
    # `sleep` (not a read-only command), the child never blocks, and the run
    # finishes before the deadline -- which proves the gate works and tells you
    # nothing about the deadline. Isolate the thing under test.
    params, ws = await session_for(
        DSA_RUN_TIMEOUT="15", DSA_BASH_TIMEOUT_MS="600000", DSA_SUPERVISOR="off"
    )
    print("\n=== deadline proof (DSA_RUN_TIMEOUT=15, supervisor off) ===")
    before = child_processes()
    async with stdio_client(params) as (read, write), ClientSession(read, write) as s:
        await s.initialize()
        r = (await s.call_tool("dsh_delegate", {
            "task": "Run this bash command and wait for it to finish: sleep 400",
            "verification": "true",
            "wait_seconds": 120})).structured_content
        print("state  :", r["state"])
        print("error  :", (r.get("error") or "")[:160])
        assert r["state"] == "failed", f"expected failed, got {r['state']}"
        assert "DSA_RUN_TIMEOUT" in (r.get("error") or "")
    await asyncio.sleep(2)
    after = child_processes()
    print(f"runtime processes: before={before} after={after}")
    assert after <= before, "runtime process leaked after the deadline kill"
    print("deadline proof PASSED")


async def main() -> int:
    await reaper_proof()
    await deadline_proof()
    print("\nall limit proofs PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
