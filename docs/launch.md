# Launching qaas — the post, the honest verdicts, and the answers

Everything needed to announce `qaas-python` publicly, in one place. Every number
below is traced to its source in the last section; nothing here is a guess.

**Before you post — a checklist**

- [ ] 0.0.2 is on PyPI and `pip install qaas-python` installs it (the post quotes the command).
- [ ] The Firestore finding on your own site is **fixed** before you describe it in public. Naming an unauthenticated-write rule on a live site is a disclosure. Either fix it first or describe it as "a database rule that accepted unauthenticated writes" with no site named.
- [ ] Do **not** name the plaintext-password finding from your site. It is your repository; it does not need to be in a launch post.
- [ ] Take the Jira board screenshot (§3) *before* the run that produces it is torn down.
- [ ] Links go in the first comment, not the body. LinkedIn downranks posts whose body carries an outbound link.

---

## 1. The post

House rules that apply to every variant: no "revolutionary", no "10x", no
"game-changer". Every number has a source. The roadmap is "help wanted", never
"coming soon". At most five hashtags. The first comment carries the links.

### Variant A — the story (recommended)

> The tickets moving is not a picture of the system working. It *is* the system working.
>
> I've open-sourced qaas: a harness that lets a fleet of governed agents run the entire QA lifecycle on their own.
>
> Fifteen agents. One maps your application. Eight go looking for defects: API, browser, database, security, architecture, load. One reproduces each finding into a failing test. One files it. One fixes it, one reviews the fix, one verifies it, and the ticket moves To Do → In Progress → Done on the Jira board your team already reads.
>
> They never message each other. The board is the work queue. The orchestrator is ordinary Python, because a model cannot enforce a budget it is itself spending.
>
> What it is not: a test generator, and not a merge bot. No merge method exists anywhere in the code. Shipping stays a human decision.
>
> Some numbers, all read back from the run ledger:
> – 14 of 15 reproductions produced a committed failing test
> – 2 confident findings, one rated critical at 0.90, were falsified before anything was filed
> – on a real React site: 17 findings for about $20, including a database rule that accepted unauthenticated writes. Discovery held it at 0.45 confidence. Reproduction took it to 0.97.
>
> Today it runs on the Claude Agent SDK. Making the model a choice (OpenAI, OpenRouter, Ollama) is the roadmap, and where I'd most like help.
>
> pip install qaas-python
>
> Point it at a repository you know well and tell me what it got wrong. False positives are the most useful bug report this project can receive.
>
> #AIAgents #SoftwareTesting #QA #OpenSource #Python

**First comment:**

> PyPI: https://pypi.org/project/qaas-python/
> Code and architecture: https://github.com/allaabdella2-us/qa-multi-agent-system
> 861 tests run offline with no API key, so you can read how the guardrails work before spending anything.

### Variant B — the builder's post (engineering audience)

> The design decision that shaped everything in qaas: the orchestrator is code, not a prompt. A model cannot enforce a budget it is itself spending.
>
> qaas is an open-source harness that runs fifteen QA agents through map → discover → reproduce → file → fix → review → verify → report, and the router that orders them, caps their concurrency, governs the budget and breaks the loops is a Python state machine. That is also why 861 tests run offline, free, with no API key.
>
> Three things are enforced in code rather than requested in a prompt:
>
> 1. Every tool call passes a PreToolUse hook. Writes are checked against that agent's path allowlist; git writes against its branch patterns. A denial returns a reason and is logged. It never kills the turn.
> 2. A finding cannot be filed without evidence. has_evidence() and is_fileable() are methods on the envelope model. An agent cannot talk its way past them.
> 3. No merge path exists. Not disabled. Absent. Pull requests open as drafts and a human ships.
>
> Every agent is its own top-level query(), not a subagent of a shared parent, so each gets a real context boundary, its own tool allowlist and its own cost number. Agents never message each other; a finding becomes a ticket and the ticket is the shared state.
>
> It ships with a deliberately buggy app and a golden ledger of 16 seeded defects plus 4 planted non-defects, so a prompt change has a recall and precision number attached rather than an opinion.
>
> Measured so far: 14 of 15 reproductions produced a committed failing test; 2 confident findings were falsified before filing; a pr-check on a real React site found 17 defects for about $20.
>
> It runs on the Claude Agent SDK today. The provider seam is about forty lines, and the plan for OpenAI, OpenRouter and Ollama behind it is written down in the repo. That is the contribution I most want. An agent is a prompt plus a YAML file and no Python, so a new agent is the other easy first PR.
>
> pip install qaas-python
>
> #AIAgents #SoftwareTesting #QA #OpenSource #Python

