# 01 — Code structure and how the pieces relate

Where every module lives, what it owns, and which module is allowed to know about
which. Read this first; the other files assume it.

---

## The one-line version

```
qaas <command>  →  cli.py  →  conductor.py  →  runner.py  →  claude_agent_sdk.query()
                                    ↑              ↑
                              a state machine   one agent, one call
```

Everything else exists to serve those four files.

---

## The whole package

```
src/qaas/
├── cli.py           1564   entry point — 13 commands
├── conductor.py      527   THE STATE MACHINE: phases, budget, concurrency, loops
├── runner.py         192   invokes ONE agent, records what it cost
├── registry.py       465   turns an agent's YAML into SDK options
├── guardrails.py     421   the permission matrix, enforced in code
├── config.py         407   loads and validates config/
├── tasks.py          361   builds the per-run task string for each agent
├── paths.py          317   where config, prompts and skills are found
├── store.py          290   run ledger, artifacts, versioned system map
├── envelope.py       290   DefectEnvelope — the only inter-agent type
├── trace.py          270   the read side of the ledger
├── target.py         261   target profiles — what makes it portable
├── scorecard.py      425   recall/precision against the golden ledger
├── discover.py       227   guesses a target profile for `qaas init`
├── sdk_compat.py      52   fails loudly when the SDK renames a hook event
│
├── prompts/                one .md per agent + _shared.md appended to all
├── plugin/                 the skills, shipped as a Claude Code plugin
│   ├── .claude-plugin/plugin.json
│   └── skills/<30 dirs>/SKILL.md
├── defaults/config/        system.yaml + agents/*.yaml that ship in the wheel
├── mcp/                    seven in-process tool servers
└── adapters/               tracker (local | jira), vcs (local | github)
```

Line counts are real, from `wc -l`. They are a rough guide to where the weight
sits: `cli.py` and `adapters/tracker.py` are the two biggest files and are mostly
surface area, not logic.

---

## The dependency graph

This is the actual import graph, extracted from the source. **Arrows point at what
a module is allowed to know about.** Nothing points back up.

```
                         cli.py
                           │  (config, paths, store, trace)
                           ▼
                      conductor.py ────────────┐
                           │                   │
        (config, envelope, │ store,            │
         mcp.context)      ▼                   │
                       runner.py               │
                           │  (registry)       │
                           ▼                   │
                      registry.py              │
                           │  (config,         │
                           │   guardrails,     │
                           │   sdk_compat)     │
                           ▼                   │
                      guardrails.py            │
                           │  (mcp.context)    │
                           ▼                   ▼
                        config.py  ────────► store.py
                           │  (target)          │  (envelope)
                           ▼                    ▼
                       target.py  ─────────► envelope.py
                           │  (paths)       (imports nothing)
                           ▼
                        paths.py
                     (imports nothing)
```

Two facts fall out of this and are worth internalising:

**`envelope.py` and `paths.py` import nothing from the package.** They are the
foundation. `envelope.py` is the contract every agent speaks; `paths.py` is how
the package finds its own resources. Both must be understandable on their own,
and both are heavily tested in isolation.

**`registry.py` imports `config.py`, so `config.py` cannot import `registry.py`.**
There is a real consequence: the validator in `config.py` that checks every MCP
server an agent names actually exists has to import `registry` *inside the
function*. The comment there says so.

---

## What each module owns

| module | owns | does **not** own |
|---|---|---|
| `cli.py` | argument parsing, printing, exit codes | any decision about a run |
| `conductor.py` | phase order, budget, concurrency, escalation, loop bounds | how one agent runs |
| `runner.py` | one `query()`, one `AgentResult` | whether that agent should run |
| `registry.py` | assembling `ClaudeAgentOptions` | what the agent then does |
| `guardrails.py` | allow/deny for every tool call | logging policy (it delegates to the store) |
| `envelope.py` | the shape of a finding, and its two gates | where findings are stored |
| `store.py` | the ledger, artifacts, the system map | what any entry means |
| `config.py` | loading and validating YAML | where the YAML lives (that is `paths.py`) |
| `paths.py` | config/prompt/skill/state resolution | reading any of them |

---

## The two decisions that explain the shape

### 1. CONDUCTOR is Python, not a prompt

The design document describes an orchestrator agent. It is implemented as an
ordinary state machine because **a model cannot enforce a budget it is itself
spending.**

Everything that must be true regardless of what a model decides lives in
`conductor.py`: phase ordering, the concurrency cap, retries, escalation, and the
loop bounds. That is also why 649 tests run offline with no API key — the
interesting logic is not behind a model call.

