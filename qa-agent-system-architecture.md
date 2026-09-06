# Multi-Agent QA & Remediation System

**Architecture, agent roster, skills, and integrations**
Version 0.1 — draft for review

---

## 1. Summary

| | |
|---|---|
| **Full system** | 15 agents across 4 layers |
| **MVP (weeks 1–6)** | 6 agents |
| **Off-the-shelf MCP servers** | 11 |
| **MCP servers you must build** | 5 |
| **Core loop** | Discover → Reproduce → File → Fix → Verify → Close |

The system runs two loops that meet at Jira. A **discovery loop** finds defects across architecture, data, API, realtime, and UI surfaces and files them as tickets. A **remediation loop** picks tickets up, fixes them, and proves the fix. Jira is the handoff boundary and the audit trail. Nothing crosses between loops except through a ticket.

---

## 2. Design principles

These drive every decision below. Read them first; they explain why the roster looks the way it does.

**An agent is a context boundary, not a personality.** Split agents where the *knowledge*, *tools*, and *output contract* differ. Do not split them to make an org chart. Two "agents" with identical tool allowlists and overlapping prompts should be one agent with two skills.

**The finder never fixes.** An agent that files and resolves its own defect grades its own homework. Discovery, triage, remediation, and verification are separate agents with separate contexts. This is the single most important structural rule here.

**Tool bloat degrades agents.** Past roughly 5–7 connected MCP servers, tool selection accuracy drops and latency rises. Every agent below has a hard tool allowlist. If an agent needs a 8th server, that is a signal to split it.

**Everything is a typed envelope.** Agents do not pass prose to each other. They pass a validated `DefectEnvelope` (§6). Prose between agents is where multi-agent systems rot.

**Write access is earned per agent, per resource, per environment.** Default is read-only. See the permission matrix in §8.

**Evidence or it did not happen.** Every filed defect carries a reproduction, a trace, a screenshot, a query plan, or a failing test. No evidence, no ticket.

---

## 3. System layers

```
┌─ LAYER 0 · CONTROL ────────────────────────────────────┐
│  CONDUCTOR (orchestrator)   CARTOGRAPHER (system map)  │
└────────────────────┬───────────────────────────────────┘
                     │ dispatch + shared map
┌─ LAYER 1 · DISCOVERY ──────────────────────────────────┐
│  KEYSTONE   VAULT   CONDUIT   PULSE                    │
│  SURFACE    USHER   WARDEN    GAUGE                    │
└────────────────────┬───────────────────────────────────┘
                     │ DefectEnvelope (draft)
┌─ LAYER 2 · TRIAGE ─────────────────────────────────────┐
│  FORGE (reproduce)  →  CLERK (dedupe, score, file)     │
└────────────────────┬───────────────────────────────────┘
                     │ Jira ticket  [agent-ready]
┌─ LAYER 3 · REMEDIATION ────────────────────────────────┐
│  MENDER (fix)  →  ARBITER (review)  →  PROOF (verify)  │
└────────────────────────────────────────────────────────┘
                     │ closed ticket + merged PR
                     ▼
              CHRONICLE (optional, reporting)
```

---

## 4. Agent roster

### Layer 0 — Control plane

---

#### 4.1 CONDUCTOR — Orchestrator

**Role.** Owns the run state machine. Decides which discovery agents fire for a given trigger, allocates budget, enforces concurrency limits, handles retries and dead-letters, and escalates to humans.

**Runs when.** Every trigger: PR opened, nightly sweep, release candidate cut, on-demand request, incident declared.

**Inputs.** Trigger event, change manifest (files/services touched), the Cartographer's system map, prior run history.

**Outputs.** Work orders to discovery agents; a run ledger; escalations to Slack.

**Tools.** GitHub MCP (read), Slack MCP, Defect Memory MCP, internal scheduler. No repo write, no Jira write, no browser.

**Skills.** `run-orchestration`, `budget-governor`, `escalation-policy`, `blast-radius-rules`.

**Why it exists.** Without a single owner of the state machine, agents duplicate work, retry storms happen, and cost is unbounded. Conductor is the only agent allowed to start other agents.

**Failure mode to watch.** Becoming a bottleneck that serialises everything. Keep its reasoning shallow — routing, not analysis.

---

#### 4.2 CARTOGRAPHER — System & Product Mapper

**Role.** Builds and maintains the shared ground truth: service catalog, module dependency graph, ownership map (CODEOWNERS → team → Jira component), route inventory, API surface, DB schema snapshot, event/topic catalog, and the product's UI task graph.

