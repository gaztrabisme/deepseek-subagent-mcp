"""Agent and run registry.

One delegated subagent == one ``DeepSeekHarness`` instance == one runtime
subprocess, owned by one worker thread and holding one persisted session.
Continuing an agent re-enters the same session, so the child keeps its context.

The SDK is synchronous and its wire has no mid-turn cancel, so the threading
model here is deliberate: work is queued to a per-agent thread, and cancellation
is implemented by closing the harness (killing the subprocess), which makes the
in-flight ``session.run`` raise ``TransportClosedError``.

One submitted unit of work is a pipeline, not a single turn: the task turn, then
the caller's verification command, then a distillation turn if the child's answer
is too big to cross the MCP boundary. ``run.done`` fires when all of it is over.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import threading
import time
import uuid
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig, Notification
from deepseek_harness.errors import TransportClosedError

from .config import Settings, log
from .verify import VerificationResult, run_verification

# Run states, in the MCP Tasks extension's vocabulary so a later migration to it
# is mechanical. The extension is typed in mcp 2.0.0 but has no server-side
# implementation, so only the names are adopted, not the machinery.
WORKING = "working"
COMPLETED = "completed"
COMPLETED_UNVERIFIED = "completed_unverified"
FAILED = "failed"
CANCELLED = "cancelled"

TERMINAL_STATES = frozenset({COMPLETED, COMPLETED_UNVERIFIED, FAILED, CANCELLED})

# What a working run is actually doing. Tasks' vocabulary has one live state and
# would lose this, so it travels alongside rather than inside `state`.
PHASE_QUEUED = "queued"
PHASE_RUNNING = "running"
PHASE_VERIFYING = "verifying"
PHASE_DISTILLING = "distilling"
PHASE_DONE = "done"

# finish_reason values that mean the turn did not complete cleanly.
UNHAPPY_FINISH = frozenset({"error", "max-tokens"})

# Streaming deltas and per-request bookkeeping. A one-word answer emits dozens
# of these, which would push the useful lines out of a bounded transcript.
NOISY_EVENTS = frozenset({"request/context", "request/header"})

# Why an agent's runtime was killed. The wire has no mid-turn cancel, so every
# stop is a process kill; the reason is what distinguishes them afterwards.
KILL_CANCEL = "cancel"
KILL_TIMEOUT = "timeout"
KILL_LOOP = "loop"
KILL_BUDGET = "budget"
KILL_STEPS = "steps"
KILL_SHUTDOWN = "shutdown"
KILL_IDLE = "idle"

# A kill for these reasons is a failure, not a caller-requested cancellation,
# and it outranks whatever the run's own result said. Ported from
# Work/harness crates/agent/src/worker.rs::outcome -- a killed process's output
# is never trusted as success.
KILL_IS_FAILURE = frozenset({KILL_TIMEOUT, KILL_LOOP, KILL_BUDGET, KILL_STEPS})

# The handoff contract, convergent across four independent codebases in
# Work/harness/research/25-context-engineering.md 4.1. Framed anti-continuation
# because a model handed its own transcript will otherwise keep working.
DISTIL_PROMPT = """\
Stop working on the task. Summarize what you did, for a different engineer who \
has none of your context and will not see this conversation.

Write exactly these seven sections, each as a markdown heading:

## Goal
## Constraints & Preferences
## Progress
State Done, In-Progress and Blocked separately.
## Key Decisions
## Next Steps
## Relevant Files
## Critical Context

