---
name: minimal-diff-discipline
description: >
  Decide what belongs in this diff and what is a separate ticket, and what to do
  when the correct fix exceeds the budget. TRIGGER - read BEFORE the first edit
  of a fix and again before opening the pull request, and whenever the task
  mentions diff size, scope, the autonomy envelope, refactoring, 'while I was in
  there', or a file limit refusal. Do NOT carry an improvement along with a bug
  fix because it is obviously better. SKIP only when no diff is being produced.
---

# Keeping the diff minimal

Every line beyond the fix is a line a reviewer has to judge and a line that can break something. The budget is not a target to fill; most good fixes are one file.

## The budget is real and it is enforced

Your policy sets `max_diff_files: 5` and `max_diff_lines: 150` (§8.2).

- **Files are counted and enforced** by the PreToolUse hook, per *distinct* file, not per call — editing one file six times is one file. On the sixth distinct file the write is refused with: *"FIXER has already changed 5 files, which is its limit of 5 (§8.2). A fix this wide is outside the autonomy envelope: stop, and escalate with what you have found."* The refusal names the files already touched. It is not a rate limit to wait out; it is the envelope closing.
- **Lines are a declared budget, not a machine check.** Nothing stops you at 150. You hold it, and REVIEWER measures it. Treat crossing it exactly as you would treat the file refusal.
- **Forbidden classes stop you whatever the size.** Migrations, anything matching `*auth*`, payment, billing, secrets, `*.tf`, `*/infra/*`, Docker and `.github` are refused with the class named: *"...is outside FIXER's autonomy envelope: it is a database migration. Changes here need human approval (§8.2). Describe the change you would make and escalate instead of making it."* Do that literally — describe the change, do not find a path that misses the glob.

## What belongs in this diff

| What you are holding | In the diff? | Why |
|---|---|---|
| The line whose behaviour causes the defining test to fail | Yes | It is the fix |
| An import, constant or helper the fix requires | Yes | Not optional, not scope |
| The regression test | Yes | Part of the fix, not an extra |
| A second call site with the *same* root cause and the *same* one-line change | Yes — and say so in the PR body | Fixing one instance of a shared cause leaves the defect live |
| A different bug you noticed while reading | No | Name it in the PR body and your final report. You may not create tickets; `create_issue` is refused for you |
| A rename, extraction, dead-code removal, formatting, type hints | No | Separate ticket, however obviously correct |
| A debug print or log line you added while tracing | No | Delete it before you commit |
| A dependency bump | No | Its blast radius is not this ticket's |
| A test that was already failing on the base | No | Report it; fixing it hides which change fixed what |
| Anything under a forbidden path | No | Escalate with the change you would have made |

## Neighbouring correct code shows you the shape of the fix

The strongest signal for what a minimal fix looks like is code beside it that already gets it right.

1. Find the sibling. Grep the file and the module for the same decorator, the same route pattern, the same call to the collaborator that broke — a handler two functions down, the other branch of the same switch, the sibling serializer.
2. Read both, and diff them by eye. List every difference: a validation call, a tenant filter, an `await`, a default, an ordering.
3. The fix is usually one of those differences, restored. Prefer *making the broken one look like the working one* to inventing a mechanism neither of them uses.
4. Verify the neighbour is actually correct before you copy it. A pattern repeated twice may be the bug twice. Check it against the contract, not against its popularity.

Two things fall out of this for free: the diff stays small, and the reviewer can check it by reading the neighbour rather than reconstructing your reasoning.

## Refactoring carried along is scope creep

It is scope creep even when the refactor is genuinely better, even when it is smaller than the fix, and even when the fix would be cleaner after it. The reason is not aesthetics: a reviewer cannot tell which hunk changed behaviour and which only moved it, so the risk of the whole diff becomes the risk of its largest hunk. A revert then reverts the refactor too, which is exactly when you least want to be reading a large patch.

The test: **if the refactor were reverted, would the defining test still pass?** If yes, it does not belong here.

## When the correct fix genuinely exceeds the budget

This is a real and useful outcome. The budget is a statement about what a review can reliably bound, so exceeding it is a finding about the defect, not an obstacle.

Do **not** slice. Three PRs of four files each, or a fix landed in halves across two tickets, defeats the control entirely: each slice is individually unreviewable, none of them fixes the defect, and the risk a human was supposed to see never gets seen.

Escalate with the real scope, specific enough that an engineer can act on it without repeating your work:

- The full file list and an honest line estimate.
- Why each file is required — one clause each.
- The root cause, stated once, plainly.
- What blocks the small version. "It cannot be done in one file because the tenant id is not threaded past the repository layer" is useful; "this is complex" is not.
- Whether a forbidden class is involved, named.

"The defect is real and reproduces, but fixing it properly means changing the session model, which is outside my envelope" saves an engineer an hour. A plausible-looking change that does not fix the defect costs them a day, and costs this system their trust.
