---
name: ticket-writer
description: >
  Compose a ticket in the house format so an engineer can act on it without
  asking questions. TRIGGER - read BEFORE writing any ticket title, description,
  or acceptance criteria, and whenever the task mentions filing, ticket text,
  issue body, repro steps, or acceptance criteria. Do NOT write a ticket from
  memory of what tickets look like. SKIP only when no ticket text will be
  produced.
---

# Writing the ticket

The reader is an engineer who has not seen the finding, has four other tickets open, and will decide in about fifteen seconds whether this one is worth their attention. Write for them.

## Title

Name the **defect**, not the symptom, and not the location.

- Bad: `Bug in orders.py` - names a file, says nothing
- Bad: `Page is blank` - names a symptom, could be twenty causes
- Good: `Order detail endpoint is not scoped to the caller's organization`
- Good: `Place order does nothing when the cart holds a single item`

Under 90 characters. A reader should be able to tell from the title alone whether it is their problem.

## Body, in this order

1. **What breaks, for whom, how often.** Two or three sentences. Lead with the user-visible consequence, not the code.
2. **Reproduction.** REPRODUCER's steps, verbatim. Do not paraphrase them, do not tidy them, do not renumber. They were minimised deliberately and every edit risks breaking the reproduction.
3. **Evidence.** Links to the artifacts. Say what each one shows.
4. **Acceptance criteria.** The failing test that must pass, named exactly. This is the contract: when that test is green the ticket is done, and there is nothing to argue about.
5. **Suggested area** if you have one, clearly marked as a suggestion. You are not the person fixing it and you may be wrong about where.

## Rules

- **Never state a cause you have not verified.** "Probably a missing await" in a ticket becomes an hour spent looking at awaits. Say what was observed; leave diagnosis to whoever fixes it.
- **Include what you ruled out.** "Reproduces for member and viewer roles but not admin" is worth more than three paragraphs of speculation.
- **Say what you did not check.** A ticket that admits its own limits is trusted; one that overstates gets discounted entirely, including the parts that were right.
- **No severity argument in the body.** The severity field carries it, scored by `severity-rubric`.
