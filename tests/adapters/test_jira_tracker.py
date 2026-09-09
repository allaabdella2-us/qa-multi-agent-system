"""JiraTracker against a local HTTP stub — real sockets, no real Jira.

The default run must never touch the network: a test suite that needs a Jira
site is a suite nobody runs. So these stand up an `http.server` on loopback that
records every request and returns canned Jira payloads. That is enough to test
the things that actually break — the shape of the JSON we send, which status
code produces which message, and what is retried — while the one test that
genuinely needs a live instance is marked `@pytest.mark.jira` and excluded by
`addopts`.

What a stub cannot tell us is whether Jira agrees with our payloads. That is
the marked test's job, and the reason it exists rather than being deleted.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from support import CONFIG_SEARCH, PACKAGED_CONFIG

from qaas.adapters.tracker import (
    JIRA_API_TOKEN_URL,
    Issue,
    JiraDataCenterTracker,
    JiraTracker,
    TrackerConfigError,
    TrackerError,
    UnknownIssue,
    adf_to_text,
    build_tracker,
    markdown_to_adf,
    repo_label,
)
from qaas.config import load_config
from qaas.envelope import DefectEnvelope
from qaas.mcp.context import ToolContext, handlers
from qaas.mcp.tracker import build_tools
from qaas.store import RunStore, SystemMapStore

REPO_ROOT = Path(__file__).resolve().parents[2]
API = "/rest/api/3"


# -- the stub ---------------------------------------------------------------


@dataclass
class Recorded:
    """One request the stub saw, in the form a test wants to assert on."""

    method: str
    path: str
    query: str
    body: Any
    headers: dict[str, str]

    @property
    def fields(self) -> dict[str, Any]:
        return (self.body or {}).get("fields", {})


@dataclass
class JiraStub:
    """A canned Jira. Routes are `(method, path) -> [(status, payload, headers)]`.

    A route with several responses pops one per call, so a test can say "429
    first, then 200" and prove what the retry logic did.
    """

    requests: list[Recorded] = field(default_factory=list)
    routes: dict[tuple[str, str], list[tuple]] = field(default_factory=dict)
    base_url: str = ""

    def route(self, method: str, path: str, *responses: tuple) -> "JiraStub":
        self.routes[(method, path)] = list(responses)
        return self

    def calls(self, method: str, path: str) -> list[Recorded]:
        return [r for r in self.requests if r.method == method and r.path == path]

    def respond(self, request: Recorded) -> tuple[int, Any, dict[str, str]]:
        queue = self.routes.get((request.method, request.path))
        if not queue:
            return 404, {"errorMessages": [f"no stub route for {request.method} {request.path}"]}, {}
        response = queue.pop(0) if len(queue) > 1 else queue[0]
        status, payload = response[0], response[1]
        headers = response[2] if len(response) > 2 else {}
        return status, payload, headers


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _dispatch(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        path, _, query = self.path.partition("?")
        recorded = Recorded(
            method=method,
            path=path,
            query=query,
            body=json.loads(raw.decode()) if raw else None,
            headers={k.lower(): v for k, v in self.headers.items()},
        )
        stub: JiraStub = self.server.stub  # type: ignore[attr-defined]
        stub.requests.append(recorded)
        status, payload, headers = stub.respond(recorded)
        encoded = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's contract
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def log_message(self, *_args: Any) -> None:
        """Silence: the stub's chatter would bury the actual assertions."""


ISSUE_JSON = {
    "key": "CORVID-1",
    "fields": {
        "summary": "Refund endpoint accepts any authenticated user",
        "status": {"name": "To Do"},
        "labels": ["agent-found", "severity-critical", "qaas-envelope-env-1"],
        "description": {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Repro: ..."}]}],
        },
        "issuelinks": [],
        "reporter": {"displayName": "QA Bot"},
        "project": {"key": "CORVID"},
        "created": "2026-01-02T09:15:00.000+0000",
        "updated": "2026-01-02T09:15:00.000+0000",
    },
}


@pytest.fixture
def stub(monkeypatch):
    """A loopback Jira. Proxies are disabled so a CI proxy cannot intercept it."""
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setenv("NO_PROXY", "*")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    stub = JiraStub(base_url=f"http://127.0.0.1:{server.server_port}")
    server.stub = stub  # type: ignore[attr-defined]
    # A short poll interval: the default 0.5s is paid back on every teardown.
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    # Sensible defaults; a test overrides only the route it is about.
    stub.route("POST", f"{API}/issue", (201, {"id": "10001", "key": "CORVID-1"}))
    stub.route("GET", f"{API}/issue/CORVID-1", (200, ISSUE_JSON))
    try:
        yield stub
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def env_for(stub: JiraStub, **overrides: str) -> dict[str, str]:
    values = {
        "JIRA_BASE_URL": stub.base_url,
        "JIRA_EMAIL": "qa-bot@acme.example",
        "JIRA_API_TOKEN": "token-abc",
        "JIRA_PROJECT_KEY": "CORVID",
        "JIRA_SECURITY_PROJECT_KEY": "CORVIDSEC",
    }
    values.update(overrides)
    return {k: v for k, v in values.items() if v}


@pytest.fixture
def tracker(stub) -> JiraTracker:
    return JiraTracker(env=env_for(stub), timeout=5.0)


# -- configuration ----------------------------------------------------------


def test_a_tracker_with_no_environment_names_every_missing_variable():
    """Naming one missing variable at a time turns one restart into four."""
    with pytest.raises(TrackerConfigError) as exc:
        JiraTracker(env={})
    message = str(exc.value)
    for name in JiraTracker.REQUIRED_ENV:
        assert name in message
    assert JIRA_API_TOKEN_URL in message  # where to actually get the token
    assert "JIRA_SECURITY_PROJECT_KEY" in message  # and the consequence of skipping it
    assert "never from config/" in message


