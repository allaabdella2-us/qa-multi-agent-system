---
name: contract-test-generation
description: >
  Write a runnable test that asserts an endpoint's declared contract, to attach
  as evidence. TRIGGER - read BEFORE producing any contract test or attaching
  test evidence to an API finding, and whenever the task mentions contract
  tests, evidence for an API defect, or proving a spec violation. Do NOT attach
  a test you have not executed against both the violating and the conforming
  behaviour. SKIP only when the finding is not a contract violation.
---

# Generating a contract test

A contract test is the difference between a finding an engineer argues with and one they fix. It converts "the response is missing a field" into a red test with a name.

## What it must do

- **Fail now, for the stated reason.** Run it. A test that passes against the current implementation is not evidence of a defect — it is evidence you misread the code.
- **Pass once fixed.** Assert the contract, not the current behaviour inverted. `assert "currency" in body` is a contract; `assert "currency" not in body` pins the bug in place permanently.
- **Be readable by whoever picks up the ticket.** The test name states the contract: `test_invoice_response_includes_currency`, not `test_api_06`.

## Assert exactly one thing

One contract violation, one test. A test asserting status, shape, and auth together fails on whichever it hits first, and the reader learns one third of what you knew.

## Include the setup

Which fixture, which role, which token. A test that only passes on a machine where someone happened to log in first is not evidence, and it will be deleted the first time it fails in CI.

## Assert the shape, not the payload

Assert that `currency` is present and is a three-character string. Do not assert `currency == "USD"` unless the contract fixes that value — otherwise the test breaks on a legitimate data change and gets deleted, taking the real assertion with it.

## Name the source

A comment giving the spec path and the clause being asserted (`openapi.yaml: Invoice.required includes currency`) means the next reader can check whether the contract itself was wrong, which is sometimes the right answer.
