"""Live proof that a delegated subagent does real work on disk.

Run directly:  DEEPSEEK_API_KEY=... uv run python tests/smoke_task.py
Costs real tokens. Not collected by pytest.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def brief(label: str, payload: dict) -> None:
    print(f"\n--- {label} ---")
    for key in ("agent_id", "run_id", "state", "finish_reason", "elapsed_seconds", "error"):
        if payload.get(key) is not None:
            print(f"  {key}: {payload[key]}")
    if payload.get("result"):
        print(f"  result ({len(payload['result'])} chars): {payload['result'][:400]}")
    if payload.get("verification"):
        v = payload["verification"]
        print(f"  verification: {v['passed']} ({v['reason']}) `{v['command']}`")
    if payload.get("usage"):
        u = payload["usage"]
        print(f"  usage: {u['total']} tokens over {u['steps']} steps")
    if payload.get("distilled"):
        print(f"  distilled from {payload['raw_response_chars']} raw chars")


async def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="dsa-task-"))
    print("workspace:", workspace)
    env = {**os.environ, "DSA_WORKSPACE": str(workspace)}
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "deepseek_subagent_mcp"], env=env
    )

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()

        result = await session.call_tool(
            "dsh_delegate",
            {
                "task": (
                    "Create fizzbuzz.py in the workspace. It must print the FizzBuzz "
                    "sequence for 1 to 20, one entry per line. Run it with python3 and "
                    "confirm the output is correct. Done when the file exists and runs."
                ),
                "name": "fizzbuzz",
                "verification": "python3 fizzbuzz.py",
                "wait_seconds": 300,
            },
        )
        first = result.structured_content or {}
        brief("dsh_delegate", first)
        agent_id, run_id = first["agent_id"], first["run_id"]

        tr = await session.call_tool("dsh_transcript", {"run_id": run_id, "limit": 25})
        print("\n--- transcript ---")
        for line in (tr.structured_content or {}).get("activity", []):
            print("  ", line)

        print("\n--- workspace after delegate ---")
        for path in sorted(workspace.iterdir()):
            print("  ", path.name)
        created = workspace / "fizzbuzz.py"
        assert created.is_file(), "the subagent did not create fizzbuzz.py"
        out = subprocess.run(
            [sys.executable, str(created)], capture_output=True, text=True, timeout=30
        )
        lines = out.stdout.strip().splitlines()
        print("  fizzbuzz.py output:", lines[:6], "...", lines[-3:])
        assert lines[:5] == ["1", "2", "Fizz", "4", "Buzz"], lines[:5]
        assert len(lines) == 20, len(lines)

        follow = await session.call_tool(
            "dsh_continue",
            {
                "agent_id": agent_id,
                "message": (
                    "Now write test_fizzbuzz.py next to it: a plain python3 script "
                    "(no pytest) that imports or runs fizzbuzz.py, checks entry 15 is "
                    "FizzBuzz, and exits nonzero on failure. Run it."
                ),
                "wait_seconds": 300,
            },
        )
        second = follow.structured_content or {}
        brief("dsh_continue", second)
        test_file = workspace / "test_fizzbuzz.py"
        print("\n--- workspace after continue ---")
        for path in sorted(workspace.iterdir()):
            print("  ", path.name)
        assert test_file.is_file(), "the subagent did not create test_fizzbuzz.py"
        check = subprocess.run(
            [sys.executable, str(test_file)], capture_output=True, text=True,
            timeout=30, cwd=workspace,
        )
        print("  test_fizzbuzz.py exit:", check.returncode, check.stdout.strip()[:200])
        assert check.returncode == 0

        listing = await session.call_tool("dsh_list", {})
        agents = (listing.structured_content or {})["agents"]
        assert len(agents[0]["runs"]) == 2, "both turns should be on one agent"
        print("\n--- dsh_list ---")
        print(json.dumps(agents, indent=2)[:600])

        await session.call_tool("dsh_cancel", {"agent_id": agent_id})

    print("\ntask smoke OK — workspace kept at", workspace)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
