"""The task each agent is given: the user turn, distinct from its system prompt.

The system prompt says who an agent is and what its standards are; that is
stable across every run and lives in `prompts/`. The task says what to do this
time — which application, which environment, which findings — and is built here
from the target profile.

Nothing in this module may name a specific application. A prompt that mentions
one repository's directory layout or one app's seeded users works exactly once.
"""

from __future__ import annotations

from qaas.config import AgentSpec, SystemConfig
from qaas.envelope import DefectEnvelope
from qaas.target import TargetProfile


def _profile(config: SystemConfig) -> TargetProfile:
    if config.profile is None:
        raise ValueError(
            "no target profile loaded. Point `target:` in system.yaml at a file "
            "in config/targets/, or create one with `qaas init <path-to-repo>`."
        )
    return config.profile


def _where(config: SystemConfig) -> str:
    """How to name the application under test to an agent.

    The resolved path, not `profile.root`. An agent's process cwd *is* the
    target root, and `root:` is spelled relative to the qaas project -- so
    telling a run "the application at `target-app`" sent it looking for
    `<target>/target-app`, a directory that does not exist. Resolved, the
    sentence is true from wherever the agent happens to be standing.
    """
    return str(config.target_root())


def _environment_brief(profile: TargetProfile) -> str:
    """What an agent can and cannot do to the running application."""
    env = profile.environment
    if env.mode == "none":
        return (
            "There is no running instance of this application available. Work "
            "statically: read the code, the schema, the spec and the tests. Say so "
            "plainly in any finding you cannot execute — a defect you reasoned to "
            "but did not observe is a weaker claim, and its confidence should show "
            "that."
        )

    lines = []
    if env.api_url:
        lines.append(f"API: {env.api_url}")
    if env.web_url:
        lines.append(f"web: {env.web_url}")
    where = "; ".join(lines)

    if env.mode == "external":
        return (
            f"The application is already running ({where}) and this system does not "
            "own it. You may read it and exercise it. You may NOT reset it, reseed "
            "it, or destroy state — someone else may be relying on it. Prefer "
            "read-only calls, and never send a request whose side effect you would "
            "not want to explain."
        )
    return (
        f"Bring the application up with `env_control` ({where}). You own this "
        "environment: seed it, reset it between journeys, and tear it down when "
        "done. Reset between independent checks so one test's leftovers are not "
        "the next one's bug."
    )


def _auth_brief(profile: TargetProfile) -> str:
    auth = profile.auth
    if auth.mode == "none":
        return "The application needs no authentication."
    if auth.mode == "token":
        return (
            f"Authenticate with the bearer token in ${auth.token_env}. "
            "`env_control.impersonate` will hand it to you."
        )
    roles = "\n".join(
        f"  - `{name}` ({r.username}){': ' + r.description if r.description else ''}"
        for name, r in auth.roles.items()
    )
    return (
        f"Sign in via `{auth.login_endpoint}`. `env_control.impersonate(role)` returns "
        f"a token for any of these accounts:\n{roles}\n"
        "Use more than one. A permission defect is invisible from a single role."
    )


def mapper(config: SystemConfig) -> str:
    p = _profile(config)
    spec = (
        f"`{p.layout.spec}` is the declared API contract — read it for the intended "
        "surface, but record what the code actually implements, and put any "
        "disagreement in `drift`."
        if p.layout.spec
        else "There is no API specification in this repository. Record the surface "
        "the code actually exposes, and note the absence of a spec in `gaps`."
    )
    ownership = (
        f"`{p.layout.ownership}` is the ownership source."
        if p.layout.ownership
        else "There is no CODEOWNERS file. Leave ownership null rather than guessing "
        "a team from a directory name — a misrouted ticket is worse than an "
        "unassigned one."
    )
    return f"""Map the application at `{_where(config)}` — your working directory.

{p.description.strip() or "No description was supplied; work it out from the code."}

Known layout — {p.layout.described()}. Treat that as a starting point, not an
inventory: verify it and record what is actually there.

{spec}

{ownership}

Ignore these directories entirely: {', '.join(p.layout.exclude)}.

Explore with Read, Grep and Glob, then publish one complete map with
`put_system_map`. Get the shape of the repository before reading any single file
closely. Breadth first: every route and every table matters more than a deep
read of any one handler.

Publish the map once, when it is complete."""


