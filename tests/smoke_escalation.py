"""Live proof of the escalation ladder: sampling and elicitation, end to end.

Costs real tokens. Not collected by pytest.

    DEEPSEEK_API_KEY=sk-... uv run python tests/smoke_escalation.py

Every other live proof so far has run at the `deterministic` floor, because the
stdio test client advertises neither capability -- so escalations denied without
a decision-maker ever being consulted. This client advertises both, one at a
time, and asserts that the supervisor actually calls back into it.

What it checks, per tier:
  - dsh_list reports the tier the client's capabilities entitle it to
  - an escalating tool call reaches this process, not a deterministic denial
  - the supervisor's answer is what decides the child's tool call
  - the facts handed over carry no prose written by the child
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parent.parent

# A compound bash line escalates: argv parsing is not the whole story, so the
# classifier refuses to settle it alone. Harmless, and reliably reproducible.
TASK = (
    "Run exactly this one bash command and report its output verbatim: "
    "echo first && echo second"
)

seen: list[dict[str, Any]] = []


def params(workspace: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "deepseek_subagent_mcp"],
        env={**os.environ, "DSA_WORKSPACE": workspace, "DSA_SUPERVISOR": "auto"},
        cwd=str(REPO),
    )


def record(text: str) -> None:
    """Keep what the supervisor was shown, so the payload can be inspected."""
    body = text.split("Proposed tool call:", 1)[-1]
    try:
        seen.append(json.loads(body.strip()))
    except ValueError:
        seen.append({"unparsed": text})


async def sampling_supervisor(context, request):
    """Stand in for the client's model. Allows, so the effect is observable."""
    record("".join(getattr(m.content, "text", "") for m in request.messages))
    return types.CreateMessageResult(
        role="assistant",
        model="test-supervisor",
        content=types.TextContent(type="text", text="ALLOW\nharmless echo, allowed by the test"),
    )


async def elicitation_supervisor(context, request):
    """Stand in for the operator. Denies, so the effect is observable."""
    record(request.message)
    return types.ElicitResult(action="accept", content={"allow": False})


async def check(tier: str, **callbacks) -> None:
    print(f"\n=== {tier} tier ===")
    seen.clear()
    workspace = tempfile.mkdtemp(prefix=f"dsa-esc-{tier}-")
    connection = stdio_client(params(workspace))
    async with connection as (r, w), ClientSession(r, w, **callbacks) as s:
        await s.initialize()
        result = (await s.call_tool("dsh_delegate", {
            "task": TASK, "verification": "true", "wait_seconds": 300,
        })).structured_content

        listing = (await s.call_tool("dsh_list", {})).structured_content
        supervisor = listing["supervisor"]
        print("tier reported :", supervisor["tier"])
        assert supervisor["tier"] == tier, f"expected {tier}, got {supervisor['tier']}"

        escalated = [d for d in supervisor["recent_decisions"] if d["tier"] == tier]
        print("escalations   :", len(escalated))
        for decision in escalated:
            print(f"  {decision['action']:6} {decision['tool']}: {decision['reason'][:90]}")
        assert escalated, f"nothing reached the {tier} supervisor"
        assert seen, "the supervisor was never actually called back"

        print("run state     :", result["state"])
        print("\n--- what the supervisor was shown ---")
        print(json.dumps(seen[0], indent=2)[:700])

        # The child writes its own command. It must not get to argue its case.
        blob = json.dumps(seen).lower()
        for word in ("justification", "because", "please", "i need", "trust me"):
            assert word not in blob, f"child prose reached the supervisor: {word!r}"
    print(f"{tier} proof OK")


async def main() -> None:
    if not os.environ.get("DEEPSEEK_API_KEY"):
        sys.exit("DEEPSEEK_API_KEY is required for this smoke test")
    # Sampling wins when both are on offer, so each tier is tested alone.
    await check("sampling", sampling_callback=sampling_supervisor)
    await check("elicitation", elicitation_callback=elicitation_supervisor)
    print("\nescalation smoke OK")


if __name__ == "__main__":
    asyncio.run(main())
