---
name: rollback-plan-authoring
description: >
  Write the rollback note that makes a fix safe to merge: what to revert, what
  to watch, and what cannot be undone. TRIGGER - read BEFORE opening a pull
  request or writing its body, and whenever the task mentions rollback, reverts,
  the PR body, what to monitor after merge, or irreversible effects. Do NOT
  write 'revert this PR if there are problems' and call it a rollback note - a
  note with no signal and no irreversibility section creates confidence without
  supplying safety. SKIP only when no pull request is being opened.
---

# Writing the rollback note

The note goes in the `body` of `open_pr`, alongside why the change is correct. Its reader is a human at 02:00 who did not write the fix and does not have your context. Everything below is written for that person.

A rollback note earns its place by answering four questions. Fewer than four is not a shorter note, it is an incomplete one.

## 1. What to revert

- The branch and the PR number, and whether reverting **that PR alone** restores the previous behaviour.
- If the change landed in more than one place — code plus a seeded fixture, code plus a flag flip — the revert order, because the wrong order can leave the system in a state neither version expects.
- Any change that must *not* be reverted with it (the regression test, usually: reverting a fix while keeping its test leaves a red suite, and that is normally what you want, so say it).
- If the fix is behind a flag, name the flag and its value. Flipping a flag is a faster rollback than a revert and the on-call needs to know it exists.

## 2. What to watch after merging

A signal, a direction, and a window. Two or three of them, not a list of everything measurable.

Choose them like this:

- **The thing that was broken.** The defect's own observable — the 500 rate on the endpoint, the order status after checkout, the empty list. If it comes back, the fix regressed.
- **The thing most likely to break from this change.** Look at your own diff and ask what the change could make worse: latency if you added a query, error rate at a call site whose input you narrowed, a downstream consumer if you changed a response field.
- **A window.** "First 30 minutes" or "the next full nightly" — a signal with no window is never checked and never cleared.

`Watch: the 500 rate on POST /v1/orders and the count of orders left in 'draft', first 30 minutes after deploy. Both should go to zero; the previous value was ~4% and ~12/hour.` That is actionable. "Monitor for errors" is not.

## 3. What is not reversible

This is the section that makes the note worth reading, and the one most often missing. `git revert` restores code. It restores nothing else.

| Irreversible effect | Why the revert does not undo it | What the note must say |
|---|---|---|
| Rows written or mutated while the fix was live | The data outlives the code | Which table/field, roughly how many rows, and whether the old code can read them |
| A migration run | Schema changes are forward-moving; a down-migration is a second risky change | This is outside your envelope entirely — escalate rather than ship |
| Poisoned caches | Reverted code reads values the new code wrote | Which cache, which keys, and how to bust them |
| Messages published, webhooks fired, emails or notifications sent | Already delivered to someone else's system | What was sent and to whom |
| Consumed sequences and external ids | Payment intents, invoice numbers, third-party records | What was created externally |
| Client-side persisted state — localStorage, a cached bundle, a service worker | Lives in the user's browser past the revert | What users will still be carrying, and whether it breaks the old code |
| A feature flag other systems began depending on | Flipping back changes their behaviour too | Who else reads the flag |

If none apply, say so explicitly: *"Nothing irreversible: the change is read-path only, writes no data, touches no cache."* An explicit "nothing" is information. Silence reads as "not considered".

## 4. What that means for the risk of the change

Close by joining the previous two sections. Reversibility, not diff size, is what sets the cost of being wrong: a one-line change that writes to a shared cache is riskier than a forty-line change in a leaf module, and the note is where you say so.

- Fully reversible, no persisted effects → the risk is bounded by the revert. Say that.
- Irreversible component present → name the mitigation before merge: put it behind a flag, add the backfill or cache-bust command *in the note as a runnable line*, or reduce scope to the reversible part.
- If a rollback would need a data repair you cannot describe, **the fix is not ready to ship alone**. Escalate rather than shipping with a note that admits the gap. A note that documents an unmanaged irreversible effect has transferred the risk to the reader without reducing it.

## Template

```
## Rollback
Revert: PR #<n> on fix/<ticket>. That alone restores previous behaviour.
Keep: the regression test in <path> (it will go red — that is expected).
Faster option: set <flag>=off, no deploy needed.

## Watch after merge
- <signal>, expect <direction>, within <window> (was <value>)
- <signal>, expect <direction>, within <window>

## Not reversible
- <effect, scale, and how to clean it up>   (or: "nothing — read path only")

## Risk
<one or two sentences: bounded by the revert, or bounded by <mitigation>>
```

ARBITER judges this note, and an unviable rollback plan is grounds for `REQUEST_CHANGES` on its own — see `adversarial-review`.
