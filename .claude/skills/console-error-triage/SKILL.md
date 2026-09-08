---
name: console-error-triage
description: >
  Decide which browser console output is a defect and which is noise. TRIGGER -
  read BEFORE reporting anything seen in a browser console, and whenever the
  task mentions console errors, warnings, unhandled rejections, or JavaScript
  errors. Do NOT report console output without triaging it - warnings are not
  defects and filing them destroys precision. SKIP only when no console output
  is involved.
---

# Triaging console output

## The classification

| Seen | Verdict |
|---|---|
| **Unhandled promise rejection** | **Always a defect.** A code path with no error handling, running in production. |
| **Uncaught exception during a normal journey** | **Defect.** Whatever came after it did not run. |
| **Failed network request the UI does not surface** | **Defect.** The user sees a blank or stale page and no explanation. |
| `console.error` from application code | Usually a defect — the developer marked it as one. |
| React key warnings, deprecation notices | Tech debt at most. Not a bug. |
| `console.warn` from application code | **Not a defect** unless it names a real failure. Often deliberate signposting. |
| Dev-only output behind an environment check | **Not a defect.** Never reaches users. |
| Extension noise, source map warnings, favicon 404 | Not a defect. Not even the app. |

## Read before you act

Capture the console **before** interacting. An error already present on load did not come from your click, and attributing it to your click sends someone hunting in the wrong handler.

## Dev-only is the trap

Output behind `if (import.meta.env.DEV)` or `NODE_ENV !== 'production'` is invisible to users by construction. Reporting it as a defect is a false positive, and a particularly damaging one because it signals the finder did not read the surrounding line.

## Report the consequence, not the text

A stack trace pasted into a ticket is not a finding. What did the user lose? "The orders list stays permanently blank after a failed request, with no error shown and no retry" is actionable. "TypeError: cannot read property 'map' of undefined" is a symptom that could arise ten ways.

Attach the trace as evidence. Lead with what broke.
