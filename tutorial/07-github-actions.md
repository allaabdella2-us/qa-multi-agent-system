# 07 — How CI works

This chapter reads `.github/workflows/ci.yml` line by line, explains why the
default test run costs nothing, and records the bug this workflow shipped with on
its very first push.

There is exactly one workflow file:

```
$ ls -R .github
workflows

.github/workflows:
ci.yml
```

Fifty-two lines, two jobs, no secrets, no third-party actions beyond
`actions/checkout`, `astral-sh/setup-uv` and `actions/upload-artifact`.

---

## The whole file

```yaml
name: CI

on:
  push: { branches: [main] }
  pull_request:

jobs:
  test:
    # Everything here is free and offline. The `llm`, `docker`, `github` and
    # `jira` markers are deselected by addopts in pyproject.toml, so no API key
    # is needed and no network call is made. Keep it that way: a CI run that
    # costs money is a CI run people switch off.
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        python-version: ["3.12", "3.13"]
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with:
          python-version: ${{ matrix.python-version }}
      # No `uv venv`: setup-uv with `python-version` has already made one and
      # exported VIRTUAL_ENV, so creating a second fails with "a virtual
      # environment already exists". And the binaries are invoked from .venv
      # directly rather than through `uv run`, which would re-resolve against
      # uv.lock and can disagree with what was just installed.
      - run: uv pip install -e ".[dev]"
      - name: Tests
        run: .venv/bin/pytest -q
      - name: Config, prompts and allowlists are coherent
        run: .venv/bin/qaas validate
      - name: Every agent's options assemble without calling the API
        run: .venv/bin/qaas run --mode pr-check --dry-run

  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with: { python-version: "3.12" }
      - run: uv build
      - run: uvx twine check dist/*
      - name: The wheel must not carry the demo app
        # target-app/ is 69M of deliberately vulnerable code. It belongs in the
        # sdist for contributors, never in site-packages.
        run: |
          if unzip -l dist/*.whl | grep -q "target-app/"; then
            echo "::error::the wheel contains target-app/"; exit 1
          fi
      - uses: actions/upload-artifact@v4
        with: { name: dist, path: dist/ }
```

---

## Job 1: `test`

### Trigger and matrix

```yaml
on:
  push: { branches: [main] }
  pull_request:
```

Pushes to `main`, and every pull request. Note what is *not* here: pushes to
other branches. This system's agents open branches of their own — `qa/repro/*`
for a reproduction, `fix/*` for a MENDER fix — and those reach CI as pull
requests, which is where the signal is wanted, rather than twice.

```yaml
    strategy:
      fail-fast: false
      matrix:
        python-version: ["3.12", "3.13"]
```

`requires-python = ">=3.12"` sits at `pyproject.toml:10`, and the classifiers
at lines 17-18 claim 3.12 and 3.13. The matrix is that claim being tested rather
than asserted.

`fail-fast: false` matters more than it looks. The default would cancel the 3.13
job the moment 3.12 failed, and version-specific failures are exactly the ones
you want both halves of.

### The three steps, and what each proves

| Step | Command | What a failure means |
|---|---|---|
| Tests | `.venv/bin/pytest -q` | A behaviour changed |
| Config coherence | `.venv/bin/qaas validate` | The YAML, prompts and allowlists disagree with each other |
| Dry run | `.venv/bin/qaas run --mode pr-check --dry-run` | An agent's options cannot be assembled |

The second and third are not redundant with the first. `qaas validate` loads
every `config/agents/*.yaml`, resolves each agent's prompt and skills, and checks
the tool allowlists — it catches an agent that names a skill nobody shipped, or an
MCP server that does not exist. `qaas run --dry-run` goes one step further and
actually builds each agent's `ClaudeAgentOptions`, without calling the API:

```
$ .venv/bin/qaas run --mode pr-check --dry-run
target: sample (none)
pr-check — 5 agents, budget $16.00, concurrency 2
  CARTOGRAPHER   claude-sonnet-5    effort=medium  turns<=60  $2.00
    tools: Read, Grep, Glob, Agent, Skill, Task, TodoWrite, ToolSearch, mcp__envelope
    prompt: 4817 chars
  CONDUIT        claude-opus-5      effort=high    turns<=60  $3.00
    ...
```

Both exit 0 and neither contacts Anthropic. That is the point: the whole
pipeline's configuration is checked on every PR, for free.

---

## Why the CI run is free: `addopts`