Rules:
- Preserve exact file paths, function names, error messages and command output. \
Do not paraphrase an identifier or a path.
- State what you changed in the repository, file by file.
- Preserve any question you could not answer, verbatim.
- Do not continue the task, propose new work, or take any tool call. Summarize only.
- Stay under {limit} characters."""


class RegistryError(RuntimeError):
    """A caller-visible problem: unknown id, capacity, or a closed agent."""


def _now() -> float:
    return time.time()


@dataclass
class Usage:
    """Provider-reported token accounting, summed across a run's model calls.

    Summing per-step input is deliberate and correct for a cost ceiling: every
    request bills the whole resent prefix, so the total is what the delegation
    actually costs, not what its final context holds.
    """

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0
    steps: int = 0

    @property
    def total(self) -> int:
        # Mirrors upstream usageTokens (packages/llm/token-meter/src/index.ts:44):
        # reasoning tokens are already inside output and must not be added again.
        return self.input + self.cache_read + self.cache_write + self.output

    def add(self, usage: dict[str, Any]) -> None:
        self.input += int(usage.get("inputTokens") or 0)
        self.output += int(usage.get("outputTokens") or 0)
        self.cache_read += int(usage.get("cacheReadTokens") or 0)
        self.cache_write += int(usage.get("cacheWriteTokens") or 0)
        self.reasoning += int(usage.get("reasoningTokens") or 0)

    def as_dict(self) -> dict[str, int]:
        return {
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "reasoning": self.reasoning,
            "total": self.total,
            "steps": self.steps,
        }

    def merge(self, other: Usage) -> None:
        self.input += other.input
        self.output += other.output
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write
        self.reasoning += other.reasoning
        self.steps += other.steps


def _event(notification: Notification) -> dict[str, Any] | None:
    if notification.method != "session.event":
        return None
    event = notification.payload.get("event")
    return event if isinstance(event, dict) else None


def summarize_notification(notification: Notification) -> str | None:
    """One readable line per notification, or None to drop it from the transcript."""
    payload = notification.payload
    if notification.method == "session.status":
        status = payload.get("status")
        return f"status: {status}" if status else None
    event = _event(notification)
    if event is None:
        return None
    kind = event.get("type")
    if not isinstance(kind, str):
        return None
    if kind in NOISY_EVENTS or kind.endswith("/chunk"):
        return None
    data = event.get("data") if isinstance(event.get("data"), dict) else {}

    if kind == "assistant/message":
        # A message with no text is a tool-call carrier; the tool/call line that
        # follows says the same thing with the tool name attached.
        text = _message_text(data)
        return f"assistant: {_clip(text, 240)}" if text else None
    if kind == "turn/end":
        reason = data.get("reason") if isinstance(data.get("reason"), dict) else {}
        line = f"turn/end: {reason.get('kind', 'unknown')}"
        error = reason.get("error") if isinstance(reason.get("error"), dict) else None
        code = error.get("code") if error else None
        return f"{line} ({code})" if code else line
    if kind.startswith("tool/"):
        name = data.get("name") or data.get("toolName") or data.get("tool")
        return f"{kind}: {name}" if name else kind
    return kind


def turn_error_detail(notification: Notification) -> str | None:
    """Pull the runtime's own error message out of a failing ``turn/end``.

    Without this the caller only sees ``finish_reason='error'``, which hides
    the actionable part (a missing API key, a bad model id).
    """
    event = _event(notification)
    if event is None or event.get("type") != "turn/end":
        return None
    data = event.get("data")
    reason = data.get("reason") if isinstance(data, dict) else None
    error = reason.get("error") if isinstance(reason, dict) else None
    if not isinstance(error, dict):
        return None
    message = str(error.get("message") or "").strip()
    code = error.get("code")
    if not message:
        return str(code) if code else None
    return f"{code}: {message}" if code else message


def usage_report(notification: Notification) -> dict[str, Any] | None:
    """The token accounting an ``assistant/message`` carries, if the adapter reported any.

    This is the only token accounting on the wire: RunResult has none, and there
    is no separate usage event (packages/core/session/src/types.ts:271).
    """
    event = _event(notification)
    if event is None or event.get("type") != "assistant/message":
        return None
    data = event.get("data")
    usage = data.get("usage") if isinstance(data, dict) else None
    return usage if isinstance(usage, dict) else None


def is_step_start(notification: Notification) -> bool:
    """A step is one model call plus the tool executions it requested."""
    event = _event(notification)
    return event is not None and event.get("type") == "step/start"


def tool_call_signature(notification: Notification) -> str | None:
    """Stable fingerprint of one tool call: name plus a hash of its arguments.

    An identical signature repeating is the cheapest reliable runaway-loop
    signal there is -- string comparison, no model involvement. The child also
    runs dsh-repeat-tool-reminder, which nudges; this is the outer backstop
    that actually stops the run.
    """
    event = _event(notification)
    if event is None or event.get("type") != "tool/call":
        return None
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    name = data.get("name") or data.get("toolName") or data.get("tool") or "?"
    # `arguments` is a raw JSON string on the wire, not an object; hashing it
    # verbatim is exactly the fingerprint we want either way.
    args = data.get("arguments", data.get("args"))
    try:
        blob = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        blob = repr(args)
    return f"{name}:{hashlib.sha256(blob.encode()).hexdigest()[:16]}"


def _message_text(data: dict[str, Any]) -> str:
    message = data.get("message")
    owner = message if isinstance(message, dict) else data
    content = owner.get("content")
    if not isinstance(content, list):
        return ""
    parts = [
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "".join(parts).strip()


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class Run:
    run_id: str
    agent_id: str
    prompt: str
    verification: str | None = None
    state: str = WORKING
    phase: str = PHASE_QUEUED
    created_at: float = field(default_factory=_now)
    started_at: float | None = None
    finished_at: float | None = None
    # The child's answer, verbatim. Reachable through dsh_transcript(raw=True).
    final_response: str = ""
    # What crosses the MCP boundary: distilled, capped, or the raw answer when
    # it already fits.
    result_text: str = ""
    distilled: bool = False
    truncated: bool = False
    finish_reason: str | None = None
    error: str | None = None
    error_detail: str | None = None
    event_count: int = 0
    transcript: deque[str] = field(default_factory=lambda: deque(maxlen=400))
    done: threading.Event = field(default_factory=threading.Event)
    signatures: Counter[str] = field(default_factory=Counter)
    usage: Usage = field(default_factory=Usage)
    verification_result: VerificationResult | None = None
    # (kill kind, human reason) set by a ceiling the run crossed. The reaper
    # reads it, kills the runtime, and the worker's finally block makes the
    # kill outrank whatever the run itself reported.
    trip: tuple[str, str] | None = None
    deadline: float | None = None

    @property
    def loop_tripped(self) -> str | None:
        return self.trip[1] if self.trip and self.trip[0] == KILL_LOOP else None

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "state": self.state,
            "phase": self.phase,
            "task": _clip(self.prompt, 120),
            "finish_reason": self.finish_reason,
            "elapsed_seconds": round(
                (self.finished_at or _now()) - (self.started_at or self.created_at), 1
            ),
            "activity_count": self.event_count,
            "usage": self.usage.as_dict(),
            "last_activity": self.transcript[-1] if self.transcript else None,
        }

    def detail(self) -> dict[str, Any]:
        out = self.summary()
        out["result"] = self.result_text
        if self.distilled:
            out["distilled"] = True
            out["raw_response_chars"] = len(self.final_response)
        if self.truncated:
            out["truncated"] = True
        if self.verification_result is not None:
            out["verification"] = self.verification_result.as_dict()
        if self.error:
            out["error"] = self.error
        return out


class Agent:
    """A live DeepSeek Harness runtime, driven by a serial task queue."""

    def __init__(self, agent_id: str, name: str, workspace: Path, model: str, settings: Settings):
        self.agent_id = agent_id
        self.name = name
        self.workspace = workspace
        self.model = model
        self.settings = settings
        self.session_id = f"dsa-{agent_id}"
        self.created_at = _now()

        self._runs: dict[str, Run] = {}
        self._order: list[str] = []
        self._queue: deque[Run] = deque()
        self._wake = threading.Condition()
        self._closing = False
        self._closed = False
        self._kill_kind: str | None = None
        self.last_activity = _now()
        self._ready = threading.Event()
        self._start_error: str | None = None
        self._harness: DeepSeekHarness | None = None
        self._thread = threading.Thread(
            target=self._worker, name=f"dsh-agent-{agent_id}", daemon=True
        )
        self._thread.start()

    # -- public API -----------------------------------------------------

    def wait_ready(self, timeout: float = 60.0) -> str | None:
        """Block until the runtime has booted. Returns the startup error, or None."""
        if not self._ready.wait(timeout):
            return f"runtime did not start within {timeout:g}s"
        return self._start_error

    def submit(self, prompt: str, verification: str | None = None) -> Run:
        run = Run(
            run_id=f"run-{uuid.uuid4().hex[:12]}",
            agent_id=self.agent_id,
            prompt=prompt,
            verification=verification,
            transcript=deque(maxlen=self.settings.transcript_limit),
        )
        with self._wake:
            if self._start_error is not None:
                raise RegistryError(
                    f"agent {self.agent_id} failed to start: {self._start_error}"
                )
            if self._closing or self._closed:
                raise RegistryError(f"agent {self.agent_id} is closed")
            self._runs[run.run_id] = run
            self._order.append(run.run_id)
            self._queue.append(run)
            self._wake.notify_all()
        return run

    def get_run(self, run_id: str) -> Run | None:
        with self._wake:
            return self._runs.get(run_id)

    def runs(self) -> list[Run]:
        with self._wake:
            return [self._runs[rid] for rid in self._order]

    @property
    def busy(self) -> bool:
        with self._wake:
            return any(r.state == WORKING for r in self._runs.values())

    @property
    def closed(self) -> bool:
        return self._closed

    def usage(self) -> Usage:
        total = Usage()
        for run in self.runs():
            total.merge(run.usage)
        return total

    def info(self) -> dict[str, Any]:
        runs = self.runs()
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "model": self.model,
            "workspace": str(self.workspace),
            "session_id": self.session_id,
            "state": "closed" if self._closed else ("busy" if self.busy else "idle"),
            "age_seconds": round(_now() - self.created_at, 1),
            "usage": self.usage().as_dict(),
            "runs": [r.summary() for r in runs],
        }

    def close(self, reason: str = "cancelled by caller", kind: str = KILL_CANCEL) -> None:
        """Kill the runtime. The only cancel the SDK wire supports."""
        with self._wake:
            if self._closing:
                return
            self._closing = True
            self._kill_kind = kind
            pending = list(self._queue)
            self._queue.clear()
            self._wake.notify_all()
        log.info("agent %s closing (%s): %s", self.agent_id, kind, reason)
        for run in pending:
            run.state = CANCELLED
            run.phase = PHASE_DONE
            run.error = reason
            run.finished_at = _now()
            run.done.set()
        harness = self._harness
        if harness is not None:
            try:
                harness.close()
            except Exception:  # noqa: BLE001 - teardown must not raise at callers
                log.warning("agent %s: harness.close failed", self.agent_id, exc_info=True)
        self._thread.join(timeout=15)
        if self._thread.is_alive():
            log.warning("agent %s: worker thread did not exit within 15s", self.agent_id)
        self._closed = True

    # -- worker ---------------------------------------------------------

    def _worker(self) -> None:
        try:
            self._harness = DeepSeekHarness(
                DeepSeekHarnessConfig(
                    provider=self.settings.provider,
                    model=self.model,
                    max_tokens=self.settings.max_tokens,
                    cwd=str(self.workspace),
                    session_root=str(self.settings.session_root),
                    cordis=self.settings.cordis,
                    api_key=self.settings.api_key,
                    base_url=self.settings.base_url,
                    env={
                        **self.settings.child_env(),
                        "DSA_HOOKS_CONFIG": str(
                            self.settings.hooks_config(self.agent_id)
                        ),
                    },
                    request_timeout_seconds=self.settings.request_timeout_seconds,
                )
            )
            session = self._harness.start_session(self.session_id)
        except Exception as exc:  # noqa: BLE001 - reported through every queued run
            self._start_error = f"{type(exc).__name__}: {exc}"
            log.error("agent %s failed to start: %s", self.agent_id, self._start_error)
            self._ready.set()
            self._fail_all_pending(self._start_error)
            self._closed = True
            return
        self._ready.set()
        log.info(
            "agent %s ready: model=%s workspace=%s", self.agent_id, self.model, self.workspace
        )

        while True:
            with self._wake:
                while not self._queue and not self._closing:
                    self._wake.wait()
                if self._closing:
                    break
                run = self._queue.popleft()
                run.state = WORKING
                run.phase = PHASE_RUNNING
                run.started_at = _now()
                run.deadline = run.started_at + self.settings.run_timeout
                self.last_activity = run.started_at

            collect = self._collector(run)
            try:
                result = session.run(run.prompt, on_notification=collect)
            except TransportClosedError as exc:
                kind = self._kill_kind
                if kind in KILL_IS_FAILURE:
                    run.state = FAILED
                    run.error = self._kill_message(kind, run)
                elif self._closing:
                    run.state = CANCELLED
                    run.error = self._kill_message(kind, run)
                else:
                    run.state = FAILED
                    run.error = f"runtime closed unexpectedly: {exc}"
            except Exception as exc:  # noqa: BLE001 - surfaced as a failed run
                log.warning("agent %s run %s raised", self.agent_id, run.run_id, exc_info=True)
                run.state = FAILED
                run.error = f"{type(exc).__name__}: {exc}"
            else:
                run.final_response = result.final_response
                run.result_text = result.final_response
                run.finish_reason = result.finish_reason
                if result.finish_reason in UNHAPPY_FINISH:
                    run.state = FAILED
                    run.error = (
                        run.error_detail
                        or f"turn ended with finish_reason={result.finish_reason!r}"
                    )
                else:
                    self._finish_pipeline(run, session, collect)
            finally:
                # A killed process's output is never read as success.
                if self._kill_kind in KILL_IS_FAILURE:
                    run.state = FAILED
                    run.error = self._kill_message(self._kill_kind, run)
                run.phase = PHASE_DONE
                run.finished_at = _now()
                self.last_activity = run.finished_at
                run.done.set()
                log.info(
                    "run %s %s in %.1fs, %d tokens",
                    run.run_id,
                    run.state,
                    run.finished_at - (run.started_at or run.finished_at),
                    run.usage.total,
                )

        self._fail_all_pending("agent closed")

    def _collector(self, run: Run):
        """Notification sink: transcript, error detail, usage, and the ceilings."""

        def collect(notification: Notification) -> None:
            line = summarize_notification(notification)
            if line is not None:
                run.transcript.append(line)
                run.event_count += 1
            detail = turn_error_detail(notification)
            if detail is not None:
                run.error_detail = detail
            usage = usage_report(notification)
            if usage is not None:
                run.usage.add(usage)
                budget = self.settings.turn_token_budget
                if budget and run.usage.total > budget and run.trip is None:
                    run.trip = (
                        KILL_BUDGET,
                        f"run used {run.usage.total} tokens, over the "
                        f"DSA_TURN_TOKEN_BUDGET of {budget}",
                    )
            if is_step_start(notification):
                run.usage.steps += 1
                if run.usage.steps > self.settings.max_steps and run.trip is None:
                    run.trip = (
                        KILL_STEPS,
                        f"run reached {run.usage.steps} steps, over the "
                        f"DSA_MAX_STEPS ceiling of {self.settings.max_steps}",
                    )
            signature = tool_call_signature(notification)
            if signature is not None:
                run.signatures[signature] += 1
                seen = run.signatures[signature]
                if seen >= self.settings.loop_strikes and run.trip is None:
                    run.trip = (
                        KILL_LOOP,
                        f"the same tool call repeated {seen} times "
                        f"({signature.split(':')[0]}) with identical arguments",
                    )
            self.last_activity = _now()

        return collect

    def _finish_pipeline(self, run: Run, session: Any, collect) -> None:
        """Verify the caller's acceptance command, then distil an oversized answer."""
        if run.verification:
            run.phase = PHASE_VERIFYING
            run.verification_result = run_verification(
                run.verification,
                self.workspace,
                self.settings,
                budget=(run.deadline - _now()) if run.deadline else None,
            )
            log.info(
                "run %s verification %s: %s",
                run.run_id,
                "passed" if run.verification_result.passed else "failed",
                run.verification_result.reason,
            )
        else:
            # No command means no evidence. A turn that ended cleanly is not the
            # same as a task that is right, and saying so is the whole point.
            run.verification_result = VerificationResult(
                "", False, "no verification command was given for this turn"
            )
        verified = run.verification_result.passed

        cap = self.settings.result_cap_chars
        if len(run.final_response) > cap:
            run.phase = PHASE_DISTILLING
            self._distil(run, session, collect, cap)

        run.state = COMPLETED if verified else COMPLETED_UNVERIFIED
        if not verified and run.verification:
            run.error = f"verification failed: {run.verification_result.reason}"

    def _distil(self, run: Run, session: Any, collect, cap: int) -> None:
        """One further turn in the same session, asking for the handoff contract."""
        before = run.usage.output
        try:
            result = session.run(DISTIL_PROMPT.format(limit=cap), on_notification=collect)
        except Exception:  # noqa: BLE001 - a failed distillation falls back to truncation
            log.warning("run %s distillation failed", run.run_id, exc_info=True)
            run.result_text = _truncate(run.final_response, cap)
            run.truncated = True
            return
        text = (result.final_response or "").strip()
        if not text:
            run.result_text = _truncate(run.final_response, cap)
            run.truncated = True
            return
        run.distilled = True
        produced = run.usage.output - before
        if produced > 0:
            # The character cap is derived from DSA_SUMMARY_TOKENS via an assumed
            # chars-per-token ratio. This is the ratio the provider actually
            # reported, logged so the assumption can be replaced by a measurement.
            log.info(
                "run %s distillation: %d chars / %d output tokens = %.2f chars per token "
                "(DSA_CHARS_PER_TOKEN is %.2f)",
                run.run_id,
                len(text),
                produced,
                len(text) / produced,
                self.settings.chars_per_token,
            )
        if len(text) > cap:
            text = _truncate(text, cap)
            run.truncated = True
        run.result_text = text

    def _kill_message(self, kind: str | None, run: Run) -> str:
        if kind == KILL_TIMEOUT:
            return (
                f"run exceeded DSA_RUN_TIMEOUT ({self.settings.run_timeout:g}s); "
                "the runtime was killed"
            )
        if kind in (KILL_LOOP, KILL_BUDGET, KILL_STEPS):
            label = {
                KILL_LOOP: "runaway loop detected",
                KILL_BUDGET: "token budget exceeded",
                KILL_STEPS: "step ceiling exceeded",
            }[kind]
            reason = run.trip[1] if run.trip else kind
            return f"{label}: {reason}; the runtime was killed"
        if kind == KILL_IDLE:
            return f"agent reaped after DSA_IDLE_TIMEOUT ({self.settings.idle_timeout:g}s) idle"
        if kind == KILL_SHUTDOWN:
            return "server shutting down"
        return "cancelled by caller"

    def _fail_all_pending(self, reason: str) -> None:
        with self._wake:
            pending = [r for r in self._runs.values() if r.state == WORKING]
            self._queue.clear()
        for run in pending:
            run.state = CANCELLED
            run.phase = PHASE_DONE
            run.error = reason
            run.finished_at = _now()
            run.done.set()


