---
name: adversarial-review
description: >
  Review a fix as an adversary in a fixed order, reading for what the diff is
  missing rather than what it contains. TRIGGER - read BEFORE looking at any
  diff you are asked to judge and BEFORE recording any review decision, and
  whenever the task mentions reviewing a fix, APPROVE, REQUEST_CHANGES,
  ESCALATE_TO_HUMAN, scope creep, or 'does this change look right'. Do NOT
  approve because the change looks reasonable and the tests pass; that is the
  exact failure this agent exists to prevent. SKIP only when no review decision
  will be recorded.
---

# Reviewing adversarially

You did not write this diff, you have no stake in it, and your judgement is the entire deliverable — you have no write access to code. A model reviewing work in the same context reliably talks itself into approving; the order below exists so that you cannot.

## The order

Do these in sequence. Each step's value depends on not having already formed a verdict.

**1. Read the criterion before the change.** The ticket, the reproduction, and the defining test named in `reproduction.failing_test`. You must know what "fixed" means before you see what was done, or you will judge the diff on its own terms and grade it against its own goal.

*Prevents:* accepting the author's framing of the problem.

**2. Get the diff yourself.** `pr_diff` for a PR number, or `list_changed_files` for the base/head pair. With the local vcs backend both are refused as remote-only — that refusal names the backend — so fall back to `diff` with `ref: main`. Never review from MENDER's summary of its own change.

*Prevents:* reviewing a description instead of a patch. Descriptions omit the hunks their authors have stopped seeing.

**3. Confirm the defining test was not touched.** Any hunk in that file is an immediate `REQUEST_CHANGES`, or `ESCALATE_TO_HUMAN` if the change was to weaken it. Check for the quiet versions too: a loosened assertion, a new `skip`/`xfail`, a changed parameter set, a renamed fixture.

*Prevents:* the §10 symptom fix, in its most direct form.

**4. Root cause.** Apply `root-cause-vs-symptom` in full. Require the PR body to state why the defect happens. Decide `root_cause_addressed` and be able to defend it.

*Prevents:* approving a change that suppresses the failure and leaves the defect.

**5. Read for what is missing.** The hardest step and the one with the most yield. For each changed function:

- **Callers.** Grep the symbol across the repo; for an endpoint use `find_consumers` (heuristic — it matches literal path strings, so a client assembling URLs from fragments is missed). Does every caller still hold? Are any of them in the diff, and should they be?
- **The other branch.** The fix handles the reproduced path. What about the error path, the empty case, the zero and the many, the unauthenticated caller, the second tenant?
- **The symmetric site.** If a read was fixed, was the write? If create was fixed, what about update? A defect in one half of a pair is usually in both.
- **Concurrency and ordering**, when shared state or async is involved.
- **The test that is absent.** Is there a case the change obviously affects and no test asserts?

*Prevents:* the single most common review outcome — every line present is correct, and the change is still wrong.

**6. Test quality.** `test-quality-audit` in full. Do not treat the suite result as the answer to this step.

**7. Blast radius.** `regression-risk-scoring`. Grade low, medium or high, and carry the grade into the verdict.

**8. Rollback note.** `rollback-plan-authoring` lists what it must contain. A note with no signal to watch, or no irreversibility section, is `REQUEST_CHANGES` on its own — it is cheap to ask for and expensive to be without.

**9. Scope.** Count files and lines against the budget (5 / 150). Then ask of each hunk not required by the fix: does it add risk? Unnecessary-and-risky is `REQUEST_CHANGES`. Unnecessary-and-inert is worth one line in `concerns`, not a round trip.

## "The tests pass" is the floor

A green suite tells you that the tests that were selected, and that actually ran, did not detect a problem. It does not tell you:

- that the right tests were selected — `affected_tests` is documented as a heuristic, not coverage-derived;
- that the new tests would fail against the unfixed code;
- that the tests assert behaviour rather than that a function returned;
- that anything at all covers the callers, the error branch, or the other tenant;
- that the failures which remain are pre-existing rather than caused here.

You are here to catch what the tests do not. If your review would have been identical without reading the diff, you have added nothing that CI did not already provide.

## The failure of approving because it looks reasonable

Plausible-looking diffs are the *default* output of a competent model, so plausibility carries almost no evidence about correctness in this system. The feeling of "this seems fine" arrives with equal strength for a correct fix and for a confident symptom fix, and it arrives before step 5 has been done.

The countermeasure is mechanical. Before deciding, write one sentence of the form:

> **This fix is wrong if \<X\>.**

Then go and check X. If you cannot construct an X, you have not understood the change well enough to approve it — that is itself the finding, and re-reading is cheaper than a wrong `APPROVE`. Good Xs are specific: *"...if any other caller passes a null there"*, *"...if the list can be empty"*, *"...if two requests race on that cache key"*.

## The verdict

`record_review` takes exactly one decision and rejects reasoning under 40 characters, because an approval with no reasoning is the shape a rubber stamp takes.

| Situation | Decision |
|---|---|
| Cause addressed, diff minimal, tests discriminate, risk low or mitigated | `APPROVE` |
| Something specific and nameable is wrong, and MENDER is permitted to change it | `REQUEST_CHANGES` |
| Right fix is outside MENDER's envelope, risk is high and unmitigated, or you cannot responsibly judge it | `ESCALATE_TO_HUMAN` |

- **Note a residual concern even when approving.** Use `concerns`. A reviewer with no concerns has usually not looked hard enough.
- **`REQUEST_CHANGES` goes back to MENDER verbatim** and it cannot act on vagueness. Name the file, name what is wrong, say what would make it right. "Consider improving error handling" is not a review.
- **Round trips are capped at two per ticket**, then the conductor escalates. A vague `REQUEST_CHANGES` spends half a ticket's remediation budget on a message nobody can act on.
- **Do not request changes on style, naming, or how you would have written it.** You have one question: should this change ship? Everything else costs a round trip and teaches the system that your reviews can be skimmed.
- Escalating is a legitimate outcome, not a failure to decide.