def test_a_partially_configured_tracker_names_only_what_is_missing():
    with pytest.raises(TrackerConfigError) as exc:
        JiraTracker(env={"JIRA_BASE_URL": "https://acme.atlassian.net", "JIRA_EMAIL": "a@b.c"})
    headline = str(exc.value).split(".")[0]
    assert "JIRA_API_TOKEN" in headline and "JIRA_PROJECT_KEY" in headline
    assert "JIRA_EMAIL" not in headline


def test_an_empty_variable_counts_as_missing():
    """An exported-but-empty var is the most common way this goes wrong."""
    with pytest.raises(TrackerConfigError, match="JIRA_API_TOKEN"):
        JiraTracker(env={**env_for(JiraStub(base_url="https://x.example")), "JIRA_API_TOKEN": "  "})


def test_a_base_url_without_a_scheme_is_rejected_at_construction():
    with pytest.raises(TrackerConfigError, match="not a URL"):
        JiraTracker(env={**env_for(JiraStub(base_url="x")), "JIRA_BASE_URL": "acme.atlassian.net"})


def test_the_backend_is_chosen_by_config(monkeypatch, tmp_path, stub):
    for name, value in env_for(stub).items():
        monkeypatch.setenv(name, value)
    built = build_tracker("jira", tmp_path)
    assert isinstance(built, JiraTracker)
    assert (built.default_project, built.security_project) == ("CORVID", "CORVIDSEC")


def test_the_security_project_is_none_when_unconfigured(stub):
    """None means 'refuse', not 'fall back to the public project'."""
    tracker = JiraTracker(env=env_for(stub, JIRA_SECURITY_PROJECT_KEY=""))
    assert tracker.security_project is None


def test_data_center_is_an_explicit_stub_pointing_at_the_difference():
    with pytest.raises(NotImplementedError) as exc:
        JiraDataCenterTracker()
    message = str(exc.value)
    assert "PAT" in message and "v2" in message
    assert "JIRA_PERSONAL_ACCESS_TOKEN" in message


# -- ADF --------------------------------------------------------------------


def test_markdown_to_adf_covers_the_house_ticket_format():
    doc = markdown_to_adf(
        "## Repro\n"
        "\n"
        "Call the endpoint twice.\n"
        "\n"
        "- as user A\n"
        "- as user B\n"
        "\n"
        "```bash\ncurl -X POST /v1/refund\n```\n"
    )
    assert doc["type"] == "doc" and doc["version"] == 1
    kinds = [node["type"] for node in doc["content"]]
    assert kinds == ["heading", "paragraph", "bulletList", "codeBlock"]

    heading, paragraph, bullets, code = doc["content"]
    assert heading["attrs"]["level"] == 2
    assert heading["content"][0]["text"] == "Repro"
    assert paragraph["content"][0]["text"] == "Call the endpoint twice."
    assert [item["content"][0]["content"][0]["text"] for item in bullets["content"]] == [
        "as user A",
        "as user B",
    ]
    assert code["attrs"]["language"] == "bash"
    assert code["content"][0]["text"] == "curl -X POST /v1/refund"


def test_markdown_to_adf_never_emits_an_empty_text_node():
    """ADF rejects a text node with an empty string, which is a 400 at Jira."""
    doc = markdown_to_adf("")
    assert doc["content"] == [{"type": "paragraph"}]
    for node in markdown_to_adf("# \n\n```\n```\n")["content"]:
        for child in node.get("content", []):
            assert child.get("text") != ""


def test_unsupported_markdown_survives_as_literal_text():
    """The converter is honest about its subset: nothing is silently dropped."""
    doc = markdown_to_adf("A **bold** [link](http://x) and | a | table |")
    assert "**bold**" in doc["content"][0]["content"][0]["text"]
    assert "| a | table |" in doc["content"][0]["content"][0]["text"]


def test_adf_round_trips_back_to_readable_text():
    assert "Repro" in adf_to_text(markdown_to_adf("## Repro\n\nbody"))


# -- create_issue -----------------------------------------------------------


def test_create_issue_posts_adf_to_the_named_project(tracker, stub):
    issue = tracker.create_issue(
        project="CORVID",
        title="Refund endpoint accepts any authenticated user",
        body="## Repro\n\n- POST /v1/refund",
        labels=["agent-found"],
        severity="critical",
        envelope_id="env-1",
        fingerprint="sha256:abc123",
        reporter="CLERK",
    )
    assert isinstance(issue, Issue)
    assert issue.key == "CORVID-1"

    posted = stub.calls("POST", f"{API}/issue")
    assert len(posted) == 1
    fields = posted[0].fields
    assert fields["project"] == {"key": "CORVID"}
    assert fields["summary"] == "Refund endpoint accepts any authenticated user"
    assert fields["issuetype"] == {"name": "Bug"}

    # The description is a document, not a string: v3 rejects a string outright.
    assert isinstance(fields["description"], dict)
    assert fields["description"]["type"] == "doc"
    assert [n["type"] for n in fields["description"]["content"]][:2] == ["heading", "bulletList"]

    # House metadata rides on labels, the one field every project has.
    assert "agent-found" in fields["labels"]
    assert "severity-critical" in fields["labels"]
    assert "qaas-envelope-env-1" in fields["labels"]
    assert "qaas-fp-abc123" in fields["labels"]

    # Basic auth, because that is what Jira Cloud API tokens use.
    assert posted[0].headers["authorization"].startswith("Basic ")


def test_create_issue_records_the_filing_agent_in_the_body(tracker, stub):
    """Jira sets `reporter` from the credential, so the agent's name must live
    somewhere that stays true."""
    tracker.create_issue(project="CORVID", title="A defect", body="Repro: x", reporter="CLERK")
    text = json.dumps(stub.calls("POST", f"{API}/issue")[0].fields["description"])
    assert "CLERK" in text


