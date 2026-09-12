<div align="center">

# 🐦‍⬛ qaas

### A multi-agent QA system that finds real defects — and proves it

**Reads your application → finds defects → reproduces each with a failing test → files the ticket → fixes it → reviews the fix → verifies it.**

[![PyPI](https://img.shields.io/pypi/v/qaas-python?color=3775A9&logo=pypi&logoColor=white)](https://pypi.org/project/qaas-python/)
[![Python](https://img.shields.io/pypi/pyversions/qaas-python?color=3776AB&logo=python&logoColor=white)](https://pypi.org/project/qaas-python/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](https://github.com/allaabdella2-us/qa-multi-agent-system/blob/main/LICENSE)
[![CI](https://github.com/allaabdella2-us/qa-multi-agent-system/actions/workflows/ci.yml/badge.svg)](https://github.com/allaabdella2-us/qa-multi-agent-system/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-830%20offline-success)](#-contributing)
[![Built on](https://img.shields.io/badge/built%20on-Claude%20Agent%20SDK-D97757)](https://docs.claude.com/en/api/agent-sdk/overview)

[Quickstart](#-quickstart-in-60-seconds) · [Your repo](#-point-it-at-your-repository) · [Jira](#-file-into-jira) · [Roadmap](#️-roadmap) · [Architecture](https://github.com/allaabdella2-us/qa-multi-agent-system/blob/main/ARCHITECTURE.md)

</div>

---

Most "AI QA" tools generate tests. **This one behaves like a QA team.**

Fifteen agents, each with its own context, tool allowlist and budget, coordinated by
a state machine that is ordinary Python — because a model cannot enforce a budget
it is itself spending.

<div align="center">
  <img src="https://raw.githubusercontent.com/allaabdella2-us/qa-multi-agent-system/main/docs/roster.png" alt="The fifteen agents by phase: Map — Mapper. Discovery, eight agents — Architect, API, Browser, DBA, Auditor, Socket, Guide, Load. Triage, two — Reproducer and Triage. Remediation, two — Fixer and Reviewer. Verify — Verifier. Reporting — Reporter." width="900">
</div>

Six phases, and every agent in them is its own `query()`. **ROUTER** is the sixteenth
and is deliberately not in the picture: it is the Python state machine that dispatches
the rest.

---

## ✨ Why this one is different

| | |
|---|---|
| 🧠 **The orchestrator is code, not a prompt** | A model cannot enforce a budget it is spending. Phase ordering, concurrency, retries and the loop breakers live in `router.py`. That is also why **830 tests run offline, free, with no API key.** |
| 🧱 **Every agent is its own `query()`** | Not subagents of a shared parent. Each gets a real context boundary, an enforceable tool allowlist, and its own cost number. |
| 🔬 **Evidence or it did not happen** | `has_evidence()` and `is_fileable()` are methods on the envelope model, not requests in a prompt. An agent cannot talk its way past them. |
| 📊 **Measured, not asserted** | A deliberately buggy demo app ships with a golden ledger of **16 seeded defects + 4 planted non-defects**. `qaas score` reports recall *and* precision, so a prompt change has a number attached. |
| 🔒 **Merge is impossible by construction** | No merge method exists anywhere. `gh pr merge` is refused. Pull requests open as drafts. Shipping stays a human decision. |
| 🔍 **Every action is on the record** | 28 kinds of ledger event — every tool call, denial, verdict and escalation. `qaas trace` reads it back as a timeline. |

---

## 📺 Watch it work

```bash
pip install 'qaas-python[ui]'
qaas run --mode pr-check --dashboard    # or: qaas dashboard
```

<div align="center">
  <img src="https://raw.githubusercontent.com/allaabdella2-us/qa-multi-agent-system/main/docs/dashboard.png" alt="The qaas dashboard during a live run: a phase rail with map, discover, reproduce and file lit and verify and report struck through; one card per agent showing its cost, findings and the tool it is calling right now; a timeline lane per agent; findings by severity with their Jira keys; and the ledger streaming on the right." width="900">
</div>

Fifteen agents do not run in one scrolling column. The dashboard is
**read-only** — it shows a run, it cannot start one — and it reads the same
`ledger.jsonl` that `qaas trace` does, so it opens finished runs too.

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

Tired of four exports in every new shell? Put them in a `.env` — `.qaas/.env` is
already gitignored — and every command reads it. **Anything you export wins over
the file**, so a stale `.env` can never redirect a run.

- 🔁 **Dedupe across runs** — tickets carry a `qaas-fp-<fingerprint>` label, so the next run recognises an already-filed defect and increments its occurrence count instead of filing again.
- 🔐 **Security findings are refused** unless `JIRA_SECURITY_PROJECT_KEY` is set. A vulnerability in a project the whole company can read is a disclosure with no undo.

> [!TIP]
> Keep the committed backend `local` — it writes tickets as JSON under
> `.qaas/tickets/` so you can read what *would* be filed. Switch per shell with
> `QAAS_TRACKER=jira`.

### 📌 A view per repository, made for you

Point it at a new repository and it provisions that repository's own Jira view
before the first agent starts — so there is something to watch *during* the run,
not a report afterwards.

```console
$ QAAS_TRACKER=jira qaas run --repo https://github.com/acme/checkout.git --mode nightly
target: checkout (none)
filter created — https://you.atlassian.net/issues/?filter=10001
every ticket from this run carries the label repo-checkout
```

Every ticket the system files carries `repo-<target>`, stamped in code rather
than asked of an agent. A **saved filter** over exactly that label is the
per-repository view, and on a company-managed project a **board** is built over
the filter too.

```bash
qaas board                  # find or create this target's view
qaas board --no-create      # show the label and JQL, touch nothing
```

> [!NOTE]
> **Not a project per repository.** Creating a Jira project needs administrator
> rights a bot account rarely has, and a project per repository is unmanageable
> by the tenth one. A filter needs no special grant.
>
> **Not always a board, either.** Team-managed (next-gen) projects own their own
> board and cannot have a second one built over a filter — Jira's API will
> happily create one and give it no page in the UI. So the project's style is
> checked first, and on a team-managed project you get the filter alone. You are
> told which you got, and the link always opens. Point `JIRA_PROJECT_KEY` at a
> **company-managed** project and you get a real board per repository, with
> To Do / In Progress / Done.

Run it twice on the same repository and it **reuses** what is there. A run is
never failed over this: a run that found nine defects and could not make a view
has still done its job.

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
│ house-lint  │ stdio │ ./tools/lint-mcp --strict │ MAPPER │
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
falls off past ~7), and **FIXER is already at the cap** — the agent people most
want to extend.

---

## ✏️ Make the prompts yours

Agents are **a prompt plus a YAML file**. Both are yours to change.

```bash
qaas prompts list              # which prompt is in force, and where it came from
qaas prompts eject API     # copy it to .qaas/prompts/ and edit freely
qaas prompts diff              # what you changed vs. what shipped
```

Prefer **adding** to replacing — drop a `API.append.md` beside it:

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
    0s  MAPPER        agent_started   model=claude-sonnet-5
    4s  MAPPER        tool_call ×34   Read×25, Glob×6, ToolSearch×2
    6s  MAPPER        denial          tool=Bash  reason=Bash is not in MAPPER's
                                      tool allowlist (Read, Grep, Glob).
  146s  MAPPER        system_map      version=20260907T233530  sections=[12]
  156s  MAPPER        agent_finished  subtype=success  num_turns=45
```

```bash
qaas trace <run-id> --follow                       # watch a run as it happens
qaas trace <run-id> --quiet                        # decisions only, no file reads
qaas trace <run-id> --agent verifier --kind verdict   # filter
qaas trace <run-id> --json                         # export
qaas show <run-id>                                 # mode, commit, tickets, escalations
qaas runs                                          # everything that ever ran
```

**Watch a run live.** `--follow` tails the ledger of a run in progress — start it
in a second terminal the moment a run begins, or even before, and it waits.

```console
$ qaas trace run-20260909T163240-83b11c --follow --quiet
following run-20260909T163240-83b11c — ctrl-c to stop
16:32:40 -            run_started      mode=pr-check  agents=[8]  target_sha=da19f406
16:32:40 MAPPER       agent_started    model=claude-sonnet-5
16:32:46 MAPPER       denial           tool=Bash  reason=Bash is not in MAPPER's
                                       tool allowlist (Read, Grep, Glob).
16:36:11 ARCHITECT    envelope         severity=major  domain=architecture
16:41:03 TRIAGE       ticket           action=created  key=QA-118  severity=major
```

Drop `--quiet` to see every file the agents read, one line each. Combine with
`--agent` and `--kind` to watch one agent, or only the verdicts.

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
| 🎟️ **Ticket rate limit** | Over the per-run cap the call is denied and the router escalates rather than filing. |
| 🧪 **Immutable test** | The agent fixing a defect may not edit the test that defines it. |
| 🚫 **No filesystem settings** | `setting_sources=[]` — a repository qaas is inspecting cannot inject settings, hooks or MCP servers into the process running it. |

Denials return a reason and are logged; they never kill the turn. The agent reads
the refusal and adapts.

---

## 📈 How you know it works

`qaas` ships with a **deliberately buggy demo app** and a golden ledger recording
every defect seeded into it — plus **planted non-defects**: correct-but-suspicious
code that a careless reader would report.

```bash
cd target-app && docker compose up -d
qaas run --mode nightly && qaas score
```

`qaas score` reports recall, precision and severity agreement against that
ledger. That matters more than any number this page could print: it means a
change to a prompt, a threshold or a model has **a measurement attached** rather
than an opinion.

> [!CAUTION]
> **Seeded defects are easier than real ones**, and this system was calibrated
> against them. A benchmark shows the loop works end to end and does not spray
> false positives. It does not show that it will find the hard bug in your
> codebase. Run it on something you know well and judge it on what it finds
> there.

---

## 🗺️ Roadmap

Today `qaas` runs on the **Claude Agent SDK** only. The next step is making the
model a choice rather than an assumption — and that is where help is most
welcome.

- **🔀 Provider abstraction.** Extract a thin runner interface so `runner.py`
  talks to *a* provider rather than to one. Everything else — the envelope, the
  guardrails, the ledger, the phase machine — is already provider-agnostic; the
  coupling is `ClaudeAgentOptions`, the hook events, and `query()`.
- **🤖 OpenAI SDK.** A second implementation behind that interface, so agents can
  run on GPT models. The interesting work is not the API call: it is mapping tool
  definitions, streamed tool calls and a stop condition onto the same
  `PreToolUse`/`PostToolUse`/`Stop` contract the guardrails and the output
  contract depend on.
- **🌐 OpenRouter.** One endpoint, many models — the cheapest way to answer
  "which model is actually best at *this* agent's job", per agent, with
  `qaas score` as the referee.
- **🦙 Ollama.** Local, open-weight models for the agents that do not need a
  frontier model. Cost per run is the reason discovery fans out so carefully;
  running MAPPER or LOAD locally changes that arithmetic.
- **📊 A per-agent model matrix.** Model choice is already per-agent config
  (`model:` in `config/agents/*.yaml`). Once several providers exist, the honest
  next question is a scored comparison rather than a preference.
- **🧰 More MCP servers and skills** — the parts you can add today without
  touching Python.

### 🙌 Contributions wanted

This is a solo project and the roadmap above is bigger than one person. If any
of it interests you, **please open an issue or a PR** — especially:

- a provider implementation (OpenAI, OpenRouter, Ollama, anything else)
- a new agent, which is a prompt plus a YAML file and *no Python*
- running `qaas` against your own repository and reporting what it got wrong —
  false positives are the most useful bug report this project can receive

Good first issues: add an agent, add a skill, or point it at a codebase you know
well and tell us what it missed.

---

## 🤝 Contributing

```bash
git clone https://github.com/allaabdella2-us/qa-multi-agent-system
cd qa-multi-agent-system
uv venv && uv pip install -e ".[dev]"

pytest                 # 830 tests, offline, free — keep it that way
pytest -m docker       # needs: cd target-app && docker compose up -d
qaas validate
```

The `llm`, `docker`, `github` and `jira` markers are deselected by default.
**A CI run that costs money is a CI run people switch off.**

> [!IMPORTANT]
> If you change a seeded defect in `target-app/`, retire its ledger entry with
> `fixed_in:` in the same commit — a stale ledger silently corrupts every score.

New to the codebase? [`ARCHITECTURE.md`](https://github.com/allaabdella2-us/qa-multi-agent-system/blob/main/ARCHITECTURE.md) explains the entry
point, the five phases, what moves between agents, and what the guardrails stop.

---

<div align="center">

**MIT licensed** · [LICENSE](https://github.com/allaabdella2-us/qa-multi-agent-system/blob/main/LICENSE) 

</div>
