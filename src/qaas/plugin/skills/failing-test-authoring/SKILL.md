---
name: failing-test-authoring
description: >
  Write the test that proves a defect exists and defines what fixing it means.
  TRIGGER - read BEFORE writing any test that captures a defect, and whenever
  the task mentions a failing test, regression test, acceptance criteria, or
  'write a test for this bug'. Do NOT write the test until the defect reproduces
  reliably. SKIP only when no test is being authored.
---

# Authoring the failing test

This single artifact gets used three times: evidence on the ticket, acceptance criterion for the fix, regression test afterwards. Write it for all three readers.

## It must fail for the right reason

Run it and **read the failure message**. A test that fails because of a typo in a fixture path is not evidence of anything, and it will be discovered by the person trying to fix the defect, who will then distrust the whole ticket.

The assertion message should state the contract: `assert "currency" in invoice, "spec marks Invoice.currency required"`.

## Assert the contract, never the bug

```python
# Right: passes when fixed.
assert len(response["items"]) <= limit

# Wrong: pins the defect in place forever.
assert len(response["items"]) == 30
```

The second passes today, fails when someone fixes the bug, and gets deleted as flaky. It is worse than no test.

## One defect, one test

A test asserting three things fails on the first and hides the other two. Separate tests, separate names.

## Name it for the contract

`test_orders_list_respects_limit_parameter` — a reader who sees this go green knows exactly what is now true. `test_api_01` communicates nothing and will not survive a refactor of the ledger.

## Self-contained

Sets up its own state, does not depend on test ordering, cleans up after itself. A test that only passes as part of a suite, or only after another test ran, is not a reproduction — it is a coincidence.

## Never weaken it later

The test defines success. If it seems wrong, that is an escalation, not an edit. A fixer who can edit the acceptance criterion has no acceptance criterion — this is exactly the symptom-fix failure the system is built to prevent.
