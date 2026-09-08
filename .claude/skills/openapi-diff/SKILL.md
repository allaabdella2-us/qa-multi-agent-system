---
name: openapi-diff
description: >
  Compare a declared API spec against what the implementation actually does, and
  classify each difference by consumer impact. TRIGGER - read BEFORE reporting
  any spec drift or breaking change, and whenever the task mentions OpenAPI, the
  spec, contract drift, breaking changes, schema comparison, or 'does the API
  match the docs'. Do NOT report a textual difference as a defect without
  classifying its impact. SKIP only when no spec comparison is involved.
---

# Diffing spec against implementation

## Breaking is defined from the consumer's side

The only question that matters: **would a client written against the old contract break against the new behaviour?** Not whether the change is large, or intentional, or an improvement.

| Change | Verdict | Why |
|---|---|---|
| Response field removed | **Breaking** | A client reads it and gets `undefined` |
| Response field made optional / nullable | **Breaking** | Same failure, arriving intermittently, which is worse |
| Required request field added | **Breaking** | Every existing call now 422s |
| Status code changed | **Breaking** | Clients branch on status |
| Enum value removed | **Breaking** | A client that sends it now fails |
| Type narrowed (`string` to `enum`, `int` to `int>=1`) | **Breaking** | Previously valid input rejected |
| Endpoint removed | **Breaking** | Obviously |
| Optional response field added | Non-breaking | Ignored by old clients |
| Enum value added to a **response** | Breaking-ish — flag it | Clients with exhaustive switches fail |
| Optional request field added | Non-breaking | |
| Description or example changed | Not a change | Do not report it |

## Direction matters

"In the spec, absent from the implementation" and "in the implementation, absent from the spec" are different defects with different fixes:

- **Spec has it, code does not** — a broken promise. Anyone reading the docs writes code that fails. Usually the more severe of the two.
- **Code has it, spec does not** — undocumented surface. Nobody depends on it deliberately, but nothing protects it either, and it is often unmaintained and unguarded.

Say which direction you found. A report that only says "currency mismatch" leaves the reader to work out who is wrong.

## Prove it

A structural diff is a hypothesis. Call the endpoint, capture the real response, and attach it. A diff between two files is evidence about files; a captured response is evidence about the system.

Then generate the contract test. A finding that ships with a test failing today and passing when fixed is a finding nobody has to argue about.
