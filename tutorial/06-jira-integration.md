# 06 — How tickets reach Jira

This chapter follows one ticket from the moment CLERK decides to file it to the
moment Jira answers the POST, and explains why the code is split the way it is.

If you are configuring a real Jira instance rather than reading the code,
`docs/jira-setup.md` is the operator's guide — permissions, environment
variables, first-contact procedure, troubleshooting table. This chapter is about
the machinery underneath it.

---

## The split that shapes everything

There are two files, and the line between them is the whole design.

| File | Owns | Why |
|---|---|---|
| `src/qaas/mcp/tracker.py` | **Policy** — who may file, how many, where security findings go | Written once, holds for every backend |
| `src/qaas/adapters/tracker.py` | **Mechanics** — how an issue is stored | Swappable; a new backend is a new subclass |

The adapter module says so in its own opening lines
(`src/qaas/adapters/tracker.py:1-8`):

```python
"""Tracker adapters — the issue tracker behind one interface.

The MCP server in `qaas.mcp.tracker` holds the policy (who may file, how many,
where security findings go). This module holds only storage mechanics, so that
pointing the system at real Jira is a new subclass and nothing else. That split
matters: guardrails that live in the adapter would have to be re-implemented,
and re-audited, for every backend.
"""
```

The last sentence is the reason. If the "security findings never go to a public
project" rule lived in `JiraTracker`, then `LocalTracker` would need its own copy
of it, and so would the next backend, and each copy would need its own audit. One
rule, one place, enforced identically whether the ticket lands in a JSON file or
on someone's board.

The MCP server states the other half of the same argument
(`src/qaas/mcp/tracker.py:3-8`):

```python
The rules below are code, not prompt text, and that is deliberate. §8.1 says
only CLERK creates and only CLERK and PROOF transition; §4.12 caps tickets per
run and requires that hitting the cap escalates instead of filing; §10 lists
"security findings leak into public tickets" as a named failure mode. A prompt
can be argued with, misread, or dropped from a truncated context. A refusal
returned from the tool cannot.
```

---

## The interface

`TrackerAdapter` is an ABC at `src/qaas/adapters/tracker.py:105`. It declares
five abstract methods:

| Method | Line | Contract |
|---|---|---|
| `create_issue` | 109 | File a new issue and return it, key assigned |
| `transition` | 124 | Move an issue to a new status, recording who and why |
| `link` | 128 | Relate two existing issues |
| `search` | 132 | Return matching issues, newest first |
| `get` | 146 | One issue by key, or None |

(The class docstring at line 106 still says "The four operations" — a stale
count, not a fifth method hiding somewhere.)

Everything crosses that boundary as one Pydantic model, `Issue`
(`src/qaas/adapters/tracker.py:79`), described as "One tracker issue, in the
shape both backends agree to speak." It carries `envelope_id` and `fingerprint`
as first-class fields, which matters later: those are how a ticket gets traced
back to the finding that produced it.

Two vocabularies are closed sets, defined at lines 36-37:

```python
STATUSES = ("open", "in_progress", "in_review", "resolved", "closed", "wont_fix", "duplicate")
LINK_TYPES = ("duplicates", "relates", "blocks", "blocked-by", "regression-of", "caused-by")
```

The comment above them explains the closure: "so an agent that invents a status
gets the list back and retries rather than writing a state nothing downstream can
interpret." Validation for both lives on the base class
(`_check_status`, line 174; `_check_link_type`, line 180) rather than in each
subclass, "so every backend rejects the same inputs with the same message; an
agent should not have to learn two dialects."

### The routing surface

The base class also exposes two properties the MCP server asks about
(`src/qaas/adapters/tracker.py:149-167`):

```python
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
```

Read that `None` docstring twice. It is the contract that makes the refusal at
the top of this system possible: the adapter can say "I have no restricted
project", and the only legal response is to file nothing.

---

## `LocalTracker` — the default

`src/qaas/adapters/tracker.py:188`. Issues are JSON files under
`<root>/tickets/`. No credentials, no network.

