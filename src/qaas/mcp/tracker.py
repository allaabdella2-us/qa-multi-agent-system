"""The `tracker` MCP server — every ticket the system files passes through here.

The rules below are code, not prompt text, and that is deliberate. §8.1 says
only TRIAGE creates and only TRIAGE and VERIFIER transition; §4.12 caps tickets per
run and requires that hitting the cap escalates instead of filing; §10 lists
"security findings leak into public tickets" as a named failure mode. A prompt
can be argued with, misread, or dropped from a truncated context. A refusal
returned from the tool cannot.

Which backend actually stores the ticket is `config.tracker`'s business — see
`qaas.adapters.tracker`. Policy lives here so it holds identically for local
files and for real Jira.

The shipped roster grants the transition right to FIXER and VERIFIER, not TRIAGE,
and each only to the statuses its own task tells it to use —
`TRANSITION_STATUSES` below.
"""

from __future__ import annotations

import asyncio
import copy
import os
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from claude_agent_sdk import create_sdk_mcp_server, tool
from rich.console import Console

from qaas.adapters.tracker import (
    DEFAULT_PROJECT,
    LINK_TYPES,
    SECURITY_PROJECT,
    STATUSES,
    TrackerError,
    build_tracker,
    issue_summary,
    key_project,
    normalise_ticket,
    repo_label,
)
from qaas.envelope import DefectClass, DefectEnvelope, Domain
from qaas.mcp.context import ToolContext, err, ok

_T = TypeVar("_T")

#: Set to 1 to rehearse every tracker write instead of performing it. This is
#: the safety rail for first contact with a real Jira: the policy above still
#: runs, the payload is still assembled, the ledger still records what would
#: have happened — and nothing is sent. Reads (`search`) stay live, because a
#: dedupe check that cannot see the real backlog rehearses the wrong run.
TRACKER_DRY_RUN_ENV = "QAAS_TRACKER_DRY_RUN"

#: Prefixed to every dry-run tool result. An agent that is told "filed CORVID-1"
#: will report a filed ticket, and a human will go looking for it; the notice
#: has to be the first thing in the result, not a footnote.
DRY_RUN_NOTICE = (
    f"DRY RUN — NOTHING WAS FILED. {TRACKER_DRY_RUN_ENV}=1 is set, so the tracker rehearsed "
    "this call and sent nothing to the backend. The key below is a placeholder and does not "
    "exist. Do not report this as a filed ticket."
)

#: Dry-run lines go to stderr so an operator watching a run sees them without
#: them landing in anything that parses stdout.
_console = Console(stderr=True)


