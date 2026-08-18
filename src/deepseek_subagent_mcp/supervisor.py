"""The middle manager: a verdict server the child's PreToolUse hook calls.

The child cannot reach the MCP client directly — the SDK wire carries three
methods and no approval message. Hooks, however, run as ordinary local
processes, so the hook connects to a unix socket here and blocks on a verdict.

Three tiers, in order of preference, chosen once at startup and logged:

  sampling      ctx.session.create_message -- the MCP client's model decides
  elicitation   ctx.session.elicit        -- the operator decides
  deterministic no escalation path        -- escalate becomes deny

Whatever the tier, `guard.classify` runs first and settles the clear cases, so
the dangerous ones never depend on a model and the routine ones cost nothing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import anyio
from mcp.types import ClientCapabilities, ElicitationCapability, SamplingCapability

from .config import Settings, log
from .guard import ALLOW, DENY, ESCALATE, Verdict, classify

TIER_SAMPLING = "sampling"
TIER_ELICITATION = "elicitation"
TIER_DETERMINISTIC = "deterministic"

SUPERVISOR_SYSTEM_PROMPT = """\
You are a security supervisor for a delegated coding agent. The agent runs \
unattended in a workspace directory and has proposed a tool call that an \
automated policy could not classify.

Answer with exactly one word on the first line, ALLOW or DENY, then one short \
sentence of reason on the second line.