```python
class LocalTracker(TrackerAdapter):
    """Issues as JSON files under `<root>/tickets/`.

    The key sequence is shared across projects and derived from what is already
    on disk, so keys stay monotonic and unique across runs and across processes
    without a database. Reusing a key would silently rewrite a ticket an
    engineer may already be looking at, so allocation never counts down.
    """
```

Key allocation is `_next_key` (line 229): `max` of every number already on disk,
plus one. That is deliberately not a counter in a file — a counter can be reset,
and a reset counter overwrites CORVID-3 with a different defect.

This is the backend you should read output from first. `.qaas/tickets/*.json` is
exactly what would have been filed, on your own disk.

---

## `JiraTracker` — Jira Cloud over REST v3

`src/qaas/adapters/tracker.py:537`. Its docstring is the authoritative list of
what it reads from the environment:

```python
    Required:
      * ``JIRA_BASE_URL``   — e.g. ``https://acme.atlassian.net``
      * ``JIRA_EMAIL``      — the bot account's Atlassian account email
      * ``JIRA_API_TOKEN``  — an API token, not a password (Jira Cloud uses
        HTTP Basic with email + token)
      * ``JIRA_PROJECT_KEY``— the default project, e.g. ``CORVID``

    Optional:
      * ``JIRA_SECURITY_PROJECT_KEY`` — the restricted project. When it is
        unset, `security_project` is None and the MCP server refuses to file
        security findings at all. ...
      * ``JIRA_ISSUE_TYPE`` — the issue type to create, default ``Bug``. Not
        every project has a type called Bug.
```

The required four are named as a class attribute at line 566, so the CLI and the
tests can enumerate them without duplicating the list:

```python
    REQUIRED_ENV = (
        "JIRA_BASE_URL",
        "JIRA_EMAIL",
        "JIRA_API_TOKEN",
        "JIRA_PROJECT_KEY",
    )
    SECURITY_ENV = "JIRA_SECURITY_PROJECT_KEY"
    ISSUE_TYPE_ENV = "JIRA_ISSUE_TYPE"
```

### Where they are read: at construction, not at first use

`__init__` (line 575) reads every variable and validates it immediately. If any
of the four is unset or empty, it raises `TrackerConfigError` before the object
exists. The reason is written into that exception class
(`src/qaas/adapters/tracker.py:54-60`):

```python
class TrackerConfigError(TrackerError):
    """The tracker itself is misconfigured — wrong credentials, missing settings.

    Raised at construction, never mid-run. A tracker that only discovers it
    cannot reach Jira after twenty findings have been produced has thrown away
    the run: the findings are gone from context and nobody can act on them.
    """
```

The missing-variable message (`_missing_env_message`, line 608) names *every*
missing variable at once, not the first: "Listing only the first missing variable
turns one restart into four."

Credentials come from `os.environ` and nowhere else. The class docstring is blunt
about it: "Credentials never come from `config/` — that directory is committed."

---

## `build_tracker` — the one place the choice is made

`src/qaas/adapters/tracker.py:1301`:

```python
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
```

It is called once, from the MCP server's `build_tools`
(`src/qaas/mcp/tracker.py:112-114`):

```python
    # Built even in dry-run mode: constructing a JiraTracker is what validates
    # the credentials, and a rehearsal against a configuration that could never
    # have worked proves nothing.
    tracker = build_tracker(ctx.config.tracker, ctx.store.root)
```

There is also a third subclass, `JiraDataCenterTracker`
(`src/qaas/adapters/tracker.py:1256`), which is a stub that raises. It is not an
unfinished feature — it is a marker that Data Center is a *different* integration
(PAT auth, REST v2, plain-text descriptions rather than ADF), and that
subclassing `JiraTracker` for it would inherit exactly the machinery that is
wrong there.

---

## Choosing the backend: `config.tracker` and `QAAS_TRACKER`

The field is a `Literal` on `SystemConfig` (`src/qaas/config.py:192-193`):

```python
    #: Overridable with QAAS_TRACKER. Keep the committed value `local`.
    tracker: Literal["local", "jira"] = "local"
```

The override is applied in `load_config` (`src/qaas/config.py:321-334`):

