# CLAUDE.md

Guidance for Claude Code working in this repository.

## What this is

`qaas` is a harness **around Claude Code**, not a program that calls an API.

Sixteen agents read a target application, find defects, join the ones that
belong together, reproduce them into failing tests, file tickets, fix them and
verify the fix. Each agent is a real Claude Code session — its own OS process,
its own context, its own tool allowlist and budget — and a Python state machine
decides the order they run in and may refuse any tool call any of them makes.

Design documents: `qa-agent-system-architecture.md` is the design this is built
to, and code comments cite it by section (`§8.1`, `§5.3`); read the section
before changing behaviour it describes. `BUILD_PLAN.md` is the milestone
checklist. `ARCHITECTURE.md` walks the code as it stands, `MANUAL.md` is the
user-facing command reference, `docs/dashboard.md` covers `qaas dashboard`, and
`PROVIDERS_PLAN.md` is the staged plan for making the model a choice. A change
that outdates this file usually outdates those too.

## Commands

```bash
uv venv && uv pip install -e ".[dev]"    # setup
npx playwright install chromium          # only for UI (BROWSER, GUIDE) runs

pytest                                   # 1034 tests, no API calls, no network
pytest tests/test_guardrails.py::test_name -x
pytest -m docker                         # needs target-app running
pytest -m 'llm or github or jira'        # tiers excluded by default in pyproject

qaas init <repo>                         # scaffold .qaas/ and a target profile
qaas validate                            # config + prompts + allowlists, no API call
qaas targets                             # profiles this project can see
qaas doctor --target corvid              # what a target makes possible
qaas prompts list / eject / diff         # prompt overrides in .qaas/prompts/
qaas tracker-check                       # Jira credentials, before spending anything
qaas run --mode pr-check --dry-run       # renders each agent's options, no API call
qaas run --mode nightly --only API       # real run, costs money
qaas run --repo <path-or-url>            # clone, profile and run against anything
qaas run --mode fix-cycle --from-board "Ready for Fix"   # the board picks the work
qaas runs / qaas show <run-id> / qaas map
qaas trace <run-id> [--follow] [--quiet] [--agent NAME] [--kind KIND] [--json]
qaas dashboard [<run-id>]                # the ledger and the config, in a browser
qaas board                               # find-or-create this target's Jira board
qaas score [<run-id>]                    # recall/precision against the golden ledger
qaas sweep                               # run + score + fail below the precision gate

cd target-app && docker compose up -d    # the demo app under test
```

**Before pushing, run what CI runs** (`.github/workflows/ci.yml`). There is no
linter or formatter configured, so these three are the whole gate:

```bash
pytest -q                                # offline, free, no API key
qaas validate                            # config, prompts and allowlists cohere
qaas run --mode pr-check --dry-run       # every agent's options assemble
```

CI adds two checks on the packaging job: the wheel must **not** contain
`target-app/`, and it must contain `prompts/`, `defaults/config/` and the skills
plugin. The second direction was missing and a packaging change could ship a
wheel that installs, validates, and has no agents.

Markers `llm`, `docker`, `github`, `jira` are deselected by `addopts`. The
default `pytest` run is offline and free, and must stay that way. The suite
clears `QAAS_TRACKER`, `QAAS_VCS`, `QAAS_CONFIG_DIR` and `QAAS_HOME` and blanks
`QAAS_ENV_FILE` at **import** time in `tests/conftest.py`, not in a fixture:
`tests/mcp/conftest.py` calls `load_config` during collection, so a
session-scoped autouse fixture runs too late for the module that needs it most.

`qaas dashboard` needs the `[ui]` extra and reads `.qaas/` **relative to the
working directory** — run it from the target project's checkout, not from here,
or you will be looking at the demo app's runs.

## Architecture

### Two facts that shape everything else

1. **ROUTER is Python, not a prompt.** `router.py` is an ordinary state machine:
   phase ordering, concurrency, the spend and wall-clock governor, the §8.3 loop
   breakers, escalation. A model cannot enforce a budget it is itself spending.

2. **Each agent is its own top-level `query()`**, not an SDK subagent — and that
   `query()` spawns **a Claude Code process**. That is what makes the context
   boundary, the tool allowlist and the per-agent cost number real rather than
   bookkeeping: they are separate operating-system processes.
   `agents=`/`AgentDefinition` is only for intra-agent fan-out. The binary
   normally ships with the wheel (`claude_agent_sdk/_bundled/claude`), which the
   SDK prefers over `PATH` — so `qaas validate` mirrors that order rather than
   calling `shutil.which` alone, which reported a problem to every user whose
   only copy was the bundled one.

### Phase pipeline (`router.py`)

```
map    -> discover     -> synthesise  -> reproduce  -> file   -> verify   -> report
MAPPER    API/BROWSER/…  SYNTHESIZER     REPRODUCER    TRIAGE    VERIFIER    REPORTER
```

The phases exist because the dependencies are real: discovery cannot start
without the map, triage cannot start without findings. Within a phase, agents
are independent and run concurrently up to the mode's cap.

**Discovery and reporting dispatch by LAYER; every other phase dispatches by
name.** So adding a discovery or reporting agent is a prompt file plus a YAML
file and no Python — `_phase_discover` falls back to `tasks.discovery` for
anything without a bespoke builder. API and BROWSER keep bespoke builders
because they name tools only they have. DBA and AUDITOR were added by prompt and
YAML alone, and found that it had not been true before them:
`_phase_discover` dispatched from a closed dict, so a new discovery agent
validated, assembled, appeared in `--dry-run`, and silently did nothing.
`test_every_shipped_agent_is_actually_dispatchable` is what holds that line now.