def api(config: SystemConfig, mode: str) -> str:
    p = _profile(config)
    spec_line = (
        f"Compare the implementation against `{p.layout.spec}` with `diff_openapi`, "
        "and judge each difference by consumer impact with `classify_breaking`."
        if p.layout.spec
        else "There is no specification to diff against, so the contract is implicit. "
        "Judge each endpoint against what its own code promises and what its "
        "consumers assume: look for handlers that disagree with each other."
    )
    prove = (
        "Prove what you report. Call the endpoint and capture the real request and "
        "response. A finding you have not observed is a hypothesis, not a defect, "
        "and its confidence should say so."
        if p.environment.is_reachable
        else "You cannot call this API, so every finding is a reading of the code. "
        "Quote the lines that support it, and keep confidence honest about the "
        "fact that you did not observe the behaviour."
    )
    filing = (
        "This is a diagnostic run: report what you find, but nothing will be filed."
        if mode == "incident"
        else "Findings that survive reproduction become tickets. Hold yourself to that bar."
    )
    return f"""Audit the API of the application at `{_where(config)}` — your working directory.

Start with `get_system_map` for the route inventory. Do not rediscover it.

{_environment_brief(p)}

{_auth_brief(p)}

Then work the surface systematically. For each endpoint: does the implementation
match what is declared? Who is allowed to call it, and does the code actually
check that? What happens on the error paths, and with a large or hostile input?

{spec_line}

The structural questions a tool can answer for you. The ones it cannot — is this
endpoint scoped to the caller's tenant, is this check the right check — need you
to read the handler and compare it against its neighbours. Endpoints in the same
file that disagree with each other are where the defects are.

{prove}

{filing}"""


def browser(config: SystemConfig, mode: str) -> str:
    p = _profile(config)
    if not p.environment.is_reachable or not p.environment.web_url:
        return f"""There is no running UI for the application at `{_where(config)}`, so there is
nothing for you to explore.

Report that in your final message and stop. Do not substitute reading the
frontend source for driving it: your entire value is that you see what a user
sees, and a static read of a component is a different, weaker claim that another
agent is better placed to make."""

    scope = (
        "Walk the primary journeys from the task graph only. This run is time-boxed, "
        "so depth on the critical paths beats coverage."
        if mode == "pr-check"
        else "Walk the primary journeys from the task graph first, then explore. "
        "Exploratory wandering is where you find what nobody wrote a test for — "
        "take the paths a confused or impatient user would take."
    )
    return f"""Explore the running product at {p.environment.web_url} as a user experiences it.

{p.description.strip()}

Start with `get_system_map` for `ui_routes` and `task_graph`. That is your itinerary.

{_environment_brief(p)}

{_auth_brief(p)}

{scope}

At every step: read the page, check the console, interact, observe what changed.
When something is wrong, find the shortest path to it, then capture a screenshot
and the console output as evidence before moving on.

Judge like a user, not like a reviewer with opinions about the code. Report what
fails, misleads, blocks or excludes someone. Do not report what you would have
designed differently."""


