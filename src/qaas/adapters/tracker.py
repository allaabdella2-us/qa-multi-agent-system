"""Tracker adapters — the issue tracker behind one interface.

The MCP server in `qaas.mcp.tracker` holds the policy (who may file, how many,
where security findings go). This module holds only storage mechanics, so that
pointing the system at real Jira is a new subclass and nothing else. That split
matters: guardrails that live in the adapter would have to be re-implemented,
and re-audited, for every backend.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# The house project keys. `SECURITY_PROJECT` is the restricted one (§4.12 step 5,
# §10 "security findings leak into public tickets"); it is defined here so the
# adapter and the server cannot drift apart about what "restricted" means.
DEFAULT_PROJECT = "CORVID"
SECURITY_PROJECT = "CORVID-SEC"

# A closed vocabulary, so an agent that invents a status gets the list back and
# retries rather than writing a state nothing downstream can interpret.
STATUSES = ("open", "in_progress", "in_review", "resolved", "closed", "wont_fix", "duplicate")
LINK_TYPES = ("duplicates", "relates", "blocks", "blocked-by", "regression-of", "caused-by")

_KEY_RE = re.compile(r"^(?P<project>[A-Z][A-Z0-9-]*)-(?P<number>\d+)$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TrackerError(Exception):
    """Anything the caller could have avoided: bad key, bad status, bad link."""


class UnknownIssue(TrackerError):
    """The referenced issue key does not exist in this tracker."""


class TrackerConfigError(TrackerError):
    """The tracker itself is misconfigured — wrong credentials, missing settings.

    Raised at construction, never mid-run. A tracker that only discovers it
    cannot reach Jira after twenty findings have been produced has thrown away
    the run: the findings are gone from context and nobody can act on them.
    """


class Transition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at: datetime = Field(default_factory=_utcnow)
    status: str
    by: str | None = None
    comment: str = ""


class IssueLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    to: str


class Issue(BaseModel):
    """One tracker issue, in the shape both backends agree to speak."""

    model_config = ConfigDict(extra="forbid")

    key: str
    project: str
    title: str
    body: str = ""
    status: str = "open"
    labels: list[str] = Field(default_factory=list)
    severity: str | None = None
    envelope_id: str | None = None
    fingerprint: str | None = None
    reporter: str | None = None
    links: list[IssueLink] = Field(default_factory=list)
    history: list[Transition] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    @property
    def number(self) -> int:
        match = _KEY_RE.match(self.key)
        return int(match.group("number")) if match else 0


class TrackerAdapter(ABC):
    """The five operations the system needs from any tracker."""

    @abstractmethod
    def create_issue(
        self,
        *,
        project: str,
        title: str,
        body: str = "",
        labels: list[str] | None = None,
        severity: str | None = None,
        envelope_id: str | None = None,
        fingerprint: str | None = None,
        reporter: str | None = None,
    ) -> Issue:
        """File a new issue and return it, key assigned."""

    @abstractmethod
    def transition(self, key: str, status: str, *, by: str | None = None, comment: str = "") -> Issue:
        """Move an issue to a new status, recording who and why."""

    @abstractmethod
    def link(self, key: str, to: str, link_type: str = "relates") -> Issue:
        """Relate two existing issues."""

    @abstractmethod
    def search(
        self,
        *,
        text: str | None = None,
        project: str | None = None,
        status: str | None = None,
        label: str | None = None,
        envelope_id: str | None = None,
        fingerprint: str | None = None,
        limit: int = 20,
    ) -> list[Issue]:
        """Return matching issues, newest first."""

    @abstractmethod
    def get(self, key: str) -> Issue | None:
        """One issue by key, or None."""

    # -- routing surface ---------------------------------------------------
    # The MCP server decides *whether* a finding is restricted (§4.12 step 5);
    # the adapter decides *what the projects are called*, because a real Jira's
    # keys come from the deployment, not from a constant in this file. Asking
    # the adapter keeps one routing path for both backends instead of two.

    @property
    def default_project(self) -> str:
        """Where an ordinary finding is filed when the caller names no project."""
        return DEFAULT_PROJECT

    @property
    def security_project(self) -> str | None:
        """The restricted project, or None if this backend has none configured.

        None is not "file it somewhere else": it means the caller must refuse.
        A security finding in a public project cannot be un-disclosed (§10).
        """
        return SECURITY_PROJECT

    # -- shared validation ------------------------------------------------
    # Kept on the base class so every backend rejects the same inputs with the
    # same message; an agent should not have to learn two dialects.

    @staticmethod
    def _check_status(status: str) -> str:
        if status not in STATUSES:
            raise TrackerError(f"unknown status '{status}'; use one of: {', '.join(STATUSES)}")
        return status

    @staticmethod
    def _check_link_type(link_type: str) -> str:
        if link_type not in LINK_TYPES:
            raise TrackerError(
                f"unknown link type '{link_type}'; use one of: {', '.join(LINK_TYPES)}"
            )
        return link_type


class LocalTracker(TrackerAdapter):
    """Issues as JSON files under `<root>/tickets/`.

    The key sequence is shared across projects and derived from what is already
    on disk, so keys stay monotonic and unique across runs and across processes
    without a database. Reusing a key would silently rewrite a ticket an
    engineer may already be looking at, so allocation never counts down.
    """

    def __init__(self, root: Path | str):
        self.dir = Path(root) / "tickets"
        self.dir.mkdir(parents=True, exist_ok=True)

    # -- storage ----------------------------------------------------------

    def _path(self, key: str) -> Path:
        if not _KEY_RE.match(key):
            raise TrackerError(f"'{key}' is not an issue key; expected e.g. {DEFAULT_PROJECT}-1")
        return self.dir / f"{key}.json"

    def _write(self, issue: Issue) -> Issue:
        self._path(issue.key).write_text(issue.model_dump_json(indent=2))
        return issue

    def get(self, key: str) -> Issue | None:
        path = self._path(key)
        return Issue.model_validate_json(path.read_text()) if path.exists() else None

    def _require(self, key: str) -> Issue:
        issue = self.get(key)
        if issue is None:
            raise UnknownIssue(f"no issue '{key}' in the tracker")
        return issue

    def issues(self) -> list[Issue]:
        found = []
        for path in self.dir.glob("*.json"):
            if _KEY_RE.match(path.stem):
                found.append(Issue.model_validate_json(path.read_text()))
        return sorted(found, key=lambda i: i.number)

    def _next_key(self, project: str) -> str:
        highest = max((i.number for i in self.issues()), default=0)
        return f"{project}-{highest + 1}"

    # -- operations -------------------------------------------------------

    def create_issue(
        self,
        *,
        project: str,
        title: str,
        body: str = "",
        labels: list[str] | None = None,
        severity: str | None = None,
        envelope_id: str | None = None,
        fingerprint: str | None = None,
        reporter: str | None = None,
    ) -> Issue:
        if not title.strip():
            raise TrackerError("an issue needs a title")
        issue = Issue(
            key=self._next_key(project),
            project=project,
            title=title.strip(),
            body=body,
            labels=sorted(set(labels or [])),
            severity=severity,
            envelope_id=envelope_id,
            fingerprint=fingerprint,
            reporter=reporter,
            history=[Transition(status="open", by=reporter, comment="filed")],
        )
        return self._write(issue)

    def transition(self, key: str, status: str, *, by: str | None = None, comment: str = "") -> Issue:
        self._check_status(status)
        issue = self._require(key)
        if issue.status == status:
            raise TrackerError(f"{key} is already '{status}'")
        issue.status = status
        issue.updated_at = _utcnow()
        issue.history.append(Transition(status=status, by=by, comment=comment))
        return self._write(issue)

    def link(self, key: str, to: str, link_type: str = "relates") -> Issue:
        self._check_link_type(link_type)
        if key == to:
            raise TrackerError("an issue cannot be linked to itself")
        issue = self._require(key)
        self._require(to)  # refuse dangling links: a link to nothing is worse than none
        if not any(link.type == link_type and link.to == to for link in issue.links):
            issue.links.append(IssueLink(type=link_type, to=to))
            issue.updated_at = _utcnow()
        return self._write(issue)

    def search(
        self,
        *,
        text: str | None = None,
        project: str | None = None,
        status: str | None = None,
        label: str | None = None,
        envelope_id: str | None = None,
        fingerprint: str | None = None,
        limit: int = 20,
    ) -> list[Issue]:
        needle = (text or "").lower().strip()
        found = []
        for issue in self.issues():
            if project and issue.project != project:
                continue
            if status and issue.status != status:
                continue
            if label and label not in issue.labels:
                continue
            if envelope_id and issue.envelope_id != envelope_id:
                continue
            if fingerprint and issue.fingerprint != fingerprint:
                continue
            if needle and needle not in f"{issue.title}\n{issue.body}".lower():
                continue
            found.append(issue)
        found.reverse()  # newest first
        return found[: max(1, limit)]


# -- Atlassian Document Format ---------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.+)$")
_FENCE_RE = re.compile(r"^```\s*([A-Za-z0-9_+#.-]*)\s*$")


def _adf_text(text: str) -> list[dict[str, Any]]:
    """ADF forbids an empty text node, so an empty string yields no children."""
    return [{"type": "text", "text": text}] if text else []


def _adf_paragraph(text: str) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "paragraph"}
    children = _adf_text(text)
    if children:
        node["content"] = children
    return node


def markdown_to_adf(text: str) -> dict[str, Any]:
    """Convert the markdown subset the house ticket format uses into ADF.

    Jira Cloud's REST v3 rejects a plain string for `description` — it wants an
    Atlassian Document Format node tree — so something has to do this. Rather
    than take a markdown dependency for one field, this handles exactly the
    block constructs the house ticket format actually emits, and is explicit
    about the rest.

    Supported:
      * ATX headings `#` through `######`      -> heading nodes, levels 1-6
      * blank-line separated paragraphs        -> paragraph nodes
      * `-`, `*` or `+` bullet lists           -> bulletList / listItem
      * ``` fenced code, optional language     -> codeBlock

    NOT supported, deliberately. These are left as literal characters in the
    text rather than dropped, because a reader can still understand
    `**blocker**` or `[log](artifact://x)`, but cannot understand a body with
    pieces silently missing:
      * inline marks — bold, italic, inline code, links
      * ordered lists, nested lists, task lists
      * tables, block quotes, images, horizontal rules, footnotes
      * raw HTML

    A ticket that needs richer rendering should link to an artifact instead.
    """
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    content: list[dict[str, Any]] = []
    paragraph: list[str] = []
    bullets: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            content.append(_adf_paragraph("\n".join(paragraph)))
            paragraph.clear()

    def flush_bullets() -> None:
        if bullets:
            content.append(
                {
                    "type": "bulletList",
                    "content": [
                        {"type": "listItem", "content": [_adf_paragraph(item)]} for item in bullets
                    ],
                }
            )
            bullets.clear()

    index = 0
    while index < len(lines):
        line = lines[index]
        fence = _FENCE_RE.match(line.strip())
        if fence:
            flush_paragraph()
            flush_bullets()
            index += 1
            body: list[str] = []
            while index < len(lines) and not _FENCE_RE.match(lines[index].strip()):
                body.append(lines[index])
                index += 1
            index += 1  # step over the closing fence, or past the end if unterminated
            node: dict[str, Any] = {"type": "codeBlock"}
            if fence.group(1):
                node["attrs"] = {"language": fence.group(1)}
            children = _adf_text("\n".join(body))
            if children:
                node["content"] = children
            content.append(node)
            continue

        index += 1
        heading = _HEADING_RE.match(line.strip())
        if heading:
            flush_paragraph()
            flush_bullets()
            content.append(
                {
                    "type": "heading",
                    "attrs": {"level": len(heading.group(1))},
                    "content": _adf_text(heading.group(2).strip()),
                }
            )
            continue

        bullet = _BULLET_RE.match(line)
        if bullet:
            flush_paragraph()
            bullets.append(bullet.group(1).strip())
            continue

        if not line.strip():
            flush_paragraph()
            flush_bullets()
            continue

        flush_bullets()
        paragraph.append(line.rstrip())

    flush_paragraph()
    flush_bullets()
    if not content:
        content.append({"type": "paragraph"})
    return {"type": "doc", "version": 1, "content": content}


def adf_to_text(node: Any) -> str:
    """Flatten an ADF tree back to plain text, best effort.

    Only used when reading an issue back out of Jira, so that `Issue.body` is
    something an agent can grep. It is lossy on purpose — round-tripping ADF is
    not a goal, and pretending otherwise would invite callers to trust it.
    """
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "\n".join(part for part in (adf_to_text(child) for child in node) if part)
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    if kind == "text":
        return str(node.get("text", ""))
    if kind == "hardBreak":
        return "\n"
    inner = adf_to_text(node.get("content", []))
    if kind in ("paragraph", "heading", "codeBlock", "listItem", "blockquote"):
        return inner
    return inner


# -- Jira Cloud -------------------------------------------------------------

#: Where a human goes to mint the credential this adapter needs. Named in the
#: configuration error, because "JIRA_API_TOKEN is missing" without this line
#: sends the reader to a search engine.
JIRA_API_TOKEN_URL = "https://id.atlassian.com/manage-profile/security/api-tokens"

JIRA_API_BASE = "/rest/api/3"

#: The Agile (board and sprint) API. A different base path, not a different
#: host — boards simply do not exist under `/rest/api/3`, and asking for one
#: there returns a 404 that reads like a missing board rather than a missing
#: endpoint.
JIRA_AGILE_BASE = "/rest/agile/1.0"

JIRA_TIMEOUT_S = 30.0
#: Reads are retried on 429; see `JiraTracker._request` for why writes are not.
JIRA_READ_ATTEMPTS = 3
JIRA_MAX_RETRY_WAIT_S = 30.0

#: House metadata rides on labels because they are the only field guaranteed to
#: exist in every Jira project. Custom fields differ per instance and screen, so
#: writing to one is the fastest way to a 400 on someone else's Jira.
SEVERITY_LABEL_PREFIX = "severity-"
ENVELOPE_LABEL_PREFIX = "qaas-envelope-"
FINGERPRINT_LABEL_PREFIX = "qaas-fp-"

#: Every ticket a run files carries `repo-<target>`. It is what makes a
#: per-repository board possible without a per-repository *project*: the board
#: is a saved filter over this label, and creating a filter needs no
#: administrator rights while creating a project does.
REPO_LABEL_PREFIX = "repo-"

#: House status -> the Jira workflow names it plausibly means. Jira workflows
#: are per-project and unknowable from here, so this is a set of candidates to
#: try, never an assertion; a miss returns the real transition list (see
#: `transition`) rather than guessing.
JIRA_STATUS_ALIASES: dict[str, tuple[str, ...]] = {
    "open": ("open", "to do", "todo", "backlog", "new", "reopened"),
    "in_progress": ("in progress", "in development", "doing", "start progress"),
    "in_review": ("in review", "code review", "review", "in code review"),
    "resolved": ("resolved", "done", "fixed", "resolve issue"),
    "closed": ("closed", "done", "close issue"),
    "wont_fix": ("won't fix", "wont fix", "will not do", "won't do", "declined"),
    "duplicate": ("duplicate", "duplicated", "closed as duplicate"),
}

#: House link type -> (Jira link type names to try in order, direction). The
#: direction says which end of the Jira link `key` sits on: "outward" means the
#: link reads `key <outward phrase> to`.
JIRA_LINK_TYPES: dict[str, tuple[tuple[str, ...], str]] = {
    "duplicates": (("Duplicate", "Duplicates"), "outward"),
    "relates": (("Relates", "Related"), "outward"),
    "blocks": (("Blocks", "Blocker"), "outward"),
    "blocked-by": (("Blocks", "Blocker"), "inward"),
    "regression-of": (("Problem/Incident", "Causes", "Relates"), "inward"),
    "caused-by": (("Problem/Incident", "Causes", "Relates"), "inward"),
}


def _fingerprint_label_value(fingerprint: str | None) -> str | None:
    """The digest half of a fingerprint, e.g. `sha256:ab12…` -> `ab12…`.

    The algorithm prefix is dropped because a colon in a Jira label is not
    worth betting a 400 on. The digest itself is kept whole: dedupe matches on
    exact equality, and a truncated fingerprint would quietly collide.
    """
    digest = (fingerprint or "").split(":")[-1].strip()
    return digest or None


def _label_safe(value: str | None, prefix: str = "") -> str | None:
    """A Jira label, or None if the value cannot be one.

    Jira rejects labels containing whitespace, and caps them at 255 characters.
    Silently mangling an id would produce a label that never matches on the way
    back out, so an unusable value produces no label at all.
    """
    if not value:
        return None
    candidate = f"{prefix}{value.strip()}"
    if not candidate or any(char.isspace() for char in candidate) or len(candidate) > 255:
        return None
    return candidate


def repo_label(slug: str | None) -> str | None:
    """The label every ticket from a run against `slug` carries, or None.

    None when the slug cannot survive being a Jira label at all (whitespace,
    empty, too long). Returning None rather than a mangled value is deliberate:
    a mangled label would never match the board filter, so the tickets would
    file successfully and then be invisible on the board someone was told to
    watch — the worst of the three outcomes.
    """
    return _label_safe(_label_slug(slug), REPO_LABEL_PREFIX)


def _label_slug(value: str | None) -> str | None:
    """`My Repo.git` -> `my-repo`. The same shape `qaas init` gives a target."""
    if not value:
        return None
    slug = re.sub(r"[^a-z0-9-]+", "-", value.strip().lower()).strip("-")[:64]
    return slug or None


@dataclass(frozen=True)
class BoardInfo:
    """What a per-repository board provisioning attempt produced.

    `board_id` is None when Jira refused to create a board — which happens on
    team-managed projects, where boards belong to the project and cannot be
    made over an arbitrary filter. That is not a failure of the run: the filter
    still exists, the tickets still carry the label, and `url` still points at
    something a human can open. `note` says which of the two they got.
    """

    slug: str
    label: str
    jql: str
    filter_id: int | None = None
    filter_name: str = ""
    board_id: int | None = None
    board_name: str = ""
    url: str = ""
    created_filter: bool = False
    created_board: bool = False
    note: str | None = None


class JiraTracker(TrackerAdapter):
    """Jira Cloud, over REST API v3, with credentials from the environment.

    Everything this adapter needs is read from environment variables at
    construction and validated there: a tracker that only discovers it cannot
    authenticate after a run has produced twenty findings has destroyed the
    run, because the findings live in an agent's context and the context is
    gone. Credentials never come from `config/` — that directory is committed.

    Required:
      * ``JIRA_BASE_URL``   — e.g. ``https://acme.atlassian.net``
      * ``JIRA_EMAIL``      — the bot account's Atlassian account email
      * ``JIRA_API_TOKEN``  — an API token, not a password (Jira Cloud uses
        HTTP Basic with email + token)
      * ``JIRA_PROJECT_KEY``— the default project, e.g. ``CORVID``

    Optional:
      * ``JIRA_SECURITY_PROJECT_KEY`` — the restricted project. When it is
        unset, `security_project` is None and the MCP server refuses to file
        security findings at all. That refusal is the point: filing a
        vulnerability into a project the whole company can read is a
        disclosure, and there is no undo (§4.12 step 5, §10).
      * ``JIRA_ISSUE_TYPE`` — the issue type to create, default ``Bug``. Not
        every project has a type called Bug.

    Jira Server / Data Center is a different product with different auth; see
    `JiraDataCenterTracker` (§5.1).
    """

    REQUIRED_ENV = (
        "JIRA_BASE_URL",
        "JIRA_EMAIL",
        "JIRA_API_TOKEN",
        "JIRA_PROJECT_KEY",
    )
    SECURITY_ENV = "JIRA_SECURITY_PROJECT_KEY"
    ISSUE_TYPE_ENV = "JIRA_ISSUE_TYPE"

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        timeout: float = JIRA_TIMEOUT_S,
    ):
        source: Mapping[str, str] = os.environ if env is None else env
        values = {name: (source.get(name) or "").strip() for name in self.REQUIRED_ENV}
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise TrackerConfigError(self._missing_env_message(missing))

        base_url = values["JIRA_BASE_URL"].rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise TrackerConfigError(
                f"JIRA_BASE_URL is '{base_url}', which is not a URL. It must include the "
                "scheme and be your Jira site root, e.g. https://acme.atlassian.net "
                "(no /jira, no /rest/api path)."
            )

        self.base_url = base_url
        self.email = values["JIRA_EMAIL"]
        self._token = values["JIRA_API_TOKEN"]
        self._project = values["JIRA_PROJECT_KEY"]
        self._security_project = (source.get(self.SECURITY_ENV) or "").strip() or None
        self.issue_type = (source.get(self.ISSUE_TYPE_ENV) or "").strip() or "Bug"
        self.timeout = timeout
        # Built once, at construction, so proxy settings are read from the
        # environment the tracker was configured in rather than per call.
        self._opener = urllib.request.build_opener()
        self._link_type_cache: list[dict[str, Any]] | None = None
        self._account_id_cache: str | None = None

    @classmethod
    def _missing_env_message(cls, missing: list[str]) -> str:
        """Name every missing variable, and say where the token comes from.

        Listing only the first missing variable turns one restart into four.
        """
        verb = "is" if len(missing) == 1 else "are"
        return (
            f"JiraTracker is not configured: {', '.join(missing)} {verb} unset or empty in "
            f"the environment. Set all of {', '.join(cls.REQUIRED_ENV)} — JIRA_BASE_URL is "
            "your site root (https://acme.atlassian.net), JIRA_EMAIL is the bot account's "
            "Atlassian email, JIRA_API_TOKEN is an API token created at "
            f"{JIRA_API_TOKEN_URL} (a password will not work), and JIRA_PROJECT_KEY is the "
            f"default project key (e.g. CORVID). Set {cls.SECURITY_ENV} as well to a "
            "restricted project, or security findings will be refused rather than filed "
            "into a public one (§4.12, §10). These are credentials: they come from the "
            "environment, never from config/. Or run with tracker: local."
        )

    # -- routing ----------------------------------------------------------

    @property
    def default_project(self) -> str:
        return self._project

    @property
    def security_project(self) -> str | None:
        return self._security_project

    # -- HTTP -------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        credential = base64.b64encode(f"{self.email}:{self._token}".encode()).decode()
        return {
            "Authorization": f"Basic {credential}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "qaas-tracker/1.0",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        retry_on_429: bool = False,
        api_base: str = JIRA_API_BASE,
    ) -> Any:
        """One Jira call. `retry_on_429` is only ever true for reads.

        A retried POST /issue is a duplicate ticket, and duplicate tickets are
        precisely what this system exists to prevent: a 429 can arrive after
        Jira has already created the issue, so the second attempt files it
        twice. Reads are idempotent and honour `Retry-After`.
        """
        url = f"{self.base_url}{api_base}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        attempts = JIRA_READ_ATTEMPTS if retry_on_429 else 1

        for attempt in range(1, attempts + 1):
            request = urllib.request.Request(
                url, data=payload, method=method, headers=self._headers()
            )
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw.strip() else {}
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < attempts:
                    time.sleep(self._retry_after(exc))
                    continue
                raise self._http_error(exc, method, path) from None
            except urllib.error.URLError as exc:
                raise TrackerError(
                    f"could not reach Jira at {self.base_url} ({exc.reason}); check "
                    "JIRA_BASE_URL, your network, and any proxy settings"
                ) from None
            except TimeoutError:
                raise TrackerError(
                    f"Jira did not answer {method} {path} within {self.timeout:g}s; "
                    "it may be degraded — retry, or check status.atlassian.com"
                ) from None
            except json.JSONDecodeError:
                raise TrackerError(
                    f"Jira returned a non-JSON body for {method} {path}; the URL in "
                    f"JIRA_BASE_URL ({self.base_url}) may point at a proxy or login page "
                    "rather than at a Jira site"
                ) from None
        raise TrackerError(f"Jira rate-limited {method} {path} after {attempts} attempts")

    @staticmethod
    def _retry_after(exc: urllib.error.HTTPError) -> float:
        """Seconds to wait, from the server's own header where it gives one."""
        raw = (exc.headers.get("Retry-After") if exc.headers else None) or ""
        try:
            wait = float(raw.strip())
        except ValueError:
            wait = 1.0
        return max(0.0, min(wait, JIRA_MAX_RETRY_WAIT_S))

    def _http_error(self, exc: urllib.error.HTTPError, method: str, path: str) -> TrackerError:
        """Turn a status code into something the reader can act on.

        Generic "HTTP 403" tells an operator nothing about which of the four
        plausible causes they have.
        """
        detail = self._error_detail(exc)
        suffix = f" Jira said: {detail}" if detail else ""
        if exc.code == 401:
            return TrackerError(
                "Jira rejected the credentials (401). Check JIRA_EMAIL / JIRA_API_TOKEN: "
                "the email must be the Atlassian account the token was minted for, and the "
                f"token must be an API token from {JIRA_API_TOKEN_URL}, not a password. "
                f"Revoked and expired tokens also return 401.{suffix}"
            )
        if exc.code == 403:
            return TrackerError(
                f"Jira refused the request (403) for {method} {path}. The account "
                f"{self.email} is authenticated but lacks permission — check that it has "
                f"Browse Projects and Create Issues on {self.default_project}"
                + (f" and {self.security_project}" if self.security_project else "")
                + f", and that the project has not been archived.{suffix}"
            )
        if exc.code == 404:
            return TrackerError(
                f"Jira has no such resource (404) for {method} {path}. If this was a "
                f"create, check JIRA_PROJECT_KEY='{self.default_project}'"
                + (
                    f" / {self.SECURITY_ENV}='{self.security_project}'"
                    if self.security_project
                    else ""
                )
                + " — a project key that does not exist, or that this account cannot "
                f"browse, both surface as 404.{suffix}"
            )
        if exc.code == 429:
            return TrackerError(
                f"Jira rate-limited {method} {path} (429) and this call is not safe to "
                "retry automatically. Slow the run down or lower the per-run ticket cap "
                f"(§4.12).{suffix}"
            )
        if 500 <= exc.code < 600:
            return TrackerError(
                f"Jira returned {exc.code} for {method} {path}. This is Jira's side, not "
                f"the request's; retry later.{suffix}"
            )
        return TrackerError(f"Jira rejected {method} {path} with HTTP {exc.code}.{suffix}")

    @staticmethod
    def _error_detail(exc: urllib.error.HTTPError) -> str:
        """Jira's own explanation, which is usually the useful half."""
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 - an unreadable error body must not mask the status
            return ""
        if not isinstance(payload, dict):
            return ""
        parts = [str(message) for message in payload.get("errorMessages", []) or []]
        errors = payload.get("errors")
        if isinstance(errors, dict):
            parts += [f"{field}: {message}" for field, message in errors.items()]
        return "; ".join(parts)[:500]

    # -- mapping ----------------------------------------------------------

    @staticmethod
    def _parse_time(value: Any) -> datetime:
        """Jira timestamps look like 2024-05-01T09:15:00.000+0000."""
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                pass
        return _utcnow()

    def _issue_from_jira(self, data: dict[str, Any]) -> Issue:
        """Map a Jira issue onto the house `Issue`.

        `status` carries Jira's own status name, not one of `STATUSES`: the
        workflow belongs to the project, and rewriting "Awaiting QA" into
        "in_review" would be this adapter inventing facts.
        """
        fields = data.get("fields") or {}
        labels = [str(label) for label in fields.get("labels") or []]

        def unprefixed(prefix: str) -> str | None:
            """The house metadata hidden in a label, on the way back out."""
            return next(
                (label[len(prefix) :] for label in labels if label.startswith(prefix)), None
            )

        severity = unprefixed(SEVERITY_LABEL_PREFIX)
        envelope_id = unprefixed(ENVELOPE_LABEL_PREFIX)
        # Put the algorithm prefix back, so a fingerprint read out of Jira
        # compares equal to one an envelope computes (`DefectEnvelope.fingerprint`).
        fingerprint = unprefixed(FINGERPRINT_LABEL_PREFIX)
        if fingerprint and len(fingerprint) == 64 and all(c in "0123456789abcdef" for c in fingerprint):
            fingerprint = f"sha256:{fingerprint}"
        status = ((fields.get("status") or {}).get("name")) or "open"
        project = ((fields.get("project") or {}).get("key")) or self.default_project
        reporter = (fields.get("reporter") or {}).get("displayName")
        return Issue(
            key=str(data.get("key", "")),
            project=str(project),
            title=str(fields.get("summary") or ""),
            body=adf_to_text(fields.get("description")),
            status=str(status),
            labels=sorted(labels),
            severity=severity,
            envelope_id=envelope_id,
            fingerprint=fingerprint,
            reporter=reporter,
            links=self._links_from_jira(fields.get("issuelinks") or []),
            created_at=self._parse_time(fields.get("created")),
            updated_at=self._parse_time(fields.get("updated")),
        )

    @staticmethod
    def _links_from_jira(raw_links: list[dict[str, Any]]) -> list[IssueLink]:
        """Map Jira's link vocabulary back to the house one, best effort."""
        reverse: dict[tuple[str, str], str] = {}
        for house, (names, direction) in JIRA_LINK_TYPES.items():
            reverse.setdefault((names[0].lower(), direction), house)
        links: list[IssueLink] = []
        for entry in raw_links:
            name = str(((entry.get("type") or {}).get("name") or "")).lower()
            if entry.get("outwardIssue"):
                other, direction = entry["outwardIssue"], "outward"
            elif entry.get("inwardIssue"):
                other, direction = entry["inwardIssue"], "inward"
            else:
                continue
            key = other.get("key")
            if not key:
                continue
            links.append(IssueLink(type=reverse.get((name, direction), "relates"), to=str(key)))
        return links

    _FIELDS = "summary,status,labels,description,issuelinks,reporter,project,created,updated"

    # -- configuration checks ---------------------------------------------
    # Read-only calls an operator can make before a run files anything real.
    # They live here rather than in the CLI because knowing which endpoint
    # answers "can this account create issues in this project" is Jira
    # knowledge, and this module is where Jira knowledge is allowed to be.

    #: The four project permissions this system needs, in Jira's own
    #: vocabulary. Anything less and the failure arrives mid-run: Browse to read
    #: an issue back, Create to file, Transition to close, Link to dedupe.
    PROJECT_PERMISSIONS = ("BROWSE_PROJECTS", "CREATE_ISSUES", "TRANSITION_ISSUES", "LINK_ISSUES")

    def whoami(self) -> dict[str, Any]:
        """The account these credentials belong to. Proves auth without writing."""
        return self._request("GET", "/myself", retry_on_429=True)

    def project_info(self, key: str) -> dict[str, Any]:
        """One project's metadata. A 404 here means the key is wrong or unreadable."""
        return self._request("GET", f"/project/{urllib.parse.quote(key)}", retry_on_429=True)

    def project_permissions(self, key: str) -> dict[str, bool]:
        """Which of `PROJECT_PERMISSIONS` this account actually holds on `key`.

        Asked of Jira rather than inferred from a successful read: browsing a
        project and being able to file into it are different grants, and the
        gap between them is where a first live run dies.
        """
        data = self._request(
            "GET",
            "/mypermissions",
            params={"projectKey": key, "permissions": ",".join(self.PROJECT_PERMISSIONS)},
            retry_on_429=True,
        )
        granted = data.get("permissions") or {}
        return {
            name: bool((granted.get(name) or {}).get("havePermission"))
            for name in self.PROJECT_PERMISSIONS
        }

    def project_statuses(self, key: str) -> dict[str, list[str]]:
        """Issue type name -> the status names its workflow contains.

        This is the only read-only view of a project's workflow vocabulary.
        `/issue/{key}/transitions` is more precise but needs an issue that
        already exists, and shows only the edges out of that one issue's
        current status — useless for "will this project ever accept 'closed'".
        """
        data = self._request(
            "GET", f"/project/{urllib.parse.quote(key)}/statuses", retry_on_429=True
        )
        entries = data if isinstance(data, list) else []
        return {
            str(entry.get("name") or ""): [
                str((status or {}).get("name") or "") for status in entry.get("statuses") or []
            ]
            for entry in entries
            if isinstance(entry, dict)
        }

    @staticmethod
    def map_house_statuses(status_names: list[str]) -> dict[str, str | None]:
        """House status -> the project status it will resolve to, or None.

        Mirrors `_match_transition`'s candidate order, so what this predicts is
        what a transition will actually do. A None is a silent failure waiting
        to happen: PROOF asks for 'closed', nothing matches, and the ticket sits
        open while the run reports success.
        """
        available = {name.strip().lower(): name for name in status_names if name.strip()}
        mapped: dict[str, str | None] = {}
        for house in STATUSES:
            candidates = [house, house.replace("_", " "), house.replace("-", " ")]
            candidates += list(JIRA_STATUS_ALIASES.get(house, ()))
            mapped[house] = next(
                (available[candidate] for candidate in candidates if candidate in available), None
            )
        return mapped

    # -- boards -----------------------------------------------------------
    #
    # A board per repository, without a project per repository. Creating a Jira
    # *project* needs administrator rights that a bot account normally does not
    # have, and a project per repository is unmanageable by the tenth repo.
    # Creating a saved *filter* needs no special grant, and a board can be built
    # over a filter — so every repository gets its own board inside one project,
    # and the tickets are separated by a label rather than by a project key.

    def find_filter(self, name: str) -> dict[str, Any] | None:
        """A filter owned by this account with exactly this name, or None.

        Matched on the account's own filters rather than on all visible ones:
        `filter/search` returns other people's filters too, and adopting a
        stranger's filter as the run's board would silently repoint it.
        """
        params = {"filterName": name, "expand": "jql", "maxResults": "50"}
        account = self._account_id()
        if account:
            # Omitted rather than sent empty: Jira answers an empty accountId
            # with a 400, which would read as "the filter API is broken".
            params["accountId"] = account
        data = self._request("GET", "/filter/search", params=params, retry_on_429=True)
        for entry in data.get("values") or []:
            if str(entry.get("name") or "").strip() == name:
                return entry
        return None

    def create_filter(self, *, name: str, jql: str, description: str = "") -> dict[str, Any]:
        """A saved filter, shared with authenticated users.

        The share permission is not optional decoration: Jira refuses to build
        a board over a private filter, and the refusal arrives as a 400 on the
        *board* call, two steps away from the cause.
        """
        return self._request(
            "POST",
            "/filter",
            body={
                "name": name,
                "jql": jql,
                "description": description,
                "favourite": True,
                "sharePermissions": [{"type": "authenticated"}],
            },
        )

    def find_board(self, name: str) -> dict[str, Any] | None:
        """A board with exactly this name, or None. Agile API."""
        data = self._request(
            "GET",
            "/board",
            params={"name": name, "maxResults": "50"},
            retry_on_429=True,
            api_base=JIRA_AGILE_BASE,
        )
        for entry in data.get("values") or []:
            if str(entry.get("name") or "").strip() == name:
                return entry
        return None

    def create_board(self, *, name: str, filter_id: int, board_type: str = "kanban") -> dict[str, Any]:
        return self._request(
            "POST",
            "/board",
            body={"name": name, "type": board_type, "filterId": filter_id},
            api_base=JIRA_AGILE_BASE,
        )

    def board_url(self, board_id: int) -> str:
        return f"{self.base_url}/jira/software/projects/{self._project}/boards/{board_id}"

    def filter_url(self, filter_id: int) -> str:
        return f"{self.base_url}/issues/?filter={filter_id}"

    def _account_id(self) -> str | None:
        """This credential's Atlassian account id, fetched once.

        Cached because `find_filter` is called on every run and `/myself` is
        the same answer every time.
        """
        if self._account_id_cache is None:
            try:
                self._account_id_cache = str(self.whoami().get("accountId") or "")
            except TrackerError:
                self._account_id_cache = ""
        return self._account_id_cache or None

    def ensure_repo_board(
        self,
        slug: str,
        *,
        display: str | None = None,
        project: str | None = None,
    ) -> BoardInfo:
        """Find or create the board for one repository. Idempotent.

        Called at the top of every run, so it must be safe to call when
        everything already exists — the second run against a repository reuses
        the board rather than making `repo QA (2)`.

        A board this could not create is reported, not raised. The run's job is
        to find defects and file them; a missing board makes the tickets harder
        to look at, and nothing else. Losing the findings over it would be the
        larger failure.
        """
        label = repo_label(slug)
        if label is None:
            raise TrackerError(
                f"'{slug}' cannot become a Jira label, so no per-repository board can be "
                "built for it. Give the target a simpler name with `qaas init --name`."
            )
        key = project or self._project
        jql = f'project = "{key}" AND labels = "{label}" ORDER BY created DESC'
        name = f"{display or _label_slug(slug)} — QA (qaas)"

        info = BoardInfo(slug=_label_slug(slug) or slug, label=label, jql=jql)

        existing = self.find_filter(name)
        if existing is None:
            created = self.create_filter(
                name=name,
                jql=jql,
                description=(
                    f"Defects filed automatically by qaas against {slug}. "
                    f"Every ticket carries the label {label}."
                ),
            )
            info = replace(info, filter_id=int(created["id"]), filter_name=name, created_filter=True)
        else:
            info = replace(info, filter_id=int(existing["id"]), filter_name=name)

        info = replace(info, url=self.filter_url(info.filter_id))

        board = self.find_board(name)
        if board is not None:
            return replace(
                info,
                board_id=int(board["id"]),
                board_name=name,
                url=self.board_url(int(board["id"])),
            )

        try:
            made = self.create_board(name=name, filter_id=int(info.filter_id))
        except TrackerError as exc:
            # Team-managed projects own their boards and reject this; so does an
            # account without "Create shared objects". Both leave the filter
            # usable, which is why this returns rather than raises.
            return replace(
                info,
                note=(
                    f"Jira would not create a board over the filter ({exc}). The filter "
                    f"exists and every ticket carries {label}, so open the filter link "
                    "above, or create a board from it by hand in Jira."
                ),
            )
        board_id = int(made["id"])
        return replace(
            info,
            board_id=board_id,
            board_name=name,
            url=self.board_url(board_id),
            created_board=True,
        )

    # -- operations -------------------------------------------------------

    @staticmethod
    def _description(body: str, reporter: str | None) -> str:
        """The ticket body with the filing agent named inside it.

        Jira sets `reporter` from the credential whatever we send, so the agent
        that found the defect has to be recorded somewhere that stays true.
        """
        if not reporter:
            return body
        return (
            f"{body}\n\nFiled by {reporter} (automated QA)."
            if body
            else f"Filed by {reporter} (automated QA)."
        )

    @staticmethod
    def _labels_for(
        labels: list[str] | None,
        severity: str | None,
        envelope_id: str | None,
        fingerprint: str | None,
    ) -> list[str]:
        """Caller labels plus the house metadata labels, deduped and sorted."""
        all_labels = set(labels or [])
        for value, prefix in (
            (severity, SEVERITY_LABEL_PREFIX),
            (envelope_id, ENVELOPE_LABEL_PREFIX),
            (_fingerprint_label_value(fingerprint), FINGERPRINT_LABEL_PREFIX),
        ):
            label = _label_safe(value, prefix)
            if label:
                all_labels.add(label)
        return sorted(all_labels)

    def create_payload(
        self,
        *,
        project: str,
        title: str,
        body: str = "",
        labels: list[str] | None = None,
        severity: str | None = None,
        envelope_id: str | None = None,
        fingerprint: str | None = None,
        reporter: str | None = None,
    ) -> dict[str, Any]:
        """The exact JSON body `create_issue` would POST to `/issue`.

        Split out so `qaas tracker-check --dry-run-ticket` can show an operator
        the ADF and the labels before a real ticket lands in front of real
        people. It must be the same code path: a preview that *reconstructs*
        the payload is correct only until the day it drifts, and it would be
        trusted either way.
        """
        if not title.strip():
            raise TrackerError("an issue needs a title")
        return {
            "fields": {
                "project": {"key": project},
                "summary": title.strip()[:255],  # Jira's summary limit; a 400 here is silly
                "description": markdown_to_adf(self._description(body, reporter)),
                "issuetype": {"name": self.issue_type},
                "labels": self._labels_for(labels, severity, envelope_id, fingerprint),
            }
        }

    def create_issue(
        self,
        *,
        project: str,
        title: str,
        body: str = "",
        labels: list[str] | None = None,
        severity: str | None = None,
        envelope_id: str | None = None,
        fingerprint: str | None = None,
        reporter: str | None = None,
    ) -> Issue:
        payload = self.create_payload(
            project=project,
            title=title,
            body=body,
            labels=labels,
            severity=severity,
            envelope_id=envelope_id,
            fingerprint=fingerprint,
            reporter=reporter,
        )
        created = self._request("POST", "/issue", body=payload, retry_on_429=False)
        key = str(created.get("key") or "")
        if not key:
            raise TrackerError(f"Jira accepted the issue but returned no key: {created!r}")

        fallback = self._local_issue(
            key, project, title, self._description(body, reporter),
            list(payload["fields"]["labels"]),
            severity, envelope_id, fingerprint, reporter,
        )
        # Reading the issue back is a convenience, not the record. The ticket
        # exists the moment Jira answered the POST, so a failure here must not
        # lose the key — a filed ticket nobody can name is a stranded ticket.
        try:
            return self.get(key) or fallback
        except TrackerError:
            return fallback

    @staticmethod
    def _local_issue(
        key: str,
        project: str,
        title: str,
        body: str,
        labels: list[str],
        severity: str | None,
        envelope_id: str | None,
        fingerprint: str | None,
        reporter: str | None,
    ) -> Issue:
        """What we know about an issue Jira created but would not read back."""
        return Issue(
            key=key,
            project=project,
            title=title.strip(),
            body=body,
            labels=labels,
            severity=severity,
            envelope_id=envelope_id,
            fingerprint=fingerprint,
            reporter=reporter,
            history=[Transition(status="open", by=reporter, comment="filed")],
        )

    def get(self, key: str) -> Issue | None:
        if not _KEY_RE.match(key):
            raise TrackerError(f"'{key}' is not an issue key; expected e.g. {self._project}-1")
        try:
            data = self._request(
                "GET", f"/issue/{urllib.parse.quote(key)}",
                params={"fields": self._FIELDS}, retry_on_429=True,
            )
        except TrackerError as exc:
            if "(404)" in str(exc):
                return None
            raise
        return self._issue_from_jira(data)

    def _require(self, key: str) -> Issue:
        issue = self.get(key)
        if issue is None:
            raise UnknownIssue(f"no issue '{key}' in the tracker")
        return issue

    def transition(self, key: str, status: str, *, by: str | None = None, comment: str = "") -> Issue:
        """Move an issue by *status name*, resolved against the real workflow.

        Jira's API takes a transition id, and ids are per-project and unstable,
        so the name has to be resolved every time. A name that does not resolve
        raises with the transitions that do exist: doing nothing quietly is the
        worst outcome here, because the caller believes the ticket moved.
        """
        issue = self._require(key)
        available = (
            self._request(
                "GET", f"/issue/{urllib.parse.quote(key)}/transitions", retry_on_429=True
            ).get("transitions")
            or []
        )
        chosen = self._match_transition(status, available)
        if chosen is None:
            offered = ", ".join(
                f"'{t.get('name')}' (-> {(t.get('to') or {}).get('name', '?')})"
                for t in available
            ) or "none at all"
            raise TrackerError(
                f"cannot move {key} to '{status}': no such transition from its current "
                f"status '{issue.status}'. Jira offers: {offered}. Jira workflows are "
                "per-project — use one of those names, not a house status."
            )

        self._request(
            "POST", f"/issue/{urllib.parse.quote(key)}/transitions",
            body={"transition": {"id": str(chosen["id"])}}, retry_on_429=False,
        )

        if comment or by:
            note = f"{by or 'qaas'}: {comment}" if comment else f"Transitioned by {by}."
            try:
                self._request(
                    "POST", f"/issue/{urllib.parse.quote(key)}/comment",
                    body={"body": markdown_to_adf(note)}, retry_on_429=False,
                )
            except TrackerError as exc:
                # The transition already applied; saying nothing would leave the
                # caller believing the audit trail is complete when it is not.
                raise TrackerError(
                    f"{key} was transitioned to '{chosen.get('name')}', but the comment "
                    f"could not be added: {exc}"
                ) from None

        return self._require(key)

    @staticmethod
    def _match_transition(
        status: str, available: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Match a requested status against transition and target names.

        Case-insensitive, and it tries the house aliases (`in_progress` ->
        "In Progress", "Doing", ...) so callers speaking the house vocabulary
        work against an ordinary Jira workflow without a mapping file.
        """
        wanted = status.strip().lower()
        candidates = [wanted, wanted.replace("_", " "), wanted.replace("-", " ")]
        candidates += list(JIRA_STATUS_ALIASES.get(wanted, ()))
        seen: list[str] = []
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.append(candidate)
            for entry in available:
                names = {
                    str(entry.get("name") or "").strip().lower(),
                    str((entry.get("to") or {}).get("name") or "").strip().lower(),
                }
                if candidate in names and entry.get("id") is not None:
                    return entry
        return None

    def _link_type_names(self) -> list[dict[str, Any]]:
        if self._link_type_cache is None:
            self._link_type_cache = (
                self._request("GET", "/issueLinkType", retry_on_429=True).get("issueLinkTypes")
                or []
            )
        return self._link_type_cache

    def link(self, key: str, to: str, link_type: str = "relates") -> Issue:
        self._check_link_type(link_type)
        if key == to:
            raise TrackerError("an issue cannot be linked to itself")
        self._require(key)
        self._require(to)  # refuse dangling links: a link to nothing is worse than none

        names, direction = JIRA_LINK_TYPES[link_type]
        installed = {str(entry.get("name") or ""): entry for entry in self._link_type_names()}
        chosen = next((name for name in names if name in installed), None)
        if chosen is None:
            # Relates exists in every stock Jira; if even that is gone, say what
            # this instance does have rather than sending an unusable name.
            chosen = next((name for name in installed if name.lower() == "relates"), None)
        if chosen is None:
            raise TrackerError(
                f"this Jira has no link type usable for '{link_type}'; it offers: "
                f"{', '.join(sorted(installed)) or 'none'}"
            )

        # Jira reads a link as "<outwardIssue> <outward phrase> <inwardIssue>",
        # so `key` sits at whichever end the house link type names. Getting this
        # backwards silently inverts the meaning of every link filed.
        near, far = ("outwardIssue", "inwardIssue") if direction == "outward" else (
            "inwardIssue", "outwardIssue"
        )
        self._request(
            "POST", "/issueLink",
            body={"type": {"name": chosen}, near: {"key": key}, far: {"key": to}},
            retry_on_429=False,
        )
        return self._require(key)

    def search(
        self,
        *,
        text: str | None = None,
        project: str | None = None,
        status: str | None = None,
        label: str | None = None,
        envelope_id: str | None = None,
        fingerprint: str | None = None,
        limit: int = 20,
    ) -> list[Issue]:
        """Search by JQL, scoped to the configured projects by default.

        Unscoped, this would sweep every project the bot can see — slow, and it
        drags unrelated tickets into an agent's context where they read as
        prior art. The scope is the projects this system files into.
        """
        clauses: list[str] = []
        if project:
            clauses.append(f"project = {self._jql_value(project)}")
        else:
            scope = [self._project] + ([self._security_project] if self._security_project else [])
            clauses.append(
                "project in (" + ", ".join(self._jql_value(p) for p in scope) + ")"
            )
        if status:
            clauses.append(f"status = {self._jql_value(status)}")
        for value, prefix in (
            (label, ""),
            (envelope_id, ENVELOPE_LABEL_PREFIX),
            (_fingerprint_label_value(fingerprint), FINGERPRINT_LABEL_PREFIX),
        ):
            if value:
                clauses.append(f"labels = {self._jql_value(f'{prefix}{value}')}")
        if text and text.strip():
            clauses.append(f"text ~ {self._jql_value(text.strip())}")

        jql = " AND ".join(clauses) + " ORDER BY created DESC"
        # POST /search/jql, not GET /search: Atlassian deprecated the latter,
        # which is exactly the trap §5.1 warns about.
        data = self._request(
            "POST", "/search/jql",
            body={
                "jql": jql,
                "maxResults": max(1, min(int(limit), 100)),
                "fields": self._FIELDS.split(","),
            },
            retry_on_429=True,
        )
        return [self._issue_from_jira(entry) for entry in data.get("issues") or []]

    @staticmethod
    def _jql_value(value: str) -> str:
        """Quote a JQL literal. Unescaped quotes are an injection, not a typo."""
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'


class JiraDataCenterTracker(TrackerAdapter):
    """Not implemented: Jira Server / Data Center is a different integration.

    §5.1 flags this as a decision to take before wiring anything. Data Center
    is not Cloud with a different hostname: it authenticates with a Personal
    Access Token (`Authorization: Bearer <pat>`) rather than email + API token,
    it serves REST API v2 rather than v3, and v2 takes a plain-text or wiki
    description where v3 requires ADF — so `JiraTracker`'s converter is not
    just unnecessary there, it is wrong. Atlassian's official hosted MCP server
    is Cloud-only; the self-hosted route is the community
    `sooperset/mcp-atlassian` server.

    Implementing this by subclassing `JiraTracker` would be a mistake: it would
    inherit the ADF conversion and the v3 paths, and fail in ways that look
    like Jira being broken rather than the adapter being wrong.
    """

    REQUIRED_ENV = ("JIRA_BASE_URL", "JIRA_PERSONAL_ACCESS_TOKEN", "JIRA_PROJECT_KEY")

    def __init__(self, *_args: Any, **_kwargs: Any):
        raise NotImplementedError(
            "JiraDataCenterTracker is a stub. Jira Server/Data Center uses PAT auth "
            "(Authorization: Bearer, from "
            f"{', '.join(self.REQUIRED_ENV)}) against REST API v2, whose description "
            "field is plain text or wiki markup rather than ADF — so this is a separate "
            "adapter, not a flag on JiraTracker (§5.1). Use tracker: jira for Jira Cloud, "
            "or tracker: local."
        )

    def create_issue(self, **_kwargs: Any) -> Issue:  # pragma: no cover - unreachable stub
        raise NotImplementedError

    def transition(self, key: str, status: str, **_kwargs: Any) -> Issue:  # pragma: no cover
        raise NotImplementedError

    def link(self, key: str, to: str, link_type: str = "relates") -> Issue:  # pragma: no cover
        raise NotImplementedError

    def search(self, **_kwargs: Any) -> list[Issue]:  # pragma: no cover
        raise NotImplementedError

    def get(self, key: str) -> Issue | None:  # pragma: no cover
        raise NotImplementedError


def build_tracker(backend: str, root: Path | str) -> TrackerAdapter:
    """Pick the adapter named by `config.tracker`.

    `jira` builds a live Jira Cloud client, which validates its environment
    here and now: a misconfigured tracker must fail before an agent starts
    finding things, not after.
    """
    if backend == "local":
        return LocalTracker(root)
    if backend == "jira":
        return JiraTracker()
    raise ValueError(f"unknown tracker backend '{backend}'; expected 'local' or 'jira'")


def issue_summary(issue: Issue) -> dict[str, object]:
    """The compact view returned to an agent; full bodies would bloat context."""
    return {
        "key": issue.key,
        "project": issue.project,
        "title": issue.title,
        "status": issue.status,
        "severity": issue.severity,
        "labels": issue.labels,
        "envelope_id": issue.envelope_id,
        "links": [link.model_dump() for link in issue.links],
        "updated_at": issue.updated_at.isoformat(),
    }


__all__ = [
    "DEFAULT_PROJECT",
    "SECURITY_PROJECT",
    "STATUSES",
    "LINK_TYPES",
    "Issue",
    "IssueLink",
    "Transition",
    "TrackerAdapter",
    "TrackerError",
    "TrackerConfigError",
    "UnknownIssue",
    "LocalTracker",
    "JiraTracker",
    "JiraDataCenterTracker",
    "JIRA_API_TOKEN_URL",
    "markdown_to_adf",
    "adf_to_text",
    "build_tracker",
    "issue_summary",
]
