# Changelog

## 0.0.2

### The ticket cap stops throttling findings you already paid for

**Behaviour change.** `max_tickets_per_run` was 10, in both
`thresholds` and TRIAGE's own policy. That is lower than the number of real
defects an ordinary repository holds, so a run that found eighty filed ten —
and the other seventy sat on disk as envelopes nobody was looking at. The cap
was doing the work of a precision gate, badly: it throttles *output*, and
nothing about it makes the findings it withholds any more likely to be right.

Both numbers are now 100, which is the number a §8.3 loop breaker should be. A
TRIAGE stuck in a loop filing thousands of tickets is a real hazard and the
ledger is the wrong place to discover it; eighty legitimate defects is not that,
and the two cases needed telling apart.

The pair is the part worth knowing about. `mcp/tracker.py` enforces
`min(policy.max_tickets_per_run, thresholds.max_tickets_per_run)`, so a project
that raised the threshold in its own `system.yaml` and left TRIAGE's shipped
policy alone still filed ten, with nothing anywhere saying why. Raising one
alone silently changes nothing — which is the same shape as the bug that put
that `min()` there, seen from the other side.

Findings over the cap were never discarded: they keep their envelopes,
`_phase_file` skips envelopes that already carry a ticket key, and a resume
files the next batch. The dashboard's description of this threshold said the
cap "takes the most severe findings first, so what it drops is the tail" —
both halves were wrong, and it now says what actually happens.

### A run survives the provider running out of quota

`run-20260919T152757-4c8c37`: 8 agents, 2h45m, $56.58, 34 findings, discovery
complete — and then TRIAGE and ten REPRODUCER invocations died within seconds of
each other on "You've hit your session limit · resets 4:20pm". Eleven identical
escalations, zero tickets, and findings that were filed by hand hours later. The
budget governor knew about dollars and wall clock and nothing about a provider
that has stopped serving, and the reserve holds back *clock*, which buys nothing
against that wall.

`is_quota_error` classifies it, `_dispatch` raises `QuotaExhausted` rather than
escalating, `_gather` stops starting queued work, and `run()` skips filing and
verification instead of dispatching them into the same limit. The run ends with
one `quota_exhausted` ledger line — a new `LedgerKind`, because "a human must
look at this finding" and "run it again at 4:20pm" are opposite instructions —
carrying the count of findings still unfiled and the exact command that resumes
the run. Resume itself is unchanged: the agents that succeeded are already
skipped, and the envelopes that never got a ticket are already picked up.
### The import graph reads TypeScript and JavaScript

`test_runner` learned to run vitest and jest, and the graph behind
`affected_tests` was still Python-only — so a TS target's suite ran and its
*selection* fell back to comparing filenames, which is the half that makes this
more than a guess. Verified against a real Next.js console: tests ran, the graph
was empty. It now scans TS/JS import syntax (static, re-export, dynamic
`import()`, `require`) and resolves it the way the language does — extensionless
specifiers, `./dir` to `./dir/index.*`, an ESM `./y.js` back to the `y.ts` it was
emitted from, and `tsconfig.json` `paths`/`baseUrl` aliases, without which most
imports in a modern TS repository resolve to nothing. Still parse-only: no
dependency, no `node`, no `tsc`. A repository holding both languages gets one
graph covering both, and what the graph could not read is named in the answer
rather than averaged into "no tests are affected".

### Test selection is derived now, not guessed

`affected_tests` ranked tests by filename similarity, so a test reaching the
changed module through a caller scored zero — which is exactly step 3 of
`regression-suite-selection`, the step that catches a broken consumer of a shared
helper. `importgraph.py` builds a reverse-reachable import closure with stdlib
`ast`, parse-only, and the ranking reports the hop distance. It degrades to the
old heuristic for non-Python targets and says which answer it gave.

### Two doors on the dashboard were open by default