def discovery(config: SystemConfig, mode: str, spec: "AgentSpec") -> str:
    """The task for a discovery agent with no hand-written builder.

    The architecture's claim is that adding an agent needs a prompt file and a
    YAML file and no Python. That was not true: `_phase_discover` dispatched
    from a hardcoded dict of builders, so a new discovery agent was silently
    skipped with `no task builder` -- it validated, it assembled, it appeared in
    `--dry-run`, and then it did nothing. DBA and AUDITOR were added exactly
    that way and this is the bug they found.

    What an agent should be told is: which application, what it can reach, and
    what its own prompt says its domain is. Everything specific to a domain
    belongs in that agent's prompt, not here -- API and BROWSER keep their
    bespoke builders because they name tools (`diff_openapi`, the browser) that
    only they have.
    """
    p = _profile(config)
    reach = (
        "The application is reachable, so prove what you report: observe the "
        "behaviour and capture the evidence. A finding you have not observed is a "
        "hypothesis, and its confidence should say so."
        if p.environment.is_reachable
        else "There is no reachable instance, so every finding is a reading of the "
        "code. Quote the lines that support it and keep your confidence honest "
        "about not having observed the behaviour."
    )
    return f"""Audit {p.name} for defects in your domain.

Layout — {p.layout.described()}

Your own instructions define what your domain is and what counts as evidence in
it. Work within it and leave the other surfaces to the agents that own them —
that is about where you *search*, not about what you may say. If a defect you
can evidence depends on a fact outside your surface, state that fact, say where
you read it, and include that file in `location.paths`. A defect whose proof
spans two surfaces arrives as two minor findings otherwise, and the half that
would have made it a blocker is the half nobody filed.

{reach}

Emit one envelope per distinct defect with `emit_envelope`. Finding nothing is a
valid outcome; inventing something to report is not. Deduplicate against
`search_similar` before you emit, so a defect this system already knows about
comes back as an occurrence rather than a new finding.

Mode: {mode}."""


def synthesis(config: SystemConfig, mode: str, finding_count: int) -> str:
    """The task for a synthesis agent.

    Its subject is *this run's findings*, not the application — but unlike a
    reporting agent it may open the code, because a conjunction has to be
    confirmed in the two files before it is worth asserting. Naming no domain
    here on purpose: the whole value is that it belongs to none of them.
    """
    p = _profile(config)
    return f"""Join this run's findings, at `{_where(config)}` — your working directory.

{finding_count} findings have been emitted by agents that could not see each
other's work. Each was judged on its own surface. Your question is the one none
of them could ask: **do two or more of these describe a single defect that is
worse than the sum of its parts?**

Start with `list_envelopes`. Read all of them before you judge any of them.

What you are looking for is a conjunction — a defect whose proof needs a fact
from one finding and a fact from another:

- An access-control bypass that needs a missing check in one place and a missing
  constraint in another. Either alone is a note; together they are a way in.
- A data-loss or corruption path that needs a write in one component and the
  absence of a guard in a different one.
- A race or duplication that needs a client's retry behaviour and a server's
  non-idempotency.
- A severity that is wrong because the finding that justifies raising it was
  filed by a different agent against a different surface.

Two findings in the same file are usually two defects, not one. Sharing a path
is a hint, not the answer.

Then **confirm before you emit**. Open both files and read the code that each
half claims. A conjunction you reasoned to but did not verify is weaker than
either of the findings it is built from, and emitting it makes this run worse
rather than better. If you cannot confirm it, say so and emit nothing.

When you do emit, with `emit_envelope`:

- Put every path both halves named into `location.paths`, so the composite is
  anchored in both surfaces.
- Put the ids of the findings you composed from into `dedupe.similar_to`.
- State the severity of the *composite*, and say in the summary why it is higher
  than either component — that reasoning is the finding.
- Cite the evidence the components already carry. You do not need new evidence
  for facts they established; you need it for the join.

Do not go looking for new defects in a domain. Nine agents have already done
that, better than you can from here, and a finding you make alone is one you
have taken from the agent that owns that surface. Finding no conjunction is the
common outcome and a completely valid one — say so plainly and stop.

Layout — {p.layout.described()}

Mode: {mode}."""


