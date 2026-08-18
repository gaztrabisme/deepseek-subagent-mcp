"""Live proof of the result contract: distillation, the cap, and the archive.

Costs real tokens. Not collected by pytest.

    DEEPSEEK_API_KEY=sk-... uv run python tests/smoke_result.py

Two things are proved here that no unit test can:

1. A verbose child answer is replaced by the seven-section handoff, produced by
   the child itself in the same session, and the raw text is still reachable.
2. A finished run outlives the agent that produced it. Delegate, walk away long
   enough for the reaper to take the process, come back, read the result.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parent.parent
SECTIONS = [
    "Goal", "Constraints", "Progress", "Key Decisions",
    "Next Steps", "Relevant Files", "Critical Context",
]

# The report has to land in the *reply*, not in a file. An earlier version of
# this task asked for a long report and the child helpfully wrote it to
# report.md and answered in three lines -- correct behaviour, useless test.
VERBOSE_TASK = (
    "Create three files in the workspace: alpha.py printing 'alpha', beta.py "
    "printing 'beta', and gamma.py printing 'gamma'. Run each one. Then, in "
    "your final reply itself and not in any file, write a detailed report of at "
    "least 700 words describing what you did, what each file contains line by "
    "line, how you verified it, and what could go wrong. Do not write the "
    "report to disk. Done when all three files exist and run."
)


def params(workspace: str, **env_overrides) -> StdioServerParameters:
    env = {**os.environ, "DSA_WORKSPACE": workspace, **env_overrides}
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "deepseek_subagent_mcp"],
        env=env,
        cwd=str(REPO),
    )


async def distillation_proof() -> None:
    print("\n=== distillation proof (DSA_SUMMARY_TOKENS=800) ===")
    workspace = tempfile.mkdtemp(prefix="dsa-result-")
    # cap = 800 * 3.5 = 2800 chars. A 600-word report is comfortably over it;
    # a seven-section handoff is comfortably under.
    connection = stdio_client(params(workspace, DSA_SUMMARY_TOKENS="800"))
    async with connection as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        result = (await s.call_tool("dsh_delegate", {
            "task": VERBOSE_TASK,
            "name": "verbose",
            "verification": "python3 alpha.py && python3 beta.py && python3 gamma.py",
            "wait_seconds": 420,
        })).structured_content

        print("state       :", result["state"])
        print("verification:", result.get("verification", {}).get("reason"))
        print("distilled   :", result.get("distilled"))
        print("raw chars   :", result.get("raw_response_chars"))
        print("result chars:", len(result["result"]))
        print("usage       :", result["usage"]["total"], "tokens /",
              result["usage"]["steps"], "steps")
        print("\n--- what crossed the boundary ---")
        print(result["result"][:1200])

        assert result["state"] == "completed", result.get("error")
        assert result.get("distilled") is True, "a 600-word report should have been distilled"
        assert len(result["result"]) <= 2800, "the cap was not applied"
        missing = [s_ for s_ in SECTIONS if s_.lower() not in result["result"].lower()]
        assert not missing, f"handoff is missing sections: {missing}"

        raw = (await s.call_tool("dsh_transcript", {
            "run_id": result["run_id"], "limit": 1, "raw": True,
        })).structured_content
        assert len(raw["raw_response"]) == result["raw_response_chars"]
        assert len(raw["raw_response"]) > len(result["result"])
        print(f"\nraw response still reachable: {len(raw['raw_response'])} chars")
    print("distillation proof OK")


async def archive_proof() -> None:
    print("\n=== archive proof (DSA_IDLE_TIMEOUT=5) ===")
    workspace = tempfile.mkdtemp(prefix="dsa-archive-")
    connection = stdio_client(params(workspace, DSA_IDLE_TIMEOUT="5"))
    async with connection as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        first = (await s.call_tool("dsh_delegate", {
            "task": "Reply with the single word: archived",
            "verification": "true",
            "wait_seconds": 180,
        })).structured_content
        run_id, agent_id = first["run_id"], first["agent_id"]
        print("run          :", run_id, first["state"])

        print("waiting for the reaper...")
        deadline = time.time() + 40
        while time.time() < deadline:
            await asyncio.sleep(3)
            listing = (await s.call_tool("dsh_list", {})).structured_content
            if listing["archived_runs"] >= 1 and listing["live"] == 0:
                break
        print("archived_runs:", listing["archived_runs"], "| live:", listing["live"])
        assert listing["live"] == 0, "the agent was never reaped"
        assert listing["archived_runs"] >= 1, "the run was not archived"

        after = (await s.call_tool("dsh_await", {
            "run_id": run_id, "wait_seconds": 1,
        })).structured_content
        print("await after reap:", after["state"], "|", after["result"][:60])
        assert after["state"] == "completed"
        assert after["result"], "the archived run lost its result"

        transcript = (await s.call_tool("dsh_transcript", {
            "run_id": run_id, "limit": 5,
        })).structured_content
        assert transcript["activity"], "the archived run lost its transcript"
        print("transcript after reap:", len(transcript["activity"]), "lines")

        gone = await s.call_tool("dsh_continue", {
            "agent_id": agent_id, "message": "still there?",
        })
        assert gone.is_error, "continuing a reaped agent should fail: the session is gone"
        print("continue after reap: refused, as it must be")
    print("archive proof OK")


async def main() -> None:
    if not os.environ.get("DEEPSEEK_API_KEY"):
        sys.exit("DEEPSEEK_API_KEY is required for this smoke test")
    await distillation_proof()
    await archive_proof()
    print("\nresult smoke OK")


if __name__ == "__main__":
    asyncio.run(main())