def test_create_issue_refuses_an_empty_title_without_calling_jira(tracker, stub):
    with pytest.raises(TrackerError, match="needs a title"):
        tracker.create_issue(project="CORVID", title="   ")
    assert stub.requests == []


def test_create_issue_keeps_the_key_when_the_readback_fails(tracker, stub):
    """The ticket exists the moment Jira answers the POST. Dropping the key
    because a follow-up GET failed would strand a real ticket."""
    stub.route("GET", f"{API}/issue/CORVID-1", (500, {"errorMessages": ["boom"]}))
    issue = tracker.create_issue(project="CORVID", title="A defect", body="b", severity="major")
    assert issue.key == "CORVID-1"
    assert issue.severity == "major"


def test_create_issue_is_never_retried_on_429(tracker, stub):
    """A retried create is a duplicate ticket — the exact failure this system
    exists to prevent. The 429 may arrive after Jira already filed it."""
    stub.route("POST", f"{API}/issue", (429, {"errorMessages": ["rate limit"]}, {"Retry-After": "0"}))
    with pytest.raises(TrackerError) as exc:
        tracker.create_issue(project="CORVID", title="A defect")
    assert len(stub.calls("POST", f"{API}/issue")) == 1
    assert "not safe to retry" in str(exc.value)


def test_reads_are_retried_on_429_honouring_retry_after(tracker, stub):
    """Reads are idempotent, so backing off and retrying is free."""
    stub.route(
        "GET",
        f"{API}/issue/CORVID-1",
        (429, {"errorMessages": ["slow down"]}, {"Retry-After": "0"}),
        (200, ISSUE_JSON),
    )
    assert tracker.get("CORVID-1").key == "CORVID-1"
    assert len(stub.calls("GET", f"{API}/issue/CORVID-1")) == 2


# -- reading ----------------------------------------------------------------


def test_get_maps_a_jira_issue_onto_the_house_shape(tracker):
    issue = tracker.get("CORVID-1")
    assert issue is not None
    assert issue.project == "CORVID"
    assert issue.status == "To Do"  # Jira's workflow name, not a house status
    assert issue.severity == "critical"
    assert issue.envelope_id == "env-1"
    assert "Repro" in issue.body


def test_a_fingerprint_survives_the_round_trip_through_a_label(tracker, stub):
    """Dedupe compares fingerprints for equality, so a lossy round trip would
    quietly re-file every defect it already filed."""
    digest = "sha256:" + "ab12cd34" * 8
    tracker.create_issue(project="CORVID", title="A defect", fingerprint=digest)
    label = next(
        label
        for label in stub.calls("POST", f"{API}/issue")[0].fields["labels"]
        if label.startswith("qaas-fp-")
    )
    stub.route(
        "GET",
        f"{API}/issue/CORVID-1",
        (200, {**ISSUE_JSON, "fields": {**ISSUE_JSON["fields"], "labels": [label]}}),
    )
    assert tracker.get("CORVID-1").fingerprint == digest


def test_get_returns_none_for_a_missing_issue(tracker, stub):
    stub.route("GET", f"{API}/issue/CORVID-9", (404, {"errorMessages": ["Issue does not exist"]}))
    assert tracker.get("CORVID-9") is None


def test_search_builds_scoped_jql_and_uses_the_current_endpoint(tracker, stub):
    stub.route("POST", f"{API}/search/jql", (200, {"issues": [ISSUE_JSON]}))
    found = tracker.search(text='refund "now"', status="Done", fingerprint="sha256:abc123", limit=5)
    assert [i.key for i in found] == ["CORVID-1"]

    jql = stub.calls("POST", f"{API}/search/jql")[0].body["jql"]
    assert 'project in ("CORVID", "CORVIDSEC")' in jql  # never the whole instance
    assert 'status = "Done"' in jql
    assert 'labels = "qaas-fp-abc123"' in jql
    assert 'text ~ "refund \\"now\\""' in jql  # quotes escaped, not injected
    assert jql.endswith("ORDER BY created DESC")


# -- transition -------------------------------------------------------------


TRANSITIONS = {
    "transitions": [
        {"id": "11", "name": "Start progress", "to": {"name": "In Progress"}},
        {"id": "31", "name": "Done", "to": {"name": "Done"}},
    ]
}


def test_transition_resolves_a_status_name_to_an_id(tracker, stub):
    stub.route("GET", f"{API}/issue/CORVID-1/transitions", (200, TRANSITIONS))
    stub.route("POST", f"{API}/issue/CORVID-1/transitions", (204, {}))
    stub.route("POST", f"{API}/issue/CORVID-1/comment", (201, {}))

    tracker.transition("CORVID-1", "done", by="PROOF", comment="test_refund_authz passes")

    posted = stub.calls("POST", f"{API}/issue/CORVID-1/transitions")[0]
    assert posted.body == {"transition": {"id": "31"}}

    # Who and why: the history is the audit trail (§4.12).
    comment = json.dumps(stub.calls("POST", f"{API}/issue/CORVID-1/comment")[0].body)
    assert "PROOF" in comment and "test_refund_authz" in comment


def test_transition_matches_a_house_status_against_a_real_workflow(tracker, stub):
    """`in_progress` is house vocabulary; "Start progress" is Jira's."""
    stub.route("GET", f"{API}/issue/CORVID-1/transitions", (200, TRANSITIONS))
    stub.route("POST", f"{API}/issue/CORVID-1/transitions", (204, {}))
    stub.route("POST", f"{API}/issue/CORVID-1/comment", (201, {}))
    tracker.transition("CORVID-1", "in_progress")
    assert stub.calls("POST", f"{API}/issue/CORVID-1/transitions")[0].body["transition"]["id"] == "11"


