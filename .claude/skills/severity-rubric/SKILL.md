---
name: severity-rubric
description: >
  Score a defect's severity against the house rubric (blocker, critical, major,
  minor, trivial). TRIGGER - read BEFORE writing any severity value into an
  envelope, a ticket, or a verdict, and whenever the task mentions severity,
  priority, impact, 'how bad is this', triage scoring, or which findings matter
  most. Do NOT score from intuition or from how interesting the bug was to find;
  this skill is the only authority on severity in this system. SKIP only when no
  severity field will be written.
---

# Scoring severity

Severity is a claim about **consequence to users**, not about how the defect was found, how clever the finding was, or how confident the reporter sounded. Score it as the person who has to prioritise the fix would.

| Severity | The test it must pass | Examples |
|---|---|---|
| **blocker** | Data loss, a security breach, or a core flow fully broken on the production path. Someone is paged. | Cross-tenant read of another org's data; a migration that drops a column; auth bypass on a handshake |
| **critical** | A major feature is unusable, there is no workaround, and most users hit it. | Checkout fails for every single-item cart; refund endpoint accepts any role |
| **major** | Degraded or broken for a subset, and a workaround exists. | Missing index causing an 8s page load; a list endpoint returning every row |
| **minor** | Cosmetic, edge case, or low frequency. | Focus ring missing on one button; 200 returned where 404 belongs |
| **trivial** | Polish, cleanup, not user-facing. | Unused import; dead code |

## The four questions, in order

1. **Can data be lost, corrupted, or exposed to someone who should not see it?** If yes, it is blocker. Stop here — nothing below downgrades this.
2. **Is there a workaround a user could actually discover?** No workaround pushes up a level; an obvious one pushes down.
3. **What fraction of users hit it, on what fraction of attempts?** "Every user, every time" and "one user, once" are different defects even with identical symptoms.
4. **Is the failure silent?** A silent failure outranks a loud one at the same blast radius, because nobody reports it and nobody trusts the result. A button that does nothing is worse than a button that shows an error.

## Traps

- **Do not score by effort.** A defect that took four hours to reproduce is not more severe for it.
- **Do not score by domain.** Accessibility failures are real defects scored on the same scale — minor when they inconvenience, major when they block a user from completing a task, and never dismissed because they are "just a11y".
- **Do not inflate to get attention.** A rubric everyone games is a rubric nobody reads. If severity keeps landing on critical, the rubric is being used as an argument rather than a measurement.
- **Do not deflate to seem measured.** Understating a blocker is the more expensive error of the two.
- **Security findings** are scored on impact like anything else, but routing is separate and non-negotiable — see `routing-rules`.

## Confidence is not severity

They are independent axes and conflating them is the most common scoring error. A defect can be a near-certain minor (confidence 0.95, severity minor) or a suspected blocker (confidence 0.5, severity blocker). Score the consequence if the finding is true; record separately how sure you are that it is.