The server binds 127.0.0.1, which stops a network peer and does nothing about
the browser already running as this user. A site on attacker-controlled DNS
rebinding to 127.0.0.1 became same-origin and could read the whole run ledger;
and `_set_override` read `await request.json()` with no Content-Type check, so a
cross-origin `text/plain` fetch — a CORS-simple request, sent with no preflight —
rewrote `overrides.yaml`. Loopback Host required, JSON content type required,
cross-origin writes refused.

### A target's own test suite ran with your credentials

`test_runner` built the child environment as `dict(os.environ, ...)` minus two
pytest keys, so the target repository's suite — someone else's code, cloned from
a pasted URL — ran with `ANTHROPIC_API_KEY`, `JIRA_API_TOKEN` and `GITHUB_TOKEN`
in scope. A `conftest.py` reading `os.environ` was the whole exploit. It is an
allowlist now.

### env_control never checked whether it owned the environment

`environment.mode` is the load-bearing field of the target system — `none` means
static reads only, `external` means "exercise it but never reset it, someone else
may be relying on it". `spin_up`, `seed`, `reset` and `tear_down` consulted only
whether a compose file existed and whether `docker` was on PATH, so a compose
file left lying in a repository was enough to destroy a shared staging
environment the profile had declared off-limits.

### API and AUDITOR could not meet their own evidence bar

Three prompts make observation the standard — "a finding you have not observed is
a hypothesis, not a defect" — and no agent held a tool that could issue an HTTP
request. `WebFetch` is refused to every agent, correctly; nothing let them reach
the running app either. `env_control.http_request` closes that, contained to the
target's own origin: an agent passes a path, never a host.

### Calibration was measuring the wrong things

- **An anchor is *where*, not *what*.** The right endpoint plus the right file
  scored 0.75 against a 0.5 threshold with zero keyword overlap, so "this
  endpoint is slow" was credited with finding the cross-tenant leak in the same
  handler. Recall counted it; the real defect went in `missed`.
- **Duplicates were outside the precision denominator**, so a run that found
  three defects and filed forty restatements scored 100% — the exact ticket spam
  `qaas sweep --min-precision` exists to catch.
- **`--domain` narrowed only the golden side**, so scoring with `--domain api`
  charged every correct frontend and database finding as a false positive.
- **UI-08 sat under `not_defects`** with no `why_correct` while its own text
  called it a latent defect, so a correct report of it was scored as noise.
- **API-09 is a WebSocket defect filed under `api`**, so SOCKET could only ever
  miss it and be charged for finding it.

### A named target that did not exist was silently ignored

`if chosen and profiles:` meant "named but absent stays fatal" held only when
some profile existed *somewhere* — and a fresh `pip install` has none, because
the packaged config ships no `targets/`. So `--target`, `QAAS_TARGET` or
`system.yaml` naming a profile that was not there fell back to the working
directory, pointing every write-path sandbox, the test runner's cwd and the SDK
subprocess at whatever directory the operator was standing in.

`--config` now heads the layered search rather than replacing it, which is what
`paths.py` has always documented and what `QAAS_CONFIG_DIR` already did. A
`--config` naming a directory that does not exist is an error instead of a
silently dropped layer.

### Other fixes

- `.env` is found from the project root, not the working directory, so running
  from a subdirectory no longer loses every credential.
- `parse_env` strips an unquoted trailing `# comment`, which was becoming part of
  the value — `JIRA_PROJECT_KEY=KAN  # the board` was a corrupted project key.
- `emit_envelope` resolves the `artifact://` uris it is handed. The evidence gate
  accepted a well-formed string naming a file that had never been written.
- `emit_envelope` strips server-owned fields and closes its schema; an agent
  could supply `id` and overwrite another agent's envelope on disk.
- `fingerprint()` folds `service`/`endpoint`/`ui_route` the way the similarity
  scorer already did, so `GET /v1/orders` and `get /v1/orders/` stop being two
  defects.
- `defect_memory.record` and `mark_resolved` are gated by policy, like every
  other write tool on the envelope server.
- A glob `write_path` no longer escapes the checkout — `fnmatch`'s `*` crosses
  `/`, so `*_test.py` matched `/etc/x_test.py`.
