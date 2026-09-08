---
name: dedupe-strategy
description: >
  Decide whether a finding is already known before it becomes a ticket. TRIGGER
  - run BEFORE creating any ticket, without exception, and whenever the task
  mentions duplicates, 'have we seen this', existing tickets, recurrence,
  regression, or occurrence counts. Do NOT create an issue before completing
  this check; duplicate storms are the single fastest way for a team to stop
  reading anything this system files. SKIP never - if no prior defect matches,
  the check still ran and the answer was no.
---

# Deduplicating before filing

## Order of checks

1. **Fingerprint first.** `fingerprint` gives the structural identity: domain, class, service, endpoint, route, file paths with line numbers stripped. An exact match is a duplicate, full stop. The fingerprint deliberately ignores prose, so two agents describing one defect in different words collide by design.
2. **`search_similar` second.** This catches near-matches the fingerprint misses: same defect after a refactor moved the file, or the same root cause reached through a different endpoint.
3. **`get_occurrences` on any candidate.** An existing ticket tells you what to do next.

## What to do with a match

| Situation | Action |
|---|---|
| Exact fingerprint, ticket open | Increment occurrence, add the new evidence to the existing ticket. **Do not create.** |
| Exact fingerprint, ticket **resolved** | This is a **regression**, not a duplicate. File it as one, linked to the original, and say which ticket it regressed. |
| Near match, same root cause | Add to the existing ticket. Note explicitly why you judged them the same cause. |
| Near match, different root cause, similar symptom | Two tickets. Link them. Say in each why they are not the same defect. |
| No match | File it, then `record` it so the next run dedupes against it. |

## The judgment call

Similarity scores rank candidates; they do not decide. The real question is: **would one fix close both?** If yes, one ticket. If a maintainer would have to make two separate changes, two tickets even when the symptoms look identical.

Err toward merging. A merged pair that should have been split costs one comment; a split pair that should have been merged costs two engineers investigating the same bug and finding each other halfway.

## Recording

`record` is not optional and not a formality. A finding that is filed but not recorded will be filed again next run, by an agent that had no way to know. The occurrence count is also the signal that a defect is getting worse — a count climbing across runs is worth more attention than a single new finding.