### Variant C — short (repost or comment thread)

> Open-sourced qaas: fifteen governed agents that find defects in a running application, reproduce each one into a failing test, file it, fix it, verify the fix, and move the ticket to Done on your Jira board. The orchestrator is Python, not a prompt. No merge method exists. 861 tests run without an API key.
>
> Real numbers: 14 of 15 reproductions became failing tests; a real React site yielded 17 findings for about $20.
>
> pip install qaas-python — and tell me what it got wrong.

---

## 2. Is "harness" the honest word?

**Verdict: yes, with one caveat you should say out loud.**

A harness is the thing that holds the workers. It routes them, limits them,
refuses them and records them. That is precisely what the product is:

| harness function | where it lives |
|---|---|
| ordering, concurrency, budget, loop breakers | `router.py`, a Python state machine |
| refusal of writes, branches, merges | `guardrails.py`, a `PreToolUse` hook |
| what may be filed | `has_evidence()` / `is_fileable()` on the envelope model |
| the record | `ledger.jsonl`, 28 event kinds, append-only |

The agents are replaceable data: a prompt plus a YAML file. DBA and AUDITOR were
added without touching the router, the runner, the registry or the guardrails.
The harness is the product; the agents are what it holds.

The alternatives are worse:

- **"Framework"** overclaims generality. Nothing is designed to be extended in Python.
- **"Platform"** overclaims scale and hosting. It is a CLI on one machine.
- **"AI QA team"** is anthropomorphic and invites the "it's just prompts" reply.
- **"QA copilot"** is wrong in kind. A copilot sits beside a person; this runs unattended for thirty minutes.
- **"Tool"** underclaims. A tool does one thing; this holds fifteen things.

**The caveat.** "Harness" usually implies the thing inside is swappable. Today the
model is not: it runs on the Claude Agent SDK only. So the accurate phrase is
*a harness whose first provider is Claude*, and the provider seam in the roadmap
is the work that makes the word fully earned. Say it plainly in the post. Hiding
it is the one thing that would make a reader distrust the rest.

---

## 3. Screenshots — three, in this order

**1. `docs/dashboard.png` — the live run.** This is the image that stops the
scroll. It is the only one that shows many agents at once with a cost each, two
denials, one escalation, a blocker with a Jira key and the ledger streaming. Keep
the right-hand ledger column in the crop: the `TRIAGE ESCALATION` line reading
"security finding needs JIRA_SECURITY_PROJECT_KEY; filed to the open project
instead" is the governance story in one line.

**2. `docs/roster.png` — the fifteen by phase.** Answers "what are the agents"
without a paragraph, and matches the post's one-line tour of the roster.

**3. A Jira board with qaas tickets on it — you need to take this one.** Nothing
in `docs/` shows it today, and it is the picture of the headline claim: tickets
in To Do / In Progress / Done, with the `repo-<target>` and `qaas-fp-…` labels
visible on a card. Take it during a Jira-backed run, not after. If you cannot
get one before posting, use `docs/architecture-loops.png` instead: the discovery
loop and the remediation loop meeting at the ticket makes the same point as a
diagram.

Not recommended: a terminal capture of `qaas trace` (already in the README, and
it reads as a log rather than a system), and the logo (you removed it from the
README deliberately; the post should match).

---

## 4. "Why not just use Claude Code's code review, or prompt an LLM on the whole repo?"

**The one-paragraph answer.** They answer a different question. Code review asks
"is this diff good?" and reads code. qaas asks "does this *running* application
have defects, and are they real?" and exercises it: it calls the API, drives the
browser, reads the database, then tries to reproduce what it found into a failing
test before it is allowed to file. The evidence rule is a method on the data
model, not a request in a prompt, and that is why two findings a reviewer-style
pass believed at 0.90 confidence were thrown out before anyone saw them. It is
bounded by a budget governor and per-agent caps, so the cost is a number per
agent rather than one opaque bill. And it closes the loop: it files, fixes,
reviews the fix, verifies it and moves the ticket, stopping exactly where a
human should decide. Most importantly it is scored: a golden ledger gives every
prompt change a recall and precision number. A review prompt has nothing to be
scored against.

