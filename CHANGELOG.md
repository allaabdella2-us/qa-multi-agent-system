# Changelog

## 1.1.0 — 2026-09-10

A security and correctness release. An adversarial audit of the whole codebase
found that the §8.1/§8.2 write-permission matrix was enforced on one path and
effectively unenforced on two others, plus a family of argv and path holes
around it. Every fix below has a regression test that was confirmed to fail
before it.

### The permission matrix is now true on every door

`Guardrail._check_path` is the single implementation of the write rules.
`Write`/`Edit` reach it through `check()`, shell commands through `_check_bash`,
and the `vcs` MCP server calls it instead of keeping its own near-copy.

- **`Bash` bypassed the whole matrix.** `_check_bash` consulted `FORBIDDEN_BASH`,
  the branch patterns and a substring test against `protected_paths`, and nothing
  else — so `sed -i '' api/app/auth.py` reached what `Edit` on that path was
  refused. PROOF, the verification gate, holds `Bash` with no `write_paths`: it
  was read-only only against `Write`, and could edit the source it verified.
- **`mcp__vcs__*` had its own copy of the rules, and the copy had drifted.**
  `check()` short-circuits every `mcp__*` call to "is this server declared"
  without reading its arguments, so the server was the only gate on its own
  tools — and it had never learned about `forbidden_paths`.
- Shell writes now count against the §8.2 diff budget, which they never did.
- A command that mutates and whose destination cannot be resolved is **refused**,
  naming `Write`/`Edit` in the reason. Reading a shell command is best-effort;
  guessing is not one of the options.

### Argv and path handling

- **`push` published onto `main`.** `git push origin <name>` parses its argument
  as a *refspec*, so `qa/repro/x:main` cleared every gate — `fnmatch` matched the
  branch pattern, and `is_protected_head` tested the whole string. Verified
  against a real bare remote. Branch names now refuse `:` and a leading `+`, and
  the refspec is stated explicitly on both sides.
- **`git diff --output=<path>` wrote any file.** `diff`'s `ref` reached argv with
  no `_reject_flaglike`, from the one tool documented read-only — held by
  ARBITER, whose policy grants no write access at all.
- **`create_branch`'s start point had no validator** while the name beside it had
  two.
- **Test-runner selectors executed code outside the target root.** A path-shaped
  `selector` or `test_id` was passed to pytest unchecked while `cwd` two lines
  above was contained; and selectors reach argv with no `--` separator, so one
  starting with `-` was parsed as an option (`-p`, `-c`, `-o addopts=`).
- **`_read_spec` read any file and fetched any URL.** Paths are contained in the
  target root; a URL is narrowed to the application under test. Fetching anything
  else is network research, which `guardrails` already refuses outright.
- **Refs that become URL paths are now shape-checked.** `list_changed_files`
  interpolates `base`/`head` into a `gh api` path, where `?`, `#`, `%` and `..`
  restructure the request. `gh` flags are passed joined (`--flag=value`), so a
  title or body can never be re-read as a flag.
- **Artifact names are contained, not substituted.** The old guard was a
  two-entry denylist that a backslash walked through.

### Enforcement that could be disarmed

- **The ledger was written in the locale encoding**, and every denial reason
  contains `§` and `—`. Where that resolves to ASCII, the first denial raised
  `UnicodeEncodeError` from inside `Guardrail.pre_tool_use` — the primary
  enforcement point — killing the turn instead of returning a reason. All text IO
  is now explicitly UTF-8.
- **The Jira token survived a cross-host redirect.** A hand-set `Authorization`
  header is copied across hosts by urllib; `_resolve_url` follows redirects on
  purpose and runs at the top of every Jira-backed run, so an SSO site's 302 sent
  the bot's credential to the identity provider.
- **`required_plugins = ["pytest-asyncio"]`.** Without it, an interpreter lacking
  the plugin skipped all 156 async tests — the hooks, the conductor loop, the
  guardrail callbacks, every MCP server — and exited 0.

### Gates, budgets and contracts

- **The diff budget reset on every loop.** It counted a set on a context rebuilt
  per dispatch, so MENDER's "5 files per run" started again on each
  MENDER/ARBITER round trip and each ticket.
- **`must_call` counted denied and errored calls.** The tally was taken in
  PreToolUse; an errored `record_verdict` satisfied PROOF's contract and the Stop
  hook let it stop with no verdict.
- **`has_evidence()` accepted an empty string.** `failing_test is not None` let
  `""` pass the §2 evidence gate a model is not supposed to be able to argue past.
- **Location-less envelopes all hashed the same.** Two unrelated findings sharing
  a domain and a class collided, and `defect_memory` suppressed the second as a
  duplicate — persisting that suppression into every future run.
- **The golden ledger's `domain` and `severity` are validated at load.** A bad
  domain scored 0.0 against everything forever; a bad severity crashed
  `summary()` after a paid run.
- **`.env` beat an exported-but-empty variable.** Presence, not truthiness — the
  case that matters is a CI job whose secret is unset and a checkout carrying a
  stale `.env`.
- **`build_options` failures escaped `run_agent`**, aborting the whole run while
  sibling agents kept spending unrecorded.

### Other fixes

- Generated contract tests no longer hard-code `admin@northwind.test` /
  `password123` / `/v1/auth/login` — the last demo credentials in `src/`. Against
  any other target every generated test failed at its login fixture, and the
  envelope cited a "failing contract test" that was really a login failure.
- `search` forwarded house status names to Jira, which validates its own and
  answers 400 — so CLERK's dedupe errored on every call and the duplicate ticket
  the system exists to prevent got filed.
- `find_filter` failed open on an unknown account id and could adopt — and, since
  0.4.0, rewrite — a stranger's saved filter. `update_filter_jql` un-shared the
  filter it repaired.
- `reset` reported a wipe that a timed-out `compose rm` had not performed.
- `_host_port` mis-parsed `127.0.0.1:8000:8000` and port ranges.
- `run_n_times` had no aggregate deadline: 20 runs at the 900s cap is five hours
  in one tool call.
- Reading a run no longer creates one — `qaas show <typo>` left a permanent
  phantom in `qaas runs`.
- A clone that timed out left a partial tree that the next run greeted with
  "using existing clone".
- A widening to an untyped schema was classified narrowed and reported BREAKING.
- `qaas score` takes `--target`, so a `--repo` run is not scored against the demo
  app's ledger.

### Also

- `rm -R`, `rm --recursive` and `rm --force` are refused alongside `rm -rf`.
- `_branch_from_command` only inspects git commands; it read `python -c` as
  `switch -c` and refused it as a bad branch name.
- CLAUDE.md's stale paths corrected: agents live in
  `src/qaas/defaults/config/agents/`, skills in `src/qaas/plugin/skills/` and
  load as a Claude Code plugin.

767 tests, still offline and free by default.

## 1.0.0

First stable release. Fifteen agents, the Phase 3 fix loop, one Jira view per
repository, and a live run view.
