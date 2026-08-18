# deepseek-subagent-mcp

Gives Claude Code, Codex, or any other MCP client a DeepSeek Harness agent it can
delegate work to, the way it would delegate to one of its own subagents.

**MCP** (Model Context Protocol) is the standard by which a coding agent loads
external tools. **DeepSeek Harness** is DeepSeek's open-source agent runtime — a
model in a loop with file and shell tools, released August 2026 under MIT. This
server sits between them: it runs a Harness agent in a separate process and
exposes six tools for starting, watching, continuing, and stopping it.

The child agent has its own context window. That is the point — you hand it a
self-contained task, it burns its own tokens working through the files, and you
get back a result instead of a transcript.

## Requirements

- Python 3.11 or newer
- A DeepSeek API key from [platform.deepseek.com](https://platform.deepseek.com)
- macOS 14+ on Apple Silicon, or Linux on x86-64 or arm64

No Node.js install is needed: the Harness runtime ships as a self-contained
executable inside the `deepseek-harness-sdk` wheel. That wheel is also the
platform limit — it publishes `macosx_14_0_arm64`, `manylinux_2_28_x86_64` and
`manylinux_2_28_aarch64` and nothing else, so Windows, Intel macs, and macOS 13
cannot install this at all.

## Install

```sh
uvx --from git+https://github.com/gaztrabisme/deepseek-subagent-mcp deepseek-subagent-mcp
```

### Claude Code

Add to `.mcp.json` in your project, or to `~/.claude.json` for every project:

```json
{
  "mcpServers": {
    "deepseek-subagent": {
      "command": "uvx",
      "args": [
        "--from", "git+https://github.com/gaztrabisme/deepseek-subagent-mcp",
        "deepseek-subagent-mcp"
      ],
      "env": {
        "DEEPSEEK_API_KEY": "sk-...",
        "DSA_WORKSPACE": "/path/to/your/project"
      }
    }
  }
}
```

### Codex

Add to `~/.codex/config.toml`:

```toml
[mcp_servers.deepseek-subagent]
command = "uvx"
args = ["--from", "git+https://github.com/gaztrabisme/deepseek-subagent-mcp", "deepseek-subagent-mcp"]
env = { DEEPSEEK_API_KEY = "sk-...", DSA_WORKSPACE = "/path/to/your/project" }
```

## Tools

| Tool | What it does |
|---|---|
| `dsh_delegate` | Start a new subagent on a task. Returns an `agent_id` and `run_id` immediately. |
| `dsh_await` | Block until a run finishes; returns the result. |
| `dsh_continue` | Send follow-up work to an existing agent, in its original session. |
| `dsh_list` | Every agent this server owns, with state, cost, and run history. |
| `dsh_cancel` | Stop an agent and release its process. |
| `dsh_transcript` | What an agent actually did — tool calls, messages, turn endings, and the raw response. |

Runs are asynchronous by default because a coding task can take many minutes and
MCP clients time out individual tool calls. `dsh_delegate` returns as soon as the
work is queued; `dsh_await` does the waiting and reports progress while it does.
For short tasks, pass `wait_seconds` to `dsh_delegate` and skip the second call.

Each `dsh_delegate` creates one agent, holding one runtime process and one
persisted session. `dsh_continue` re-enters that session, so the child still has
its earlier turns in context.

## Every delegation states how it will be checked

`dsh_delegate` requires a `verification` argument: the command that proves the
task is done.

```
dsh_delegate(task="Fix the failing date parser", verification="pytest -q tests/test_dates.py")
```

The server runs that command itself, in the agent's workspace, after the child
finishes. A child reporting its own test results is a claim; an exit code is a
fact, and agents declaring victory prematurely is a well-documented failure mode.

| Outcome | State |
|---|---|
| Command exits 0 | `completed` |
| Command fails, times out, or was never given | `completed_unverified`, with the output |

The command is classified by the same policy that gates the child's own calls
before it runs — the caller is another agent and can be prompt-injected, so
"the caller asked for it" is not authorization. Pass `verification="true"` when
there is genuinely nothing to check; an explicit lie beats a silent default.

## What comes back

A subagent that returns its full transcript has defeated its own purpose. When
the child's answer is larger than `DSA_SUMMARY_TOKENS`, it is asked — in the same
session, as one further turn — to replace it with a handoff summary in seven
sections: Goal, Constraints & Preferences, Progress, Key Decisions, Next Steps,
Relevant Files, Critical Context. That is what crosses the MCP boundary.

An answer already under the cap is returned verbatim and costs no extra turn.
The raw response is always kept: `dsh_transcript(run_id, raw=True)`.

## Supervised execution

The child's tool calls are gated before they run. A `PreToolUse` hook inside the
runtime hands each proposed call to this server, which answers allow or deny; a
denied call comes back to the model as a blocked tool result carrying the reason,
and the model adapts.

A deterministic classifier decides first, and it decides most calls. Reading
files, `ls`, `grep`, version-control reads, running the workspace's own code and
tests are allowed with no model involved. Privileged commands, deletes outside
the workspace, fetch-piped-into-a-shell, and anything touching SSH keys or `.env`
are denied outright — including through a harmless-looking verb, because `cat
~/.ssh/id_rsa` is a read-only tool applied to a secret. Only what the classifier
cannot classify escalates.

Escalation runs at the best tier the client supports, resolved at startup and
reported by `dsh_list`:

| Tier | Who decides | Requires |
|---|---|---|
| `sampling` | the MCP client's model | client advertises `sampling` |
| `elicitation` | you, in your client | client advertises `elicitation` |
| `deterministic` | nobody — escalation denies | always available |

Every tier fails closed. An unreachable supervisor, a timeout, a malformed
request, or a client that supports neither capability all produce a denial, never
an approval.

The ladder is walked rather than picked from once: a tier that *errors* falls
through to the next one, so a client that drops sampling — deprecated in the
2026-07-28 spec revision, still working today — degrades to asking you instead of
denying everything. A tier that *times out* does not fall through; an unanswered
question is a no, and re-asking on another channel would only double the wait.

Set `DSA_SUPERVISOR=off` to disable the gate entirely.

What the supervisor is shown is structured facts, never the child's prose: the tool, the programs
in each pipeline segment, and every path the command names with an inside-or-outside-the-workspace
flag. The child writes both the command and any justification for it, and a child that can argue
its own case will. A path that cannot be resolved statically — `$TMPDIR/out.txt` — is reported as
unresolved rather than guessed at, and counts as outside.

`examples/claude_supervisor.py` runs the whole pattern against a real Claude, for clients that do
not advertise `sampling` themselves:

```sh
DEEPSEEK_API_KEY=sk-... uv run python examples/claude_supervisor.py
```

## Ceilings and cost

A delegated agent spends your money in a loop, so four independent ceilings bound
it, and every run reports what it used.

| Ceiling | Knob | Enforced by |
|---|---|---|
| Wall-clock per run | `DSA_RUN_TIMEOUT` | killing the runtime |
| Total tokens per run | `DSA_TURN_TOKEN_BUDGET` | killing the runtime |
| Model calls per run | `DSA_MAX_STEPS` | killing the runtime |
| Identical repeated tool calls | `DSA_LOOP_STRIKES` | killing the runtime |

There is no mid-turn cancel on the wire, so every stop is a process kill. A kill
for a ceiling always outranks whatever the run itself reported: a killed
process's output is never read as success.

`dsh_delegate`, `dsh_await` and `dsh_list` all report token usage — input,
output, cache reads and writes, and step count — summed from what the provider
reported. Per-step input is summed deliberately: every request bills the whole
resent prefix, so the total is what the delegation actually cost.

## Configuration

Every setting is an environment variable on the server process.

| Variable | Default | Meaning |
|---|---|---|
| `DEEPSEEK_API_KEY` | — | Required. Passed to the child runtime. |
| `DEEPSEEK_BASE_URL` | DeepSeek's public API | Point at a proxy or a self-hosted endpoint. |
| `DSA_MODEL` | `deepseek-v4-pro` | Model id for delegated work. `deepseek-v4-flash` is cheaper. |
| `DSA_WORKSPACE` | the server's working directory | Directory the child reads and writes. |
| `DSA_MAX_AGENTS` | `4` | Live agents allowed at once. Each holds a process. |
| `DSA_SESSION_ROOT` | `<workspace>/.dsh-sessions` | Where session logs are written. |
| `DSA_MAX_TOKENS` | provider default | Per-request output cap for the child. |
| `DSA_TURN_TOKEN_BUDGET` | unset | Total tokens one run may spend before it is killed. |
| `DSA_MAX_STEPS` | `40` | Model calls one run may make before it is killed. |
| `DSA_LOOP_STRIKES` | `3` | Identical tool calls before the run is killed as a runaway. |
| `DSA_RUN_TIMEOUT` | `1800` | Seconds before a run is killed and reported failed. |
| `DSA_IDLE_TIMEOUT` | `900` | Seconds before an idle agent is reaped and evicted. |
| `DSA_RUN_ARCHIVE` | `200` | Finished runs kept readable after their agent is reaped. |
| `DSA_SUMMARY_TOKENS` | `2000` | Result size above which the child is asked to distil. |
| `DSA_CHARS_PER_TOKEN` | `3.5` | Conversion used for that cap. Measured at 3.54 on this workload. |
| `DSA_VERIFY_TIMEOUT` | `300` | Seconds the verification command may run, capped by the run's remaining deadline. |
| `DSA_SUPERVISOR` | `auto` | `auto` / `sampling` / `elicitation` / `off`. |
| `DSA_SUPERVISOR_TIMEOUT` | `120` | Seconds to wait for a verdict before denying. |
| `DSA_SANDBOX_MODE` | `workspace-write` | `read-only`, `workspace-write`, or `danger-full-access`. |
| `DSA_REASONING_EFFORT` | `low` | `off` / `low` / `high` / `max`. Drives cost hard. |
| `DSA_CONTEXT_WINDOW` | `200000` | Working budget compaction is measured against. |
| `DSA_BASH_TIMEOUT_MS` | `60000` | Executor-level bound on one bash call. |
| `DSA_REQUEST_TIMEOUT` | none | Seconds to wait on one runtime request. |
| `DSA_TRANSCRIPT_LIMIT` | `400` | Activity lines retained per run. |
| `DSA_LOG_LEVEL` | `info` | Server log level. Writes to stderr only. |
| `DSA_CORDIS` | the packaged composition | A path, or `bundled` for upstream's minimal config. |
| `DSA_PROVIDER` | `deepseek-official` | Provider route registered by the composition. |

## Limits you should know before relying on this

These come from the Harness SDK wire protocol, not from choices made here.

- **The filesystem sandbox does not cover bash.** `dsh-fs-sandbox` confines the
  model's `write`/`edit` tools to the workspace, but `dsh-bash-sandbox` is not in
  the bundled runtime executable, so bash itself is unconfined. The supervisor
  covers this — it gates every tool including bash, upstream of execution. With
  `DSA_SUPERVISOR=off` there is no boundary on bash at all; point it at a branch
  or a scratch directory.
- **The sandbox restricts file effects only** — not network, processes, or
  syscalls. And `workspace-write` permits `/tmp` as well as the workspace root.
- **Cancel kills the process.** There is no mid-turn cancel on the wire, so
  `dsh_cancel` terminates the runtime. Edits already written stay on disk, and
  the session cannot be resumed afterwards.
- **A reaped agent's session is gone, but its results are not.** After
  `DSA_IDLE_TIMEOUT` the process is released; `dsh_await` and `dsh_transcript`
  still work on its finished runs, `dsh_continue` does not.
- **Sessions live as long as the process.** There is no per-session close, so
  memory grows with an agent's history. Cancel agents you are done with.
- **Upstream is a developer preview.** `deepseek-harness-sdk` is pinned at
  `==0.1.0rc7`; two release candidates shipped inside a week. Expect the wire to
  move.

## Development

```sh
uv sync
uv run pytest                  # 127 tests, no API key, no network
uv run ruff check .
uv run deepseek-subagent-mcp   # starts on stdio; a client drives it
```

Live tests need a real key and cost tokens; they are not collected by pytest:

```sh
DEEPSEEK_API_KEY=sk-... uv run python tests/smoke_task.py        # the product works
DEEPSEEK_API_KEY=sk-... uv run python tests/smoke_result.py      # distillation and the archive
DEEPSEEK_API_KEY=sk-... uv run python tests/smoke_supervisor.py  # the gate works
DEEPSEEK_API_KEY=sk-... uv run python tests/smoke_escalation.py  # both escalation tiers
DEEPSEEK_API_KEY=sk-... uv run python tests/smoke_limits.py      # reaper and deadline
DEEPSEEK_API_KEY=sk-... uv run python tests/smoke_mcp.py         # all six tools
```

`CLAUDE.md` carries the architecture and the upstream constraints; `wiki/`
carries the decision record and what was measured.

## License

MIT.
