---
name: routing-rules
description: >
  Send a ticket to the right project and audience. TRIGGER - read BEFORE
  choosing any project, component, or assignee, and whenever the task mentions
  routing, which project, security findings, restricted tickets, the product
  backlog, or tech debt. Do NOT route a security finding by intuition - a leak
  here is irreversible. SKIP only when no ticket is being filed.
---

# Routing

Route by **class**, never by severity. A trivial vulnerability still goes to the restricted project; a blocker UX problem still goes to the product backlog.

| Class | Destination | Why |
|---|---|---|
| `vulnerability`, or any envelope with `security_relevant: true` | **Restricted project only** | A public ticket describing an unpatched vulnerability is a disclosure. Irreversible. |
| `ux-friction` | Product backlog | Not a bug. An engineer cannot action "this flow is confusing"; a product owner can. |
| `tech-debt` | Debt backlog | Filing debt as a bug corrupts bug metrics and buries real defects. |
| `bug`, `regression`, `perf-regression` | Engineering, by component | The default path. |

## Security routing is absolute

If there is any doubt whether a finding is security-relevant, **it is**. Route it restricted and let a human downgrade it. The asymmetry is total: a security ticket wrongly filed as restricted costs someone a click, while a vulnerability wrongly filed in public cannot be taken back — it is indexed, cached, and in someone's notification history within minutes.

Never explain a vulnerability's exploitation path in a ticket that might be public. Never paste a leaked credential, token, or key into any ticket, restricted or not — reference where it was found instead.

## Ownership

Resolve component and team from the system map's ownership section (see `ownership-resolution`). Where the map records no owner, **file it unassigned and say so in the ticket**. A guessed assignee is worse than no assignee: it stalls in someone's queue while they work out it is not theirs, and the real owner never sees it.

## Ambiguity

If routing is genuinely unclear — a security-adjacent performance issue, a bug that is arguably product's call — **escalate rather than choose**. Routing mistakes are expensive to reverse and cheap to ask about.
