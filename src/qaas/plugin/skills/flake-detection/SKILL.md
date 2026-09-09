---
name: flake-detection
description: >
  Measure whether a test outcome is deterministic or intermittent before
  trusting it. TRIGGER - read BEFORE recording any reproduction verdict, and
  whenever the task mentions flakiness, intermittent failures, 'sometimes it
  passes', reliability, or repeated runs. Do NOT record a verdict from a single
  run. SKIP only when no verdict is being recorded.
---

# Measuring flake

A single run tells you what happened once. Reproduction verdicts require knowing what happens repeatedly, and this is the check that separates a real defect from a coincidence.

## The measurement

Run the test N times (the run's `flake_runs`, default 5) with `run_n_times`, unchanged, in the same environment. The flake rate is the share of runs whose outcome differs from the majority.

- **0.0** — deterministic. Verdict `reproduced` (or the finding is genuinely absent).
- **Anything above 0.0** — verdict `flaky`. Record the actual rate; do not round it to zero because it "mostly fails".

## Flaky is its own verdict

A flaky defect is not a reproduced defect with a caveat. It goes to the quarantine queue, not into the normal ticket flow, because an intermittent failing test poisons a fix cycle: the fixer sees it pass, closes the ticket, and the defect ships.

Recording 0.0 for something that failed four times in five is the single most damaging thing to get wrong here. It converts a quarantine item into a confident ticket and an acceptance criterion nobody can rely on.

## Sources of flake, worth distinguishing

- **Timing** — races, unawaited work, fixed sleeps. Usually a real defect wearing a flake costume.
- **Shared state** — a previous run's data. Usually a fixture problem, not a product problem.
- **Clock or ordering dependence** — real defects that only surface under specific conditions.
- **The environment** — a slow container, a cold cache. Not the product's fault, and worth saying so.

Say which you believe it is, and say that you are inferring. The distinction changes who picks the ticket up.

## More runs when it matters

Five runs cannot distinguish 0.05 from 0.0. For a defect that would be blocker or critical if real, run more before recording a confident verdict — the cost of the extra runs is trivial against the cost of a wrong verdict either way.
