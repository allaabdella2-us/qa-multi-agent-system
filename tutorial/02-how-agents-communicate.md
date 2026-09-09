# How agents communicate

This file covers the single data structure that every agent in `qaas` reads and
writes — the `DefectEnvelope` — and the machinery around it: the gates that live
on the model rather than in a prompt, the fingerprint that makes the same defect
hash the same twice, the one server-side write path, and how the conductor moves
a finding from agent to agent without ever reading an agent's prose.

---

## 1. Agents never pass prose to each other

Eight agents run in a `qaas` pipeline. Each one is its own top-level `query()`
with its own context window, so nothing is shared implicitly — no scratchpad, no
conversation history, no "as I mentioned earlier". Whatever CONDUIT learns about
a broken endpoint is gone the moment its turn ends, unless it wrote it down in
the one format the rest of the system reads.

That format is `DefectEnvelope`, in `src/qaas/envelope.py`. It is the **only**
inter-agent type. There is no second one, no free-text channel, no "notes" field
for things that did not fit.

Why this matters concretely: an agent's final assistant message is not consumed
by anything. `src/qaas/runner.py:1-6` says so at the top of the module:

```python
"""Running one agent: build its options, stream its turn, record what it cost.

Every invocation is its own `query()`. What comes back that matters is not the
agent's prose — that is a summary for the log — but what it wrote through its
tools, plus the cost and turn count the ledger needs.
"""
```

The prose is logged. The envelope is the product.

> **A precise caveat.** The conductor *renders* a task string for the next agent
> and that string contains English. But it is generated from typed envelope
> fields by `src/qaas/tasks.py`, not copied from anything an agent wrote as
> prose. The direction matters: structured data in, formatted text out. Never
> text in, meaning out.

---

## 2. The model

`src/qaas/envelope.py:153-187`:

```python
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
```

One envelope carries a finding through its whole life. A discovery agent creates
it with `domain`, `class`, `title`, `summary`, `severity`, `confidence` and
whatever `location` and `evidence` it has. FORGE fills in `reproduction`. CLERK
fills in `jira`. Nothing is copied into a second object at any stage.

### Sub-models

| field | type | filled by |
|---|---|---|
| `location` | `Location` — service, paths, endpoint, ui_route, commit_sha | discovery |
| `evidence` | `list[Evidence]` — type + uri + note | any agent, via `put_artifact` |
| `reproduction` | `Reproduction` — status, steps, failing_test, flake_rate, environment | FORGE only |
| `impact` | `Impact` — user_facing, data_loss_risk, security_relevant, … | discovery |
| `dedupe` | `Dedupe` — fingerprint, similar_to, occurrence_count | the store, on write |
| `jira` | `TrackerRef` — key, project, status | CLERK |

The enums (`Domain`, `DefectClass`, `Severity`, `ReproStatus`, `EvidenceType`)
are all `StrEnum`, defined at `src/qaas/envelope.py:21-77`. `Severity` carries a
`rank` property (`envelope.py:48-51`) so callers can sort by severity without a
lookup table — the conductor uses it when it has to triage more findings than the
per-run cap allows.

### `extra="forbid"` everywhere

Every part of the envelope inherits from `Strict` (`src/qaas/envelope.py:80-83`):

```python
class Strict(BaseModel):
    """Base for every envelope part: unknown fields are an error, not a shrug."""

    model_config = ConfigDict(extra="forbid", use_enum_values=False)
```

`Location`, `Evidence`, `Environment`, `Reproduction`, `Impact`,
`SuggestedOwner`, `Dedupe` and `TrackerRef` all subclass it, and
`DefectEnvelope` restates `extra="forbid"` in its own `model_config`.

The effect, observed:

```console
$ .venv/bin/python -c "
from qaas.envelope import DefectEnvelope
from pydantic import ValidationError
try:
    DefectEnvelope.model_validate({'run_id':'r','discovered_by':'CONDUIT','domain':'api','class':'bug','title':'t','summary':'s','severity':'major','confidence':0.8,'notes':'extra field'})
except ValidationError as e:
    print(e)
"
1 validation error for DefectEnvelope
notes
  Extra inputs are not permitted [type=extra_forbidden, input_value='extra field', input_type=str]
```

An agent that invents a field gets told, immediately, with the field name. It
does not get a silently-discarded value that nothing downstream will ever see.
The module docstring states the rule (`src/qaas/envelope.py:1-5`): *validate on
write and on read; reject malformed envelopes rather than repairing them.*