The comment at the top of the `test` job states the rule:

```yaml
    # Everything here is free and offline. The `llm`, `docker`, `github` and
    # `jira` markers are deselected by addopts in pyproject.toml, so no API key
    # is needed and no network call is made. Keep it that way: a CI run that
    # costs money is a CI run people switch off.
```

The mechanism is four lines in `pyproject.toml:66-72`:

```toml
markers = [
    "llm: test makes real Claude API calls (costs money)",
    "docker: test needs a Docker daemon and the target app running",
    "github: test talks to a real GitHub repository over the network",
    "jira: test talks to a real Jira instance (needs JIRA_* credentials)",
]
addopts = "-m 'not llm and not docker and not github and not jira'"
```

`addopts` prepends that marker expression to *every* invocation of `pytest`.
There is nothing to remember and nothing to configure — a bare `pytest` is
already the free, offline run, in CI and on a laptop alike.

The four markers name four different kinds of expensive:

| Marker | Cost | Example |
|---|---|---|
| `llm` | Money — real Claude API calls | `tests/test_skills_actually_load.py` (whole module) |
| `docker` | A Docker daemon plus the target app running | `tests/target_app/test_seeded_defects.py`, `tests/mcp/test_env_control.py:272` |
| `github` | Network, plus a real repository | `tests/adapters/test_github_vcs.py:457` |
| `jira` | Credentials, plus a real Jira site | `tests/adapters/test_jira_tracker.py:715` |

The current split, measured:

```
$ .venv/bin/pytest -q
...
649 passed, 22 deselected in 15.54s

$ .venv/bin/pytest -q --collect-only -m "" | tail -1
671 tests collected in 0.45s
```

649 of 671 tests run in CI. The other 22 exist and are runnable — they are not
dead code — but they need something CI does not have:

```bash
pytest -m jira          # after exporting JIRA_* and configuring a scratch project
pytest -m docker        # after cd target-app && docker compose up -d
pytest -m 'llm or github or jira'
```

### The reasoning, spelled out

"A CI run that costs money is a CI run people switch off" is not a slogan. The
failure mode is concrete: a suite that needs `ANTHROPIC_API_KEY` cannot run on a
fork's pull request, so contributions from outside the repository get no signal;
a suite that bills per push gets a budget conversation and then gets disabled;
and a suite that intermittently fails on a live third-party service teaches
people to ignore red.

`CLAUDE.md` states the same rule as a house invariant: "the default `pytest` run
is offline and free, and must stay that way."

That rule has been broken once, and the code records it. From
`src/qaas/config.py:324-329`:

```python
    # `tracker: local` is the committed default and must stay that way. When
    # `jira` was committed instead, 18 tests failed and 14 errored: the agent
    # fixtures build a real JiraTracker, which demands credentials CI does not
    # have. The house rule is that the default `pytest` run is offline and free,
    # and a committed backend switch silently breaks it -- so the switch belongs
    # in the environment of the person who wants it, not in the repo.
```

Note the shape of that failure: nobody edited a test. Committing one word in a
YAML file was enough, because the fixtures build a real tracker and a real
`JiraTracker` validates credentials at construction (chapter 06). This is why the
backend override lives in `QAAS_TRACKER` and not in `system.yaml`.

Excluded tests are still designed to be *useful* rather than aspirational. The
Jira module says why its one live test is kept
(`tests/adapters/test_jira_tracker.py:11-12`):

```python
What a stub cannot tell us is whether Jira agrees with our payloads. That is
the marked test's job, and the reason it exists rather than being deleted.
```

---

## Job 2: `build`

Four steps, no matrix. It does not need one: the wheel is `py3-none-any`.

```yaml
      - run: uv build
      - run: uvx twine check dist/*
```

`uv build` produces both distributions; `twine check` validates the metadata
PyPI will render — mostly that the long description (the README) parses. Both
run locally in the same form:

```
$ uv build
Building source distribution...
Building wheel from source distribution...
Successfully built dist/qaas_python-0.1.0.tar.gz
Successfully built dist/qaas_python-0.1.0-py3-none-any.whl

$ uvx twine check dist/*
Checking dist/qaas_python-0.1.0-py3-none-any.whl: PASSED
Checking dist/qaas_python-0.1.0.tar.gz: PASSED
```

### The wheel-contents assertion

