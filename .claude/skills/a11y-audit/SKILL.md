---
name: a11y-audit
description: >
  Check a rendered page against WCAG for failures that block or exclude users.
  TRIGGER - read BEFORE reporting any accessibility issue, and whenever the task
  mentions accessibility, a11y, WCAG, screen readers, contrast, keyboard
  navigation, focus, or ARIA. Do NOT report an accessibility finding without
  naming the specific success criterion it violates. SKIP only when no rendered
  UI is in scope.
---

# Auditing accessibility

These are real defects, scored on the normal severity scale. Major when they block someone from completing a task; minor when they inconvenience. Never dismissed as cosmetic, and never inflated past what the rubric supports.

## What to check, in value order

1. **Every input has an accessible name** (WCAG 1.3.1, 4.1.2). A `<label for>`, an `aria-label`, or `aria-labelledby`. **A placeholder is not a label** — it vanishes on focus and many screen readers skip it. An unlabelled search box is announced as "edit text, blank".
2. **Contrast** (1.4.3). 4.5:1 for body text, 3:1 for large text and UI component boundaries. Compute the ratio and state it; "looks light" is not a finding.
3. **Keyboard reachability** (2.1.1, 2.1.2). Tab through the whole task. Everything clickable must be reachable and operable, and you must be able to get back out of every widget. A focus trap is major — it strands the user with no recovery.
4. **Visible focus** (2.4.7). If focus is invisible, keyboard navigation is guesswork.
5. **Meaningful order** (1.3.2, 2.4.3). Tab order should follow the visual order.
6. **Status messages announced** (4.1.3). An error shown only in red text, with no `role="alert"` or `aria-live`, does not exist for a screen reader user — they submit and hear nothing.
7. **Images and icon buttons** (1.1.1). Decorative images `alt=""`; meaningful ones described. An icon-only button with no accessible name is unusable.

## Name the criterion

Every finding states the number and what a user experiences: "1.3.1 — the orders search input has only a placeholder, so screen reader users hear an unlabelled text field and cannot tell what it filters."

The number makes it checkable. The consequence makes it worth fixing. A finding with only the number reads as compliance box-ticking; a finding with only the consequence is hard to verify.

## Precision

Check the specific element pair you are reporting, not the theme generally. A design where one button variant fails contrast and the rest pass is one finding, not a sweeping claim about the palette — and a sweeping claim will be dismissed along with the real instance inside it.
