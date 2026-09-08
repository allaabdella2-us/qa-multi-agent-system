---
name: root-cause-vs-symptom
description: >
  Tell a fix that removes the cause from one that suppresses the symptom, from
  either side of the review. TRIGGER - read BEFORE writing a fix and BEFORE
  judging one, and whenever the task mentions root cause, symptom, why the
  defect happens, a try/except or null check added by a fix, a special case, or
  a widened type. Do NOT decide a change is a real fix because the test went
  green; symptom fixes turn tests green, that is what makes them dangerous. SKIP
  only when no fix is being written or reviewed.
---

# Root cause or symptom

A symptom fix makes the failing test pass while leaving the defect in the system, usually in a shape that will come back under a slightly different input. §10 names this as the failure mode with the most controls pointed at it, because it is the one that looks most like success.

Both agents use this skill. MENDER reads it **before writing**; ARBITER reads it **before judging**, and records the answer in `record_review`'s `root_cause_addressed`.

## The two questions

1. **If the defining test did not exist, would this change still make the program correct?** A change that is only justified by the test is a change aimed at the test.
2. **Would this change have prevented the defect if the input had been slightly different?** A fix that survives only the exact reproduced input is a special case wearing a fix's clothes.

A change that fails either is a symptom fix, whatever the diff looks like.

## Signatures of a symptom fix

Each of these is a shape you can see in a diff without understanding the domain. Seeing one is not proof — it is the point where you must be able to justify it.

| In the diff | Why it is suspect | What the real fix usually is |
|---|---|---|
| `try/except` wrapped around a call that only raises because the caller passed something it built wrong | The caller is the bug; the handler now hides it | Fix what the caller constructs, or validate at the boundary that produced it |
| `if order_id == 31:` / `if len(items) == 1:` — the exact value or size from the reproduction | The test's input is now a branch in production code | The boundary is the clue, not the case: an off-by-one, an empty-case branch, a `>` that should be `>=` |
| A field or parameter widened — `int` → `int \| None`, a model field made optional, an annotation relaxed to `Any` | The type was the contract, and the fix moved the contract to fit the bad data | Find who produces the bad value and stop them producing it |
| `if user is None: return []` where the user cannot legitimately be absent | The null is the defect. Returning empty makes it silent | Trace where the lookup failed; usually a missing filter, a wrong key, or a session not populated |
| `.get(key, default)` replacing `[key]`, where a missing key means an upstream bug | Converts a loud failure into wrong data | Fix the producer that omits the key |
| `except Exception:` with a `pass` or a `logger.warning` | Every future defect on this path is now invisible | Catch the specific expected exception at the boundary where it is expected; let the rest propagate |
| A retry, a `sleep`, or a re-fetch added around an intermittent failure | Races do not get slower, they get rarer | Find the ordering or shared-state assumption that is wrong |
| A snapshot, fixture or expected constant edited to match observed output | The bug has been written down as the spec | The contract decides the expected value; see `test-quality-audit` |
| The defining test edited in any way | Not a fix at all | Immediate escalation; MENDER may not do this |

**Defensive code is not automatically a symptom fix.** A null check, a type coercion or a `try/except` at a *trust boundary* — deserialising external input, a third-party response, a user-supplied payload, a cache miss — is correct engineering. The distinction is whether the bad value was produced *inside* the system by code you control. Inside, a guard hides a bug; at the edge, a guard is the design.

## Tracing from the failure to the cause

Do this before proposing a fix, and again when reviewing one that skipped it.

1. **Start from the wrong value, not the exception.** The stack trace tells you where it surfaced. The assertion's *actual* value tells you what is wrong. Write it down.
2. **Name the invariant that was violated**, in one sentence, in domain terms: "every order row returned belongs to the session's tenant", "the total equals the sum of line items", "a placed order is never left in `draft`". If you cannot name it, you do not yet know what is broken.
3. **Find the last point where the invariant held.** Walk backwards from the failure through the frames — reading the code, or with `run_single` on a narrower test — until you reach a frame where the state is still correct. The defect lives between that frame and the next.
4. **Ask where the bad value entered.** Keep walking up while the value is only being *passed*. Stop at the frame where it is *created, defaulted, parsed, or filtered*. That frame is the cause. A value that is wrong three frames deep and wrong at every frame above is one bug at the bottom, not three.
5. **Check the blast radius of the cause.** Ask what else calls the frame you landed on, and what other symptoms that cause could produce. If your fix handles only the symptom in the ticket, you have found the cause and fixed a leaf.
6. **State cause and mechanism in one line** for the PR body: *"`list_orders` builds its query without the tenant filter, so any session sees every row; the fix adds the filter the sibling `get_order` already applies."* If you cannot write that line, do not open the PR.

## MENDER: using this before you write

- Do the trace first. The trace is cheap; a rejected round trip is not — the loop breaker allows two MENDER→ARBITER round trips per ticket and then escalates.
- When the trace lands on a forbidden class (auth, migrations, payment, billing, infra), stop there. Say what the cause is and what you would change. A symptom fix outside the forbidden path, chosen because the real fix was inside it, is the worst available outcome: it looks compliant and it is not a fix.
- When the cause is real but the fix is bigger than the budget, that is `minimal-diff-discipline`'s escalation, not a licence to patch the symptom instead.

## ARBITER: using this when you read

- Decide `root_cause_addressed` explicitly. It is a field on `record_review`, and a review that leaves it unconsidered has skipped the one check that separates you from a linter.
- Demand the cause sentence. If the PR body does not say *why* the defect happens, you cannot judge whether the change addresses it, and `REQUEST_CHANGES` naming that absence is a legitimate, cheap review.
- Test the fix against a neighbouring input, on paper: same code path, one field different. If your description of what happens then is "it would fail again", the fix is a special case.
- A symptom fix with real tests and a small diff is still a symptom fix. Minimality and green tests are not evidence about causation.
- When the diff is a symptom fix and the real fix is outside MENDER's envelope, the verdict is `ESCALATE_TO_HUMAN`, not `REQUEST_CHANGES` — sending it back asks for a change the other agent is not permitted to make.
