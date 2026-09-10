# How this system works

A guide to `qaas` for someone who has never opened it. Read this before
`CLAUDE.md` (which is terse notes for people already oriented) and before
`qa-agent-system-architecture.md` (which is the original design, not the code).

---

## 1. What it is, in one paragraph

`qaas` points a team of AI agents at an application, finds real defects in it,
reproduces them with runnable failing tests, files tickets, fixes some of them,
reviews the fixes, and verifies the fix actually worked. It is not a chatbot with
tools bolted on. It is a Python state machine that invokes agents the way a build
system invokes compilers: on a schedule, within a budget, with hard limits on
what each one may touch.

The thing it is pointed at is called the **target**. A bundled deliberately-buggy
demo app (`target-app/`) ships with it, along with a list of every bug seeded
into it (`target-app/defects.yaml`), so you can measure whether the agents
actually find things rather than take their word for it.

---

## 2. Entry point

There is exactly one:

```toml
# pyproject.toml
[project.scripts]
qaas = "qaas.cli:app"
```

`qaas` on the command line runs `src/qaas/cli.py`. Everything starts there. The
commands you will actually use:

| command | what it does | costs money? |
|---|---|---|
| `qaas validate` | checks config, prompts and allowlists are coherent | no |
| `qaas doctor --target corvid` | what this target makes possible | no |
| `qaas run --mode pr-check --dry-run` | renders each agent's exact options and prompt | no |
| `qaas run --mode pr-check` | **a real run** | yes |
| `qaas runs` / `qaas show <id>` | what happened in past runs | no |
| `qaas score` | recall/precision against the golden ledger | no |
| `qaas sweep` | run + score + fail below the quality gate — the cron entry | yes |
| `qaas tracker-check` | Jira auth/permissions preflight, creates nothing | no |

The tracker and vcs backends are `local` in the committed config and must stay
that way — the offline test suite builds real adapters, and a committed `jira`
breaks it. Switch per-shell instead:

```bash
QAAS_TRACKER=jira qaas run --mode nightly     # files to real Jira
QAAS_TRACKER=jira qaas tracker-check          # preflight only
```

Start with `qaas validate`, then `qaas run --dry-run`. Neither calls the API.

---

## 3. The shape of the code

```
src/qaas/
├── cli.py          866  entry point; every command above
├── router.py    452  THE STATE MACHINE — phases, budget, concurrency, loops
├── runner.py       118  invokes ONE agent, records what it cost
├── registry.py     275  turns an agent's YAML into SDK options
├── guardrails.py   413  the permission matrix, enforced in code
├── envelope.py     263  DefectEnvelope — the only type agents pass each other
├── store.py        225  run ledger, artifacts, versioned system map
├── config.py       201  loads and validates config/
├── tasks.py        349  builds the per-run task string for each agent
├── target.py       249  target profiles — what makes it portable
├── scorecard.py    424  measures recall/precision against the golden ledger
├── discover.py     227  guesses a target profile for `qaas init`
├── sdk_compat.py    52  fails loudly when the SDK changes hook names
├── prompts/            one .md per agent + _shared.md appended to all
├── mcp/                seven in-process tool servers (see §6)
└── adapters/           tracker (local | jira), vcs (local | github)
```

If you read three files, read `router.py`, `envelope.py`, `guardrails.py`.
Everything else is in service of those.

---

## 4. The two decisions that explain everything else

**ROUTER is Python, not a prompt.** The original design has an orchestrator
agent. It is implemented as an ordinary state machine instead, because *a model
cannot enforce a budget it is itself spending*. Phase ordering, concurrency,
retries, escalation and the budget governor are all plain code in
`router.py`. This also makes runs reproducible and cheap to unit-test — 547
tests run offline with no API calls.

**Each agent is its own top-level `query()`.** Not SDK subagents of a shared
parent. That is what gives each agent a real context boundary, an enforceable
per-agent tool allowlist, and its own `total_cost_usd`. Nesting them would pool
cost into one number and blur the allowlist that the whole permission model
depends on.

---

## 5. The flow

### 5.1 One run, end to end

```
$ qaas run --mode full-loop
        │
        ▼
    cli.py  ── loads config/system.yaml + config/agents/*.yaml + the target profile
        │
        ▼
 router.py  ── runs five phases in order, in-process
        │
        ├── PHASE 1  map        MAPPER                    → system-map.json (versioned, pinned)
        ├── PHASE 2  discover   API, BROWSER, …           → DefectEnvelopes   [concurrent]
        ├── PHASE 3  reproduce  REPRODUCER                → failing test per finding
        ├── PHASE 4  file       TRIAGE                    → tickets
        └── PHASE 5  verify     VERIFIER ⇄ FIXER ⇄ REVIEWER   [bounded loop]
```

