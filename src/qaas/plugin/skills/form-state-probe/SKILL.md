---
name: form-state-probe
description: >
  Test what a form does when validation fails, when submission fails, and when
  the user comes back. TRIGGER - read BEFORE testing any form, and whenever the
  task mentions forms, validation, input handling, submit behaviour, or lost
  input. Do NOT test only the successful submission - the failure branches are
  where the defects are. SKIP only when no form is in scope.
---

# Probing a form

The successful submission is tested. The failure branches are not, and that is where the defects concentrate.

## The sequence

1. **Submit empty.** Does validation fire at all?
2. **Fill everything correctly except one field.** Submit. Then — the critical check — **is everything else still there?**
3. **Fix the one field.** Submit. Does it now succeed?
4. **Make the server reject it** (duplicate value, stale resource). Is the input preserved this time too?
5. **Submit twice quickly.** One resource, or two?
6. **Navigate away and back.** Draft preserved, or silently gone?

## Lost input is the big one

Step 2 catches the most damaging form defect there is. A validation-failure branch that resets state to its initial value means a user who mistypes one field loses everything they entered. It is easy to write by accident, invisible to the developer who tests with two fields, and infuriating on a form with twelve.

Distinguish carefully: a form that clears on **validation failure** is a defect. A form that clears on **successful submission** is correct. Check which branch you actually triggered — reporting the second as the first is a false positive that undermines the real finding.

## Also worth checking

- **Validation timing.** Errors on every keystroke before the user has finished typing are their own defect.
- **Error placement.** An error at the top of a long form, with no indication of which field, is barely better than none.
- **Announcement.** Is the error reachable by a screen reader? See `a11y-audit`.
- **Double submit.** No disabled state during submission means duplicate resources.
- **Trimming.** Does a trailing space defeat validation?
