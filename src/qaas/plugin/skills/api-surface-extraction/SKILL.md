---
name: api-surface-extraction
description: >
  Extract every HTTP and WebSocket endpoint with its method, path, handler and
  real auth requirement. TRIGGER - read BEFORE recording routes in a system map
  or auditing an API surface, and whenever the task mentions endpoints, routes,
  the API surface, handlers, or 'what does this service expose'. Do NOT trust a
  spec file as the list of what exists. SKIP only when the route inventory is
  already available from the system map.
---

# Extracting the API surface

## Find routes three ways, then reconcile

1. **The router.** Decorators, route tables, `include_router` calls. Follow prefixes — a router mounted under `/v1` means every path inside it is longer than it looks.
2. **The declared spec.** OpenAPI, protobuf, GraphQL schema.
3. **The generated spec**, if the framework produces one at runtime (`/openapi.json`). This is what the framework thinks it implements, which is a third and distinct thing.

Reconciling these three is where the findings are. A route in the spec but not the router is a broken promise to consumers. A route in the router but not the spec is undocumented surface, often forgotten and unmaintained.

## Record auth as the code enforces it

This is the field most often recorded wrong, because it is the one people record from intent rather than observation.

- A dependency that extracts a user is **authentication**. It says who you are.
- A check on role, org, or ownership is **authorization**. It says what you may touch.
- An endpoint with the first and not the second is authenticated and unguarded — a very common and very serious defect.

Record what the handler actually checks, per endpoint, not what the router group implies. Middleware that "protects" a prefix must be verified to apply to each route under it, not assumed to.

## Order-sensitive registration

Where a literal path and a parameterised path overlap (`/orders/legacy` and `/orders/{id}`), **registration order decides which wins**. Record the order. A literal registered after a greedy parameterised route is unreachable — and reachability is not visible in a spec.

## Also record

Pagination parameters and whether they are actually applied; the error shape each endpoint returns; the status codes it can produce. Downstream agents test against these and cannot infer them.