Two field validators back that up: `title` must be a single line
(`envelope.py:189-194`), and `discovered_by` must look like an agent name
(`envelope.py:196-201`):

```python
    @field_validator("discovered_by")
    @classmethod
    def _agent_name(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Z][A-Z_]{2,23}", v):
            raise ValueError("discovered_by must be an agent name in SCREAMING_CASE")
        return v
```

---

## 3. The two gates live on the model, not in a prompt

`src/qaas/envelope.py:203-224`:

```python
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
```

Three conditions, one method:

1. an artifact or a failing test,
2. confidence at or above the gate (`min_confidence_to_file: 0.6` in
   `src/qaas/defaults/config/system.yaml:13`),
3. reproduction status is not `not_reproducible`.

### Why a method beats a prompt instruction

A prompt saying "only file findings with evidence" is a request. It travels
inside the model's context, where it competes with everything else the agent has
read, and it can be truncated, deprioritised or argued around. Worse, it is
unauditable: you cannot point at a line of code and say *this is where the rule
is applied*, because it is applied — or not — inside a forward pass.

`is_fileable()` is a function. It runs the same way every time. An agent that
believes very strongly it has found something important still gets `(False, "no
evidence: needs an artifact or a failing test")`, and the value of that tuple
does not depend on how persuasive the agent was.

And because it is a method on the shared type, **every consumer gets the same
answer**. Four independent call sites use it:

| caller | file | what it does with the answer |
|---|---|---|
| `emit_envelope` | `src/qaas/mcp/envelope_server.py:153` | tells the emitting agent immediately if the finding is held |
| `record_reproduction` | `src/qaas/mcp/envelope_server.py:320` | tells FORGE whether its verdict will reach CLERK |
| `list_envelopes` | `src/qaas/mcp/envelope_server.py:240,257` | the `fileable_only` filter and the `fileable` column |
| `_phase_file` | `src/qaas/conductor.py:322-325` | decides whether to dispatch CLERK at all |

There is no way for those four to disagree, because there is only one
implementation.

Here it is refusing a finding with no evidence attached (the full script is in
§4 below; this is its first two lines of output):

```
has_evidence: False
is_fileable : (False, 'no evidence: needs an artifact or a failing test')
```

And here is a real held finding from a real run, `qaas show` reading it back off
disk:

```console
$ .venv/bin/qaas show run-20260907T233304-ca7657
...
critical api          Read-only viewer role can create and place orders through
the UI  (SURFACE, conf 0.10)  held: confidence 0.10 below gate 0.60
...
minor    frontend     Orders search field has no accessible name (WCAG 1.3.1,
4.1.2)  (SURFACE, conf 0.12)  held: confidence 0.12 below gate 0.60
```

Two of fifteen findings were held before CLERK ever saw them. Nobody had to
remember to check; `_phase_file` simply never put them in the list.

---

## 4. `fingerprint()`: structural identity

Two agents can find the same defect. The same defect can be found again next
week. Both need to hash the same, and neither can be relied on to phrase it the
same way.

`src/qaas/envelope.py:228-245`:

```python
    def fingerprint(self) -> str:
        """A stable structural identity for this defect.

        Deliberately excludes prose, line numbers, commit sha, run id and
        timestamps: the same defect reported by two agents in different words,
        or found again after the file moved a few lines, must hash the same.
        """
        paths = sorted(normalize_path(p) for p in self.location.paths)
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
```

**What it hashes:** domain, defect class, service, endpoint, ui_route, and the
sorted normalised path list. Six structural facts.

**What it excludes, on purpose:** `title`, `summary`, `severity`, `confidence`,
`evidence`, `id`, `run_id`, `discovered_by`, `discovered_at`, `commit_sha`, and —
via `normalize_path` — line numbers. Every one of those is either prose, an
opinion, or a fact about *this sighting* rather than about the defect.

Demonstrated. Two agents, different words, different cited line, same identity:

```console
$ .venv/bin/python - <<'PY'
from qaas.envelope import DefectEnvelope

base = {"run_id": "run-demo", "discovered_by": "CONDUIT", "domain": "api",
        "class": "bug", "severity": "major", "confidence": 0.8,
        "summary": "The endpoint validates limit and then never applies it."}

a = DefectEnvelope.model_validate(base | {
    "title": "GET /v1/orders ignores its limit parameter",
    "location": {"endpoint": "GET /v1/orders",
                 "paths": ["target-app/api/app/routes/orders.py:112-113"]}})
b = DefectEnvelope.model_validate(base | {
    "discovered_by": "SURFACE",
    "title": "orders list returns every row regardless of ?limit",
    "location": {"endpoint": "GET /v1/orders",
                 "paths": ["api/app/routes/orders.py:118"]}})

print("has_evidence:", a.has_evidence())
print("is_fileable :", a.is_fileable())
print("CONDUIT     :", a.fingerprint())
print("SURFACE     :", b.fingerprint())
print("equal       :", a.fingerprint() == b.fingerprint())
PY
has_evidence: False
is_fileable : (False, 'no evidence: needs an artifact or a failing test')
CONDUIT     : sha256:8703e5233f6ca8794eb646a73929436e23b2f12aff17418b9e7dd48f11c24288
SURFACE     : sha256:8703e5233f6ca8794eb646a73929436e23b2f12aff17418b9e7dd48f11c24288
equal       : True
```

The first is CONDUIT reporting
`target-app/api/app/routes/orders.py:112-113` with the title *"GET /v1/orders
ignores its limit parameter"*. The second is SURFACE reporting
`api/app/routes/orders.py:118` with the title *"orders list returns every row
regardless of ?limit"*. Same endpoint, same file, same class — one defect, one
hash.

`with_fingerprint()` (`envelope.py:247-251`) returns a copy carrying the computed
hash, and `RunStore.put_envelope` (`src/qaas/store.py:167-182`) calls it on every
write, so nothing lands on disk unfingerprinted.

### The bug in `normalize_path`

`src/qaas/envelope.py:261-286`. Read the docstring — it is a bug report:

```python
def normalize_path(path: str) -> str:
    r"""Reduce a cited path to the file it names, so the same file hashes alike.

    `target-app/api/app/auth.py:112-113` -> `api/app/auth.py`

    Two things defeated the old version, and both were found in live data rather
    than reasoned about. Its regex was `:\d+(?::\d+)?$`, which matched `:142`
    and `:142:5` but NOT `:112-113` -- and a line *range* is how an agent
    naturally cites a region, so the strip almost never fired. And nothing
    removed the repo-root prefix, so `target-app/api/app/auth.py` and
    `api/app/auth.py` were different files as far as the hash was concerned.

    The visible damage was that `occurrence_count` never left 1: the same defect
    reported across three runs produced three identities, so `get_occurrences`
    could never say a defect was recurring. Deduplication itself survived only
    because CLERK matches on similarity rather than on this hash.

    `scorecard._norm_path` delegates here. They must not drift: a scorer that
    considers two paths equal while the fingerprint considers them distinct is
    two answers to one question.
    """
    p = re.sub(r":\d+(?:[-:]\d+)?$", "", path.strip().replace("\\", "/")).lstrip("./")
    for prefix in ("target-app/", "target_app/"):
        if p.startswith(prefix):
            p = p[len(prefix):]
    return p
```

The old regex was `:\d+(?::\d+)?$` — "colon, digits, optionally colon and more
digits". It handles `file.py:142` and `file.py:142:5`. It does not handle
`file.py:112-113`, because a hyphen is not a colon. The fix is one character
class: `[-:]`.

Side by side, run against the two regexes directly:

```console
$ .venv/bin/python -c "
import re
old = r':\d+(?::\d+)?\$'
new = r':\d+(?:[-:]\d+)?\$'
for p in ['api/app/auth.py:142', 'api/app/auth.py:142:5', 'api/app/auth.py:112-113']:
    print(f'{p:28} old->{re.sub(old, \"\", p):22} new->{re.sub(new, \"\", p)}')
"
api/app/auth.py:142          old->api/app/auth.py        new->api/app/auth.py
api/app/auth.py:142:5        old->api/app/auth.py        new->api/app/auth.py
api/app/auth.py:112-113      old->api/app/auth.py:112-113 new->api/app/auth.py
```

Why this was so damaging: a line *range* is the natural way for an agent to cite
a region of code. Look at a real envelope from a real run —
`.qaas/runs/run-20260907T233304-ca7657/envelopes/61297327-…json`:

```json
  "location": {
    "service": "corvid-orders-api",
    "paths": [
      "target-app/api/app/routes/orders.py:49-51",
      "target-app/api/app/routes/invoices.py:34"
    ],
```