**`synthesise` is the join.** Agent independence is what makes each specialist
good and it has a cost: a defect whose proof spans two surfaces arrives as two
findings, each correctly judged minor by an agent that could only see its half.
Nothing joined them — `fingerprint()` leads with `domain`, so the two halves are
*guaranteed* to hash differently, and the prompts said "leave the other surfaces
to the agents that own them". SYNTHESIZER reads every envelope and asks the one
question no discovery agent can.

It sits **before** reproduce, and that placement is the design: a composite is
an ordinary envelope, so it earns a REPRODUCER context, a failing test and a
ticket like any other finding. REPORTER already sees everything and can already
emit, and is useless for this precisely because it runs *after* file and verify —
anything it emits is written, logged, scored, and never filed. That orphaning is
now logged rather than silent. SYNTHESIZER skips below two findings and says so,
and has no `must_call` deliberately: most runs contain no conjunction, and an
agent required to emit will assemble one wearing a severity higher than either
of its parts.

**REPRODUCER runs once per finding**, in a fresh context, so cost scales with
findings rather than with agents — and serially, at `concurrency=1`. Two
invocations are separate contexts but not separate sandboxes: both hold `vcs`
and `env_control` against the same `target_root`, so they branch, commit and
reset the same working tree and the same compose stack.

Because that cost scales with findings, `_phase_reproduce` gates on
`thresholds.reproduce_min_severity` (default `major`). A nightly run that found
85 mostly-minor issues opened 85 frontier-model contexts and spent $45 filing
nothing. A finding below the floor is still **filed** — `is_fileable` wants
evidence, confidence and "not `not_reproducible`", and `unattempted` passes all
three — it just does not earn a committed failing test. The gate cannot move
`qaas score`, because the scorecard reads envelopes and REPRODUCER emits none;
that is what makes it a pure cost lever rather than a calibration change.

### The verify loop

`_verify_loop` dispatches VERIFIER; on `NOT_FIXED` it calls `_remediate`
(FIXER -> REVIEWER, bounded by `max_mender_arbiter_round_trips`) and re-runs
VERIFIER, bounded by `max_proof_reopens`. Every exit either records a verdict or
escalates. A roster with no FIXER escalates `NOT_FIXED` to a human immediately,
which is right: the alternative is re-running VERIFIER against unchanged code.

Four facts here are bug-derived.

**VERIFIER must re-verify the branch FIXER wrote.** The envelope names the
*repro* branch, which by construction carries a failing test and no fix, so
sending VERIFIER back there made VERIFIED unreachable. Where FIXER put the fix is
only knowable after the fact, so `_branch_written_since` reads it out of the
ledger, scoped to entries since a mark — a run verifies several tickets against
one ledger and an earlier ticket's `fix/*` branch is the wrong answer.

**`_entry_since` scopes the verdict and review lookups the same way.** Unscoped,
they took the last entry for the ticket from anywhere in the ledger, so a
VERIFIER that finished without calling `record_verdict` silently inherited the
previous verdict. That is not only a crash path — the Stop hook deliberately
lets an agent through after one block, so a silent VERIFIER is ordinary. On a
resumed run it means a VERIFIED nobody verified.

**A loop that carries nothing forward is a retry.** `record_review` refuses
REQUEST_CHANGES without `concerns`, on the stated grounds that FIXER gets them
verbatim — and nothing carried them, so round two dispatched FIXER with a
byte-identical prompt and `max_mender_arbiter_round_trips: 2` bought a second
attempt at the same coin flip. `_review_feedback` assembles the text in Python
from typed ledger data and `tasks.fixer(..., feedback=...)` renders it: no new
tool, no new `LedgerKind`, nothing a model has to be trusted to pass on.

**FIXER's `write_paths` include `qa/repro`** — REPRODUCER's sandbox, where the
failing test lives. §10's symptom-fix guard therefore depended entirely on
`policy.protected_paths`, which no agent YAML sets and nothing else populated,
so `Guardrail._protected_path` always returned `False`.
`_with_protected_test` deep-copies the spec per invocation and names that
ticket's test: per-invocation because which test is protected depends on which
ticket is being fixed, deep-copied because the roster is shared across the run.

### Budget and time

`Budget.check()` runs before every dispatch and raises `BudgetExceeded`, which
is a control working rather than an error. Three properties are worth knowing:

- **Spend carries across a resume.** `already_spent` is read back from the run's
  own results, so `qaas run --run-id <existing>` cannot reset the cap. The wall
  clock deliberately does not carry: it measures this process, and a run resumed
  the next morning has not been running all night.
- **`allowance(spec, slots=N)` divides the remainder** across the dispatches
  actually in flight. `spent` only moves after an agent returns, so `_gather`
  used to start `max_concurrency` agents each told it could spend the entire
  remaining budget.
- **`max_wall_clock_s` can preempt.** `_dispatch` wraps `run_agent` in
  `asyncio.wait_for` bounded by the run's remaining clock. Without it the check
  ran only *between* dispatches, so one wedged Playwright session outlived the
  cap indefinitely and `pr-check` advertised a 900-second bound it could not
  keep.

`RunMode.reserve_fraction` (default 0.15) holds back enough clock that a run
which stops early still files what it found. `BudgetExceeded` used to unwind past
file, verify and report, leaving envelopes on disk, no ticket, no report, and
whoever scheduled it looking at a run that cost money and produced nothing
actionable. The finding phases check with `reserve=True`; filing and reporting
run against the full cap.

