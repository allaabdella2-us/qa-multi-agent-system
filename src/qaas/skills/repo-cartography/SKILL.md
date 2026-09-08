---
name: repo-cartography
description: >
  Map a repository into services, modules and dependency edges. TRIGGER - read
  BEFORE exploring an unfamiliar repository or publishing any system map, and
  whenever the task mentions mapping, the system map, services, module
  structure, dependency graph, or 'what is in this codebase'. Do NOT start
  opening files at random; the order of exploration is the whole technique. SKIP
  only when a current map already exists and you are reading it rather than
  building it.
---

# Mapping a repository

## Order matters

Breadth before depth, always. The failure mode is opening `main.py`, following an import, and forty minutes later knowing one code path deeply and the repository not at all.

1. **Shape first.** Directory listing at depth 2-3. Manifests (`pyproject.toml`, `package.json`, `go.mod`) mark service boundaries better than any convention.
2. **Entry points.** One per service. Where does the process start, what does it bind, what does it mount?
3. **Declared contracts.** OpenAPI specs, schema files, migrations, router configs. These are dense and authoritative — one file often yields twenty routes.
4. **Only then, handlers.** And only enough to answer specific questions the contracts left open.

## Record the code, not the documentation

Where a comment, a README, or a spec disagrees with the implementation, **the implementation is what you record** — and the disagreement itself goes in `drift`, because it is a finding another agent will act on. Quietly preferring one side destroys information.

## Missing is `null`

A guessed field is worse than an absent one, because everything downstream trusts the map without re-deriving it. An empty list means "none exist"; `null` means "not determined". Keep them distinct — they lead to different downstream behaviour.

## Dependency edges

Record only edges that matter for reasoning about coupling: cross-service calls, cross-layer imports, shared database tables. A complete import graph of every module is noise that makes cycles harder to see, not easier.

## Cost

You are the cheapest agent per unit of value in the system, because every other agent stops re-deriving this. Spend turns on coverage. An incomplete map is not a partial success — it silently caps the quality of every discovery agent that reads it.
