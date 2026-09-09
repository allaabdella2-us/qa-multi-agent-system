---
name: test-first-fix
description: >
  Fix a defect test-first: run the defining test, watch it fail, and only then
  change code. TRIGGER - read BEFORE editing a single line of product code on a
  ticket, and whenever the task mentions fixing a defect, making a failing test
  pass, writing a regression test, or 'the ticket has a failing test'. Do NOT
  start editing on the strength of having read the test; a test you have not
  watched fail is a guess about what is broken. SKIP only when no code is being
  changed.
---

# Fixing test-first

The ticket carries `reproduction.failing_test` as a nodeid. That test is the definition of success and the order below is built around it. Running it only after the fix is how a change that fixes nothing ships with a green suite.

## 1. Run it and watch it fail

`run_single` on the nodeid, before any edit, in the environment the reproduction pinned — `seed`, `set_flag`, `set_clock`, `impersonate` from `reproduction.environment`. A different environment tells you about a different defect.

Read the failure text, not the colour. Write down the assertion that failed and the expected-versus-actual pair. That pair is what your change has to move, and it is the only thing that will later tell you whether it moved for the right reason.

| The first run | What it means | What you do |
|---|---|---|
| Fails on the ticket's assertion, expected vs actual as described | The defect is live and yours to fix | Proceed to step 2 |
| **Passes** | Already fixed, or the test does not capture the defect | Stop. Escalate. See below |
| Errors in collection or fixture setup | Broken environment, not evidence | Repair the environment and re-run; if you cannot, escalate |
| Fails with a message the ticket does not describe | Wrong nodeid, or a second defect on top | Resolve which before changing anything |
| Passes on some runs | Flaky. Confirm with `run_n_times`, n=5 | A flake is a quarantine finding, not a fix (§10) |

## When it passes before you have touched anything

This is the case the step exists to catch, and the wrong move is to make a plausible change anyway and claim the ticket. Both explanations are escalations, and they are different escalations, so say which one you believe and why:

- **Already fixed** — name the commit or change you think closed it. Transition the ticket with that reason rather than opening a PR that changes nothing.
- **The test does not capture the defect** — quote what the test asserts, quote what the reproduction describes, and state the gap.

You may not widen, weaken, retitle, or delete the defining test to make it fail. When the run lists that path in your policy's `protected_paths`, the write is refused outright — the guardrail answers that it "is the test that defines success for this ticket and may not be edited (§10)". When it is not listed, the rule holds anyway. A fixer that can edit its own acceptance criterion has no acceptance criterion, and this is the failure mode the system is most designed to prevent.

## 2. Make it pass for the right reason

Before you edit, be able to complete this sentence: *the test fails because \<cause\>, and my change makes it pass by \<mechanism\>*. If the mechanism is "I caught the exception", "I added a branch for that input", or "I made the field optional", you are about to write a symptom fix — stop and read `root-cause-vs-symptom`.

After the edit, re-run the same nodeid and confirm that **the failure you wrote down is the one that disappeared**. Green is not enough. A test goes green for the wrong reason when the code now returns early, when the assertion is no longer reached, when an exception is swallowed before it propagates, or when a fixture stopped producing the triggering data. Each of those looks identical in a summary line.

## 3. Regression test, proven against the old code

The defining test proves this defect is gone. The regression test states the contract that was violated, so the defect cannot come back in a different shape. They are not the same test — if your regression test is a copy of the defining test with a new name, you have added nothing.

Write it against the boundary the reproduction found. "Fails with one item, works with two" means the regression test is parameterised over 0, 1 and 2 items, not a second assertion about one item.

**Verify it fails against the unfixed code.** Two ways, in order of preference:

1. **Write the regression test before the fix.** Run it, watch it fail, keep the failure text, then fix. Nothing to undo, and the evidence is free.
2. **Stash the fix and re-run.** `git stash push -- <changed files>`, `run_single` on the new test, confirm it fails, `git stash pop`. Do not reach for `git reset --hard` or `git checkout main` — both are refused, and for good reason.

Record the observed old-code failure in the PR body. ARBITER cannot run this check itself (it has no shell and no write access), so your statement of it, with the failure message, is the evidence the review depends on.

## 4. Before you open anything

1. `affected_tests` on your changed paths, then `run_suite` on what it names. Its ranking is a documented heuristic, not coverage — widen it when the change is in shared code.
2. Establish pre-existing failures on the base first, or you will attribute someone else's breakage to your diff. `regression-suite-selection` covers the selection; use it.
3. Read the failures. A summary count is not a result.
4. Open the PR as a draft on your `fix/*` branch with a rollback note — `rollback-plan-authoring`. Never merge.

A green run you did not first watch turn from red is not evidence about this defect. It is evidence that a suite passes, which was already true yesterday.