def test_an_unknown_status_lists_the_transitions_that_do_exist(tracker, stub):
    """Silently doing nothing is the worst outcome: the caller believes the
    ticket moved and stops looking at it."""
    stub.route("GET", f"{API}/issue/CORVID-1/transitions", (200, TRANSITIONS))
    with pytest.raises(TrackerError) as exc:
        tracker.transition("CORVID-1", "Awaiting Sign-off")
    message = str(exc.value)
    assert "Awaiting Sign-off" in message
    assert "'Done'" in message and "'Start progress'" in message
    assert "To Do" in message  # the status it is stuck in
    assert stub.calls("POST", f"{API}/issue/CORVID-1/transitions") == []


def test_transition_refuses_an_unknown_issue(tracker, stub):
    stub.route("GET", f"{API}/issue/CORVID-9", (404, {"errorMessages": ["nope"]}))
    with pytest.raises(UnknownIssue, match="no issue 'CORVID-9'"):
        tracker.transition("CORVID-9", "Done")


# -- link -------------------------------------------------------------------


LINK_TYPES_JSON = {
    "issueLinkTypes": [
        {"id": "10000", "name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
        {"id": "10001", "name": "Duplicate", "inward": "is duplicated by", "outward": "duplicates"},
        {"id": "10002", "name": "Relates", "inward": "relates to", "outward": "relates to"},
    ]
}


def test_link_uses_the_instances_own_link_types_and_the_right_direction(tracker, stub):
    stub.route("GET", f"{API}/issue/CORVID-2", (200, {**ISSUE_JSON, "key": "CORVID-2"}))
    stub.route("GET", f"{API}/issueLinkType", (200, LINK_TYPES_JSON))
    stub.route("POST", f"{API}/issueLink", (201, {}))

    tracker.link("CORVID-1", "CORVID-2", "duplicates")
    body = stub.calls("POST", f"{API}/issueLink")[0].body
    assert body["type"] == {"name": "Duplicate"}
    # Jira reads a link as "<outward> duplicates <inward>", so CORVID-1 — the
    # issue that duplicates the other — must be the outward end.
    assert body["outwardIssue"] == {"key": "CORVID-1"}
    assert body["inwardIssue"] == {"key": "CORVID-2"}


def test_link_refuses_an_unknown_house_link_type(tracker, stub):
    with pytest.raises(TrackerError, match="unknown link type"):
        tracker.link("CORVID-1", "CORVID-2", "supersedes")
    assert stub.requests == []


def test_link_refuses_a_dangling_target(tracker, stub):
    stub.route("GET", f"{API}/issue/CORVID-9", (404, {"errorMessages": ["nope"]}))
    with pytest.raises(UnknownIssue):
        tracker.link("CORVID-1", "CORVID-9")
    assert stub.calls("POST", f"{API}/issueLink") == []


# -- error messages ---------------------------------------------------------


def test_a_401_points_at_the_credential_variables(tracker, stub):
    stub.route("GET", f"{API}/issue/CORVID-1", (401, {"errorMessages": ["Unauthorized"]}))
    with pytest.raises(TrackerError) as exc:
        tracker.get("CORVID-1")
    message = str(exc.value)
    assert "401" in message
    assert "JIRA_EMAIL" in message and "JIRA_API_TOKEN" in message
    assert JIRA_API_TOKEN_URL in message


def test_a_403_points_at_project_permissions(tracker, stub):
    stub.route("POST", f"{API}/issue", (403, {"errorMessages": ["Forbidden"]}))
    with pytest.raises(TrackerError) as exc:
        tracker.create_issue(project="CORVID", title="A defect")
    message = str(exc.value)
    assert "403" in message
    assert "permission" in message and "Create Issues" in message
    assert "qa-bot@acme.example" in message  # which account is short of rights


def test_a_404_names_the_project_key_variables(tracker, stub):
    stub.route("POST", f"{API}/issue", (404, {"errorMessages": ["project not found"]}))
    with pytest.raises(TrackerError) as exc:
        tracker.create_issue(project="CORVID", title="A defect")
    message = str(exc.value)
    assert "404" in message
    assert "JIRA_PROJECT_KEY='CORVID'" in message
    assert "CORVIDSEC" in message


def test_the_three_failures_do_not_share_a_message(tracker, stub):
    """Distinct causes must read differently, or the operator debugs the wrong one."""
    messages = []
    for code in (401, 403, 404):
        stub.route("POST", f"{API}/issue", (code, {"errorMessages": [str(code)]}))
        with pytest.raises(TrackerError) as exc:
            tracker.create_issue(project="CORVID", title="A defect")
        messages.append(str(exc.value))
    assert len(set(messages)) == 3


def test_an_unreachable_jira_names_the_base_url(stub):
    """Port 9 is the discard protocol: nothing is listening there."""
    tracker = JiraTracker(
        env=env_for(stub, JIRA_BASE_URL="http://127.0.0.1:9"), timeout=2.0
    )
    with pytest.raises(TrackerError, match="could not reach Jira"):
        tracker.get("CORVID-1")


def test_jiras_own_error_text_is_passed_through(tracker, stub):
    stub.route(
        "POST",
        f"{API}/issue",
        (400, {"errors": {"issuetype": "The issue type 'Bug' does not exist in this project"}}),
    )
    with pytest.raises(TrackerError, match="does not exist in this project"):
        tracker.create_issue(project="CORVID", title="A defect")


# -- security routing, through the MCP server ------------------------------
# The rule lives in qaas.mcp.tracker so it holds for every backend; these
# assert it holds when the backend is Jira.


def jira_ctx(stub, tmp_path: Path, monkeypatch, **env_overrides: str) -> ToolContext:
    """A CLERK context whose tracker is a Jira pointed at the stub.

    The environment is set through monkeypatch so it is torn down with the
    test: leaking JIRA_* into the session would silently change what every
    later test's `build_tracker` does.
    """
    config = load_config(search=CONFIG_SEARCH)
    for name, value in env_for(stub, **env_overrides).items():
        monkeypatch.setenv(name, value)
    return ToolContext(
        store=RunStore.new(root=tmp_path / ".qaas"),
        maps=SystemMapStore(root=tmp_path / ".qaas"),
        config=config.model_copy(update={"tracker": "jira"}),
        agent=config.agents["CLERK"],
        target_root=REPO_ROOT,
    )


def security_envelope(ctx: ToolContext) -> DefectEnvelope:
    envelope = DefectEnvelope.model_validate(
        {
            "run_id": ctx.store.run_id,
            "discovered_by": "WARDEN",
            "domain": "security",
            "class": "vulnerability",
            "title": "Refund endpoint accepts any authenticated user",
            "summary": "POST /v1/orders/{id}/refund never checks the caller's role.",
            "severity": "critical",
            "confidence": 0.9,
            "location": {"endpoint": "POST /v1/orders/{order_id}/refund"},
            "evidence": [{"type": "log", "uri": "artifact://run/refund.log"}],
            "impact": {"security_relevant": True},
        }
    )
    ctx.store.put_envelope(envelope)
    return envelope


@pytest.fixture
def clean_jira_env(monkeypatch):
    """Keep the ambient environment out of it, both ways."""
    for name in (*JiraTracker.REQUIRED_ENV, JiraTracker.SECURITY_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("no_proxy", "*")
    yield


async def test_a_security_finding_is_filed_into_the_jira_security_project(
    stub, tmp_path, monkeypatch, clean_jira_env
):
    stub.route("POST", f"{API}/issue", (201, {"id": "1", "key": "CORVIDSEC-1"}))
    stub.route(
        "GET",
        f"{API}/issue/CORVIDSEC-1",
        (200, {**ISSUE_JSON, "key": "CORVIDSEC-1",
               "fields": {**ISSUE_JSON["fields"], "project": {"key": "CORVIDSEC"}}}),
    )
    ctx = jira_ctx(stub, tmp_path, monkeypatch)
    tools = handlers(build_tools(ctx))
    envelope = security_envelope(ctx)

    result = await tools["create_issue"](
        {"title": "Refund authz", "body": "Repro: ...", "envelope_id": envelope.id}
    )
    assert not result.get("isError"), result
    assert result["structuredContent"]["project"] == "CORVIDSEC"
    assert result["structuredContent"]["restricted"] is True
    assert stub.calls("POST", f"{API}/issue")[0].fields["project"] == {"key": "CORVIDSEC"}


async def test_a_security_finding_is_refused_when_no_security_project_is_configured(
    stub, tmp_path, monkeypatch, clean_jira_env
):
    """Refusing is the correct behaviour: a vulnerability in a project the whole
    company can read is a disclosure, and there is no undo (§4.12, §10)."""
    ctx = jira_ctx(stub, tmp_path, monkeypatch, JIRA_SECURITY_PROJECT_KEY="")
    tools = handlers(build_tools(ctx))
    envelope = security_envelope(ctx)

    result = await tools["create_issue"](
        {"title": "Refund authz", "body": "Repro: ...", "envelope_id": envelope.id}
    )
    assert result.get("isError")
    text = "\n".join(block["text"] for block in result["content"])
    assert "JIRA_SECURITY_PROJECT_KEY" in text
    assert "do not file it anywhere" in text

    # Nothing reached Jira, and the refusal is on the record.
    assert stub.calls("POST", f"{API}/issue") == []
    assert ctx.count("tickets") == 0
    assert list(ctx.store.ledger("denial"))[0].detail["tool"] == "create_issue"


async def test_an_ordinary_finding_goes_to_the_configured_default_project(
    stub, tmp_path, monkeypatch, clean_jira_env
):
    ctx = jira_ctx(stub, tmp_path, monkeypatch)
    tools = handlers(build_tools(ctx))
    result = await tools["create_issue"]({"title": "Slow list endpoint", "body": "Repro: ..."})
    assert not result.get("isError"), result
    assert stub.calls("POST", f"{API}/issue")[0].fields["project"] == {"key": "CORVID"}


async def test_asking_for_the_public_project_for_a_security_finding_is_refused(
    stub, tmp_path, monkeypatch, clean_jira_env
):
    ctx = jira_ctx(stub, tmp_path, monkeypatch)
    tools = handlers(build_tools(ctx))
    envelope = security_envelope(ctx)
    result = await tools["create_issue"](
        {"title": "Refund authz", "body": "b", "envelope_id": envelope.id, "project": "CORVID"}
    )
    assert result.get("isError")
    text = "\n".join(block["text"] for block in result["content"])
    assert "never go to a public project" in text
    assert "CORVIDSEC" in text
    assert stub.calls("POST", f"{API}/issue") == []


# -- the one test that needs a real Jira ------------------------------------


@pytest.mark.jira
def test_a_real_jira_accepts_what_this_adapter_sends():
    """Excluded by default (`-m 'not jira'`): needs JIRA_* pointed at a real
    site and files an actual ticket. A stub proves our JSON is what we think it
    is; only Jira proves Jira agrees. Run it once after configuring, with
    `pytest -m jira`."""
    tracker = JiraTracker()
    issue = tracker.create_issue(
        project=tracker.default_project,
        title="qaas adapter smoke test — safe to close",
        body="## Repro\n\n- this ticket was filed by the JiraTracker smoke test\n",
        labels=["agent-found", "qaas-smoke-test"],
        severity="trivial",
        reporter="CLERK",
    )
    assert issue.key.startswith(tracker.default_project)
    assert tracker.get(issue.key) is not None


# -- qaas tracker-check -----------------------------------------------------
# The command exists so that the first live run is not also the first time
# anyone finds out whether the configuration works. These prove it makes no
# writes, names every problem, and never renders the token.


HEALTHY_STATUSES = [
    {
        "id": "10004",
        "name": "Bug",
        "statuses": [
            {"id": "10000", "name": "To Do"},
            {"id": "3", "name": "In Progress"},
            {"id": "10001", "name": "Done"},
        ],
    }
]

ALL_PERMISSIONS = {
    "permissions": {
        name: {"havePermission": True} for name in JiraTracker.PROJECT_PERMISSIONS
    }
}


def route_healthy_jira(stub: JiraStub, statuses: Any = None) -> JiraStub:
    """Every read-only endpoint `tracker-check` touches, all answering yes."""
    stub.route("GET", f"{API}/myself", (200, {"displayName": "QA Bot", "accountId": "5b10"}))
    stub.route("GET", f"{API}/mypermissions", (200, ALL_PERMISSIONS))
    for key, name in (("CORVID", "Corvid Engineering"), ("CORVIDSEC", "Corvid Security")):
        stub.route("GET", f"{API}/project/{key}", (200, {"key": key, "name": name}))
        stub.route(
            "GET",
            f"{API}/project/{key}/statuses",
            (200, statuses if statuses is not None else HEALTHY_STATUSES),
        )
    return stub


@pytest.fixture
def jira_config(tmp_path) -> Path:
    """A copy of the shipped config with `tracker: jira`.

    A copy rather than a monkeypatched SystemConfig, because the command reads
    the backend out of system.yaml and that reading is part of what is tested.
    """
    import shutil

    dest = tmp_path / "config"
    shutil.copytree(PACKAGED_CONFIG, dest)
    system = dest / "system.yaml"
    system.write_text(system.read_text().replace("tracker: local", "tracker: jira", 1))
    return dest


@pytest.fixture
def cli_runner(monkeypatch):
    """A CliRunner with a wide terminal, so rich does not wrap the assertions."""
    from typer.testing import CliRunner

    monkeypatch.setenv("COLUMNS", "220")
    return CliRunner()


def run_check(cli_runner, jira_config: Path, *args: str):
    from qaas import cli

    return cli_runner.invoke(cli.app, ["tracker-check", "--config", str(jira_config), *args])


def test_tracker_check_names_every_missing_environment_variable(
    cli_runner, jira_config, stub, clean_jira_env
):
    """One name at a time turns one restart into four. And nothing is contacted:
    there was nothing to contact with."""
    result = run_check(cli_runner, jira_config)
    assert result.exit_code == 1, result.output
    for name in JiraTracker.REQUIRED_ENV:
        assert name in result.output
    assert "MISSING" in result.output
    assert "not ready" in result.output
    assert stub.requests == []


def test_tracker_check_never_renders_the_token(
    cli_runner, jira_config, stub, monkeypatch, clean_jira_env
):
    secret = "atatt-do-not-print-me-wxyz"
    for name, value in env_for(stub, JIRA_API_TOKEN=secret).items():
        monkeypatch.setenv(name, value)
    route_healthy_jira(stub)

    result = run_check(cli_runner, jira_config)
    assert secret not in result.output
    assert "ends ...wxyz" in result.output  # enough to tell two tokens apart, useless alone


def test_tracker_check_reports_a_good_configuration_as_ready(
    cli_runner, jira_config, stub, monkeypatch, clean_jira_env
):
    for name, value in env_for(stub).items():
        monkeypatch.setenv(name, value)
    route_healthy_jira(stub)

    result = run_check(cli_runner, jira_config)
    assert result.exit_code == 0, result.output
    assert "ready" in result.output
    assert "QA Bot" in result.output          # who the credentials belong to
    assert "Corvid Engineering" in result.output
    assert "Corvid Security" in result.output  # the restricted project is checked too
    assert "In Progress" in result.output      # the workflow it will transition through
    # A check that files something is not a check.
    assert [r for r in stub.requests if r.method != "GET"] == []


def test_tracker_check_reports_an_unmappable_house_status_as_a_problem(
    cli_runner, jira_config, stub, monkeypatch, clean_jira_env
):
    """A workflow with nowhere to land 'resolved' fails at closing time, which
    is exactly when nobody is watching."""
    for name, value in env_for(stub).items():
        monkeypatch.setenv(name, value)
    route_healthy_jira(
        stub,
        statuses=[
            {
                "name": "Bug",
                "statuses": [{"name": "To Do"}, {"name": "In Progress"}, {"name": "Awaiting Sign-off"}],
            }
        ],
    )

    result = run_check(cli_runner, jira_config)
    assert result.exit_code == 1, result.output
    assert "resolved" in result.output and "closed" in result.output
    assert "Awaiting Sign-off" in result.output  # what the project does offer


def test_tracker_check_names_a_missing_issue_type_and_what_the_project_has(
    cli_runner, jira_config, stub, monkeypatch, clean_jira_env
):
    for name, value in env_for(stub, JIRA_ISSUE_TYPE="Defect").items():
        monkeypatch.setenv(name, value)
    route_healthy_jira(stub)

    result = run_check(cli_runner, jira_config)
    assert result.exit_code == 1, result.output
    assert "Defect" in result.output and "JIRA_ISSUE_TYPE" in result.output


def test_tracker_check_names_the_permissions_the_account_lacks(
    cli_runner, jira_config, stub, monkeypatch, clean_jira_env
):
    for name, value in env_for(stub).items():
        monkeypatch.setenv(name, value)
    route_healthy_jira(stub)
    stub.route(
        "GET",
        f"{API}/mypermissions",
        (200, {"permissions": {"BROWSE_PROJECTS": {"havePermission": True}}}),
    )

    result = run_check(cli_runner, jira_config)
    assert result.exit_code == 1, result.output
    assert "CREATE_ISSUES" in result.output and "TRANSITION_ISSUES" in result.output


def test_tracker_check_warns_that_security_findings_will_be_refused(
    cli_runner, jira_config, stub, monkeypatch, clean_jira_env
):
    """Unset is a supported posture, not a broken one: it refuses rather than
    discloses. So it warns, and still exits zero."""
    for name, value in env_for(stub, JIRA_SECURITY_PROJECT_KEY="").items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("JIRA_SECURITY_PROJECT_KEY", raising=False)
    route_healthy_jira(stub)

    result = run_check(cli_runner, jira_config)
    assert result.exit_code == 0, result.output
    assert "JIRA_SECURITY_PROJECT_KEY" in result.output
    assert "REFUSED" in result.output


def test_dry_run_ticket_prints_the_real_payload_and_sends_nothing(
    cli_runner, jira_config, stub, monkeypatch, clean_jira_env
):
    for name, value in env_for(stub).items():
        monkeypatch.setenv(name, value)
    route_healthy_jira(stub)

    result = run_check(cli_runner, jira_config, "--dry-run-ticket")
    assert result.exit_code == 0, result.output
    assert '"type": "doc"' in result.output      # ADF, not a plain string
    assert '"heading"' in result.output
    assert "severity-critical" in result.output
    assert "qaas-envelope-env-sample-0001" in result.output
    assert '"name": "Bug"' in result.output
    # The whole point: a preview that posts is not a preview.
    assert [r for r in stub.requests if r.method == "POST"] == []


def test_dry_run_ticket_renders_what_create_issue_would_actually_send(tracker, stub):
    """The preview and the real payload must be the same code path, or the
    preview is correct only until the day it drifts."""
    preview = tracker.create_payload(project="CORVID", title="A defect", body="## Repro\n\n- x")
    tracker.create_issue(project="CORVID", title="A defect", body="## Repro\n\n- x")
    assert stub.calls("POST", f"{API}/issue")[0].body == preview


def test_tracker_check_on_the_local_backend_needs_no_credentials(cli_runner, tmp_path, clean_jira_env):
    from qaas import cli

    result = cli_runner.invoke(
        cli.app, ["tracker-check", "--config", str(PACKAGED_CONFIG), "--root", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "local" in result.output and "ready" in result.output


# -- QAAS_TRACKER_DRY_RUN ---------------------------------------------------
# The rail for first contact: the policy above still runs, the ledger still
# records what would have happened, and nothing reaches Jira.


async def test_dry_run_creates_nothing_and_says_so(stub, tmp_path, monkeypatch, clean_jira_env):
    monkeypatch.setenv("QAAS_TRACKER_DRY_RUN", "1")
    ctx = jira_ctx(stub, tmp_path, monkeypatch)
    tools = handlers(build_tools(ctx))

    result = await tools["create_issue"]({"title": "Slow list endpoint", "body": "Repro: ..."})
    assert not result.get("isError"), result

    text = "\n".join(block["text"] for block in result["content"])
    assert "NOTHING WAS FILED" in text
    assert "Do not report this as a filed ticket" in text
    assert result["structuredContent"]["filed"] is False
    assert result["structuredContent"]["dry_run"] is True

    # A key an agent can carry through the rest of its run without choking.
    assert result["structuredContent"]["key"].startswith("CORVID-")

    assert stub.calls("POST", f"{API}/issue") == []
    entries = list(ctx.store.ledger("dry_run"))
    assert [e.detail["action"] for e in entries] == ["create_issue"]
    assert entries[0].detail["project"] == "CORVID"
    # Never as a `ticket`: anything counting filed tickets must not count this.
    assert list(ctx.store.ledger("ticket")) == []


async def test_dry_run_still_routes_security_findings_and_still_refuses(
    stub, tmp_path, monkeypatch, clean_jira_env
):
    """A rehearsal that skipped the routing rule would rehearse the wrong run."""
    monkeypatch.setenv("QAAS_TRACKER_DRY_RUN", "1")
    ctx = jira_ctx(stub, tmp_path, monkeypatch, JIRA_SECURITY_PROJECT_KEY="")
    tools = handlers(build_tools(ctx))
    envelope = security_envelope(ctx)

    refused = await tools["create_issue"](
        {"title": "Refund authz", "body": "b", "envelope_id": envelope.id}
    )
    assert refused.get("isError")
    assert "do not file it anywhere" in "\n".join(b["text"] for b in refused["content"])


async def test_dry_run_routes_a_security_finding_to_the_restricted_project(
    stub, tmp_path, monkeypatch, clean_jira_env
):
    monkeypatch.setenv("QAAS_TRACKER_DRY_RUN", "1")
    ctx = jira_ctx(stub, tmp_path, monkeypatch)
    tools = handlers(build_tools(ctx))
    envelope = security_envelope(ctx)

    result = await tools["create_issue"](
        {"title": "Refund authz", "body": "b", "envelope_id": envelope.id}
    )
    assert not result.get("isError"), result
    assert result["structuredContent"]["project"] == "CORVIDSEC"
    assert result["structuredContent"]["restricted"] is True
    assert stub.calls("POST", f"{API}/issue") == []


async def test_dry_run_transition_and_link_send_nothing(stub, tmp_path, monkeypatch, clean_jira_env):
    monkeypatch.setenv("QAAS_TRACKER_DRY_RUN", "1")
    ctx = jira_ctx(stub, tmp_path, monkeypatch)
    # PROOF, not CLERK: §8.1 gives the transition right to the agent that
    # verifies a fix, and the dry run must not paper over the policy.
    ctx.agent = load_config(search=CONFIG_SEARCH).agents["PROOF"]
    tools = handlers(build_tools(ctx))

    moved = await tools["transition"](
        {"key": "CORVID-1", "status": "resolved", "comment": "test passes"}
    )
    assert not moved.get("isError"), moved
    assert "NOTHING WAS FILED" in "\n".join(b["text"] for b in moved["content"])
    assert moved["structuredContent"]["filed"] is False

    linked = await tools["link"]({"key": "CORVID-1", "to": "CORVID-2", "type": "duplicates"})
    assert not linked.get("isError"), linked
    assert linked["structuredContent"]["filed"] is False

    # Not one call reached Jira — not even the read-back a live transition does.
    assert stub.requests == []
    assert {e.detail["action"] for e in ctx.store.ledger("dry_run")} == {"transition", "link"}


async def test_without_the_flag_nothing_changes(stub, tmp_path, monkeypatch, clean_jira_env):
    """The rail is off by default, and the default path is still the live one."""
    monkeypatch.delenv("QAAS_TRACKER_DRY_RUN", raising=False)
    ctx = jira_ctx(stub, tmp_path, monkeypatch)
    tools = handlers(build_tools(ctx))
    result = await tools["create_issue"]({"title": "Slow list endpoint", "body": "Repro: ..."})
    assert not result.get("isError"), result
    assert len(stub.calls("POST", f"{API}/issue")) == 1
    assert "dry_run" not in result["structuredContent"]


# -- per-repository boards --------------------------------------------------
#
# One board per repository, inside one Jira project. The board is a filter over
# a `repo-<target>` label rather than a project of its own, because creating a
# project needs administrator rights that a bot account does not have.

AGILE = "/rest/agile/1.0"


def test_a_target_name_becomes_a_label_and_an_unusable_one_becomes_nothing():
    """None, not a mangled value. A mangled label files fine and never matches
    the board filter, so the ticket exists and is invisible."""
    assert repo_label("Claude-Code-Training") == "repo-claude-code-training"
    assert repo_label("my repo") == "repo-my-repo"
    assert repo_label("") is None
    assert repo_label(None) is None


def route_no_existing_board(stub: JiraStub) -> None:
    stub.route("GET", f"{API}/myself", (200, {"accountId": "acct-1", "displayName": "QA bot"}))
    stub.route("GET", f"{API}/filter/search", (200, {"values": []}))
    stub.route("POST", f"{API}/filter", (200, {"id": "10100", "name": "x"}))
    stub.route("GET", f"{AGILE}/board", (200, {"values": []}))
    stub.route("POST", f"{AGILE}/board", (200, {"id": 42, "name": "x"}))


def test_ensure_repo_board_creates_a_shared_filter_and_a_board_over_it(tracker, stub):
    route_no_existing_board(stub)

    info = tracker.ensure_repo_board("claude-code-training")

    assert info.label == "repo-claude-code-training"
    assert info.filter_id == 10100 and info.created_filter
    assert info.board_id == 42 and info.created_board
    assert info.url.endswith("/boards/42")

    body = stub.calls("POST", f"{API}/filter")[0].body
    assert 'labels = "repo-claude-code-training"' in body["jql"]
    assert 'project = "CORVID"' in body["jql"]
    # Jira refuses to build a board over a private filter, and says so on the
    # *board* call — two steps from the cause.
    assert body["sharePermissions"] == [{"type": "authenticated"}]
    assert stub.calls("POST", f"{AGILE}/board")[0].body["filterId"] == 10100


def test_a_second_run_against_the_same_repo_reuses_the_board(tracker, stub):
    """Idempotence is the whole requirement: this is called at the top of every
    run, and the second run must not produce `repo QA (2)`."""
    stub.route("GET", f"{API}/myself", (200, {"accountId": "acct-1"}))
    name = "claude-code-training — QA (qaas)"
    stub.route("GET", f"{API}/filter/search", (200, {"values": [{"id": "10100", "name": name}]}))
    stub.route("GET", f"{AGILE}/board", (200, {"values": [{"id": 42, "name": name}]}))

    info = tracker.ensure_repo_board("claude-code-training")

    assert (info.filter_id, info.board_id) == (10100, 42)
    assert not info.created_filter and not info.created_board
    assert stub.calls("POST", f"{API}/filter") == []
    assert stub.calls("POST", f"{AGILE}/board") == []


def test_a_refused_board_still_returns_a_usable_filter(tracker, stub):
    """Team-managed projects own their boards and reject this. The run's job is
    to find defects; losing them over a board would be the larger failure."""
    stub.route("GET", f"{API}/myself", (200, {"accountId": "acct-1"}))
    stub.route("GET", f"{API}/filter/search", (200, {"values": []}))
    stub.route("POST", f"{API}/filter", (200, {"id": "10100"}))
    stub.route("GET", f"{AGILE}/board", (200, {"values": []}))
    stub.route("POST", f"{AGILE}/board", (400, {"errorMessages": ["board create not allowed"]}))

    info = tracker.ensure_repo_board("claude-code-training")

    assert info.board_id is None
    assert info.filter_id == 10100
    assert "filter=10100" in info.url
    assert info.note and "repo-claude-code-training" in info.note


def test_a_target_name_that_cannot_be_a_label_is_refused_loudly(tracker, stub):
    with pytest.raises(TrackerError):
        tracker.ensure_repo_board("   ")


async def test_every_filed_ticket_carries_its_repository_label(stub, tmp_path, monkeypatch, clean_jira_env):
    """Stamped by the system, not asked of the agent. It is the only thing that
    puts the ticket on that repository's board."""
    ctx = jira_ctx(stub, tmp_path, monkeypatch)
    ctx = ToolContext(
        store=ctx.store, maps=ctx.maps, agent=ctx.agent, target_root=ctx.target_root,
        config=ctx.config.model_copy(update={"target": "claude-code-training"}),
    )
    tools = handlers(build_tools(ctx))

    result = await tools["create_issue"]({"title": "A defect", "body": "Repro: ..."})

    assert not result.get("isError"), result
    assert "repo-claude-code-training" in stub.calls("POST", f"{API}/issue")[0].fields["labels"]