- `HARNESS_TOOLS` is derived from `ALWAYS_GRANTED`; the comment claimed they
  could not drift and they had.
- FIXER's `must_call: [mcp__vcs__open_pr]` is dropped when the configured backend
  has no remote. Under the committed `vcs: local` it was a contract no behaviour
  could satisfy, so the Stop hook blocked on every fix.
- `mcp__vcs__commit` no longer spends FIXER's §8.2 diff budget on its own
  `write_paths`; `LocalGit.commit` tolerates a write path the repository does not
  have, instead of staging nothing.
- `board_url` rejects a redirect that leaves the Jira site — on an SSO-enforced
  instance it was handing out the identity provider's login URL.
- Jira `search` pages past the 100-row API cap instead of silently halving the
  200-row window `--from-board` asks for.
- `thresholds.max_tickets_per_run` is enforced by the tracker, not only by
  TRIAGE's prompt.
- `run_n_times` divides by the runs that completed, not the runs requested — an
  early stop reported an 85% flake rate on a test that never disagreed with
  itself.
- `contract_diff` and `impersonate` resolve the target's base URL from the
  profile instead of hardcoding `localhost:8000` and a service named `web`.
- `store.ledger()` counts unreadable lines; `put_artifact` no longer silently
  overwrites another finding's evidence; `SystemMapStore` no longer mkdirs from a
  reader.
- `trace --follow` survives a resumed run; `qaas show` dates a resumed run from
  its latest session; `--kind` beats `--quiet`, as documented.
- Per-agent caps span the run rather than one dispatch; concurrent dispatches
  share the remaining budget instead of each being handed all of it; a non-budget
  exception still writes `run_finished`.
- `qaas sweep` exits non-zero when the run failed or stopped early, and checks
  that before the no-ledger return.
- `--only` is validated against the mode, not the whole roster; `--run-id` on a
  run that does not exist is refused instead of silently starting a new one;
  a credential embedded in a `--repo` URL is no longer echoed or stored;
  `--repo` refuses to reuse a same-named profile pointing at a different
  repository.
- The test suite clears every `QAAS_*` override at import time, not in a session
  fixture that ran after collection — `QAAS_TRACKER=jira` in a shell errored
  fourteen tests before the fixture meant to prevent it could run.

### Every MCP tool error was delivered to agents as a success

`err()` set `isError`, the MCP wire spelling. The SDK builds the result from
`result.get("is_error", False)` and drops anything else. So every refusal from
all seven in-process servers — a guardrail denial, "you may not file that",
"that is not reproducible" — arrived at the model marked **successful**, and the
"errors are returned, not raised, so the agent reads the reason and corrects
itself" contract had never once run. `err()` now sets both.

### The shell was a way round the write matrix again

`_writes_of` keyed its mutation test on `argv[0]` and treated anything it did not
recognise as writing nothing — the exact opposite of the rule the module states
about itself. Reproduced against the shipped roster: read-only VERIFIER could run
`env sed -i`, `timeout 5 sed -i`, `xargs sed -i`, `find -exec sed -i`, `curl | sh`,
`echo $(rm f)`, `sed --in-place`, `cp -t`, `echo x >| f` and plain `rm f` against
paths FIXER itself is forbidden.