`run()` also catches non-`BudgetExceeded` exceptions and still writes
`run_finished`. Without that, a ledger held an opening line with no closing one,
which every reader treats as "still running", forever.

**A provider quota is the third wall, and it is not `BudgetExceeded`.**
`is_quota_error` classifies an agent's error text (one phrase list, shared with
`cli._quota_preflight`); `_dispatch` raises `QuotaExhausted` instead of
escalating; `_gather` stops starting queued work; and `run()` skips file, verify
and report rather than dispatching TRIAGE into the limit that just killed
discovery — the reserve holds back *clock*, which buys nothing when the wall is
the provider's. It ends with **one** `quota_exhausted` ledger line (a new
`LedgerKind`, because "a human must look at this finding" and "run it again at
4:20pm" are opposite instructions) carrying the unfiled-finding count and the
exact resume command, which is also `report.resume_command` and the
`stopped_early` sentence. Resume is the existing mechanism and deliberately not
a new one: `_succeeded_agents` skips what finished, `_phase_file` skips
envelopes that already carry a ticket. From `run-20260919T152757-4c8c37` — 8
agents, 2h45m, $56.58, 34 findings, then TRIAGE and ten REPRODUCERs dying
seconds apart on the same session limit: eleven identical escalations, zero
tickets, and the findings filed by hand hours later.

A bare `429` is deliberately *not* one of the markers: `_dispatch`'s own timeout
error reads `exceeded the run's remaining wall clock (429s)`.

### The DefectEnvelope is the only inter-agent type

`envelope.py`. Agents never pass prose to each other. Validated on write and on
read, `extra="forbid"` everywhere. Two gates live on the model rather than in a
prompt: `has_evidence()` (an artifact or a failing test) and `is_fileable()`
(evidence + confidence ≥ threshold + not `not_reproducible`).

`fingerprint()` deliberately excludes prose, line numbers, commit sha and
timestamps, so the same defect found twice hashes the same. It folds
`service`/`endpoint`/`ui_route` the way `defect_memory.location_score` already
folded them — the two halves of the dedupe system disagreed, so `GET /v1/orders`
and `get /v1/orders/` scored as one location and fingerprinted as two defects.
When an envelope carries no location at all it falls back to the normalised
title: prose is what this function otherwise excludes on purpose, and a weaker
identity beats a wrong one. Without that fallback, two unrelated findings
sharing only a domain and a class hashed identically, and the second was
suppressed as "already tracked as PROJ-N" — in the cross-run store, permanently.

`emit_envelope` strips server-owned fields (`id`, `run_id`, `discovered_by`,
`dedupe`, `jira`) and `EMIT_SCHEMA` is `additionalProperties: false`. Every
argument used to be forwarded into `model_validate`, so an agent could supply
`id` and overwrite a peer's envelope on disk. It also **resolves** every
`artifact://` uri it is handed: `has_evidence()` was satisfied by a well-formed
string naming a file that had never been written.

### Agents are data, not code

An agent is a prompt in `src/qaas/prompts/<AGENT>.md` plus a YAML file in
`src/qaas/defaults/config/agents/<agent>.yaml`. Sixteen ship. A file of the same
name under `<project>/config/agents/` or `.qaas/config/agents/` *shadows* the
packaged one; this checkout's `config/` holds only `targets/`, so adding to the
roster means editing the packaged directory and not an override.

Adding one should require **no change** to router, runner, registry or
guardrails — treat a change to those files while adding an agent as a sign
something is wrong. The exception is a new *phase*, which is a router change by
definition: `synthesis` is the second one the roster has ever needed.

Constraints enforced in `config.py`: at most 6 MCP servers per agent (§5.3, tool
selection accuracy falls off past 5–7), and every `must_call` tool must name a
server the agent actually has.

`prompts/_shared.md` is appended to every agent prompt — house rules go there,
not copy-pasted into six prompts. Prompts resolve through `Workspace.prompt_dirs`
(`.qaas/prompts/` beats the packaged copy), **file by file and independently**,
so overriding `API.md` keeps the house `_shared.md`. A `<AGENT>.append.md` is
inserted between the agent block and the shared block — never after it, because
the house rules must stay the last word. `qaas prompts list/eject/diff`.

Role and standards live in the system prompt; *procedure* lives in
`src/qaas/plugin/skills/<skill>/SKILL.md` (30 of them); the per-run *task* —
which app, which environment, which finding — is built in `tasks.py`.

Skills reach an agent as a Claude Code **plugin** (`--plugin-dir`), not through
filesystem settings, so they travel inside the wheel instead of depending on a
`.claude/skills/` in whatever repository the user happens to be standing in. The
layout is not optional: a directory of bare `<skill>/SKILL.md` folders loads
**nothing, silently**; only `.claude-plugin/plugin.json` + `skills/<name>/SKILL.md`
gives a stable namespace, taken from the manifest's `name`. `qualified_skills`
therefore rewrites an agent's `skills:` entries to `qaas:<skill>` — the SDK
matches skill names down two channels with different rules, and the unqualified
name loads on one but never matches the allow rule on the other. A skill no
plugin provides is dropped, not passed through.

**Nothing in `tasks.py` or a prompt may name a specific application.** The
system is pointed at a target profile; a prompt mentioning one repo's layout or
one app's seeded users works exactly once.

### Guardrails: enforcement is in the PreToolUse hook

