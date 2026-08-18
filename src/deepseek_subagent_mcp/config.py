"""Server settings, resolved once from the environment at startup."""

from __future__ import annotations

import json
import logging
import os
import shlex
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Every module logs through this one. Handlers are attached in
# configure_logging(), which writes to stderr and only stderr: stdout carries
# JSON-RPC frames to the MCP client and a stray handler there corrupts the wire.
log = logging.getLogger("dsa")


def configure_logging(level: str) -> None:
    """Attach a stderr handler. Safe to call more than once."""
    log.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("dsa %(levelname)s %(name)s: %(message)s"))
    log.addHandler(handler)
    log.setLevel(getattr(logging, level.upper(), logging.INFO))
    log.propagate = False

# DeepSeek-V4-Pro. The harness SDK's own default is deepseek-v4-flash; a
# delegated subagent is doing real work, so we pay for the stronger model.
DEFAULT_MODEL = "deepseek-v4-pro"
DEFAULT_PROVIDER = "deepseek-official"

# The composition shipped with this package. Upstream's bundled config mounts
# eight plugins and leaves the child with no compaction, no sandbox, no token
# accounting and no file-edit tools. See runtime/hardened.cordis.yml.
HARDENED_CORDIS = Path(__file__).parent / "runtime" / "hardened.cordis.yml"
APPROVAL_HOOK = Path(__file__).parent / "runtime" / "approval_hook.py"


def _int_env(name: str, default: int | None) -> int | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}")
    return value