**The honest converse.** For a single pull request, `/code-review` is faster and
cheaper, and you should use it. qaas is for the nightly or pre-release sweep of
a system that is running, not for the inline diff. They are complements.

| | one big LLM prompt | Claude Code review | qaas |
|---|---|---|---|
| Reads the code | yes | yes | yes |
| Exercises the running app (API, browser, DB) | no | no | yes |
| Evidence required before a finding counts | asked for | asked for | enforced in code |
| Reproduces the finding into a failing test | no | no | yes, per finding |
| Cost bounded per agent and per run | no | per invocation | yes, governor and caps |
| Files, fixes, reviews the fix, verifies | no | comments only | yes |
| Moves the ticket on your board | no | no | yes |
| Can merge | n/a | no | no method exists |
| Scored against a golden ledger | no | no | yes, `qaas score` |
| Right for a single PR | sometimes | yes | no, too heavy |

---

## 5. Questions people will ask, and the answers

### What it is

**How is this different from tools that generate tests?**
Those write tests for code that presumably works. qaas looks for code that does
not, reproduces the defect into a failing test, and files it with evidence. The
test is the proof of a bug, not coverage.

**Does it replace QA engineers?**
No. It replaces the first pass and the ticket-writing. The judgment calls stay
human: what is worth fixing, what ships, what merges. Nothing in the system can
merge, by construction.

**What does "governed" mean?**
Every tool call passes a hook in Python before it runs. Writes are checked
against the agent's allowed paths, git writes against its branch patterns,
and a class of changes (auth, payments, secrets, migrations, infrastructure,
CI) stops at a human however small it looks. A denial returns a reason and is
logged; the agent reads it and works around it.

**Why fifteen agents instead of one?**
Each agent is its own context, its own tool allowlist and its own cost number.
One agent with every tool cannot be told "you may read but never write", and a
single context cannot hold a whole application. The number is not the point;
the boundaries are.

**Why do the agents never talk to each other?**
Because a message between two models is not on the record and not inspectable.
A finding becomes a structured envelope, the envelope becomes a ticket, and the
ticket is the shared state, on the board your team already reads. When a ticket
moves from To Do to In Progress, an agent picked it up. That is the whole
protocol, and a person can watch it.

### Cost and safety

**What does a run cost?**
Two real data points. A pr-check on the bundled demo app: 8 agents, 3 findings,
2 filed, $7.85. A pr-check on a real React marketing site: 6 discovery agents,
17 findings, $19.64 for the discovery pass, and $8 more for the reproduce-and-
file continuation. Cost is per agent in the ledger, so you can see where it
went.

**Why is reproduction the expensive part, and can I turn it down?**
It runs once per finding in a fresh context, so its cost scales with what was
found, not with how many agents ran. Since 0.0.2 only findings at or above a
severity floor (default `major`) are reproduced; the rest are filed on the
evidence discovery already produced. In replay across 22 runs that took
reproduction from 132 dispatches to 98, roughly $231 to $172, and would still
have caught the falsified critical finding.

**Can it touch production?**
Only if you point it there, and even then `external` mode means read and
exercise, never reset. `none` mode is static reads only and is where most first
runs on a real repository start. `compose` mode, where it owns the lifecycle, is
for an environment it started itself.

**Can it merge?**
No. There is no merge method in the code. `gh pr merge` is refused by the
guardrail. Pull requests open as drafts.

**Can a repository it inspects attack it?**
It loads no settings, hooks or MCP servers from the target's filesystem
(`setting_sources=[]`). A cloned repository cannot inject anything into the
process holding your credentials. Credentials themselves are never in a profile;
profiles name environment variables.

**Is it safe to let it write to my Jira?**
Every ticket carries a repository label and a defect fingerprint, stamped in
code rather than asked of a model, so the next run recognises a filed defect
instead of filing it again. Security findings are refused unless a separate
security project is configured, because a vulnerability in a project the whole
company can read is a disclosure with no undo. `qaas tracker-check` validates
credentials and prints the exact JSON it would send, and creates nothing.