`guardrails.py` implements the §8.1 write-permission matrix. The critical fact:
an `allowed_tools` entry naming a whole tool **auto-approves it before
`can_use_tool` is consulted**, so a policy implemented only in that callback is
silently dead code (this repo had exactly that bug — REPRODUCER's sandbox check).
Primary enforcement is the `PreToolUse` hook; `can_use_tool` is a second layer
for calls the allowlist did not auto-approve. Both call one `check()` so they
cannot disagree.

**One `check()` was not enough, because there are more than two doors.** The
matrix was enforced for `Write`/`Edit` and unenforced for `Bash` and
`mcp__vcs__*`: `sed -i` reached what `Write` could not, and `check()`
short-circuits every `mcp__*` call to "is the server declared" without reading
its arguments, so `mcp/vcs.py`'s own near-copy of the rules was the only gate
there — and it had never learned about `forbidden_paths`. `Guardrail._check_path`
is now the single implementation and all three doors call it. When adding a
path-taking surface, call it; do not restate it.

`_check_path` takes `count_against_budget`. `_check_diff_budget` *mutates*
`touched_files`, and `mcp/vcs.py:commit` validates every staging pathspec through
the same function — so committing `api/app` and `web/src`, which are FIXER's own
`write_paths`, charged two entries to a budget of five before a line had changed.

Reading a shell command for what it writes is best-effort, so the rule that makes
it sound is: **a command that mutates and whose destination cannot be resolved is
refused**, naming `Write`/`Edit` in the reason. Guessing is the one option that is
not available.

That rule was stated and not implemented. `_writes_of` keyed its mutation test on
`argv[0]` and fell through to "writes nothing" for everything it did not
recognise, which made every mutator reachable by putting one word in front of it.
Reproduced against the shipped roster: read-only VERIFIER could run `env sed -i`,
`timeout 5 sed -i`, `xargs sed -i`, `find -exec sed -i`, `curl | sh`,
`echo $(rm f)` and plain `rm f` against paths FIXER itself is forbidden. Four
shapes now:

- **One quote-aware pass.** `_tokenise` is `shlex` with `punctuation_chars`. The
  old regex split could not see quoting, so it cut `echo 'hello; world' && ls`
  into nonsense and cut `echo x >| f` at the `|` of `>|`, leaving a dangling `>`
  whose target was never checked.
- **Wrappers are peeled** (`env`, `timeout`, `nice`, `nohup`, …) before `argv[0]`
  is read, and **indirection is refused** (`xargs`, `eval`, `find -exec`,
  command substitution, a pipe into a shell) — there is no destination to name.
- **Deletion and revert are writes**: `rm`, `git rm`, `git checkout -- P`,
  `git restore P`, and `mv`'s *source*. Removing a file changes it more
  completely than editing it does.
- **Patterns are case-folded on both sides.** Every shipped pattern is lowercase,
  `fnmatch` on POSIX is case-sensitive, and macOS is not — so `api/app/Auth.py`
  named the file `*auth*` exists to protect and matched nothing.

A glob `write_path` is contained before it is matched. The directory branch
proves containment with `is_relative_to`; the glob branch only matched a string,
and `relative` falls back to the absolute path when the target is outside the
root — so `fnmatch("/etc/x_test.py", "*_test.py")` is True, because fnmatch's `*`
crosses `/`.

One residual limit, stated because it is a decision: `python foo.py` and
`python -m pytest` are arbitrary code and are **allowed**. Refusing them was
tried and it refuses how FIXER and VERIFIER run the suite; a guardrail that
blocks the system's own happy path is one that gets switched off.

Anything an agent supplies that becomes argv is a flag until proven otherwise.
`_reject_flaglike` refuses a leading `-`, and `_reject_refspec` also refuses `:`
and a leading `+`, because `git push origin <name>` parses its argument as a
*refspec* — `qa/repro/x:main` published onto main past every branch-pattern and
protected-name check. The shell door applies the same rule now; it had not.
Test-runner selectors reach pytest with no `--` separator, so one starting with
`-` is an option (`-p`, `-c`, `-o addopts=`), and a path-shaped one is contained
against `target_root` the way `cwd` already was.

`ALWAYS_GRANTED` (ToolSearch, Skill, TodoWrite, Task, Agent) is read by
`build_allowed_tools`, and `HARNESS_TOOLS` is *derived* from it rather than
restated — the comment claimed the two could not drift and they had. Denying
`ToolSearch` breaks MCP access entirely, since MCP tools arrive deferred.

Denials return a reason and are logged to the ledger; they never kill the turn.

### Hooks enforce the output contract

`registry.build_hooks`. The `Stop` hook blocks an agent that has not called its
`must_call` tools while it still has a turn to fix it — the router would only
find out afterwards. It honours `stop_hook_active`, because blocking twice burns
the budget.

`must_call` is satisfied only by a call that was permitted and did not error. An
errored `record_verdict` must not let VERIFIER stop with no verdict. That is
right in general and was wrong in one place: FIXER's
`must_call: [mcp__vcs__open_pr]` against the committed `vcs: local` is a contract
no behaviour can satisfy, because `LocalGit` has no remote and the call returns
`err()`. `satisfiable_contract` drops a `must_call` entry the configured backend
cannot provide, and logs the drop.

The `PostToolUse` hook tells an agent immediately when an emitted envelope was
**held** rather than filed, since discovering that at the end is too late to
attach evidence. It reads the tool *text* as well as `structuredContent`:
`ok()` attaches the flag under that key and the SDK does not reliably forward
it, so the nudge silently never fired.

### MCP servers are in-process

`src/qaas/mcp/` — seven servers built with `create_sdk_mcp_server()`:

| server | tools |
|---|---|
| `envelope` | emit_envelope, record_reproduction, record_verdict, record_review, list_envelopes, get_system_map, put_system_map, put_artifact |
| `defect_memory` | search_similar, fingerprint, record, get_occurrences, mark_resolved |
| `tracker` | create_issue, transition, link, search |
| `test_runner` | run_suite, run_single, run_n_times, affected_tests, get_coverage |
| `env_control` | spin_up, seed, reset, set_flag, get_flags, set_clock, http_request, impersonate, status, tear_down |
| `contract_diff` | diff_openapi, classify_breaking, find_consumers, generate_contract_test |
| `vcs` | current_branch, create_branch, write_file, commit, diff, list_branches, push, open_pr, pr_diff, list_changed_files |

They close over a `ToolContext` (run store, config, agent spec, pinned map
version), so validation, guardrails and persistence happen where the state
already is. Servers enforce their own domain rules as a third belt: the tracker
refuses an agent that may not file, `vcs` refuses a branch outside the agent's
patterns, `defect_memory.record` and `mark_resolved` are gated by policy.
Playwright is the one stdio subprocess, declared in `registry.STDIO_SERVERS`.

Tool results use `ok()`/`err()` from `mcp/context.py`: errors are **returned, not
raised**, so the agent reads the reason and corrects itself. `err()` sets **both**
`isError` and `is_error`, and that is the bug rather than belt-and-braces. The
MCP wire format spells it `isError`; the SDK reads the handler's dict with
`result.get("is_error", False)` and drops anything else. Every refusal from all
seven servers — a guardrail denial, "you may not file", "not reproducible" —
was delivered to the model as a *successful* tool result, and the whole
self-correction contract had never once run. `tests/mcp/conftest.py:is_error`
reads `is_error` for the same reason: a test double that reads a different field
from the real consumer tests the double, which is how 200 tests agreed with the
bug.

`env_control` gates its lifecycle tools on `environment.mode`. `spin_up`, `seed`,
`reset` and `tear_down` pass `needs_lifecycle=True` to `preflight`, which refuses
unless the profile says `compose`. Checking only "does a compose file exist and
is docker on PATH" meant a compose file left lying in a repository was enough to
destroy a shared staging environment declared `external`.

`env_control.http_request` exists because API, AUDITOR and LOAD are all told that
observation is the evidence bar — "a finding you have not observed is a
hypothesis, not a defect" — and none of them held a tool that could issue an HTTP
request. `WebFetch` is refused to every agent on the correct grounds that
findings come from the code and the running app rather than the web; nothing let
them reach the running app either. It takes a **path**, never a host, and
resolves it against the one origin the run is pointed at.

`test_runner` builds its child environment from an **allowlist**. It was
`dict(os.environ, ...)` minus two pytest keys, so the target repository's own
test suite — someone else's code, cloned from a pasted URL under
`qaas run --repo` — ran with this user's `ANTHROPIC_API_KEY`, `JIRA_API_TOKEN`
and `GITHUB_TOKEN` in scope. A `conftest.py` reading `os.environ` is the whole
exploit, and running the target's tests is this server's purpose.

A project can declare more servers in `system.yaml` under `mcp_servers:`.
Declaring one grants nothing — an agent receives it only by naming it in its own
`mcp_servers:` list — and `SystemConfig._agents_name_real_servers` checks every
such name at load time, so a typo fails `qaas validate` instead of surfacing as
`UnknownServer` part-way through a paid run. There is deliberately no in-process
Python server type: that would mean `importlib.import_module` on a name from a
config file, executing arbitrary module-level code inside the process holding
this user's credentials.

### Where a real parse buys something

`importgraph.py` is the only place in the system that parses rather than guesses,
and it exists for one caller. `affected_tests` decides what VERIFIER runs against
a diff, and it decided by comparing *filenames* — so a test reaching the changed
module through a caller scored zero, which is step 3 of
`regression-suite-selection` ("a fix inside a shared helper breaks its consumers,
not itself"). The step the procedure calls essential was delegated to a ranking
that structurally could not see it. A reverse-reachable closure over the import
graph sees it in two hops, and the result reports the distance.

Three properties, all load-bearing:

- **Parse-only.** `ast.parse` on text that is never imported and never executed.
  The target is someone else's repository, and running its module-level code
  inside this process is not a thing to do for a test ranking.
- **Python-only, and it says so.** A Go or TypeScript target yields an empty
  graph, `affected_tests` falls back to the filename heuristic, and the answer
  names which method produced it. "No tests are affected" and "I cannot read this
  language" are different answers.
- **Never raises.** A syntax error, an unreadable file, a symlink loop — all
  skipped and counted. A ranking that crashes the verify phase is worse than one
  that is merely incomplete.

Hidden directories are skipped wholesale. Found by running it against qaas
itself: `.claude/worktrees/` held two stale checkouts of the whole repository, so
every changed file reported three copies of every test.

### One Jira view per repository, not one project

Every ticket carries `repo-<target>`, stamped by `mcp/tracker.py` rather than
asked of TRIAGE. `JiraTracker.ensure_repo_board` finds-or-creates a saved filter
over exactly that label, plus a board over the filter **only where one can
render**; `cli._ensure_board` calls it at the top of every Jira-backed run. A
*project* per repo would need admin rights a bot account rarely has; a filter
needs none.

**A 200 from the board API is not a working board.** Team-managed (next-gen)
projects own their board; `POST /rest/agile/1.0/board` over a filter still
returns 201, and the UI has no page for the result. `is_team_managed` is the
authoritative gate and skips the attempt there. `_board_is_reachable` (no
`location` -> unusable) is a *second, weaker* check applied **only to a board that
already existed**: Jira populates `location` asynchronously, so a board read back
immediately after creation reports None whatever its project.

`board_url` never assembles a URL: it follows
`/secure/RapidBoard.jspa?rapidView=<id>` and takes whatever Jira resolves it to,
because a company-managed board's path carries a `/c/` segment a hand-built URL
missed. It now **rejects a resolution that left the site** — on an SSO-enforced
instance that redirect lands on the identity provider's login page, so the "board
URL" written into tickets was a link to Okta.

**A reused filter is only the right filter if its JQL still matches.** Filters are
found by name and the name does not encode the project, so repointing
`JIRA_PROJECT_KEY` left the filter scoped to the old project — tickets filed
correctly and were invisible on the view. `ensure_repo_board` repairs the JQL.

None of this can fail a run: the filter is the fallback and the findings matter
more than the view.

**`--from-board` is the one place the board drives the system.** It resolves the
tickets carrying this repo's label that sit in a given status, finds the run
holding their envelopes, and feeds them in as `--ticket` would. It is
deliberately a **pull, not a subscription**: the board chooses the *work*, and
ROUTER still schedules everything after that out of the ledger. Agents polling a
tracker for the length of a run would put a rate-limited dependency on the
control path and buy nothing, because the budget governor and the loop breakers
have to live in code regardless. Two details are bug-derived: the status is
matched **case-blind in the CLI, not by the adapter** (both backends match
exactly, and a column's name is whatever casing someone typed), and resolution
happens **before** the `--dry-run` return, because "what would this pick up" is
the question `--dry-run` exists to answer.

`JiraTracker.search` pages with `nextPageToken`. Jira caps a page at 100 and the
clamp used to stop there, silently halving the 200-row window `--from-board`
asks for — so on a board with 120 cards the one that was dragged could fall off
the end and the run would report finding nothing.

### Targets make it portable

`target.py` / `config/targets/*.yaml`. `environment.mode` is the load-bearing
field: `none` (static reads only), `external` (exercise but never reset), or
`compose` (own the lifecycle). `profile.capabilities()` drives which agents can
usefully run, and `agent_usable` lives beside it because two callers must agree —
`qaas doctor` reports it and the router acts on it. They did not agree for a
while: doctor would say "agents that cannot: BROWSER" and a run would dispatch
BROWSER anyway and spend its whole budget looking for a browser that was never
there. Credentials are never in a profile; it names environment variables.

**The qaas project and the target are two different roots.** `paths.Workspace`
owns "where qaas lives"; `SystemConfig.target_root()` → `profile.root_path()`
owns "the application under test". `ToolContext.target_root` carries the second,
and it is what the write-path allowlist, the test runner's cwd, the vcs sandbox
and the SDK subprocess `cwd` are all anchored on. They coincide only for the
bundled demo. A policy's `write_paths`, `protected_paths` and `forbidden_paths`
are **target-relative** — a pattern written `*/x/*` will not match a repository's
own root-level `x/`.

**A named target that does not exist is fatal.** The guard used to read
`if chosen and profiles:`, so "named but absent stays fatal" held only when some
profile existed *somewhere* — and a fresh `pip install` has none, because the
packaged config ships no `targets/`. So `--target`, `QAAS_TARGET` or
`system.yaml` naming a profile that was not there fell back to the working
directory, pointing every write-path sandbox, the test runner's cwd and the SDK
subprocess at whatever directory the operator happened to be standing in, and
said nothing.

`qaas run --repo <path-or-url>` clones into `.qaas/targets/<slug>` and provisions
a profile through the same helper as `qaas init` (`_provision_target`). Keep it
one code path — two ways to decide what is under test is two sets of rules about
where someone else's code lands on disk. A credential embedded in the URL is
redacted before it is printed or stored on the profile; it reaches `git clone`
and nothing else. Reusing a same-named profile compares what it points at first,
because the name is the repository's *basename* and two checkouts called `api`
collide.

### The dashboard reads; it never participates

`src/qaas/ui/` is `qaas dashboard` — a localhost page over a run's ledger. There
is no path from the page to a dispatch, a ticket, a branch or the target's files,
and `test_only_the_override_route_writes` holds the line: every route is a GET
except **one**, named in `WRITE_ROUTES`, and adding a second is an architectural
change rather than a feature.

That one route writes `overrides.yaml`, and the shape of the exception is the
point: it changes what a model *is* — which model an agent runs, a turn cap, a
threshold — and never what an agent is *allowed to do*. `config.TUNABLE_AGENT_FIELDS`
is the whole vocabulary; `policy`, `mcp_servers`, `builtin_tools`, `skills` and
`must_call` are absent deliberately, because a page reachable by anything running
as this user must not be a second, quieter door onto the §8.1 matrix. A refused
field is named in the error rather than dropped, the candidate is validated
through a real `load_config` in a scratch copy before anything lands, and
deleting the file undoes all of it. It writes to the layer
`config._apply_overrides` will actually *read* — that loader takes the nearest
layer already containing an `overrides.yaml`, and writing unconditionally to
`config_dirs[0]` silently discarded every override in force.

**Binding 127.0.0.1 is not the same as being private.** It stops a network peer
and does nothing about the browser already running as this user. `LocalOnly`
middleware closes two doors that were open by default: a site on
attacker-controlled DNS rebinding its own name to 127.0.0.1 became same-origin
and could read the entire ledger, and `_set_override` read `await request.json()`
with no Content-Type check — so a cross-origin `text/plain` fetch, a CORS-*simple*
request sent with no preflight, rewrote `overrides.yaml`. The Host must be a
loopback literal; a write must be `application/json` and same-origin. This does
not make the dashboard a security boundary and is not meant to.

`overrides.yaml` is also the only **partial** config layer. Everything else
replaces whole — an `agents/fixer.yaml` in a nearer directory shadows the
packaged file entirely — which is right for forking an agent and wrong for
changing one line, because the fork freezes that agent's policy and prompt on the
day it was copied.

It adds **no `LedgerKind` member and no router change**, and must not grow one.
Phase boundaries are not in the ledger, so the phase is *derived* from the `layer`
of the agents that have started (`triage` splits by name, because the router
dispatches REPRODUCER and TRIAGE by name too). Deriving is what lets it open runs
written before it existed — including the ones naming the earlier roster
(CARTOGRAPHER, FORGE, VAULT), which render as `not in this roster` rather than
crashing the view. Anything reading the ledger must treat an agent name as data,
not as a key into today's config.

`ui/config_view.py` is the other half: what the installation is *configured* to
do. It is still a reader — `/api/config` is a GET like every other route. It
reports a credential as *whether the variable is set*, never as its value: a
profile names an environment variable precisely so the secret stays out of the
file, and rendering it into a page would put it back
(`test_no_credential_value_reaches_the_payload`).

`ui/state.py` folds entries one at a time, so replay and live tail are the same
`apply()` calls; neither it nor `config_view.py` imports a web dependency, which
is what keeps both read models in the offline suite. `ui/server.py` keeps **one**
`trace.tail` thread per run and fans out over SSE — a browser tab gets a snapshot
of the existing view plus deltas, never its own re-read of a 40,000-line file. A
client dropped for falling behind is *told*: discarding it alone left its
generator parked on `await client.get()` forever, so `EventSource` saw an open
connection and never reconnected, and a frozen tab looks live. Two numbers are
deliberate: the progress bar measures elapsed against `max_wall_clock_s` and
never dollars (no shipped mode sets a budget cap, so a percentage would be
invented), and cost sums `agent_finished` rather than `results/*.json`, because a
run predating the per-invocation filename has fifteen ledger lines for one agent
and one clobbered result file.

### Runtime state

`.qaas/` (gitignored):

- `.env` — credentials, read before every command. Anything already exported
  wins, and `QAAS_ENV_FILE=` disables the whole mechanism. Found from the
  *project root*, not the working directory: every other resolution in the
  system walks up, and this one did not, so `cd api && qaas run` found no `.env`
  and failed several steps later on a missing variable.
- `runs/<id>/ledger.jsonl` — append-only audit trail: every tool call, denial and
  escalation. `envelopes/`, `artifacts/`, `results/` beside it.
- `system-map/` — versioned, shared across runs and pinned per run, so a bad map
  cannot half-propagate.
- `memory.db` — the cross-run defect memory.
- `scores/<run-id>.json` — each scoring, persisted.

The ledger's `kind` is a closed set (`store.LedgerKind`, 29 members) — **add a
member, never repurpose one**: the router reads `verdict`, `review` and `vcs`
back for control flow, so these are a wire format, not labels. `store.ledger()`
skips and counts lines it cannot parse; a run killed mid-write leaves a truncated
last line, and raising on it made the whole ledger unreadable through `qaas
show`, `qaas trace` and every dashboard route — for exactly the run whose audit
trail mattered most.

Evidence is referenced by `artifact://<run>/<name>` uris; `resolve_artifact`
rejects paths escaping the store, and `put_artifact` appends a counter rather
than overwriting a *different* finding's evidence under the same agent-chosen
name.

`trace.py` is the read side (`qaas trace`, `qaas show`); it reads the file once
and filters in memory, because `store.ledger(kind)` is a full-file scan of a file
that runs to tens of thousands of lines. `tail` counts `run_started` minus
`run_finished` rather than latching on the first `run_finished`, because a
resumed run legitimately contains both.

### Calibration is the point

`target-app/` is a deliberately buggy FastAPI + React app; `target-app/defects.yaml`
is the golden ledger. `scorecard.py` measures recall, precision, false-positive
rate and severity agreement against it, and `Scorecard.by_agent(envelopes)`
splits every number by `discovered_by` — that join was always one step away and
never made, so the most actionable calibration fact ("LOAD is at 40% precision on
performance, AUDITOR at 95% on security") was invisible behind a run-wide average
that moves too little to read.

**If you change a seeded defect, change its ledger entry in the same commit** — a
stale ledger silently corrupts every score. Retire a repaired defect with
`fixed_in: <ref>`; never delete the entry, because its severity and domain
expectations are what make a past score reproducible. The `not_defects` section
plants correct-but-suspicious code so precision is measured, not assumed; every
entry there must carry `why_correct`.

Three matching rules are bug-derived. **An anchor is *where*, never *what*** — the
right endpoint plus the right file scored 0.75 against a 0.5 threshold on keyword
overlap of exactly zero, so "this endpoint is slow" was credited with finding the
cross-tenant leak in the same handler. **Duplicates count against precision** —
they were outside the denominator as well as the numerator, so a run that found
three defects and filed forty restatements scored 100%, which is the exact ticket
spam `qaas sweep --min-precision` exists to catch. **`--domain` filters both
sides** — it narrowed only the golden ledger, so scoring with `--domain api`
charged every correct frontend and database finding as a false positive.

**That obligation is a human's, and lands at merge.** No agent's `write_paths`
include the ledger, deliberately: an agent that can retire an entry can raise its
own recall without fixing anything. So a review must never block a fix on the
ledger being updated — FIXER is not permitted to make that change, and demanding
it deadlocks the fix loop. That is not hypothetical; CORVID-7 escalated twice,
REVIEWER requiring the edit and the guardrail refusing it, before the
contradiction was visible. The general rule this taught, now in
`adversarial-review`: **never request a change the author is not permitted to
make** — route it as separate human work instead.

Run `qaas score` after changing any prompt, threshold or model. It is the only
way to know whether a change helped.

### What survives a run

`defects` says a defect was **seen before**. The `outcomes` table says what became
of it — `verified | not_fixed | regressed | review_rejected | held |
not_reproducible | false_positive` — and `search_similar` renders it beside the
match. Before it, held findings, REQUEST_CHANGES and NOT_FIXED were typed ledger
lines the router read back *within* a run and nothing read afterwards, so the
system had a memory and no lessons.

**Every write is Python's, from outside an agent's turn.** `router._record_outcomes`
runs once before `run_finished`; `cli._persist_score` and
`cli._remember_false_positives` run in a process with no agent in it, measured
against a ledger that is in nobody's `write_paths`. An agent that can write its
own outcome can record itself as correct and raise its own apparent precision
without finding anything — the same shape as an agent that can retire a golden
entry, and it has to be designed out rather than trusted away.

**Automatic confidence-threshold feedback is deliberately not built.**
`confidence` is a number the discovering agent writes into its own envelope, so
any rule of the form "agent X has been reliable, lower X's gate" is an agent
grading itself. Measure it, print it, and require a human to write
`overrides.yaml`.

**The regression loop was unreachable in every shipped roster.** The REGRESSION
branch in `defect_memory.record` fires only on `resolved_at`; its only writer was
the `mark_resolved` *tool*; and VERIFIER — the one agent that closes a ticket —
has no `defect_memory` server. So the most valuable thing a system with a memory
can say could never be said. The fix is not to grant VERIFIER the server: it is
for the router to write the fact it already has in hand.

**Memory is partitioned by target.** It was one `memory.db` per state root with an
unfiltered `SELECT * FROM defects`, and `qaas run --repo` puts every clone under
that one root — so one project's memory answered another's, and a close-enough
match from an unrelated codebase came back as "already tracked as PROJ-N, do not
file again". A suppression, persisted, repeating every run. The column migrates
in place (`_MIGRATIONS`, applied from `PRAGMA table_info`) and `''` means
"recorded before this column existed" and stays visible, because the memory
deliberately outlives a release.

### Where qaas's own resources come from

`paths.py` exists because the package used to assume it was running from its own
git checkout, so a `pip install` produced a CLI where every command needing config
died. It separates two ideas that had been collapsed into `Path.cwd()` — where
qaas's resources live, and where the user's project state lives — from a third
that belongs to `TargetProfile.root_path`. Precedence is the ordinary one:

1. explicit — `--config` / `QAAS_CONFIG_DIR`
2. project — `<project>/.qaas/config`, and the source-checkout `<project>/config`
3. packaged — `src/qaas/defaults/config`, `src/qaas/prompts`, `src/qaas/plugin`

`--config` **heads** that search rather than replacing it, which is what this file
has always documented and what `QAAS_CONFIG_DIR` already did. Collapsing it meant
the flag and the environment variable, documented as the same precedence step,
behaved differently. A `--config` naming a directory that does not exist is an
error, not a silently dropped layer.

Layering *granularity* differs by kind, deliberately: `system.yaml` — **first hit
wins whole**, because merging run-mode dictionaries across layers produces a
configuration nobody wrote and nobody can read back; `agents/*.yaml`, prompts and
skills — **union by name, higher layer shadows**, so raising FIXER's budget is one
dropped-in file rather than a fork of the whole roster.

Distribution name is `qaas-python` (`qaas` was taken); the import package and the
CLI are both `qaas`. Everything an installed run needs sits under `src/qaas/`.
**The wheel must never carry `target-app/`** — 69M of deliberately vulnerable code
has no business in `site-packages`; CI fails the build if it appears. The sdist
does carry it, so a contributor can run `qaas score`.

### SDK drift

`sdk_compat.py` reads hook-event names out of the installed `claude-agent-sdk` at
import time and fails loudly if they change. Published docs disagreed with the
installed package (camelCase vs PascalCase events, `HookMatcher(event=/handler=)`
vs `matcher=/hooks=`) — trust the installed package, not the docs.

## Conventions

- Comments explain *why*, and many record a bug that was actually hit. **Do not
  delete those**; they are the reason the code looks the way it does.
- New agent capability goes in a skill or a prompt. New *enforcement* goes in
  Python, never in a prompt.
- `setting_sources=[]` — nothing is loaded from the filesystem. It was
  `["project"]`, defended on reproducibility grounds, and that held only while
  `cwd` was *our* repo. `cwd` is the target now, and with `qaas run --repo` it can
  be a repository cloned seconds ago from a pasted URL; "project" would load its
  `.claude/settings.json`, hooks and MCP servers into a process holding
  Anthropic, Jira and GitHub credentials. It must be an explicit `[]`, not None.
- No merge path exists anywhere, and force-push is not a parameter. Merge is a
  human decision (§8.4), and `FORBIDDEN_BASH` enforces it.
- A test double that reads a different field from the real consumer tests the
  double. If a helper interprets a result, point it at the key production reads.
