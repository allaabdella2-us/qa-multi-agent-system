# Changelog

## 0.1.0 — first release

`qaas` — a multi-agent QA system built on the Claude Agent SDK. Sixteen roles:
ROUTER is the Python state machine, and fifteen agents map a codebase, find
defects, reproduce them into failing tests, file tickets, fix them and prove the
fix.

- **MAPPER** builds the system map everything else reads.
- **ARCHITECT, API, BROWSER, DBA, AUDITOR, SOCKET, GUIDE, LOAD** are the
  discovery layer, run concurrently up to the mode's cap.
- **REPRODUCER** turns a finding into a committed failing test; **TRIAGE**
  dedupes, scores and files.
- **FIXER → REVIEWER → VERIFIER** is the bounded fix loop; **REPORTER** writes
  up the run.

Two decisions shape everything: the orchestrator is Python rather than a prompt,
because a model cannot enforce a budget it is itself spending; and each agent is
its own top-level `query()`, which is what gives a real context boundary, a
per-agent tool allowlist and a per-agent cost number.

767 tests, offline and free by default — no API key, no network, no Docker.

### Enforcement, in code rather than in prompts

The §8.1/§8.2 write-permission matrix is enforced by one function,
`Guardrail._check_path`, reached from all three doors: `Write`/`Edit` through
`check()`, shell commands through `_check_bash`, and the `vcs` MCP server. A
command that mutates and whose destination cannot be resolved is refused, not
guessed at. Shell writes count against the diff budget. Denials return a reason
and land in the run ledger rather than killing the turn.

Everything an agent supplies that becomes argv is treated as a flag until proven
otherwise: a branch name may not be a refspec, a ref may not be an option, and a
path-shaped test selector is contained inside the target root before pytest sees
it. Spec references are read from inside the checkout or fetched from the
application under test, and nowhere else.
