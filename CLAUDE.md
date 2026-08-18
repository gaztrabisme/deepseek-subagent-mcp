# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An MCP server that exposes **DeepSeek Harness** (`dsh`, DeepSeek's open-source agent runtime,
released 2026-08-13, MIT) as a delegatable subagent to Claude Code, Codex, or any other MCP
client. Six tools: `dsh_delegate`, `dsh_await`, `dsh_continue`, `dsh_list`, `dsh_cancel`,
`dsh_transcript`.

It ships four things upstream does not: a hardened plugin composition, an external supervision
layer that gates the child's tool calls, process-lifetime management the wire protocol cannot
provide, and a result contract — verified completion and a distilled handoff rather than a raw
transcript.

## Commands

```sh
uv sync
uv run pytest                                     # 137 unit tests, no API key, no network
uv run ruff check .
uv run pytest tests/test_guard.py -q              # the classifier alone
uv run pytest tests/test_runs.py::test_run_deadline_kills_the_agent
uv build
uv run deepseek-subagent-mcp                      # stdio; a client drives it
```

Live smokes cost real tokens, need `DEEPSEEK_API_KEY`, and are **not** collected by pytest. Run
them by path after touching `runs.py`, `server.py`, `guard.py`, `verify.py`, `supervisor.py`, or
the YAML:

```sh
DEEPSEEK_API_KEY=sk-... uv run python tests/smoke_mcp.py        # all 6 tools over stdio
DEEPSEEK_API_KEY=sk-... uv run python tests/smoke_task.py       # real file writes, 2-turn session
DEEPSEEK_API_KEY=sk-... uv run python tests/smoke_result.py     # distillation, cap, run archive
DEEPSEEK_API_KEY=sk-... uv run python tests/smoke_limits.py     # reaper + run deadline
DEEPSEEK_API_KEY=sk-... uv run python tests/smoke_supervisor.py # allow/deny through the hook
DEEPSEEK_API_KEY=sk-... uv run python tests/smoke_escalation.py # sampling + elicitation tiers
```

`smoke_supervisor.py` is the one that proves the security story. `smoke_task.py` proves the
product works. `smoke_result.py` proves the caller gets a result rather than a transcript.

### Boot-testing a composition change

Plugin-tree load errors surface as a truncated `AggregateError`, so bisect instead of reading it.
The probe harness used during the build lives at `/tmp/dsa-hard/probe.py` (recreate if gone: it
writes a YAML to a temp file, spawns the runtime exe with `DSH_CORDIS_CONFIG`, sends one
`initialize` frame, and reports whether a `"result"` came back). Add one plugin group at a time.

The runtime executable is at
`.venv/lib/python3.11/site-packages/deepseek_harness_runtime/runtime/dsh-jsonrpc-agent-pkg-macos-arm64`.

## Architecture

Six modules. The interesting parts are the threading seam, the run pipeline, and the supervision
chain.

- **`config.py`** — `Settings.from_env()`, read once at import in `server.py`. Every knob is a
  `DSA_*` environment variable (README has the table). Also owns `child_env()` (what the
  composition reads via `!!js process.env`), `hooks_config()` (the per-agent `hooks.json`),
  `result_cap_chars`, and `configure_logging()`. Every module logs through `config.log`.
- **`runs.py`** — `Registry` → `Agent` → `Run`. **One delegated subagent = one `DeepSeekHarness`
  = one runtime subprocess = one worker thread = one persisted session.** `dsh_continue`
  re-enters the same session, so the child keeps its context. A background reaper enforces the
  idle timeout, the run deadline, and the token/step/loop trips, and archives finished runs
  before evicting their agent.
- **`guard.py`** — deterministic classification of a proposed tool call into allow / deny /
  escalate. Pure, no I/O, heavily tested.
- **`verify.py`** — classifies then executes the caller's acceptance command.
- **`trace.py`** — append-only JSONL of verdicts, runs and calibration samples, read back by
  `scripts/trace_report.py`. Owned by the `Registry` and shared with the `Supervisor`. It runs on
  the agent's worker thread, so `Trace.write` catches **every** exception: an error escaping there
  kills that thread, the run whose trace failed still looks fine, and every later run on that
  agent hangs. Observability is never worth a delegation.
