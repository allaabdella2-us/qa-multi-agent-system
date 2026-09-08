---
name: regression-suite-selection
description: >
  Choose which tests to run after a change, so verification is fast and still
  catches breakage. TRIGGER - read BEFORE selecting tests to run for a
  verification, and whenever the task mentions regression suite, affected tests,
  which tests to run, or test selection. Do NOT run the entire suite by default
  and do NOT run only the one test that was failing. SKIP only when no test
  selection is being made.
---

# Selecting the regression suite

Two failure modes bracket this, and both are common. Running everything is slow enough that verification gets skipped under time pressure. Running only the original failing test catches nothing — that test passing is the *premise* of the check, not the check.

## Build the set

1. **The original failing test.** Always. Already run in step 2 of the protocol.
2. **`affected_tests` on the diff.** Direct coverage of changed files.
3. **Tests for the changed module's callers.** A fix inside a shared helper breaks its consumers, not itself.
4. **Contract tests for any touched endpoint.** A fix that changes a response shape is a breaking change wearing a bugfix label.
5. **The journey the defect lived on**, end to end, if UI or realtime was involved.

## Widen when the diff is risky

Shared utility, auth, serialisation, or anything under a migration — widen substantially. A narrow selection over a wide blast radius is a verification that proves almost nothing, and it looks identical in the ledger to one that proves a lot.

## Pre-existing failures

Some tests were already failing before the fix. **Establish that before you start**, on the base commit, or you will attribute someone else's breakage to this change and reopen a correct fix.

Report them explicitly: "the suite passed except two failures also present on the base commit, unrelated to this change." Silently ignoring them is how a real regression hides among the known noise.

## Say what you did not run

A verdict that names its own coverage is trustworthy. "VERIFIED — original test passes, 34 affected tests pass, did not run the load suite" lets a reader judge the residual risk. A bare "VERIFIED" invites them to assume you checked more than you did.
