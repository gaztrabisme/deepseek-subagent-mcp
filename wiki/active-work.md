# Active work

Written as a handoff: a fresh session should be able to resume from this file plus `CLAUDE.md`
without reading any conversation history.

## State

Feature-complete against the production plan and live-verified. Public at
`github.com/gaztrabisme/deepseek-subagent-mcp`, CI green on macOS-14 arm64 and Linux x86-64
across Python 3.11 / 3.12 / 3.13.

| Area | State |
|---|---|
| Six MCP tools over stdio | done, live-verified |
| Hardened composition (19 plugin entries) | done, boot-tested by bisection |
| Supervised execution (classifier + tiered escalation) | done, live-verified |
| Agent reaping, run deadline, loop gate, kill precedence | done, live-verified |
| Token budget, step ceiling, per-run cost reporting | done, live-verified |
| Required verification, executed server-side | done, live-verified |
| Distillation on overflow, seven-section handoff | done, live-verified |
| Terminal-run archive | done, live-verified |
| Tasks-aligned state names, phases, progress reporting | done |
| stderr logging on every failure path | done |
| Packaging, ruff, GitHub Actions CI | done |
| Escalation ladder, both tiers | done, live-verified |
| Unit tests | 121 passing, ruff clean |
| Live smoke scripts | 6, all passing |

## Next, in the order I would do it

1. **Confirm which tier Claude Code itself provides.** The ladder is proved end to end against a
   client that advertises each capability (`tests/smoke_escalation.py`), so the code is no longer
   in question — but which rung a real Claude Code session lands on is still unmeasured. Add the
   server to a client and read `dsh_list → supervisor.tier`. If it reports `deterministic`,
   escalations deny rather than reaching anyone: safe, but noisier than intended.
2. **Measure the classifier's false-positive rate.** A red-team probe modelled on
   `GoDucThanh/cockpit/scripts/red_team.py`: adversarial-but-plausible tasks, every escalation
   denied, reporting what the child *attempted* with full arguments.
   `DSA_SUPERVISOR=allow-escalations` exists to benchmark this and is never for real work.
3. **Decide on PyPI.** Currently install-from-git. Publishing needs a name reservation and a
   release workflow; `uv build` already runs in CI and uploads the artifact.
4. **Watch for SEP-2577 removing sampling.** The top rung of the escalation ladder is deprecated
   as of the 2026-07-28 spec revision. It works today and an erroring tier now falls through to
   elicitation, so removal degrades rather than breaks — but it will change which tier real
   clients land on.

## Measured, not assumed

On macOS 26.5.2 / arm64 / Python 3.11.5, against `deepseek-v4-pro`:

- Runtime cold boot 6.7s. One-word turn 1.2s. FizzBuzz task with a file write, two bash calls and
  a passing verification 7.8s / 8,435 tokens / 3 steps. Follow-up turn in the same session 8.6s.
- **Chars per token 3.54**, from 2,033 characters against 575 provider-reported output tokens.
  `DSA_CHARS_PER_TOKEN` defaults to 3.50, so the cap is now calibrated rather than guessed. Worth
  re-measuring if the model or `reasoningEffort` changes.
- Distillation: an 8,438-character report became a 2,033-character seven-section handoff in 52.0s.
- Sandbox (D10): `write` to `$HOME` **denied**; `echo > $HOME/...` via bash **succeeded**;
  write inside the workspace succeeded.
- Supervisor (D12): `ls -la` and `python3 hello.py` allowed by policy with no model involved;
  `echo > $HOME/...` and `curl -o` both denied. The home write is the same call that defeated
  the sandbox.
- Reaper: with `DSA_MAX_AGENTS=1, DSA_IDLE_TIMEOUT=5`, a second delegation succeeds after the
  first is reaped, and the first run stays readable through the archive.
- Deadline: with `DSA_RUN_TIMEOUT=15` and the gate off, a `sleep 400` task ends `failed` with the
  timeout reason and leaks no runtime process.

## Open

- **Which supervisor tier Claude Code actually gets** — see next step 1. The ladder itself is
  proved; which rung a real client offers is not.
- **The classifier's false-positive rate is unmeasured.** Two denials seen in live runs were
  arguably noise: a compound bash line and `sleep`. Both were safe outcomes and the child adapted,
  but the rate matters and nobody has counted it.
- **`DSA_MAX_STEPS=40` and `DSA_TURN_TOKEN_BUDGET` unset are judgement, not measurement.**
  Observed runs used 3–4 steps and 8k–17k tokens, so 40 is roughly a 10× headroom. Revisit once
  there is a distribution rather than five samples.
- **Session rehydration across process restarts is unverified.** Nothing depends on it.
- **Windows, Intel macs, and macOS 13** are unsupported: no runtime wheel exists.
- **Upstream is a developer preview.** `deepseek-harness-sdk` is pinned at `==0.1.0rc7`; three
  releases exist in total and two release candidates shipped inside a week. Expect the wire to
  move.

## Deferred by decision

- **PyPI publication.** Install-from-git for now.
- **Adopting the MCP Tasks extension** — fully typed in `mcp` 2.0.0 but with no server-side
  implementation, so it means dropping to `mcp.server.lowlevel.Server`. The state vocabulary is
  already aligned, so the migration is mechanical when the SDK catches up.
- **A custom runtime executable including `dsh-bash-sandbox`** (needs Node ≥22.19 and pnpm;
  neither is installed). Would confine bash directly. Lower priority now that the supervisor
  covers it upstream of execution.
