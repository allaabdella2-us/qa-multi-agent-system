# Changelog

## 0.0.2

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
