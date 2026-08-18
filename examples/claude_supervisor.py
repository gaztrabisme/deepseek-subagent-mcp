"""A real Claude as the supervisor for a delegated DeepSeek agent.

This is the middle-manager pattern end to end: DeepSeek does the work, and every
tool call the policy classifier cannot settle on its own is handed to Claude,
which answers allow or deny before the call runs.

    DEEPSEEK_API_KEY=sk-... uv run python examples/claude_supervisor.py
    DEEPSEEK_API_KEY=sk-... uv run python examples/claude_supervisor.py --model opus

It needs the `claude` CLI on PATH and logged in. Nothing else: an MCP client that
advertises the `sampling` capability *is* the supervisor seat, and this script
fills it by shelling out to Claude in headless mode.

Inside a real MCP client — Claude Code, say — none of this is needed. The client
already advertises sampling, so its own model answers escalations and the tier
resolves to `sampling` on its own. This script exists so the pattern can be run,
watched, and understood without one.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parent.parent

TASK = """\
Create wordcount.py in the workspace: a command-line tool that takes a file path \
as its single argument and prints the five most common words and their counts, \
one per line, as "word: count", most frequent first. Words are case-insensitive \
and split on non-letters.

Also create test_wordcount.py with pytest tests covering an empty file, an \
ordinary file, and a file with fewer than five distinct words.

Done when `python3 -m pytest -q` passes in the workspace.
"""

VERIFICATION = "python3 -m pytest -q"

# The first task is ordinary work and the classifier settles all of it without a
# model, which is the point of having a classifier. To see the supervisor decide
# anything you have to ask for something it cannot settle -- so the follow-up
# deliberately asks for one call of each kind.
FOLLOW_UP = """\
Two more things, each as a single shell command, and report what happened:

1. Run the tests and then run the tool on a file you create called sample.txt, \
as ONE command line joined with &&.
2. Write a one-line summary of the results to a file OUTSIDE this workspace, at \
$TMPDIR/wordcount-summary.txt.

If a command is blocked, say so and move on. Do not try to work around a block.
"""

verdicts: list[tuple[str, str]] = []


def ask_claude(system_prompt: str, question: str, model: str) -> str:
    """One headless Claude call. Blocking -- callers push it to a thread."""
    result = subprocess.run(
        ["claude", "-p", question, "--model", model, "--append-system-prompt", system_prompt],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        # Deny by saying so plainly: the caller turns anything not starting with
        # ALLOW into a denial, and the reason is worth seeing.
        return f"DENY\nsupervisor CLI failed: {result.stderr.strip()[:200]}"
    return result.stdout.strip()


def make_supervisor(model: str):
    async def supervise(context, request: types.CreateMessageRequestParams):
        question = "".join(getattr(m.content, "text", "") for m in request.messages)
        answer = await anyio.to_thread.run_sync(
            ask_claude, request.system_prompt or "", question, model
        )
        head, _, tail = answer.partition("\n")
        verdicts.append((head.strip(), tail.strip()))
        print(f"\n  [supervisor] {head.strip()} — {tail.strip()[:160]}")
        return types.CreateMessageResult(
            role="assistant",
            model=f"claude-{model}",
            content=types.TextContent(type="text", text=answer),
        )

    return supervise


async def main(model: str) -> None:
    if not os.environ.get("DEEPSEEK_API_KEY"):
        sys.exit("DEEPSEEK_API_KEY is required")

    workspace = tempfile.mkdtemp(prefix="dsa-claude-sup-")
    print(f"workspace : {workspace}")
    print(f"supervisor: claude {model}")
    print("worker    : deepseek-v4-pro\n")

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "deepseek_subagent_mcp"],
        env={**os.environ, "DSA_WORKSPACE": workspace, "DSA_LOG_LEVEL": "warning"},
        cwd=str(REPO),
    )

    connection = stdio_client(params)
    async with connection as (r, w), ClientSession(
        r, w, sampling_callback=make_supervisor(model)
    ) as session:
        await session.initialize()

        print("delegating...", flush=True)
        result = (await session.call_tool("dsh_delegate", {
            "task": TASK,
            "name": "wordcount",
            "verification": VERIFICATION,
            "wait_seconds": 600,
        })).structured_content

        print(f"  first task  : {result['state']}\n")
        print("following up with work the policy cannot settle alone...", flush=True)
        follow = (await session.call_tool("dsh_continue", {
            "agent_id": result["agent_id"],
            "message": FOLLOW_UP,
            "verification": VERIFICATION,
            "wait_seconds": 600,
        })).structured_content

        listing = (await session.call_tool("dsh_list", {})).structured_content
        supervisor = listing["supervisor"]

        print("\n--- outcome ---")
        print(f"  state       : {follow['state']}")
        print(f"  verification: {follow.get('verification', {}).get('reason')}")
        agent = listing["agents"][0]
        print(f"  tokens      : {agent['usage']['total']} over {agent['usage']['steps']} steps")
        print(f"  tier        : {supervisor['tier']}")

        print("\n--- every verdict, in order ---")
        for decision in supervisor["recent_decisions"]:
            print(f"  {decision['action']:6} [{decision['tier']:13}] "
                  f"{decision['tool'] or '-':6} {decision['reason'][:80]}")

        decisions = supervisor["recent_decisions"]
        escalated = [d for d in decisions if d["tier"] == "sampling"]
        print(f"\n  {len(escalated)} of {len(decisions)} calls needed Claude; "
              f"the rest were settled by policy, with no model call and no latency.")

        print("\n--- what the subagent handed back ---")
        print(follow["result"][:1500])

        print("\n--- workspace ---")
        for path in sorted(Path(workspace).iterdir()):
            if not path.name.startswith("."):
                print(f"  {path.name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="sonnet", help="Claude model for the supervisor")
    asyncio.run(main(parser.parse_args().model))
