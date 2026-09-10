# Build Plan — Multi-Agent QA & Remediation System

## Context

`qa-agent-system-architecture.md` (v0.1) specifies a 15-agent QA system across four layers, but the repo contains nothing else — no code, no config, no target. The doc is a design, not a build: it names agents, tools, and contracts, and closes with seven open decisions (§13) that block implementation.

This plan turns that design into a running system. Per the answers given:

- **Runtime** — a Python service on the **Claude Agent SDK** (`claude-agent-sdk`). ROUTER is a real state machine in code; each agent is a `query()` invocation with a hard tool allowlist.
- **Scope** — the doc's own Phase 1 loop (MAPPER, BROWSER, API, REPRODUCER, TRIAGE, VERIFIER), genuinely end-to-end, on a framework where agents 7–15 are config rather than code.
- **Target** — a bundled, deliberately-buggy demo app as the system under test, with a golden defect ledger so precision/recall is measurable, not asserted.
- **Integrations** — every external system behind an adapter with a local fake. The loop runs offline; real Jira/GitHub is a config swap.

That settles §13: monorepo, Postgres, no existing E2E suite, ephemeral envs provided by Docker Compose against the bundled app.

Status: plan approved, no code written yet. The milestone table below is the working checklist — keep it current as work lands.

---

## Two design decisions that shape everything

**1. ROUTER is Python, not a prompt.** §4.1 says "keep its reasoning shallow — routing, not analysis," and §10 makes it the enforcement point for budget and concurrency. A model cannot enforce a budget it is spending. So the run state machine, dispatch, retries, dead-letter queue, and the §8.3 loop breakers are ordinary code. This also makes runs reproducible and cheap to test.

**2. Every agent is its own top-level `query()`, not a subagent of a parent.** The SDK's `agents=` parameter nests subagents under one conversation; that blurs the per-agent tool allowlist §5.3 depends on and pools cost into one number. Running each agent as a separate `query()` gives a genuine context boundary (design principle §2), an enforceable per-agent allowlist, and per-agent `total_cost_usd` from its `ResultMessage`. `AgentDefinition`/`agents=` stays available for *intra-agent* fan-out (e.g. BROWSER exploring several routes in parallel).

---

## Architecture

```
qa-multi-agent-system/
├── qa-agent-system-architecture.md      # the spec (unchanged)
├── BUILD_PLAN.md                        # this plan
├── pyproject.toml                       # uv-managed, py3.12
├── config/
│   ├── system.yaml                      # run modes, budgets, thresholds, model per agent
│   ├── agents/<AGENT>.yaml              # allowlist, prompt path, effort, max_turns, budget
│   └── rules/                           # layering.yaml, severity-rubric.yaml, routing.yaml
├── src/qaas/
│   ├── envelope.py        # Pydantic DefectEnvelope v1.0 (§6) — the only inter-agent type
│   ├── registry.py        # AgentSpec (YAML) -> ClaudeAgentOptions
│   ├── router.py       # run state machine, budget governor, concurrency, dead-letter
│   ├── runner.py          # invoke one agent, stream messages, record cost/usage/artifacts
│   ├── guardrails.py      # can_use_tool + hooks = the §8.1 permission matrix, in code
│   ├── store.py           # run ledger, artifact store, versioned system-map
│   ├── scorecard.py       # §12 metrics; scores a run against the golden ledger
│   ├── prompts/<AGENT>.md # system prompts, one file per agent
│   ├── mcp/               # in-process SDK MCP servers (create_sdk_mcp_server)
│   └── adapters/          # tracker{local,jira}, vcs{local,github}
├── target-app/            # system under test
│   ├── api/               # FastAPI + Postgres + one WebSocket endpoint
│   ├── web/               # small UI (Vite + React)
│   ├── openapi.yaml       # the spec implementation is allowed to drift from
│   ├── defects.yaml       # GOLDEN LEDGER — every seeded defect, with expected domain/severity
│   └── docker-compose.yml
├── tests/                 # pytest; no API calls except the marked e2e tier
└── .qaas/                 # runtime: runs/, artifacts/, memory.db, tickets/ (gitignored)
```

### The five custom MCP servers are in-process, not subprocesses

§5.2 says five servers must be built. All of them are `create_sdk_mcp_server()` servers running inside the orchestrator process — no protocol implementation, no subprocess management, and tool calls land in Python where validation and guardrails already live.

