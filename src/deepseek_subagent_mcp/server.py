"""MCP server exposing DeepSeek Harness as a delegatable subagent."""

from __future__ import annotations

import sys
import time
from contextlib import asynccontextmanager
from typing import Any

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from . import __version__
from .config import Settings, configure_logging, log
from .runs import TERMINAL_STATES, Registry, RegistryError, Run
from .supervisor import Supervisor

SERVER_INSTRUCTIONS = """\
Delegate self-contained engineering work to a DeepSeek Harness agent running in
its own process. The child reads and edits files and runs shell commands in the
workspace you name, so give it a task with a clear definition of done.

Typical loop: dsh_delegate -> dsh_await -> (dsh_continue to iterate) ->
dsh_cancel when finished. Runs are asynchronous; dsh_await polls.

Every delegation needs a `verification` command -- the command that proves the
work is done, which this server runs itself after the child finishes. A run is
only reported `completed` when that command exits 0; otherwise it comes back
`completed_unverified` with the output.

The child's tool calls are gated by a policy classifier before they execute, and
anything the classifier cannot settle is escalated to you. It still works
unattended between escalations, so point it at a branch, a worktree, or a
scratch directory rather than anything you cannot afford to have edited.
"""

settings = Settings.from_env()
registry = Registry(settings)
supervisor = Supervisor(settings, registry, trace=registry.trace)


@asynccontextmanager
async def _lifespan(_app):
    """Run the approval socket for as long as the server is up."""
    async with anyio.create_task_group() as tg:
        await tg.start(supervisor.serve)
        try:
            yield {}
        finally:
            tg.cancel_scope.cancel()
            supervisor.cleanup()


app = MCPServer(
    "deepseek-subagent",
    version=__version__,
    instructions=SERVER_INSTRUCTIONS,
    lifespan=_lifespan,
)

# How often a wait reports progress back to the client while it blocks.
PROGRESS_INTERVAL = 3.0


async def _wait_for(run: Run, seconds: float, ctx: Context | None = None) -> None:
    """Block for up to `seconds`, reporting progress while we wait.

    run.done.wait is a blocking SDK-adjacent call, so it crosses to a thread.
    Chunking it is what makes progress reporting possible at all.
    """
    if seconds <= 0:
        return
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        finished = await anyio.to_thread.run_sync(
            run.done.wait, min(PROGRESS_INTERVAL, remaining)
        )
        if finished:
            return
        if ctx is not None:
            await _report(ctx, run, seconds, seconds - (deadline - time.monotonic()))


async def _report(ctx: Context, run: Run, total: float, elapsed: float) -> None:
    message = run.transcript[-1] if run.transcript else run.phase
    try:
        await ctx.report_progress(round(elapsed, 1), total=total, message=f"{run.phase}: {message}")
    except Exception:  # noqa: BLE001 - the client may not have asked for progress
        log.debug("progress report failed for %s", run.run_id, exc_info=True)


def _result(run: Run) -> dict[str, Any]:
    out = run.detail()
    if run.state not in TERMINAL_STATES:
        out["hint"] = (
            f"Still working. Call dsh_await(run_id='{run.run_id}') to keep waiting, "
            f"or dsh_transcript(run_id='{run.run_id}') to see what it is doing."
        )
    elif run.state == "completed":
        out["hint"] = (
            f"Done and verified. Call dsh_continue(agent_id='{run.agent_id}', ...) to "
            "iterate in the same session, or dsh_cancel to release the runtime."
        )
    elif run.state == "completed_unverified":
        out["hint"] = (
            "The child finished but the verification command did not pass. Read "
            f"`verification.output_tail`, then dsh_continue(agent_id='{run.agent_id}', ...) "
            "with what to fix."
        )
    return out


