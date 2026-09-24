# The dashboard

`qaas dashboard` is a localhost page with two halves. The **runs** view answers
*what happened* — sixteen agents, what each spent, what it found and what it was
refused. The **configuration** view answers *what would happen* — the roster,
the prompts in force, the servers, the models, the rails and the governors.

Worth knowing before you read a card: qaas is not a program that calls an API.
It is a harness around Claude Code itself. Each agent card on this page is a
**real Claude Code session** — its own process, its own context, its own tool
allowlist, its own budget — run in a phase order by a Python state machine that
can refuse any tool call any of them makes. The cost on a card is that process's
cost. The refusals on it were denied before the tool ran.

```bash
pip install 'qaas-python[ui]'
qaas dashboard
```

It opens on `http://127.0.0.1:7777`. The web dependencies all arrive with the
Claude Agent SDK already, so in practice the extra installs nothing new; it is
there so the page pins what it imports rather than relying on another package's
dependency graph continuing to supply it.

---

## Starting it

```bash
qaas dashboard                       # the live run if there is one, else the newest
qaas dashboard <run-id>              # a particular run
qaas run --mode nightly --dashboard  # start both; the URL prints before the first dispatch
```

| flag | |
|---|---|
| `--port N` | default 7777; the next few are tried if it is taken, and it says which it took |
| `--host H` | default `127.0.0.1`; loopback only (`127.0.0.1`, `::1`, `localhost`) — anything else is refused |
| `--no-open` | do not open a browser |
| `--root PATH` | the state directory; default: the project's `.qaas/`, found by walking up |
| `--config DIR` | the config layer to read and write tuning to, exactly as `qaas run --config` |

> [!IMPORTANT]
> **Run it from the project whose runs you want.** The page reads the `.qaas/`
> of the project it is started in, found by walking up from the working
> directory the way every qaas command does — so `cd api && qaas dashboard` still
> finds the project's runs. Started inside your application's checkout it shows
> that application's runs; started inside the qaas checkout it shows the demo
> app's. Same command, different board.

> [!WARNING]
> **It binds loopback only, and refuses anything else.** The ledger carries agent
> task previews, refused command lines and your target's absolute paths. The
> page's defence against DNS rebinding is a check that the `Host` header names
> loopback — which a client on another machine controls, so on `0.0.0.0` it would
> protect nothing. `--host 0.0.0.0` therefore stops with an error rather than
> serving. To look from another machine, forward the port over SSH
> (`ssh -L 7777:127.0.0.1:7777 host`) so the page still sees loopback.

`qaas run --dashboard` starts the server in a background thread, so the page
dies when the run ends. To keep watching after a run finishes, start
`qaas dashboard` separately in its own terminal.

---

## The runs view

**The phase rail** across the top lights `map → discover → reproduce → file →
verify → report` as agents of each layer start and finish.

**Agent bands.** One band per roster layer, in pipeline order, each with its own
icon and colour, its settled count and its spend. Bands wrap: a discovery layer
of eight keeps a row to itself while triage, remediation and reporting share
one. A card shows the agent's cost, findings and refusals; a running card also
shows **the tool it is calling right now**, which is what makes sixteen
concurrent agents read as a team rather than a log.

Two cases the CLI does not surface:

- A **skipped** agent says *why*. That is the answer to "why didn't BROWSER
  run" — usually the target has no reachable UI.
- An agent named by an **older roster** is drawn as `not in this roster` rather
  than dropped, so runs recorded before a rename still open.

**The timeline** gives each agent a lane across the wall clock, with a marker
where it emitted a finding, was refused, escalated, or filed a ticket. A refusal
appears *inside* the lane of the agent that was refused.

**The tabs** below hold findings by severity, every guardrail refusal, the
tickets, escalations, artifacts and the scorecard. Clicking a finding opens it
with its evidence, its reproduction steps, and the exact reason it was held if
it was not fileable.