Rules:
- Default to DENY when uncertain. A denied call costs the agent one retry; an \
allowed destructive call cannot be undone.
- DENY anything that deletes or overwrites outside the workspace, touches \
credentials or SSH keys, installs software system-wide, changes system \
configuration, or sends data to a network endpoint.
- ALLOW ordinary development work: running the workspace's own code and tests, \
reading files, inspecting version control, writing inside the workspace.
- You are shown structured facts only. There is no justification from the agent \
to weigh, and you should not infer intent beyond what the facts state.
"""


class Supervisor:
    """Owns the socket, the tier decision, and the escalation path."""

    def __init__(self, settings: Settings, registry: Any = None):
        self.settings = settings
        # The registry is how a verdict resolves the workspace it is judging
        # against. The hook reports one too, but that is a field the supervised
        # side supplies, and the security boundary should not read it.
        self.registry = registry
        self.socket_path = Path(settings.approval_socket)
        self._session: Any = None
        self.tier: str = TIER_DETERMINISTIC
        self.decisions: list[dict[str, Any]] = []

    # -- session binding ------------------------------------------------

    def bind(self, session: Any) -> None:
        """Capture the MCP session on the first tool call and pick a tier.

        Escalation happens on a worker thread's timeline, outside any tool
        call, so the session has to be captured rather than passed in.
        """
        if self._session is not None:
            return
        self._session = session
        self.tier = self._resolve_tier(session)
        log.info("supervisor tier = %s", self.tier)

    def _resolve_tier(self, session: Any) -> str:
        if self.settings.supervisor == "off":
            return TIER_DETERMINISTIC
        check = getattr(session, "check_client_capability", None)
        if check is None:
            return TIER_DETERMINISTIC
        try:
            if self.settings.supervisor in ("auto", "sampling") and check(
                ClientCapabilities(sampling=SamplingCapability())
            ):
                return TIER_SAMPLING
            if self.settings.supervisor in ("auto", "elicitation") and check(
                ClientCapabilities(elicitation=ElicitationCapability())
            ):
                return TIER_ELICITATION
        except Exception:  # noqa: BLE001 - a capability probe must never block startup
            log.warning("client capability probe failed; falling back to deterministic",
                        exc_info=True)
            return TIER_DETERMINISTIC
        return TIER_DETERMINISTIC

    # -- decisions ------------------------------------------------------

    def workspace_for(self, agent_id: str | None, fallback: str | None) -> Path:
        """The workspace a verdict is judged against, resolved server-side."""
        if agent_id and self.registry is not None:
            agent = self.registry.find_agent(agent_id)
            if agent is not None:
                return agent.workspace
            log.warning("verdict for unknown agent %r; using the server workspace", agent_id)
            return self.settings.workspace
        return Path(fallback) if fallback else self.settings.workspace

    async def decide(
        self,
        tool_name: str,
        tool_input: dict,
        workspace: Path,
        agent_id: str | None = None,
    ) -> Verdict:
        verdict = classify(tool_name, tool_input, workspace)
        if verdict.action != ESCALATE:
            self._record(verdict, tier="policy", tool=tool_name, agent_id=agent_id)
            return verdict
        if self.settings.supervisor == "allow-escalations":
            resolved = Verdict(ALLOW, f"escalation auto-allowed: {verdict.reason}", verdict.facts)
            self._record(resolved, tier="policy", tool=tool_name, agent_id=agent_id)
            return resolved
        resolved = await self._escalate(verdict, tool_name, workspace)
        self._record(resolved, tier=self.tier, tool=tool_name, agent_id=agent_id)
        return resolved

    async def _escalate(self, verdict: Verdict, tool_name: str, workspace: Path) -> Verdict:
        if self._session is None or self.tier == TIER_DETERMINISTIC:
            return Verdict(
                DENY,
                f"denied: {verdict.reason} (no supervisor available; escalation fails closed)",
                verdict.facts,
            )
        record = {
            "tool": tool_name,
            "workspace": str(workspace),
            "policy_reason": verdict.reason,
            "facts": verdict.facts,
        }
        try:
            with anyio.fail_after(self.settings.supervisor_timeout):
                if self.tier == TIER_SAMPLING:
                    return await self._ask_model(record, verdict)
                return await self._ask_human(record, verdict)
        except TimeoutError:
            return Verdict(DENY, f"denied: supervisor did not answer within "
                                 f"{self.settings.supervisor_timeout:g}s", verdict.facts)
        except Exception as exc:  # noqa: BLE001 - any failure fails closed
            return Verdict(DENY, f"denied: supervisor unavailable ({type(exc).__name__})",
                           verdict.facts)

    async def _ask_model(self, record: dict, verdict: Verdict) -> Verdict:
        from mcp.types import SamplingMessage, TextContent

        body = json.dumps(record, indent=2, default=str)
        result = await self._session.create_message(
            messages=[SamplingMessage(
                role="user",
                content=TextContent(type="text", text=f"Proposed tool call:\n{body}"),
            )],
            system_prompt=SUPERVISOR_SYSTEM_PROMPT,
            max_tokens=200,
            temperature=0,
        )
        text = getattr(result.content, "text", "") or ""
        head, _, tail = text.strip().partition("\n")
        decided = head.strip().upper()
        reason = tail.strip() or verdict.reason
        if decided.startswith("ALLOW"):
            return Verdict(ALLOW, f"supervisor allowed: {reason}", verdict.facts)
        return Verdict(DENY, f"supervisor denied: {reason}", verdict.facts)

    async def _ask_human(self, record: dict, verdict: Verdict) -> Verdict:
        body = json.dumps(record, indent=2, default=str)
        result = await self._session.elicit(
            message=(
                "A delegated DeepSeek Harness agent proposed a tool call that policy "
                f"could not classify ({verdict.reason}). Allow it?\n\n{body}"
            ),
            requested_schema={
                "type": "object",
                "properties": {"allow": {"type": "boolean",
                                         "description": "Allow this tool call"}},
                "required": ["allow"],
            },
        )
        if getattr(result, "action", None) == "accept" and (result.content or {}).get("allow"):
            return Verdict(ALLOW, "operator allowed", verdict.facts)
        return Verdict(DENY, "operator denied", verdict.facts)

    def _record(
        self, verdict: Verdict, tier: str, tool: str = "", agent_id: str | None = None
    ) -> None:
        self.decisions.append({
            "action": verdict.action,
            "reason": verdict.reason,
            "tier": tier,
            "tool": tool,
            "agent_id": agent_id,
        })
        del self.decisions[:-200]
        log.info("verdict %s [%s] agent=%s tool=%s: %s",
                 verdict.action, tier, agent_id or "-", tool or "-", verdict.reason)

    # -- socket ---------------------------------------------------------

    async def serve(self, *, task_status=anyio.TASK_STATUS_IGNORED) -> None:
        if self.socket_path.exists():
            self.socket_path.unlink()
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        listener = await anyio.create_unix_listener(self.socket_path)
        os.chmod(self.socket_path, 0o600)
        task_status.started()
        async with listener:
            await listener.serve(self._handle)

    def cleanup(self) -> None:
        """Remove the socket file. The listener is closed by its task group."""
        try:
            self.socket_path.unlink(missing_ok=True)
        except OSError:
            log.warning("could not remove %s", self.socket_path, exc_info=True)

    async def _handle(self, stream) -> None:
        async with stream:
            try:
                raw = await _read_line(stream)
                request = json.loads(raw)
                agent_id = request.get("agent_id") or None
                workspace = self.workspace_for(agent_id, request.get("workspace"))
                verdict = await self.decide(
                    request.get("tool_name") or "",
                    request.get("tool_input") or {},
                    workspace,
                    agent_id,
                )
                reply = {"action": verdict.action, "reason": verdict.reason}
            except Exception as exc:  # noqa: BLE001 - a broken request denies
                log.warning("supervisor request failed", exc_info=True)
                reply = {"action": DENY, "reason": f"supervisor error: {type(exc).__name__}"}
            await stream.send((json.dumps(reply) + "\n").encode())


async def _read_line(stream, limit: int = 1_048_576) -> str:
    buffer = b""
    while b"\n" not in buffer:
        chunk = await stream.receive(65536)
        if not chunk:
            break
        buffer += chunk
        if len(buffer) > limit:
            raise ValueError("request too large")
    return buffer.split(b"\n", 1)[0].decode()