Both failure modes in one field: a range, and a repo-root prefix. Under the old
code neither was stripped, so this defect's fingerprint was pinned to the exact
lines `49-51` *and* to the fact that the target happened to sit at
`target-app/`. Find the same defect after someone inserts a blank line above it,
or point `qaas` at the same code checked out somewhere else, and you get a
different hash.

The symptom was subtle, which is why it survived: `occurrence_count` (default
`1`, `envelope.py:140`) never went above 1. Recurrence tracking simply never
fired. Deduplication kept working only because CLERK dedupes on *similarity*
through the `defect_memory` server rather than on this hash — a second mechanism
masking the first one's failure.

The current behaviour, verified:

```console
$ .venv/bin/python -c "
from qaas.envelope import normalize_path
for p in ['target-app/api/app/auth.py:112-113', 'api/app/auth.py:142', 'api/app/auth.py:142:5', './api/app/auth.py']:
    print(f'{p!r:42} -> {normalize_path(p)!r}')
"
'target-app/api/app/auth.py:112-113'       -> 'api/app/auth.py'
'api/app/auth.py:142'                      -> 'api/app/auth.py'
'api/app/auth.py:142:5'                    -> 'api/app/auth.py'
'./api/app/auth.py'                        -> 'api/app/auth.py'
```

The last line of the module (`envelope.py:289-290`) keeps the old name alive:

```python
#: Kept as the old name so nothing importing it breaks; it always meant this.
_strip_line_number = normalize_path
```

---

## 5. The write path: one MCP tool, and the identity is stamped server-side

An agent has exactly one way to emit a finding: the `emit_envelope` tool on the
`envelope` MCP server, `src/qaas/mcp/envelope_server.py:109-115`.

```python
    @tool(
        "emit_envelope",
        "Report one defect. This is the only way a finding leaves your session. "
        "Rejected envelopes come back with the reason; fix the fields and retry.",
        EMIT_SCHEMA,
    )
    async def emit_envelope(args: dict[str, Any]) -> dict[str, Any]:
```

The schema handed to the model (`EMIT_SCHEMA`, `envelope_server.py:19-99`)
requires `domain`, `class`, `title`, `summary`, `severity`, `confidence`. Note
what is *not* in it: `run_id`, `discovered_by`, `id`, `discovered_at`. The agent
is never asked for those, and there is nowhere to put them.

They are stamped by the server, `envelope_server.py:125-141`:

```python
        payload = {k: v for k, v in args.items() if k != "similar_to"}
        payload["run_id"] = ctx.store.run_id
        payload["discovered_by"] = ctx.agent.name

        # A discovery agent does not get to certify its own finding as
        # reproduced (§2: the finder never grades its own homework). Whatever it
        # claims here, the status is reset and FORGE decides independently.
        # Without this the whole triage gate is bypassed by an agent simply
        # asserting it already reproduced the defect — which is exactly what
        # happened on the first full pipeline run, and FORGE was skipped.
        environment = (args.get("reproduction") or {}).get("environment", {})
        steps = (args.get("reproduction") or {}).get("steps", [])
        payload["reproduction"] = {
            "status": "unattempted",
            "steps": steps,
            "environment": environment,
        }
```

Three separate forgeries closed in fifteen lines:

- **`run_id`** comes from `ctx.store.run_id`. An agent cannot file into another
  run.
- **`discovered_by`** comes from `ctx.agent.name`. CONDUIT cannot claim a finding
  was SURFACE's; more usefully, attribution in the ledger is trustworthy, so
  `qaas score` can measure per-agent recall honestly.
- **`reproduction.status`** is forced to `"unattempted"`, whatever the agent
  said. The comment records what happened when it was not: on the first full
  pipeline run an agent asserted it had already reproduced the defect,
  `_phase_reproduce` saw no `unattempted` drafts, and FORGE — the noise filter
  the entire system depends on — was skipped.

The `ToolContext` those values come from is built per agent invocation by the
conductor (`src/qaas/conductor.py:192-200`) and closed over by the server. The
agent has no handle on it.

Everything after the stamping is ordinary validation
(`envelope_server.py:145-164`):

```python
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
```

Note that a rejection is **returned**, not raised — `err()` from
`src/qaas/mcp/context.py:59-66` marks the result `isError` and the agent reads
the field-level problems and tries again. And note that a held envelope is still
persisted and still consumes the agent's finding cap: it goes to the human review
queue, not to `/dev/null`.