Each phase is a method on `Router`: `_phase_map`, `_phase_discover`,
`_phase_reproduce`, `_phase_file`, `_phase_verify`.

Which agents run is **config, not code** — `run_modes` in `config/system.yaml`:

```yaml
pr-check:   [MAPPER, API, BROWSER, REPRODUCER, TRIAGE]        $16
nightly:    [MAPPER, API, BROWSER, REPRODUCER, TRIAGE]        $40
fix-cycle:  [VERIFIER, FIXER, REVIEWER]                              $20
full-loop:  all eight                                             $60
```

Note the shape: **REPRODUCER runs once per finding**, in a fresh context each time. So
cost scales with how much was found, not with how many agents exist.

### 5.2 Dispatching one agent

```
router._dispatch(spec, task)
        │
        ├── Budget.check()          raises BudgetExceeded → a control working, not an error
        │
        ▼
 runner.run_agent(spec, ctx, task)
        │
        ├── registry.build_options(spec, ctx)
        │       ├── system prompt   ← prompts/<AGENT>.md + prompts/_shared.md
        │       ├── task string     ← tasks.py (which app, which env, which finding)
        │       ├── skills          ← .claude/skills/*/SKILL.md
        │       ├── mcp_servers     ← in-process, closed over ToolContext
        │       ├── allowed_tools   ← this agent's allowlist only
        │       └── hooks           ← PreToolUse / PostToolUse / Stop
        │
        ▼
 claude_agent_sdk.query(...)        one top-level query, its own context
        │
        ▼
 AgentResult   cost_usd, num_turns, subtype, error   → written to the run ledger
```

Failures are **captured, not raised**. One agent falling over costs the run that
agent's findings, not the whole run; the router decides whether to retry, skip
or escalate.

### 5.3 The remediation loop (phase 5)

This is the only phase with a cycle in it, so it is the only one that needs
explicit bounds:

```
        ┌─────────────────────────────────────────────┐
        ▼                                             │
      VERIFIER ──VERIFIED──▶ done                        │
        │                                             │
        ├──REGRESSED──▶ escalate to a human           │
        │                                             │
     NOT_FIXED                                        │
        │                                             │
        ├─ reopens ≥ max_proof_reopens ─▶ escalate    │
        ▼                                             │
      FIXER ──▶ REVIEWER ──APPROVE────────────────────┘
                    │
                    ├─ REQUEST_CHANGES ─▶ back to FIXER (capped at 2 trips)
                    └─ ESCALATE_TO_HUMAN ─▶ stop
```

The bound is the point. Without `max_proof_reopens`, a fix that keeps missing the
defect cycles until the budget is gone and the run ends with no verdict and no
money left to reach one. Escalating after one reopen costs a human five minutes;
not escalating costs the whole run.

VERIFIER's verdict is a **typed ledger entry** written through a tool
(`record_verdict`), never parsed out of the agent's prose.

---

## 6. Data: what actually moves between agents

### 6.1 The DefectEnvelope is the only inter-agent type

Agents never pass prose to each other. `envelope.py` defines a Pydantic model
with `extra="forbid"`, and an agent's only write path is the
`envelope.emit_envelope` tool, which validates and rejects with field-level
errors.

```
envelope_version  id  run_id  discovered_by  discovered_at
domain            defect_class     title      summary
location          evidence         reproduction
impact            severity         confidence
suggested_owner   suggested_fix_area   autonomy_eligible
dedupe            jira
```

Two gates live on the model itself rather than in a prompt, because a prompt is a
request and a method is a rule:

- `has_evidence()` — an artifact or a failing test, or it is not a finding
- `is_fileable()` — evidence **and** confidence ≥ threshold **and** not
  `not_reproducible`

`fingerprint()` deliberately **excludes** prose, line numbers, commit sha and
timestamps, so the same defect found twice by two differently-worded agents
hashes the same. That is what makes deduplication possible at all.

### 6.2 Where state lives

```
.qaas/                          (gitignored — runtime state)
├── runs/<run-id>/
│   ├── ledger.jsonl            append-only: every tool call, denial, escalation
│   ├── envelopes/*.json        the findings
│   ├── artifacts/              screenshots, logs, traces
│   └── results/*.json          per-agent cost, turns, duration
├── system-map/                 versioned, shared across runs, pinned per run
├── memory.db                   SQLite: defect fingerprints for dedupe
└── tickets/*.json              the local tracker (when tracker: local)
```

The ledger is the audit trail. `qaas show <run-id>` reads it. Evidence is
referenced by `artifact://<run>/<name>` URIs, and `resolve_artifact` rejects any
path escaping the store.

