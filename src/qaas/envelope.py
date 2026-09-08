"""The DefectEnvelope — the one contract every agent speaks (architecture §6).

Validate on write and on read; reject malformed envelopes rather than repairing
them. Agents never pass prose to each other, only these.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ENVELOPE_VERSION = "1.0"


class Domain(StrEnum):
    ARCHITECTURE = "architecture"
    DATABASE = "database"
    API = "api"
    WEBSOCKET = "websocket"
    FRONTEND = "frontend"
    UX = "ux"
    SECURITY = "security"
    PERFORMANCE = "performance"


class DefectClass(StrEnum):
    BUG = "bug"
    REGRESSION = "regression"
    UX_FRICTION = "ux-friction"
    TECH_DEBT = "tech-debt"
    VULNERABILITY = "vulnerability"
    PERF_REGRESSION = "perf-regression"


class Severity(StrEnum):
    BLOCKER = "blocker"
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    TRIVIAL = "trivial"

    @property
    def rank(self) -> int:
        """0 is most severe. Lets callers compare and sort without a lookup table."""
        return _SEVERITY_ORDER.index(self)


_SEVERITY_ORDER = [
    Severity.BLOCKER,
    Severity.CRITICAL,
    Severity.MAJOR,
    Severity.MINOR,
    Severity.TRIVIAL,
]


class ReproStatus(StrEnum):
    REPRODUCED = "reproduced"
    FLAKY = "flaky"
    NOT_REPRODUCIBLE = "not_reproducible"
    UNATTEMPTED = "unattempted"


class EvidenceType(StrEnum):
    SCREENSHOT = "screenshot"
    TRACE = "trace"
    QUERY_PLAN = "query_plan"
    LOG = "log"
    FRAME_CAPTURE = "frame_capture"
    TEST_OUTPUT = "test_output"
    HAR = "har"


class Strict(BaseModel):
    """Base for every envelope part: unknown fields are an error, not a shrug."""

    model_config = ConfigDict(extra="forbid", use_enum_values=False)


class Location(Strict):
    service: str | None = None
    paths: list[str] = Field(default_factory=list)
    endpoint: str | None = None
    ui_route: str | None = None
    commit_sha: str | None = None


class Evidence(Strict):
    type: EvidenceType
    uri: str
    note: str = ""

    @field_validator("uri")
    @classmethod
    def _known_scheme(cls, v: str) -> str:
        if not re.match(r"^(artifact|file|https?)://", v):
            raise ValueError(
                "evidence uri must start with artifact://, file://, http:// or https://"
            )
        return v


class Environment(Strict):
    branch: str = ""
    fixture: str = ""
    flags: dict[str, Any] = Field(default_factory=dict)


class Reproduction(Strict):
    status: ReproStatus = ReproStatus.UNATTEMPTED
    environment: Environment = Field(default_factory=Environment)
    steps: list[str] = Field(default_factory=list)
    failing_test: str | None = None
    flake_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    verified_by: str | None = None


class Impact(Strict):
    user_facing: bool = False
    affected_surface: str = ""
    data_loss_risk: bool = False
    security_relevant: bool = False
    frequency_estimate: str = ""


class SuggestedOwner(Strict):
    component: str | None = None
    team: str | None = None


class Dedupe(Strict):
    fingerprint: str | None = None
    similar_to: list[str] = Field(default_factory=list)
    occurrence_count: int = Field(default=1, ge=1)


class TrackerRef(Strict):
    key: str | None = None
    project: str | None = None
    status: str | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DefectEnvelope(Strict):
    """A single finding, at any stage of its life from draft to filed."""

    envelope_version: Literal["1.0"] = ENVELOPE_VERSION
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str
    discovered_by: str
    discovered_at: datetime = Field(default_factory=_utcnow)

    domain: Domain
    defect_class: DefectClass = Field(alias="class")

    title: Annotated[str, Field(min_length=1, max_length=90)]
    summary: Annotated[str, Field(min_length=1)]

    location: Location = Field(default_factory=Location)
    evidence: list[Evidence] = Field(default_factory=list)
    reproduction: Reproduction = Field(default_factory=Reproduction)
    impact: Impact = Field(default_factory=Impact)

    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0)

    suggested_owner: SuggestedOwner = Field(default_factory=SuggestedOwner)
    suggested_fix_area: str = ""
    autonomy_eligible: bool = False

    dedupe: Dedupe = Field(default_factory=Dedupe)
    jira: TrackerRef = Field(default_factory=TrackerRef)

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        ser_json_timedelta="iso8601",
    )

    @field_validator("title")
    @classmethod
    def _single_line_title(cls, v: str) -> str:
        if "\n" in v:
            raise ValueError("title must be a single line")
        return v.strip()

    @field_validator("discovered_by")
    @classmethod
    def _agent_name(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Z][A-Z_]{2,23}", v):
            raise ValueError("discovered_by must be an agent name in SCREAMING_CASE")
        return v

    # -- evidence gate ----------------------------------------------------
    # "Evidence or it did not happen" (§2). Enforced here rather than in a
    # prompt so an agent cannot talk its way past it.

    def has_evidence(self) -> bool:
        return bool(self.evidence) or self.reproduction.failing_test is not None

    def is_fileable(self, min_confidence: float = 0.6) -> tuple[bool, str]:
        """Whether CLERK may file this. Returns (ok, reason-if-not).

        The confidence gate is §7; the evidence gate is §2. Anything that fails
        goes to the human review queue instead of the tracker.
        """
        if not self.has_evidence():
            return False, "no evidence: needs an artifact or a failing test"
        if self.confidence < min_confidence:
            return False, (
                f"confidence {self.confidence:.2f} below gate {min_confidence:.2f}"
            )
        if self.reproduction.status == ReproStatus.NOT_REPRODUCIBLE:
            return False, "not reproducible"
        return True, ""

    # -- dedupe -----------------------------------------------------------

    def fingerprint(self) -> str:
        """A stable structural identity for this defect.

        Deliberately excludes prose, line numbers, commit sha, run id and
        timestamps: the same defect reported by two agents in different words,
        or found again after the file moved a few lines, must hash the same.
        """
        paths = sorted(_strip_line_number(p) for p in self.location.paths)
        parts = [
            self.domain.value,
            self.defect_class.value,
            self.location.service or "",
            self.location.endpoint or "",
            self.location.ui_route or "",
            "|".join(paths),
        ]
        digest = hashlib.sha256("\x1f".join(parts).encode()).hexdigest()
        return f"sha256:{digest}"

    def with_fingerprint(self) -> "DefectEnvelope":
        """Return a copy carrying its computed fingerprint."""
        return self.model_copy(
            update={"dedupe": self.dedupe.model_copy(update={"fingerprint": self.fingerprint()})}
        )

    def to_json(self) -> str:
        return self.model_dump_json(by_alias=True, indent=2)

    @classmethod
    def from_json(cls, raw: str | bytes) -> "DefectEnvelope":
        return cls.model_validate_json(raw)


def _strip_line_number(path: str) -> str:
    """`src/ws/session.ts:142` -> `src/ws/session.ts`."""
    return re.sub(r":\d+(?::\d+)?$", "", path.strip())
