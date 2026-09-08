---
name: verification-protocol
description: >
  Verify that a fix actually fixed the defect, in the right order, against the
  original criterion. TRIGGER - read BEFORE verifying any fix or transitioning
  any ticket toward done, and whenever the task mentions verification,
  confirming a fix, closing a ticket, VERIFIED, or 'did this work'. Do NOT
  declare anything verified without running the original failing test first.
  SKIP only when nothing is being verified.
---

# Verifying a fix

You are the closing authority. An incorrect VERIFIED puts a defect back in front of users with a ticket saying it was fixed — which is worse than never having filed it, because now nobody is looking.

## The order is not negotiable

1. **Same environment.** Bring up the patched build with the fixture, flags and role recorded in the original reproduction. A different environment proves nothing about this defect.
2. **The original failing test, first.** It must now pass. If it does not, the verdict is `NOT_FIXED` and you are finished — do not continue, do not investigate, do not decide the test was wrong.
3. **Regression suite for the affected area**, selected from the diff.
4. **Re-walk the journey** if UI or realtime behaviour was touched. A passing unit test does not mean the button works.

Running these out of order — regression suite first, or a fresh manual check before the original test — is how a fix that addressed a different symptom gets marked verified.

## The original test is the criterion

Do not write a new, more forgiving test. Do not edit the existing one. Do not accept "the test is outdated" as an argument — if you genuinely believe the acceptance criterion is wrong, that is an **escalation**, not a verdict, and certainly not an edit.

## Three verdicts, no fourth

- **VERIFIED** — original test passes, nothing else broke. Ticket to done, PR ready for a human to merge.
- **NOT_FIXED** — original test still fails. Reopen with the exact delta between expected and observed. Be specific; the next agent works only from this.
- **REGRESSED** — original passes, something else broke. Block and escalate, naming what broke.

"Verified with caveats" is not a verdict. If there are caveats, it is one of the other two.

## Re-run anything that smells flaky

A test that passes intermittently has not passed. Run it again before calling VERIFIED, and if it is genuinely flaky say so rather than taking the pass.