### Accuracy

**How do you know it is not spraying false positives?**
The golden ledger plants four correct-but-suspicious pieces of code alongside
sixteen seeded defects. `qaas score` reports precision, not just recall. And
reproduction is a second opinion in a fresh context: of the 15 findings it took
in this project's own history, 14 produced a committed failing test and 2 were
falsified, including a "read-only viewer can create orders" rated critical at
0.90 that dropped to 0.10.

**How did it do on real code, not the demo?**
On a real React site, pr-check mode: 17 findings across API, browser, database,
security and architecture. Reproduction was attempted on the eight rated major
or above and all eight reproduced. The best example is a database rule that
accepted unauthenticated writes: discovery emitted it at 0.45 confidence, which
is below the filing threshold, so it was held. Reproduction confirmed it and the
envelope now stands at 0.97. The system declined to file its own scariest
finding until it had proof.

**Seeded bugs are easier than real ones. Isn't the benchmark flattering?**
Yes, and the README says so in a warning block. The benchmark shows the loop
works end to end and does not spray false positives. It does not show it will
find the hard bug in your codebase. Run it on something you know and judge it on
that.

### Practical

**Do I need Jira?**
No. The default tracker is `local`: tickets are written as JSON so you can read
what would have been filed. Set `QAAS_TRACKER=jira` per shell when you want the
real thing.

**Do I need Docker?**
No. `none` mode reads code, schema and spec with nothing running. Docker is for
the bundled demo app and for `compose` mode.

