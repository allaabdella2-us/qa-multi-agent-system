---
name: exploratory-ui-walk
description: >
  Drive a product through real user journeys and notice what breaks. TRIGGER -
  read BEFORE opening a browser on a product, and whenever the task mentions
  exploring the UI, walking a journey, clicking through, testing a flow, or 'use
  the app and see what happens'. Do NOT start clicking without a route and a
  reset environment. SKIP only when not driving a browser.
---

# Walking the product

## Before the first click

Reset the environment and seed it. State left by the previous journey is the single largest source of false findings — a cart with items from the last walk turns a working page into a "defect".

Know which task you are performing and what success looks like, from the task graph. Wandering without a goal produces observations; walking a task produces findings.

## The loop, every step

1. **Read the page.** What is on it, what is interactive, what does it claim.
2. **Check the console.** Before acting, so you know what was already there.
3. **Act.** One interaction.
4. **Observe.** What changed — the page, the URL, the console, the network.

Skipping step 2 is how a pre-existing error gets attributed to your click.

## Where defects actually live

Not on the happy path — someone tested that. They live at:

- **Boundaries.** Zero items, one item, many. The single-item case is under-tested precisely because it feels the same as the many case to whoever wrote it.
- **Second attempts.** Submit, go back, submit again. Refresh mid-flow. Use the browser back button, which nobody tests and every user presses.
- **Failure branches.** Make validation fail, then look at what happened to the input already typed.
- **Interruptions.** Navigate away mid-request and back.
- **Roles.** The same journey as viewer, member, admin.

## Use the test ids

Elements carry `data-testid`. Use them rather than text or CSS position — text changes, layout shifts, and a walk that breaks on a copy edit is a walk that produces false findings forever.

## Judge as a user

Report what **fails, misleads, blocks, or excludes**. Not what you would have designed differently. Spacing you dislike, copy you would have written otherwise, a colour choice — none of these are defects, and filing them is how a team learns to ignore this system.

A button that does nothing when clicked is the highest-value find available: silent, undetectable by monitoring, and users blame themselves.
