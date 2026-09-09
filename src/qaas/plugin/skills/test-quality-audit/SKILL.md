---
name: test-quality-audit
description: >
  Audit the tests that come with a fix for whether they can actually fail.
  TRIGGER - read BEFORE judging any regression test attached to a fix, and
  whenever the task mentions test quality, asserted-to-pass tests, mocks,
  snapshots, coverage, or 'are these tests real'. Do NOT accept a green run as
  evidence that a test discriminates; a test that passes against the unfixed
  code tests nothing and is the most common way a bad fix looks good. SKIP only
  when a fix carries no tests - which is itself the finding.
---

# Auditing the tests on a fix

A regression test has one job: fail if the defect comes back. A test that cannot fail is worse than no test, because it occupies the slot where a real one would go and it makes the next reviewer relax.

The audit answers one question — **would this test have failed against the code before the fix?** — and everything below is a way of getting at it.

## The central technique: run it against the old code

- **MENDER can do this directly.** Write the regression test before the fix and watch it fail, or `git stash push -- <changed files>`, `run_single` on the new test, confirm the failure, `git stash pop`. Put the observed old-code failure message in the PR body.
- **ARBITER cannot.** You have `Read`, `Grep`, `Glob` and read-only vcs and test_runner; no shell, no write access, no way to revert the diff. So:
  1. Running `run_single` on the new test in the current tree proves it passes with the fix. That is the premise, not the check — do not record it as evidence of quality.
  2. Do the revert **on paper**: for each hunk of the fix, ask which assertion in the new test goes red if that hunk is deleted. If no assertion depends on any hunk, the test does not test the fix.
  3. Require the PR body to state that the check was run, and quote the old-code failure. If it does not, `REQUEST_CHANGES` asking for exactly that. It is the cheapest and most specific review you can write, and it puts the work where the tools are.

## Shapes that cannot fail

| In the test | Why it proves nothing | What it should assert |
|---|---|---|
| `result = f(x)` and then nothing, or `assert result`, or `assert result is not None` | Only that the call returned | The value: the field, the count, the status the contract promises |
| `assert isinstance(r, list)` / `assert "items" in body` | Shape, not behaviour. True before the fix as well | What is *in* the list, and why that is right |
| `assert r.status_code == 200` on a defect about the response body | The endpoint was probably 200 while broken | The body field the defect corrupted |
| `assert total == 4`, where 4 is whatever the code printed | Pins observed output as the spec. If it was pinned *before* the fix, it pins the bug | The contract-derived value, with the assertion message naming the contract |
| `mock_repo.get.assert_called_once()` | That the code called something. The bug may be in what it did with the answer | The behaviour after the call. Mock the boundary, never the unit under test |
| A mock standing in for the function that contained the bug | The buggy code no longer runs in the test | Exercise the real code; mock only what crosses a process boundary |
| A snapshot or expected fixture updated in the same PR | The bug may have been written down as the expectation | Check the snapshot diff moved *toward* the contract, not toward the observed output |
| `try: ... except Exception: pass` around the assertion, or `pytest.raises(Exception)` | Swallows the failure, or matches any failure including an import error | Let it raise; name the specific exception type and match its message |
| `assert` after a `return`, or inside an `if` that is never true | Never executes | Unconditional assertions |
| Function not named `test_*`, a `skip`/`xfail` marker, an empty `parametrize` list, a method on an uncollected class | Never runs. Confirm it appeared in the run's per-test rows | It must show as `passed` in `run_suite` output, by nodeid |
| Passes only after another test, or reuses the defining test's fixture state | A coincidence of ordering, not a reproduction | Self-contained setup and teardown |

## Beyond "can it fail"

- **Is it a different test from the defining one?** A regression test that is the defining test renamed adds nothing. The defining test proves *this* defect is gone; the regression test states the *contract* so the defect cannot return in another shape.
- **Does it cover the boundary?** If the reproduction found "fails with one item, works with two", the test is parameterised over 0, 1 and 2. A single-case test at the reported value is a special case, and it pairs suspiciously often with a special-cased fix (`root-cause-vs-symptom`).
- **Does it assert the contract, not the bug?** `assert len(items) <= limit` survives a future fix; `assert len(items) == 30` goes red when someone gets it right and is then deleted as flaky.
- **Was the defining test touched?** Diff its file against the base. Any hunk there is a `REQUEST_CHANGES`; a weakened assertion, a new skip marker, or a changed parametrisation is an `ESCALATE_TO_HUMAN`.
- **Was it actually run?** Check the per-test rows from `run_suite` or `run_single`, by nodeid. A summary count does not tell you your test was among them, and a test that matched nothing returns an error rather than a pass.
- **Is it deterministic?** If the area is timing- or ordering-sensitive, `run_n_times` with n=5. A test that passes intermittently has not passed, and a flaky regression test is deleted within a month.

## Coverage is not quality

`get_coverage` measures which lines executed, and it returns an error rather than an estimate when `coverage` is not installed. Executed is not asserted: a covered line under a vacuous assertion is more dangerous than an uncovered one, because it reports as safe. Use coverage only to find lines the new tests never reach — that is a real gap — never as evidence that the tests are good.

## When the tests are the only problem

A correct fix with a vacuous test is `REQUEST_CHANGES`, not `APPROVE` with a concern. The test is what stops the defect returning after everyone involved has forgotten the ticket, and it is the cheapest thing in the change to get right. Name the file, name the assertion, and say what it should assert instead — MENDER receives your words verbatim and there are only two round trips.