The **scorecard** tab scores a run against the golden ledger of the target that
is configured now — so it refuses, and says so, when the run was recorded
against a different target root (a `qaas run --repo` clone, say). Scoring that
run against the demo's `defects.yaml` would report 0% for reasons that have
nothing to do with the run.

**The ledger column** on the right streams events as they are written.
"Decisions only" hides file reads, which is what `qaas trace --quiet` does.

### Live, finished, and interrupted

A run is **live** while its ledger holds more `run_started` lines than
`run_finished` ones (a resumed run legitimately holds two of each) *and*
something is still writing it. A run that stopped without closing — the process
was killed, the machine slept for good, the terminal was closed — shows as
**interrupted**: the rail stops pulsing, the clock stops at the last entry
rather than counting on for ever, and no tail is kept open on it.

Two rules decide that:

- **Runs from 0.0.2 on close themselves.** Ctrl-c, an error, or the provider
  running out of quota all end with a `run_finished` line; an interrupt's carries
  `interrupted: true` and the exact `qaas run --run-id …` command that resumes it.
- **Older ledgers, and runs killed outright,** are judged by silence: once a
  "live" ledger has gone unwritten for longer than its run's own wall-clock cap
  plus ten minutes — eight hours when the run recorded no cap — it is
  interrupted. `qaas dashboard` with no run id opens a genuinely live run over a
  stale one, and `qaas trace --follow` stops following it and says why.

### Two numbers that are deliberate

The progress bar measures **elapsed time against `max_wall_clock_s`**, never
dollars. No shipped run mode sets a budget ceiling, so a "% of budget" bar would
be invented.

Cost is summed from the ledger's `agent_finished` lines rather than from
`results/*.json`. One run on disk has fifteen `agent_finished` lines for an
agent and a single result file, because it predates the per-invocation filename,
and the append-only ledger is the half that survived.

---

## The configuration view

Eleven sections, each a filterable list and a reading pane.

| section | what it answers |
|---|---|
| **agents** | model, effort, turn cap, tools, servers, skills, output contract, prompt |
| **prompts** | which file each agent is given, and from which layer |
| **mcp servers** | in-process, stdio and declared — and who may use each |
| **models** | model choice grouped by model rather than by agent |
| **skills** | all thirty, with the qualified name the SDK matches on |
| **hooks** | the three events, and what each one may block |
| **guardrails** | the write-permission matrix, per agent |
| **run modes** | roster, concurrency, wall clock, budget |
| **thresholds & loops** | each governor, with the sentence saying what it costs |
| **target** | environment mode, capabilities, which credential variables are set |
| **settings** | tracker, config layers, prompt and plugin directories |

Two questions it answers that were a `grep` before.

**Which file defined this agent, and what does it shadow?** Agents union by
name with the nearer layer winning, so more than one `agents/fixer.yaml` can
exist and only the first is in force. The agent's detail pane shows the path
that won with a layer badge, and names any file it shadows. That is the answer
to "I edited the YAML and nothing changed".

**Is this credential set?** The settings section lists each variable the active
tracker reads and whether it is present. It never shows a value — a profile
names an environment variable precisely so the secret stays out of the file, and
a token rendered into a page is a token in a browser cache and in every
screenshot of it.

---

## Editing tuning

Model, effort, turn cap and every threshold are editable. Changing one writes
`overrides.yaml` into the nearest *project* config layer — the one
`load_config` reads it back from, including a `--config` directory — and the
next run sees it:

```console
$ qaas run --mode fix-cycle --dry-run
  FIXER          claude-opus-4-7    effort=high    turns<=80
```

**"Reset overrides"** under the section nav deletes the file, and every value
returns to what the packaged config and any `agents/*.yaml` override say.

### What is editable, and what is not

| editable | not editable |
|---|---|
| `model` | `policy` — write paths, branches, forbidden classes |
| `effort` | `mcp_servers` |
| `max_turns` | `builtin_tools` |
| `max_budget_usd` | `skills` |
| `enabled` | `must_call` |
| every `thresholds:` value | |

