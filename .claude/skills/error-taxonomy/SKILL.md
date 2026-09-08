---
name: error-taxonomy
description: >
  Audit error responses for a consistent shape, correct status codes, and no
  internal leakage. TRIGGER - read BEFORE reporting anything about error
  handling, and whenever the task mentions error shapes, status codes, error
  responses, stack traces, exception handlers, or 'what happens when it fails'.
  Do NOT judge an error path without triggering it. SKIP only when no error
  behaviour is in scope.
---

# Auditing error behaviour

## One shape, everywhere

Every error from one API should have the same envelope. Mixed shapes — some endpoints returning `{"error": {"code", "message"}}` and others the framework default `{"detail": "..."}` — force every client into defensive parsing, and clients that skip it break on the paths nobody tested.

Check the framework's default handlers specifically. Hand-written handlers are usually consistent; the defaults for validation errors, 404s, and unhandled exceptions are where the second shape leaks in, because nobody wrote them.

## Status codes carry meaning

| Situation | Code | Common mistake |
|---|---|---|
| Malformed syntax | 400 | |
| Well-formed, semantically invalid | 422 | Returned as 400, or as 200 with an error body |
| Not authenticated | 401 | Confused with 403 |
| Authenticated, not permitted | 403 | Returned as 404 — sometimes deliberate, to avoid confirming existence |
| Resource absent | 404 | **Returned as 200 with an empty body** — a silent failure clients cannot detect |
| Gone permanently | 410 | Correct and deliberate. Not a defect. |
| Server fault | 500 | Returned for client errors, hiding real faults in the noise |

A 200 on a failure is the worst of these. It is undetectable without reading the body, so retries never fire and monitoring stays green.

## Leakage

An unhandled exception must never return a traceback, file path, library version, SQL fragment, or local variable to a client. Each is a free map of the system for anyone probing it. Findings here are `security_relevant: true`.

Check the debug flag's effect too: an app that leaks only when `DEBUG=1` is one environment variable from leaking in production, and that is worth reporting as a configuration risk even where the production value is currently correct.

## Trigger the paths

Every claim about an error path needs the path actually triggered and the real response captured. Send the malformed body, request the missing id, call it unauthenticated. Reading the handler tells you what it intends; only calling it tells you what the framework does around it.
