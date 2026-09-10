"""The `envelope` MCP server — the only way a finding leaves an agent.

Validation happens here rather than in a prompt. An agent that emits a malformed
envelope gets the field-level errors back and can correct them; an agent that
emits a finding with no evidence gets refused. Neither is negotiable by argument.
"""

from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from pydantic import ValidationError

from qaas.envelope import DefectEnvelope, Reproduction
from qaas.mcp.context import ToolContext, err, ok

EMIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["domain", "class", "title", "summary", "severity", "confidence"],
    "properties": {
        "domain": {
            "type": "string",
            "enum": ["architecture", "database", "api", "websocket", "frontend", "ux", "security", "performance"],
        },
        "class": {
            "type": "string",
            "enum": ["bug", "regression", "ux-friction", "tech-debt", "vulnerability", "perf-regression"],
        },
        "title": {"type": "string", "maxLength": 90, "description": "One line naming the defect, not the symptom."},
        "summary": {"type": "string", "description": "2-4 sentences: what breaks, when, for whom."},
        "severity": {"type": "string", "enum": ["blocker", "critical", "major", "minor", "trivial"]},
        "confidence": {
            "type": "number", "minimum": 0, "maximum": 1,
            "description": "How sure you are a maintainer would accept this. Below 0.6 goes to human review.",
        },
        "location": {
            "type": "object",
            "properties": {
                "service": {"type": "string"},
                "paths": {"type": "array", "items": {"type": "string"}, "description": "Repo-relative, optionally file.py:line"},
                "endpoint": {"type": "string", "description": "e.g. 'GET /v1/orders'"},
                "ui_route": {"type": "string", "description": "e.g. '/checkout/review'"},
                "commit_sha": {"type": "string"},
            },
        },
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["type", "uri"],
                "properties": {
                    "type": {"type": "string", "enum": ["screenshot", "trace", "query_plan", "log", "frame_capture", "test_output", "har"]},
                    "uri": {"type": "string", "description": "An artifact:// uri from put_artifact"},
                    "note": {"type": "string"},
                },
            },
        },
        "reproduction": {
            "type": "object",
            "description": (
                "The steps you took and the state you observed. You cannot mark a "
                "finding reproduced — REPRODUCER verifies that independently, which is "
                "the point of a separate triage agent."
            ),
            "properties": {
                "steps": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Exactly what you did, in order, so REPRODUCER can start from it.",
                },
                "environment": {
                    "type": "object",
                    "properties": {
                        "branch": {"type": "string"},
                        "fixture": {"type": "string"},
                        "flags": {"type": "object"},
                    },
                },
            },
        },
        "impact": {
            "type": "object",
            "properties": {
                "user_facing": {"type": "boolean"},
                "affected_surface": {"type": "string"},
                "data_loss_risk": {"type": "boolean"},
                "security_relevant": {"type": "boolean"},
                "frequency_estimate": {"type": "string"},
            },
        },
        "suggested_owner": {
            "type": "object",
            "properties": {"component": {"type": "string"}, "team": {"type": "string"}},
        },
        "suggested_fix_area": {"type": "string"},
        "similar_to": {"type": "array", "items": {"type": "string"}, "description": "Known ticket keys this resembles."},
    },
}