def _truncate(text: str, cap: int) -> str:
    marker = "\n\n[truncated; call dsh_transcript(run_id, raw=True) for the full response]"
    keep = max(0, cap - len(marker))
    return text[:keep] + marker


class Registry:
    """Owns every live agent for the lifetime of the MCP server process.

    A background reaper enforces the ceilings the wire cannot: a run deadline,
    an idle-agent timeout, and the token/step/loop trips. Without it, agents
    accumulate against DSA_MAX_AGENTS and every later delegation fails until the
    server restarts.

    Evicted agents leave their finished runs behind in a bounded archive, so a
    caller who delegates, goes away, and comes back can still read the result.
    """

    def __init__(self, settings: Settings, start_reaper: bool = True):
        self.settings = settings
        self._agents: dict[str, Agent] = {}
        self._archive: OrderedDict[str, Run] = OrderedDict()
        self._lock = threading.Lock()
        self._counter = itertools.count(1)
        self._stop = threading.Event()
        self._reaper: threading.Thread | None = None
        if start_reaper:
            interval = max(1.0, min(30.0, settings.idle_timeout / 4))
            self._reaper = threading.Thread(
                target=self._reap_loop, args=(interval,), name="dsh-reaper", daemon=True
            )
            self._reaper.start()

    def reap_once(self) -> list[tuple[str, str]]:
        """One reaper pass. Returns the (agent_id, reason) pairs it acted on."""
        now = _now()
        acted: list[tuple[str, str]] = []
        for agent in self.agents():
            if agent.closed:
                continue
            active = [r for r in agent.runs() if r.state == WORKING and r.started_at]
            tripped = next((r for r in active if r.trip), None)
            overdue = next((r for r in active if r.deadline and now > r.deadline), None)
            if tripped is not None and tripped.trip is not None:
                kind, reason = tripped.trip
                agent.close(reason, kind=kind)
                acted.append((agent.agent_id, kind))
            elif overdue is not None:
                agent.close("run deadline exceeded", kind=KILL_TIMEOUT)
                acted.append((agent.agent_id, KILL_TIMEOUT))
            elif not active and not agent.busy:
                if now - agent.last_activity > self.settings.idle_timeout:
                    agent.close("idle", kind=KILL_IDLE)
                    acted.append((agent.agent_id, KILL_IDLE))
        self._evict_closed()
        return acted

    def _evict_closed(self) -> None:
        """Drop closed agents, keeping their finished runs readable."""
        with self._lock:
            # An agent whose worker is still in flight keeps its slot for now.
            # close() joins the thread with a timeout, so a run can outlast the
            # kill -- evicting it here would lose a run that is still going.
            closed = [
                a for a in self._agents.values()
                if a.closed and all(r.state in TERMINAL_STATES for r in a.runs())
            ]
            for agent in closed:
                for run in agent.runs():
                    self._archive[run.run_id] = run
                self._agents.pop(agent.agent_id, None)
            while len(self._archive) > self.settings.run_archive:
                self._archive.popitem(last=False)
        for agent in closed:
            log.info("agent %s evicted; %d runs archived", agent.agent_id, len(agent.runs()))

    def _reap_loop(self, interval: float) -> None:
        while not self._stop.wait(interval):
            try:
                self.reap_once()
            except Exception:  # noqa: BLE001 - a reaper must never die
                log.warning("reaper pass failed", exc_info=True)

    def create_agent(self, name: str | None, workspace: Path, model: str) -> Agent:
        with self._lock:
            live = [a for a in self._agents.values() if not a.closed]
            if len(live) >= self.settings.max_agents:
                raise RegistryError(
                    f"agent limit reached ({self.settings.max_agents} live). "
                    "Cancel one with dsh_cancel, or raise DSA_MAX_AGENTS."
                )
            agent_id = f"a{next(self._counter)}"
            agent = Agent(
                agent_id=agent_id,
                name=name or f"subagent-{agent_id}",
                workspace=workspace,
                model=model,
                settings=self.settings,
            )
            self._agents[agent_id] = agent
            return agent

    def agent(self, agent_id: str) -> Agent:
        with self._lock:
            agent = self._agents.get(agent_id)
        if agent is None:
            raise RegistryError(f"unknown agent_id {agent_id!r}")
        return agent

    def find_agent(self, agent_id: str) -> Agent | None:
        with self._lock:
            return self._agents.get(agent_id)

    def find_run(self, run_id: str) -> Run:
        """A run by id, live or archived. Archived runs are finished and read-only."""
        with self._lock:
            agents = list(self._agents.values())
            archived = self._archive.get(run_id)
        for agent in agents:
            run = agent.get_run(run_id)
            if run is not None:
                return run
        if archived is not None:
            return archived
        raise RegistryError(f"unknown run_id {run_id!r}")

    def agents(self) -> list[Agent]:
        with self._lock:
            return list(self._agents.values())

    def archived_runs(self) -> list[Run]:
        with self._lock:
            return list(self._archive.values())

    def shutdown(self) -> None:
        self._stop.set()
        for agent in self.agents():
            if not agent.closed:
                agent.close("server shutting down", kind=KILL_SHUTDOWN)
