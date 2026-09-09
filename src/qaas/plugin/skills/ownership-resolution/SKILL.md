---
name: ownership-resolution
description: >
  Resolve a file path or component to an owning team. TRIGGER - read BEFORE
  assigning a ticket, before recording the ownership section of a system map,
  and whenever the task mentions owners, teams, assignees, CODEOWNERS, or 'who
  should fix this'. Do NOT guess a team from a directory name; an unassigned
  ticket beats a misrouted one. SKIP only when nothing is being assigned.
---

# Resolving ownership

## Source of truth, in order

1. `CODEOWNERS` — the only authoritative source when it exists.
2. An explicit ownership manifest (`OWNERS`, service catalog, team annotations).
3. Nothing else. Not directory names, not commit history, not who last touched the file.

Commit history is a specific trap: the person who last edited a file is often the person who fixed someone else's bug in it, and routing to them is a small tax on being helpful.

## Matching

`CODEOWNERS` patterns are last-match-wins in most implementations — a later rule overrides an earlier one for the same path. Walk the file in order and keep the last match, not the first. Glob patterns match path segments; `web/src/routes/Orders*` covers `OrdersList.tsx` but not `web/src/components/OrderRow.tsx`.

For a finding spanning several files, resolve each and take the owner of the file where the **fix** most likely lands, not where the symptom appears. A UI symptom caused by an API defect belongs to the API team.

## When there is no owner

Record `null`, assign nothing, and say so explicitly in the ticket: "No owner recorded in CODEOWNERS for this path."

That sentence is doing real work. It tells a human exactly what to fix, and it converts a silent misroute into a visible gap in the ownership file. Guessing produces a ticket that sits in the wrong queue until someone notices, and it teaches the team that assignments from this system cannot be trusted.
