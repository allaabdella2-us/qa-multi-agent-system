# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`qaas` — a multi-agent QA system built on the Claude Agent SDK. Agents read a
target application, find defects, reproduce them, and file tickets. It is built
to the design in `qa-agent-system-architecture.md`; `BUILD_PLAN.md` is the
milestone checklist. Code comments cite that document by section (`§8.1`,
`§5.3`) — when changing behaviour those sections describe, read the section
first, and update the plan's milestone table when work lands.

## Commands

```bash
uv venv && uv pip install -e ".[dev]"    # setup
npx playwright install chromium          # only for UI (SURFACE) runs

pytest                                   # 649 tests, no API calls, no network
pytest tests/test_guardrails.py::test_name -x
pytest -m docker                         # needs target-app running
pytest -m 'llm or github or jira'        # tiers excluded by default in pyproject

qaas validate                            # config + prompts + allowlists, no API call
qaas doctor --target corvid              # what a target makes possible
qaas run --mode pr-check --dry-run       # renders each agent's options, no API call
qaas run --mode nightly --only CONDUIT   # real run, costs money
qaas runs / qaas show <run-id> / qaas map
qaas trace <run-id> --follow --quiet     # watch a live run's decisions
qaas board                               # find-or-create this target's Jira board
qaas trace <run-id> [--agent NAME] [--kind KIND] [--json]   # the ledger, readably
qaas score [<run-id>]                    # recall/precision against the golden ledger
qaas sweep                               # run + score + fail below the precision gate

cd target-app && docker compose up -d    # the demo app under test
```

Markers `llm`, `docker`, `github`, `jira` are deselected by `addopts`; the
default `pytest` run is offline and free, and must stay that way.

## Architecture

### Two things that shape everything

1. **CONDUCTOR is Python, not a prompt.** `conductor.py` is an ordinary state
   machine: phase ordering, concurrency, the budget governor, loop breakers,
   escalation. A model cannot enforce a budget it is itself spending.
2. **Each agent is its own top-level `query()`**, not an SDK subagent. That is
   what gives a real context boundary, a per-agent tool allowlist, and a
   per-agent cost number. `agents=`/`AgentDefinition` is only for intra-agent
   fan-out.

### Phase pipeline (`conductor.py`)

```
map -> discover -> reproduce -> file -> verify -> report
CARTOGRAPHER   CONDUIT/SURFACE/…   FORGE   CLERK   PROOF   CHRONICLE
```

Discovery agents run concurrently up to the mode's cap; FORGE runs **once per
finding** in a fresh context (so cost scales with findings, not agents); PROOF
loops with bounded reopens and escalates rather than cycling. `Budget.check()`
runs before every dispatch and raises `BudgetExceeded`, which is a control
working, not an error.

### The DefectEnvelope is the only inter-agent type

`envelope.py`. Agents never pass prose to each other. Validated on write and on
read; `extra="forbid"` everywhere. Two gates live on the model itself, not in a
prompt: `has_evidence()` (an artifact or a failing test) and `is_fileable()`
(evidence + confidence ≥ threshold + not `not_reproducible`). `fingerprint()`
deliberately excludes prose, line numbers, commit sha and timestamps so the same
defect found twice hashes the same.

### Agents are data, not code

An agent is a prompt in `src/qaas/prompts/<AGENT>.md` plus a file in
`config/agents/<agent>.yaml`. Adding one should require **no change** to
conductor, runner, registry or guardrails — treat a change to those files while
adding an agent as a sign something is wrong. Constraints enforced in
`config.py`: at most 6 MCP servers per agent (§5.3, tool-selection accuracy),
and every `must_call` tool must name a server the agent actually has.

Discovery AND reporting agents dispatch **by layer**; every other phase
dispatches by name. So adding a discovery or reporting agent is a prompt file
plus a YAML file and no Python --
`_phase_discover` falls back to `tasks.discovery` for anything without a
bespoke builder. VAULT and WARDEN were added that way and found that it was
not true before them.

`prompts/_shared.md` is appended to every agent prompt — house rules go there,
not copy-pasted into six prompts. Prompts resolve through `Workspace.prompt_dirs`
(`.qaas/prompts/` beats the packaged copy), **file by file and independently**, so
overriding `CONDUIT.md` keeps the house `_shared.md`. A `<AGENT>.append.md` is
inserted between the agent block and the shared block — never after it, because
the house rules must stay the last word. `qaas prompts list/eject/diff`.
Role and standards live in the system prompt;
*procedure* lives in `.claude/skills/*/SKILL.md`; the per-run *task* (which app,
which environment, which finding) is built in `tasks.py`.

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

`ALWAYS_GRANTED` (ToolSearch, Skill, TodoWrite, Task, Agent) is read by both
`build_allowed_tools` and the guardrail — a mismatch there silently disables
every skill. Denying `ToolSearch` breaks MCP access entirely, since MCP tools
arrive deferred.

Denials return a reason and are logged to the ledger; they never kill the turn.

### Hooks enforce the output contract

`registry.build_hooks`: the `Stop` hook blocks an agent that has not called its
`must_call` tools, while it still has a turn to fix it (the conductor would only
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
raised**, so the agent reads the reason and corrects itself.

### One Jira view per repository, not one project

Every ticket carries `repo-<target>`, stamped by `mcp/tracker.py` rather than
asked of CLERK. `JiraTracker.ensure_repo_board` finds-or-creates a saved filter
over exactly that label, plus a board over the filter **only where one can
render**; `cli._ensure_board` calls it at the top of every Jira-backed run. A
*project* per repo would need admin rights a bot account rarely has; a filter
needs none.

**A 200 from the board API is not a working board.** Team-managed (next-gen)
projects own their board; `POST /rest/agile/1.0/board` over a filter still
returns 201, and the resulting board has no `location` and therefore no page in
the UI — every candidate URL 404s. So `is_team_managed` skips the attempt, and
`_board_is_reachable` re-checks anything that was created. `board_url` never
assembles a URL either: `/secure/RapidBoard.jspa?rapidView=<id>` is followed and
whatever Jira resolves it to is the answer. None of this can fail a run — the
filter is always the fallback, and the findings matter more than the view.

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
repurpose one**: the conductor reads `verdict`, `review` and `vcs` back for
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
ledger being updated — MENDER is not permitted to make that change, and demanding
it deadlocks the fix loop. That is not hypothetical; CORVID-7 escalated twice,
ARBITER requiring the edit and the guardrail refusing it, before the contradiction
was visible. The general rule this taught, now in `adversarial-review`: **never
request a change the author is not permitted to make** — route it as separate
human work instead.

Run `qaas score` after changing any prompt, threshold or model. It is the only
way to know whether a change helped.

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