**Runs when.** On a schedule (daily) and on structural change (new service, new migration, new route).

**Inputs.** Repo trees, OpenAPI specs, migration files, router configs, Figma file structure, Confluence architecture pages.

**Outputs.** A versioned `system-map.json` consumed by every other agent. This is the artifact that keeps 14 agents from each re-deriving the codebase.

**Tools.** GitHub MCP (read), Filesystem MCP (read), Postgres MCP (read-only, schema introspection), Atlassian MCP (Confluence read), Figma MCP (read).

**Skills.** `repo-cartography`, `dependency-graph-build`, `ownership-resolution`, `product-task-graph`, `api-surface-extraction`.

**Why it exists.** This is the highest-leverage agent in the system. Every other agent gets cheaper and more accurate because the map already exists. Skipping it means every discovery agent burns context rediscovering the same structure.

---

### Layer 1 — Discovery specialists

Each discovery agent produces **draft** DefectEnvelopes. None of them file tickets. None of them write code.

---

#### 4.3 KEYSTONE — Architecture Analyst

**Domain.** Structure, boundaries, coupling, drift.

**Detects.**
- Circular dependencies between modules or services
- Layering violations (UI importing data-access, domain importing framework)
- God modules, fan-in/fan-out outliers
- Duplicated domain logic across services
- Drift between ADRs/architecture docs and the actual code
- Missing or wrong service boundaries (shared DB tables across service lines)
- Dead code and orphaned endpoints

**Tools.** GitHub MCP (read), Filesystem MCP (read), Atlassian MCP (Confluence read for ADRs), Context7 (framework conventions), Test Runner MCP (read coverage maps).

**Skills.** `dependency-cycle-analysis`, `layering-rules` (encodes *your* rules), `adr-drift-check`, `coupling-metrics`, `dead-code-detection`.

**Severity bias.** Mostly Major/Minor. Rarely blocks a release on its own but drives the highest-value refactor tickets.

**Failure mode.** Opinion spam — filing "this could be cleaner" as defects. Constrain it with an explicit rules file. If a finding does not violate a written rule, it becomes a *Tech Debt* ticket type, not a *Bug*.

---

#### 4.4 VAULT — Database & Data Integrity Analyst

**Domain.** Schema, migrations, query behaviour, data correctness.