```python
    # Backend overrides from the environment, so pointing a run at a real
    # tracker or forge is not a committed file change.
    #
    # `tracker: local` is the committed default and must stay that way. When
    # `jira` was committed instead, 18 tests failed and 14 errored: the agent
    # fixtures build a real JiraTracker, which demands credentials CI does not
    # have. The house rule is that the default `pytest` run is offline and free,
    # and a committed backend switch silently breaks it -- so the switch belongs
    # in the environment of the person who wants it, not in the repo.
    for key, var in (("tracker", TRACKER_ENV), ("vcs", VCS_ENV), ("target", TARGET_ENV)):
        raw_value = (os.environ.get(var) or "").strip()
        if raw_value:
            # Backends are lowercase literals; a target is a profile name.
            raw[key] = raw_value if key == "target" else raw_value.lower()
```

That comment records a bug that was actually hit. Someone committed
`tracker: jira` to `system.yaml`; the offline test suite immediately started
demanding Jira credentials, because the agent fixtures build a real tracker. The
constant names are at `src/qaas/config.py:264-266`:

```python
#: Environment overrides for the two swappable backends.
TRACKER_ENV = "QAAS_TRACKER"
VCS_ENV = "QAAS_VCS"
```

So the way to point a run at Jira is:

```bash
QAAS_TRACKER=jira qaas run --mode pr-check
```

Not an edit to a committed file.

---

## The preflight: `qaas tracker-check`

Defined at `src/qaas/cli.py:1513`. The section comment above it
(`src/qaas/cli.py:1242-1246`) states its purpose:

```python
# -- tracker-check ----------------------------------------------------------
# Everything below exists so that the first live run is not also the first time
# anyone finds out whether the configuration works. It makes read-only calls
# only: an operator must be able to run it against the team's real board
# without wondering what it left behind.
```

Its options:

```
$ qaas tracker-check --help
 Usage: qaas tracker-check [OPTIONS]

 Validate the tracker configuration without creating anything.
 ...
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --config          -c      <path>  Config directory.                          │
│ --root                    <path>  Runtime state directory. [default: .qaas]  │
│ --dry-run-ticket                  Also render the JSON that would be POSTed  │
│                                   for a sample finding.                      │
│ --help                            Show this message and exit.                │
╰──────────────────────────────────────────────────────────────────────────────╯
```

Against the committed default it reports the local backend and stops:

```
$ qaas tracker-check
tracker backend: local   (tracker: in .../src/qaas/defaults/config/system.yaml)

tickets are written as JSON under .qaas/tickets (16 so far)
no credentials are needed and nothing leaves this machine. Read what it files
there before switching to tracker: jira.

ready
```

With `tracker: jira`, `_tracker_check_jira` (`src/qaas/cli.py:1425`) runs four
read-only probes, each of which maps onto a Jira endpoint the adapter owns:

| Check | Adapter method | Line |
|---|---|---|
| Who am I authenticated as | `whoami()` → `GET /myself` | 861 |
| Does the project exist | `project_info()` → `GET /project/{key}` | 865 |
| Can this account write to it | `project_permissions()` → `GET /mypermissions` | 869 |
| Does the workflow speak our statuses | `project_statuses()` + `map_house_statuses()` | 888, 909 |

Those methods live on the adapter rather than in the CLI, and
`src/qaas/adapters/tracker.py:850-854` says why:

```python
    # -- configuration checks ---------------------------------------------
    # Read-only calls an operator can make before a run files anything real.
    # They live here rather than in the CLI because knowing which endpoint
    # answers "can this account create issues in this project" is Jira
    # knowledge, and this module is where Jira knowledge is allowed to be.
```

Two details worth knowing:

**The token is never printed.** `_env_display` (`src/qaas/cli.py:1301`) renders a
four-character tail and nothing else:

```python
    if name in _JIRA_SECRET_ENV:
        if len(text) < 12:
            return "[green]set[/green] (too short to show a tail safely)"
        return f"[green]set[/green] (ends ...{text[-4:]})"
```

**Only four statuses are failures.** `src/qaas/cli.py:1262-1266`:

```python
#: House statuses this system actually drives. An unmapped one here is a real
#: failure: PROOF asks for 'closed', nothing in the workflow matches, and the
#: ticket stays open while the run reports a clean close. The rest of `STATUSES`
#: are human dispositions — worth reporting, not worth failing on.
_DRIVEN_STATUSES = ("open", "in_progress", "resolved", "closed")
```