The other typed write tools on the same server follow the same shape, each
refusing the wrong caller by name:

| tool | line | caller check |
|---|---|---|
| `put_system_map` | `envelope_server.py:192-194` | `Only CARTOGRAPHER may publish the system map.` |
| `record_reproduction` | `envelope_server.py:293-295` | `Only FORGE records reproduction verdicts.` |
| `record_verdict` | `envelope_server.py:357-359` | `Only PROOF records verification verdicts.` |
| `record_review` | `envelope_server.py:416-418` | `Only ARBITER records review decisions.` |

### Where it lands

`RunStore.put_envelope`, `src/qaas/store.py:167-182`:

```python
    def put_envelope(self, envelope: DefectEnvelope) -> Path:
        """Persist an envelope, stamping its fingerprint if absent."""
        if envelope.dedupe.fingerprint is None:
            envelope = envelope.with_fingerprint()
        path = self.dir / "envelopes" / f"{envelope.id}.json"
        path.write_text(envelope.to_json())
        self.log(
            "envelope",
            agent=envelope.discovered_by,
            envelope_id=envelope.id,
            ...
```

One JSON file per finding under `.qaas/runs/<run-id>/envelopes/`, plus one
`envelope` line in the ledger. `envelopes()` (`store.py:184-186`) globs and
re-validates on read, so a hand-edited file fails at load rather than halfway
through the run.

---

## 6. The phase pipeline

`src/qaas/conductor.py` is a plain Python state machine — no model, no prompt.
Its docstring (`conductor.py:1-15`) explains why:

```python
"""CONDUCTOR — the run state machine.

Deliberately not an LLM. §4.1 wants its reasoning shallow (routing, not analysis)
and §10 makes it the enforcement point for budget and concurrency — and a model
cannot enforce a budget it is itself spending.
```

The run is five lines, `conductor.py:228-235`:

```python
            map_version = await self._phase_map(specs, store, budget, report)
            await self._phase_discover(specs, store, budget, report, mode, map_version)
            await self._phase_reproduce(specs, store, budget, report, map_version)
            if run_mode.files_tickets:
                await self._phase_file(specs, store, budget, report, map_version)
            else:
                store.log("skipped", reason="mode does not file tickets", mode=mode)
            await self._phase_verify(specs, store, budget, report, map_version)
```

| phase | method | agent(s) | reads | writes |
|---|---|---|---|---|
| map | `_phase_map` (`:248`) | CARTOGRAPHER | the target repo | the system map |
| discover | `_phase_discover` (`:269`) | CONDUIT, SURFACE | the map | envelopes, `status=unattempted` |
| reproduce | `_phase_reproduce` (`:290`) | FORGE | envelopes with `status=unattempted` | `reproduction` on each |
| file | `_phase_file` (`:318`) | CLERK | envelopes where `is_fileable()` | `jira.key` |
| verify | `_phase_verify` (`:332`) | PROOF (+ MENDER, ARBITER) | envelopes with `jira.key` | ledger verdicts |

The ordering is not tidiness. Each phase's input query is literally the previous
phase's output field.

**Discovery → reproduce** (`conductor.py:300`):

```python
        drafts = [e for e in store.envelopes() if e.reproduction.status.value == "unattempted"]
```

**Reproduce → file** (`conductor.py:322-325`):

```python
        fileable = [
            e for e in store.envelopes()
            if e.is_fileable(self.config.thresholds.min_confidence_to_file)[0]
        ]
```

**File → verify** (`conductor.py:336`):

```python
        pending = [e for e in store.envelopes() if e.jira.key]
```

Three one-line queries over the same on-disk collection. No message passing, no
queue, no shared memory between agents. The envelope store *is* the channel.

### FORGE runs once per finding

`_phase_reproduce`, `conductor.py:290-316`:

```python
    async def _phase_reproduce(self, specs, store, budget, report, map_version) -> None:
        """One FORGE invocation per finding.

        Separate contexts on purpose: reproducing finding B should not inherit
        whatever FORGE talked itself into while working on finding A.
        """
```

```python
        jobs = [
            (spec, tasks.forge(draft, self.config, self.config.thresholds.flake_runs))
            for draft in drafts
        ]
        await self._gather(jobs, store, budget, report, map_version, concurrency=2)
```