**Which models does it use?**
Model is per-agent configuration (`model:` in each agent's YAML). Today every
provider is Claude through the Claude Agent SDK. Auth is the Claude Code CLI if
you are signed in, otherwise `ANTHROPIC_API_KEY`.

**Can I use OpenAI, Gemini, OpenRouter or Ollama?**
Not yet. See §7 for exactly what is planned and why.

**Can I change what an agent does?**
`qaas prompts eject API` copies the prompt into your project and you edit it.
Better: drop an `API.append.md` beside it with your house rules, and keep
receiving the base prompt's improvements.

### Contributing

**What is a good first PR?**
A new agent: a prompt plus a YAML file, no Python. A new skill. Or run it on a
repository you know well and file what it got wrong; a false positive is the
most useful bug report the project can receive. The biggest ask is a provider
implementation, and the plan for it is written down in `PROVIDERS_PLAN.md`.

---

## 6. Critiques to expect, and the honest reply to each

**"It's just prompts."**
The prompts are the least of it. The router, the guardrails, the evidence gate
and the ledger are Python, and 861 tests exercise them without a model in the
loop. Ask which part of a competitor's system still works with the API key
removed.

**"Calibrated on toy bugs."**
True, and the README says so in a caution block. The counter-evidence is the run
on a real site, and the standing request is: run it on yours and report what it
got wrong.

**"$20 a run is expensive."**
It is per run, not per finding, and the ledger shows which agent spent what. Set
it against an hour of a QA engineer's time for seventeen written-up findings.
The severity floor cut reproduction cost by about a quarter in replay, and local
models for the cheap agents are on the roadmap.

**"Vendor lock-in to Anthropic."**
Today, yes. The contact surface with the SDK is about forty lines, the seam is
designed, and the plan for OpenAI, OpenRouter and Ollama is in the repository.
It is the first item on the roadmap because it is the fair criticism.

**"Fifteen agents is over-engineered."**
Each agent is a context boundary, a tool allowlist and a cost number. A single
agent cannot be given "read but never write" for one phase and "write to
`fix/*` only" for another. The count follows from the boundaries, not the
other way round.

**"Agents writing to my Jira is frightening."**
Every ticket carries a fingerprint and a repository label stamped in code.
Security findings are refused without a separate project. `tracker-check`
creates nothing. And the local tracker exists precisely so you can read a run's
output before you ever connect a real board.

**"It's non-deterministic; how can you trust it?"**
It is, which is why every tool call, denial, verdict and escalation is on the
record and every run is pinned to the commit it examined. You do not trust the
model; you read the ledger.

**"Solo project, bus factor of one."**
True. That is the ask in the post.

**"Why not the OpenAI Agents SDK, or LangGraph, or CrewAI?"**
The guardrail model needs a hook that can *deny* a tool call before it runs.
The Claude Agent SDK's PreToolUse hook does that. The OpenAI Agents SDK's hooks
are observational and cannot veto, which is why the plan runs the agent loop in
qaas itself for non-Claude providers rather than adopting another framework's.

---

## 7. Providers — what to say today

Say this: **the Claude Agent SDK is the first provider, not the design.** The
envelope, the guardrails, the ledger and the phase machine are provider-neutral
already. The coupling is the SDK's options object, its hook events and its
`query()` loop, about forty lines across two files.

The written plan (`PROVIDERS_PLAN.md`), in order:

1. **A provider seam** in the runner with zero behaviour change. The Claude path
   moves behind it verbatim.
2. **OpenAI**, chat completions first because every OpenAI-compatible endpoint
   speaks it, then the responses API, which becomes the default for OpenAI's
   own models.
3. **OpenRouter** through the same implementation with a different base URL.
   One endpoint, many models, and `qaas score` as the referee for "which model
   is best at *this* agent's job".
4. **Ollama** through the same implementation, for the agents that do not need
   a frontier model. A model with no known price is refused at validate time
   unless you opt in, because a dollar governor that reads zero is decorative.
5. **A scored per-agent model matrix**, so the answer to "which model" is a
   number rather than a preference.

**Gemini** is not a separate item and should not be promised as one: it reaches
the harness through its OpenAI-compatible endpoint, the same way OpenRouter and
Ollama do. If someone asks, that is the answer.

Two constraints worth stating when asked why it was done this way rather than
by adopting another agent framework: the hooks must be able to deny a tool call
before it runs, and the same one hook implementation must serve every provider
so the guardrails cannot disagree with themselves. Give no dates.

---

## 8. Commands for the first comment

```bash
pip install 'qaas-python[ui]'
qaas init .                            # inspect the repo, write a target profile
qaas validate                          # config, prompts, allowlists — no API call
qaas run --mode pr-check --dry-run     # what every agent would receive — no API call
qaas run --mode pr-check --dashboard   # the real thing, with the live page
qaas trace <run-id> --follow --quiet   # the decisions, as they happen
qaas score                             # recall and precision against the golden ledger
qaas board                             # this repository's Jira view
qaas prompts eject API                 # make a prompt yours
```

---

## 9. Where every number comes from

| claim | source |
|---|---|
| 15 agents, ROUTER is Python, six phases | `README.md`, `CLAUDE.md` |
| 861 offline tests, no API key | README badge; 861 passing locally on 2026-09-12 |
| 16 seeded defects + 4 planted non-defects | `target-app/defects.yaml` |
| 28 ledger event kinds | `store.LedgerKind` |
| 14 of 15 reproductions → failing test; 13 raised confidence; 2 falsified, one critical 0.90 → 0.10 | `CHANGELOG.md`, 0.0.2 entry, from 22 runs in this project's `.qaas/runs/` |
| replay: 132 → 98 reproduction dispatches, ~$231 → ~$172 | same entry |
| demo pr-check: 8 agents, 3 findings, 2 filed, 2 denials, 1 escalation, $7.85 | `docs/dashboard.png` |
| real site pr-check: 17 findings, 6 discovery agents, $19.64, wall-clock cap hit at 1843 s | `run-20260912T121805-b2c052` ledger, `run_finished` |
| real site: 8 of 8 major-or-above findings reproduced; 9 below the floor filed on discovery evidence | same ledger, `reproduction` and `skipped` lines |
| Firestore blocker 0.45 → 0.97 | same ledger, `envelope` lines for `d6dfbea9…` at 12:36:34 and 13:29:22, `reproduction` at 13:29:22 |
| continuation cost $8.01; TRIAGE failed on a session limit, so no tickets were filed on that run | same ledger, second `run_finished` |
| SDK coupling ~40 lines; provider plan and rejected alternatives | `PROVIDERS_PLAN.md` |
| the dashboard is read-only, every route a GET | `tests/ui/test_server.py` |
