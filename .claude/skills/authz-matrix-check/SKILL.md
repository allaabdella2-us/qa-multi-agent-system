---
name: authz-matrix-check
description: >
  Check every endpoint against the role matrix to find missing authorization.
  TRIGGER - read BEFORE auditing any endpoint for permissions, and whenever the
  task mentions authorization, roles, permissions, access control, IDOR,
  tenancy, multi-tenant scoping, or 'who can call this'. Do NOT treat the
  presence of an auth dependency as evidence that access is checked - that
  confusion is the defect this skill exists to catch. SKIP only when no endpoint
  has an access-control requirement.
---

# Auditing authorization

## The distinction that produces the defect

**Authentication** proves who you are. **Authorization** decides what you may touch. An endpoint with the first and not the second is the most common serious API defect there is, and it looks correct at a glance because there is clearly an auth dependency on the handler.

Two questions per endpoint, always separately:

1. Does it establish an identity?
2. Does it check that *this* identity may act on *this* resource?

## Build the matrix

Rows are endpoints, columns are roles, plus a column for "resource belonging to another tenant". Fill each cell with what the code actually does, and compare against what the spec or product intent says it should. Every disagreement is a candidate finding.

Do not fill a cell from the router group or a middleware prefix. Verify per handler — a route added later under a "protected" prefix very often misses the check that its neighbours have.

## Three failure shapes

- **Missing role check.** Handler requires a user, never inspects the role. A viewer can refund.
- **Missing tenancy scope.** Lookup filters by resource id alone, not by the caller's organization. Any authenticated user reads any record by guessing an id. This is the highest-severity shape and the easiest to miss, because the endpoint behaves perfectly for the tester's own data.
- **Check that cannot fail.** The check exists but reads a client-supplied value, or compares the resource's own org against itself.

## Read the neighbours

The strongest signal is inconsistency. When five handlers in a file filter on `org_id` and the sixth does not, that sixth is almost certainly a dropped clause rather than a deliberate exception. Symmetry-breaking is the cheapest defect detector available here.

## Prove it, carefully

Demonstrate with two accounts: create a resource as one, read it as the other, capture the response. That is proof.

Do not escalate beyond demonstrating access. Do not extract data volume, do not chain into further systems, do not test how much you can reach. One captured cross-tenant read is complete evidence; anything past it is an incident of your own making.

Everything found here is `security_relevant: true` and routes restricted. See `routing-rules`.
