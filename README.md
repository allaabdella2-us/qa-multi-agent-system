<p align="center">
  <img src="docs/logo.jpg" alt="qaas" width="140">
</p>

<h1 align="center">qaas</h1>

<p align="center">
  <em>Sixteen governed Claude Code sessions that read your repository, find
  defects, reproduce them into failing tests, file them, fix them and verify
  the fix.</em>
</p>

<p align="center">
  <a href="https://pypi.org/project/qaas-python/"><img src="https://img.shields.io/pypi/v/qaas-python.svg" alt="PyPI"></a>
  <img src="https://img.shields.io/badge/python-3.12%2B-blue.svg" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="MIT">
</p>

---

## What it is

`qaas` is a harness **around Claude Code**, not a program that calls an API.

Each agent is a real Claude Code session: its own process, its own context, its
own tool allowlist, its own budget. A Python state machine runs them in order and
can refuse any tool call any of them makes. That is what makes the per-agent cost
number, the context boundary and the write-permission matrix real rather than
bookkeeping — they are separate operating-system processes with separate
permissions.

```
map    -> discover     -> synthesise  -> reproduce  -> file   -> verify   -> report
MAPPER    API/BROWSER/…  SYNTHESIZER     REPRODUCER    TRIAGE    VERIFIER    REPORTER
```

<p align="center">
  <img src="docs/roster.png" alt="The agent roster" width="820">
</p>

## Install

```bash
pip install qaas-python          # the CLI and the import package are both `qaas`
pip install "qaas-python[ui]"    # adds `qaas dashboard`
```

Requires Python 3.12+ and an `ANTHROPIC_API_KEY`. The Claude Code binary ships
with the SDK, so there is nothing else to install. Add
`npx playwright install chromium` only if you want the browser agents.

## Five minutes

```bash
cd your-project
qaas init .                      # writes .qaas/ and a target profile
qaas doctor                      # what this target makes possible
qaas run --mode pr-check --dry-run   # the whole plan, no API call, no money
qaas run --mode pr-check             # a real run
qaas show <run-id>
```

Or point it at anything without setting up first:

```bash
qaas run --repo https://github.com/someone/their-project --mode nightly
```

`--dry-run` renders every agent's model, turn cap, tool allowlist and prompt size
without spending anything. Run it before the first paid run, and after any change
to a prompt or a policy.

## What you actually get

A **run ledger** — every tool call, every refusal, every escalation, append-only,
on disk. `qaas trace <run-id> --follow` streams it; `qaas dashboard` renders it.

**Defect envelopes** rather than prose. Agents never hand each other paragraphs.
One validated type crosses every boundary, with two gates on the model itself:
a finding needs evidence, and it needs confidence above the filing threshold.
Findings that fail are held for a human, not filed.

**Failing tests**, not bug reports. REPRODUCER takes a finding, writes the
shortest test that makes it appear, runs it five times to measure flake, and
commits it on its own branch. A finding it cannot reproduce is demoted — that
filter is the point.

**Tickets** in Jira or in a local file-backed tracker, deduplicated across runs
by a structural fingerprint that ignores prose, line numbers and timestamps.

**Fixes**, on their own branches, as draft pull requests, reviewed adversarially
and verified against the original failing test before anything is closed. Nothing
merges. Merge is a human decision and there is no code path to it.

<p align="center">
  <img src="docs/dashboard.png" alt="The dashboard" width="820">
</p>

## Run modes

| mode | agents | wall clock | files tickets |
|---|---|---|---|
| `pr-check` | 8 | 15 min | yes |
| `nightly` | 13 | 2 h | yes |
| `incident` | 1 | 10 min | no — diagnostic only |
| `fix-cycle` | 3 | 1 h | yes |
| `full-loop` | 16 | 3 h | yes |

`qaas run --mode nightly --only API` restricts a run to named agents.
`qaas run --mode fix-cycle --from-board "Ready for Fix"` takes its work from the
board: drag a card into that column and the next run picks it up.

## The part that makes it safe to point at your code

Every agent's write permissions are declared in its config and enforced in
Python, before the tool runs — not requested in a prompt.

- **Discovery agents cannot write at all.** The finder never fixes.
- **FIXER writes to source and cannot touch the test that defines success**, nor
  migrations, auth, payment paths, secrets or CI config. Those stop at a human
  however small the change looks.
- **The shell is not a way around any of it.** A command that mutates and whose
  destination cannot be resolved is refused, naming `Write`/`Edit` in the reason.
  Wrappers are peeled, indirection is refused, deletion counts as a write.