Now: one quote-aware `shlex` pass instead of a regex split that could not see
quoting; wrappers peeled before `argv[0]` is read; indirection (`xargs`, `eval`,
`find -exec`, command substitution, a pipe into a shell) refused rather than
guessed at; deletion and revert treated as writes (`rm`, `git rm`, `git checkout
-- P`, `git restore P`, `mv`'s source); `git push` refspecs refused at this door
as they already were in `mcp/vcs.py`. The bypass corpus ships as a parametrized
test.

Pattern matching is case-folded on both sides. Every shipped pattern is
lowercase, `fnmatch` on POSIX is case-sensitive, and macOS is not — so
`api/app/Auth.py` named the file `*auth*` exists to protect and matched nothing.

`python foo.py` and `python -m pytest` stay allowed, deliberately: refusing them
refuses how FIXER and VERIFIER run the suite.

### FIXER could rewrite the test that defines success

`policy.protected_paths` was set by no agent YAML and by nothing else, so
`Guardrail._protected_path` always returned False and §10's symptom-fix guard was
a sentence in a prompt. Meanwhile `fixer.yaml` grants `qa/repro` — REPRODUCER's
sandbox, where the failing test lives. The router now names that ticket's test on
a per-invocation copy of the spec.

### SYNTHESIZER: the defect no single agent could see

A new `synthesis` layer between discover and reproduce. Each discovery agent is
its own process with its own context, which is what makes each of them good — and
a defect whose proof spans two surfaces therefore arrived as two findings, each
correctly judged minor by an agent that could only see its half. Nothing joined
them, `fingerprint()` leads with `domain` so the halves were guaranteed to hash
differently, and four prompts told agents to report the half they could evidence
and drop the other.

SYNTHESIZER reads every envelope and asks whether two of them describe one defect
worse than either. It confirms the join by opening both files before asserting
it, and it emits before reproduce, so a composite earns a failing test and a
ticket like any other finding. In `nightly` and `full-loop`; not in `pr-check`.
No `must_call`: most runs contain no conjunction, and an agent required to emit
will assemble one wearing a severity higher than either of its parts.

The four prompts now ask for the cross-surface fact and the file it was read in,
so the halves share a join key instead of a note in prose.

### Runs now learn from each other

A new `outcomes` table records what became of each finding — `verified`,
`not_fixed`, `regressed`, `review_rejected`, `held`, `not_reproducible` — and
`search_similar` renders it beside the match. All of this was already known
*inside* a run and lost at the end of it.

Every write is Python's, from outside an agent's turn: `router._record_outcomes`
before `run_finished`, `cli._persist_score` in a process with no agent in it. An
agent that can write its own outcome can raise its own apparent precision without
finding anything. Automatic confidence-threshold feedback is deliberately **not**
built for the same reason — measure it, print it, let a human write
`overrides.yaml`.

**The regression loop was unreachable in every shipped roster.** REGRESSION fires
only on `resolved_at`, whose only writer was the `mark_resolved` tool — and
VERIFIER, the one agent that closes a ticket, has no `defect_memory` server. The
router writes it now.

`memory.db` is partitioned by target. It was one file per state root with an
unfiltered `SELECT * FROM defects`, and `qaas run --repo` puts every clone under
that root — so a close-enough match from an unrelated codebase came back as
"already tracked as PROJ-N, do not file again". Existing databases migrate in
place and keep their rows visible.

`qaas score` now reports precision, false positives and severity agreement **per
agent**, and persists each scoring to `.qaas/scores/<run_id>.json`.

### The fix loop carried no feedback

`record_review` refuses REQUEST_CHANGES without `concerns`, on the grounds that
FIXER gets them verbatim — and nothing carried them, so round two dispatched
FIXER with a byte-identical prompt. `max_mender_arbiter_round_trips: 2` bought a
second attempt at the same coin flip.

`_latest_verdict` and `_latest_review` are also scoped to the dispatch that
should have produced them, the way `_branch_written_since` already was.
Unscoped, a VERIFIER that finished without recording a verdict — an ordinary
path, since the Stop hook lets an agent through after one block — silently
inherited the previous one. On a resumed run that is a VERIFIED nobody verified.

### Other loop and robustness fixes

- **A hung agent could outlive the run's own clock.** `Budget.check()` only ran
  *between* dispatches, so `max_wall_clock_s` bounded the gaps and nothing else.
  Each dispatch is now bounded by the run's remaining time.
- **A run that ran out of time filed nothing.** `BudgetExceeded` unwound past
  file, verify and report, leaving envelopes on disk and no ticket. `RunMode.
  reserve_fraction` (default 0.15) holds back enough clock to turn work already
  paid for into tickets.
- **A resumed run re-filed every ticket.** `_phase_file` now skips findings that
  already carry a `jira.key`, and says how many.
- **A crashed agent's spend was recorded as $0**, so the governor was told
  nothing had been spent. A conservative estimate is recorded instead, flagged
  `cost_estimated` so it is never presented as measured.
- **Envelopes were credited to whichever agent finished after them.** The
  per-agent diff now filters on `discovered_by`.
- **REPRODUCER ran two-wide over one working tree.** Separate contexts, but the
  same `target_root`, the same branches and the same compose stack. Now serial.
- **One malformed ledger line made a whole run unreadable.** `store.ledger()`
  skips and counts unparseable lines; a killed run is exactly when the audit
  trail matters.
- **Three dispatch paths had no `budget.check()`** despite the docstring saying
  every one did.
- The held-envelope nudge reads the tool text as well as `structuredContent`,
  which does not reliably survive the SDK — so it fires while the agent still
  has turns to attach the evidence.
- `.env.example` claimed security findings fall back to `JIRA_PROJECT_KEY`. They
  are refused instead; filing one publicly has no undo.

### Reproduction now has a severity floor

`thresholds.reproduce_min_severity` (default `major`) decides which findings are
worth a REPRODUCER context. Reproduction is the one phase whose cost scales with
*findings* rather than agents, and each dispatch is a fresh frontier-model
context: a nightly run that found 85 issues, most of them minor, spent $45 there
and filed nothing.

That number is worth reading closely, because it says where each control
applies. `max_findings_per_agent_run` had already held that run to **25**
dispatches — 25 × ~$1.75 is the $45 — and the cap has always taken the most
severe findings first. So on a run that exceeds the cap, the cap is the lever and
this floor changes little; lower the cap if that is the run you have. The floor
is for the ordinary run that never reaches it, which is every run in this
project's own history: across 22 of them it takes reproduction from 132
dispatches to 98, roughly $231 to $172.

What that buys is measurable in the same 22 runs. Of the 15 findings REPRODUCER
actually took, 14 produced a committed failing test and 13 came back with their
confidence raised. Two were falsified — including a `critical` "read-only viewer
can create and place orders" that went 0.90 → 0.10 and would otherwise have been
filed. A floor of `major` still catches that one. The one it gives up is a
`minor` accessibility false positive.

A finding below the floor is **still filed**, on the evidence discovery already
produced: `is_fileable` asks for evidence, confidence and "not
not_reproducible", and `unattempted` passes all three. What it does not get is a
committed failing test — the right thing to spend a context on for a blocker and
the wrong thing for a trivium.

This cannot move `qaas score`: the scorecard reads envelopes and REPRODUCER
emits none. It trades reproduction depth for cost, and nothing else.

**Behaviour change.** Releases before 0.0.2 reproduced everything. One line
restores that:

```yaml
thresholds:
  reproduce_min_severity: trivial
```

The fan-out escalation now leads the escalation list and names the count and the
cap, rather than arriving third in a list of sixteen — it is the line that
explains the bill.

### Saying what it actually is

Every document now leads with the same sentence, because "built on the Claude
Agent SDK" sounded like an imported package and undersold the thing by a mile:

> qaas is not a program that calls an API. It is a harness around Claude Code
> itself. Fifteen real Claude Code sessions, each with its own context, its own
> tool allowlist, its own budget, run in a phase order by a Python state machine
> that can refuse any tool call any of them makes.

It is also literally how it works: the SDK resolves `shutil.which("claude")` and
spawns that binary once per agent invocation. The context boundary, the tool
allowlist and the per-agent cost number are separate operating-system processes,
not bookkeeping — which is the difference between a claim and a fact.

The README tagline and the PyPI description are the short form: *a harness that
runs fifteen governed Claude Code sessions over your repo*.

### The board can start the work

```bash
qaas run --mode fix-cycle --from-board "Ready for Fix"
```

Every ticket carrying this repository's label that currently sits in that status
becomes the run's work list. It finds the run that produced each finding, so
nobody has to know a run id. Drag a card into the column, and the next run picks
it up; on a timer, "drag a card and an agent starts work" is literally true.

This is the one thing in the system driven *by* the board rather than recorded
on it, and the limit is deliberate. It is a pull, not a subscription: the board
chooses the **work**, and ROUTER still schedules everything after that out of
the ledger. Fifteen agents polling a rate-limited API for the length of a run
would buy nothing, and the budget governor and loop breakers have to live in
code regardless.

The status is your project's own word for it and is matched case-blind, because
"Ready for Fix" is whatever casing someone typed when they made the column. The
repo label is what scopes it — a shared Jira project holds every repository's
tickets. And the tickets resolve before `--dry-run` renders, since "what would
this pick up" is the question `--dry-run` exists to answer.

### `qaas validate` says which Claude Code binary it will run

Each agent runs as a Claude Code subprocess, so `validate` — the command whose
job is "tell me what is wrong before I spend anything" — now reports when there
is no binary to spawn, and notes when the one it found is the bundled one.

It mirrors the SDK's own resolution order, which is **bundled first, then
`PATH`**: the `claude-agent-sdk` wheel ships a `claude` executable for common
platforms and the SDK prefers it. So `pip install qaas-python` is usually the
whole install, and what a user must actually supply is authentication — signed
in to `claude` for plan quota, or `ANTHROPIC_API_KEY` to pay per token. The
difference decides what a run costs them, which is worth saying next to any
cost figure.

### Tuning is editable from the page

The configuration view's model, effort, turn cap and thresholds are now
controls rather than readouts. Changing one writes `overrides.yaml` beside your
config, and the next run sees it — `qaas run --dry-run` renders the new model
immediately.

This is the dashboard's **only** writing route, and the shape of the exception
is the point: it changes what a model *is*, never what an agent is *allowed to
do*. An agent's policy, tool list, MCP servers and `must_call` contract cannot
be set from the page at any price — they are the §8.1 write-permission matrix
the guardrails enforce, and a page reachable by anything running as this user
must not be a second, quieter door onto them. A refused field is named in the
error rather than silently dropped.

A candidate is validated through a real `load_config` in a scratch copy before
anything is written, so a value that would not load is refused rather than
discovered on the next paid run. Deleting the file undoes everything, and
"reset overrides" is that `rm`.

`overrides.yaml` is also the only **partial** config layer. Every other layer
replaces whole, which is right for forking an agent and wrong for changing one
line: the fork freezes that agent's policy and prompt on the day it was copied,
so a later fix to a `forbidden_paths` pattern never reaches you.

### The agent grid got its screen back

Fifteen cards cost a screen and a half, most of it repeating each card's layer
under its name — which the band header above it already states for every card
in it. What is left is what changes: the name, the state, and the three
numbers. The grid went from roughly a thousand pixels to three hundred and
fifty, so the timeline and the findings now sit above the fold.

### Light and dark

A toggle in the top bar, and a full light palette behind it. Three states, not
two: explicit light, explicit dark, and no choice at all — which is the default
and follows the operating system, so a page opened on a light machine is light
without anyone having to ask for it.

Getting there meant tokenising four surfaces that had been written as literal
hex inside their rules. A literal is a colour the second palette cannot reach,
which is how a page that "supports light mode" ends up with three black panels
in it; a test now fails on any opaque colour outside a palette block, and
another fails if either light palette stops redefining a token the dark one
sets. The event colours are darkened rather than reused: a refusal and a
regression have to catch the eye at the same speed against white as against
near-black.

The stored choice is read in a blocking inline script in the head. `app.js` is
a module, so it runs after first paint, and choosing the theme there showed a
frame of the wrong one on every load.

### A configuration view

`qaas dashboard` grew a second half. The runs view answers "what happened"; the
configuration view answers "what would happen" — the roster with each agent's
model, tools, skills and output contract; which prompt file is in force and
which layer it came from; the MCP servers and who may use each; models grouped
by model; the thirty skills and their qualified names; the three hook events
and what each may block; the write-permission matrix per agent; the run modes;
and the governors, each with the sentence explaining what it costs.

That is the same object graph `qaas validate`, `qaas prompts list`, `qaas
doctor` and `qaas run --dry-run` already print, in one place instead of four
terminal tables. Two questions it answers that were previously a grep: which
file defined this agent, and what that file shadows.

It stays a reader. `/api/config` is a GET, the route-table test still passes,
and there is no form on the page. A credential is reported as *whether the
variable is set* and never as its value — a profile names an environment
variable so the secret stays out of the file, and a token rendered into HTML is
a token in a browser cache.

### The dashboard reads a resumed run correctly

`qaas run --run-id <id>` appends a second `run_started` to a ledger that already
carries a `run_finished`. Three things in the read model assumed that never
happens, and together they rendered a live run as a finished one:

- **liveness** asked whether the ledger *contains* `run_finished`. It now counts
  starts against finishes, so a run is live while it has been started more times
  than it has been finished.
- **the header** kept the first session's verdict, writing "stopped early —
  wall-clock cap" across the top of a run that was actively dispatching.
- **agent cards** left an agent the first session never reached marked
  `never_ran`, which is the one thing the resume exists to change. They are
  queued again.

Separately, an agent's finding count counted `envelope` *lines* rather than
envelopes. An envelope is re-logged when a later phase revises it — REPRODUCER
raising a held finding's confidence writes a second line naming the same id and
the same discovering agent — so one live run showed 21 findings against 17 real
envelopes. The count is now taken where the deduplication already happens.

### `qaas dashboard`

A localhost page that reads a run's ledger and shows it as instrumentation: the
phase rail advancing, one card per agent with its cost, turn count and the tool
it is calling right now, a timeline lane each, findings by severity with their
evidence, and every guardrail refusal as it fires. `qaas run --dashboard` starts
it before the first agent dispatches.

Read-only by construction — every route is a GET, and a test asserts the route
table contains nothing else. It adds no ledger kind and no router change: the
phase is derived from each agent's layer, which is what lets it open runs
recorded long before it existed, including ones naming a retired roster.

Web dependencies are an optional extra:

```bash
pip install 'qaas-python[ui]'
```

All three (`starlette`, `uvicorn`, `sse-starlette`) already arrive with the
Claude Agent SDK, so it installs nothing new in practice — the extra pins what
the UI imports. The read model imports none of them and stays in the offline
suite.

## 0.0.1 — first release

`qaas` — a multi-agent QA system built on the Claude Agent SDK. Sixteen roles:
ROUTER is the Python state machine, and fifteen agents map a codebase, find
defects, reproduce them into failing tests, file tickets, fix them and prove the
fix.

- **MAPPER** builds the system map everything else reads.
- **ARCHITECT, API, BROWSER, DBA, AUDITOR, SOCKET, GUIDE, LOAD** are the
  discovery layer, run concurrently up to the mode's cap.
- **REPRODUCER** turns a finding into a committed failing test; **TRIAGE**
  dedupes, scores and files.
- **FIXER → REVIEWER → VERIFIER** is the bounded fix loop; **REPORTER** writes
  up the run.

Two decisions shape everything: the orchestrator is Python rather than a prompt,
because a model cannot enforce a budget it is itself spending; and each agent is
its own top-level `query()`, which is what gives a real context boundary, a
per-agent tool allowlist and a per-agent cost number.

767 tests, offline and free by default — no API key, no network, no Docker.

### Enforcement, in code rather than in prompts

The §8.1/§8.2 write-permission matrix is enforced by one function,
`Guardrail._check_path`, reached from all three doors: `Write`/`Edit` through
`check()`, shell commands through `_check_bash`, and the `vcs` MCP server. A
command that mutates and whose destination cannot be resolved is refused, not
guessed at. Shell writes count against the diff budget. Denials return a reason
and land in the run ledger rather than killing the turn.

Everything an agent supplies that becomes argv is treated as a flag until proven
otherwise: a branch name may not be a refspec, a ref may not be an option, and a
path-shaped test selector is contained inside the target root before pytest sees
it. Spec references are read from inside the checkout or fetched from the
application under test, and nowhere else.