def report(config: SystemConfig, mode: str) -> str:
    """The task for a reporting agent.

    Its subject is the run, not the application. Every other agent is pointed at
    the target and asked what is wrong with it; a reporting agent is pointed at
    what just happened and asked what it means. So this task names no routes, no
    schema and no layout -- only the run's own record.
    """
    p = _profile(config)
    return f"""Summarise this run of {p.name}.

Your input is the run itself: the envelopes emitted, the verdicts recorded, the
denials, the escalations, and what each agent cost in turns. Read them with
`list_envelopes` and the run's own ledger. You are not auditing the application
-- the other agents did that -- you are auditing what this run learned.

Report:

- What was found, grouped by severity and domain, and which findings were held
  rather than filed and why.
- Recurrence: which of these the system has seen before, and how often. A defect
  reported for the fourth time is a different problem from a new one.
- Where agents were refused or escalated, and whether the refusal looks correct.
- What the run could NOT do -- surfaces nothing reached, agents that had no
  capability to work with. A gap in coverage is worth as much as a finding, and
  nobody else reports it.

Be specific and short. A report that restates every envelope is a worse version
of `qaas show`. The value here is the pattern across them, and the honest
account of what was not looked at.

Mode: {mode}."""


def reproducer(envelope: DefectEnvelope, config: SystemConfig, flake_runs: int) -> str:
    p = _profile(config)
    evidence = "\n".join(f"  - {e.type.value}: {e.uri} {e.note}".rstrip() for e in envelope.evidence)
    managed = p.environment.is_managed
    pinning = (
        "Pin the environment with `env_control` — a known fixture, known flags, a "
        "known branch — so the reproduction runs identically later."
        if managed
        else "You cannot reset this environment, so pin what you can and record the "
        "rest: note in `environment` exactly what state you found it in. An "
        "unpinnable reproduction is worth recording as such, not worth faking."
    )
    return f"""Reproduce this finding, or demote it.

  id:         {envelope.id}
  reported by {envelope.discovered_by} as {envelope.severity.value} / {envelope.domain.value}
  title:      {envelope.title}
  summary:    {envelope.summary}
  location:   {envelope.location.model_dump(exclude_none=True)}
  confidence: {envelope.confidence:.2f}
  evidence:
{evidence or "  (none attached)"}

The application is at `{_where(config)}`, which is your working directory. {pinning}

Find the shortest path that makes the defect appear. Write a failing test for it
under `qa/repro/` on a `qa/repro/*` branch, and run it {flake_runs} times with
`run_n_times` to measure flake.

Finish by calling `record_reproduction` with your verdict. If you could not make
it happen, say `not_reproducible` and lower the confidence to match. That is a
useful, correct outcome — it is the filter this whole system depends on, and
passing through a finding you could not reproduce costs more than dropping a
real one."""


def triage(config: SystemConfig, cap: int) -> str:
    p = _profile(config)
    owners = (
        f"Resolve component and team from the system map's ownership section, which "
        f"came from `{p.layout.ownership}`."
        if p.layout.ownership
        else "This repository records no ownership, so file unassigned and say so. "
        "Do not infer a team from a directory name."
    )
    return f"""Triage this run's findings and file what deserves to be filed.

Call `list_envelopes` with `fileable_only: true` to see what reached you.
Findings that failed the evidence or confidence gate are not in that list and are
not yours to file — they are already in the human review queue.

For each one, in this order:

1. `search_similar` and `get_occurrences` first. An existing ticket gets the new
   evidence and an incremented count, not a second ticket.
2. Score severity against the rubric, by consequence to users.
3. {owners}
4. Compose the ticket in the house format, with REPRODUCER's steps verbatim and the
   failing test as the acceptance criterion.
5. Route by class. Security findings go to the restricted project.
6. `record` the defect in memory with its ticket key, so the next run dedupes
   against it.

You may file at most {cap} tickets in this run. If you reach that, stop and
escalate rather than filing more."""


def verifier(ticket_key: str, envelope: DefectEnvelope | None, branch: str) -> str:
    repro = ""
    if envelope:
        steps = "\n".join(f"    {i}. {s}" for i, s in enumerate(envelope.reproduction.steps, 1))
        repro = f"""
The original finding:

  title:        {envelope.title}
  failing test: {envelope.reproduction.failing_test or "(none recorded)"}
  environment:  {envelope.reproduction.environment.model_dump()}
  steps:
{steps or "    (none recorded)"}
"""
    return f"""Verify the fix on ticket {ticket_key}, on branch `{branch}`.
{repro}
Bring up the patched build with `env_control`, using the same fixture and flags
as the original reproduction — a different environment proves nothing.

Run the original failing test first. It must now pass. Then run the regression
suite for the affected area, selected with `affected_tests` against the diff.

Return exactly one verdict — VERIFIED, NOT_FIXED or REGRESSED — and transition
the ticket accordingly. Say what you actually observed, including anything you
skipped or could not run."""