One job per draft, each a separate `_dispatch` → `run_agent` → `query()`. Two
consequences worth naming:

- **Cost scales with findings, not with agents.** Twelve findings is twelve FORGE
  invocations. This is why `max_findings_per_agent_run` (25, in
  `system.yaml:14`) exists and why `_phase_reproduce` triages down to it by
  severity when exceeded (`conductor.py:305-310`).
- **No cross-contamination.** FORGE cannot carry a theory from finding A into
  finding B, because it has no memory of A.

The task string that carries the envelope into that fresh context is
`tasks.forge` (`src/qaas/tasks.py:221-254`) — every line of it built from typed
fields:

```python
    return f"""Reproduce this finding, or demote it.

  id:         {envelope.id}
  reported by {envelope.discovered_by} as {envelope.severity.value} / {envelope.domain.value}
  title:      {envelope.title}
  summary:    {envelope.summary}
  location:   {envelope.location.model_dump(exclude_none=True)}
  confidence: {envelope.confidence:.2f}
```

---

## 7. The conductor reads decisions out of the ledger

This is the part that surprises people, and it is worth dwelling on.

PROOF returns a verdict. MENDER writes to a branch. ARBITER approves or rejects.
The conductor needs all three to route. It gets none of them from the agents'
final messages.

`src/qaas/conductor.py:462-471`:

```python
    @staticmethod
    def _latest_verdict(store, ticket_key: str) -> str | None:
        """PROOF's verdict is a typed ledger entry, never parsed from prose."""
        verdicts = [e for e in store.ledger("verdict") if e.detail.get("ticket_key") == ticket_key]
        return verdicts[-1].detail.get("verdict") if verdicts else None

    @staticmethod
    def _latest_review(store, ticket_key: str) -> str | None:
        reviews = [e for e in store.ledger("review") if e.detail.get("ticket_key") == ticket_key]
        return reviews[-1].detail.get("decision") if reviews else None
```

The loop reads the *append-only audit log*. The chain is:

1. PROOF calls `mcp__envelope__record_verdict`.
2. That handler (`envelope_server.py:357-380`) validates and calls
   `ctx.store.log("verdict", agent="PROOF", ticket_key=..., verdict=..., ...)`.
3. The line is appended to `.qaas/runs/<id>/ledger.jsonl`.
4. `_latest_verdict` filters the ledger for `kind == "verdict"` and this ticket,
   and takes the last one.

Three properties fall out of this that prose parsing does not give you:

- **The routing value is validated at the point it is created.** `record_verdict`
  will not accept a `VERIFIED` with no `ran` list (`envelope_server.py:362-368`):
  *"A verdict nobody can audit is not a verdict."* `record_review` will not
  accept a decision with under 40 characters of reasoning, or a
  `REQUEST_CHANGES` with no concerns (`envelope_server.py:426-435`). By the time
  the conductor reads it, it is already known good.
- **The audit trail and the control flow are the same object.** There is no
  possible drift between what the run did and what the log says it did, because
  the log is what the run consulted.
- **Resumption is free.** `qaas run --run-id <existing>` re-reads the same file.

`LedgerKind` (`src/qaas/store.py:30-86`) is a closed `StrEnum` for exactly this
reason, and its docstring says so:

```python
    `"kind":"denial"`, and every ledger already on disk still parses. Adding a
    kind means adding a member here -- deliberately a visible act, because the
    conductor reads several of these back for control flow (`_latest_verdict`,
    `_latest_review`, `_branch_written_since`), so a rename is a breaking
    change to a wire format, not a rename.
```

A typo at a `store.log()` call site used to invent a kind nobody would ever look
for. Now it fails at the write.

### `_branch_written_since`, and the bug that produced it

The third reader is the strangest, and the comment above it explains a live
failure. `conductor.py:361-370`:

```python
        # The envelope names the *repro* branch, which by construction carries a
        # failing test and no fix -- it is written before any fix exists. Sending
        # PROOF back there after a remediation round made this loop unable to
        # ever reach VERIFIED: MENDER would fix, ARBITER approve, and PROOF
        # re-verify the unfixed branch it had just failed on, burn a reopen and
        # escalate. A live run recorded exactly that ("this branch cannot carry
        # a fix"). Where MENDER put the fix is only knowable after the fact, so
        # it is read back out of the ledger below.
```

