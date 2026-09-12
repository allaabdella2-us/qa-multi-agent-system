# Changelog

## 0.0.2

### Reproduction now has a severity floor

`thresholds.reproduce_min_severity` (default `major`) decides which findings are
worth a REPRODUCER context. Reproduction is the one phase whose cost scales with
*findings* rather than agents, and each dispatch is a fresh frontier-model
context: a nightly run that found 85 issues, most of them minor, spent $45 there
and filed nothing.

That number is worth reading closely, because it says where each control
applies. `max_findings_per_agent_run` had already held that run to **25**
dispatches — 25 × ~$1.75 is the $45 — and the cap has always taken the most
severe findings first. So on a run that exceeds the cap, the cap is the lever and
this floor changes little; lower the cap if that is the run you have. The floor
is for the ordinary run that never reaches it, which is every run in this
project's own history: across 22 of them it takes reproduction from 132
dispatches to 98, roughly $231 to $172.

What that buys is measurable in the same 22 runs. Of the 15 findings REPRODUCER
actually took, 14 produced a committed failing test and 13 came back with their
confidence raised. Two were falsified — including a `critical` "read-only viewer
can create and place orders" that went 0.90 → 0.10 and would otherwise have been
filed. A floor of `major` still catches that one. The one it gives up is a
`minor` accessibility false positive.

A finding below the floor is **still filed**, on the evidence discovery already
produced: `is_fileable` asks for evidence, confidence and "not
not_reproducible", and `unattempted` passes all three. What it does not get is a
committed failing test — the right thing to spend a context on for a blocker and
the wrong thing for a trivium.

This cannot move `qaas score`: the scorecard reads envelopes and REPRODUCER
emits none. It trades reproduction depth for cost, and nothing else.

**Behaviour change.** Releases before 0.0.2 reproduced everything. One line
restores that:

```yaml
thresholds:
  reproduce_min_severity: trivial
```

The fan-out escalation now leads the escalation list and names the count and the
cap, rather than arriving third in a list of sixteen — it is the line that
explains the bill.

### `qaas dashboard`

A localhost page that reads a run's ledger and shows it as instrumentation: the
phase rail advancing, one card per agent with its cost, turn count and the tool
it is calling right now, a timeline lane each, findings by severity with their
evidence, and every guardrail refusal as it fires. `qaas run --dashboard` starts
it before the first agent dispatches.

Read-only by construction — every route is a GET, and a test asserts the route
table contains nothing else. It adds no ledger kind and no router change: the
phase is derived from each agent's layer, which is what lets it open runs
recorded long before it existed, including ones naming a retired roster.

Web dependencies are an optional extra:

```bash
pip install 'qaas-python[ui]'
```

All three (`starlette`, `uvicorn`, `sse-starlette`) already arrive with the
Claude Agent SDK, so it installs nothing new in practice — the extra pins what
the UI imports. The read model imports none of them and stays in the offline
suite.

## 0.0.1 — first release

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