@app.tool()
async def dsh_delegate(
    task: str,
    verification: str,
    workspace: str | None = None,
    instructions: str | None = None,
    model: str | None = None,
    name: str | None = None,
    wait_seconds: float = 0,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Start a new DeepSeek Harness subagent on a task.

    Returns immediately with an agent_id and run_id unless wait_seconds is set.
    Each call creates a fresh agent with its own runtime process and session;
    use dsh_continue to give more work to an agent that already exists.

    Args:
        task: What to do, with a clear definition of done. The child cannot ask
            you clarifying questions, so state the acceptance criteria.
        verification: The shell command that proves the task is done, run by
            this server in the workspace after the child finishes — e.g.
            "pytest -q" or "npm test && npm run lint". Its exit code decides
            whether the run is reported completed or completed_unverified. Pass
            "true" if there is genuinely nothing to check.
        workspace: Directory the child reads and writes. Relative paths resolve
            against the server's configured workspace. Defaults to that workspace.
        instructions: Optional standing guidance prepended to the task, e.g.
            coding conventions or files to leave alone.
        model: DeepSeek model id. Defaults to the server's configured model.
        name: Human label for this agent, shown in dsh_list.
        wait_seconds: Block up to this long for the run to finish. 0 returns at once.
    """
    if ctx is not None:
        supervisor.bind(ctx.session)
    try:
        resolved = settings.resolve_workspace(workspace)
    except OSError as exc:
        raise RegistryError(f"cannot resolve workspace {workspace!r}: {exc}") from exc
    if not resolved.is_dir():
        raise RegistryError(f"workspace is not an existing directory: {resolved}")
    if not (verification or "").strip():
        raise RegistryError(
            "verification is required: give the command that proves the task is done, "
            'or "true" if there is genuinely nothing to check.'
        )

    agent = registry.create_agent(name=name, workspace=resolved, model=model or settings.model)
    start_error = await anyio.to_thread.run_sync(agent.wait_ready, 60.0)
    if start_error is not None:
        raise RegistryError(f"DeepSeek Harness runtime failed to start: {start_error}")
    prompt = f"{instructions.strip()}\n\n---\n\n{task}" if instructions else task
    run = agent.submit(prompt, verification=verification)
    await _wait_for(run, wait_seconds, ctx)
    out = _result(run)
    out["workspace"] = str(resolved)
    out["model"] = agent.model
    return out


@app.tool()
async def dsh_await(
    run_id: str,
    wait_seconds: float = 120,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Wait for a run to finish and return its result.

    Safe to call repeatedly. If the run is still going when wait_seconds
    elapses, this returns the current state rather than an error. Runs whose
    agent has since been reaped are still readable — their results are archived.

    Args:
        run_id: The run to wait on, from dsh_delegate or dsh_continue.
        wait_seconds: Maximum time to block. Use a longer value for big tasks.
    """
    run = registry.find_run(run_id)
    await _wait_for(run, wait_seconds, ctx)
    return _result(run)


@app.tool()
async def dsh_continue(
    agent_id: str,
    message: str,
    verification: str | None = None,
    wait_seconds: float = 0,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Send follow-up work to an existing subagent, in its original session.

    The child keeps the full context of its earlier turns, so refer to prior
    work directly ("the migration you just wrote"). Work is queued: if the agent
    is mid-run, this message runs after it.

    Args:
        agent_id: Agent to continue, from dsh_delegate or dsh_list.
        message: The follow-up instruction.
        verification: Command proving this follow-up is done. Omit to skip
            verification for this turn; the run then reports completed_unverified.
        wait_seconds: Block up to this long for the run to finish. 0 returns at once.
    """
    if ctx is not None:
        supervisor.bind(ctx.session)
    agent = registry.agent(agent_id)
    run = agent.submit(message, verification=verification)
    await _wait_for(run, wait_seconds, ctx)
    return _result(run)


@app.tool()
async def dsh_list() -> dict[str, Any]:
    """List every subagent this server owns, with its state, cost, and run history."""
    agents = [a.info() for a in registry.agents()]
    spent = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 0, "steps": 0}
    for info in agents:
        for key in spent:
            spent[key] += info["usage"][key]
    return {
        "agents": agents,
        "live": sum(1 for a in agents if a["state"] != "closed"),
        "limit": settings.max_agents,
        "default_model": settings.model,
        "default_workspace": str(settings.workspace),
        "tokens_spent": spent,
        "archived_runs": len(registry.archived_runs()),
        "trace": str(registry.trace.path) if registry.trace.enabled else None,
        "limits": {
            "run_timeout_seconds": settings.run_timeout,
            "idle_timeout_seconds": settings.idle_timeout,
            "max_steps": settings.max_steps,
            "turn_token_budget": settings.turn_token_budget,
            "result_cap_chars": settings.result_cap_chars,
        },
        "supervisor": {
            "tier": supervisor.tier,
            "sandbox_mode": settings.sandbox_mode,
            "recent_decisions": supervisor.decisions[-10:],
        },
    }


@app.tool()
async def dsh_cancel(agent_id: str) -> dict[str, Any]:
    """Stop a subagent and release its runtime process.

    The harness protocol has no mid-turn cancel, so this kills the child
    process. Any in-flight run is reported as cancelled, and file edits it
    already made stay on disk. Cancelling ends the session: its context cannot
    be resumed, so start a new agent rather than continuing this one.

    Args:
        agent_id: Agent to stop.
    """
    agent = registry.agent(agent_id)
    was_busy = agent.busy
    await anyio.to_thread.run_sync(agent.close)
    return {
        "agent_id": agent_id,
        "closed": True,
        "interrupted_running_work": was_busy,
        "usage": agent.usage().as_dict(),
        "runs": [r.summary() for r in agent.runs()],
    }


@app.tool()
async def dsh_transcript(run_id: str, limit: int = 60, raw: bool = False) -> dict[str, Any]:
    """Show what a subagent actually did during a run.

    Returns the tail of its activity log — tool calls, assistant messages, turn
    endings. Use this to check progress on a long run, or to understand a
    failure. Returns live data while the run is still going.

    Args:
        run_id: The run to inspect.
        limit: How many of the most recent activity lines to return.
        raw: Also return the child's full uncapped response. dsh_delegate
            returns a distilled version when the answer is large; this is where
            the original text lives.
    """
    run = registry.find_run(run_id)
    lines = list(run.transcript)
    tail = lines[-limit:] if limit > 0 else lines
    out = {
        "run_id": run_id,
        "agent_id": run.agent_id,
        "state": run.state,
        "phase": run.phase,
        "usage": run.usage.as_dict(),
        "activity_count": run.event_count,
        "showing": len(tail),
        "truncated": len(tail) < len(lines),
        "activity": tail,
    }
    if raw:
        out["raw_response"] = run.final_response
    return out


def main() -> None:
    configure_logging(settings.log_level)
    log.info(
        "starting: model=%s workspace=%s max_agents=%d sandbox=%s supervisor=%s",
        settings.model,
        settings.workspace,
        settings.max_agents,
        settings.sandbox_mode,
        settings.supervisor,
    )
    if registry.trace.enabled:
        log.info("tracing decisions and runs to %s", registry.trace.path)
    if not settings.api_key:
        print(
            "warning: DEEPSEEK_API_KEY is not set in this server's environment; "
            "the harness runtime will fail unless it inherits one.",
            file=sys.stderr,
        )
    try:
        app.run()
    finally:
        registry.shutdown()


if __name__ == "__main__":
    main()