| Server | Tools (per §5.2) | Backing |
|---|---|---|
| `envelope` | `emit_envelope`, `get_system_map`, `put_artifact` | Pydantic validation → run store |
| `test_runner` | `run_suite`, `run_single`, `run_n_times`, `affected_tests`, `get_coverage` | pytest + Playwright, structured JSON |
| `env_control` | `spin_up`, `seed`, `reset`, `set_flag`, `impersonate`, `tear_down` | Docker Compose against `target-app/` |
| `defect_memory` | `search_similar`, `fingerprint`, `record`, `get_occurrences`, `mark_resolved` | SQLite + `sqlite-vec` embeddings, structural fingerprint |
| `contract_diff` | `diff_openapi`, `classify_breaking`, `find_consumers`, `generate_contract_test` | `openapi.yaml` vs. live routes |
| `tracker` | `create_issue`, `transition`, `link`, `search` | adapter: local JSON tickets or Atlassian |

WebSocket Harness MCP is deferred — SOCKET is Phase 2. Off-the-shelf servers (Playwright, Filesystem, GitHub) are declared as stdio configs in `config/agents/*.yaml`, so adding one is a YAML edit.

### Guardrails are code, not prompting

§8.1's matrix becomes a `Policy` per agent, enforced in `can_use_tool` and a `preToolUse` hook — both of which see the tool name and arguments before execution:

- **Path scoping** — Filesystem/Edit/Write calls resolved and checked against the agent's allowed roots. REPRODUCER and FIXER only; everyone else denied.
- **Branch scoping** — git writes matched against the agent's branch regex (`qa/repro/*`, `fix/*`); `main` and any force-push denied outright.
- **Ticket rate limit** — `tracker.create_issue` counted per run/project/day; over cap the call is denied with a reason and ROUTER escalates instead of filing (§4.12).
- **Immutable test** — FIXER (Phase 3) denied any edit to the test path recorded on the envelope (§10).

Denials return `PermissionResultDeny` with a message, so the agent gets feedback and adapts rather than dying. Every denial is written to the run ledger — that log is the audit trail the doc asks for.

### The envelope is the only contract

`envelope.py` is a Pydantic model of §6, and agents cannot emit anything else: their sole write path is `envelope.emit_envelope`, which validates and rejects with field-level errors on failure. Prose never crosses an agent boundary. `confidence < 0.6` routes to a human queue instead of TRIAGE (§7).

---

## Milestones

Each milestone ends with a check that runs without human judgment.

### M0 — Contract and skeleton ✅ done
`envelope.py`, `store.py`, config loading, `qaas` CLI (`run`, `validate`, `score`, `replay`). No agents yet.
**Verify:** `pytest` — 45 tests green: envelope round-trip, rejection of malformed envelopes, fingerprint stability, ledger/artifact/system-map behaviour, and the §5.3 tool-budget and §8.1 permission-matrix rules asserted against the real config. `qaas validate` and `qaas run --dry-run` both clean.

### M1 — Target app and golden ledger ✅ done
FastAPI + Postgres + React UI + one WS endpoint. Seed ~14 defects spanning the Phase-1 domains, each recorded in `defects.yaml` with expected domain, severity, and location: OpenAPI drift, a missing role check, unbounded list endpoint, inconsistent error shapes, a broken checkout step, an unhandled promise rejection, contrast/label a11y failures, a form that loses input on error, plus 2–3 planted *non-defects* to catch false positives.
**Verify:** `docker compose up` → app reachable; `pytest tests/target_app` proves each seeded defect is real and each planted non-defect is not.

### M2 — MCP servers and adapters ✅ done
The six servers above plus local tracker/vcs adapters.
**Verify:** `pytest tests/mcp` — every tool exercised directly, zero API calls. `defect_memory` proven to dedupe two differently-worded reports of the same defect.

### M3 — Agent runtime + MAPPER ✅ done
`registry.py`, `runner.py`, `guardrails.py`, then the first real agent. MAPPER goes first because §4.2 is right that everything downstream gets cheaper once the map exists.
**Verify:** `qaas run --agent MAPPER` writes a versioned `system-map.json` covering the target app's services, routes, schema, and ownership; schema-validated. Guardrail tests assert a write attempt from a read-only agent is denied and logged.

