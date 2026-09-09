---
name: repro-minimisation
description: >
  Reduce a reported defect to the shortest sequence that still triggers it.
  TRIGGER - read BEFORE attempting to reproduce any finding, and whenever the
  task mentions reproduction, repro steps, minimisation, or 'can you make this
  happen again'. Do NOT record the reporter's steps as the reproduction without
  reducing them. SKIP only when no reproduction is being produced.
---

# Minimising a reproduction

The minimal reproduction is the most valuable artifact this system produces. It is read by whoever fixes the defect, becomes the acceptance criterion, and survives as the regression test.

## Reproduce first, minimise second

Get it happening reliably at least twice before removing anything. Minimising something you have seen once produces a shorter sequence that reproduces nothing, and you cannot tell whether your last cut fixed it or it was never deterministic.

## Then cut, one at a time

Remove one step, re-run, check the defect still appears. If it vanishes, put the step back — it was necessary. One change per iteration: cut two and you learn nothing about either.

Cut in this order, because it removes the most noise per step:
1. Steps before the defect's first observable symptom
2. Fields, items, and records beyond the minimum
3. Role and permission complexity — does it need admin, or any user?
4. Seeded data beyond what the steps touch

## What must survive

The reproduction must state the environment exactly: branch, fixture, flags, role, and clock if it matters. "Log in and go to orders" is not a reproduction — as whom, with what data?

If it only reproduces with a specific fixture, that is not a weakness to hide; it is a fact about the defect and possibly the most informative thing you learned.

## Boundaries are the signal

When the defect appears with one item and not two, **that is the finding**, and it is far more useful than the symptom. Record the boundary explicitly. It usually names the bug — an off-by-one in a guard, an empty-case branch, a comparison that should have been `>=`.

## Honest outcomes

Not reproducible is a real, useful verdict, and delivering it is the job working correctly. Passing through a finding you could not reproduce costs the team more than dropping a real defect: it teaches them that tickets from this system may be fiction, and that judgment applies to every ticket afterward.
