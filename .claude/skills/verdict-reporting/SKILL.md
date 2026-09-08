---
name: verdict-reporting
description: >
  Write a verification verdict a human can act on without re-running the work.
  TRIGGER - read BEFORE writing any verdict, verification comment, or ticket
  transition note, and whenever the task mentions reporting a verdict, VERIFIED,
  NOT_FIXED, REGRESSED, or verification results. Do NOT report a verdict without
  saying what was actually run. SKIP only when no verdict is being written.
---

# Reporting the verdict

## Structure

1. **The verdict word**, alone and first: `VERIFIED`, `NOT_FIXED`, or `REGRESSED`.
2. **What you ran** — the original test by name, how many affected tests, which journey.
3. **What you observed** — pass counts, and every failure by name.
4. **What you did not run**, and why.
5. **Residual risk**, if any.

## On NOT_FIXED, the delta is the deliverable

The next agent works from this and nothing else. Give it: the exact assertion that failed, expected versus actual, and whether the behaviour changed at all from before the fix.

"Still failing" is useless. "`test_orders_list_respects_limit` still fails: requested limit 5, received 30 rows. Unchanged from before the fix — the `.limit()` call is still absent from the query chain" is a fix in one reading.

## On REGRESSED, name the casualty

Which test, which behaviour, and whether it is related to the fix or coincidental. A regression report that does not identify what broke is an alarm with no address.

## Never overstate

If a step was skipped, say so. If the environment differed from the original in any way, say so. If a test passed on the second attempt after failing on the first, say so and treat it as flaky.

The value of this verdict rests entirely on it being the one report in the system that is never optimistic. A single VERIFIED that turns out to be wrong costs more credibility than ten honest NOT_FIXEDs — because after that, every closed ticket has to be re-checked by hand, which is the exact work this system exists to remove.
