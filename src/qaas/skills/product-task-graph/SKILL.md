---
name: product-task-graph
description: >
  Build or read the graph of user-facing tasks: what a person comes to this
  product to do and which routes and actions each task needs. TRIGGER - read
  BEFORE recording a task graph or planning UI exploration, and whenever the
  task mentions user journeys, flows, task graph, critical paths, or 'what can a
  user do here'. Do NOT derive journeys from the route list alone - routes are
  not tasks. SKIP only when following an existing task graph step by step.
---

# The product task graph

A route inventory says what pages exist. A task graph says **what someone is trying to accomplish**, and those are different objects. `/checkout/review` is a route; "place an order" is a task that happens to pass through it.

## Building one

For each task record: the goal in a user's words, the entry point, the ordered steps, the success condition, and what the user needs to already have (an account, a populated cart, a specific role).

Derive tasks from what the product is *for* — nouns in the domain model and verbs in the UI copy — then check each against the routes. Two useful signals fall out:

- A route no task passes through is **orphaned**. Either a task is missing from the graph or the route is dead. Both are worth reporting.
- A task with no complete route path is **broken or unimplemented**, and that is a finding on its own.

## Prerequisites are the valuable part

Most journey testing fails at setup, not at the step under test. Record precisely what state each task needs before its first step: which role, what seeded data, which flags. A task graph without prerequisites sends an explorer into a login wall and produces a false finding about a page that was working.

## Include the unhappy paths

The graph is not just the golden path. For each task, record what should happen when it fails: validation rejects the input, the network drops, the session expires, the resource is gone. Those branches are where defects concentrate, precisely because nobody writes tests for them.

## Rank by consequence

Mark which tasks are critical — the ones where failure means lost revenue, lost data, or a user who cannot recover. Time-boxed runs walk those first, and "we ran out of budget before checkout" is a much worse outcome than "we ran out of budget before the settings page".
