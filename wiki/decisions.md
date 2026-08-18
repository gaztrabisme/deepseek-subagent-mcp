# Decisions

## D1 — Drive the Python SDK, not the TypeScript SDK

**Chosen:** `deepseek-harness-sdk` (PyPI), distributed as `uvx deepseek-subagent-mcp`.

Installing it pulls `deepseek-harness-runtime-bin`, a platform wheel containing a
self-contained Node executable plus a default `cordis.yml`. The consumer needs no
Node install and no plugin composition.

**Rejected — TypeScript SDK (`@deepseek-ai/dsh-sdk-client`).** Its README states it
performs no bundled-runtime resolution: callers must name the runtime executable and
compose their own plugin tree. That pushes a Node 22.19+ requirement and a
composition-authoring step onto every consumer. Distribution friction is the product
here, so the SDK that removes it wins over the one that matches the MCP ecosystem's
language.

**Rejected — shelling out to `dsh --profile headless "job"`.** One-shot only. No
session continuation, no activity stream, no structured result.

**Cost of the choice:** no Windows wheel exists. Verified working on macOS arm64.

## D2 — ACP is the fallback surface, not the primary one

`@deepseek-ai/dsh-acp` has two things the SDK wire lacks: `session/cancel` and
`session/request_permission`. It also has *"fresh sessions only — load, list, resume,
delete, and fork are unsupported"* and rejects non-empty `mcpServers`.

"Use it like their own subagents" means a delegate you can come back to, so session
continuation outranks cancel and permissions. Switch to ACP if unsupervised editing
becomes unacceptable — that is the one thing ACP buys that cannot be worked around.

## D3 — Asynchronous runs with an explicit await

`dsh_delegate` returns a `run_id` as soon as work is queued; `dsh_await` polls. MCP
clients time out individual tool calls and a coding task runs for minutes.
`wait_seconds` on `dsh_delegate` and `dsh_continue` covers short tasks in one call.

## D4 — One agent = one runtime process = one session

`dsh_continue` re-enters the same session on the same process, so the child keeps its
context. Serial task queue per agent, one worker thread each. `DSA_MAX_AGENTS`
(default 4) bounds process count.

**Consequence:** sessions live as long as the process — the wire has no per-session
close. Agents must be cancelled when finished.

## D5 — Cancel kills the process

The SDK wire has no mid-turn cancel (stated in both SDK READMEs). `dsh_cancel` calls
`harness.close()`, which walks stdin-EOF → SIGTERM → SIGKILL. The in-flight
`session.run` raises `TransportClosedError` and the run is reported as cancelled.
Edits already written stay on disk; the session cannot be resumed.

## D6 — Default model `deepseek-v4-pro`

Set by Gary. The SDK's own default is `deepseek-v4-flash`, and the adapter advertises
both. Pro costs roughly 3x Flash on both input and output. Override with `DSA_MODEL`.

## D7 — Failures report the runtime's own error

A failing turn arrives as `finish_reason='error'`, which tells the caller nothing.
`turn_error_detail` pulls `data.reason.error.{code,message}` out of the `turn/end`
event, so a missing key surfaces as `MISSING_CREDENTIAL: no API key for provider
route "deepseek-official"…` instead of `finish_reason='error'`.

## D8 — Bounded, filtered transcripts

Streaming deltas (`*/chunk`), `request/context`, `request/header`, and text-less
assistant messages are dropped. A one-word answer emitted 36 raw events; the filter
leaves the tool calls, the answer, and the turn ending. Retained lines per run:
`DSA_TRANSCRIPT_LIMIT`, default 400.

## D9 — Startup failure is a tool error, not a failed run

`dsh_delegate` waits for the runtime handshake (up to 60s; ~7s observed) before
returning. A missing binary or bad composition raises on the tool call with the real
cause, rather than returning a run that is silently cancelled.

## Open