- **Nothing reaches the network** except the application under test. `WebFetch`
  and `WebSearch` are refused to every agent: findings come from the code and the
  running app, not the web.
- **Your credentials stay yours.** The target's own test suite runs with an
  allowlisted environment, not a copy of yours.
- **Nothing is loaded from the target's filesystem settings.** A repository
  cloned from a URL cannot inject hooks or MCP servers into the process holding
  your API key.

Every refusal is logged with its reason. An agent that is refused adapts; the
turn does not die.

## Does it actually find things?

`qaas score` answers that with numbers rather than vibes.

The repository ships `target-app/`, a deliberately buggy FastAPI + React
application, and `target-app/defects.yaml`, a golden ledger of exactly what is
wrong with it — plus a `not_defects` section of correct-but-suspicious code, so
precision is *measured* rather than assumed. Matching is deterministic: using a
model to judge whether a finding matches a seeded defect would make the score
depend on the same class of system being measured.

```bash
cd target-app && docker compose up -d
qaas run --mode nightly
qaas score                       # recall, precision, false-positive rate, per agent
qaas sweep --min-precision 0.7   # run + score + exit non-zero below the gate
```

`qaas sweep` is the cron line. It fails loudly rather than quietly filling a
backlog nobody reads.

## Pointing it at your own project

A **target profile** is what makes this portable. Nothing in any prompt names a
specific application; the profile does.

```yaml
name: my-service
root: .
layout:
  backend: [api]
  frontend: [web]
  spec: openapi.yaml
environment:
  mode: compose          # none | external | compose
  api_url: http://localhost:8000
  web_url: http://localhost:5173
auth:
  mode: login
  login_endpoint: POST /v1/auth/login
  roles:
    admin: {username: admin@example.test, password_env: DEMO_ADMIN_PASSWORD}
```

`environment.mode` is the load-bearing field. `none` means static reads only.
`external` means agents may exercise the app but never reset it — someone else
may be relying on it, and a reset has no undo. `compose` means this run owns the
lifecycle and may seed, reset and tear down. The system refuses lifecycle
operations the profile has not granted.

Credentials are never written into a profile; it names environment variables.
`qaas doctor` tells you which agents a given profile can usefully run, and
`qaas init` will guess most of this from your repository and tell you what it
guessed.

## Customising it

An agent is a prompt file plus a YAML file. Adding one needs no Python.

```bash
qaas prompts list                # every prompt and which layer it came from
qaas prompts eject API           # copy it into .qaas/prompts/ to edit
qaas prompts diff                # what you changed against what ships
```

Config layers the ordinary way — an explicit `--config` beats your project, and
your project beats what shipped. Agent files, prompts and skills union by name,
so raising one agent's budget is one dropped-in file rather than a fork of the
whole roster.

The dashboard can edit *tuning* — which model an agent runs, a turn cap, a
threshold — and deliberately cannot edit *permissions*. Those live in a file a
person edits.

## Documentation

| | |
|---|---|
| [MANUAL.md](MANUAL.md) | every command and flag |
| [ARCHITECTURE.md](ARCHITECTURE.md) | how the code is put together |
| [CLAUDE.md](CLAUDE.md) | the design decisions and the bugs behind them |
| [docs/dashboard.md](docs/dashboard.md) | the live run view |
| [docs/jira-setup.md](docs/jira-setup.md) | Jira credentials and the per-repo board |
| [docs/launch.md](docs/launch.md) | running it for the first time |
| [CHANGELOG.md](CHANGELOG.md) | what changed |

## Contributing

```bash
git clone https://github.com/allaabdella2-us/qa-multi-agent-system
cd qa-multi-agent-system
uv venv && uv pip install -e ".[dev]"

pytest -q                              # 987 tests, offline, free, no API key
qaas validate                          # config, prompts and allowlists cohere
qaas run --mode pr-check --dry-run     # every agent's options assemble
```

Those three are the whole gate; CI runs exactly them. The default `pytest` run
makes no API calls and touches no network, and must stay that way — the tiers
that do are behind markers (`llm`, `docker`, `github`, `jira`) and deselected by
default.

If you change a seeded defect in `target-app/`, change its entry in
`defects.yaml` in the same commit. A stale golden ledger silently corrupts every
score.

## Status

0.0.2, beta. It works, it is used, and the interesting parts have tests. The
model is currently Claude; [PROVIDERS_PLAN.md](PROVIDERS_PLAN.md) is the staged
plan for making that a choice.

MIT licensed.