---

## The path a ticket actually takes

CLERK is the only agent with tracker write access. Its config says so
(`src/qaas/defaults/config/agents/clerk.yaml:12-21`):

```yaml
mcp_servers: [envelope, tracker, defect_memory]
builtin_tools: [Read]
policy:
  may_create_tickets: true
  max_tickets_per_run: 10           # §4.12: over cap, pause and escalate

skills: [severity-rubric, dedupe-strategy, ticket-writer, routing-rules, ownership-resolution]

# Filing nothing is allowed; filing without checking for duplicates is not.
must_call: [mcp__defect_memory__search_similar]
```

That `must_call` is enforced by the `Stop` hook, not by the prompt — CLERK cannot
end its turn without having searched for duplicates first.

When CLERK calls `mcp__tracker__create_issue`, control lands in
`src/qaas/mcp/tracker.py:139`. Here is the whole gate sequence, in order.

### 1. May this agent file at all? (`src/qaas/mcp/tracker.py:140-145`)

```python
        if not policy.may_create_tickets:
            return deny(
                "create_issue",
                f"{ctx.agent.name} may not create tickets (§8.1: CLERK only). "
                "Emit your finding as an envelope; CLERK files it.",
            )
```

`policy` is `ctx.agent.policy`, a `Policy` model (`src/qaas/config.py:27`) whose
defaults are all read-only. `deny` (line 118) does two things: logs to the ledger
and returns an error result.

```python
    def deny(tool_name: str, reason: str) -> dict[str, Any]:
        """Refuse, and leave a trace. A denial nobody can see is not a guardrail."""
        ctx.store.log("denial", agent=ctx.agent.name, tool=tool_name, reason=reason)
        return err(reason)
```

Note `err`, not `raise`. From `src/qaas/mcp/context.py:59-65`: "Tool errors are
returned, not raised: the agent should read the reason and correct itself rather
than have the turn die."

### 2. The per-run cap (`src/qaas/mcp/tracker.py:147-159`)

```python
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
```

The counter is on `ToolContext` (`src/qaas/mcp/context.py:43-48`) — a plain dict
of `bump`/`count`, rebuilt per agent invocation. The cap is not advice in a
prompt; the eleventh call returns a refusal.

### 3. Is this finding restricted? (`src/qaas/mcp/tracker.py:161-167`)

```python
        envelope = None
        if args.get("envelope_id"):
            envelope = ctx.store.get_envelope(args["envelope_id"])
            if envelope is None:
                return err(f"No envelope '{args['envelope_id']}' in this run. Emit it first.")

        restricted = bool(args.get("security_relevant")) or (envelope is not None and is_restricted(envelope))
```

`is_restricted` is at line 68:

```python
def is_restricted(envelope: DefectEnvelope) -> bool:
    """Whether this finding may only be filed into the restricted project.

    Two independent triggers, because either alone is enough to make a public
    ticket a disclosure: the reporter flagged security impact, or the defect is
    classified as a vulnerability.
    """
    return envelope.impact.security_relevant or envelope.defect_class == DefectClass.VULNERABILITY
```

### 4. Routing, including the refusal (`src/qaas/mcp/tracker.py:170-197`)

```python
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
```

**This is the most important refusal in the file.** There is no fallback. A
security finding with no restricted project available is not filed into the
public project "so at least someone sees it" — it is not filed at all, and the
agent is told to escalate through a private channel.

The asymmetry is the argument: refusing loses a ticket, and a lost ticket can be
re-found on the next run. Falling back loses control of a vulnerability, and you
cannot un-tell people.

The same block also refuses an agent that tries to *name* a public project for a
restricted finding (lines 187-194), so the routing cannot be argued around by
passing `project` explicitly.

### 5. Labels (`src/qaas/mcp/tracker.py:199-204`)

```python
        severity = args.get("severity") or (envelope.severity.value if envelope else None)
        labels = list(args.get("labels") or [])
        if "agent-found" not in labels:
            labels.append("agent-found")
        if restricted and "security" not in labels:
            labels.append("security")
```