The right-hand column is the §8.1 write-permission matrix, and the separation is
the whole reason this page is allowed to write at all. A page on loopback is
reachable by anything running as your user; it may change *which model an agent
runs*, and it must not be able to widen *what that agent may write*. Attempting
one returns a 400 naming the field rather than silently dropping it, and a test
drives four policy-widening payloads at the route and asserts the agent's write
paths are untouched afterwards.

Edit those in the config file, where a human reads a diff.

### `overrides.yaml` is the only partial layer

Every other config layer replaces whole. Dropping an `agents/fixer.yaml` into
`.qaas/config/agents/` shadows the packaged file **entirely** — which is right
when you are forking an agent, and wrong when you want to change one line,
because the fork freezes that agent's policy, prompt and tool list on the day
you copied it. A later fix to a `forbidden_paths` pattern never reaches you.

So this one merges, and merges narrowly:

```yaml
# .qaas/config/overrides.yaml
agents:
  FIXER:
    model: claude-opus-4-7
  API:
    model: claude-sonnet-5
thresholds:
  min_confidence_to_file: 0.75
  reproduce_min_severity: critical
```

It is an ordinary file. Write it by hand, commit it, or delete it — the page is
a convenience over it, not a second authority. A field outside the tunable set
is ignored however it is written, including by hand.

A change is validated through a real `load_config` in a scratch copy **before**
anything lands, so a value that would not load is refused now rather than found
on the next paid run. The file is replaced atomically, an unknown agent name is
refused, and a hand-edited `overrides.yaml` that no longer parses is reported
(naming the file) rather than turning every save into a server error — "Reset
overrides" still works to recover from it.

It is **never** written into the installed package. With no project config
directory yet (`pip install` then `qaas dashboard` in a directory holding only
`.qaas/runs/`), the page creates `.qaas/config/` and writes there, rather than
into `site-packages` — which would have changed every project using that
environment, or failed outright on a read-only install.

---

## Light and dark

The button at the right of the top bar. Three states, not two: explicit light,
explicit dark, and no choice at all — the default, which follows your operating
system. The choice is stored per browser and survives a reload.

---

## What it cannot do

The dashboard has exactly **one** route that writes, and it writes
`overrides.yaml`. There is no path from the page to a dispatch, a ticket, a
branch or your target's files. `test_only_the_override_route_writes` holds that
line: every route is a GET except the one named in `WRITE_ROUTES`, and adding a
second is an architectural change rather than a feature.

Ctrl-c stops the view and never the run, exactly like `qaas trace --follow`.

---

## Troubleshooting

**The page is blank, or an old version.** Browsers cache the module script.
Hard-reload with Cmd+Shift+R (Ctrl+Shift+R on Windows and Linux).

**It shows the wrong project's runs.** It reads the `.qaas/` of the project it
was started in, walking up from the working directory. Start it from inside the
project you want, or pass `--root <project>/.qaas`.

**`--host` is refused.** Only loopback is served; see the warning under
*Starting it*. Forward the port over SSH instead.

**A run shows as interrupted.** It stopped without writing `run_finished` —
killed, or run before 0.0.2 by a process that was stopped. Resume it with
`qaas run --mode <mode> --run-id <id>`; agents that already finished are not
re-run.

**The dashboard died when the run ended.** `qaas run --dashboard` serves from a
background thread that ends with the run. Start `qaas dashboard` separately to
keep watching.

**`qaas dashboard` says the extra is missing.**

```bash
pip install 'qaas-python[ui]'
```

Quote it. In zsh, square brackets are a glob pattern and an unquoted
`qaas-python[ui]` fails with `no matches found` before pip ever sees it.

**A run says "stopped early" but agents are still working.** Fixed in 0.0.2. A
resumed run appends a second `run_started`, and the old check asked whether the
ledger *contained* `run_finished` rather than counting starts against finishes.
Upgrade.