def fixer(
    ticket_key: str,
    envelope: DefectEnvelope | None,
    config: SystemConfig | None = None,
    *,
    feedback: str = "",
) -> str:
    """The fix task. FIXER is Phase 3; the router's loop calls this once it exists.

    `feedback` is what makes the second attempt different from the first.
    VERIFIER's `observed` and REVIEWER's concerns were both captured as typed
    ledger entries and then dropped on the floor, so a FIXER/REVIEWER round trip
    re-ran the identical prompt. The router assembles the text; this only has to
    give it somewhere to land, above the defect rather than below it — an agent
    that reads "here is what you got wrong last time" first reads the rest
    differently.
    """
    where = f"the application at `{_where(config)}`" if config and config.profile else "the target application"
    prior = (
        f"""
This is not the first attempt. A previous fix for {ticket_key} did not hold:

{feedback}

Read that before you read the defect. Repeating the last attempt costs a round
trip and reaches the same verdict — if you believe the previous change was right
and the objection is wrong, say so and escalate rather than submitting it again.
"""
        if feedback.strip()
        else ""
    )
    detail = ""
    if envelope:
        steps = "\n".join(f"    {i}. {s}" for i, s in enumerate(envelope.reproduction.steps, 1))
        detail = f"""
  title:        {envelope.title}
  summary:      {envelope.summary}
  location:     {envelope.location.model_dump(exclude_none=True)}
  failing test: {envelope.reproduction.failing_test or "(none recorded)"}
  steps:
{steps or "    (none recorded)"}
"""
    return f"""Fix ticket {ticket_key} in {where}.
{prior}{detail}
Read the affected code with the system map for context, then write the smallest
change that makes the failing test pass. Add a regression test. Run the affected
suite locally before you open anything.

The failing test defines success and you may not edit it. If you believe the test
itself is wrong, that is an escalation, not a licence to change it.

Move {ticket_key} to `in_progress` before you start, and say in the comment which
branch you are working on. The board is how everyone else finds out this ticket is
being worked; a ticket that goes from To Do to Done with nothing in between tells
a person watching that a fix appeared from nowhere.

Work on a `fix/*` branch. Open the pull request as a draft with a rollback note.
Never merge — merge is a human decision."""


def reviewer(
    ticket_key: str, envelope: DefectEnvelope | None = None, *, guidance: str = ""
) -> str:
    """The adversarial review task. REVIEWER is Phase 3.

    `guidance` is a human's answer to an escalation, assembled by the router out
    of typed ledger data the way `feedback` is for FIXER. REVIEWER is usually
    the agent that escalated, and it had no slot at all for an answer -- so the
    reviewer that raised the question re-raised it on the next run having never
    been told, and a person answered CORVID-7 once for every run that touched
    it. It leads the task rather than trailing it: a reviewer that reads "this
    was escalated and here is the decision" first reads the diff differently.
    """
    context = ""
    if envelope:
        context = (
            f"\n\nThe defect it claims to fix: {envelope.title}\n"
            f"The test that defines success: {envelope.reproduction.failing_test or '(none recorded)'}\n"
        )
    answered = f"\n{guidance.strip()}" if guidance.strip() else ""
    return f"""Review the fix for {ticket_key} as an adversarial reviewer.{context}{answered}

Does the change address the root cause or only the symptom? Is the diff minimal?
Does it break a contract, a schema, or a public API? Does it introduce a security
or performance regression? Are the regression tests real, or asserted to pass?
Is the rollback note viable?

Return APPROVE, REQUEST_CHANGES with specifics, or ESCALATE_TO_HUMAN. You have no
write access to code: your judgement is the deliverable."""