The system map is **versioned and pinned per run** so a bad map cannot
half-propagate through a run that already started.

### 6.3 The seven MCP servers

All in-process (`create_sdk_mcp_server`), no subprocesses, no protocol
implementation. They close over a `ToolContext` holding the run store, config,
agent spec and pinned map version — so validation, guardrails and persistence
happen where the state already is.

| server | what it gives an agent |
|---|---|
| `envelope` | `emit_envelope`, `get_system_map`, `put_artifact` |
| `test_runner` | `run_suite`, `run_single`, `run_n_times`, `affected_tests` |
| `env_control` | `spin_up`, `seed`, `reset`, `set_flag`, `impersonate`, `tear_down` |
| `defect_memory` | `search_similar`, `fingerprint`, `record`, `get_occurrences` |
| `contract_diff` | `diff_openapi`, `classify_breaking`, `generate_contract_test` |
| `tracker` | `create_issue`, `transition`, `link`, `search` |
| `vcs` | `create_branch`, `write_file`, `commit`, `push`, `open_pull_request` |

Tool results use `ok()` / `err()` from `mcp/context.py`. Errors are **returned,
not raised**, so an agent reads the reason and corrects itself instead of dying.

Playwright is the one exception — a real stdio subprocess, declared in
`registry.STDIO_SERVERS`.

---

## 7. Agents are data, not code

An agent is **a prompt plus a YAML file**. Nothing else.

```
src/qaas/prompts/API.md      role, standards, what good looks like
config/agents/api.yaml       model, budget, tools, skills, policy
```

Adding an agent should require **no change** to `router.py`, `runner.py`,
`registry.py` or `guardrails.py`. A change to those files while adding an agent
is a sign something is wrong.

Where each kind of instruction belongs — this split matters and is easy to get
wrong:

| what | where | why |
|---|---|---|
| role, standards | `prompts/<AGENT>.md` | system prompt, stable across runs |
| house rules for all agents | `prompts/_shared.md` | appended to every prompt; not copy-pasted six times |
| **procedure** | `.claude/skills/<name>/SKILL.md` | loaded on demand, shared between agents |
| the per-run **task** | `tasks.py` | which app, which environment, which finding |
| **enforcement** | `guardrails.py` | a prompt is a request; code is a rule |

**Nothing in `tasks.py` or a prompt may name a specific application.** A prompt
that mentions one repo's layout or one app's seeded users works exactly once.

### The sixteen agents

| agent | layer | does |
|---|---|---|
| MAPPER | map | services, routes, schema, ownership → `system-map.json` |
| ARCHITECT | discovery | circular deps, layering violations, god modules, dead code |
| API | discovery | API contract drift; ships a failing contract test |
| BROWSER | discovery | drives the UI through real journeys |
| DBA | discovery | schema constraints the code assumes and the database does not enforce |
| AUDITOR | discovery | missing authorization, secrets, vulnerable dependencies, leaked internals |
| SOCKET | discovery | WebSocket auth, reconnect, ordering, backpressure |
| GUIDE | discovery | whether a person can *find* a feature, not just whether it works |
| LOAD | discovery | N+1 queries, unindexed hot paths, unbounded results, bundle outliers |
| REPRODUCER | triage | reproduces, minimises, measures flake, commits a failing test |
| TRIAGE | triage | dedupes, scores severity, routes, files — the only tracker writer |
| FIXER | remediation | the minimal fix, on a `fix/*` branch |
| REVIEWER | remediation | adversarial review: APPROVE / REQUEST_CHANGES / ESCALATE |
| VERIFIER | verify | re-runs the original test → VERIFIED / NOT_FIXED / REGRESSED |
| REPORTER | reporting | what the run found, what recurred, and what it could not reach |

ROUTER is the sixteenth. It is the Python state machine in `router.py`
rather than an agent, because a model cannot enforce a budget it is spending.

---

## 8. Guardrails: how agents are actually constrained

The critical fact, learned the hard way in this repo:

> An `allowed_tools` entry naming a whole tool **auto-approves it before
> `can_use_tool` is consulted.** A policy implemented only in that callback is
> silently dead code.

So primary enforcement is the **`PreToolUse` hook**, with `can_use_tool` as a
second layer for calls the allowlist did not auto-approve. Both call one
`check()` function so they cannot disagree.

What is enforced:

- **Path scoping** — writes resolved and checked against `write_paths`. REPRODUCER and
  FIXER only; everyone else denied.
- **Branch scoping** — git writes matched against branch patterns
  (`qa/repro/*`, `fix/*`). `main` and force-push denied outright.
- **Ticket rate limit** — `max_tickets_per_run: 10`; over cap the call is denied
  and the router escalates instead of filing.