### 6. Into the adapter (`src/qaas/mcp/tracker.py:228-240`)

```python
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
```

This is the boundary. Everything above ran identically for `local` and `jira`.

### 7. Stamp the key back onto the envelope (`src/qaas/mcp/tracker.py:242-256`)

```python
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
```

Another bug recorded in place. `DefectEnvelope.jira` is a `TrackerRef`
(`src/qaas/envelope.py:143`, referenced at line 181) holding `key`, `project` and
`status`. Without this write the discovery loop and the remediation loop never
meet: MENDER selects work by `envelope.jira.key`, and every key was `None`.

---

## The ADF conversion

Jira Cloud's REST v3 refuses a plain string for `description`. It wants an
Atlassian Document Format node tree. `markdown_to_adf`
(`src/qaas/adapters/tracker.py:335`) is what stands in the gap:

```python
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

    A ticket that needs richer rendering should link to an artifact.
    """
```

Two design choices worth naming:

- **No markdown dependency.** One field does not justify a parser in the
  dependency list, and a general parser would emit node types this converter
  would then have to map anyway.
- **Unsupported syntax survives as literal text.** `**blocker**` reaches the
  reader as `**blocker**` rather than vanishing. A body with pieces silently
  missing is unreadable; a body with stray asterisks is merely ugly.

`adf_to_text` (line 440) is the reverse, used only when reading an issue back so
that `Issue.body` is greppable. It is documented as lossy on purpose: "pretending
otherwise would invite callers to trust it."

There is one non-obvious ADF rule encoded at line 322:

```python
def _adf_text(text: str) -> list[dict[str, Any]]:
    """ADF forbids an empty text node, so an empty string yields no children."""
    return [{"type": "text", "text": text}] if text else []
```

---

## Labels, and why the metadata rides on them

`src/qaas/adapters/tracker.py:477-482`:

```python
#: House metadata rides on labels because they are the only field guaranteed to
#: exist in every Jira project. Custom fields differ per instance and screen, so
#: writing to one is the fastest way to a 400 on someone else's Jira.
SEVERITY_LABEL_PREFIX = "severity-"
ENVELOPE_LABEL_PREFIX = "qaas-envelope-"
FINGERPRINT_LABEL_PREFIX = "qaas-fp-"
```

| Label | Carries | Used for |
|---|---|---|
| `agent-found` | — | Every ticket this system files |
| `security` | — | Anything routed as restricted |
| `severity-<level>` | `Issue.severity` | Board filtering |
| `qaas-envelope-<uuid>` | `Issue.envelope_id` | Trace a ticket back to the finding and the run |
| `qaas-fp-<64 hex>` | `Issue.fingerprint` | **Cross-run dedupe** |

### `qaas-fp-*` and the two rules that make dedupe work

The fingerprint comes from `DefectEnvelope.fingerprint()`
(`src/qaas/envelope.py:228`), which "deliberately excludes prose, line numbers,
commit sha, run id and timestamps: the same defect reported by two agents in
different words, or found again after the file moved a few lines, must hash the
same."

Rule one — drop the algorithm prefix, keep the digest whole
(`src/qaas/adapters/tracker.py:511-519`):

```python
def _fingerprint_label_value(fingerprint: str | None) -> str | None:
    """The digest half of a fingerprint, e.g. `sha256:ab12…` -> `ab12…`.

    The algorithm prefix is dropped because a colon in a Jira label is not
    worth betting a 400 on. The digest itself is kept whole: dedupe matches on
    exact equality, and a truncated fingerprint would quietly collide.
    """
    digest = (fingerprint or "").split(":")[-1].strip()
    return digest or None
```

The prefix is put back on the way out (`_issue_from_jira`, lines 805-807), so a
fingerprint read from Jira compares equal to one computed from an envelope.

Rule two — an unusable value produces no label at all
(`src/qaas/adapters/tracker.py:522-534`):

```python
def _label_safe(value: str | None, prefix: str = "") -> str | None:
    """A Jira label, or None if the value cannot be one.

    Jira rejects labels containing whitespace, and caps them at 255 characters.
    Silently mangling an id would produce a label that never matches on the way
    back out, so an unusable value produces no label at all.
    """
```