### M4 — Discovery: API + BROWSER ✅ done
API gets `contract_diff` and ships a failing contract test as evidence (§4.5). BROWSER runs scripted journeys first, then exploratory from the Mapper task graph.
**Verify:** `qaas run --mode pr-check` emits envelopes; `qaas score` reports how many golden defects in those two domains were found and how many findings were not in the ledger.

### M5 — Triage: REPRODUCER + TRIAGE ✅ done
REPRODUCER reproduces, minimizes, runs N times for flake rate, and commits a failing test to `qa/repro/*`. TRIAGE dedupes, scores against the rubric, resolves owner from the map, routes, and files — the only agent holding tracker write.
**Verify:** full discovery→triage run produces local tickets with real repro steps and attached failing tests; a second run on the same code files **zero** new tickets and increments occurrence counts instead.

### M6 — Close the loop: VERIFIER + ROUTER run modes ✅ done, verified live
VERIFIER re-runs REPRODUCER's test against a patched build and returns `VERIFIED`/`NOT_FIXED`/`REGRESSED`. ROUTER gains all five discovery run modes, budget governor, concurrency caps, and escalation.
**Verify:** the acceptance test for the whole system — fix one seeded defect by hand on a branch, run `qaas run --mode fix-cycle --ticket <id>`, and VERIFIER returns `VERIFIED`; revert the fix and it returns `NOT_FIXED`. Then `qaas run --mode nightly && qaas score` prints the §12 scorecard: acceptance rate, duplicate rate, false-positive rate, cost per accepted ticket.

### Skills, hooks and loops ✅ done

**Skills** — 23 under `.claude/skills/<name>/SKILL.md`, carrying the procedure the
architecture names in §4. Agents declare them in `config/agents/*.yaml`; the
registry passes `skills=` and sets `setting_sources=["project"]` (project only —
filesystem skills need it, and project settings live in the repo so a run stays
reproducible; `user` and `local` stay excluded, with a test asserting it).
Descriptions carry an explicit TRIGGER/BEFORE/SKIP clause, tested for, because a
description that merely names a topic is a skill that never fires. Content moved
out of the prompts rather than being copied — the severity rubric now lives only
in `severity-rubric`.

**Hooks** — `PreToolUse` re-runs the guardrail `check()` (a second enforcement
point in case `can_use_tool` is shadowed; both call one decision function so they
cannot disagree) and counts calls. `PostToolUse` tells an agent immediately when
an envelope was recorded but held, rather than letting it discover at the end
that nothing counted. `Stop` blocks an agent that skipped its declared
deliverable (`must_call` in its config), honouring `stop_hook_active` so a
genuinely stuck agent cannot loop the budget away.

**Loops** — `_phase_verify` is now a bounded remediation loop: VERIFIER → NOT_FIXED
→ FIXER → REVIEWER → VERIFIER, enforcing `max_proof_reopens` and
`max_mender_arbiter_round_trips` from §8.3. VERIFIER's verdict is a typed ledger
entry (`record_verdict`), never parsed from prose. With no FIXER in the Phase 1
roster a NOT_FIXED escalates immediately instead of re-running VERIFIER against
unchanged code. `qaas sweep` is the cron entry point: run, score, and exit
non-zero below the §11 precision gate.

### M7 — Phase 3: the fix loop (beyond the original plan)

FIXER and REVIEWER, with the §8.2 autonomy envelope enforced in `guardrails.py`
rather than requested in a prompt: a diff budget counted per distinct file, and
forbidden path classes (migrations, auth, payment, secrets, infrastructure, CI)
that stop at a human however small the change looks. `record_review` refuses a
verdict with no actionable reasoning, because a rubber stamp is worse than no
review. Two new run modes: `fix-cycle` and `full-loop`.

Merge remains impossible by construction: no merge method exists anywhere in the
codebase, `gh pr merge` is refused, and pull requests open as drafts.

**Verify:** router tests prove the bounded loop (VERIFIER → FIXER → REVIEWER →
VERIFIER, capped by `max_mender_arbiter_round_trips` and `max_proof_reopens`), and
guardrail tests prove every forbidden class and the diff budget. Live: a
`full-loop` run on the demo app that ends in a VERIFIED ticket.

