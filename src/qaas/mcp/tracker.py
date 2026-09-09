"""The `tracker` MCP server — every ticket the system files passes through here.

The rules below are code, not prompt text, and that is deliberate. §8.1 says
only CLERK creates and only CLERK and PROOF transition; §4.12 caps tickets per
run and requires that hitting the cap escalates instead of filing; §10 lists
"security findings leak into public tickets" as a named failure mode. A prompt
can be argued with, misread, or dropped from a truncated context. A refusal
returned from the tool cannot.

Which backend actually stores the ticket is `config.tracker`'s business — see
`qaas.adapters.tracker`. Policy lives here so it holds identically for local
files and for real Jira.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

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
    repo_label,
)
from qaas.envelope import DefectClass, DefectEnvelope
from qaas.mcp.context import ToolContext, err, ok

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

    Two independent triggers, because either alone is enough to make a public
    ticket a disclosure: the reporter flagged security impact, or the defect is
    classified as a vulnerability.
    """
    return envelope.impact.security_relevant or envelope.defect_class == DefectClass.VULNERABILITY


CREATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["title", "body"],
    "properties": {
        "title": {"type": "string", "description": "Names the defect, not the symptom."},
        "body": {
            "type": "string",
            "description": "House format: repro steps, evidence links, impact, acceptance criteria.",
        },
        "envelope_id": {
            "type": "string",
            "description": "The envelope this ticket files. Supply it: routing and severity are read from it.",
        },
        "project": {
            "type": "string",
            "description": f"Defaults to {DEFAULT_PROJECT}. Security findings are forced to {SECURITY_PROJECT}.",
        },
        "labels": {"type": "array", "items": {"type": "string"}},
        "severity": {"type": "string", "enum": ["blocker", "critical", "major", "minor", "trivial"]},
        "security_relevant": {
            "type": "boolean",
            "description": "Force restricted routing when no envelope carries the flag.",
        },
    },
}


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

    def deny(tool_name: str, reason: str) -> dict[str, Any]:
        """Refuse, and leave a trace. A denial nobody can see is not a guardrail."""
        ctx.store.log("denial", agent=ctx.agent.name, tool=tool_name, reason=reason)
        return err(reason)

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
        "File one tracker issue. Dedupe first — a duplicate costs the team more than a miss. "
        "Security findings are routed to the restricted project automatically.",
        CREATE_SCHEMA,
    )
    async def create_issue(args: dict[str, Any]) -> dict[str, Any]:
        if not policy.may_create_tickets:
            return deny(
                "create_issue",
                f"{ctx.agent.name} may not create tickets (§8.1: CLERK only). "
                "Emit your finding as an envelope; CLERK files it.",
            )

        cap = policy.max_tickets_per_run
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

        envelope = None
        if args.get("envelope_id"):
            envelope = ctx.store.get_envelope(args["envelope_id"])
            if envelope is None:
                return err(f"No envelope '{args['envelope_id']}' in this run. Emit it first.")

        restricted = bool(args.get("security_relevant")) or (envelope is not None and is_restricted(envelope))
        requested = (args.get("project") or "").strip() or None

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

        severity = args.get("severity") or (envelope.severity.value if envelope else None)
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
            # is not a rehearsal of the run you are about to do.
            n = ctx.bump("tickets")
            key = f"{project}-{9000 + n}"
            rehearse(
                "create_issue",
                f"would file '{args['title']}' in {project} as {key}",
                action="create_issue", key=key, project=project, title=args["title"],
                severity=severity, labels=sorted(labels), restricted=restricted,
                envelope_id=envelope.id if envelope else None, count=n, cap=cap,
            )
            return ok(
                f"{DRY_RUN_NOTICE} It would have filed '{args['title']}' into {project} "
                f"(placeholder key {key}, severity {severity or 'unset'}, labels "
                f"{', '.join(sorted(labels))}) — {n}/{cap} for this run.",
                key=key, project=project, title=args["title"], severity=severity,
                labels=sorted(labels), restricted=restricted,
                envelope_id=envelope.id if envelope else None,
                tickets_filed=n, tickets_cap=cap, dry_run=True, filed=False,
            )

        try:
            issue = tracker.create_issue(
                project=project,
                title=args["title"],
                body=args["body"],
                labels=labels,
                severity=severity,
                envelope_id=envelope.id if envelope else None,
                fingerprint=envelope.fingerprint() if envelope else None,
                reporter=ctx.agent.name,
            )
        except TrackerError as exc:
            return err(f"Tracker rejected the issue: {exc}")

        # Stamp the key back onto the envelope. The two loops of this system meet
        # at the ticket (§1) and this write is that junction: without it the
        # remediation half can never find anything to work on, because it selects
        # by `envelope.jira.key`. A whole full-loop run reached CLERK, filed ten
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
                "key": {"type": "string", "description": f"e.g. {DEFAULT_PROJECT}-12"},
                "status": {"type": "string", "enum": list(STATUSES)},
                "comment": {"type": "string", "description": "Why. Verdicts without reasons are not reviewable."},
            },
        },
    )
    async def transition(args: dict[str, Any]) -> dict[str, Any]:
        if not policy.may_transition_tickets:
            return deny(
                "transition",
                f"{ctx.agent.name} may not transition tickets (§8.1: CLERK and PROOF only).",
            )

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
            issue = tracker.transition(
                args["key"], args["status"], by=ctx.agent.name, comment=args.get("comment", "")
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
            issue = tracker.link(args["key"], args["to"], link_type)
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
                "project": {"type": "string"},
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
        try:
            issues = tracker.search(
                text=args.get("text"),
                project=args.get("project"),
                status=args.get("status"),
                label=args.get("label"),
                envelope_id=args.get("envelope_id"),
                fingerprint=args.get("fingerprint"),
                limit=int(args.get("limit") or 20),
            )
        except TrackerError as exc:
            return err(f"Search refused: {exc}")
        if not issues:
            return ok("No issues match.", issues=[], count=0)
        lines = [f"  {i.key}  [{i.status}]  {i.severity or '-'}  {i.title}" for i in issues]
        return ok(
            f"{len(issues)} issue(s):\n" + "\n".join(lines),
            issues=[issue_summary(i) for i in issues],
            count=len(issues),
        )

    return [create_issue, transition, link, search]


def build(ctx: ToolContext):
    """Construct the tracker MCP server bound to one agent's run context."""
    return create_sdk_mcp_server(name="tracker", version="1.0.0", tools=build_tools(ctx))