A mangled label is worse than a missing one: it looks present and never matches,
so it silently breaks the thing it exists for.

CLERK searches for those labels before filing. Its `search` is JQL scoped to the
configured projects (`src/qaas/adapters/tracker.py:1209-1213`): "Unscoped, this
would sweep every project the bot can see — slow, and it drags unrelated tickets
into an agent's context where they read as prior art."

---

## Writes are never retried

`_request` (`src/qaas/adapters/tracker.py:647`) takes a `retry_on_429` flag, and
its docstring is the whole rule:

```python
        """One Jira call. `retry_on_429` is only ever true for reads.

        A retried POST /issue is a duplicate ticket, and duplicate tickets are
        precisely what this system exists to prevent: a 429 can arrive after
        Jira has already created the issue, so the second attempt files it
        twice. Reads are idempotent and honour `Retry-After`.
        """
```

`create_issue` passes `retry_on_429=False` (line 1017). So does `link`
(line 1194). `whoami`, `project_info`, `project_permissions`,
`project_statuses`, `get` and `search` all pass `True`.

Related: after Jira answers the POST, reading the issue back is treated as a
convenience, not the record (`src/qaas/adapters/tracker.py:1027-1033`):

```python
        # Reading the issue back is a convenience, not the record. The ticket
        # exists the moment Jira answered the POST, so a failure here must not
        # lose the key — a filed ticket nobody can name is a stranded ticket.
        try:
            return self.get(key) or fallback
        except TrackerError:
            return fallback
```

---

## A worked example

### What CLERK filed with `tracker: local`

This is a real ticket from this repository's `.qaas/tickets/CORVID-1.json`,
abbreviated in the body only:

```json
{
  "key": "CORVID-1",
  "project": "CORVID",
  "title": "Checkout 'Back to order form' clears session state and orphans the draft order",
  "body": "**What breaks, for whom, how often:** When a user clicks \"Back to order form\" ...",
  "status": "open",
  "labels": [
    "agent-found"
  ],
  "severity": "major",
  "envelope_id": "3f980be7-c30c-4a93-9b9b-ea2828af2844",
  "fingerprint": "sha256:bbe668f5b468a14e3f2fd8f979099a7a7679f42382a2c6e9c96d604d5f900fd3",
  "reporter": "CLERK",
  "links": [],
  "history": [
    {
      "at": "2026-09-06T21:46:24.482844Z",
      "status": "open",
      "by": "CLERK",
      "comment": "filed"
    }
  ],
  "created_at": "2026-09-06T21:46:24.482858Z",
  "updated_at": "2026-09-06T21:46:24.482859Z"
}
```

Note `reporter: "CLERK"` — the adapter records the agent that filed. `LocalTracker`
can store that faithfully; Jira cannot, which is the next subsection.

### The same shape, on its way to Jira

`create_payload` (`src/qaas/adapters/tracker.py:963`) builds the exact body
`create_issue` POSTs. It is split out for a reason stated in its docstring:

```python
        """The exact JSON body `create_issue` would POST to `/issue`.

        Split out so `qaas tracker-check --dry-run-ticket` can show an operator
        the ADF and the labels before a real ticket lands in front of real
        people. It must be the same code path: a preview that *reconstructs*
        the payload is correct only until the day it drifts, and it would be
        trusted either way.
        """
```

Here is real output from calling it with a security-relevant finding
(`JIRA_PROJECT_KEY=CORVID`, `JIRA_SECURITY_PROJECT_KEY=CORVID-SEC`, no network
involved — `create_payload` sends nothing):

