# Filing into real Jira

This system can file its findings into Jira Cloud. Doing that badly — a wrong
project key, a security finding in a public project, forty duplicate tickets on
a Monday morning — is worse than not doing it at all, because the team stops
reading anything the system files and you do not get a second chance at that.

So this page is written to be followed in order. It assumes you have never seen
this system before.

**Jira Server and Jira Data Center are not supported.** See
[the last section](#jira-server--data-center-is-not-supported).

---

## 1. What you need in Jira

An **Atlassian account for the bot**. Use a dedicated account, not your own.
Everything this system files will be attributed to it — Jira sets the reporter
from the credential and ignores anything the API says — and you want to be able
to revoke it without locking yourself out.

An **API token** for that account, created at
<https://id.atlassian.com/manage-profile/security/api-tokens>. Click *Create API
token*, give it a label you will recognise in six months, and copy the value: it
is shown once. A password will not work. Neither will a token minted from a
different Atlassian account than the email you configure.

A **project to file into**, and the bot account needs these four permissions on
it:

| Permission | Jira's name for it | What breaks without it |
|---|---|---|
| Browse | `Browse Projects` | Nothing can be read back. Every create appears to fail even when it succeeded, and dedupe sees an empty backlog. |
| Create | `Create Issues` | Nothing can be filed. A run produces findings and files none of them. |
| Transition | `Transition Issues` | VERIFIER cannot close a ticket it verified. The fix ships and the ticket sits open. |
| Link | `Link Issues` | Duplicates cannot be linked to the original, and regressions cannot be linked to the ticket they regress. |

Grant them to a project role the bot account is in, rather than to the account
directly, so the next bot inherits them.

A **second, restricted project** for security findings. This is optional and the
consequence of skipping it is deliberate — see
[`JIRA_SECURITY_PROJECT_KEY`](#the-environment) below.

---

## 2. The environment

Credentials come from the environment, never from `config/` — that directory is
committed. Export these in the shell that runs `qaas`.

| Variable | Required | What it does | What breaks without it |
|---|---|---|---|
| `JIRA_BASE_URL` | yes | Your site root, e.g. `https://acme.atlassian.net`. No `/jira`, no `/rest/api` path, and the scheme is not optional. | The tracker refuses to construct. A value without a scheme is rejected by name at startup. |
| `JIRA_EMAIL` | yes | The Atlassian account email the API token was minted for. Jira Cloud authenticates with HTTP Basic over email + token. | 401 on every call. A token paired with the wrong email is indistinguishable from a revoked one. |
| `JIRA_API_TOKEN` | yes | An API token from the URL above. | 401 on every call. |
| `JIRA_PROJECT_KEY` | yes | The project ordinary findings go to, e.g. `ENG`. | The tracker refuses to construct. |
| `JIRA_SECURITY_PROJECT_KEY` | no, but read this | The restricted project security findings go to. | **Security-relevant findings are refused rather than filed.** They are not filed into the public project as a fallback. See below. |
| `JIRA_ISSUE_TYPE` | no | The issue type to create. Defaults to `Bug`. | A 400 from Jira if the project has no type called `Bug` — some projects call it `Defect`, or `Task`. |
| `QAAS_TRACKER_DRY_RUN` | no | Set to `1` to rehearse every tracker write instead of performing it. | Nothing. Off by default; this is the safety rail, not a feature. |

All four required variables are read and validated when the tracker is built,
before any agent starts work. That is on purpose: a tracker that discovers it
cannot authenticate after a run has produced twenty findings has destroyed the
run, because those findings live in an agent's context and the context is gone.

### Why a missing `JIRA_SECURITY_PROJECT_KEY` refuses rather than falls back

A finding is routed as restricted when the reporting agent flagged security
impact, or when the defect is classified as a vulnerability. If no restricted
project is configured, `create_issue` **refuses the finding and tells the agent
to escalate it to a human through a private channel** — it does not file it
anywhere.

That is the correct behaviour and it is enforced in code, not in a prompt. A
vulnerability filed into a project the whole company can read is a disclosure,
and there is no undo: you cannot un-tell people. Refusing loses a ticket.
Falling back loses control of a vulnerability.

If you do configure it, point it at a project whose issue-level security scheme
actually restricts visibility. `qaas tracker-check` cannot verify this for you —
Jira exposes no read-only view of a project's security scheme — so it says so
and asks you to confirm it in a browser. Do that once, properly.

---

## 3. Check the configuration before it files anything

```bash
qaas tracker-check
```

Read-only. It authenticates, reads each project, reads this account's
permissions on them, checks the issue type exists, and reports how the house
status vocabulary maps onto the project's actual workflow. It creates nothing
and exits non-zero if anything is wrong, naming each problem as something you
can act on.

It never prints the token. `JIRA_API_TOKEN` shows as `set (ends ...wxyz)`, or
`MISSING`.

```
tracker backend: jira   (tracker: in config/system.yaml)

                     environment
 variable                    status                what it does
 JIRA_BASE_URL               set (https://…)       site root, e.g. …
 JIRA_EMAIL                  set (qa-bot@acme…)    the bot account's Atlassian email
 JIRA_API_TOKEN              set (ends ...1234)    an API token, not a password
 JIRA_PROJECT_KEY            set (ENG)             default project for ordinary findings
 JIRA_SECURITY_PROJECT_KEY   set (SEC)             restricted project; without it …
 JIRA_ISSUE_TYPE             unset                 issue type to create (default: Bug)

                connection
 check                        detail
 auth                    ok   authenticated as QA Bot
 project ENG             ok   Acme Engineering (default)
 permissions ENG         ok   Browse, Create, Transition, Link
 issue type ENG          ok   'Bug' exists in ENG
 project SEC             ok   Acme Security (restricted)
 …

   workflow — ENG / Bug
 house status   maps onto
 open           To Do
 in_progress    In Progress
 in_review      no match
 resolved       Done
 closed         Done
 …

ready — nothing was created by this check.
```

### The workflow table is the part people skip

The system speaks a fixed vocabulary — `open`, `in_progress`, `in_review`,
`resolved`, `closed`, `wont_fix`, `duplicate` — and your Jira workflow speaks
whatever your team called things. There is no mapping file: a transition is
resolved by name against the project's real workflow at the moment it happens,
trying the house name and a list of common aliases.

`tracker-check` shows you that mapping in advance. Four of those statuses are
**driven by the system** and an unmapped one is a failure, not a note:

* `open` — a reopened ticket
* `in_progress` — TRIAGE picking work up
* `resolved`, `closed` — VERIFIER closing a ticket whose fix it verified

If `resolved` has nowhere to land, the run reports a verified fix and the ticket
stays open, and nobody notices until someone audits the board. `tracker-check`
exits non-zero for those. The other three (`in_review`, `wont_fix`,
`duplicate`) are human dispositions; an unmapped one is reported but does not
fail the check.

### Seeing the exact payload

```bash
qaas tracker-check --dry-run-ticket
```

Renders the JSON body that *would* be POSTed to `/rest/api/3/issue` for a sample
finding, and sends nothing. This is the same code path `create_issue` uses, not
a reconstruction of it, so what you read is what would be sent. Use it to look
at the ADF and the labels before you trust either.

---

## 4. First contact, in order

Do these four steps in this order. Each one can only fail in ways the previous
one could not have caught.

**1. `tracker: local`.** In `config/system.yaml`:

```yaml
tracker: local
```

Run something real — `qaas run --mode pr-check` — and then read
`.qaas/tickets/*.json`. These are the tickets the system would file, as JSON,
on your disk. Nothing left the machine. If the titles are vague or the repro
steps are thin, fix that here, where the only cost is your time.

**2. Switch to `tracker: jira` and run `qaas tracker-check`.** Fix everything it
names. Then `qaas tracker-check --dry-run-ticket` and read the payload.

**3. Rehearse a whole run against real credentials.**

```bash
export QAAS_TRACKER_DRY_RUN=1
qaas run --mode pr-check
```

With this set, `create_issue`, `transition` and `link` do everything except send:
the routing rules run, the per-run ticket cap is consumed, the payload is
assembled, and each rehearsed write is printed to the terminal and recorded in
the run ledger as a `dry_run` entry — never as a `ticket`, so nothing that
counts filed tickets counts these. The tool result tells the agent plainly that
nothing was filed and that the key it received is a placeholder, so it cannot
report a success it did not achieve.

`search` stays live, because a dedupe check against an imaginary backlog
rehearses the wrong run.

Read the ledger afterwards:

```bash
grep dry_run .qaas/runs/<run-id>/ledger.jsonl
```

**4. Go live into a scratch project.** Unset `QAAS_TRACKER_DRY_RUN`, point
`JIRA_PROJECT_KEY` at a project nobody's sprint depends on, and do one real run.
Read every ticket it filed. Only then point it at the team's board.

---

## 5. What the tickets look like

**Title** — the summary field, truncated to Jira's 255-character limit. Names
the defect, not the symptom.

**Body** — the `description` field, as an Atlassian Document Format tree. Jira
Cloud's REST v3 rejects a plain string here, so the house markdown is converted.
The converter supports exactly what the house ticket format emits: ATX headings,
paragraphs, `-`/`*`/`+` bullet lists, and fenced code blocks with an optional
language. Everything else — bold, italics, links, tables, ordered lists, block
quotes — is left in the ticket as literal characters rather than dropped,
because a reader can still understand `**blocker**` but cannot understand a body
with pieces silently missing. A ticket that needs richer rendering should link
to an artifact.

The name of the agent that found the defect is appended to the body ("Filed by
TRIAGE (automated QA)."), because Jira sets `reporter` from the credential and
that field will always say "QA Bot".

**Labels** carry all the house metadata. Labels, specifically, because they are
the only field guaranteed to exist in every Jira project — custom fields differ
per instance and per screen, and writing to one is the fastest route to a 400 on
someone else's Jira.

| Label | Example | Meaning |
|---|---|---|
| `agent-found` | `agent-found` | Filed by this system. Added to every ticket. |
| `security` | `security` | Added to anything routed as restricted. |
| `severity-*` | `severity-critical` | The house severity: blocker, critical, major, minor, trivial. |
| `qaas-envelope-*` | `qaas-envelope-env-1` | The defect envelope this ticket was filed from, so a ticket can be traced back to the finding and the run that produced it. |
| `qaas-fp-*` | `qaas-fp-ab12…` (64 hex chars) | The defect fingerprint. |

A value that cannot legally be a Jira label — one containing whitespace, or
longer than 255 characters — produces no label at all rather than a mangled one.
A mangled label is worse: it never matches on the way back out, so it silently
breaks the thing it was for.

### How `qaas-fp-*` drives dedupe across runs

The fingerprint is a hash of the defect's identity, not of its text, so the same
defect fingerprints the same on Tuesday as it did on Monday. Before filing,
TRIAGE searches for `labels = "qaas-fp-<digest>"`; a hit means this defect already
has a ticket, and the correct action is to add evidence to it, not to file a
second one.

Two details make that work:

* The `sha256:` prefix is dropped from the label (a colon in a Jira label is not
  worth betting a 400 on) and put back when the label is read out, so a
  fingerprint read from Jira compares equal to one computed from an envelope.
* The digest is never truncated. Dedupe matches on exact equality, and a
  shortened fingerprint would quietly collide — two unrelated defects sharing a
  ticket is a harder failure to notice than a duplicate.

Searches are scoped to the configured projects. An unscoped search would sweep
every project the bot can see, which is slow and drags unrelated tickets into an
agent's context where they read as prior art.

---

## 6. Troubleshooting

Keyed by what the system actually prints. The distinct wording is deliberate:
four different causes that all read "HTTP 403" is how an operator spends an
afternoon debugging the wrong one.

| Message contains | Cause | Fix |
|---|---|---|
| `JiraTracker is not configured: … unset or empty in the environment` | One or more of the four required variables is unset, or exported as an empty string. Every missing one is named at once. | Export them in the shell that runs `qaas`. An exported-but-empty variable counts as missing — that is the most common version of this. |
| `JIRA_BASE_URL is '…', which is not a URL` | The base URL has no scheme, or points below the site root. | Use `https://acme.atlassian.net`. No `/jira`, no `/rest/api/3`. |
| `Jira rejected the credentials (401)` | Wrong email for the token, a password instead of a token, or a revoked or expired token. | Confirm `JIRA_EMAIL` is the account the token was minted for, and mint a fresh token if in doubt. |
| `Jira refused the request (403) for …` | Authenticated, but the account lacks a permission on that project — or the project is archived. | `qaas tracker-check` names which of Browse / Create / Transition / Link is missing on which project. |
| `Jira has no such resource (404) for …` | The project key does not exist, or this account cannot browse it. Both surface as 404. | Check `JIRA_PROJECT_KEY` and `JIRA_SECURITY_PROJECT_KEY`. Keys are case-sensitive. |
| `Jira rate-limited … (429) and this call is not safe to retry automatically` | Jira throttled a write. Writes are never retried: a 429 can arrive *after* Jira created the issue, so retrying files it twice. | Slow the run down, or lower the per-run ticket cap in `config/system.yaml`. Reads are retried automatically and honour `Retry-After`. |
| `Jira returned 503 for … This is Jira's side, not the request's` | Jira is degraded. | Retry later; check <https://status.atlassian.com>. |
| `could not reach Jira at … ; check JIRA_BASE_URL, your network, and any proxy settings` | DNS, network, or a proxy. Nothing was sent. | Check the hostname resolves from this machine, and `HTTPS_PROXY` / `NO_PROXY`. |
| `Jira did not answer … within 30s` | Timeout. | Retry; check <https://status.atlassian.com>. |
| `Jira returned a non-JSON body for …` | `JIRA_BASE_URL` points at a proxy, an SSO login page, or something else that is not a Jira site. | Open the URL in a browser and confirm you land on Jira, not a login wall. |
| `The issue type 'Bug' does not exist in this project` (Jira's own words, passed through) | The project calls its bug type something else. | Set `JIRA_ISSUE_TYPE`. `qaas tracker-check` lists the types the project has. |
| `cannot move ENG-12 to 'closed': no such transition from its current status '…'. Jira offers: …` | The house status does not match any transition available from where the ticket is now. The message lists the transitions that do exist. | Check the workflow table in `qaas tracker-check`. Rename a workflow status, or use a project whose workflow speaks these words. |
| `this Jira has no link type usable for '…'; it offers: …` | The instance's link types were customised and even `Relates` is gone. | Add a link type, or accept unlinked duplicates. |
| `Jira accepted the issue but returned no key` | Jira answered a create with no key in the body. Rare; usually a proxy rewriting responses. | Check what sits between you and Jira. |
| `Refused: this is a security-relevant finding and this tracker has no restricted project configured` | Working as designed. | Set `JIRA_SECURITY_PROJECT_KEY`, or handle the finding through a private channel. It will not be filed publicly. |
| `Ticket cap reached (N for this run)` | The per-run cap in `config/system.yaml`. | This is an escalation, not a filing problem: hitting the cap means something upstream is wrong, and forty more tickets will not fix it. Read the run before raising the cap. |
| `an issue needs a title` / `unknown status '…'` / `unknown link type '…'` / `'…' is not an issue key` | An agent sent something invalid. Nothing was sent to Jira. | Nothing to fix; the agent is told the valid values and retries. |

---

## Jira Server / Data Center is not supported

`JiraDataCenterTracker` exists as a stub that raises. It is not an unfinished
feature you can enable — it is a placeholder marking a decision nobody has
taken.

Data Center is not Cloud with a different hostname:

* it authenticates with a Personal Access Token (`Authorization: Bearer <pat>`),
  not email + API token;
* it serves REST API **v2**, not v3;
* v2 takes a plain-text or wiki-markup description where v3 requires ADF — so
  the ADF conversion in `JiraTracker` is not merely unnecessary there, it is
  wrong.

Implementing it by subclassing `JiraTracker` would inherit the v3 paths and the
ADF converter and fail in ways that look like Jira being broken rather than the
adapter being wrong. It needs to be a separate adapter.

Atlassian's official hosted MCP server is Cloud-only. The self-hosted route is
the community `sooperset/mcp-atlassian` server.

Until someone builds it: use `tracker: local`.
