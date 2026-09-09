<div align="center">

# 🐦‍⬛ qaas

### A multi-agent QA system that finds real defects — and proves it

**Reads your application → finds defects → reproduces each with a failing test → files the ticket → fixes it → reviews the fix → verifies it.**

[![PyPI](https://img.shields.io/pypi/v/qaas-python?color=3775A9&logo=pypi&logoColor=white)](https://pypi.org/project/qaas-python/)
[![Python](https://img.shields.io/pypi/pyversions/qaas-python?color=3776AB&logo=python&logoColor=white)](https://pypi.org/project/qaas-python/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![CI](https://github.com/allaabdella2-us/qa-multi-agent-system/actions/workflows/ci.yml/badge.svg)](https://github.com/allaabdella2-us/qa-multi-agent-system/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-649%20offline-success)](#-contributing)
[![Built on](https://img.shields.io/badge/built%20on-Claude%20Agent%20SDK-D97757)](https://docs.claude.com/en/api/agent-sdk/overview)

[Quickstart](#-quickstart-in-60-seconds) · [Your repo](#-point-it-at-your-repository) · [Jira](#-file-into-jira) · [Architecture](ARCHITECTURE.md)

</div>

---

Most "AI QA" tools generate tests. **This one behaves like a QA team.**

Ten agents, each with its own context, tool allowlist and budget, coordinated by
a state machine that is ordinary Python — because a model cannot enforce a budget
it is itself spending.

```
      DISCOVERY LOOP                                   REMEDIATION LOOP
 ┌──────────────────────────────────────┐        ┌───────────────────────────┐
 │  CARTOGRAPHER ─▶ CONDUIT ─┐          │        │   MENDER ─▶ ARBITER       │
 │   (system map)   SURFACE ─┴─▶ FORGE ─┼─▶ CLERK│    (fix)     (review)     │
 │                 (discover)   (repro) │  (file)│                           │
 └──────────────────────────────┬───────┘        └──────────┬────────────────┘
                                │                           │
                                ▼                           ▼
                           [ TICKET ] ◀──────────────  PROOF (verify)
```

Nothing crosses between the loops except a ticket — which is also the audit trail.

---

## ✨ Why this one is different

| | |
|---|---|
| 🧠 **The orchestrator is code, not a prompt** | A model cannot enforce a budget it is spending. Phase ordering, concurrency, retries and the loop breakers live in `conductor.py`. That is also why **649 tests run offline, free, with no API key.** |
| 🧱 **Every agent is its own `query()`** | Not subagents of a shared parent. Each gets a real context boundary, an enforceable tool allowlist, and its own cost number. |
| 🔬 **Evidence or it did not happen** | `has_evidence()` and `is_fileable()` are methods on the envelope model, not requests in a prompt. An agent cannot talk its way past them. |
| 📊 **Measured, not asserted** | A deliberately buggy demo app ships with a golden ledger of **16 seeded defects + 4 planted non-defects**. `qaas score` reports recall *and* precision, so a prompt change has a number attached. |
| 🔒 **Merge is impossible by construction** | No merge method exists anywhere. `gh pr merge` is refused. Pull requests open as drafts. Shipping stays a human decision. |
| 🔍 **Every action is on the record** | 28 kinds of ledger event — every tool call, denial, verdict and escalation. `qaas trace` reads it back as a timeline. |

---

## 🤖 The roster

| agent | layer | what it does |
|---|---|---|
| 🗺️ CARTOGRAPHER | map | services, routes, schema, ownership → the system map everything reads |
| 🏛️ KEYSTONE | discovery | circular deps, layering violations, god modules, dead code |
| 🔌 CONDUIT | discovery | API contract drift, authz gaps, error-shape inconsistency |
| 🖱️ SURFACE | discovery | drives the UI through real journeys |
| 🗄️ VAULT | discovery | schema constraints the code assumes and the database does not enforce |
| 🔒 WARDEN | discovery | missing authorization, secrets, vulnerable dependencies, leaks |
| 📡 PULSE | discovery | WebSocket auth, reconnect, ordering, backpressure |
| 🧭 USHER | discovery | whether a person can *find* a feature, not just whether it works |
| ⏱️ GAUGE | discovery | N+1 queries, unindexed hot paths, unbounded results, bundle outliers |
| 🔨 FORGE | triage | reproduces, minimises, measures flake, commits a failing test |
| 📝 CLERK | triage | dedupes, scores severity, routes, files — the only tracker writer |
| 🔧 MENDER | remediation | the minimal fix, on a branch |
| ⚖️ ARBITER | remediation | adversarial review: APPROVE / REQUEST_CHANGES / ESCALATE |
| ✅ PROOF | verify | re-runs the original test → VERIFIED / NOT_FIXED / REGRESSED |
| 📊 CHRONICLE | reporting | what the run found, what recurred, and what it could not reach |

**CONDUCTOR** is the sixteenth. It is the Python state machine rather than an
agent — a model cannot enforce a budget it is itself spending.

---

## 🚀 Quickstart in 60 seconds

```bash
pip install qaas-python
```

```bash
cd ~/code/your-app
qaas init .                          # inspects the repo, writes + activates a target profile
qaas validate                        # config, prompts, allowlists      ← no API call
qaas run --mode pr-check --dry-run   # every agent's exact options      ← no API call
```

Or point it straight at a URL:

```bash
qaas run --repo https://github.com/you/your-app --dry-run
```

> [!TIP]
> Nothing above contacts an API. `--dry-run` prints exactly what each agent would
> receive — model, budget, turn cap, tool allowlist, prompt size.

**Auth:** the Claude Code CLI if you are signed in, otherwise `ANTHROPIC_API_KEY`.

---

## 🎯 Point it at your repository

```bash
qaas init https://github.com/you/your-app     # clones, inspects, writes a profile
qaas init ~/code/your-app --name your-app     # or a local path
```

`init` writes `.qaas/config/targets/<name>.yaml` **and activates it**. Every value
in it is a **guess you are expected to correct** — it reports what it detected and
what it could not find.

The load-bearing field is `environment.mode`:

| mode | meaning | what agents may do |
|---|---|---|
| 🔵 `none` | no running instance | read code, schema and spec only |
| 🟡 `external` | already running (staging, dev server) | read and exercise, **never** reset |
| 🟢 `compose` | qaas owns the lifecycle | seed, reset, tear down |

> [!NOTE]
> `none` is a perfectly good place to start, and where most first runs against a
> real repository begin. Findings stay honest about it: an agent that could not
> observe a behaviour says so and lowers its confidence.

**Credentials never live in the profile.** It names environment variables:

```yaml
auth:
  mode: login
  login_endpoint: POST /api/v1/session
  roles:
    admin:  { username: qa-admin@example.com,  password_env: APP_ADMIN_PASSWORD }
    viewer: { username: qa-viewer@example.com, password_env: APP_VIEWER_PASSWORD }
```

---

## 🎫 File into Jira

```bash
export JIRA_BASE_URL=https://you.atlassian.net
export JIRA_EMAIL=you@example.com
export JIRA_API_TOKEN=...            # an API token, not a password
export JIRA_PROJECT_KEY=QA

QAAS_TRACKER=jira qaas tracker-check  # auth, project, permissions, workflow — creates nothing
QAAS_TRACKER=jira qaas run --mode nightly
```

`tracker-check` validates credentials, confirms the project and issue type exist,
maps your workflow statuses, and prints the exact JSON it *would* POST. **It
creates nothing.**

- 🔁 **Dedupe across runs** — tickets carry a `qaas-fp-<fingerprint>` label, so the next run recognises an already-filed defect and increments its occurrence count instead of filing again.
- 🔐 **Security findings are refused** unless `JIRA_SECURITY_PROJECT_KEY` is set. A vulnerability in a project the whole company can read is a disclosure with no undo.

> [!TIP]
> Keep the committed backend `local` — it writes tickets as JSON under
> `.qaas/tickets/` so you can read what *would* be filed. Switch per shell with
> `QAAS_TRACKER=jira`.

---

## 🔌 Bring your own MCP servers

Declare them in `system.yaml`. No Python to edit.

```yaml
mcp_servers:
  house-lint:
    type: stdio
    command: ./tools/lint-mcp
    args: ["--strict"]
    env: { LINT_TOKEN: "${ACME_LINT_TOKEN}" }   # from the environment, never a literal
  remote-docs:
    type: http
    url: https://mcp.example/v1
```

Then add the name to any agent's `mcp_servers:` list. **Declaring a server grants
nothing** — an agent receives it only by naming it.

```console
$ qaas validate
                       Declared MCP servers
┃ name        ┃ kind  ┃ what it runs              ┃ used by      ┃
│ house-lint  │ stdio │ ./tools/lint-mcp --strict │ CARTOGRAPHER │
│ remote-docs │ http  │ https://mcp.example/v1    │ nobody       │
```

> [!WARNING]
> A server's tools are allowed **wholesale** once an agent names it. qaas checks
> that the agent declared the server; it cannot inspect what a third-party
> server's tools actually do. **A server you declare is a server you trust.**

There is deliberately **no in-process Python server type** — a module path from a
config file would mean importing arbitrary code into the process holding your
Anthropic, Jira and GitHub credentials. Wrap it in a stdio entry point instead.

Two limits worth knowing: **6 MCP servers per agent** (tool-selection accuracy
falls off past ~7), and **MENDER is already at the cap** — the agent people most
want to extend.

---

## ✏️ Make the prompts yours

Agents are **a prompt plus a YAML file**. Both are yours to change.

```bash
qaas prompts list              # which prompt is in force, and where it came from
qaas prompts eject CONDUIT     # copy it to .qaas/prompts/ and edit freely
qaas prompts diff              # what you changed vs. what shipped
```

Prefer **adding** to replacing — drop a `CONDUIT.append.md` beside it:

```markdown
## Our conventions
- Never file a finding without a curl reproduction.
- Treat any 500 on a write path as blocker severity.
```

That block is inserted between the agent's prompt and the shared house rules, so
you keep receiving improvements to the base prompt instead of forking it forever.

---

## 🔍 Full traceability

Every tool call, denial, verdict and escalation is on the record.

```console
$ qaas trace run-20260908T182034-c6ed26
    t+  agent         kind            detail
    0s  -             run_started     mode=nightly  agents=[7]
    0s  CARTOGRAPHER  agent_started   model=claude-sonnet-5
    4s  CARTOGRAPHER  tool_call ×34   Read×25, Glob×6, ToolSearch×2
    6s  CARTOGRAPHER  denial          tool=Bash  reason=Bash is not in CARTOGRAPHER's
                                      tool allowlist (Read, Grep, Glob).
  146s  CARTOGRAPHER  system_map      version=20260907T233530  sections=[12]
  156s  CARTOGRAPHER  agent_finished  subtype=success  num_turns=45
```

```bash
qaas trace <run-id> --agent proof --kind verdict   # filter
qaas trace <run-id> --json                         # export
qaas show <run-id>                                 # mode, commit, tickets, escalations
qaas runs                                          # everything that ever ran
```

Runs are pinned to the **commit of the target** they examined, so a finding can
be replayed against the tree that produced it.

---

## 🛡️ Safety rails

Enforced in code, not requested in a prompt:

| rail | what it does |
|---|---|
| 📁 **Path scoping** | Writes checked against that agent's `write_paths`. Most agents cannot write at all. |
| 🌿 **Branch scoping** | Git writes must match the agent's patterns (`qa/repro/*`, `fix/*`). `main` and force-push refused outright. |
| ⛔ **Forbidden classes** | Migrations, auth, payment, secrets, infrastructure, CI — stop at a human however small the change looks. |
| 🎟️ **Ticket rate limit** | Over the per-run cap the call is denied and the conductor escalates rather than filing. |
| 🧪 **Immutable test** | The agent fixing a defect may not edit the test that defines it. |
| 🚫 **No filesystem settings** | `setting_sources=[]` — a repository qaas is inspecting cannot inject settings, hooks or MCP servers into the process running it. |

Denials return a reason and are logged; they never kill the turn. The agent reads
the refusal and adapts.

---

## 📈 Benchmarks, honestly

Against the bundled demo app and its golden ledger:

Two runs against the demo app, scored automatically:

| metric | run A (full loop) | run B (discovery only) |
|---|--:|--:|
| 🎯 recall | **81%** — 13 of 16 | **69%** — 11 of 16 |
| 🔇 precision | **100%** — 0 FP | **92%** — 1 FP |
| 🏷️ severity agreement | **100%** | **100%** |

**Both numbers are shown on purpose.** A single figure would be the flattering
one, and it would not survive contact with a second run. These are stochastic
agents: the two runs did not find the same 11–13 defects — run B caught a
contrast failure run A missed, and missed three run A found. Expect variance of
this order.

The ledger's `not_defects` section plants **correct-but-suspicious** code, so
precision is measured rather than assumed.

> [!CAUTION]
> **Treat this as a floor, not a proof.** Seeded defects are easier than real
> ones and the system was calibrated against them. Two runs is not a sample.
> These numbers show the loop works end to end and does not spray false
> positives — not that it will find the hard bug in your codebase.

---

## 📋 Status

Honest about what exists:

- ✅ **All 16 agents in the design are built.**
- ✅ **Adding one needs a prompt file and a YAML file — no Python.** Six were added that way, which is how the claim got tested.
- ✅ The fix loop has closed end to end on a real defect: `NOT_FIXED → MENDER → ARBITER APPROVE → VERIFIED`.
- ✅ 30 skills, 7 in-process MCP servers, 649 offline tests.
- ⚠️ Running the bundled demo needs `export CORVID_PASSWORD=password123` — credentials come from the environment, including the demo's.

---

## 🤝 Contributing

```bash
git clone https://github.com/allaabdella2-us/qa-multi-agent-system
cd qa-multi-agent-system
uv venv && uv pip install -e ".[dev]"

pytest                 # 649 tests, offline, free — keep it that way
pytest -m docker       # needs: cd target-app && docker compose up -d
qaas validate
```

The `llm`, `docker`, `github` and `jira` markers are deselected by default.
**A CI run that costs money is a CI run people switch off.**

> [!IMPORTANT]
> If you change a seeded defect in `target-app/`, retire its ledger entry with
> `fixed_in:` in the same commit — a stale ledger silently corrupts every score.

New to the codebase? [`ARCHITECTURE.md`](ARCHITECTURE.md) explains the entry
point, the five phases, what moves between agents, and what the guardrails stop.

---

<div align="center">

**MIT licensed** · [LICENSE](LICENSE) · Built on the [Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/overview)

</div>