```json
{
  "fields": {
    "project": { "key": "CORVID-SEC" },
    "summary": "Refund endpoint accepts any authenticated user",
    "description": {
      "type": "doc",
      "version": 1,
      "content": [
        { "type": "heading", "attrs": { "level": 2 },
          "content": [ { "type": "text", "text": "Repro" } ] },
        { "type": "bulletList",
          "content": [
            { "type": "listItem", "content": [ { "type": "paragraph",
              "content": [ { "type": "text", "text": "authenticate as an ordinary customer" } ] } ] },
            { "type": "listItem", "content": [ { "type": "paragraph",
              "content": [ { "type": "text", "text": "POST /v1/orders/{id}/refund for another account's order" } ] } ] }
          ] },
        { "type": "codeBlock", "attrs": { "language": "bash" },
          "content": [ { "type": "text",
            "text": "curl -X POST -H \"Authorization: Bearer $TOKEN\" https://api.example.com/v1/orders/9001/refund" } ] },
        { "type": "heading", "attrs": { "level": 2 },
          "content": [ { "type": "text", "text": "Impact" } ] },
        { "type": "paragraph",
          "content": [ { "type": "text", "text": "Any authenticated user can refund any order." } ] },
        { "type": "paragraph",
          "content": [ { "type": "text", "text": "Filed by CLERK (automated QA)." } ] }
      ]
    },
    "issuetype": { "name": "Bug" },
    "labels": [
      "agent-found",
      "qaas-envelope-3f980be7-c30c-4a93-9b9b-ea2828af2844",
      "qaas-fp-bbe668f5b468a14e3f2fd8f979099a7a7679f42382a2c6e9c96d604d5f900fd3",
      "security",
      "severity-critical"
    ]
  }
}
```

Five things to read off that payload:

1. `project.key` is `CORVID-SEC`, not `CORVID` — routed, not requested.
2. The markdown became a `doc` node tree. Headings, bullets and the fenced block
   with its language attribute all survived.
3. `"Filed by CLERK (automated QA)."` was appended as a trailing paragraph.
   `_description` (line 930) explains why: "Jira sets `reporter` from the
   credential whatever we send, so the agent that found the defect has to be
   recorded somewhere that stays true."
4. `summary` is truncated to 255 characters at line 988 — "Jira's summary limit;
   a 400 here is silly."
5. Labels are sorted and deduped, with the fingerprint's `sha256:` prefix
   stripped.

You can see this for your own configuration without sending anything:

```bash
QAAS_TRACKER=jira qaas tracker-check --dry-run-ticket
```

---

## The rehearsal rail: `QAAS_TRACKER_DRY_RUN`

`src/qaas/mcp/tracker.py:36-50`:

```python
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
```

Three details that make the rehearsal honest:

- **The cap is still consumed** (`src/qaas/mcp/tracker.py:206-209`): "a rehearsal
  that ignores the rate limit is not a rehearsal of the run you are about to do."
- **The ledger kind is `dry_run`, never `ticket`** (lines 123-131): "anything
  counting filed tickets must not count these, and a flag on a `ticket` entry is
  one missed `if` away from being counted."
- **`search` stays live** (lines 384-385): dedupe against an imaginary backlog
  rehearses a run that files things the real one would not.

Read it back afterwards:

```bash
grep dry_run .qaas/runs/<run-id>/ledger.jsonl
```

---

## How this is tested without a Jira instance

`tests/adapters/test_jira_tracker.py:1-13`:

```python
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
```

That one live test is `test_a_real_jira_accepts_what_this_adapter_sends`
(line 716). It files an actual ticket titled "qaas adapter smoke test — safe to
close", and runs only under `pytest -m jira`. Chapter 07 covers why the markers
are deselected by default.

---

## Summary

| Question | Answer | Where |
|---|---|---|
| Which backend? | `config.tracker`, overridden by `QAAS_TRACKER` | `config.py:193`, `config.py:330` |
| Who may file? | `policy.may_create_tickets` — CLERK only | `mcp/tracker.py:140` |
| How many? | `policy.max_tickets_per_run`, default 10; over cap is an escalation | `mcp/tracker.py:147` |
| Where does a security finding go? | The restricted project, or nowhere | `mcp/tracker.py:176-194` |
| What if no restricted project? | Refused. No fallback. | `mcp/tracker.py:178-186` |
| Why ADF? | Jira Cloud REST v3 rejects a plain string | `adapters/tracker.py:335` |
| How is a duplicate found next week? | `qaas-fp-<digest>` label, exact match | `adapters/tracker.py:511` |
| How do I check before filing? | `qaas tracker-check [--dry-run-ticket]` | `cli.py:1513` |
| How do I rehearse a whole run? | `QAAS_TRACKER_DRY_RUN=1` | `mcp/tracker.py:41` |