**What the first live run found.** It ended NOT_FIXED, and reading the verdict
turned up two defects in this system rather than one in the target:

1. `fixtures.sql` was not idempotent. Compose mounts it into
   `docker-entrypoint-initdb.d`, so any explicit `env_control.seed()`/`reset()`
   was its *second* application and died on a duplicate key — leaving `status`
   reporting no fixture and no way to pin a reproduction's environment. It
   truncates first now.
2. `_verify_loop` re-read the branch off the envelope on every pass. That is the
   *repro* branch, written before a fix exists, so VERIFIER was sent back to the
   unfixed branch it had just failed on. VERIFIED was unreachable by
   construction. The loop now reads FIXER's branch out of the ledger, scoped to
   the entries one remediation round appended.

Both are the point of running the thing live: neither was visible to 528 offline
tests, and (2) meant no `full-loop` run could ever have closed.

**A third defect, found only by re-running.** CORVID-7 then escalated twice with
a correct one-line fix sitting on the branch, because two rules in this repo
contradicted each other: `CLAUDE.md` required FIXER to retire the seeded
defect's ledger entry in the same commit, and `fixer.yaml`'s `write_paths`
forbade it. FIXER tried and the guardrail refused
(`Edit  write refused: target-app/defects.yaml is outside FIXER's sandbox`).
Every seeded defect has a ledger entry, so this deadlocked *any* fix to any of
them. The sandbox was right and the rule was wrong: the ledger is the answer key
and must stay unwritable by the agents it scores, so the same-commit obligation
now sits on a human at merge, and `adversarial-review` carries the general
lesson — never request a change the author is not permitted to make.

**Closed live on CORVID-8**, 2026-09-08, `fix-cycle`, $6.84, zero escalations:

```
VERIFIER  NOT_FIXED   defect confirmed present
FIXER                 currency: str added to InvoiceOut (1 line of product code)
REVIEWER  APPROVE
VERIFIER  VERIFIED    re-verified on FIXER's branch
```

VERIFIER ran the original failing test 5 of 5 times for flake, then the full 22-test
`qa/repro` suite, and correctly attributed all 11 failures to other open tickets
(CORVID-7, CORVID-SEC-6) with source confirmation rather than to the diff. The
second VERIFIER dispatch is the hop the branch-selection fix above made reachable at
all. Verified independently afterwards: the diff is one product line and the live
endpoint returns `currency`.

CORVID-7 remains escalated, correctly — shipping it alone makes orders 26-30
unreachable in a UI with no pager (UI-08, filed as CORVID-16). That is a product
decision, which is what escalation is for.

### Phase D — the run trace is legible ✅ done

The ledger was the richest thing a run produced and nothing could read it. 28
distinct `kind` values were written across `src/`; `LedgerEntry.kind` was a bare
`str` whose comment named 7 of them, and `qaas show` — docstring: "findings and
ledger" — read exactly one (`denial`), printing no cost, no mode, no duration, no
verdicts, no escalations, no tickets. The audit trail existed; the audit did not.

- `qaas trace <run-id>` renders the ledger as a timeline with cost accumulating,
  filtered by `--agent` / `--kind` (repeatable, validated against the enum) and
  exportable with `--json`. Consecutive tool calls by one agent fold into a
  single row: a real run logs ~2400 `tool_call` lines against ~150 of everything
  else, and one row each buries the dispatches and verdicts worth reading.
- `qaas show` now leads with mode, target commit, duration, cost against budget,
  every ticket and its latest verdict, and the escalations.
- `LedgerKind` (a `StrEnum`, so the wire format and every `== "denial"`
  comparison are unchanged) closes the set: a typo now raises at the `store.log`
  call instead of inventing a 29th kind no reader looks for.
- **Two provenance gaps closed.** `agent_started` recorded `task_chars=len(task)`
  — the length of the prompt, not the prompt — so the instruction an agent
  actually received was unrecoverable and five REPRODUCER dispatches differed only by
  character count; the task now goes to the artifact store with a preview inline.
  And `run_started` recorded no commit, so a run was not pinned to the code it
  examined; it now carries the target's sha, branch and dirty flag.

**Verify:** `pytest tests/test_trace.py` — 29 tests over a synthetic ledger and a
scripted router, offline. Includes an AST sweep asserting every literal passed
to `store.log()` anywhere in `src/qaas/**` is a `LedgerKind` member, so the next
kind added cannot go missing from the readers.

