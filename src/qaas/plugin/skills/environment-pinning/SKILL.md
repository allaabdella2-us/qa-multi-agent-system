---
name: environment-pinning
description: >
  Pin every variable a reproduction depends on so it runs identically later.
  TRIGGER - read BEFORE reproducing a defect or verifying a fix, and whenever
  the task mentions environment, fixtures, seeding, reset, flags, determinism,
  or 'it works on my machine'. Do NOT run a reproduction against whatever state
  happens to be present. SKIP only when no environment is involved.
---

# Pinning the environment

An unpinned reproduction is a story about something that once happened. Pinning is what converts it into a fact anyone can re-check.

## Pin all five

1. **Branch or commit.** What code.
2. **Fixture.** Which seeded dataset, by name.
3. **Flags.** Every feature flag that is set, including the ones you did not change — a default that shifts later silently invalidates the reproduction.
4. **Role.** Which user, which permissions. "Logged in" is not pinned.
5. **Clock**, if any behaviour is time-dependent.

All five go in the envelope's `environment` block. VERIFIER will bring up the same environment to verify the fix, and a mismatch there means the verification proves nothing.

## Reset between attempts

Reset before each reproduction attempt. Residue from the previous attempt is the most common cause of a defect that "sometimes reproduces" — the second run starts from state the first run created, which is a different test.

## Seed, do not hand-build

Use the named fixture. Data you created by clicking through the UI cannot be recreated exactly by anyone else, including you tomorrow.

## Say what you could not pin

If the defect depends on something outside your control — a container's startup timing, an external service, wall-clock time of day — say so explicitly in the reproduction. That is not a failure of the work; it is a material fact about the defect, and hiding it wastes the fixer's afternoon.