- **`supervisor.py`** — the unix-socket verdict server and the escalation tiers.
- **`server.py`** — the six tools. Thin: validates arguments, calls the registry, shapes results.
  Domain behaviour lives in `runs.py`.
- **`runtime/hardened.cordis.yml`** — the plugin composition. `runtime/approval_hook.py` — the
  hook command.

### The run pipeline

One submitted unit of work is three stages on the worker thread, not one turn. `run.done` fires
when all of it is over, so `dsh_await` needs no change to see the whole thing:

```
task turn (session.run)
  → verification   run_verification(): classify, then execute, exit code decides the state
  → distillation   one further session.run in the SAME session, only if the answer overflows
```

The pipeline must always finish before the reaper acts on the run, because a kill does not reach
a subprocess the worker thread is blocked in. Verification is therefore capped by the run's
remaining deadline, and `_evict_closed` refuses to evict an agent whose runs are not all terminal.

`run.phase` (`queued` / `running` / `verifying` / `distilling` / `done`) reports which stage is
live; `run.state` uses the MCP Tasks vocabulary (`working` / `completed` / `completed_unverified`
/ `failed` / `cancelled`) so a later migration to the Tasks extension is mechanical. Do not adopt
the extension itself yet — it is typed in `mcp` 2.0.0 but has no server-side implementation, so
adopting it means dropping to `mcp.server.lowlevel.Server`.

### The sync/async seam

The upstream Python SDK (`deepseek_harness`) is **synchronous**; MCP is asyncio. Every blocking
call crosses via `anyio.to_thread.run_sync` — `run.done.wait`, `agent.wait_ready`, `agent.close`.
Never call SDK methods directly from a tool coroutine. `_wait_for` chunks the wait so
`ctx.report_progress` can fire between chunks.

### The supervision chain

```
child calls a tool
  → dsh tools/pre-execute
  → PreToolUse hook (local process, tool call as JSON on stdin, --agent <id> on argv)
  → unix socket → supervisor.py
      guard.classify:  allow → exit 0        (no model, no latency)
                       deny  → exit 2 + reason
                       escalate ↓
      tier ladder:  sampling (ctx.session.create_message — the MCP client's model)
                  → elicitation (ctx.session.elicit_form — the operator)
                  → deterministic (deny)
      a tier that ERRORS falls through to the next; a tier that TIMES OUT denies
  → exit 0 allows; exit 2 blocks with stderr as the reason the model reads
```

The whole ladder is resolved once, on the first tool call, from the client's advertised
capabilities; `dsh_list` reports its head. Sampling is deprecated as of the 2026-07-28 spec
revision (SEP-2577) and still works, which is exactly why an erroring tier falls through instead
of denying. The socket runs in the MCPServer lifespan. `supervisor.bind(ctx.session)`
captures the session because escalation happens on a worker thread's timeline, outside any tool
call.

**Four invariants worth preserving:**

1. **The supervisor never sees the child's prose.** The child authors both the command and any
   justification; showing it that text lets it argue its own case. It receives structured facts
   only. `test_facts_never_carry_child_prose` asserts this.
2. **Classify on parsed argv, never raw text.** A substring matcher false-positives on quoted
   strings, comments and documentation — the host sandbox blocked a write of this repo's own task
   list, and later a test file, for exactly that reason — and false-negatives on anything
   obfuscated. `_command_heads` inspects every pipeline segment so a dangerous tail cannot hide
   behind a harmless head.
3. **The workspace a verdict is judged against comes from the registry, not the request.** The
   hook reports one, but the hook is on the supervised side of the boundary. `--agent <id>` in
   the hook command is what makes the server-side lookup possible.
4. **A read-only verb is not a read-only call.** `cat ~/.ssh/id_rsa` is `cat`. Sensitive paths are
   refused whatever the command, before the per-command rules get a say.
