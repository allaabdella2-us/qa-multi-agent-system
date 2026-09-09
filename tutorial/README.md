<div align="center">

# 📚 qaas tutorial

**How this codebase actually works — one aspect per file, with real line numbers.**

</div>

---

Every file here points at real code. If a page says `registry.py:331`, that line
exists and says what the page claims it says. Where a design looks strange, the
page explains what broke to make it that way — that is the house habit, and it is
usually the most useful part.

## Start here

| # | file | what it covers |
|---|---|---|
| 🗺️ **01** | [Code structure](01-code-structure.md) | Every module, the real import graph, what each one owns, and the two decisions that explain the shape |
| 📨 **02** | [How agents communicate](02-how-agents-communicate.md) | The `DefectEnvelope` — the only thing agents pass each other. The gates that live on the model instead of in a prompt |

## Configuring and extending

| # | file | what it covers |
|---|---|---|
| 🪝 **03** | [Skills and hooks](03-skills-and-hooks.md) | Where the 30 skills are wired in, and what each of the three hooks enforces |
| 🔌 **04** | [MCP servers](04-mcp-servers.md) | The seven in-process servers, and declaring your own in YAML |
| ✏️ **05** | [Prompt configuration](05-prompt-configuration.md) | Ejecting, overriding, and appending without forking |

## Integrations and shipping

| # | file | what it covers |
|---|---|---|
| 🎫 **06** | [Jira integration](06-jira-integration.md) | How a finding becomes a ticket, traced through the adapter |
| ⚙️ **07** | [GitHub Actions](07-github-actions.md) | The CI workflow, and why it never costs money |
| 📦 **08** | [Packaging and publishing](08-packaging-and-publishing.md) | From source to `pip install qaas-python` |
| 🛡️ **09** | [Guardrails and safety](09-guardrails-and-safety.md) | What stops an agent doing damage — and two holes that were found and closed |

---

## The 30-second version

```
qaas <command>  →  cli.py  →  conductor.py  →  runner.py  →  claude_agent_sdk.query()
                                   ↑               ↑
                             a state machine   one agent, one call
```

Five phases, in order:

```
map ──▶ discover ──▶ reproduce ──▶ file ──▶ verify
 │          │            │           │         │
CARTO-   CONDUIT       FORGE       CLERK     PROOF ⇄ MENDER ⇄ ARBITER
GRAPHER  SURFACE    (once per                    (bounded loop)
                     finding)
```

Three things that are true of this codebase and unusual enough to state up front:

1. **The orchestrator is Python, not a prompt.** A model cannot enforce a budget
   it is itself spending — so phase order, concurrency, retries, escalation and
   the loop bounds are ordinary code. That is why 649 tests run offline and free.

2. **Agents never pass prose.** The only inter-agent type is a validated
   `DefectEnvelope`, and two of its rules — "has evidence" and "is fileable" — are
   *methods on the model*, not requests in a prompt. An agent cannot talk its way
   past a method.

3. **The ledger is control flow, not just logging.** The conductor reads verdicts
   and review decisions back out of `ledger.jsonl` rather than parsing them out of
   an agent's reply.

---

## Related documents

- [`../README.md`](../README.md) — what the project is, and how to use it
- [`../ARCHITECTURE.md`](../ARCHITECTURE.md) — the system in one document
- [`../CLAUDE.md`](../CLAUDE.md) — terse notes for people already oriented
- [`../qa-agent-system-architecture.md`](../qa-agent-system-architecture.md) — the original design, including the 8 agents not yet built

---

<div align="center">
<sub>Every line reference in these pages was checked against the source. If one drifts, the code is right and the page is wrong.</sub>
</div>