```python
# src/qaas/conductor.py — the five phases, in order
map_version = await self._phase_map(specs, store, budget, report)
await self._phase_discover(specs, store, budget, report, mode, map_version)
await self._phase_reproduce(specs, store, budget, report, map_version)
if run_mode.files_tickets:
    await self._phase_file(...)
await self._phase_verify(...)
```

### 2. Every agent is its own top-level `query()`

Not SDK subagents under a shared parent. Nesting would pool cost into one number
and blur the per-agent tool allowlist that the whole permission model depends on.

One `query()` per agent gives three things that matter:

- a real context boundary — CONDUIT cannot see what SURFACE read
- an enforceable allowlist — the tools are set per call, not per session
- a per-agent `total_cost_usd`, straight off the `ResultMessage`

`AgentDefinition` / `agents=` remains available for *intra-agent* fan-out.

---

## Agents are data, not code

An agent is **a prompt plus a YAML file**. That is the whole definition.

```
src/qaas/prompts/CONDUIT.md              role and standards
src/qaas/defaults/config/agents/conduit.yaml   model, budget, tools, skills, policy
```

Adding an agent should require **no change** to `conductor.py`, `runner.py`,
`registry.py` or `guardrails.py`. A change to one of those while adding an agent
is a sign something is wrong.

Where each kind of instruction belongs — this split is easy to get wrong and the
codebase is strict about it:

| what | where | why |
|---|---|---|
| role, standards | `prompts/<AGENT>.md` | stable across every run |
| house rules for all agents | `prompts/_shared.md` | appended to every prompt, not copy-pasted into eight |
| **procedure** | `plugin/skills/<name>/SKILL.md` | loaded on demand, shared between agents |
| the per-run **task** | `tasks.py` | which app, which environment, which finding |
| **enforcement** | `guardrails.py` | a prompt is a request; code is a rule |

> **Nothing in `tasks.py` or a prompt may name a specific application.** A prompt
> that mentions one repo's layout works exactly once. This rule has been broken
> and fixed: two `contract_diff` tool descriptions once said
> `target-app/openapi.yaml` directly to the model.

---

## The eight agents

| agent | layer | does |
|---|---|---|
| CARTOGRAPHER | map | services, routes, schema, ownership → `system-map.json` |
| CONDUIT | discovery | API contract drift; ships a failing contract test |
| SURFACE | discovery | drives the UI through real journeys |
| FORGE | triage | reproduces, minimises, measures flake, commits a failing test |
| CLERK | triage | dedupes, scores severity, routes, files — the only tracker writer |
| MENDER | remediation | the minimal fix, on a `fix/*` branch |
| ARBITER | remediation | adversarial review: APPROVE / REQUEST_CHANGES / ESCALATE |
| PROOF | verify | re-runs the original test → VERIFIED / NOT_FIXED / REGRESSED |

The design names 16. Eight are built; CONDUCTOR is the state machine rather than
an agent; seven Phase-2 agents are designed and not written.

---

## Runtime state

```
.qaas/                          (gitignored)
├── config/                     your config, if you ran `qaas init`
├── runs/<run-id>/
│   ├── ledger.jsonl            append-only: every tool call, denial, escalation
│   ├── envelopes/*.json        the findings
│   ├── artifacts/              screenshots, logs, traces
│   └── results/*.json          per-agent cost, turns, duration
├── system-map/                 versioned, shared across runs, pinned per run
├── memory.db                   SQLite: defect fingerprints, for dedupe
└── tickets/*.json              the local tracker
```

The ledger is not only an audit trail — the conductor **reads it back** for
control flow. `_latest_verdict`, `_latest_review` and `_branch_written_since` all
query it. That is unusual and it is deliberate: a verdict is a typed ledger entry,
never parsed out of an agent's prose. See [02 — how agents
communicate](02-how-agents-communicate.md).

---

## Reading order for a newcomer

1. `qaas validate` then `qaas run --mode pr-check --dry-run` — watch the machine
   describe itself, for free
2. `envelope.py` — the contract everything moves
3. `conductor.py::run` — the five phases
4. `defaults/config/agents/conduit.yaml` + `prompts/CONDUIT.md` — what an agent *is*
5. `guardrails.py::check` — the one function both enforcement points call
6. `.qaas/runs/<id>/ledger.jsonl` from a real run, or `qaas trace <id>`

---

**Next:** [02 — How agents communicate](02-how-agents-communicate.md)