**Detects.**
- Destructive or non-reversible migrations; missing down-migrations
- Migrations that lock tables under load
- Missing indexes on foreign keys and frequent filter columns; unused indexes
- N+1 query patterns traced from ORM call sites
- Missing constraints (nullable-that-shouldn't-be, absent FKs, no unique constraint behind a uniqueness assumption)
- Query plans that regress against a baseline
- Transaction boundary errors, isolation-level mismatches
- PII stored unencrypted or in the wrong column class

**Tools.** Postgres MCP Pro (read-only role, restricted mode — plans and health, never production writes), GitHub MCP (read), Filesystem MCP (read), Grafana MCP (DB dashboards).

**Skills.** `migration-review`, `query-plan-analysis`, `index-audit`, `n-plus-one-detection`, `constraint-gap-analysis`, `pii-column-classification`.

**Critical constraint.** This agent connects to a **read replica or a seeded staging DB**, never to production primary. Restricted mode plus a read-only role, both.

---

#### 4.5 CONDUIT — Backend, API & Contract Analyst

**Domain.** HTTP/gRPC surface, contracts, auth, error behaviour.

**Detects.**
- OpenAPI/schema drift between spec and implementation
- Breaking changes to consumers (removed field, narrowed type, changed status code)
- Missing or wrong authorization checks (endpoint-by-role matrix gaps)
- Non-idempotent handlers on retry-safe verbs
- Inconsistent error taxonomy (mixed shapes, leaked stack traces, wrong status codes)
- Missing pagination, unbounded result sets
- Absent rate limiting on expensive endpoints
- Input validation gaps, mass-assignment exposure
- Timeout and retry misconfiguration between services

**Tools.** Contract Diff MCP (custom), HTTP Probe MCP (custom or `fetch`), GitHub MCP (read), Filesystem MCP (read), Postgres MCP (read), Sentry MCP (real error shapes in the wild).

**Skills.** `openapi-diff`, `authz-matrix-check`, `idempotency-review`, `error-taxonomy`, `contract-test-generation`, `rate-limit-audit`.

**Note.** This agent generates *contract tests* as evidence, not just findings. A CONDUIT defect ships with a failing contract test attached.

---

#### 4.6 PULSE — Realtime & WebSocket Analyst

**Domain.** Persistent connections, streaming, event ordering.

**Detects.**
- Auth bypass on the upgrade handshake (token checked on HTTP, not on WS upgrade)
- No reconnect strategy, or reconnect without jittered backoff (thundering herd)
- Message loss on reconnect; no resume token / sequence number
- Out-of-order delivery where order is assumed
- Missing heartbeat/ping-pong; zombie connections held open
- Backpressure absent — server buffers unboundedly when a client stalls
- Room/channel authorization not re-checked after subscription
- Memory leaks from unremoved listeners
- Fan-out cost blowups (one write → N thousand pushes)
- No graceful degradation to polling

**Tools.** **WebSocket Harness MCP (you build this — see §5.2)**, Playwright MCP (browser-side connection behaviour), Grafana MCP (connection count, message rate), GitHub MCP (read), Chrome DevTools MCP (frame inspection).

**Skills.** `socket-lifecycle-harness`, `reconnect-storm-simulation`, `message-ordering-fuzz`, `backpressure-probe`, `ws-authz-check`, `fanout-cost-model`.

**Why it is its own agent.** WebSocket failure modes are stateful and time-dependent. They do not surface in request/response testing at all. This is the specialism most teams under-serve and it is where the nastiest production incidents come from.

---

#### 4.7 SURFACE — Frontend & UI Explorer

**Domain.** The rendered product, as a user experiences it.

**Detects.**
- Broken flows, dead ends, unreachable states
- Console errors and unhandled promise rejections during real flows
- Visual regressions against baseline or against Figma
- Accessibility failures (WCAG: contrast, focus order, missing labels, keyboard traps)
- Responsive breakpoints breaking layout
- Loading/empty/error states missing or wrong
- Form validation gaps, lost input on error
- Slow interactions (INP), layout shift (CLS)
- State desync after navigation or refresh

**Tools.** Playwright MCP (isolated browser profile), Chrome DevTools MCP (console, network, perf traces), Figma MCP (design source of truth), Filesystem MCP (read), Environment Control MCP (custom — seed data, reset state, toggle flags).

**Skills.** `exploratory-ui-walk`, `a11y-audit`, `visual-diff`, `console-error-triage`, `responsive-matrix`, `form-state-probe`, `interaction-latency`.

**Mode.** Runs two ways: **scripted** (known critical journeys, every PR) and **exploratory** (agent-directed wandering from the Cartographer's task graph, nightly). Exploratory mode is where you find the things nobody wrote a test for.

---

#### 4.8 USHER — Product Navigation & UX Guide

**Domain.** "How do I do X in this product?" — for humans and as a defect signal.

This agent has two jobs and they reinforce each other.

**Job 1 — Assistive.** Answers user and support questions by navigating the actual product: "where do I change billing frequency?" It returns a step-by-step walkthrough with screenshots, grounded in the live UI rather than stale documentation.

**Job 2 — Diagnostic.** Every time it struggles, that is a finding. Steps that took more clicks than expected, labels that did not match user vocabulary, features it could not locate, help docs that contradicted the UI. These become **UX friction** tickets, distinct from bugs.

**Tools.** Playwright MCP, Atlassian MCP (Confluence — help docs), Figma MCP, Defect Memory MCP, Environment Control MCP (demo account).

**Skills.** `product-task-graph`, `walkthrough-writer`, `friction-scoring`, `doc-ui-consistency-check`, `vocabulary-gap-analysis`.

**Output.** Walkthroughs (to users/support) and `ux-friction` envelopes (to triage). Never files a Bug directly.

---

#### 4.9 WARDEN — Security & Dependency Auditor

**Domain.** Vulnerabilities, secrets, supply chain.

**Detects.** SAST findings, hardcoded secrets and keys, vulnerable dependencies with reachable call paths, insecure defaults, missing security headers, overly permissive CORS, injection surfaces, insecure deserialisation, exposed debug endpoints.

**Tools.** Semgrep MCP, GitHub MCP (Dependabot/advisories, read), Filesystem MCP (read), Contract Diff MCP.

**Skills.** `sast-triage`, `secret-scan`, `dependency-reachability`, `security-header-audit`, `cve-severity-contextualisation`.

**Critical rule.** Security findings **never** auto-file into a public Jira project. They route to a restricted project or a private channel. Encode this in CLERK's routing rules, not in WARDEN's prompt.

---

#### 4.10 GAUGE — Performance & Load Analyst

**Domain.** Latency, throughput, resource behaviour under load.

**Detects.** Endpoint latency regressions vs baseline, memory growth under sustained load, connection pool exhaustion, cache miss patterns, cold-start regressions, bundle size growth, unoptimised assets, slow render paths.

**Tools.** Grafana MCP (or Datadog), Chrome DevTools MCP (traces, Lighthouse), Load Runner MCP (custom wrapper around k6/Artillery), Postgres MCP (read).

**Skills.** `load-profile-design`, `latency-baseline-compare`, `trace-analysis`, `bundle-budget-check`, `resource-leak-detection`.

**Runs.** Nightly and pre-release only. Too expensive and too noisy per-PR.

---

### Layer 2 — Triage

---

#### 4.11 FORGE — Reproduction Engineer

**Role.** Turns a finding into a deterministic, minimal reproduction plus a **failing test**. Findings it cannot reproduce are demoted or dropped.

**Runs when.** On every draft DefectEnvelope before it reaches CLERK.

**Outputs.** A repro recipe (exact env, seed data, steps), a failing automated test committed to a scratch branch, and a confidence score. Marks `NOT_REPRODUCIBLE` or `FLAKY` where appropriate.

**Tools.** Playwright MCP, Test Runner MCP (custom), Environment Control MCP (custom), GitHub MCP (write to `qa/repro/*` branches only), Filesystem MCP.

**Skills.** `repro-minimisation`, `failing-test-authoring`, `flake-detection` (run N times, measure), `environment-pinning`.

**Why it exists.** This is your noise filter and it is non-negotiable. Without FORGE, discovery agents flood Jira with unreproducible findings and engineers stop trusting the system within two weeks. The failing test it writes also becomes the acceptance criterion PROOF checks later. One artifact, three uses.

---

#### 4.12 CLERK — Triage & Jira Scribe

**Role.** The only agent with Jira write access. Dedupes, scores severity, resolves ownership, writes the ticket, links related work, and routes.

**Steps.**
1. **Dedupe** against the Defect Memory MCP (embedding + fingerprint match). Existing ticket → increment occurrence, add evidence, do not create.
2. **Score** severity and priority against the rubric (§7).
3. **Resolve owner** via the Cartographer ownership map → Jira component + assignee group.
4. **Compose** the ticket: title, repro, evidence links, impact, suggested area, acceptance criteria.
5. **Route**: security → restricted project; UX friction → product backlog; bug → engineering; tech debt → debt backlog.
6. **Label** `agent-found`, plus `agent-ready` if the fix is inside the autonomy envelope (§8).

**Tools.** Atlassian MCP (Jira + Confluence, **write**), Defect Memory MCP, Slack MCP (escalation), GitHub MCP (read, for linking).

**Skills.** `dedupe-strategy`, `severity-rubric`, `jira-ticket-writer` (your house format), `ownership-resolution`, `routing-rules`, `duplicate-merge-etiquette`.

**Rate limit.** Hard cap on tickets created per run, per project, per day. Exceeding it pauses and escalates to a human rather than filing. This one control prevents the most common failure of systems like this.

---

### Layer 3 — Remediation

---

#### 4.13 MENDER — Remediation Engineer

**Role.** Picks up `agent-ready` tickets and produces a pull request.

Deliberately **one agent, many skills** rather than five domain-specific fixers. Fixing is one activity — read ticket, understand code, write minimal change, make the failing test pass, don't break neighbours. The domain knowledge lives in loadable skills, not in separate contexts. Five fixer agents would mean five nearly identical prompts and constant misrouting on cross-cutting bugs.

**Loop.**
1. Pull ticket + repro + failing test.
2. Read affected code with the system map for context.
3. Write the minimal fix. Make the failing test pass. Add regression tests.
4. Run the affected test suite locally.
5. Open a PR linked to the ticket, with a rollback note.
6. Transition the ticket to In Review.

**Tools.** GitHub MCP (write to `fix/*` branches, open PRs — **never** merge, never push to main), Filesystem MCP (write, workspace only), Test Runner MCP, Context7 (library docs), Postgres MCP (read), Playwright MCP (verify UI fixes).

**Skills.** `test-first-fix`, `minimal-diff-discipline`, `patch-etiquette` (your code conventions), `rollback-plan-authoring`, plus domain playbooks: `fix-frontend`, `fix-backend-api`, `fix-database`, `fix-websocket`, `fix-architecture`.

**Hard limits.** Cannot touch migrations, auth code, payment paths, or infrastructure config without human approval. Cannot exceed a configured diff size. Cannot modify the failing test that defines success. All three are enforced in tooling, not prompting.

---

#### 4.14 ARBITER — Review & Risk Gate

**Role.** Reviews MENDER's PR as an adversarial reviewer. Judges correctness, scope creep, hidden regressions, and risk.

**Checks.** Does the fix address the root cause or the symptom? Is the diff minimal? Does it break contracts, schemas, or public API? Does it introduce security or performance regressions? Are regression tests real, or asserted-to-pass? Is the rollback plan viable?

**Outputs.** `APPROVE` / `REQUEST_CHANGES` (with specifics, back to MENDER) / `ESCALATE_TO_HUMAN`.

**Tools.** GitHub MCP (read + PR comment), Semgrep MCP, Contract Diff MCP, Filesystem MCP (read). **No write access to code.**

**Skills.** `adversarial-review`, `root-cause-vs-symptom`, `regression-risk-scoring`, `test-quality-audit`.

**Why separate from MENDER.** Same reason as finder/fixer. A model reviewing its own diff in the same context reliably rationalises it.

---

#### 4.15 PROOF — Verification & Regression Gate

**Role.** The closing authority. Re-runs FORGE's original repro against the patched build and confirms the defect is gone and nothing else broke.

**Steps.** Deploy PR to an ephemeral environment → run the original failing test (must now pass) → run the full regression suite for affected areas → run a targeted SURFACE/PULSE pass if UI or realtime was touched → compare performance baseline → post verdict to the ticket.

**Outputs.** `VERIFIED` (ticket → Done, PR ready for human merge) or `NOT_FIXED` (reopen, back to MENDER with the delta) or `REGRESSED` (block, escalate).

**Tools.** Test Runner MCP, Playwright MCP, WebSocket Harness MCP, Environment Control MCP, Atlassian MCP (**transition only**, not create), GitHub MCP (status checks).

**Skills.** `verification-protocol`, `regression-suite-selection`, `baseline-comparison`, `verdict-reporting`.

---

#### 4.16 CHRONICLE — Reporting *(optional, phase 4)*

Weekly quality reports, trend analysis, agent performance metrics, hot-spot identification. Read-only across Jira, GitHub, and the Defect Memory. Add it once you have three months of data and not before.

---

## 5. Integrations

### 5.1 Off-the-shelf MCP servers

| # | Server | Purpose | Used by | Access |
|---|---|---|---|---|
| 1 | **Playwright MCP** (`@playwright/mcp`, Microsoft) | Browser automation, accessibility tree, screenshots, E2E | SURFACE, USHER, FORGE, PULSE, MENDER, PROOF | Isolated browser profile |
| 2 | **Chrome DevTools MCP** | Console, network, performance traces, WS frame inspection | SURFACE, GAUGE, PULSE | Clean profile |
| 3 | **Atlassian MCP** (official Rovo remote server) | Jira issues, transitions, Confluence read | CLERK (write), PROOF (transition), others read | OAuth 2.1, dedicated bot user |
| 4 | **GitHub MCP** (or GitLab equivalent) | Repos, PRs, issues, checks, advisories | Nearly all | Scoped per agent |
| 5 | **Sentry MCP** | Production errors, real-world failure shapes | CONDUIT, WARDEN, CLERK | Read, single project via OAuth |
| 6 | **Postgres MCP Pro** | Query plans, index health, schema | VAULT, CONDUIT, MENDER | Read-only role + restricted mode |
| 7 | **Semgrep MCP** | SAST, custom rule enforcement | WARDEN, ARBITER, KEYSTONE | Read |
| 8 | **Figma MCP** (Dev Mode) | Design source of truth for visual diffs | SURFACE, USHER, CARTOGRAPHER | Read |
| 9 | **Grafana MCP** (or Datadog) | Metrics, dashboards, alerting context | GAUGE, VAULT, PULSE | Read |
| 10 | **Context7** | Current library/framework docs | KEYSTONE, MENDER | Public docs |
| 11 | **Slack MCP** | Human-in-loop escalation, notifications | CONDUCTOR, CLERK | Post to specific channels |
| 12 | **Filesystem MCP** | Local workspace file access | Most | Read; write only for FORGE/MENDER |

**Jira deployment note.** Atlassian's official hosted server is Cloud-only and uses OAuth 2.1. If you are on Jira Server or Data Center, use the community `sooperset/mcp-atlassian` server (self-hosted, Docker, PAT auth) instead. Confirm current endpoints and auth before you wire anything — Atlassian has already deprecated one endpoint in this product's lifetime.

**Protocol note.** The MCP spec revision dated 2026-07-28 is a breaking change: the initialize handshake and session ID are removed (stateless core), servers must act as formal OAuth 2.1 resource servers, and Sampling/Roots/Logging are deprecated on a 12-month clock. Pin your SDK versions, target this revision for anything you build, and check that each third-party server you adopt has shipped a release against it. Roughly a third of the public server ecosystem is actively maintained; treat the rest as unusable.

### 5.2 MCP servers you need to build

No adequate off-the-shelf option exists for these. Budget real engineering time.

| # | Server | Why you must build it | Core tools to expose |
|---|---|---|---|
| 1 | **WebSocket Harness MCP** | Nothing off-the-shelf drives stateful socket testing. This is PULSE's entire capability. | `connect(url, auth)`, `send(frame)`, `assert_ordering(seq)`, `simulate_disconnect(mode)`, `reconnect_storm(n, jitter)`, `stall_consumer(ms)`, `measure_backpressure()`, `capture_frames(duration)` |
| 2 | **Test Runner MCP** | Agents need structured pass/fail/flake data, not scraped CLI output. | `run_suite(selector)`, `run_single(test)`, `run_n_times(test, n)` (flake detection), `get_coverage(paths)`, `affected_tests(diff)` |
| 3 | **Environment Control MCP** | Deterministic repro is impossible without controlled state. | `spin_up(branch)`, `seed(fixture)`, `reset()`, `set_flag(k,v)`, `set_clock(t)`, `impersonate(role)`, `tear_down()` |
| 4 | **Defect Memory MCP** | Dedupe is the difference between a useful system and ticket spam. Needs vector + fingerprint search over historical defects. | `search_similar(envelope)`, `fingerprint(envelope)`, `record(envelope, jira_key)`, `get_occurrences(fp)`, `mark_resolved(fp)` |
| 5 | **Contract Diff MCP** | Semantic API-change detection, not text diff. | `diff_openapi(a,b)`, `classify_breaking(change)`, `find_consumers(endpoint)`, `generate_contract_test(endpoint)` |

Optionally a sixth, **Load Runner MCP**, wrapping k6 or Artillery for GAUGE. You can start by shelling out to the CLI and promote it to an MCP server later.

### 5.3 Per-agent tool allowlist

| Agent | MCP servers | Count |
|---|---|---|
| CONDUCTOR | GitHub, Slack, Defect Memory | 3 |
| CARTOGRAPHER | GitHub, Filesystem, Postgres, Atlassian, Figma | 5 |
| KEYSTONE | GitHub, Filesystem, Atlassian, Context7, Semgrep | 5 |
| VAULT | Postgres, GitHub, Filesystem, Grafana | 4 |
| CONDUIT | Contract Diff, GitHub, Filesystem, Sentry, Postgres | 5 |
| PULSE | WS Harness, Playwright, Chrome DevTools, Grafana, GitHub | 5 |
| SURFACE | Playwright, Chrome DevTools, Figma, Env Control, Filesystem | 5 |
| USHER | Playwright, Atlassian, Figma, Env Control, Defect Memory | 5 |
| WARDEN | Semgrep, GitHub, Filesystem, Contract Diff | 4 |
| GAUGE | Grafana, Chrome DevTools, Load Runner, Postgres | 4 |
| FORGE | Playwright, Test Runner, Env Control, GitHub, Filesystem, WS Harness | 6 |
| CLERK | Atlassian, Defect Memory, Slack, GitHub | 4 |
| MENDER | GitHub, Filesystem, Test Runner, Context7, Postgres, Playwright | 6 |
| ARBITER | GitHub, Semgrep, Contract Diff, Filesystem | 4 |
| PROOF | Test Runner, Playwright, WS Harness, Env Control, Atlassian, GitHub | 6 |

None exceeds 6. That is intentional.

---

## 6. The Defect Envelope

The one contract every agent speaks. Validate on write and on read; reject malformed envelopes rather than repairing them.

```jsonc
{
  "envelope_version": "1.0",
  "id": "uuid",
  "run_id": "uuid",
  "discovered_by": "PULSE",
  "discovered_at": "ISO8601",

  "domain": "architecture|database|api|websocket|frontend|ux|security|performance",
  "class": "bug|regression|ux-friction|tech-debt|vulnerability|perf-regression",

  "title": "one line, under 90 chars",
  "summary": "2-4 sentences: what breaks, when, for whom",

  "location": {
    "service": "checkout-api",
    "paths": ["src/ws/session.ts:142"],
    "endpoint": "WS /v1/orders/stream",
    "ui_route": "/checkout/review",
    "commit_sha": "abc123"
  },

  "evidence": [
    { "type": "screenshot|trace|query_plan|log|frame_capture|test_output|har",
      "uri": "artifact://...", "note": "" }
  ],

  "reproduction": {
    "status": "reproduced|flaky|not_reproducible|unattempted",
    "environment": { "branch": "", "fixture": "", "flags": {} },
    "steps": ["..."],
    "failing_test": "tests/ws/reconnect.spec.ts::resumes_after_drop",
    "flake_rate": 0.0,
    "verified_by": "FORGE"
  },

  "impact": {
    "user_facing": true,
    "affected_surface": "all authenticated users on mobile web",
    "data_loss_risk": false,
    "security_relevant": false,
    "frequency_estimate": "every reconnect"
  },

  "severity": "blocker|critical|major|minor|trivial",
  "confidence": 0.0,

  "suggested_owner": { "component": "Realtime", "team": "platform" },
  "suggested_fix_area": "add sequence-number resume in session.ts",
  "autonomy_eligible": true,

  "dedupe": {
    "fingerprint": "sha256:...",
    "similar_to": ["PROJ-1284"],
    "occurrence_count": 3
  },

  "jira": { "key": null, "project": null, "status": null }
}
```

---

## 7. Severity rubric

Write this down and make it the `severity-rubric` skill. Agents scoring severity by vibe is the second-fastest way to lose engineering trust.

| Severity | Definition | Example |
|---|---|---|
| **Blocker** | Data loss, security breach, or core flow fully broken in production path | WS auth bypass; migration drops a column |
| **Critical** | Major feature unusable, no workaround, affects most users | Checkout fails on reconnect |
| **Major** | Feature degraded or broken for a subset; workaround exists | Missing index causes 8s page load |
| **Minor** | Cosmetic, edge-case, or low-frequency | Focus ring missing on one button |
| **Trivial** | Polish, cleanup, non-user-facing | Unused import, dead code |

**Confidence gate.** Envelopes with `confidence < 0.6` never auto-file. They go to a human review queue.

---

## 8. Guardrails

### 8.1 Write permission matrix

| Resource | Who can write | Constraint |
|---|---|---|
| Jira (create) | CLERK only | Rate-limited; restricted routing for security |
| Jira (transition) | CLERK, PROOF | PROOF may only close verified |
| Git branches | FORGE (`qa/repro/*`), MENDER (`fix/*`) | Never main, never force-push |
| Pull requests | MENDER (open only) | Merge is always human |
| Filesystem | FORGE, MENDER | Sandboxed workspace only |
| Database | Nobody | Read-only replicas across the board |
| Production | Nobody | Ever |
| Infra / secrets | Nobody | Ever |

### 8.2 Autonomy envelope

MENDER may act without human pre-approval only when **all** hold:

- Severity is Major or below, or Critical with an unambiguous single-line fix
- The diff touches fewer than N files and fewer than M lines (start with 5 / 150)
- No migration, auth, payment, or infra config file is touched
- A failing test exists and the fix makes it pass
- The affected code has existing test coverage above a threshold
- The ticket carries `agent-ready`

Everything else stops at a human. Tighten these limits at first; loosen them from evidence, not optimism.

### 8.3 Loop breakers

- Max 2 MENDER→ARBITER round trips per ticket, then escalate
- Max 1 reopen from PROOF per ticket, then escalate
- Global per-run token and wall-clock budget, enforced by CONDUCTOR
- If a discovery agent produces more than K findings in one run, pause and escalate rather than file

### 8.4 Human-in-loop gates

Mandatory human touch at: PR merge, Blocker/Critical triage, any security ticket, any autonomy-envelope exception, and weekly sampling of ten auto-closed tickets to audit quality.

---

## 9. Run modes

| Mode | Trigger | Agents | Budget |
|---|---|---|---|
| **PR check** | PR opened/updated | CONDUIT, VAULT, WARDEN, SURFACE (scripted), KEYSTONE | Fast, ~10 min |
| **Nightly sweep** | Cron | All discovery, SURFACE + USHER in exploratory mode | Deep, ~2 h |
| **Pre-release** | RC cut | All discovery + GAUGE full load profile | Full, blocking |
| **On-demand** | Human asks | CONDUCTOR selects by question | Scoped |
| **Incident** | Alert fires | CONDUIT + VAULT + PULSE + GAUGE, read-only, no filing | Fast, diagnostic |
| **Fix cycle** | `agent-ready` ticket | MENDER → ARBITER → PROOF | Per ticket |

---

## 10. Failure modes and controls

| Failure | Control |
|---|---|
| **Ticket spam** — hundreds of low-value tickets, team disengages | FORGE gate, confidence threshold, per-run creation caps, dedupe |
| **Duplicate storms** — same defect filed under different phrasings | Defect Memory MCP with fingerprint + embedding dedupe, mandatory |
| **Symptom fixes** — MENDER patches the test, not the bug | ARBITER root-cause check; MENDER cannot edit the defining test |
| **Flaky tests become defects** | FORGE runs N times and records flake rate; flakes route to a separate quarantine queue |
| **Agents disagree, work stalls** | Explicit tiebreaker: PROOF's verdict is final; ARBITER escalates rather than loops |
| **Context poisoning** — bad map propagates everywhere | Cartographer output is versioned and validated; agents pin a map version per run |
| **Cost blowup** | CONDUCTOR budget governor; expensive agents (GAUGE) on schedule only |
| **Tool sprawl degrades reasoning** | Hard per-agent allowlists (§5.3), reviewed whenever an agent is added |
| **Security findings leak into public tickets** | CLERK routing rules, enforced before the Jira call |

---

## 11. Phased rollout

**Phase 1 — Prove the loop (weeks 1–6). 6 agents.**
CARTOGRAPHER, SURFACE, CONDUIT, FORGE, CLERK, PROOF.
Discovery on two surfaces only. No auto-fix at all. Goal: does the system file tickets an engineer is glad to receive? Measure precision. If under 70% accepted, stop and tune before adding anything.

**Phase 2 — Add depth (weeks 7–12). +4 → 10 agents.**
VAULT, PULSE, WARDEN, CONDUCTOR.
Build the WebSocket Harness and Environment Control MCP servers here. Still no auto-fix.

**Phase 3 — Close the loop (weeks 13–20). +3 → 13 agents.**
MENDER, ARBITER, plus KEYSTONE.
Auto-fix enabled with the tightest possible autonomy envelope: trivial and minor only, human merge always.

**Phase 4 — Scale (week 21+). +2 → 15 agents.**
USHER, GAUGE, optionally CHRONICLE. Widen the autonomy envelope based on measured ARBITER approval and PROOF verification rates.

---

## 12. Metrics that matter

**Discovery quality.** Ticket acceptance rate (target >80%), duplicate rate (<5%), false positive rate (<15%), escaped defects found in production that agents should have caught.

**Triage quality.** Reproduction success rate, severity agreement with human reviewers, ownership routing accuracy.

**Remediation quality.** ARBITER first-pass approval rate, PROOF verification rate, human merge rate without changes, regression rate of agent-authored fixes (this is the one that decides whether you widen autonomy).

**System health.** Cost per accepted ticket, cost per verified fix, mean time from discovery to verified fix, human escalation rate.

Track ticket acceptance rate from day one. It is the single number that tells you whether engineers trust the system.

---

## 13. Open decisions for you

1. **Jira Cloud or Data Center?** Determines official vs community MCP server and the whole auth story.
2. **Monorepo or polyrepo?** Changes CARTOGRAPHER's design substantially.
3. **Do ephemeral environments exist today?** If not, Environment Control MCP is the long pole and Phase 1 slips.
4. **Which database?** The spec assumes Postgres. MySQL/Mongo change VAULT's tooling.
5. **Existing E2E suite?** If yes, SURFACE bootstraps from it. If no, add 3–4 weeks.
6. **Where does agent execution run?** CI runners, a dedicated orchestration service, or Claude Code sessions — this shapes concurrency and cost control.
7. **What is the tolerable false-positive rate for your team?** This sets every threshold in §7 and §8.

---

*Next step suggestion: pick the answers to §13, then cut this down to a Phase 1 build plan with a concrete two-week sprint scope.*