def build_tools(ctx: ToolContext) -> list:
    """The envelope tools, bound to one agent's run context.

    Split from `build` so tests can call the handlers directly without standing
    up an MCP transport.
    """

    @tool(
        "emit_envelope",
        "Report one defect. This is the only way a finding leaves your session. "
        "Rejected envelopes come back with the reason; fix the fields and retry.",
        EMIT_SCHEMA,
    )
    async def emit_envelope(args: dict[str, Any]) -> dict[str, Any]:
        cap = ctx.config.thresholds.max_findings_per_agent_run
        if ctx.count("envelopes") >= cap:
            ctx.store.log("escalation", agent=ctx.agent.name, reason="finding cap reached", cap=cap)
            return err(
                f"Finding cap reached ({cap} for this run). Emitting more is disabled. "
                "If you genuinely have more real defects than this, that is an escalation, "
                "not a filing problem: summarise what remains in your final message and stop."
            )

        payload = {k: v for k, v in args.items() if k != "similar_to"}
        payload["run_id"] = ctx.store.run_id
        payload["discovered_by"] = ctx.agent.name

        # A discovery agent does not get to certify its own finding as
        # reproduced (§2: the finder never grades its own homework). Whatever it
        # claims here, the status is reset and REPRODUCER decides independently.
        # Without this the whole triage gate is bypassed by an agent simply
        # asserting it already reproduced the defect — which is exactly what
        # happened on the first full pipeline run, and REPRODUCER was skipped.
        environment = (args.get("reproduction") or {}).get("environment", {})
        steps = (args.get("reproduction") or {}).get("steps", [])
        payload["reproduction"] = {
            "status": "unattempted",
            "steps": steps,
            "environment": environment,
        }
        if args.get("similar_to"):
            payload["dedupe"] = {"similar_to": args["similar_to"]}

        try:
            envelope = DefectEnvelope.model_validate(payload)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:8]
            )
            return err(f"Envelope rejected. Fix these fields and call again: {problems}")

        fileable, reason = envelope.is_fileable(ctx.config.thresholds.min_confidence_to_file)
        ctx.store.put_envelope(envelope)
        n = ctx.bump("envelopes")

        note = "" if fileable else f" Held from filing: {reason}. It still counts toward your cap."
        return ok(
            f"Recorded {envelope.severity.value} {envelope.domain.value} finding "
            f"'{envelope.title}' ({n}/{cap}).{note}",
            envelope_id=envelope.id,
            fingerprint=envelope.fingerprint(),
            fileable=fileable,
        )

    @tool(
        "get_system_map",
        "Read the shared system map: services, routes, ui_routes, schema, ownership, task_graph. "
        "Read this before exploring the repository yourself.",
        {"type": "object", "properties": {"section": {"type": "string", "description": "Optional single section to return."}}},
    )
    async def get_system_map(args: dict[str, Any]) -> dict[str, Any]:
        payload = ctx.maps.get(ctx.map_version)
        if payload is None:
            return err("No system map exists yet. MAPPER has not run.")
        section = args.get("section")
        if section:
            if section not in payload:
                return err(f"No section '{section}'. Available: {', '.join(sorted(payload))}")
            payload = {section: payload[section]}
        return ok(json.dumps(payload, indent=2)[:60_000], version=ctx.map_version or ctx.maps.latest_version())

    @tool(
        "put_system_map",
        "Publish the system map. Call once, with the complete map. Mapper only.",
        {
            "type": "object",
            "required": ["map"],
            "properties": {"map": {"type": "object", "description": "The complete system map."}},
        },
    )
    async def put_system_map(args: dict[str, Any]) -> dict[str, Any]:
        if ctx.agent.name != "MAPPER":
            return err("Only MAPPER may publish the system map.")
        payload = args.get("map")
        if not isinstance(payload, dict) or not payload:
            return err("map must be a non-empty object.")
        missing = [k for k in ("services", "routes") if k not in payload]
        if missing:
            return err(f"map is missing required sections: {', '.join(missing)}")
        version = ctx.maps.put(payload)
        ctx.store.log("system_map", agent=ctx.agent.name, version=version, sections=sorted(payload))
        return ok(f"Published system map {version} with sections: {', '.join(sorted(payload))}.", version=version)

    @tool(
        "put_artifact",
        "Store evidence and get back the artifact:// uri to cite in an envelope.",
        {
            "type": "object",
            "required": ["name", "content"],
            "properties": {
                "name": {"type": "string", "description": "Filename, e.g. 'orders-500.log'"},
                "content": {"type": "string"},
            },
        },
    )
    async def put_artifact(args: dict[str, Any]) -> dict[str, Any]:
        uri = ctx.store.put_artifact(args["name"], args["content"])
        return ok(f"Stored as {uri}", uri=uri)


    @tool(
        "list_envelopes",
        "List findings recorded in this run, with their reproduction status. "
        "Use this to see what you have been asked to work on.",
        {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["reproduced", "flaky", "not_reproducible", "unattempted"]},
                "fileable_only": {"type": "boolean", "description": "Only findings that passed the evidence and confidence gates."},
            },
        },
    )
    async def list_envelopes(args: dict[str, Any]) -> dict[str, Any]:
        envelopes = ctx.store.envelopes()
        wanted = args.get("status")
        if wanted:
            envelopes = [e for e in envelopes if e.reproduction.status.value == wanted]
        if args.get("fileable_only"):
            envelopes = [e for e in envelopes if e.is_fileable(ctx.config.thresholds.min_confidence_to_file)[0]]
        if not envelopes:
            return ok("No findings match.", envelopes=[])

        rows = [
            {
                "id": e.id,
                "discovered_by": e.discovered_by,
                "domain": e.domain.value,
                "severity": e.severity.value,
                "confidence": e.confidence,
                "title": e.title,
                "summary": e.summary,
                "location": e.location.model_dump(exclude_none=True),
                "evidence": [ev.model_dump() for ev in e.evidence],
                "reproduction": e.reproduction.model_dump(mode="json"),
                "fingerprint": e.dedupe.fingerprint,
                "fileable": e.is_fileable(ctx.config.thresholds.min_confidence_to_file)[0],
            }
            for e in envelopes
        ]
        summary = "\n".join(
            f"{r['id']}  [{r['severity']}/{r['domain']}] {r['title']} "
            f"(by {r['discovered_by']}, repro={r['reproduction']['status']})"
            for r in rows
        )
        return ok(f"{len(rows)} finding(s):\n{summary}", envelopes=rows)

    @tool(
        "record_reproduction",
        "Record your reproduction verdict on an existing finding. REPRODUCER only. "
        "This replaces the finding's reproduction block and adjusts its confidence.",
        {
            "type": "object",
            "required": ["envelope_id", "status", "confidence"],
            "properties": {
                "envelope_id": {"type": "string"},
                "status": {"type": "string", "enum": ["reproduced", "flaky", "not_reproducible"]},
                "confidence": {
                    "type": "number", "minimum": 0, "maximum": 1,
                    "description": "Your confidence after attempting reproduction. Lower it honestly if you could not reproduce.",
                },
                "steps": {"type": "array", "items": {"type": "string"}, "description": "The minimal steps, in order."},
                "failing_test": {"type": "string", "description": "e.g. qa/repro/test_orders_limit.py::test_limit_ignored"},
                "flake_rate": {"type": "number", "minimum": 0, "maximum": 1},
                "environment": {
                    "type": "object",
                    "properties": {"branch": {"type": "string"}, "fixture": {"type": "string"}, "flags": {"type": "object"}},
                },
                "note": {"type": "string", "description": "Why, if you could not reproduce it."},
            },
        },
    )
    async def record_reproduction(args: dict[str, Any]) -> dict[str, Any]:
        if ctx.agent.name != "REPRODUCER":
            return err("Only REPRODUCER records reproduction verdicts.")

        envelope = ctx.store.get_envelope(args["envelope_id"])
        if envelope is None:
            return err(f"No finding with id {args['envelope_id']} in this run.")

        repro = {
            "status": args["status"],
            "steps": args.get("steps", []),
            "failing_test": args.get("failing_test"),
            "flake_rate": args.get("flake_rate", 0.0),
            "environment": args.get("environment", {}),
            "verified_by": "REPRODUCER",
        }
        try:
            updated = envelope.model_copy(
                update={
                    "reproduction": Reproduction.model_validate(repro),
                    "confidence": float(args["confidence"]),
                }
            )
        except (ValidationError, ValueError) as exc:
            return err(f"Verdict rejected: {exc}")

        ctx.store.put_envelope(updated)
        fileable, reason = updated.is_fileable(ctx.config.thresholds.min_confidence_to_file)
        ctx.store.log(
            "reproduction",
            agent="REPRODUCER",
            envelope_id=updated.id,
            status=args["status"],
            flake_rate=repro["flake_rate"],
            fileable=fileable,
        )
        verdict = "will reach TRIAGE" if fileable else f"held: {reason}"
        return ok(
            f"Verdict recorded for {updated.id}: {args['status']}, confidence "
            f"{updated.confidence:.2f} — {verdict}.",
            envelope_id=updated.id,
            fileable=fileable,
        )

    @tool(
        "record_verdict",
        "Record your verification verdict on a ticket. VERIFIER only. Exactly one verdict "
        "per ticket: VERIFIED, NOT_FIXED or REGRESSED.",
        {
            "type": "object",
            "required": ["ticket_key", "verdict", "observed"],
            "properties": {
                "ticket_key": {"type": "string"},
                "verdict": {"type": "string", "enum": ["VERIFIED", "NOT_FIXED", "REGRESSED"]},
                "observed": {
                    "type": "string",
                    "description": "What you actually saw. On NOT_FIXED this is the delta the next agent works from, so be exact: which assertion failed, expected versus actual.",
                },
                "ran": {"type": "array", "items": {"type": "string"}, "description": "What you ran."},
                "not_run": {"type": "array", "items": {"type": "string"}, "description": "What you skipped, and why."},
                "envelope_id": {"type": "string"},
            },
        },
    )
    async def record_verdict(args: dict[str, Any]) -> dict[str, Any]:
        if ctx.agent.name != "VERIFIER":
            return err("Only VERIFIER records verification verdicts.")

        verdict = args["verdict"]
        if verdict == "VERIFIED" and not args.get("ran"):
            # A verdict that closes a ticket must say what backed it. Without
            # this an empty VERIFIED is indistinguishable from a thorough one.
            return err(
                "VERIFIED requires `ran` — name the original failing test and the "
                "regression tests you executed. A verdict nobody can audit is not a verdict."
            )

        ctx.store.log(
            "verdict",
            agent="VERIFIER",
            ticket_key=args["ticket_key"],
            verdict=verdict,
            envelope_id=args.get("envelope_id"),
            observed=args["observed"][:2000],
            ran=args.get("ran", []),
            not_run=args.get("not_run", []),
        )
        return ok(f"Verdict {verdict} recorded for {args['ticket_key']}.", verdict=verdict)


    @tool(
        "record_review",
        "Record your review decision on a fix. REVIEWER only. This is the decision "
        "the router routes on: APPROVE lets the fix proceed to verification, "
        "REQUEST_CHANGES sends it back, ESCALATE_TO_HUMAN stops the loop.",
        {
            "type": "object",
            "required": ["ticket_key", "decision", "reasoning"],
            "properties": {
                "ticket_key": {"type": "string"},
                "decision": {
                    "type": "string",
                    "enum": ["APPROVE", "REQUEST_CHANGES", "ESCALATE_TO_HUMAN"],
                },
                "reasoning": {
                    "type": "string",
                    "description": (
                        "Why. For REQUEST_CHANGES this goes back to FIXER verbatim, "
                        "so be specific enough to act on: name the file and what is wrong."
                    ),
                },
                "root_cause_addressed": {
                    "type": "boolean",
                    "description": "Whether the fix addresses the cause rather than the symptom.",
                },
                "diff_files": {"type": "integer", "description": "How many files the diff touches."},
                "concerns": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Specific risks, even when approving.",
                },
            },
        },
    )
    async def record_review(args: dict[str, Any]) -> dict[str, Any]:
        if ctx.agent.name != "REVIEWER":
            return err("Only REVIEWER records review decisions.")

        decision = args["decision"]
        reasoning = (args.get("reasoning") or "").strip()

        # An approval with no reasoning is not a review, and it is the shape a
        # rubber stamp takes. REQUEST_CHANGES with nothing actionable is worse:
        # it sends FIXER round the loop with no idea what to change.
        if len(reasoning) < 40:
            return err(
                f"{decision} needs reasoning a person can act on — at least a "
                "sentence naming what you checked and what you concluded."
            )
        if decision == "REQUEST_CHANGES" and not args.get("concerns"):
            return err(
                "REQUEST_CHANGES must list concerns. FIXER gets them verbatim and "
                "cannot act on a verdict with no specifics."
            )

        ctx.store.log(
            "review",
            agent="REVIEWER",
            ticket_key=args["ticket_key"],
            decision=decision,
            reasoning=reasoning,
            root_cause_addressed=args.get("root_cause_addressed"),
            diff_files=args.get("diff_files"),
            concerns=args.get("concerns", []),
        )
        return ok(f"Review recorded for {args['ticket_key']}: {decision}.", decision=decision)

    return [
        emit_envelope,
        record_review,
        record_verdict,
        list_envelopes,
        record_reproduction,
        get_system_map,
        put_system_map,
        put_artifact,
    ]


def build(ctx: ToolContext):
    """Construct the envelope MCP server bound to one agent's run context."""
    return create_sdk_mcp_server(name="envelope", version="1.0.0", tools=build_tools(ctx))