### Phase E — the qaas project and the target are two different roots ✅ done

`Path.cwd()` was doing duty as both "where qaas lives" and "the application under
test", through `ToolContext.repo_root` and `SystemConfig.target_app`. That is
true of exactly one target — the bundled demo, which happens to sit inside this
checkout — and false for every other. `ToolContext.target_root` now comes from
`profile.root_path()`, `SystemConfig.target_app` is gone, and FIXER's
`write_paths` are target-relative (`api/app`, not `target-app/api/app`).

`qaas run --repo <path-or-url>` follows from the split: it clones into
`.qaas/targets/<slug>`, provisions a profile through the same code path as `qaas
init`, and then runs. It is sugar over `--target`, reuses an existing profile
unless `--force`, and does not repoint `system.yaml`.

**Verify:** `pytest` — the write-path allowlist, the SDK subprocess `cwd` and the
forbidden-path classes are asserted against a target elsewhere on disk, with the
process cwd deliberately somewhere else; `qaas run --repo <url> --dry-run` clones
and renders without an API call. Two defects the split exposed and fixed:
`forbidden_paths` globs were written `*/x/*`, so a repository's own root-level
`.github/` matched nothing once the `target-app/` prefix went away; and target
profiles were read from the *first* config layer with a `targets/` directory
rather than layered by filename, so the first generated profile hid every
hand-written one.

### Adding agents 7–15 afterwards
A new discovery agent should be a prompt file plus a `config/agents/<NAME>.yaml` naming its allowlist — no changes to router, runner, or guardrails. Whether that holds is the real test of M3, so the first Phase-2 agent (DBA) will be added as a smoke test of the extension path before this build is called done.

---

## Cost and auth

Every run spends real money, and a nightly sweep is the expensive one. Controls, from the start:

- Per-agent and per-run `max_budget_usd` on `ClaudeAgentOptions`; ROUTER aborts the run at the cap and records partial results.
- `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` and `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS` set via `env` — Opus 5 delegates readily, and an unbounded subagent tree is the fastest way to a surprise bill.
- `--dry-run` renders each agent's exact options and prompt without calling the API; used in unit tests.
- A cheap CI profile (lower effort, smaller model for the mechanical agents) separate from the full profile.
- `total_cost_usd` per agent recorded on every run, so §12's cost-per-accepted-ticket is measured rather than guessed.

Auth: no `ANTHROPIC_API_KEY` is set here, but the Claude Code CLI (v2.1.263) is installed and authenticated, and the Agent SDK drives it — so it works as-is. `ANTHROPIC_API_KEY` remains the CI path.

Model default is `claude-opus-5` for judgment-heavy agents (API, BROWSER, REVIEWER later) and a cheaper model for mechanical ones (MAPPER extraction, TRIAGE composition), set per agent in `config/agents/*.yaml`.

---

## Verification of the whole system

Four tiers, cheapest first:

1. `pytest -m "not llm"` — envelope, guardrails, MCP tools, adapters, scoring. No API calls; runs in CI on every commit.
2. `qaas run --dry-run --mode nightly` — asserts every agent's assembled options match its §5.3 allowlist and that no agent exceeds six servers.
3. `pytest -m llm` — one cheap live run per agent against the target app, asserting shape (a valid envelope, a written map) rather than content.
4. `qaas run --mode nightly && qaas score` — the honest number: precision and recall against `target-app/defects.yaml`. §11 sets the bar at 70% acceptance before adding agents; the scorecard is what decides whether the framework earns Phase 2.

## Risks

- **Hook event names.** The SDK's `HookEvent` literals differ between docs and releases (`preToolUse` vs `PreToolUse`). M3 pins them by introspecting the installed `claude_agent_sdk` rather than trusting the docs, and `can_use_tool` carries the guardrails so a hook-name regression degrades logging, not enforcement.
- **Exploratory BROWSER is the noisiest agent.** It ships behind the confidence gate and the per-run finding cap from day one; if its false-positive rate is bad in M4, it runs scripted-only until the ledger says otherwise.
- **Seeded defects are easier than real ones.** The golden ledger measures whether the loop works, not whether it is good at finding hard bugs. Treat 100% recall on `defects.yaml` as a floor, never as evidence the system is ready for a real codebase.
