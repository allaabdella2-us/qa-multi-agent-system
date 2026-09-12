<div align="center">

# 📖 qaas — User Manual

**Everything you can do, in the order you will want to do it.**

[Install](#-install) · [First run](#-your-first-run-free) · [Commands](#-command-reference) · [Workflows](#-workflows) · [Troubleshooting](#-troubleshooting)

</div>

> The [README](README.md) is the pitch and a quickstart. This is the reference.
> For how the code works, see [`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## 📦 Install

```bash
pip install qaas-python          # the CLI and import package are both `qaas`
```

Requires **Python 3.12+**.

### Authentication

qaas drives the Claude Agent SDK. It needs one of:

| option | how |
|---|---|
| **Claude Code CLI** (easiest) | `claude` on your `PATH` and signed in — nothing else to do |
| **API key** | `export ANTHROPIC_API_KEY=sk-ant-...` |

### Optional extras

```bash
npx playwright install chromium   # only for UI exploration (the BROWSER agent)
pip install 'qaas-python[ui]'     # only for `qaas dashboard`
```

The `[ui]` extra pulls `starlette`, `uvicorn` and `sse-starlette`. All three
already arrive with the Claude Agent SDK, so in practice it installs nothing
new — it is there so the dashboard pins what it imports.

---

## 🚀 Your first run (free)

Nothing here contacts an API or costs anything.

```bash
cd ~/code/your-app
qaas init .                          # inspect the repo, write + activate a profile
qaas doctor                          # what is possible against this target
qaas validate                        # config, prompts, allowlists all coherent
qaas run --mode pr-check --dry-run   # exactly what each agent would receive
```

`qaas init` writes `.qaas/config/targets/<name>.yaml` and sets it active.

> [!IMPORTANT]
> **Every value in that profile is a guess.** `init` reports what it detected and
> what it could not find. Read it and correct it before a real run — a wrong
> `layout.backend` makes MAPPER explore blind.

Then, when you are ready to spend money:

```bash
qaas run --mode pr-check
```

---

## ⚙️ Configuration

Everything lives under `.qaas/` in your project:

```
.qaas/
├── config/
│   ├── system.yaml          run modes, thresholds, tracker, MCP servers
│   └── targets/<name>.yaml  what your app is and how to reach it
├── prompts/                 only if you ran `qaas prompts eject`
├── runs/<run-id>/           ledger, envelopes, artifacts, per-agent costs
├── tickets/*.json           the local tracker
└── memory.db                defect fingerprints, for cross-run dedupe
```

Anything you do not override falls back to what shipped in the package.

### The target profile

The load-bearing field is `environment.mode`:

| mode | meaning | what agents may do |
|---|---|---|
| 🔵 `none` | no running instance | read code, schema and spec only |
| 🟡 `external` | already running (staging, dev server) | read and exercise, **never** reset |
| 🟢 `compose` | qaas owns the lifecycle | seed, reset, tear down |

```yaml
name: your-app
root: .
default_branch: main

layout:
  backend:  [src/api]
  frontend: [web/src]
  tests:    [tests]
  spec:     openapi.yaml        # optional; enables contract diffing
  ownership: CODEOWNERS         # optional; enables ticket assignment

environment:
  mode: external
  api_url: https://staging-api.example.com
  web_url: https://staging.example.com
  health_path: /healthz

auth:
  mode: login
  login_endpoint: POST /api/v1/session
  username_field: email
  password_field: password
  token_path: access_token
  roles:
    admin:  { username: qa-admin@example.com,  password_env: APP_ADMIN_PASSWORD }
    viewer: { username: qa-viewer@example.com, password_env: APP_VIEWER_PASSWORD }
```

> [!WARNING]
> **Credentials never go in this file.** It names environment variables; you
> export the secrets. Use dedicated QA accounts on a non-production environment.

### Environment variables

| variable | what it does |
|---|---|
| `ANTHROPIC_API_KEY` | auth, if you are not signed into the Claude Code CLI |
| `QAAS_TARGET` | run against a different profile without editing config |
| `QAAS_TRACKER` | `local` (default) or `jira` |
| `QAAS_VCS` | `local` (default) or `github` |
| `QAAS_CONFIG_DIR` | use a config directory somewhere else entirely |
| `JIRA_*` | see [Filing to Jira](#filing-to-jira) |

---

## 🎛️ Run modes

| mode | agents | when |
|---|---|---|
| `incident` | API | diagnose one thing, file nothing |
| `pr-check` | MAPPER, ARCHITECT, API, BROWSER, DBA, AUDITOR, REPRODUCER, TRIAGE | on a pull request |
| `nightly` | those eight plus SOCKET, GUIDE, LOAD, REPORTER | the scheduled sweep |
| `fix-cycle` | VERIFIER, FIXER, REVIEWER | take a filed ticket and fix it |
| `full-loop` | all fifteen | discover → file → fix → verify → report |

Every mode is bounded by a **wall clock** and each agent by **`max_turns`** —
both model-agnostic, so they mean the same thing against a local model as against
a hosted one.

No spend cap ships by default: a dollar figure is one vendor's price list. Set
`max_budget_usd` on a mode or an agent if you want a ceiling, and the governor
enforces it before every dispatch.

---

## 📋 Command reference

### Setup and inspection — all free

```bash
qaas init <path-or-url> [--name N] [--api-url U] [--web-url U] [--force]
qaas targets                    # profiles available, and which is active
qaas doctor [--target N]        # what this target makes possible, per agent
qaas validate                   # config, prompts, skills, allowlists, budgets
```

`qaas doctor` tells you which agents can do useful work here and which cannot —
BROWSER with no reachable UI has nothing to do and says so rather than
substituting a weaker static read.

### Running

```bash
qaas run --mode <mode> [options]

  --dry-run              render every agent's options, call nothing
  --only AGENT           restrict to these agents (repeatable)
  --target NAME          override the active profile
  --repo URL             clone a repository and run against it
  --run-id ID            continue an existing run
  --ticket KEY           restrict a fix-cycle to these tickets (repeatable)
```

```bash
qaas run --mode pr-check --dry-run                    # free
qaas run --mode nightly                               # the usual sweep
qaas run --mode nightly --only API                # one agent
qaas run --repo https://github.com/you/app --dry-run  # clone and inspect
qaas run --mode fix-cycle --ticket QA-42              # fix one ticket
```

### Reading what happened — all free

```bash
qaas runs [--limit N]                    # every run, newest first
qaas show <run-id>                       # findings, cost, tickets, escalations
qaas trace <run-id> [--agent A] [--kind K] [--follow] [--quiet] [--json]
qaas dashboard [<run-id>]                # the same run, in a browser
qaas map [--version V]                   # the system map MAPPER built
qaas board [--no-create]                 # this target's Jira board
```

`qaas trace` is the one to reach for when you want to know *why* something
happened:

```bash
qaas trace <run-id>                              # the whole timeline
qaas trace <run-id> --agent FIXER               # one agent
qaas trace <run-id> --kind denial                # every refused tool call
qaas trace <run-id> --kind verdict --kind review # just the decisions
qaas trace <run-id> --quiet                      # decisions only, no file reads
qaas trace <run-id> --json > run.json            # export
```

#### Watching a run while it happens

`--follow` tails the ledger of a run in progress. Start it in a second terminal
the moment you kick a run off — or before, it waits for the ledger to appear.

```bash
qaas run --mode nightly &                        # terminal one
qaas trace $(ls -t .qaas/runs | head -1) --follow --quiet   # terminal two
```

Without `--quiet` you see every file an agent opens, one line each — which is
genuinely what "what is it doing right now" looks like, and is a lot. With it you
see only what the agent *decided*: findings, refusals, tickets, verdicts,
escalations. `--follow` stops on its own when the run finishes; ctrl-c is safe at
any point and stops only the view, never the run.

#### …or in a browser

`qaas trace --follow` is one scrolling column. Fifteen agents do not run in one
column: `qaas dashboard` shows them side by side.

```bash
qaas dashboard                       # the live run if there is one, else the newest
qaas dashboard <run-id>              # a particular run
qaas run --mode nightly --dashboard  # start both; the URL is printed first
```

It opens on `http://127.0.0.1:7777` and shows the phase rail advancing, one card
per agent with its cost, turn count and **the tool it is calling right now**, a
timeline lane per agent, findings by severity with their evidence, and every
refusal the guardrails issued as it happens.

| flag | |
|---|---|
| `--port N` | default 7777; the next few are tried if it is taken |
| `--host H` | default `127.0.0.1`. Leave it there — the ledger carries agent task previews, refused command lines and your target's paths |
| `--no-open` | do not open a browser |

It is **read-only**: it shows a run, it cannot start one, file anything or spend
anything. Ctrl-c stops the view and never the run, exactly like `--follow`.

Two things it shows that the CLI does not. A **skipped** agent says why it was
skipped, which is the answer to "why didn't BROWSER run". And an agent named by
an older roster is drawn as `not in this roster` rather than dropped, so runs
recorded before a rename still open.

### Measuring

```bash
qaas score [run-id] [--phase N] [--domain D]   # recall/precision vs a golden ledger
qaas sweep [--mode M] [--min-precision 0.70]   # run + score + fail below the gate
```

`qaas score` only works against a **calibration target** — one whose profile names
a `ledger:`. The bundled demo app has one; your application will not, and that is
normal.

`qaas sweep` is the cron entry point: it runs, scores, and **exits non-zero** below
the precision gate, so a scheduled sweep that starts producing noise fails loudly
instead of quietly filling a backlog nobody reads.

---

## 🔄 Workflows

### Nightly sweep on your own app

```bash
export APP_ADMIN_PASSWORD=...
qaas run --mode nightly
qaas show <run-id>
```

Start with `tracker: local`. Tickets are written as JSON under `.qaas/tickets/`,
so you can read what the system *would* file before it files anything into a
project real people watch.

### Filing to Jira

```bash
export JIRA_BASE_URL=https://you.atlassian.net
export JIRA_EMAIL=you@example.com
export JIRA_API_TOKEN=...              # an API token, not your password
export JIRA_PROJECT_KEY=QA
export JIRA_SECURITY_PROJECT_KEY=SEC   # security findings are REFUSED without this

QAAS_TRACKER=jira qaas tracker-check                     # preflight, creates nothing
QAAS_TRACKER=jira qaas tracker-check --dry-run-ticket    # + the exact JSON it would POST
QAAS_TRACKER=jira qaas run --mode nightly
```

Filed tickets carry `qaas-fp-<fingerprint>` and `qaas-envelope-<id>` labels. That
is how the next run recognises an already-filed defect and increments its
occurrence count instead of filing it again.

Rather than exporting four variables in every shell, put them in a `.env`:

```bash
mkdir -p .qaas && cat > .qaas/.env <<'EOF'
JIRA_BASE_URL=https://you.atlassian.net
JIRA_EMAIL=you@example.com
JIRA_API_TOKEN=...
JIRA_PROJECT_KEY=QA
EOF
chmod 600 .qaas/.env
```

`.qaas/` is already gitignored, which is why it is looked at before a `.env` at
your repository root. **Anything already exported wins over the file**, so a
stale `.env` cannot silently redirect a run. `QAAS_ENV_FILE=/path/to/file` names
a different one; `QAAS_ENV_FILE=` turns the whole mechanism off, which is what
CI should do.

### A view per repository

Every ticket the system files carries `repo-<target>`, stamped in code rather
than asked of an agent. At the top of a Jira-backed run, a saved filter over
exactly that label is found or created — so each repository you point it at gets
its own view without a Jira project of its own:

```console
$ QAAS_TRACKER=jira qaas run --repo https://github.com/acme/checkout.git --mode nightly
filter created — https://you.atlassian.net/issues/?filter=10001
every ticket from this run carries the label repo-checkout
```

```bash
qaas board                # find or create the view for the configured target
qaas board --no-create    # show the label and the JQL, touch nothing
qaas board -t other-repo  # a different target
```

**Whether you also get a board depends on your project type.**

| Your project | What you get | Why |
|---|---|---|
| Company-managed (classic) | A filter **and** a real board with To Do / In Progress / Done | Boards can be built over an arbitrary filter |
| Team-managed (next-gen) | The filter alone | The project owns its board and cannot have a second |

If you want real boards and your project is team-managed, create one
**company-managed** Jira project and point `JIRA_PROJECT_KEY` at it. Every
repository then gets its own board inside that one project — which is the
design. Changing `JIRA_PROJECT_KEY` also **repoints an existing filter**: the
filter is found by name and the name does not encode the project, so without
that repair it would keep asking about the old project and your tickets would
file correctly and be invisible.

Jira's API will create a board on a team-managed project and give it **no page in
the UI** — both `/jira/software/boards/<id>` and
`/jira/software/c/projects/<KEY>/boards/<id>` answer 404. So the project's style
is checked first and the attempt is skipped, rather than printing a link that
opens an error page. The filter is a good view either way: named, starred in your
sidebar, scoped to exactly one repository's defects.

A project per repository is not the design, deliberately: creating one needs
administrator rights a bot account rarely has, and it is unmanageable by the
tenth repo. Creating a saved filter needs no special grant.

A run is never failed over any of this — including "no Jira credentials at all".
Losing the findings would be the larger failure.

> [!TIP]
> Keep the committed backend `local` and switch per shell with `QAAS_TRACKER=jira`.
> Committing `tracker: jira` once broke 18 tests, because the test fixtures build
> a real Jira client and CI has no credentials.

### Fixing a defect

```bash
qaas run --mode fix-cycle --ticket QA-42
```

```
VERIFIER  NOT_FIXED  → the defect still reproduces
FIXER                → writes the minimal fix on a fix/* branch
REVIEWER  APPROVE    → adversarial review passed
VERIFIER  VERIFIED   → the original failing test now passes
```

REVIEWER may also return `REQUEST_CHANGES` (back to FIXER, capped at two round
trips) or `ESCALATE_TO_HUMAN` (stop — the fix is correct but shipping it is a
decision you should make).

> [!NOTE]
> **Merge is impossible by construction.** No merge method exists anywhere in the
> codebase, `gh pr merge` is refused, and pull requests open as drafts.

### Customising prompts

```bash
qaas prompts list              # which prompt is in force, and from where
qaas prompts eject API     # copy to .qaas/prompts/ and edit
qaas prompts diff              # what you changed vs. what shipped
```

Prefer **adding** to replacing — drop a `API.append.md` next to it and your
lines are inserted between the agent's prompt and the shared house rules. You keep
receiving improvements to the base prompt instead of forking it forever.

### Adding your own MCP server

In `.qaas/config/system.yaml`:

```yaml
mcp_servers:
  house-lint:
    type: stdio
    command: ./tools/lint-mcp
    args: ["--strict"]
    env: { LINT_TOKEN: "${ACME_LINT_TOKEN}" }
```

Then add `house-lint` to an agent's `mcp_servers:` list. Declaring it grants
nothing; an agent receives it only by naming it. `qaas validate` prints every
command a run would spawn.

> [!WARNING]
> A server's tools are allowed **wholesale** once an agent names it. qaas checks
> that the agent declared the server; it cannot inspect what a third-party
> server's tools do. **A server you declare is a server you trust.**

---

## 🛡️ What agents may and may not do

Enforced in code, not requested in a prompt:

| rail | effect |
|---|---|
| **Path scoping** | Writes checked against that agent's `write_paths`. Most agents cannot write at all. |
| **Branch scoping** | Git writes must match `qa/repro/*` or `fix/*`. `main` and force-push refused. |
| **Forbidden classes** | Migrations, auth, payment, secrets, infrastructure, CI — stop at a human. |
| **Ticket rate limit** | Over the per-run cap the call is denied and the run escalates. |
| **Immutable test** | The agent fixing a defect may not edit the test that defines it. |
| **No filesystem settings** | A repository qaas inspects cannot inject settings, hooks or MCP servers into the process running it. |

A denial returns a reason and is logged; it never kills the turn. See them with
`qaas trace <run-id> --kind denial`.

---

## 🔧 Troubleshooting

| symptom | cause and fix |
|---|---|
| `no target profile loaded` | Run `qaas init <path>`, or set `QAAS_TARGET`. With exactly one profile on disk it is used automatically. |
| `qaas score` says there is no golden ledger | Expected. Scoring needs a calibration target with a `ledger:`; ordinary applications do not have one. |
| `Role 'x' names environment variable Y ... and it is unset` | Export `Y`. Credentials come from the environment, never the profile. |
| BROWSER does nothing | It needs a reachable UI. Check `environment.mode` and `web_url`; `qaas doctor` will say so. |
| `spend cap reached` | Not an error — the governor working. Raise `max_budget_usd` for that mode or narrow with `--only`. |
| `mode 'x': agents can spend $N but the cap is $M` | The mode cannot finish. Raise its cap or drop an agent. |
| An agent names an MCP server that does not exist | `qaas validate` names it. Add it under `mcp_servers:` or fix the typo. |
| Running the bundled demo fails to log in | `export CORVID_PASSWORD=password123` — the demo's credentials come from the environment too. |

### Getting a straight answer about a run

```bash
qaas show <run-id>                      # the summary
qaas trace <run-id> --kind denial       # what was refused, and why
qaas trace <run-id> --agent REPRODUCER       # one agent's whole story
qaas trace <run-id> --json > run.json   # everything, machine-readable
```

Every tool call, denial, verdict and escalation is in
`.qaas/runs/<run-id>/ledger.jsonl`. `qaas trace` is a reader over it, not a
summary of it — nothing is hidden from you.

---

## 💸 Keeping runs bounded

- **`--dry-run` first, always.** It is free and shows exactly what would happen.
- **`--only AGENT`** while you are tuning. One agent is a fraction of a full run.
- **REPRODUCER runs once per finding**, so a finding-heavy run is longer than an
  agent-heavy one. That is the thing that most often surprises people — and it is
  the single biggest lever on what a run costs.
- **`thresholds.reproduce_min_severity`** (default `major`) is that lever.
  Findings below it are filed on the evidence discovery already produced instead
  of each opening a fresh context. Set it to `trivial` to reproduce everything.
- **If runs come back expensive and noisy, raise the gates before touching
  anything else:**

  ```yaml
  # .qaas/config/system.yaml
  thresholds:
    min_confidence_to_file: 0.75    # from 0.6 — drops low-confidence noise
    max_findings_per_agent_run: 10  # from 25 — caps the REPRODUCER fan-out
    reproduce_min_severity: major   # don't buy a context for a trivium
  ```

  Then run `qaas score` — a threshold change without a number attached is a
  guess.
- **`max_turns`** is the per-agent bound, and **`max_wall_clock_s`** the per-mode
  one. Both are enforced in code and neither assumes a provider.
- **`incident` mode files nothing** — useful for diagnosing without paperwork.
- Set **`max_budget_usd`** on a mode or an agent if you are running against a
  paid API and want a hard ceiling. Nothing ships with one.

---

## 📚 Where to go next

| document | for |
|---|---|
| [README](README.md) | what this is and why |
| [ARCHITECTURE.md](ARCHITECTURE.md) | the system in one document |
| [qa-agent-system-architecture.md](qa-agent-system-architecture.md) | the original design, including the 8 agents not yet built |

---

<div align="center">
<sub>MIT licensed · Issues and pull requests welcome</sub>
</div>