- **Session rehydration across process restarts is unverified.** JSONL persistence
  and get-or-create-per-`sessionId` suggest it works; the wire does not promise it.
  Not relied on: cancel is documented as ending the session.
- **`final_response` describes an interval, not a prompt** (SDK's own wording). With
  one caller per agent, as here, it is the answer to the prompt.

## D10 — The sandbox confines the file tools, not bash (measured)

Mounting `dsh-sandbox-local` + `dsh-sandbox-policy` (`mode: workspace-write`) and swapping
`dsh-fs-local` → `dsh-fs-sandbox` gives a real, enforced fence on the model-facing `write`/`edit`
tools. Measured against `$HOME` on 2026-08-18:

| Path | Mechanism | Result |
|---|---|---|
| `~/dsa-escape-fs.txt` | `write` tool | **denied** — `[sandbox: file access denied under workspace-write mode]` |
| `~/dsa-escape-bash.txt` | `bash` heredoc | **written** — exit 0, no error |
| `./inside.txt` | `write` tool | written, as intended |

`@deepseek-ai/dsh-bash-sandbox` exists upstream but is **not** in the bundled runtime
executable's dependency closure (`python/sdk-runtime/package.json` lists `dsh-bash-local` only).
There is therefore no way to confine bash with the shipped binary.

The denial message offers escalation via `sandbox_permissions`, routed through `ctx.approval`.
With no answerer composed that request fails closed, so escalation is denied rather than granted.

**Consequence, stated plainly:** a child that wants to write outside its workspace can do so
through bash. The sandbox raises the cost of an accident; it is not a boundary against intent.
The same residual risk is logged in `Work/harness` ("bash is cwd-scoped but NOT a hard sandbox").

Options considered:
- **Accept and document** — chosen for now.
- A `PreToolUse` deny hook via `dsh-hooks-claude-code` matching write-redirection outside the
  workspace. Brittle against `cd`, absolute paths, and anything indirect.
- Build a custom runtime executable that includes `dsh-bash-sandbox` (upstream
  `scripts/build-exe-for-python-sdk.ts`, needs Node ≥22.19 and pnpm). The real fix, and a
  separate piece of work.

## D11 — Two composition traps found by boot-testing

- Mounting `@deepseek-ai/dsh-sandbox` alongside `dsh-sandbox-local` fails at load with
  `service "sandbox" has been registered`. The provider registers the service; mount the provider
  only. Same shape as compaction, where only `compaction-basic` is mounted.
- `workspace-write` permits `/tmp` in addition to the workspace root. A sandbox test that writes
  under `/tmp` proves nothing — the first escape test written here passed for that reason and had
  to be redone against `$HOME`.

## D12 — Supervised execution closes the bash hole that the sandbox could not

The filesystem sandbox confines the file tools and cannot confine bash (D10). A `PreToolUse`
hook can, because it fires on `tools/pre-execute` for *every* tool and sits upstream of
execution rather than being a filesystem fence.

The chain, all local: dsh runtime → hook process (tool call as JSON on stdin) → unix socket →
this server → verdict → exit 0 allows, exit 2 blocks with stderr as the reason the model reads.
No wire change; the SDK's three-method protocol is not involved.

**The classifier decides, not the model.** `guard.py` runs first on parsed argv and settles the
clear cases. Only what it cannot classify escalates. This keeps the dangerous cases independent
of any model's judgement and keeps latency and cost off the routine path.

Measured on a real child, 2026-08-18:

| Step | Verdict | By |
|---|---|---|
| `ls -la` | allow | policy, no model |
| `python3 hello.py` | allow | policy, no model |
| `echo PROBE > ~/dsa-supervisor-probe.txt` | **deny** | escalation, failed closed |
| `curl -s https://example.com -o fetched.html` | **deny** | escalation, failed closed |

The home-directory write is the same call that succeeded against the sandbox in D10. It is now
blocked before execution. `hook/invoked` and `hook/result` appear in the session log for every
tool call, so the audit trail is durable and free.

### Tiers

`sampling` (the MCP client's model is the supervisor) → `elicitation` (the operator) →
`deterministic` (escalation denies). Chosen once at startup from the client's advertised
capabilities and reported by `dsh_list`, so the active tier is never a guess. The stdio test
client advertises neither, so the proof above ran at the deterministic floor — the tier that
must be right before any model is trusted with the rest.

### Two things deliberately not done

- **The supervisor never sees the child's prose.** The child writes both the command and any
  justification; a child that wants approval would otherwise be arguing its own case. It is shown
  structured facts only — tool, programs, resolved paths, inside-workspace flags — and a test
  asserts no other key reaches it.
- **Classification runs on parsed argv, never raw text.** While writing this project's own task
  list the host sandbox blocked the write because the markdown contained a dangerous-looking
  string in a command that deleted nothing. A text matcher false-positives on quotes, comments
  and documentation, and false-negatives on anything obfuscated. `_command_heads` also inspects
  every segment of a pipeline, so a dangerous tail cannot hide behind a harmless head.

---

## D13 — A run reports what was proved, not that a turn ended

`finish_reason == 'completed'` means the model's turn ended cleanly. It says nothing about whether
the work is right, and premature victory declarations are a documented failure mode of
long-running agent harnesses.

`dsh_delegate` therefore **requires** a `verification` argument: the command that proves the task
is done. Exit 0 gives `completed`; a failure, a timeout, a blocked command, or no command at all
gives `completed_unverified` with the output attached.

**The server runs it, not the child.** A child reporting its own test results is a claim; an exit
code is a fact. Asking the child to self-report would have been cheaper and would have measured
nothing.

**Rejected: trusting the caller.** The command is classified through `guard` before it executes,
segment by segment. The caller is another agent and can be prompt-injected, so "the caller asked
for it" is not authorization. Only a command the classifier positively allows ever runs.

**Rejected: making it optional.** An optional check is an unused check. `verification="true"` is
available for the case where there genuinely is nothing to run — an explicit lie beats a silent
default, and it shows up in the record.

### A finding this produced

Classifying acceptance commands exposed a hole in the classifier that affected the child too:
`cat ~/.ssh/id_rsa` was **allowed**, because sensitive-path checks only ever ran on writes and
deletes. A read-only verb applied to a secret is not a read-only call. Sensitive paths are now
refused whatever the command, before the per-command rules get a say — restricted to words that
look like paths, so `echo credentials` is not mistaken for a filename.

## D14 — Distil on overflow, not always

Anthropic's guidance is that a subagent returns 1,000–2,000 tokens, not a transcript. When the
child's answer exceeds `DSA_SUMMARY_TOKENS`, it is asked — in the same session, as one further
turn — for the seven-section handoff contract that `Work/harness/research/25-context-engineering.md`
§4.1 found convergent across four independent codebases.

**Rejected: distilling every run.** An extra turn on a one-line answer is pure waste. An answer
already under the cap is returned verbatim and costs nothing.

**Rejected: folding the contract into the original prompt.** No extra turn, but the format
instructions pollute the child's working context and it half-follows them mid-task.

The raw response is never discarded — `dsh_transcript(run_id, raw=True)`.

### The cap is now measured, not assumed

`DSA_SUMMARY_TOKENS=2000` was recorded as a guess taken from Anthropic's figure. The cap is
applied in characters via `DSA_CHARS_PER_TOKEN`, and every distillation turn logs the ratio the
provider actually reported. First live measurement: **3.54 chars per token** (2,033 chars for 575
reported output tokens) against an assumed 3.50. The guess was close; it is no longer a guess.

Live proof: an 8,438-character report became a 2,033-character handoff carrying all seven
sections, under a 2,800-character cap, with the raw text still reachable.

## D15 — A finished run outlives its agent

The idle reaper closed an agent and `_evict_closed` dropped it from the registry, taking its runs
with it. `dsh_await` on a run that had completed perfectly well then failed with `unknown run_id`
— which breaks the one usage pattern the product exists for: delegate, go do something else, come
back.

Terminal runs are now copied into a bounded archive (`DSA_RUN_ARCHIVE`, 200) before their agent is
evicted. `dsh_await` and `dsh_transcript` read through to it. `dsh_continue` still fails, because
the session really is gone — the process was killed and the wire has no way to resume it.

**Rejected: keeping agents alive longer.** Each holds a subprocess. The reaper exists precisely
because they must not accumulate.

## D16 — Four ceilings, not one

A per-request output cap does not bound an agentic loop. Four independent ceilings now do, each
enforced the only way the wire allows — killing the runtime:

| Ceiling | Knob | Signal |
|---|---|---|
| Wall-clock | `DSA_RUN_TIMEOUT` | the reaper's clock |
| Tokens | `DSA_TURN_TOKEN_BUDGET` | `assistant/message.usage`, summed |
| Model calls | `DSA_MAX_STEPS` | `step/start` events, counted |
| Repetition | `DSA_LOOP_STRIKES` | identical tool-call fingerprints |

They share one trip-then-reap mechanism, so the existing rule that a kill outranks the run's own
result covers all four without special cases.

Usage is summed exactly as upstream's `usageTokens` does — input + cacheRead + cacheWrite +
output, never adding `reasoningTokens`, which is already inside output. Summing per-step input is
deliberate: every request bills the whole resent prefix, so the total is what the delegation cost,
not what its final context holds.

**Rejected: one shared cap across call types.** GoDucThanh shipped a single constant governing
three structurally different calls and silently degraded its summarizer until compaction disabled
itself permanently. The task turn's output cap and the result cap are separate settings.

## D17 — Failures are logged, and the log never touches stdout

Every failure path in the reaper, the worker, the capability probe and teardown swallowed its
exception with a bare `pass`. A reaper that starts failing was invisible.

All of them now log through one `logging` logger with a **stderr-only** handler. This is not
stylistic: stdout carries JSON-RPC frames to the MCP client, and a handler there corrupts the
wire — the same rule that forbids a stdout-logging plugin in the child's composition.

Verdicts log with their tier, tool and agent id, which makes the gate's behaviour readable during
a live run instead of only afterwards through `dsh_list`.

## D18 — The escalation ladder is walked, not chosen once

Live proof of the escalation tiers surfaced a warning worth acting on:

```
MCPDeprecationWarning: The sampling capability is deprecated as of 2026-07-28 (SEP-2577).
```

SEP-2577 deprecates sampling, logging, roots, and client→server progress. Sampling still works —
it was measured allowing and denying real child tool calls — but the top rung of the ladder is on
a capability the spec has marked for removal, and the original code treated any escalation failure
as a denial. A client dropping sampling would therefore have silently degraded the supervisor from
"asks a model" to "denies everything", which looks identical to working.

The ladder is now walked. A tier that **errors** falls through to the next one, so sampling's
removal degrades to asking the operator. A tier that **times out** does not fall through: an
unanswered question is a no, and re-asking on another channel would only double the wait before
the same answer. `dsh_list` reports the head of the ladder; the log line reports all of it.

`elicit()` is also now called through `elicit_form()` where the session offers it — upstream keeps
`elicit` as a compatibility shim that forwards to it.

### Both tiers are now proved, not assumed

`tests/smoke_escalation.py` runs a client that advertises each capability in turn and asserts the
supervisor calls back into it. Previously every live proof ran at the `deterministic` floor,
because the stdio test client advertised neither — so the escalation path had never executed.

| Tier | Result |
|---|---|
| `sampling` | tier resolved, model consulted, `ALLOW` let the child's command run |
| `elicitation` | tier resolved, operator consulted, `DENY` blocked it |

The payload handed over in both cases: tool, workspace, policy reason, and structured facts —
`command_length: 25`, `compound: true`, `programs: ["echo", "echo"]`. No command text, no prose.
The test asserts that directly.