def _float_env(name: str, default: float | None) -> float | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{name} must be a positive number, got {raw!r}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    provider: str
    model: str
    max_tokens: int | None
    workspace: Path
    session_root: Path
    cordis: str | None
    max_agents: int
    request_timeout_seconds: float | None
    transcript_limit: int
    api_key: str | None
    base_url: str | None
    reasoning_effort: str
    context_window: int
    bash_timeout_ms: int
    sandbox_mode: str
    summary_tokens: int
    idle_timeout: float
    run_timeout: float
    turn_token_budget: int | None
    loop_strikes: int
    supervisor: str
    supervisor_timeout: float
    approval_socket: str
    log_level: str
    max_steps: int
    verify_timeout: float
    chars_per_token: float
    run_archive: int

    @classmethod
    def from_env(cls) -> Settings:
        workspace = Path(os.environ.get("DSA_WORKSPACE") or Path.cwd()).expanduser().resolve()
        session_root = Path(
            os.environ.get("DSA_SESSION_ROOT") or (workspace / ".dsh-sessions")
        ).expanduser().resolve()
        # DSA_CORDIS=bundled selects upstream's minimal composition; any other
        # value is a path; unset uses the hardened one shipped here.
        raw_cordis = os.environ.get("DSA_CORDIS") or ""
        if raw_cordis.strip().lower() == "bundled":
            cordis = None
        elif raw_cordis.strip():
            cordis = str(Path(raw_cordis).expanduser().resolve())
        else:
            cordis = str(HARDENED_CORDIS)
        return cls(
            provider=os.environ.get("DSA_PROVIDER") or DEFAULT_PROVIDER,
            model=os.environ.get("DSA_MODEL") or DEFAULT_MODEL,
            max_tokens=_int_env("DSA_MAX_TOKENS", None),
            workspace=workspace,
            session_root=session_root,
            cordis=cordis,
            max_agents=_int_env("DSA_MAX_AGENTS", 4) or 4,
            request_timeout_seconds=_float_env("DSA_REQUEST_TIMEOUT", None),
            transcript_limit=_int_env("DSA_TRANSCRIPT_LIMIT", 400) or 400,
            # Passed explicitly so a caller can scope credentials to this server
            # rather than the whole parent environment. None means "inherit".
            api_key=os.environ.get("DEEPSEEK_API_KEY") or None,
            base_url=os.environ.get("DEEPSEEK_BASE_URL") or None,
            reasoning_effort=os.environ.get("DSA_REASONING_EFFORT") or "low",
            # A working budget, not the model's capability. Both V4 models carry
            # 1,000,000 tokens; compaction fires at a ratio of whatever capacity
            # the adapter reports, and an 800k-token prefix resent every turn is
            # expensive. Size from need, not from the vendor maximum.
            context_window=_int_env("DSA_CONTEXT_WINDOW", 200_000) or 200_000,
            bash_timeout_ms=_int_env("DSA_BASH_TIMEOUT_MS", 60_000) or 60_000,
            sandbox_mode=os.environ.get("DSA_SANDBOX_MODE") or "workspace-write",
            # Anthropic reports subagents should return 1,000-2,000 tokens. That
            # is a starting point, NOT a measurement for this workload.
            summary_tokens=_int_env("DSA_SUMMARY_TOKENS", 2000) or 2000,
            idle_timeout=_float_env("DSA_IDLE_TIMEOUT", 900.0) or 900.0,
            run_timeout=_float_env("DSA_RUN_TIMEOUT", 1800.0) or 1800.0,
            turn_token_budget=_int_env("DSA_TURN_TOKEN_BUDGET", None),
            loop_strikes=_int_env("DSA_LOOP_STRIKES", 3) or 3,
            # auto | sampling | elicitation | off | allow-escalations
            # "off" makes every escalation a denial; "allow-escalations" makes
            # every escalation an approval and exists only for benchmarking the
            # classifier's false-positive rate. Never use it for real work.
            supervisor=os.environ.get("DSA_SUPERVISOR") or "auto",
            supervisor_timeout=_float_env("DSA_SUPERVISOR_TIMEOUT", 120.0) or 120.0,
            approval_socket=os.environ.get("DSA_APPROVAL_SOCKET")
            or str(Path(tempfile.gettempdir()) / f"dsa-approval-{os.getpid()}.sock"),
            log_level=os.environ.get("DSA_LOG_LEVEL") or "info",
            # The wire carries no step ceiling (there is no maxSteps anywhere in
            # packages/core/agent-loop), so this is enforced here by counting
            # step/start events and killing the runtime, like the run deadline.
            max_steps=_int_env("DSA_MAX_STEPS", 40) or 40,
            verify_timeout=_float_env("DSA_VERIFY_TIMEOUT", 300.0) or 300.0,
            # Used to convert DSA_SUMMARY_TOKENS into a character cap. 3.5 is a
            # conservative starting point; the real ratio is logged on every
            # distillation turn (len(text) / provider-reported outputTokens) so
            # it can be replaced with a measurement rather than another guess.
            chars_per_token=_float_env("DSA_CHARS_PER_TOKEN", 3.5) or 3.5,
            run_archive=_int_env("DSA_RUN_ARCHIVE", 200) or 200,
        )

    @property
    def result_cap_chars(self) -> int:
        """Character ceiling on what a run returns across the MCP boundary.

        The knob is in tokens because that is the unit the caller's context is
        measured in, but nothing here can count DeepSeek tokens without pulling
        in the model's tokenizer. The conversion ratio is a setting rather than a
        buried constant, and every distillation turn logs the ratio the provider
        actually reported so it can be calibrated instead of assumed.
        """
        return max(200, int(self.summary_tokens * self.chars_per_token))

    def hooks_config(self, agent_id: str) -> Path:
        """Per-agent hooks.json. The dsh bridge parses configPath once at load,
        and this server runs one runtime process per agent, so policy can vary
        per delegation even though the bridge itself is process-level."""
        path = self.session_root / "hooks" / f"{agent_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.supervisor == "off":
            hooks: dict = {}
        else:
            # The agent id travels in the command line, not in the hook's
            # payload: the supervisor then resolves the workspace from its own
            # registry instead of trusting a field the hook reports.
            command = (
                f"{shlex.quote(sys.executable)} {shlex.quote(str(APPROVAL_HOOK))} "
                f"--agent {shlex.quote(agent_id)}"
            )
            hooks = {"PreToolUse": [{"matcher": "*", "hooks": [
                {"type": "command", "command": command,
                 "timeout": int(self.supervisor_timeout + 30)}]}]}
        path.write_text(json.dumps({"hooks": hooks}, indent=2))
        return path

    def child_env(self) -> dict[str, str]:
        """Environment the hardened composition reads via `!!js process.env`."""
        return {
            "DSA_REASONING_EFFORT": self.reasoning_effort,
            "DSA_CONTEXT_WINDOW": str(self.context_window),
            "DSA_BASH_TIMEOUT_MS": str(self.bash_timeout_ms),
            "DSA_SANDBOX_MODE": self.sandbox_mode,
            "DSA_APPROVAL_SOCKET": self.approval_socket,
            "DSA_HOOK_TIMEOUT": str(self.supervisor_timeout + 20),
        }

    def resolve_workspace(self, requested: str | None) -> Path:
        """Absolute workspace for one agent. Relative paths resolve under the default."""
        if not requested:
            return self.workspace
        candidate = Path(requested).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        return candidate.resolve()
