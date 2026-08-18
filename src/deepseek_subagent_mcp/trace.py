"""Append-only trace of what this server decided, so it can be measured later.

The child's own runtime already persists a rich durable log -- every tool call
with arguments, every hook invocation with its exit code and duration, token
usage per step. What it does not hold is this server's side of the story: which
tier answered an escalation, what facts that tier was shown, whether the
verification command passed, how much a distilled answer actually compressed.
All of that lived in memory and stderr, and died with the process.

Three questions in wiki/active-work.md need exactly that data:

  - the classifier's false-positive rate (which escalations were routine)
  - whether DSA_SUMMARY_TOKENS and DSA_CHARS_PER_TOKEN are calibrated
  - whether DSA_MAX_STEPS and DSA_TURN_TOKEN_BUDGET are near real usage

So each decision and each finished run appends one JSON object here.
`scripts/trace_report.py` reads them back.

Two rules, both inherited from elsewhere in this codebase and both load-bearing:
the trace never touches stdout, which carries JSON-RPC frames; and it records
what the supervisor was shown, which is structured facts, never the prose the
child wrote.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from .config import log

SCHEMA = 1


class Trace:
    """One JSONL file, appended under a lock. Failures are logged, never raised."""

    def __init__(self, path: Path | None):
        self.path = path
        self._lock = threading.Lock()
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                log.warning("trace disabled: cannot create %s", path.parent, exc_info=True)
                self.path = None

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def write(self, kind: str, **fields: Any) -> None:
        if self.path is None:
            return
        record = {"schema": SCHEMA, "ts": round(time.time(), 3), "kind": kind, **fields}
        try:
            line = json.dumps(record, default=str, ensure_ascii=False)
            with self._lock, open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except Exception:  # noqa: BLE001 - observability is never worth a delegation
            # Broad on purpose. This runs on the agent's worker thread, and an
            # exception escaping here kills that thread: the run whose trace
            # failed looks fine, and every later run on that agent hangs. A
            # trace is the least important thing happening in this process.
            log.warning("could not append to %s", self.path, exc_info=True)

    def verdict(
        self,
        *,
        agent_id: str | None,
        tool: str,
        action: str,
        tier: str,
        reason: str,
        facts: dict[str, Any],
        latency_ms: float,
    ) -> None:
        self.write(
            "verdict",
            agent_id=agent_id,
            tool=tool,
            action=action,
            tier=tier,
            escalated=tier != "policy",
            reason=reason,
            facts=facts,
            latency_ms=round(latency_ms, 1),
        )

    def run(self, run: Any, workspace: str, model: str) -> None:
        verification = run.verification_result
        distil: dict[str, Any] = {"distilled": run.distilled, "truncated": run.truncated}
        if run.distilled:
            distil["raw_chars"] = len(run.final_response)
        self.write(
            "run",
            run_id=run.run_id,
            agent_id=run.agent_id,
            model=model,
            workspace=workspace,
            state=run.state,
            finish_reason=run.finish_reason,
            elapsed_seconds=round((run.finished_at or 0) - (run.started_at or 0), 2),
            usage=run.usage.as_dict(),
            # Lengths, not text: the trace is for measuring, not for archiving
            # somebody's source code or a client's data.
            prompt_chars=len(run.prompt),
            result_chars=len(run.result_text),
            **distil,
            verification=verification.as_dict() if verification else None,
            error=run.error,
        )

    def calibration(self, *, run_id: str, chars: int, output_tokens: int, assumed: float) -> None:
        """The chars-per-token ratio a distillation turn actually produced."""
        self.write(
            "calibration",
            run_id=run_id,
            chars=chars,
            output_tokens=output_tokens,
            observed=round(chars / output_tokens, 3),
            assumed=assumed,
        )


def open_trace(raw: str | None, session_root: Path) -> Trace:
    """Resolve DSA_TRACE. Unset writes to the session root; `off` disables it."""
    if raw is not None and raw.strip().lower() in ("off", "0", "false", ""):
        return Trace(None)
    if raw:
        return Trace(Path(raw).expanduser().resolve())
    return Trace(session_root / "trace.jsonl")