```yaml
      - name: The wheel must not carry the demo app
        # target-app/ is 69M of deliberately vulnerable code. It belongs in the
        # sdist for contributors, never in site-packages.
        run: |
          if unzip -l dist/*.whl | grep -q "target-app/"; then
            echo "::error::the wheel contains target-app/"; exit 1
          fi
```

This is the one assertion in the whole workflow that is not a test file, and it
earns its place because the thing it guards is a packaging config, not code.
`[tool.hatch.build.targets.sdist]` in `pyproject.toml:52-60` lists `target-app/`;
`[tool.hatch.build.targets.wheel]` at lines 46-50 does not. Nothing in the type
system or the test suite notices if someone "helpfully" unifies those two lists.

What it is guarding against is specific. `target-app/` is a deliberately
vulnerable FastAPI + React application — cross-tenant reads, missing role checks,
a traceback leak, mass assignment — seeded on purpose so that discovery can be
scored (see `target-app/defects.yaml`). Shipping it inside `site-packages` would
put that code on the disk of everyone who runs `pip install qaas-python`, for no
benefit, and it is 69M on disk:

```
$ du -sh target-app
 69M	target-app
```

You can run the assertion yourself:

```
$ if unzip -l dist/*.whl | grep -q "target-app/"; then echo "FOUND target-app"; else echo "no target-app in wheel"; fi
no target-app in wheel
```

Chapter 08 covers what *is* in the wheel and why the sdist keeps the demo app.

### The artifact upload

```yaml
      - uses: actions/upload-artifact@v4
        with: { name: dist, path: dist/ }
```

The built distributions are attached to the run. There is no publish step: this
workflow never uploads to PyPI. Releasing is a deliberate act, in the same spirit
as `CLAUDE.md`'s "Merge is a human decision (§8.4)".

---

## The bug this workflow had on its first run

The history is two commits:

```
$ git log --oneline -- .github/workflows/ci.yml
db22704 Fix CI: setup-uv already made the venv.
1f14188 Prepare the repo to be published: license, metadata, CI, and take out the debris.
```

`1f14188` added the workflow. `db22704`, later the same day, fixed it — because
it had never worked. The diff:

```diff
-      - run: uv venv && uv pip install -e ".[dev]"
+      # No `uv venv`: setup-uv with `python-version` has already made one and
+      # exported VIRTUAL_ENV, so creating a second fails with "a virtual
+      # environment already exists". And the binaries are invoked from .venv
+      # directly rather than through `uv run`, which would re-resolve against
+      # uv.lock and can disagree with what was just installed.
+      - run: uv pip install -e ".[dev]"
       - name: Tests
-        run: uv run pytest -q
+        run: .venv/bin/pytest -q
       - name: Config, prompts and allowlists are coherent
-        run: uv run qaas validate
+        run: .venv/bin/qaas validate
       - name: Every agent's options assemble without calling the API
-        run: uv run qaas run --mode pr-check --dry-run
+        run: .venv/bin/qaas run --mode pr-check --dry-run
```

### Failure 1: the second virtualenv

`uv venv && uv pip install -e ".[dev]"` came straight out of `CLAUDE.md`'s setup
instructions, where it is correct. In the workflow it was not, because
`astral-sh/setup-uv@v5` with a `python-version` input has *already* created
`.venv` and exported `VIRTUAL_ENV`. The following `uv venv` died on "a virtual
environment already exists at: .venv".

From the fix commit's message:

> `astral-sh/setup-uv` with `python-version` creates .venv and exports
> VIRTUAL_ENV, so the `uv venv` that followed died on "a virtual environment
> already exists at: .venv" -- both matrix jobs, before a single test ran. The
> build job passed throughout, which is why the wheel checks looked fine.

Both matrix jobs failed at the first step. Not one test ran. And the `build` job
— which does not create a venv at all — went green throughout, so the run was not
uniformly red.

The commit message does not stop at the mechanism:

> I wrote this workflow and pushed it without running it. That is the second time
> today: committing `tracker: jira` broke 18 tests the same way. A CI file is code
> and needs the same proof as code.

That is the same `tracker: jira` incident quoted above from `config.py`. Two
config-shaped changes, both shipped without being executed, both broken.

**The lesson, stated as a rule:** a workflow file is code. It compiles (YAML
parses), it looks right, and it does not work. Reading it again does not help,
because reading it is what produced it. The fix was verified by simulating the
runner locally in a throwaway venv instead:

> Verified by simulating the runner locally in a throwaway venv rather than by
> reading it again: 570 passed, `qaas validate` exit 0, `qaas run --dry-run` exit 0.

(570 was the count then; it is 649 now.)

### Failure 2: `uv run` against a stale lockfile

The second half of the fix is subtler, and it was a latent problem rather than an
observed crash. `uv run <cmd>` does not simply execute a binary — it re-resolves
the project's dependencies against `uv.lock` first, and syncs the environment to
match. That is normally the feature. Here it would have undone the `uv pip
install -e ".[dev]"` that ran one step earlier.

Worse, `uv.lock` is stale. The distribution was renamed to `qaas-python` in
`1f14188`, and the lockfile has not been regenerated since:

```
$ grep -n 'name = "qaas' uv.lock
629:name = "qaas"

$ git log --oneline -1 -- uv.lock
56d1dab Build the multi-agent QA system: Phase 1 discovery/triage plus the Phase 3 fix loop
```

`56d1dab` predates `1f14188`. So the lockfile describes a project called `qaas`
and `pyproject.toml` describes one called `qaas-python`. From the fix commit:

> Also switched from `uv run` to invoking .venv/bin directly. `uv run` re-resolves
> against uv.lock, which the distribution rename to `qaas-python` has made stale,
> so it could disagree with what `uv pip install -e` had just put in place.

Invoking `.venv/bin/pytest` and `.venv/bin/qaas` directly removes the question
entirely. What CI tests is exactly what `uv pip install -e` produced — which is
also what a contributor gets from the `CLAUDE.md` setup commands.

> **If you regenerate `uv.lock`**, that is a fine change to make, but do not take
> it as licence to switch back to `uv run`. The direct invocation is not a
> workaround for the stale lock; it is the narrower guarantee, and worth keeping.

---

## How to add a job

The workflow has no shared setup, no reusable workflow and no composite action,
which means a new job is a self-contained block. Copy the shape of `build`:

```yaml
  <job-name>:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with: { python-version: "3.12" }
      - run: uv pip install -e ".[dev]"      # only if you need the package
      - name: <what a failure means>
        run: .venv/bin/<binary> ...
```

Four rules, each of which the existing file follows:

**1. No `uv venv`.** `setup-uv` with `python-version` already made one. This is
the bug above; do not reintroduce it.

**2. Invoke from `.venv/bin/` directly, not through `uv run`.** See failure 2.

**3. It must be free and offline.** If your job needs an API key, a Docker
daemon, a live Jira or a real GitHub repository, it does not belong in the default
path — mark the tests with the corresponding marker and let `addopts` exclude
them. If you genuinely need a paid job, it belongs behind a manual trigger
(`workflow_dispatch`) or a schedule, never on `pull_request`, and it must not gate
merges from forks.

**4. Name the step after what a failure means, not after what it runs.** Compare
the existing names:

```yaml
      - name: Config, prompts and allowlists are coherent
      - name: Every agent's options assemble without calling the API
      - name: The wheel must not carry the demo app
```

Each reads as an assertion. When one goes red, the name in the GitHub UI is
already the diagnosis. `- name: Run validate` would tell a reader nothing they
could not see from the command.

### Before you push it

Run the steps locally, in order, in a clean checkout. That is the entire lesson
of `db22704`:

```bash
uv pip install -e ".[dev]"
.venv/bin/pytest -q
.venv/bin/qaas validate
.venv/bin/qaas run --mode pr-check --dry-run
```

For the build job:

```bash
uv build
uvx twine check dist/*
unzip -l dist/*.whl | grep -q "target-app/" && echo BAD || echo ok
```

---

## Summary

| | |
|---|---|
| Workflows | One: `.github/workflows/ci.yml`, 52 lines |
| Jobs | `test` (matrix 3.12/3.13) and `build` |
| Triggers | Push to `main`, and every pull request |
| Secrets used | None |
| Tests run in CI | 649 of 671; 22 deselected by `addopts` |
| Why deselected | `llm` costs money, `docker` needs a daemon, `github`/`jira` need live services |
| The packaging assertion | The wheel must not contain `target-app/` |
| Publishing | Not automated. `dist/` is uploaded as an artifact only. |
| The bug it shipped with | `uv venv` after `setup-uv` had already made one — both matrix jobs, zero tests run (`db22704`) |
| Why `.venv/bin/` and not `uv run` | `uv run` re-resolves against a `uv.lock` that still says `name = "qaas"` |