The envelope's `reproduction.environment.branch` is a `qa/repro/*` branch FORGE
created. It is correct for the first PROOF pass and wrong for every one after,
because the fix lives on a `fix/*` branch whose name MENDER chose at runtime.

`conductor.py:445-460`:

```python
    @staticmethod
    def _branch_written_since(store, mark: int) -> str | None:
        """The branch MENDER actually wrote to during one remediation round.

        Scoped to the ledger entries added since `mark` rather than searched
        run-wide, because a run verifies several tickets against one ledger and
        an earlier ticket's `fix/*` branch is the wrong answer here. The last
        write wins: MENDER ends a successful round on `push` or `open_pr`.
        """
        for entry in reversed(list(store.ledger("vcs"))[mark:]):
            if entry.agent != "MENDER":
                continue
            branch = entry.detail.get("branch")
            if branch:
                return str(branch)
        return None
```

`mark` is taken before remediation starts (`conductor.py:400`):

```python
            mark = len(list(store.ledger("vcs")))
```

Here are the real `vcs` entries by MENDER from a real run, with their line
number in the ledger:

```console
$ .venv/bin/python -c "
import json
p='.qaas/runs/run-20260907T233304-ca7657/ledger.jsonl'
for i, line in enumerate(open(p)):
    e = json.loads(line)
    if e['kind'] == 'vcs' and e.get('agent') == 'MENDER' and e['detail'].get('branch'):
        print(i, e['detail'].get('action'), '|', e['detail']['branch'])
"
1177 create_branch | fix/CORVID-7-orders-limit-not-applied
1203 commit | fix/CORVID-7-orders-limit-not-applied
1301 create_branch | fix/CORVID-7-orders-limit
1326 commit | fix/CORVID-7-orders-limit
1431 create_branch | fix/CORVID-7-orders-limit-ledger
1447 commit | fix/CORVID-7-orders-limit-ledger
1562 create_branch | fix/CORVID-8-invoice-currency
1573 write_file | fix/CORVID-8-invoice-currency
1592 commit | fix/CORVID-8-invoice-currency
```

The scoping is not theoretical. When CORVID-8's remediation round finished, an
unscoped search would have found `fix/CORVID-7-orders-limit-ledger` — CORVID-7's
branch, from earlier in the same ledger. `mark` cuts it off.

---

## 8. The remediation loop and its bounds

`_verify_loop`, `conductor.py:349-405`. Its docstring states the design in two
sentences:

```python
        """PROOF -> NOT_FIXED -> remediate -> PROOF, bounded by §8.3.

        The bound is the point. Without `max_proof_reopens` a fix that keeps
        missing the defect cycles until the budget is gone, and the run ends with
        no verdict and no money left to reach one. Escalating after one reopen
        costs a human five minutes; not escalating costs the whole run.
        """
```

The shape:

```
PROOF ──► VERIFIED   ──► done
      ──► REGRESSED  ──► escalate (a human looks at it)
      ──► no verdict ──► escalate
      ──► NOT_FIXED  ──► reopens >= max_proof_reopens ? escalate
                          : MENDER ──► ARBITER ──► APPROVE           ──► PROOF again
                                                ──► ESCALATE_TO_HUMAN ──► escalate
                                                ──► REQUEST_CHANGES   ──► MENDER again
```

Two bounds, both in `src/qaas/defaults/config/system.yaml:17-18`:

```yaml
  max_mender_arbiter_round_trips: 2
  max_proof_reopens: 1
```

**`max_proof_reopens`** caps the outer loop (`conductor.py:392-399`):

```python
            if reopens >= max_reopens:
                self._escalate(report, store, "PROOF",
                    f"{ticket}: still NOT_FIXED after {reopens} reopen(s), the limit. "
                    "Escalating rather than cycling further")
                return
```

**`max_mender_arbiter_round_trips`** caps the inner one
(`conductor.py:423-443`):

```python
        for trip in range(1, self.config.thresholds.max_mender_arbiter_round_trips + 1):
            budget.check()
            await self._dispatch(mender, store, budget, report,
                                 tasks.mender(ticket, envelope), map_version)
            if arbiter is None:
                return True

            await self._dispatch(arbiter, store, budget, report,
                                 tasks.arbiter(ticket, envelope), map_version)
            review = self._latest_review(store, ticket)
            if review == "APPROVE":
                return True
            if review == "ESCALATE_TO_HUMAN":
                self._escalate(report, store, "ARBITER", f"{ticket}: ARBITER escalated the fix")
                return False
            store.log("review_round_trip", agent="ARBITER", ticket_key=ticket, trip=trip)
```

There is a third bound outside both loops: `Budget.check()`
(`conductor.py:138-144`) runs before every dispatch and raises `BudgetExceeded`,
which `run()` catches and records as `stopped_early`. `BudgetExceeded` is
declared as *"Not an error — a control working"* (`conductor.py:34-36`).

And a fourth, which is really an absence: `_remediate` handles the case where
MENDER is not in the roster at all (`conductor.py:415-421`):

```python
        if mender is None:
            self._escalate(report, store, "PROOF",
                f"{ticket}: NOT_FIXED and no MENDER in this run's roster. "
                "Nothing here can produce a fix; a human takes it from here")
            return False
```

A Phase 1 roster has no MENDER. Escalating immediately is better than re-running
PROOF against code that nothing has changed.

### The loop, in real data

The ledger of `run-20260907T233304-ca7657`, filtered to the control-flow kinds
for one ticket:

```console
$ .venv/bin/python -c "
import json
KINDS = {'verdict','reopened','review','review_round_trip','verified'}
p='.qaas/runs/run-20260907T233304-ca7657/ledger.jsonl'
for line in open(p):
    e = json.loads(line)
    d = e['detail']
    if e['kind'] in KINDS and d.get('ticket_key') == 'CORVID-8':
        print(f\"{e['kind']:18} {e['agent']:8} {d.get('verdict') or d.get('decision') or ''}\")
"
verdict            PROOF    NOT_FIXED
reopened           PROOF
review             ARBITER  APPROVE
verdict            PROOF    VERIFIED
verified           PROOF
```

One reopen, one MENDER/ARBITER round trip, `APPROVE`, re-verify on the branch
found by `_branch_written_since` (`fix/CORVID-8-invoice-currency`), `VERIFIED`.
The loop closed inside its bounds — and `verified` carried `reopens=1`.

CORVID-7, in the same ledger, did not. (This run id was resumed several times —
`qaas show` reports five `run_started` lines — so CORVID-7 was attempted on three
separate passes. All three are here.)

```console
$ ...same command, with ticket_key == 'CORVID-7'
verdict            PROOF    NOT_FIXED
reopened           PROOF
review             ARBITER  ESCALATE_TO_HUMAN
verdict            PROOF    NOT_FIXED
reopened           PROOF
review             ARBITER  ESCALATE_TO_HUMAN
verdict            PROOF    NOT_FIXED
reopened           PROOF
review             ARBITER  REQUEST_CHANGES
review_round_trip  ARBITER
review             ARBITER  ESCALATE_TO_HUMAN
```

Read the third pass: `NOT_FIXED`, one reopen, MENDER fixes, ARBITER says
`REQUEST_CHANGES`, `review_round_trip` is logged, MENDER tries again, ARBITER
says `ESCALATE_TO_HUMAN`. The loop stopped there — it did not go round a third
time, because `max_mender_arbiter_round_trips` is 2 and an `ESCALATE_TO_HUMAN`
exits immediately regardless. That is the design working: the ticket became a
human's problem, and the run kept its budget for the other nine.

`qaas show` reads the same lines back:

```console
$ .venv/bin/qaas show run-20260907T233304-ca7657
...
tickets filed (10)
  CORVID-SEC-6  no verdict
  CORVID-7  NOT_FIXED
  CORVID-8  VERIFIED
  CORVID-9  no verdict
...
```

---

## 9. What to take away

- One type crosses every agent boundary. There is no second channel.
- Rules that must always hold are **methods on that type**, not sentences in a
  prompt. `has_evidence()` and `is_fileable()` cannot be argued with, and their
  four call sites cannot disagree.
- Identity is structural and computed, never asserted. `fingerprint()` hashes
  what a defect *is*, not how it was described — and the one time that
  normalisation was subtly wrong, recurrence tracking silently died for the life
  of the project.
- The fields that establish provenance (`run_id`, `discovered_by`) and the field
  that gates triage (`reproduction.status`) are written by the server, not the
  agent.
- The conductor routes on typed ledger entries. Its control flow and its audit
  trail are the same file.

To see all of this for yourself without spending anything:

```console
$ .venv/bin/python -m pytest tests/test_envelope.py -q
..........................                                               [100%]
26 passed in 0.05s
```
