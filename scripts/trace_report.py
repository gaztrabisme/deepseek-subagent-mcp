#!/usr/bin/env python3
"""Read a decision/run trace and answer the questions the wiki leaves open.

    uv run python scripts/trace_report.py [path/to/trace.jsonl]

Defaults to `<DSA_SESSION_ROOT>/trace.jsonl`, or `.dsh-sessions/trace.jsonl`
under the working directory. Standard library only, so it runs anywhere the
trace does.

It reports the three things nobody has measured:

  1. How often the classifier escalates, and on what. A high rate on the same
     handful of reasons is the false-positive tail -- routine work that costs a
     model call every time.
  2. Whether DSA_CHARS_PER_TOKEN matches what the provider actually reports.
  3. Whether DSA_MAX_STEPS and DSA_TURN_TOKEN_BUDGET sit anywhere near real use.

Nothing here decides anything. It puts numbers next to settings that were
picked by judgement, so the next choice can be made from data.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path


def load(path: Path) -> list[dict]:
    records = []
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                print(f"  (skipped unparseable line {number})", file=sys.stderr)
    return records


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


def rule(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


def report_verdicts(verdicts: list[dict]) -> None:
    rule(f"Verdicts ({len(verdicts)})")
    if not verdicts:
        print("  none recorded")
        return

    by_tier = Counter(v["tier"] for v in verdicts)
    by_action = Counter(v["action"] for v in verdicts)
    escalated = [v for v in verdicts if v.get("escalated")]

    for tier, count in by_tier.most_common():
        share = 100 * count / len(verdicts)
        latency = [v["latency_ms"] for v in verdicts if v["tier"] == tier]
        print(f"  {tier:14} {count:5}  {share:5.1f}%   "
              f"median {percentile(latency, 0.5):8.1f}ms  "
              f"p95 {percentile(latency, 0.95):8.1f}ms")
    print("\n  actions: " + ", ".join(f"{a}={c}" for a, c in by_action.most_common()))
    print(f"  escalation rate: {100 * len(escalated) / len(verdicts):.1f}% "
          f"({len(escalated)} of {len(verdicts)})")

    if escalated:
        rule("What escalated, most common first")
        print("  These are the false-positive candidates: work the classifier could")
        print("  not settle. Anything routine and frequent here belongs in a rule.")
        reasons = Counter(v["reason"].split(" (")[0][:88] for v in escalated)
        for reason, count in reasons.most_common(12):
            allowed = sum(
                1 for v in escalated
                if v["reason"].startswith(reason[:40]) and v["action"] == "allow"
            )
            print(f"  {count:4}x  [{allowed} allowed]  {reason}")

    denied = [v for v in verdicts if v["action"] == "deny"]
    if denied:
        rule(f"Denials ({len(denied)})")
        for reason, count in Counter(v["reason"][:88] for v in denied).most_common(10):
            print(f"  {count:4}x  {reason}")


def report_calibration(calibrations: list[dict]) -> None:
    rule(f"Chars per token ({len(calibrations)} distillation turns)")
    if not calibrations:
        print("  none recorded -- no answer has overflowed DSA_SUMMARY_TOKENS yet")
        return
    observed = [c["observed"] for c in calibrations]
    assumed = calibrations[-1]["assumed"]
    print(f"  assumed (DSA_CHARS_PER_TOKEN): {assumed}")
    print(f"  observed: min {min(observed):.2f}  median {percentile(observed, 0.5):.2f}  "
          f"p95 {percentile(observed, 0.95):.2f}  max {max(observed):.2f}")
    median = percentile(observed, 0.5)
    drift = (median - assumed) / assumed * 100
    verdict = "close enough" if abs(drift) < 10 else "worth changing"
    print(f"  median is {drift:+.0f}% from the assumption -- {verdict}")
    if len(observed) < 10:
        print(f"  {len(observed)} samples is not a distribution. Treat this as a hint.")


def report_runs(runs: list[dict]) -> None:
    rule(f"Runs ({len(runs)})")
    if not runs:
        print("  none recorded")
        return

    states = Counter(r["state"] for r in runs)
    for state, count in states.most_common():
        print(f"  {state:22} {count:4}  {100 * count / len(runs):5.1f}%")

    verified = [r for r in runs if (r.get("verification") or {}).get("command")]
    if verified:
        passed = sum(1 for r in verified if r["verification"]["passed"])
        print(f"\n  verification ran on {len(verified)} runs, passed {passed} "
              f"({100 * passed / len(verified):.0f}%)")
        failures = Counter(
            r["verification"]["reason"][:70] for r in verified if not r["verification"]["passed"]
        )
        for reason, count in failures.most_common(5):
            print(f"    {count:3}x  {reason}")

    distilled = [r for r in runs if r.get("distilled")]
    print(f"  distilled {len(distilled)} of {len(runs)} "
          f"({100 * len(distilled) / len(runs):.0f}%)")
    for run in distilled[:5]:
        print(f"    {run['raw_chars']:>7} -> {run['result_chars']:<7} chars  {run['run_id']}")

    rule("Ceilings against actual use")
    steps = [r["usage"]["steps"] for r in runs if r["usage"]["steps"]]
    tokens = [r["usage"]["total"] for r in runs if r["usage"]["total"]]
    seconds = [r["elapsed_seconds"] for r in runs if r.get("elapsed_seconds")]
    for label, values, knob in (
        ("steps", steps, "DSA_MAX_STEPS"),
        ("tokens", tokens, "DSA_TURN_TOKEN_BUDGET"),
        ("seconds", seconds, "DSA_RUN_TIMEOUT"),
    ):
        if not values:
            continue
        print(f"  {label:8} median {percentile(values, 0.5):10.1f}  "
              f"p95 {percentile(values, 0.95):10.1f}  max {max(values):10.1f}   ({knob})")
    if steps:
        print("\n  A ceiling wants headroom over p95, not over the median. On this")
        print(f"  sample that is roughly {int(percentile(steps, 0.95) * 3) or 1} steps "
              f"and {int(percentile(tokens, 0.95) * 3) if tokens else 0} tokens.")


def main() -> None:
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        root = Path(os.environ.get("DSA_SESSION_ROOT") or ".dsh-sessions")
        path = root / "trace.jsonl"
    if not path.is_file():
        sys.exit(
            f"no trace at {path}\n"
            "Set DSA_TRACE to its path, or pass it as an argument. "
            "Tracing is on by default unless DSA_TRACE=off."
        )

    records = load(path)
    print(f"{path}  ({len(records)} records)")
    by_kind = {kind: [r for r in records if r["kind"] == kind]
               for kind in ("verdict", "run", "calibration")}
    report_verdicts(by_kind["verdict"])
    report_calibration(by_kind["calibration"])
    report_runs(by_kind["run"])
    print()


if __name__ == "__main__":
    main()
