---
name: regression-risk-scoring
description: >
  Grade a change's blast radius low, medium or high, and turn that grade into a
  verdict. TRIGGER - read BEFORE recording a review decision on any diff, and
  whenever the task mentions blast radius, regression risk, breaking changes,
  call sites, contracts or schemas, response shape, status codes, shared state,
  or concurrency. Do NOT infer risk from the size of the diff; a one-line change
  to a shared serializer outranks forty lines in a leaf module. SKIP only when no
  change is being judged for risk.
---

# Scoring regression risk

Risk here means: **if this change is wrong, how much breaks, and how far from the diff?** It is independent of the defect's severity and independent of the diff's size. Score it before you decide, because the grade is what turns "I found nothing wrong" into a defensible verdict.

## The seven inputs

Work through all seven. Each has a way to measure it with the tools you have — reading and guessing is where reviews get this wrong.

| Input | How to measure it | Raises risk when |
|---|---|---|
| **Call sites** | Grep the changed symbol across the repo. For an endpoint, `find_consumers` (heuristic: it matches the literal path and the stable prefix, so a client assembling URLs from fragments is missed — a low count is weak evidence) | Many callers, or callers outside the changed module, or callers not covered by the tests that were run |
| **Contract and schema** | `diff_openapi` when the change touches an endpoint, then `classify_breaking` on each change it returns | Any change classified `breaking`. `unknown` is **not** `non_breaking` — it means the server has no rule, so judge it by hand and grade at least medium |
| **Response shape** | Read the handler's return and the model it serialises | A field removed or renamed, a type changed, nullability changed, ordering that a client relies on. Additive fields are usually safe; removals never are |
| **Status codes** | The handler's raises and returns, and the spec's declared responses | A code changed at all. Clients branch on status far more than on body |
| **Shared state** | Look for module-level values, caches, singletons, session or request-scoped globals, connection pools, class attributes | The change reads or writes anything a second request can also see |
| **Concurrency** | Async handlers, background tasks, locks, retries, anything with an implicit ordering assumption | The change alters ordering, adds a shared mutable, or replaces a race with a retry |
| **Persistence and irreversibility** | The rollback note, checked against the diff — see `rollback-plan-authoring` | The change writes data, mutates rows, or poisons a cache, so a revert does not restore the prior state |

Two more that override the grade rather than contribute to it:

- **A forbidden class in the diff** — a migration, anything auth, payment, billing, secrets, `*.tf`, infra, Docker, `.github`. These are refused at the guardrail, so their presence means the envelope was routed around. That is an immediate `ESCALATE_TO_HUMAN` regardless of everything else.
- **The critical journey.** A low-risk change on the checkout path still fails users if it is wrong. It does not raise the grade; it lowers your tolerance for an unresolved concern.

## The grades

| Grade | Looks like | What it implies for the verdict |
|---|---|---|
| **Low** | One function, private or module-local; every caller either in the diff or provably unaffected; no contract, schema, status or response-shape change; no shared state; read-path only; fully reversible | `APPROVE` when the fix is correct and minimal. Note any residual concern and move on |
| **Medium** | A shared helper or a module with several callers; an additive response field; a touched endpoint whose spec diff is `non_breaking`; writes data that a revert leaves behind but that is describable; two or more low markers together | `APPROVE` only with the callers' tests actually run, a viable rollback note, and named `concerns`. Otherwise `REQUEST_CHANGES` naming the specific gap — most often "the callers in \<file\> were not exercised" |
| **High** | Any `breaking` classification; a status code change; a removed or renamed response field; shared mutable state or concurrency; an irreversible effect with no mitigation; a forbidden class; or three or more medium markers | `REQUEST_CHANGES` when the gap is fixable within FIXER's envelope, otherwise `ESCALATE_TO_HUMAN`. A two-round-trip loop is not the right place to absorb high risk |

## How the grade meets the fix

Risk and correctness are separate axes and combining them is the point:

- **Correct fix, low risk** → approve.
- **Correct fix, high risk** → the change may still be right and still not be something this loop should land unattended. Escalate with the risk named; do not send it back for cosmetic changes that will not lower it.
- **Uncertain fix, low risk** → the cheapest resolution is usually a specific `REQUEST_CHANGES` asking for the missing test, not a debate.
- **Uncertain fix, high risk** → escalate. Two agents guessing at a high-risk change is exactly the case §8.4 reserves for a human.

## Traps

- **Diff size is not risk.** Score the reach, not the line count. The most dangerous diffs in this system are one line long.
- **Severity is not risk.** A blocker can have a low-risk fix; a minor cosmetic ticket can be fixed by editing a shared component. They are scored on different axes by different skills — severity by `severity-rubric`, risk here.
- **A heuristic's silence is not safety.** `find_consumers` and `affected_tests` both document themselves as heuristics. "No consumers found" means the search found none, and should lower your confidence in the search before it lowers the grade.
- **Do not grade from the PR body.** The author's account of the blast radius is the claim under review.
- **Say the grade out loud.** Put it in `record_review`'s `reasoning` with the two or three inputs that decided it. A grade nobody can audit is not a control, and VERIFIER reads your reasoning when it selects the regression suite.
