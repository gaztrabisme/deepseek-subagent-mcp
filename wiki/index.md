# deepseek-subagent-mcp — wiki

Internal notes. Reader-facing documentation is `../README.md`; the working guide for an agent
picking this up is `../CLAUDE.md`.

- [active-work.md](active-work.md) — what is done, what is next, what is measured
- [decisions.md](decisions.md) — D1–D17: choices, rejected options, and measurements
- [log.md](log.md) — run ledger

## What this is

An MCP server exposing DeepSeek Harness (`dsh`, released 2026-08-13, MIT) as a delegatable
subagent to Claude Code, Codex, and any other MCP client. Beyond a thin wrapper it adds four
things upstream does not have: a hardened plugin composition, external supervision of the child's
tool calls, process-lifetime management the wire protocol cannot provide, and a result contract —
verified completion and a distilled handoff rather than a raw transcript.

## The single most important fact

The bundled runtime executable inside `deepseek-harness-runtime-bin` contains **~100 plugins** —
its closure is upstream `python/sdk-runtime/package.json`. The composition it ships mounts
**eight**. Everything this project turns on was already compiled into the binary and reachable
through one YAML file. Check that manifest before concluding a capability is unavailable; check
`docs/config-catalog.md` (CI-verified) for its real config keys, not the package README.

The two things genuinely *absent* from the executable, both load-bearing:
`@deepseek-ai/dsh-bash-sandbox` (so bash cannot be confined) and any server-side deadline
primitive (so run timeouts must be external).

## Ground truth

Read from a clone of `deepseek-ai/deepseek-harness` at `0.1.0-rc.7`.

| Source | What it settled |
|---|---|
| `python/sdk-runtime/package.json` | The exe's real plugin closure. The central fact above. |
| `docs/config-catalog.md` | Authoritative plugin config. 3,151 lines, CI-verified. |
| `packages/sdk/protocol/src/types.ts` | The whole wire: 3 methods, 4 notifications. No approval, cancel, or session close. |
| `packages/sdk/server/src/index.ts:4,37` | stdout is protocol-only. `maxTokensAsSuccess` defaults to `false`. |
| `python/sdk/src/deepseek_harness/api.py` | `DeepSeekHarnessConfig`, `start_session`, `Session.run`, `RunResult`. |
| `packages/hooks/hook-protocol/README.md` | Hook wire: exit 2 blocks with stderr; `deny > ask > allow`. |
| `packages/core/session/src/types.ts:254,271` | `step/start` is `{turn, step}`; `assistant/message` carries the only `usage` on the wire; `tool/call.arguments` is a raw JSON string. |
| `packages/llm/llm/src/types.ts:135` | `TokenUsage` fields. |
| `packages/llm/token-meter/src/index.ts:44` | How to sum usage without double-counting reasoning tokens. |
| `packages/core/agent-loop/` | No `maxSteps` anywhere: a step ceiling must be external. |
| `packages/sdk/server/src/server.ts:71` | Every session event is forwarded verbatim as a `session.event` notification. |
| `packages/sandbox/*`, `packages/fs/fs-sandbox` | Modes, the fence, and what it does not cover. |
| `packages/compaction/*` | Compaction defaults and the tool-result pruner. |
| `packages/mcp/` | Only `mcp-client`. dsh consumes MCP servers; it does not expose one. |
| `packages/subagent/` | `subagent-claude-code`, `subagent-codex` — dsh delegates *outward*. This project is the inverse. |
| `examples/jsonrpc-agent/`, `examples/headless-agent/` | Working composition templates. |

## External research applied

- Anthropic, *Effective context engineering for AI agents* — subagents return a distilled
  1,000–2,000 token summary; compaction; just-in-time retrieval.
- Anthropic, *Effective harnesses for long-running agents* — agents declare victory prematurely;
  progress files survive context windows.
- `Work/harness/research/25-context-engineering.md` — the 7-section summary contract, convergent
  across oh-my-pi, pi-mono, OpenCode and Hermes.
- `Work/harness/crates/agent/src/{worker,loopgate}.rs` — timeout-then-kill, outcome precedence,
  tool-call fingerprint loop detection. All three ported.
- `Work/Vietnam-consulting/GoDucThanh` — separate caps per call type; a token estimator labelled
  "measured" that never was; budget-exhaustion must be its own outcome.
