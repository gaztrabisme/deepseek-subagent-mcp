# Run ledger

## 2026-08-18 — greenfield build, then production hardening

Route: conductor → Frame → Build. Engine: inline for the build; a three-agent task force for the
research phase (`Work/harness`, `Work/Vietnam-consulting/GoDucThanh`, dsh internals).

### Phase 1 — the wrapper

Cloned `deepseek-ai/deepseek-harness` @ `0.1.0-rc.7` and read the packages in
[index.md](index.md). Built the six-tool server on the Python SDK. Nine forks decided at the plan
block; one change on the way in — default model `deepseek-v4-pro` rather than `deepseek-v4-flash`.

Forks decided forward during the build: `mcp.server.fastmcp` does not exist in mcp 2.x (used
`MCPServer`); startup failure surfaced as an opaque "agent is closed" (added readiness
signalling); `finish_reason='error'` hid the cause (added `turn_error_detail`); transcripts
drowned in `assistant/chunk` (added the noise filter).

Verified: 19 unit tests, plus a live FizzBuzz delegation whose files were re-executed
independently by the test.

### Phase 2 — assessment

Asked whether it was production ready. It was not. Five gaps named: unsupervised child, no cost
ceiling, no reaping, no run-duration cap, single-platform verification.

### Phase 3 — research task force

Three agents in parallel. What changed the plan:

- **The bundled executable already carried ~100 plugins.** Four of the five gaps were
  configuration, not code. This reframed the whole effort.
- **`Work/harness` had not solved supervision either** — it runs `claude -p` with
  `--dangerously-skip-permissions` and gates *before* dispatch. Its transferable parts were the
  timeout/kill pattern, outcome precedence, and `loopgate.rs`.
- **GoDucThanh** supplied per-call-type caps, the token-estimator drift measurement, and the
  red-team probe shape.
- One agent contradicted itself on `maxTokensAsSuccess`; source settled it (`false`). A second
  agent claim — that `compaction-basic` requires `summarizationProvider` — was also wrong.
  **Agent reports were checked against source before being acted on, twice profitably.**

### Phase 4 — hardening

Composition (19 entries, boot-tested by bisection), packaging fixes, reaper, run deadline, loop
gate, kill precedence. Two composition traps found by booting rather than reading: mounting
`dsh-sandbox` beside `sandbox-local` double-registers, and `workspace-write` permits `/tmp`, which
made the first sandbox escape test pass while proving nothing.

Measured the sandbox honestly (D10): file tools confined, bash not.

### Phase 5 — supervised execution

Gary's design: a supervisor as middle manager, escalated to before risky calls run. Verified every
piece existed, built it in eight tasks (S1–S8), proved it live (D12). The `echo > ~/file` that
defeated the sandbox is now blocked before execution.

### Verification state

64 unit tests. Four live smoke scripts, all passing:
`smoke_mcp.py`, `smoke_task.py`, `smoke_limits.py`, `smoke_supervisor.py`.

A throwaway API key supplied by Gary was used for live runs. It was passed as a shell environment
variable and is written to no file in this repository. Live tests read `DEEPSEEK_API_KEY` from the
environment.

### Phase 6 — the result contract, the ceilings, and the repository

Gary's brief: "plan remaining features, final deliverable is a production ready product". Four
forks were decided up front — public GitHub without PyPI, verification required rather than
optional, distillation on overflow rather than always, and the four defects found by reading the
code included in scope.

Built in eight units: logging, the run archive, usage accounting with the token and step ceilings,
verification (`verify.py`), distillation, Tasks-aligned state names with progress reporting, hook
identity hardening, then packaging and CI.

Three things were found by writing the code rather than planning it:

- **`cat ~/.ssh/id_rsa` was allowed.** Sensitive-path checks only ever ran on writes and deletes,
  so any read-only verb could exfiltrate a key. Fixed for the child, not just for verification.
- **Splitting a compound command into segments loses cross-segment danger.** The first version of
  `classify_verification` split before classifying, which turned `curl … | sh` from a deny into an
  escalate. The whole line is judged first; only the "compound command line" escalation is
  re-examined segment by segment.
- **The deadline smoke had stopped testing the deadline.** With the gate on, the supervisor denies
  `sleep`, so the child never blocked and the run finished before the deadline. That leg now runs
  with `DSA_SUPERVISOR=off` to isolate the thing under test.

One test premise was wrong rather than one behaviour: asking the child for a 600-word report got a
`report.md` on disk and a three-line reply — correct behaviour, useless proof. The task now
demands the text in the reply itself.

### Verification state

106 unit tests, ruff clean. Five live smoke scripts, all passing: `smoke_mcp.py`, `smoke_task.py`,
`smoke_result.py`, `smoke_limits.py`, `smoke_supervisor.py`.

Measured this phase, on macOS 26.5.2 / arm64 / Python 3.11.5 against `deepseek-v4-pro`:

- FizzBuzz task with verification: 7.8s, 8,435 tokens over 3 steps, `python3 fizzbuzz.py` exit 0.
- Distillation: 8,438 raw chars → 2,033 chars, seven sections, 52.0s, 17,247 tokens over 4 steps.
- **Chars per token: 3.54** (2,033 chars / 575 reported output tokens). `DSA_CHARS_PER_TOKEN`
  defaults to 3.50.
- Archive: agent reaped at `DSA_IDLE_TIMEOUT=5`, `dsh_await` still returned the result and the
  transcript; `dsh_continue` correctly refused.
- Deadline: `DSA_RUN_TIMEOUT=15` with the gate off — failed with the timeout reason, no leaked
  process.

A throwaway API key supplied by Gary was used for live runs. It was passed as a shell environment
variable and is written to no file in this repository.
