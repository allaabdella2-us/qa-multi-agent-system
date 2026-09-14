# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`qaas` — a harness around Claude Code itself, not a program that calls an API.
Each agent is a real Claude Code session (see "Two things that shape
everything"): its own process, context, tool allowlist and budget, run in a
phase order by a Python state machine that can refuse any tool call any of them
makes. Agents read a target application, find defects, reproduce them into
failing tests, file tickets, fix them and verify the fix. It is built
to the design in `qa-agent-system-architecture.md`; `BUILD_PLAN.md` is the
milestone checklist. Code comments cite that document by section (`§8.1`,
`§5.3`) — when changing behaviour those sections describe, read the section
first, and update the plan's milestone table when work lands. `ARCHITECTURE.md`
walks the code as it now stands, `MANUAL.md` is the user-facing command
reference, `docs/dashboard.md` covers `qaas dashboard`, `PROVIDERS_PLAN.md` is
the staged plan for making the model a choice — a change that outdates this
file usually outdates those too.

## Commands

```bash
uv venv && uv pip install -e ".[dev]"    # setup
npx playwright install chromium          # only for UI (BROWSER) runs

pytest                                   # 987 tests, no API calls, no network
pytest tests/test_guardrails.py::test_name -x
pytest -m docker                         # needs target-app running
pytest -m 'llm or github or jira'        # tiers excluded by default in pyproject

qaas init                                # scaffold .qaas/ and a target profile
qaas validate                            # config + prompts + allowlists, no API call
qaas targets                             # profiles this project can see
qaas doctor --target corvid              # what a target makes possible
qaas prompts list / eject / diff         # prompt overrides in .qaas/prompts/
qaas tracker-check                       # Jira credentials, before spending anything
qaas run --mode pr-check --dry-run       # renders each agent's options, no API call
qaas run --mode nightly --only API       # real run, costs money
qaas run --mode fix-cycle --from-board "Ready for Fix"   # the board picks the work
qaas runs / qaas show <run-id> / qaas map
qaas trace <run-id> [--follow] [--quiet] [--agent NAME] [--kind KIND] [--json]
qaas dashboard [<run-id>]                # the ledger and the config, in a browser
qaas board                               # find-or-create this target's Jira board
qaas score [<run-id>]                    # recall/precision against the golden ledger
qaas sweep                               # run + score + fail below the precision gate

cd target-app && docker compose up -d    # the demo app under test
```

**Before pushing, run what CI runs** (`.github/workflows/ci.yml`) — there is no
linter or formatter configured, so these three are the whole gate:

```bash
pytest -q                                # offline, free, no API key
qaas validate                            # config, prompts and allowlists cohere
qaas run --mode pr-check --dry-run       # every agent's options assemble
```

CI adds one more on the packaging job: the wheel must not contain `target-app/`.

Markers `llm`, `docker`, `github`, `jira` are deselected by `addopts`; the
default `pytest` run is offline and free, and must stay that way.

`qaas dashboard` needs the `[ui]` extra and reads `.qaas/` **relative to the
working directory** — run it from the target project's checkout, not from here,
or you will be looking at the demo app's runs.

## Architecture

### Two things that shape everything

1. **ROUTER is Python, not a prompt.** `router.py` is an ordinary state
   machine: phase ordering, concurrency, the budget governor, loop breakers,
   escalation. A model cannot enforce a budget it is itself spending.
2. **Each agent is its own top-level `query()`**, not an SDK subagent — and that
   `query()` is **a Claude Code process**. The SDK resolves
   `shutil.which("claude")` and spawns the binary once per invocation, so qaas is
   not a program that calls an API: it is a harness around Claude Code itself,
   holding fifteen real sessions and deciding what each may touch. That is what
   makes the context boundary, the tool allowlist and the per-agent cost number
   real rather than bookkeeping — they are separate OS processes.
   `agents=`/`AgentDefinition` is only for intra-agent fan-out. The binary
   normally comes with the wheel -- `claude_agent_sdk/_bundled/claude`, which
   the SDK prefers over `PATH` -- so `qaas validate` mirrors that order rather
   than calling `shutil.which` alone, which reported a problem to every user
   whose only copy was the bundled one.

### Phase pipeline (`router.py`)

```
map    -> discover     -> synthesise  -> reproduce  -> file   -> verify   -> report
MAPPER    API/BROWSER/…  SYNTHESIZER     REPRODUCER    TRIAGE    VERIFIER    REPORTER
```

Discovery agents run concurrently up to the mode's cap; REPRODUCER runs **once per
finding** in a fresh context (so cost scales with findings, not agents); VERIFIER
loops with bounded reopens and escalates rather than cycling.

`synthesise` exists because independence has a cost. Each discovery agent is its
own process with its own context, which is what makes each of them good — and a
defect whose proof spans two surfaces therefore arrives as two findings, each
correctly judged minor by an agent that could only see its half. Nothing joined
them: `fingerprint()` leads with `domain`, so the two halves are *guaranteed* to
hash differently, and the prompts said "leave the other surfaces to the agents
that own them". SYNTHESIZER reads every envelope and asks the one question no
discovery agent can. It sits **before** reproduce so a composite earns a failing
test and a ticket like any other finding — REPORTER already sees everything and
can already emit, and is useless for this precisely because it runs *after* file
and verify, so anything it emits is written, scored and never filed.

It dispatches **by layer**, like discovery and reporting, so a second synthesis
agent is a prompt and a YAML. It skips below two findings and says so: a
frontier-model context dispatched to join one finding is a bill for nothing.
It has no `must_call` deliberately — most runs contain no conjunction, and an
agent required to emit will assemble one that arrives wearing a severity higher
than either of its parts.

Because that cost scales with findings, `_phase_reproduce` gates on
`thresholds.reproduce_min_severity` (default `major`) before it fans out. A run
that found 85 mostly-minor issues opened 85 contexts and spent $45 filing
nothing. A finding below the floor is still **filed** — `is_fileable` wants
evidence, confidence and "not `not_reproducible`", and `unattempted` passes all
three — it just does not get a committed failing test. The gate cannot move
`qaas score`, because the scorecard reads envelopes and REPRODUCER emits none;
that is what makes it a pure cost lever rather than a calibration change. `Budget.check()`
runs before every dispatch and raises `BudgetExceeded`, which is a control
working, not an error.

`verify` is a loop, not a step. `_verify_loop` dispatches VERIFIER; on `NOT_FIXED`
it calls `_remediate` (FIXER -> REVIEWER, bounded by
`max_mender_arbiter_round_trips`) and re-runs VERIFIER, bounded by
`max_proof_reopens`.

**A loop that carries nothing forward is a retry.** `record_review` refuses
REQUEST_CHANGES without `concerns`, on the stated grounds that FIXER gets them
verbatim — and nothing carried them, so round two dispatched FIXER with a
byte-identical prompt and `max_mender_arbiter_round_trips: 2` bought a second
attempt at the same coin flip. `_entry_since` returns the whole `LedgerEntry`
and `tasks.fixer(..., feedback=...)` renders it. Prose assembled in Python out
of typed ledger data: no new tool, no new `LedgerKind`, and nothing a model has
to be trusted to pass on.

**Both helpers are scoped to the dispatch that should have produced them.**
`_entry_since(store, kind, ticket, mark)` mirrors `_branch_written_since`.
Unscoped, they took the last entry for the ticket from anywhere in the ledger —
so a VERIFIER that finished without calling `record_verdict` silently inherited
the previous verdict. That is not only a crash path: the Stop hook deliberately
lets an agent through after one block, so a silent VERIFIER is ordinary. On a
resumed run, where `_phase_verify` re-selects every ticketed envelope, it means
a VERIFIED nobody verified.

**FIXER's `write_paths` include `qa/repro`** — REPRODUCER's sandbox, holding the
failing test — so §10's symptom-fix guard depended entirely on
`policy.protected_paths`, which no agent YAML sets and nothing else populated.
`Guardrail._protected_path` always returned False. `_with_protected_test`
deep-copies the spec per invocation and names that ticket's test; per-invocation
because which test is protected depends on which ticket is being fixed, and
deep-copied because the roster is shared across the whole run.

Two more facts there are bug-derived. VERIFIER must re-verify **the
branch FIXER wrote**, read back out of the ledger by `_branch_written_since`
and scoped to entries since a mark — the envelope names the *repro* branch,
which by construction carries a failing test and no fix, and sending VERIFIER back
there made VERIFIED unreachable in a live run. And a roster with no FIXER
escalates `NOT_FIXED` to a human immediately, which is right: the alternative is
re-running VERIFIER against unchanged code.

### The DefectEnvelope is the only inter-agent type

`envelope.py`. Agents never pass prose to each other. Validated on write and on
read; `extra="forbid"` everywhere. Two gates live on the model itself, not in a
prompt: `has_evidence()` (an artifact or a failing test) and `is_fileable()`
(evidence + confidence ≥ threshold + not `not_reproducible`). `fingerprint()`
deliberately excludes prose, line numbers, commit sha and timestamps so the same
defect found twice hashes the same.

### Agents are data, not code

An agent is a prompt in `src/qaas/prompts/<AGENT>.md` plus a YAML file in
`src/qaas/defaults/config/agents/<agent>.yaml` — the shipped roster, 15 agents.
A file of the same name under `<project>/config/agents/` or
`.qaas/config/agents/` *shadows* the packaged one; this checkout's `config/`
holds only `targets/`, so adding to the roster means the packaged directory and
not an override. Adding one should require **no change** to
router, runner, registry or guardrails — treat a change to those files while
adding an agent as a sign something is wrong. Constraints enforced in
`config.py`: at most 6 MCP servers per agent (§5.3, tool-selection accuracy),
and every `must_call` tool must name a server the agent actually has.

Discovery AND reporting agents dispatch **by layer**; every other phase
dispatches by name. So adding a discovery or reporting agent is a prompt file
plus a YAML file and no Python --
`_phase_discover` falls back to `tasks.discovery` for anything without a
bespoke builder. DBA and AUDITOR were added that way and found that it was
not true before them.

`prompts/_shared.md` is appended to every agent prompt — house rules go there,
not copy-pasted into six prompts. Prompts resolve through `Workspace.prompt_dirs`
(`.qaas/prompts/` beats the packaged copy), **file by file and independently**, so
overriding `API.md` keeps the house `_shared.md`. A `<AGENT>.append.md` is
inserted between the agent block and the shared block — never after it, because
the house rules must stay the last word. `qaas prompts list/eject/diff`.
Role and standards live in the system prompt;
*procedure* lives in `src/qaas/plugin/skills/<skill>/SKILL.md` (30 of them); the
per-run *task* (which app, which environment, which finding) is built in
`tasks.py`.

Skills reach an agent as a Claude Code **plugin** (`--plugin-dir`), not through
filesystem settings, so they travel inside the wheel instead of depending on a
`.claude/skills/` in whatever repository the user happens to be standing in. The
layout was settled by testing the CLI rather than by reading about it, and it is
not optional: a directory of bare `<skill>/SKILL.md` folders loads **nothing,
silently**; only `.claude-plugin/plugin.json` + `skills/<name>/SKILL.md` gives a
stable namespace, taken from the manifest's `name`. `qualified_skills` therefore
rewrites an agent's `skills:` entries to `qaas:<skill>` — the SDK matches skill
names down two channels with different rules, and the unqualified name loads on
one but never matches the allow rule on the other. A skill no plugin provides is
dropped, not passed through.

**Nothing in `tasks.py` or a prompt may name a specific application.** The
system is pointed at a target profile; a prompt mentioning one repo's layout or
one app's seeded users works exactly once.

### Guardrails: enforcement is in the PreToolUse hook

`guardrails.py` implements the §8.1 write-permission matrix. The critical fact:
an `allowed_tools` entry naming a whole tool **auto-approves it before
`can_use_tool` is consulted**, so a policy implemented only in that callback is
silently dead code (this repo had exactly that bug). Primary enforcement is the
`PreToolUse` hook; `can_use_tool` is a second layer for calls the allowlist did
not auto-approve. Both call one `check()` so they cannot disagree.

**One `check()` was not enough, because there are more than two doors.** The
matrix was enforced for `Write`/`Edit` and unenforced for `Bash` and
`mcp__vcs__*`: `_check_bash` consulted only `FORBIDDEN_BASH`, branch patterns and
a substring test, so `sed -i` reached what `Write` could not; `check()`
short-circuits every `mcp__*` call to "is the server declared" without reading
its arguments, so `mcp/vcs.py`'s own near-copy of the rules was the only gate
there — and it had never learned about `forbidden_paths`. `Guardrail._check_path`
is now the single implementation, and all three doors call it. When adding a
path-taking surface, call it; do not restate it.

Reading a shell command for what it writes is best-effort, so the rule that makes
it sound is: **a command that mutates and whose destination cannot be resolved is
refused**, naming `Write`/`Edit` in the reason. Guessing is the one option that
is not available. `_branch_from_command` only inspects git commands — without
that it read `python -c` as `switch -c` and refused it as a bad branch name.

**That rule was stated and not implemented.** `_writes_of` keyed its mutation
test on `parts[0]` and fell through to `([], None)` — "writes nothing" — for
everything it did not recognise, which is the exact opposite of the rule and made
every mutator reachable by putting one word in front of it. Reproduced against
the shipped roster: read-only VERIFIER could run `env sed -i`, `timeout 5 sed
-i`, `xargs sed -i`, `find -exec sed -i`, `curl | sh`, `echo $(rm f)` and plain
`rm f` against paths FIXER itself is forbidden. The corpus is now a parametrized
test; add to it rather than reasoning about whether a new spelling is covered.

Four shapes, all bug-derived:

- **One quote-aware pass.** `_tokenise` is `shlex` with `punctuation_chars`; the
  old regex split could not see quoting, so it cut `echo 'hello; world' && ls`
  into nonsense and cut `echo x >| f` at the `|` of `>|`, leaving a dangling `>`
  whose target was never checked.
- **Wrappers are peeled** (`env`, `timeout`, `nice`, `nohup`, …) before argv0 is
  read. **Indirection is refused** (`xargs`, `eval`, `find -exec`, command
  substitution, a pipe into a shell) — there is no destination to resolve.
- **Deletion and revert are writes.** `rm`, `git rm`, `git checkout -- P`,
  `git restore P`, and `mv`'s *source*. Removing a file changes it more
  completely than editing it does.
- **Pattern matching is case-folded on both sides.** Every shipped pattern is
  lowercase, `fnmatch` on POSIX is case-sensitive, and macOS is not — so
  `api/app/Auth.py` named the file `*auth*` exists to protect and matched
  nothing.

One residual limit, stated because it is a decision and not an oversight:
`python foo.py` and `python -m pytest` are arbitrary code and are **allowed**.
Refusing them was tried and it refuses how FIXER and VERIFIER run the suite; a
guardrail that blocks the system's own happy path is one that gets switched off.
`Write`/`Edit` and `write_paths` remain the boundary for what a script leaves
behind.

`git push` gets `_reject_refspec`'s rule at this door too: its argument is a
*refspec*, so `git push origin fix/x:main` names no branch any pattern check can
see. `mcp/vcs.py` learned that already; the shell door had not.

`ALWAYS_GRANTED` (ToolSearch, Skill, TodoWrite, Task, Agent) is read by both
`build_allowed_tools` and the guardrail — a mismatch there silently disables
every skill. Denying `ToolSearch` breaks MCP access entirely, since MCP tools
arrive deferred.

Anything an agent supplies that becomes argv is a flag until proven otherwise:
`_reject_flaglike` refuses a leading `-`, and `_reject_refspec` also refuses `:`
and a leading `+`, because `git push origin <name>` parses its argument as a
*refspec* — `qa/repro/x:main` published onto main past every branch-pattern and
protected-name check. Test-runner selectors reach pytest with no `--` separator,
so one starting with `-` is an option (`-p`, `-c`, `-o addopts=`), and a
path-shaped one is contained against `target_root` the way `cwd` already was.

Denials return a reason and are logged to the ledger; they never kill the turn.

### Hooks enforce the output contract

`registry.build_hooks`: the `Stop` hook blocks an agent that has not called its
`must_call` tools, while it still has a turn to fix it (the router would only
find out afterwards). It honours `stop_hook_active` — blocking twice burns the
budget. The `PostToolUse` hook tells an agent immediately when an emitted
envelope was **held** rather than filed, since discovering that at the end is
too late to attach evidence.

### MCP servers are in-process

`src/qaas/mcp/` — `envelope`, `test_runner`, `env_control`, `defect_memory`,
`contract_diff`, `tracker`, `vcs`, all via `create_sdk_mcp_server()`. They close
over a `ToolContext` (run store, config, agent spec, pinned map version), so
validation, guardrails and persistence happen where the state already is. Servers
enforce their own domain rules as a third belt (tracker refuses an agent that may
not file; vcs refuses a branch outside the agent's patterns). Playwright is the
one stdio subprocess, declared in `registry.STDIO_SERVERS`.

Tool results use `ok()`/`err()` from `mcp/context.py`: errors are **returned, not
raised**, so the agent reads the reason and corrects itself. `err()` sets
**both** `isError` and `is_error`, and that is the bug rather than
belt-and-braces: the MCP wire format spells it `isError`, the SDK reads the
handler's dict with `result.get("is_error", False)`, and this returned only the
first. So every refusal from all seven servers — a guardrail denial, "you may
not file", "not reproducible" — was delivered to the model as a **successful**
tool result, and the self-correction contract had never once run.

`structuredContent` from `ok()` is dropped by the same path, which is why
`registry._structured` never fired; the held-envelope nudge is carried in the
tool text instead.

A project can declare more servers in `system.yaml` under `mcp_servers:`.
Declaring one grants nothing — an agent receives it only by naming it in its own
`mcp_servers:` list — and `SystemConfig._agents_name_real_servers` checks every
such name against the builtin set plus the declared ones at load time, so a typo
fails `qaas validate` instead of surfacing as `UnknownServer` part-way through a
paid run.

### One Jira view per repository, not one project

Every ticket carries `repo-<target>`, stamped by `mcp/tracker.py` rather than
asked of TRIAGE. `JiraTracker.ensure_repo_board` finds-or-creates a saved filter
over exactly that label, plus a board over the filter **only where one can
render**; `cli._ensure_board` calls it at the top of every Jira-backed run. A
*project* per repo would need admin rights a bot account rarely has; a filter
needs none.

**A 200 from the board API is not a working board.** Team-managed (next-gen)
projects own their board; `POST /rest/agile/1.0/board` over a filter still
returns 201, and the UI has no page for the result — every candidate URL 404s.
`is_team_managed` is the authoritative gate and skips the attempt there.
`_board_is_reachable` (no `location` -> unusable) is a *second, weaker* check
applied **only to a board that already existed**: Jira populates `location`
asynchronously, so a board read back immediately after creation reports None
whatever its project, and gating a fresh board on it rejected good boards.
Location is necessary, not sufficient — a team-managed board eventually reports
one and still will not render.

`board_url` never assembles a URL: `/secure/RapidBoard.jspa?rapidView=<id>` is
followed and whatever Jira resolves it to is the answer (a company-managed
board's path carries a `/c/` segment that a hand-built URL missed).

**A reused filter is only the right filter if its JQL still matches.** Filters
are found by name and the name does not encode the project, so repointing
`JIRA_PROJECT_KEY` left the filter scoped to the old project — tickets filed
correctly and were invisible on the view. `ensure_repo_board` repairs the JQL;
`filter/search` is expanded with `jql` so the check costs no extra round trip.

None of this can fail a run — the filter is the fallback and the findings matter
more than the view.

**`--from-board` is the one place the board drives the system.** `qaas run --mode
fix-cycle --from-board "<status>"` resolves the tickets carrying this repo's
label that sit in that status, finds the run holding their envelopes, and feeds
them in as `--ticket` would. It is deliberately a **pull, not a subscription**:
the board chooses the *work*, and ROUTER still schedules everything after that
out of the ledger. Agents polling a tracker for the length of a run would put a
rate-limited dependency on the control path and buy nothing, because the budget
governor and the loop breakers have to live in code regardless. Two details are
bug-derived: the status is matched **case-blind in the CLI, not by the adapter**
(both backends match exactly, and a column's name is whatever casing someone
typed, so asking the adapter to filter found nothing on a board with cards in
it); and resolution happens **before** the `--dry-run` return, because "what
would this pick up" is the question `--dry-run` exists to answer.

### Targets make it portable

`target.py` / `config/targets/*.yaml`. `environment.mode` is the load-bearing
field: `none` (static reads only), `external` (exercise but never reset), or
`compose` (own the lifecycle). `profile.capabilities()` drives which agents can
usefully run — `qaas doctor` reports this. Credentials are never in a profile;
it names environment variables.

**The qaas project and the target are two different roots.** `paths.Workspace`
owns "where qaas lives"; `SystemConfig.target_root()` → `profile.root_path()`
owns "the application under test". `ToolContext.target_root` carries the second,
and it is what the write-path allowlist, the test runner's cwd, the vcs sandbox
and the SDK subprocess `cwd` are all anchored on. They coincide only for the
bundled demo. A policy's `write_paths`, `protected_paths` and `forbidden_paths`
are **target-relative** — a pattern written `*/x/*` will not match a repository's
own root-level `x/`. Target profiles layer by filename across config dirs
(`config.target_files`), like agents and skills.

`qaas run --repo <path-or-url>` clones into `.qaas/targets/<slug>` and
provisions a profile through the same helper as `qaas init` (`_provision_target`
in `cli.py`). Keep it one code path — two ways to decide what is under test is
two sets of rules about where someone else's code lands on disk.

### The dashboard reads; it never participates

`src/qaas/ui/` is `qaas dashboard` — a localhost page over a run's ledger. There
is no path from the page to a dispatch, a ticket, a branch or the target's
files, and `test_only_the_override_route_writes` holds the line: every route is
a GET except **one**, named in `WRITE_ROUTES`, and adding a second is an
architectural change rather than a feature.

That one route writes `overrides.yaml`, and the shape of the exception is the
point: it changes what a model *is* — which model an agent runs, a turn cap, a
threshold — and never what an agent is *allowed to do*. `config.TUNABLE_AGENT_FIELDS`
is the whole vocabulary; `policy`, `mcp_servers`, `builtin_tools`, `skills` and
`must_call` are absent deliberately, because a page reachable by anything
running as this user must not be a second, quieter door onto the §8.1 matrix
that `guardrails.py` exists to enforce. A refused field is named in the error
rather than dropped, the candidate is validated through a real `load_config` in
a scratch copy before anything lands, and deleting the file undoes all of it.

`overrides.yaml` is also the only **partial** config layer. Everything else
replaces whole — an `agents/fixer.yaml` in a nearer directory shadows the
packaged file entirely — which is right for forking an agent and wrong for
changing one line, because the fork freezes that agent's policy and prompt on
the day it was copied.

It adds **no `LedgerKind` member and no router change**, and must not grow one.
Phase boundaries are not in the ledger, so the phase is *derived* from the
`layer` of the agents that have started (`triage` splits by name, because the
router dispatches REPRODUCER and TRIAGE by name too). Deriving is what lets it
open runs written before it existed — including the ones naming the earlier
roster (CARTOGRAPHER, FORGE, VAULT), which render as `not in this roster` rather
than crashing the view. Anything reading the ledger must treat an agent name as
data, not as a key into today's config.

`ui/config_view.py` is the other half: what the installation is *configured* to
do, which is the same object graph `qaas validate`, `qaas prompts list`, `qaas
doctor` and `qaas run --dry-run` already print into four different terminal
tables. It is still a reader — `/api/config` is a GET like every other route,
and there is no form and nothing to post one to. It reports a credential as
*whether the variable is set*, never as its value: a profile names an
environment variable precisely so the secret stays out of the file, and
rendering it into a page would put it back (`test_no_credential_value_reaches_the_payload`).

`ui/state.py` folds entries one at a time, so replay and live tail are the same
`apply()` calls; neither it nor `config_view.py` imports a web dependency, which
is what keeps both read models in the offline suite. `ui/server.py` keeps **one** `trace.tail` thread per run and
fans out over SSE — a browser tab gets a snapshot of the existing view plus
deltas, never its own re-read of a 40,000-line file. Two numbers are deliberate:
the progress bar measures elapsed against `max_wall_clock_s` and never dollars
(no shipped mode sets a budget cap, so a percentage would be invented), and cost
sums `agent_finished` rather than `results/*.json`, because a run predating the
per-invocation filename has fifteen ledger lines for FORGE and one clobbered
`FORGE.json`.

### Runtime state

`.qaas/` (gitignored): `.env` (credentials, read before every command; anything
already exported wins, and `QAAS_ENV_FILE=` disables the whole mechanism — the
test suite sets that so a developer's `.env` cannot make the suite pass);
`runs/<id>/ledger.jsonl` (append-only audit trail —
every tool call, denial and escalation), `envelopes/`, `artifacts/`, `results/`;
plus a versioned `system-map/` shared across runs and pinned per run so a bad map
cannot half-propagate. Evidence is referenced by `artifact://<run>/<name>` uris;
`resolve_artifact` rejects paths escaping the store.

The ledger's `kind` is a closed set (`store.LedgerKind`) — **add a member, never
repurpose one**: the router reads `verdict`, `review` and `vcs` back for
control flow (`_latest_verdict`, `_latest_review`, `_branch_written_since`), so
these are a wire format, not labels. `trace.py` is the read side (`qaas trace`,
`qaas show`); it reads the file once and filters in memory, because
`store.ledger(kind)` is a full-file scan of a file that runs to tens of thousands
of lines.

### Calibration is the point

`target-app/` is a deliberately buggy FastAPI + React app; `target-app/defects.yaml`
is the golden ledger. `scorecard.py` measures recall, precision, false-positive
rate and severity agreement against it. **If you change a seeded defect, change
its ledger entry in the same commit** — a stale ledger silently corrupts every
score. Retire a repaired defect with `fixed_in: <ref>`; never delete the entry,
because its severity and domain expectations are what make a past score
reproducible. The ledger's `not_defects` section plants correct-but-suspicious
code so precision is measured, not assumed.

**That obligation is a human's, and lands at merge.** No agent's `write_paths`
include the ledger, deliberately: an agent that can retire an entry can raise its
own recall without fixing anything. So a review must never block a fix on the
ledger being updated — FIXER is not permitted to make that change, and demanding
it deadlocks the fix loop. That is not hypothetical; CORVID-7 escalated twice,
REVIEWER requiring the edit and the guardrail refusing it, before the contradiction
was visible. The general rule this taught, now in `adversarial-review`: **never
request a change the author is not permitted to make** — route it as separate
human work instead.

### Where a real parse buys something

`importgraph.py` is the one place in the system that parses rather than guesses,
and it exists for one caller. `affected_tests` answers "what should VERIFIER run
against this diff", and it answered by comparing *filenames*: a test that reaches
the changed module through a caller scored zero — which is step 3 of
`regression-suite-selection` ("a fix inside a shared helper breaks its consumers,
not itself"), so the step the procedure calls essential was delegated to a
ranking that could not see it. A reverse-reachable closure over the import graph
sees it in two hops.

Three properties, all load-bearing. **Parse-only** — `ast.parse` on text that is
never imported, because the target is someone else's repository, cloned from a
pasted URL, and running its module-level code inside the process holding this
user's credentials is not a thing to do for a test ranking. **Python-only, and it
says so** — a Go or TypeScript target yields an empty graph, `affected_tests`
falls back to the heuristic, and the result names which answer it gave, because
"no tests are affected" and "I cannot read this language" are different answers.
**Never raises** — a ranking that crashes the verification phase is worse than
one that is merely incomplete.

Run `qaas score` after changing any prompt, threshold or model. It is the only
way to know whether a change helped. `Scorecard.by_agent(envelopes)` splits every
number by `discovered_by`, which was always one join away and never made — a
run-wide average moves too little to read, while "LOAD is at 40% precision on
performance, AUDITOR at 95% on security" says which agent to tune. Each scoring
is persisted to `.qaas/scores/<run_id>.json`, because the score used to be
computed at three call sites, printed, and thrown away — and "did this change
help" is only answerable against the previous one.

### What survives a run

`defects` says a defect was **seen before**. The `outcomes` table says what
became of it — `verified | not_fixed | regressed | review_rejected | held |
not_reproducible` — and `search_similar` renders it back beside the match. Before
it, held findings, REQUEST_CHANGES and NOT_FIXED were typed ledger lines the
router read back *within* a run and nothing read afterwards, so the system had a
memory and no lessons.

**Every write is Python's, from outside an agent's turn.** `router._record_outcomes`
runs once before `run_finished`; `cli._persist_score` runs in a process with no
agent in it. An agent that can write its own outcome can record itself as correct
and raise its own apparent precision without finding anything — the same shape as
an agent that can retire a golden-ledger entry, and it has to be designed out
rather than trusted away. Agents read this through `search_similar` and have no
way to write it.

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

**Memory is partitioned by target.** It was one `memory.db` per state root with
an unfiltered `SELECT * FROM defects`, and `qaas run --repo` puts every clone
under that one root — so one project's memory answered another's, and a
close-enough match from an unrelated codebase came back as "already tracked as
PROJ-N, do not file again". A suppression, persisted, repeating every run. The
column migrates in place (`_MIGRATIONS`, applied from `PRAGMA table_info`) and
`''` means "recorded before this column existed" and stays visible, because the
memory deliberately outlives a release.

### Where qaas's own resources come from

`paths.py` exists because the package used to assume it was running from its own
git checkout, so a `pip install` produced a CLI where every command needing
config died. It separates two ideas that had been collapsed into `Path.cwd()` —
where qaas's resources live, and where the user's project state lives — from a
third that belongs to `TargetProfile.root_path`. Precedence is the ordinary one:
`--config` / `QAAS_CONFIG_DIR`, then `<project>/.qaas/config` and
`<project>/config`, then packaged.

Layering *granularity* differs by kind, deliberately: `system.yaml` — **first hit
wins whole**, because merging run-mode dictionaries across layers produces a
configuration nobody wrote and nobody can read back; `agents/*.yaml`, prompts and
skills — **union by name, higher layer shadows**, so raising FIXER's budget is
one dropped-in file rather than a fork of the whole roster.

Distribution name is `qaas-python` (`qaas` was taken); the import package and the
CLI are both `qaas`. Everything an installed run needs sits under `src/qaas/`.
**The wheel must never carry `target-app/`** — 69M of deliberately vulnerable
code has no business in `site-packages`; CI fails the build if it appears. The
sdist does carry it, so a contributor can run `qaas score`.

### SDK drift

`sdk_compat.py` reads hook-event names out of the installed `claude-agent-sdk` at
import time and fails loudly if they change. Published docs disagreed with the
installed package (camelCase vs PascalCase events, `HookMatcher(event=/handler=)`
vs `matcher=/hooks=`) — trust the installed package, not the docs.

## Conventions

- Comments explain *why*, and several record a bug that was actually hit — do not
  delete those; they are the reason the code looks the way it does.
- New agent capability goes in a skill or a prompt, not in Python. New
  *enforcement* goes in Python, never in a prompt.
- `setting_sources=[]` — nothing is loaded from the filesystem. It was
  `["project"]`, defended on reproducibility grounds, and that held only while
  `cwd` was *our* repo. `cwd` is the target now, and with `qaas run --repo` it
  can be a repository cloned seconds ago from a pasted URL; "project" would load
  its `.claude/settings.json`, hooks and MCP servers into a process holding
  Anthropic, Jira and GitHub credentials. It must be an explicit `[]`, not None.
- No merge path exists anywhere, and force-push is not a parameter. Merge is a
  human decision (§8.4), and `FORBIDDEN_BASH` enforces it.