5. **One policy, not two.** The child's commands and the caller's verification command go through
   the same `classify_bash`. A compound line is judged segment by segment — escalating them
   wholesale sent `pwd && ls` to a model, measured at 11 seconds — and cross-segment dangers are
   caught before that by `_command_heads`, which sees every segment.
6. **An escalation carries the paths the decision turns on.** `_path_facts` runs before every
   early return in `classify_bash`, so a compound line does not escalate with `compound: true` and
   nothing else — a supervisor handed no evidence denies from ignorance. Paths are structure and
   are safe to show; the child's sentences about them are not. An unexpanded `$VAR` is reported
   `resolved: false` and treated as outside, never resolved as if it were relative.

## Constraints that shaped the design

These come from the upstream protocol and the shipped binary, not from choices made here. Read
`wiki/decisions.md` before changing any of them.

- **The SDK wire has three client→server methods** (`initialize`, `session/prompt`, `shutdown`)
  and four server→client notifications. No approval, no question, no cancel, no session close.
  Every capability this server adds beyond that is external by necessity.
- **No mid-turn cancel.** `dsh_cancel` kills the process. The worker maps `TransportClosedError`
  to `CANCELLED` or `FAILED` by checking `_kill_kind`.
- **A kill for timeout, loop, budget or steps outranks the run's own result.** A killed process's
  output is never read as success (ported from `Work/harness` `worker.rs::outcome`).
- **No per-session close**, so agent count is bounded by `DSA_MAX_AGENTS` and the reaper.
- **No server-side deadline anywhere in the runtime**, and no `maxSteps` in `packages/core/
  agent-loop`. External wall-clock tracking, step counting, and subprocess kill are the only
  options.
- **Token accounting arrives only on `assistant/message.usage`** (`packages/core/session/src/
  types.ts:271`). `RunResult` carries none and there is no separate usage event. Sum it as
  upstream's `usageTokens` does — input + cacheRead + cacheWrite + output, never adding
  `reasoningTokens`, which is already inside output.
- **`step/start` is `{turn, step}`**; counting those events is the only step ceiling available.
- **`tool/call.arguments` is a raw JSON string**, not an object.
- **`dsh-bash-sandbox` is not in the bundled executable** — only `dsh-bash-local`. bash cannot be
  confined by the filesystem sandbox; that is what the supervisor exists to cover.
- **A failing turn arrives as `finish_reason='error'` with no message.** The cause is in the
  `turn/end` event at `data.reason.error.{code,message}`; `turn_error_detail` extracts it.
  Anything reporting a failure must go through it.
- **stdout is the JSON-RPC channel.** Never add a plugin that logs to stdout
  (`packages/sdk/server/src/index.ts:4`), and never attach a stdout log handler here.

## Testing approach

`tests/test_supervisor.py` drives the ladder with a fake session that advertises whichever
capabilities the case needs; async tests use the anyio plugin (`pytestmark = pytest.mark.anyio`
plus an `anyio_backend` fixture), which needs no extra dev dependency.

`tests/test_runs.py` replaces `DeepSeekHarness` with `FakeHarness` via monkeypatch and uses a
`threading.Event` gate on the fake session so cancellation has something real to interrupt. Tests
wait on `run.done`, never on sleeps. `make_settings(tmp_path, **overrides)` builds a `Settings`;
add new fields there when extending `config.py` or a dozen tests break at once.

`Registry(settings, start_reaper=False)` in tests — otherwise a background thread races them.

A test that submits work and expects `completed` must pass `verification="true"`. Without a
verification command a run is `completed_unverified` by design: no command means no evidence.

## Provenance

`wiki/index.md` maps each design fact to the file in `deepseek-ai/deepseek-harness` @ `0.1.0-rc.7`
that established it. When upstream changes, re-read those files rather than trusting this summary.
`docs/config-catalog.md` in that repo (3,151 lines, CI-verified against the real TypeScript
interfaces) is authoritative for plugin config — the package READMEs drift.
