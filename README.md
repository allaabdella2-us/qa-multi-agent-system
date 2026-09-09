# Multi-Agent QA & Remediation System

Agents that read your application, find real defects, reproduce them, and file
tickets an engineer is glad to receive. Built to the design in
[`qa-agent-system-architecture.md`](qa-agent-system-architecture.md).

Two loops meet at the ticket tracker. A **discovery loop** finds defects across
the API and UI surfaces and files them. A **remediation loop** picks them up,
fixes them, and proves the fix. Nothing crosses between the loops except through
a ticket, which is also the audit trail.

```
CARTOGRAPHER ──▶ CONDUIT ─┐
  (system map)   SURFACE ──┴──▶ FORGE ──▶ CLERK ──▶ [ticket] ──▶ PROOF
                  (discover)   (repro)   (file)                (verify)
```

---

## Running it on your own repository

### 1. Install

```bash
uv venv && uv pip install -e .
npx playwright install chromium                       # only if you want UI checks
npx @playwright/mcp@latest install-browser chrome-for-testing
```

Authentication comes from the Claude Code CLI if you are logged in
(`claude` in your PATH), or from `ANTHROPIC_API_KEY`.

### 2. Point it at your repository

```bash
qaas init https://github.com/you/your-app          # or a local path
qaas init ~/code/your-app --name your-app
```

This inspects the repository and writes `config/targets/your-app.yaml`. Every
value in it is a **guess you are expected to correct** — it reports what it
detected and what it could not find. Nothing runs and nothing is called yet.

### 3. Tell it how to reach your app

Edit the profile. The `environment.mode` field is the important decision:

| mode | what it means | what agents can do |
|---|---|---|
| `none` | no running instance | read code, schema and spec only |
| `external` | already running (staging, a dev server) | read and exercise it, never reset it |
| `compose` | this system owns the lifecycle | seed, reset, tear down |

`none` is a perfectly good place to start, and is where most first runs against a
real repository begin. Findings are honest about it: an agent that could not
observe a behaviour says so and lowers its confidence.

```yaml
environment:
  mode: external
  api_url: https://staging-api.your-app.com
  web_url: https://staging.your-app.com
  health_path: /healthz

auth:
  mode: login
  login_endpoint: POST /api/v1/login
  username_field: email
  token_path: access_token
  roles:
    admin:  { username: qa-admin@your-app.com,  password_env: QAAS_PASSWORD }
    viewer: { username: qa-viewer@your-app.com, password_env: QAAS_PASSWORD }
```

**Credentials never go in this file.** It names environment variables; you export
the secrets. Use dedicated QA accounts on a non-production environment.

### 4. Check and run

```bash
export QAAS_PASSWORD=...
qaas doctor --target your-app        # what is possible, and what is missing
qaas run --mode pr-check --target your-app --dry-run
qaas run --mode pr-check --target your-app
qaas show <run-id>                   # findings, cost, tickets, escalations
qaas trace <run-id>                  # the whole ledger as a timeline
```

`doctor` tells you which agents can do useful work against this target and which
cannot — SURFACE with no reachable UI has nothing to do, and says so rather than
substituting a weaker static read.

---

## Filing into real Jira and GitHub

Both are behind adapters, off by default. In `config/system.yaml`:

```yaml
tracker: jira      # local | jira
vcs: github        # local | github
```

**Jira Cloud** reads from the environment, and fails at startup rather than
halfway through a run:

```bash
export JIRA_BASE_URL=https://you.atlassian.net
export JIRA_EMAIL=you@example.com
export JIRA_API_TOKEN=...            # id.atlassian.com/manage-profile/security/api-tokens
export JIRA_PROJECT_KEY=ENG
export JIRA_SECURITY_PROJECT_KEY=SEC # without this, security findings are REFUSED, never filed publicly
```

**GitHub** uses the `gh` CLI, so it reuses your existing login — no token
handling. Run `gh auth login` once. There is no merge path anywhere in the
system, and force-push does not exist as a parameter: merge is always a human
decision.

Before the first live run, check the configuration without filing anything:

```bash
qaas tracker-check                    # auth, projects, permissions, workflow — read-only
qaas tracker-check --dry-run-ticket   # the exact JSON that would be POSTed
```

Then rehearse a whole run with `QAAS_TRACKER_DRY_RUN=1`, which makes every
tracker write log what it would have done and send nothing.

Start with `tracker: local`. It writes tickets as JSON under `.qaas/tickets/`, so
you can read what the system would have filed before it files anything into a
project real people watch.

**[docs/jira-setup.md](docs/jira-setup.md)** is the full walkthrough: minting the
token, the four project permissions, every environment variable, what the
tickets and their labels look like, how the fingerprint label dedupes across
runs, and a troubleshooting table keyed by the errors this adapter actually
produces. Jira Server / Data Center is not supported, and that page says why.

---

## What it costs

Measured against a small app, per run:

| agent | typical cost | what you get |
|---|---|---|
| CARTOGRAPHER | $0.50–0.65 | the system map everything else reads |
| CONDUIT | $2 | API, contract, authorization and error-handling defects |
| SURFACE | $3–4 | broken flows, console errors, accessibility, forms |
| FORGE | $1–2 **per finding** | a minimal reproduction and a failing test |
| CLERK | $0.70 | deduped, routed, written tickets |

A full nightly on a small app runs about $7–15. FORGE dominates on a
finding-heavy run because it is invoked once per finding — budget accordingly.
Every mode has a hard spend and wall-clock cap enforced in code, and the run
stops and escalates rather than overrunning.

---

## The controls that matter

Read §8 of the architecture document for the full picture. The short version:

- **The finder never fixes, and never certifies its own finding.** A discovery
  agent cannot mark its own defect reproduced — FORGE decides independently.
- **Write access is per agent, per resource**, enforced in a `PreToolUse` hook
  rather than requested in a prompt. Discovery agents cannot write at all; FORGE
  writes only under `qa/repro/` on `qa/repro/*` branches; only CLERK files.
- **Evidence or it did not happen.** A finding with no artifact and no failing
  test cannot be filed, and confidence below 0.6 goes to a human queue.
- **Security findings never reach a public project.** If no restricted project is
  configured, the filing is refused rather than downgraded.
- **Every run is capped** on spend, wall-clock, findings per agent and tickets
  per run. Hitting a cap escalates; it does not quietly continue.
- Every tool call, denial and escalation lands in `.qaas/runs/<id>/ledger.jsonl`.

---

## Calibration

`target-app/` is a deliberately buggy application whose defects are recorded in
`target-app/defects.yaml`. That makes discovery **measurable** rather than
impressive-looking:

```bash
cd target-app && docker compose up -d
qaas run --mode nightly && qaas score
```

`qaas score` reports recall, precision, false-positive rate, severity agreement
and cost per accepted finding. Use it after changing a prompt, a threshold or a
model — it is the only way to know whether a change helped.

You will not have a ledger for your own application, so `score` does not apply
there. The bar the architecture sets is **ticket acceptance rate above 70%**
before widening scope: file into a local tracker first and read what comes out.

---

## Development

```bash
pytest                     # ~460 tests, no API calls, no network
pytest -m docker           # needs the demo app running
qaas validate              # config, prompts and tool allowlists
qaas run --mode nightly --dry-run
```

Adding an agent is a prompt in `src/qaas/prompts/` plus a file in
`config/agents/`. No changes to the conductor, runner or guardrails.