def dry_run_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether tracker writes are rehearsed rather than performed.

    Read from the environment rather than config/ so that turning the rail off
    is a deliberate act in the shell that starts the run, and so that a
    committed config file can never quietly disable it.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    return (source.get(TRACKER_DRY_RUN_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


def is_restricted(envelope: DefectEnvelope) -> bool:
    """Whether this finding may only be filed into the restricted project.

    Independent triggers, because any one alone is enough to make a public
    ticket a disclosure: the reporter flagged security impact, or the defect is
    classified as a vulnerability. The `security` domain is the third: a
    finding from the security surface that ticked neither box is still one
    nobody outside the restricted project should read first.
    """
    return (
        envelope.impact.security_relevant
        or envelope.defect_class == DefectClass.VULNERABILITY
        or envelope.domain == Domain.SECURITY
    )


#: Which house statuses each agent may move a ticket to: what its prompt and its
#: task actually tell it to do, and nothing else. Holding `may_transition_tickets`
#: used to mean any status on any ticket, so FIXER, which is told only to start
#: work, could close its own ticket -- the author grading its own fix, which §8.1
#: ("VERIFIER may only close verified") exists to prevent.
#:
#: TRIAGE is absent on purpose. §8.1 lists it as a transitioner, but triage.yaml
#: does not grant `may_transition_tickets` and neither TRIAGE.md nor
#: `tasks.triage` asks it to move a ticket anywhere. An agent missing from this
#: table is refused every status, however its policy reads.
TRANSITION_STATUSES: dict[str, tuple[str, ...]] = {
    # FIXER.md and `tasks.fixer`: "move the ticket to `in_progress` before you
    # touch code". `in_review` is the true status once the draft PR is open and
    # REVIEWER has it. Never a closing status.
    "FIXER": ("in_progress", "in_review"),
    # VERIFIER.md and `verification-protocol`: VERIFIED -> "transition the ticket
    # to done" (`resolved` or `closed`; both resolve to a Jira "Done"), NOT_FIXED
    # -> "reopen" (`open`, which resolves to "Reopened" or "To Do").
    "VERIFIER": ("open", "resolved", "closed"),
}

#: Statuses that end a ticket. Named in a refusal so the reader learns whose
#: decision it is rather than only that it was not theirs.
_CLOSING = ("resolved", "closed")
_HUMAN_ONLY = ("wont_fix", "duplicate")


CREATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["title", "body", "envelope_id"],
    "properties": {
        "title": {"type": "string", "description": "Names the defect, not the symptom."},
        "body": {
            "type": "string",
            "description": "House format: repro steps, evidence links, impact, acceptance criteria.",
        },
        "envelope_id": {
            "type": "string",
            "description": (
                "Required. The envelope this ticket files, from `list_envelopes`. Routing, "
                "severity and the ticket's link back to its finding are all read from it; "
                "a ticket without one is refused."
            ),
        },
        "project": {
            "type": "string",
            "description": (
                f"Omit it. Only {DEFAULT_PROJECT} (the default) and {SECURITY_PROJECT} "
                "(restricted) are accepted. Security findings are forced to "
                f"{SECURITY_PROJECT}."
            ),
        },
        "labels": {"type": "array", "items": {"type": "string"}},
        "severity": {"type": "string", "enum": ["blocker", "critical", "major", "minor", "trivial"]},
        "security_relevant": {
            "type": "boolean",
            "description": (
                "Force restricted routing even when the envelope does not call for it. It "
                "can only restrict a ticket, never make one public."
            ),
        },
    },
}


async def _off_loop(call: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
    """Run one adapter call on a worker thread, and make every failure a `TrackerError`.

    The handlers are coroutines on the one event loop every concurrent agent in
    this process shares, and the adapter blocks: a Jira call waits up to 30s on
    a socket, and a 429 on a read sleeps up to 30s before each retry. Called
    inline, one slow Jira froze every other agent's tool calls, hooks and
    message stream for as long as it took.

    Anything else is converted here because each handler catches `TrackerError`
    and nothing more, and a tool error is returned to the agent, never raised.
    """
    try:
        return await asyncio.to_thread(call, *args, **kwargs)
    except TrackerError:
        raise
    except Exception as exc:  # noqa: BLE001 - converted, not swallowed: the text says what it was
        raise TrackerError(
            f"the tracker backend failed unexpectedly ({type(exc).__name__}: {exc})"
        ) from exc


def build_tools(ctx: ToolContext) -> list:
    """The tracker tools, bound to one agent's run context.

    Split from `build` so tests can call the handlers directly without standing
    up an MCP transport.
    """
    # Built even in dry-run mode: constructing a JiraTracker is what validates
    # the credentials, and a rehearsal against a configuration that could never
    # have worked proves nothing.
    tracker = build_tracker(ctx.config.tracker, ctx.store.root)
    policy = ctx.agent.policy
    dry_run = dry_run_enabled()
    # The cap check, the "already filed" check, the create and the count are one
    # step. They could not interleave while the adapter ran inline on the event
    # loop; on a worker thread two parallel `create_issue` calls could both pass
    # the checks before either counted, which is one ticket over the cap or the
    # same envelope filed twice.
    create_lock = asyncio.Lock()

    def deny(tool_name: str, reason: str) -> dict[str, Any]:
        """Refuse, and leave a trace. A denial nobody can see is not a guardrail."""
        ctx.store.log("denial", agent=ctx.agent.name, tool=tool_name, reason=reason)
        return err(reason)

    def projects() -> list[str]:
        """The two projects this system works in, as the backend spells them."""
        restricted = tracker.security_project
        return [tracker.default_project] + ([restricted] if restricted else [])

    def in_scope(project: str) -> str | None:
        """`project` in its configured spelling, or None when it is not one of ours."""
        wanted = project.strip().upper()
        return next((p for p in projects() if p.upper() == wanted), None)

    def key_refusal(tool_name: str, key: str) -> dict[str, Any] | None:
        """A refusal if `key` is not an issue in one of our projects, else None.

        Any key was accepted, so an agent holding a write right could move or
        link another team's ticket on a shared Jira, and nothing but the
        agent's reading of its prompt stood in the way.
        """
        project = key_project(key)
        if project is None:
            return err(f"'{key}' is not an issue key; expected e.g. {tracker.default_project}-12.")
        if in_scope(project) is None:
            return deny(
                tool_name,
                f"Refused: {key} is in project '{project}', and this system works only in "
                f"{' and '.join(projects())}. Tickets anywhere else belong to people who did "
                "not ask for this system's writes.",
            )
        return None

    def status_refusal(status: str) -> str | None:
        """Why this agent may not move a ticket to `status`, or None if it may."""
        allowed = TRANSITION_STATUSES.get(ctx.agent.name, ())
        if status in allowed:
            return None
        name = ctx.agent.name
        if not allowed:
            return (
                f"Refused: {name} holds `may_transition_tickets`, but nothing in its task tells "
                "it to move a ticket to any status, so every status is refused. A roster that "
                "means it to transition must name its statuses in TRANSITION_STATUSES "
                "(qaas/mcp/tracker.py)."
            )
        if status not in STATUSES:
            return (
                f"Refused: '{status}' is not a house status (one of: {', '.join(STATUSES)}). "
                f"{name} may move a ticket only to: {', '.join(allowed)}."
            )
        why = ""
        if status in _CLOSING:
            why = " Resolving or closing a ticket is VERIFIER's call, and only on a verified fix (§8.1)."
        elif status in _HUMAN_ONLY:
            why = f" No agent is told to mark a ticket '{status}'; that is a human's decision."
        return (
            f"Refused: {name} may move a ticket only to {', '.join(allowed)} — the statuses its "
            f"task tells it to use — and '{status}' is not one of them.{why}"
        )

    # The schemas name the projects and statuses this backend and this agent
    # actually accept, so the model learns the rule from the tool rather than
    # from a refusal after the fact.
    create_schema = copy.deepcopy(CREATE_SCHEMA)
    create_schema["properties"]["project"].update(
        enum=projects(),
        description=(
            f"Omit it. Only {' and '.join(projects())} are accepted"
            + (f"; security findings are forced to {tracker.security_project}." if tracker.security_project else ".")
        ),
    )
    agent_statuses = list(TRANSITION_STATUSES.get(ctx.agent.name) or STATUSES)

    def rehearse(tool_name: str, line: str, **detail: Any) -> None:
        """Record a write that was not performed, on the ledger and on stderr.

        The ledger kind is `dry_run`, never `ticket`: anything counting filed
        tickets must not count these, and a flag on a `ticket` entry is one
        missed `if` away from being counted.
        """
        ctx.store.log("dry_run", agent=ctx.agent.name, tool=tool_name, **detail)
        _console.print(f"[yellow]dry run[/yellow] {ctx.agent.name}: {line} [dim](not sent)[/dim]")

    @tool(
        "create_issue",
        "File one tracker issue for one envelope (`envelope_id` is required). Dedupe first — a "
        "duplicate costs the team more than a miss. Routing and severity are read from the "
        "envelope, and security findings are routed to the restricted project automatically.",
        create_schema,
    )
    async def create_issue(args: dict[str, Any]) -> dict[str, Any]:
        if not policy.may_create_tickets:
            return deny(
                "create_issue",
                f"{ctx.agent.name} may not create tickets (§8.1: TRIAGE only). "
                "Emit your finding as an envelope; TRIAGE files it.",
            )
        async with create_lock:
            return await _create_issue(args)

    async def _create_issue(args: dict[str, Any]) -> dict[str, Any]:
        """`create_issue` past the permission check, run under `create_lock`."""

        # The *effective* cap, which is what the router already computes and
        # passes into TRIAGE's task: `min(policy, thresholds)`. The tool checked
        # only the policy, so lowering `thresholds.max_tickets_per_run` to 3
        # through the dashboard left the real limit at the policy's 10, and the
        # only thing holding the lower number was TRIAGE having read its prompt.
        # A threshold enforced by a prompt is not a threshold.
        cap = min(policy.max_tickets_per_run, ctx.config.thresholds.max_tickets_per_run)
        if ctx.count("tickets") >= cap:
            ctx.store.log(
                "escalation", agent=ctx.agent.name,
                reason="ticket cap reached", cap=cap, tool="create_issue",
            )
            return deny(
                "create_issue",
                f"Ticket cap reached ({cap} for this run). Filing is disabled for the rest "
                "of the run. Hitting the cap means something upstream is wrong, and forty "
                "more tickets will not fix it: this is an escalation, not a filing problem. "
                "Summarise what remains unfiled in your final message and stop.",
            )

        # Required, and routing is decided from it. It was optional, and routing
        # read the envelope only when one was named -- so a vulnerability filed
        # without `envelope_id` (and without the optional `security_relevant`)
        # went into the public project. Whether a finding is a disclosure cannot
        # depend on an argument the caller may leave out.
        envelope_id = str(args.get("envelope_id") or "").strip()
        if not envelope_id:
            return deny(
                "create_issue",
                "Refused: `envelope_id` is required. Every ticket files exactly one envelope, "
                "and whether it may go to a public project is decided from that envelope. Find "
                "the id with `list_envelopes` and pass it.",
            )
        envelope = ctx.store.get_envelope(envelope_id)
        if envelope is None:
            return err(f"No envelope '{envelope_id}' in this run. Emit it first.")
        # The third belt the release plan asked for (the router's `_phase_file`
        # is the second). Nothing here looked at `jira.key`, so an envelope
        # already stamped -- a resumed run, or TRIAGE retrying a create whose
        # reply it never saw -- was filed a second time.
        if envelope.jira.key:
            return deny(
                "create_issue",
                f"Refused: envelope {envelope.id} is already filed as {envelope.jira.key}. "
                "Add new evidence to that ticket instead of filing it again.",
            )

        restricted = bool(args.get("security_relevant")) or is_restricted(envelope)
        requested = (args.get("project") or "").strip() or None
        if requested:
            # Any string was taken as the project -- `project: "SOMEONE-ELSES"`
            # went straight to the backend, and the Jira adapter had no list
            # either. The system files into exactly two projects.
            scoped = in_scope(requested)
            if scoped is None:
                return deny(
                    "create_issue",
                    f"Refused: '{requested}' is not a project this system files into. Only "
                    f"{' and '.join(projects())} are accepted — omit `project` and the ticket "
                    "is routed for you.",
                )
            requested = scoped

        # The project *names* come from the adapter, because a real Jira's keys
        # are set by the deployment (JIRA_PROJECT_KEY / JIRA_SECURITY_PROJECT_KEY)
        # rather than by the house constants. The routing *rule* is here, once,
        # so it holds identically for local files and for Jira.
        restricted_project = tracker.security_project

        if restricted:
            if restricted_project is None:
                return deny(
                    "create_issue",
                    "Refused: this is a security-relevant finding and this tracker has no "
                    "restricted project configured (set JIRA_SECURITY_PROJECT_KEY to a "
                    f"project with restricted visibility). Filing it into "
                    f"'{tracker.default_project}' would be a disclosure, and there is no "
                    "undo (§4.12, §10). Escalate this finding to a human through a private "
                    "channel instead — do not file it anywhere.",
                )
            if requested and requested != restricted_project:
                return deny(
                    "create_issue",
                    f"Refused: this is a security-relevant finding and '{requested}' is not the "
                    f"restricted project. Security findings never go to a public project (§4.12, "
                    f"§10) — file it into {restricted_project}, or omit `project` and it is routed "
                    "there for you.",
                )
            project = restricted_project
        else:
            project = requested or tracker.default_project

        severity = args.get("severity") or envelope.severity.value
        labels = list(args.get("labels") or [])
        if "agent-found" not in labels:
            labels.append("agent-found")
        if restricted and "security" not in labels:
            labels.append("security")
        # Which repository this defect is in, stamped by the system rather than
        # asked of the agent. It is the only thing that puts the ticket on that
        # repository's board, and an agent that forgot it would file a ticket
        # that exists and is invisible to the person watching.
        repo = repo_label(ctx.config.target)
        if repo and repo not in labels:
            labels.append(repo)

        if dry_run:
            # The cap is still consumed: a rehearsal that ignores the rate limit
            # is not a rehearsal of the run you are about to do. The title and
            # labels are shown as the backend would store them, for the same reason.
            title, _, labels = normalise_ticket(args["title"], "", labels)
            n = ctx.bump("tickets")
            key = f"{project}-{9000 + n}"
            rehearse(
                "create_issue",
                f"would file '{title}' in {project} as {key}",
                action="create_issue", key=key, project=project, title=title,
                severity=severity, labels=labels, restricted=restricted,
                envelope_id=envelope.id, count=n, cap=cap,
            )
            return ok(
                f"{DRY_RUN_NOTICE} It would have filed '{title}' into {project} "
                f"(placeholder key {key}, severity {severity or 'unset'}, labels "
                f"{', '.join(labels)}) — {n}/{cap} for this run.",
                key=key, project=project, title=title, severity=severity,
                labels=labels, restricted=restricted,
                envelope_id=envelope.id,
                tickets_filed=n, tickets_cap=cap, dry_run=True, filed=False,
            )

        try:
            issue = await _off_loop(
                tracker.create_issue,
                project=project,
                title=args["title"],
                body=args["body"],
                labels=labels,
                severity=severity,
                envelope_id=envelope.id,
                fingerprint=envelope.fingerprint(),
                reporter=ctx.agent.name,
            )
        except TrackerError as exc:
            return err(f"Tracker rejected the issue: {exc}")

        # Stamp the key back onto the envelope. The two loops of this system meet
        # at the ticket (§1) and this write is that junction: without it the
        # remediation half can never find anything to work on, because it selects
        # by `envelope.jira.key`. A whole full-loop run reached TRIAGE, filed ten
        # tickets, and then skipped verification entirely for exactly this reason.
        if envelope is not None:
            ctx.store.put_envelope(
                envelope.model_copy(
                    update={
                        "jira": envelope.jira.model_copy(
                            update={"key": issue.key, "project": project, "status": issue.status}
                        )
                    }
                )
            )

        n = ctx.bump("tickets")
        ctx.store.log(
            "ticket", agent=ctx.agent.name, action="created", key=issue.key,
            project=project, severity=severity, restricted=restricted,
            envelope_id=issue.envelope_id, count=n, cap=cap,
        )
        routed = " Routed to the restricted project because it is security-relevant." if restricted else ""
        return ok(
            f"Filed {issue.key} in {project} ({n}/{cap} this run).{routed}",
            **issue_summary(issue),
            tickets_filed=n,
            tickets_cap=cap,
            restricted=restricted,
        )

    @tool(
        "transition",
        "Move an issue to a new status. Say why in the comment — the history is the audit trail.",
        {
            "type": "object",
            "required": ["key", "status"],
            "properties": {
                "key": {"type": "string", "description": f"e.g. {tracker.default_project}-12"},
                "status": {
                    "type": "string",
                    "enum": agent_statuses,
                    "description": "Only the statuses your task tells you to use are accepted.",
                },
                "comment": {"type": "string", "description": "Why. Verdicts without reasons are not reviewable."},
            },
        },
    )
    async def transition(args: dict[str, Any]) -> dict[str, Any]:
        if not policy.may_transition_tickets:
            # It said "§8.1: TRIAGE and VERIFIER only" -- while FIXER held the
            # right and TRIAGE did not. Read from the roster, so it cannot drift.
            holders = sorted(
                name for name, spec in ctx.config.agents.items() if spec.policy.may_transition_tickets
            )
            return deny(
                "transition",
                f"{ctx.agent.name} may not transition tickets: its policy does not grant "
                f"`may_transition_tickets` (§8.1). In this roster only "
                f"{', '.join(holders) or 'no agent'} may, each to the statuses its task names.",
            )

        refused = key_refusal("transition", str(args.get("key") or ""))
        if refused is not None:
            return refused
        reason = status_refusal(str(args.get("status") or ""))
        if reason is not None:
            return deny("transition", reason)

        if dry_run:
            # The issue is not read back either: in a rehearsal nothing was ever
            # filed, so the key the agent holds is a placeholder and a lookup
            # would fail for a reason that has nothing to do with the workflow.
            rehearse(
                "transition",
                f"would move {args['key']} to '{args['status']}'",
                action="transition", key=args["key"], status=args["status"],
                comment=args.get("comment", ""),
            )
            return ok(
                f"{DRY_RUN_NOTICE} It would have moved {args['key']} to '{args['status']}'"
                + (f" with the comment: {args['comment']}" if args.get("comment") else "")
                + ". The ticket's real status is unchanged.",
                key=args["key"], status=args["status"], dry_run=True, filed=False,
            )

        try:
            issue = await _off_loop(
                tracker.transition,
                args["key"], args["status"], by=ctx.agent.name, comment=args.get("comment", ""),
            )
        except TrackerError as exc:
            return err(f"Transition refused: {exc}")

        ctx.store.log(
            "ticket", agent=ctx.agent.name, action="transitioned",
            key=issue.key, status=issue.status, comment=args.get("comment", ""),
        )
        return ok(f"{issue.key} is now '{issue.status}'.", **issue_summary(issue))

    @tool(
        "link",
        "Link two existing issues — 'duplicates' for a repeat, 'regression-of' when a resolved "
        "defect has come back.",
        {
            "type": "object",
            "required": ["key", "to"],
            "properties": {
                "key": {"type": "string"},
                "to": {"type": "string"},
                "type": {"type": "string", "enum": list(LINK_TYPES), "default": "relates"},
            },
        },
    )
    async def link(args: dict[str, Any]) -> dict[str, Any]:
        if not (policy.may_create_tickets or policy.may_transition_tickets):
            return deny(
                "link",
                f"{ctx.agent.name} has no tracker write access, and a link is a write (§8.1).",
            )
        for end in (args.get("key"), args.get("to")):
            refused = key_refusal("link", str(end or ""))
            if refused is not None:
                return refused

        link_type = args.get("type") or "relates"
        if dry_run:
            rehearse(
                "link",
                f"would link {args['key']} {link_type} {args['to']}",
                action="link", key=args["key"], to=args["to"], type=link_type,
            )
            return ok(
                f"{DRY_RUN_NOTICE} It would have linked {args['key']} {link_type} "
                f"{args['to']}. No link exists.",
                key=args["key"], to=args["to"], type=link_type, dry_run=True, filed=False,
            )

        try:
            issue = await _off_loop(tracker.link, args["key"], args["to"], link_type)
        except TrackerError as exc:
            return err(f"Link refused: {exc}")
        ctx.store.log(
            "ticket", agent=ctx.agent.name, action="linked",
            key=args["key"], to=args["to"], type=link_type,
        )
        return ok(f"{issue.key} {link_type} {args['to']}.", **issue_summary(issue))

    @tool(
        "search",
        "Find existing issues before filing a new one, or to pick up work.",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Substring of title or body."},
                "project": {"type": "string", "enum": projects()},
                "status": {"type": "string", "enum": list(STATUSES)},
                "label": {"type": "string"},
                "envelope_id": {"type": "string"},
                "fingerprint": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        },
    )
    async def search(args: dict[str, Any]) -> dict[str, Any]:
        # Deliberately live even in dry-run mode: dedupe against an imaginary
        # backlog would rehearse a run that files things the real one would not.
        project = args.get("project")
        if project:
            # Scoped like every write: a named project bypassed the adapter's
            # default scope and pulled another team's tickets into an agent's
            # context, where they read as prior art.
            project = in_scope(str(project))
            if project is None:
                return err(
                    f"'{args['project']}' is not a project this system works in; search "
                    f"{' or '.join(projects())}, or omit `project` to search both."
                )
        try:
            issues = await _off_loop(
                tracker.search,
                text=args.get("text"),
                project=project,
                status=args.get("status"),
                label=args.get("label"),
                envelope_id=args.get("envelope_id"),
                fingerprint=args.get("fingerprint"),
                limit=int(args.get("limit") or 20),
            )
        except TrackerError as exc:
            return err(f"Search refused: {exc}")
        # The local tracker skips a ticket file it cannot parse rather than
        # failing every search on it. Say so: a dedupe that silently read less
        # than the whole backlog would report "no duplicate" with confidence.
        skipped = int(getattr(tracker, "unreadable_files", 0) or 0)
        note = f"\n({skipped} ticket file(s) could not be read and were skipped.)" if skipped else ""
        if not issues:
            return ok(f"No issues match.{note}", issues=[], count=0, unreadable=skipped)
        lines = [f"  {i.key}  [{i.status}]  {i.severity or '-'}  {i.title}" for i in issues]
        return ok(
            f"{len(issues)} issue(s):\n" + "\n".join(lines) + note,
            issues=[issue_summary(i) for i in issues],
            count=len(issues),
            unreadable=skipped,
        )

    return [create_issue, transition, link, search]


def build(ctx: ToolContext):
    """Construct the tracker MCP server bound to one agent's run context."""
    return create_sdk_mcp_server(name="tracker", version="1.0.0", tools=build_tools(ctx))
