"""Run the caller's acceptance command and decide whether the work is done.

`finish_reason == 'completed'` means the child's turn ended cleanly. It does not
mean the task is right, and an agent declaring victory prematurely is a primary
failure mode of long-running harnesses. So the caller states the command that
proves the work, and this module runs it -- the server, not the child. A child
reporting its own test results is a claim; an exit code is a fact.

The command is classified through the same `guard` policy the child's own calls
go through before it executes. The caller is another agent and can be prompt
injected, so "the caller asked for it" is not authorization on its own. Only a
command the classifier positively ALLOWs runs; escalate and deny both block.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings, log
from .guard import ALLOW, classify_verification

OUTPUT_TAIL_CHARS = 2000


@dataclass
class VerificationResult:
    command: str
    passed: bool
    reason: str
    exit_code: int | None = None
    output_tail: str = ""
    duration_seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "command": self.command,
            "passed": self.passed,
            "reason": self.reason,
            "duration_seconds": round(self.duration_seconds, 1),
        }
        if self.exit_code is not None:
            out["exit_code"] = self.exit_code
        if self.output_tail:
            out["output_tail"] = self.output_tail
        return out


def run_verification(command: str, workspace: Path, settings: Settings) -> VerificationResult:
    """Classify, then execute, then report. Never raises."""
    command = (command or "").strip()
    if not command:
        return VerificationResult(command, False, "no verification command was given")

    verdict = classify_verification(command, workspace)
    if verdict.action != ALLOW:
        log.warning("verification blocked (%s): %s", verdict.action, verdict.reason)
        return VerificationResult(
            command, False, f"blocked before execution: {verdict.reason}"
        )

    started = time.time()
    try:
        # Executed through a shell because an acceptance command is written as a
        # shell line; every segment of it was classified above.
        completed = subprocess.run(  # noqa: S602 - shell use is the classified path
            command,
            shell=True,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=settings.verify_timeout,
        )
    except subprocess.TimeoutExpired:
        return VerificationResult(
            command,
            False,
            f"timed out after DSA_VERIFY_TIMEOUT ({settings.verify_timeout:g}s)",
            duration_seconds=time.time() - started,
        )
    except OSError as exc:
        return VerificationResult(
            command,
            False,
            f"could not run: {type(exc).__name__}: {exc}",
            duration_seconds=time.time() - started,
        )

    duration = time.time() - started
    output = ((completed.stdout or "") + (completed.stderr or "")).strip()
    tail = output[-OUTPUT_TAIL_CHARS:] if len(output) > OUTPUT_TAIL_CHARS else output
    if completed.returncode == 0:
        return VerificationResult(
            command, True, "exit 0", 0, tail, duration
        )
    return VerificationResult(
        command,
        False,
        f"exited {completed.returncode}",
        completed.returncode,
        tail,
        duration,
    )