- **Forbidden path classes** — migrations, auth, payment, secrets, infra, CI stop
  at a human however small the change looks.
- **Diff budget** — counted per distinct file.
- **Immutable test** — FIXER may not edit the test recorded on the envelope.

Denials **return a reason and are logged**; they never kill the turn. The agent
reads the denial and adapts.

`ALWAYS_GRANTED` (ToolSearch, Skill, TodoWrite, Task, Agent) is read by both
`build_allowed_tools` and the guardrail — a mismatch there silently disables every
skill. Denying `ToolSearch` breaks MCP access entirely, since MCP tools arrive
deferred.

**Merge is impossible by construction.** No merge method exists anywhere in the
codebase, `gh pr merge` is refused by `FORBIDDEN_BASH`, and pull requests open as
drafts. Merging is a human decision.

### Hooks enforce the output contract

- **`Stop`** blocks an agent that has not called its `must_call` tools, *while it
  still has a turn to fix it* — the router would only find out afterwards. It
  honours `stop_hook_active`; blocking twice burns budget.
- **`PostToolUse`** tells an agent immediately when an emitted envelope was
  **held** rather than filed, since discovering that at the end is too late to
  attach evidence.

---

## 9. Targets: why this is portable

`target.py` + `config/targets/*.yaml`. The load-bearing field is
`environment.mode`:

| mode | meaning |
|---|---|
| `none` | static reads only — no running app |
| `external` | exercise it, but never reset it |
| `compose` | own the lifecycle: spin up, seed, reset, tear down |

`profile.capabilities()` decides which agents can usefully run; `qaas doctor`
reports it. **Credentials are never in a profile** — it names environment
variables, and the values live outside the repo.

Pointing this at a different application is a new YAML file, not a code change.

---

## 10. Calibration: the part that keeps it honest

`target-app/` is deliberately buggy. `target-app/defects.yaml` is the **golden
ledger**: every seeded defect with the domain, severity and location a correct
agent should conclude. `scorecard.py` measures recall, precision, false-positive
rate and severity agreement against it.

```
$ qaas score
recall              81%   (13 of 16)
precision          100%
false positives      0
severity agreement 100%
cost per accepted  $1.12
```

The `not_defects` section plants correct-but-suspicious code, so **precision is
measured rather than assumed**.

Two rules:

1. **Run `qaas score` after changing any prompt, threshold or model.** It is the
   only way to know whether a change helped.
2. **The ledger is not writable by the agents it scores** — deliberately. An
   agent that can retire an entry can raise its own recall without fixing
   anything. Retiring a repaired defect (`fixed_in: <ref>`) is a human's job at
   merge time.

And the caveat that matters most: seeded defects are easier than real ones. Treat
100% recall on `defects.yaml` as a floor, never as evidence the system is ready
for a real codebase.

---

## 11. Testing

```bash
pytest                    # 547 tests, no API calls, no network, free
pytest -m docker          # 18 tests, needs target-app running
pytest -m 'llm'           # real API calls — excluded by default
```

The markers `llm`, `docker`, `github`, `jira` are deselected by `addopts` in
`pyproject.toml`. **The default `pytest` run is offline and free, and must stay
that way.**

Four tiers, cheapest first:

1. `pytest` — envelope, guardrails, MCP tools, adapters, scoring
2. `qaas run --dry-run` — asserts assembled options match each allowlist
3. `pytest -m llm` — one cheap live run per agent, asserting shape not content
4. `qaas run && qaas score` — the honest number

---

## 12. Reading order for a newcomer

1. `qaas validate` then `qaas run --mode pr-check --dry-run` — see the machine
   describe itself, for free
2. `envelope.py` — the contract everything else moves
3. `router.py::run` — the five phases
4. `config/agents/api.yaml` + `prompts/API.md` — what an agent *is*
5. `guardrails.py::check` — the one function both enforcement points call
6. `.qaas/runs/<id>/ledger.jsonl` from a real run — what actually happened

---

## 13. Known gaps

Honesty about what is *not* proven, so nobody inherits a false impression:

- **Eight of sixteen agents.** ARCHITECT, DBA, SOCKET, GUIDE, AUDITOR, LOAD and
  REPORTER are designed but not built.
- **The extensibility claim is untested.** "A new agent needs only a prompt and a
  YAML" is the architecture's central promise, and no one has added a ninth agent
  to check it.
- **`fix-cycle` leaves the working tree on a `fix/*` branch.** Harmless when
  watched; it would corrupt the next run of an unattended `qaas sweep` on cron.
- **Only ever run against the bundled demo app**, whose bugs it was told about.
- **`wont_fix` and `duplicate`** have no matching status in the connected Jira
  workflow, so those transitions would fail.
