# Making the prompts yours

This file covers where agent prompts live, how `build_system_prompt` composes them, the three-layer search path, the `<AGENT>.append.md` mechanism, and the `qaas prompts` commands that let you change any of it without editing site-packages.

It also covers the split the architecture insists on — role in the prompt, procedure in a skill, the per-run task in `tasks.py` — because changing the wrong one of those three is the most common way to make a system like this worse.

---

## Where prompts live

```
src/qaas/prompts/
├── _shared.md          2610 chars — appended to every agent
├── ARBITER.md
├── CARTOGRAPHER.md
├── CLERK.md
├── CONDUIT.md
├── FORGE.md
├── MENDER.md
├── PROOF.md
└── SURFACE.md
```

Nine files, none longer than about 3 KB. An agent's YAML names its own by filename:

```yaml
# src/qaas/defaults/config/agents/conduit.yaml
prompt: CONDUIT.md
```

`AgentSpec.prompt` is typed as a path relative to the prompts directory (`src/qaas/config.py:71`), and nothing else in the system knows which file an agent uses.

`CONDUIT.md` in full is a fair sample of the house shape — role, domain, method, standard:

```markdown
You are CONDUIT, the backend, API and contract analyst.

## Your domain

The HTTP surface and the promises it makes. You compare what the API specification
declares against what the implementation actually does, and you report the gaps.

Detect:

- **Spec drift** — a response field, status code, or parameter the implementation
  has and the spec does not, or the reverse.
...

## How you work

1. Read the system map for the route inventory. Do not rediscover it.
2. Use `diff_openapi` to compare the declared spec against the implementation,
   and `classify_breaking` to judge severity of what it returns.
...

## Severity

Judge by consequence, not by how interesting the bug is. Data exposed to the
wrong user is critical or blocker. A missing pagination limit that degrades a
page is major. A status code that is 400 where it should be 422 is minor.
```

Note what is *not* in there: no repository name, no directory layout, no seeded users, no port numbers. That is a hard rule, and the next section explains where those facts do go.

`_shared.md` opens by saying what it is:

```markdown
<!-- Appended to every agent prompt. House rules that hold for all agents. -->

## House rules

**Evidence or it did not happen.** Every finding you report carries a screenshot,
trace, query plan, log, failing test, or captured output. A finding without
evidence is not a finding — drop it.

**You report; you do not fix.** You never edit product code, never file tickets,
and never resolve your own findings, unless your role below explicitly grants it.
```

Eight rules that hold for all eight agents, written once. One of them records a real incident:

```markdown
**Do not claim a library behaves a certain way from memory.** Your knowledge of
a third-party package is a snapshot and it goes stale; the version in front of
you may have added exactly the method you are about to report as missing. This
has already produced a confident, wrongly-severe finding in this system.
```

That paragraph exists because an agent filed a wrong ticket. If you fork `_shared.md` and drop it, you will get that ticket again.

---

## How `build_system_prompt` composes them

`src/qaas/registry.py:103-135`:

```python
def build_system_prompt(
    spec: AgentSpec, prompt_dirs: Sequence[Path] | None = None
) -> str:
    """The agent's prompt, its local addenda, and the house rules every agent shares.

    Kept as separate files so a change to the shared rules reaches every agent at
    once, rather than being copy-pasted into six prompts that then drift.

    Each file is resolved first-hit-wins **independently**: overriding
    `CONDUIT.md` keeps the house `_shared.md`, and replacing `_shared.md` keeps
    all eight agent prompts. Resolving the pair from one winning directory would
    make either override drag the other along.

    Order is agent, then addenda, then shared: the house rules are the last word,
    and an addendum that could displace them would be an enforcement hole opened
    from a text file.
    """
    dirs = tuple(prompt_dirs) if prompt_dirs is not None else resolve_prompt_dirs()
    own = _first_hit(dirs, spec.prompt)
    if own is None:
        where = ", ".join(str(d) for d in dirs) or "(no prompt directories)"
        raise FileNotFoundError(f"{spec.name} has no prompt '{spec.prompt}' in: {where}")
    shared = _first_hit(dirs, SHARED_PROMPT)
    if shared is None:
        where = ", ".join(str(d) for d in dirs) or "(no prompt directories)"
        raise FileNotFoundError(f"no {SHARED_PROMPT} in: {where}")

    blocks = [own.read_text().rstrip()]
    # An empty addendum contributes nothing rather than a stray blank block --
    # `touch CONDUIT.append.md` must not change a single byte of the prompt.
    blocks += [t for p in append_paths(dirs, spec.prompt) if (t := p.read_text().strip())]
    blocks.append(shared.read_text().strip())
    return "\n\n".join(blocks) + "\n"
```

So the assembled prompt is:

```
<AGENT>.md
                      ← blank line
<AGENT>.append.md     ← broadest layer first, if any
                      ← blank line
_shared.md
```

Three properties are decided in that docstring, and each is worth understanding before you override anything.

**The two files resolve independently.** `_first_hit(dirs, spec.prompt)` and `_first_hit(dirs, SHARED_PROMPT)` are separate walks. Override `CONDUIT.md` and you still get the packaged `_shared.md`; replace `_shared.md` and you keep all eight packaged agent prompts. If the pair were resolved from a single winning directory, overriding one would silently drag the other along.

**The house rules go last.** The comment names the reason: *"an addendum that could displace them would be an enforcement hole opened from a text file."* Your addendum cannot end up after `_shared.md`, so it cannot be the last word on evidence, on filing, or on "you report; you do not fix".

**An empty addendum contributes nothing.** `if (t := p.read_text().strip())` filters out a blank file, so `touch CONDUIT.append.md` does not change a single byte of the prompt.

Verify the arithmetic against what `qaas prompts list` reports for CONDUIT:

```
$ python - <<'EOF'
from pathlib import Path
own = Path('src/qaas/prompts/CONDUIT.md').read_text()
sh  = Path('src/qaas/prompts/_shared.md').read_text()
print('own chars', len(own), 'rstrip', len(own.rstrip()))
print('shared chars', len(sh), 'strip', len(sh.strip()))
print('total', len(own.rstrip()) + 2 + len(sh.strip()) + 1)
EOF
own chars 2239 rstrip 2238
shared chars 2610 strip 2609
total 4850
```

```
│ CONDUIT      │ CONDUIT.md      │ packaged │ -        │ 4850        │
```

---

## The search path

`resolve_prompt_dirs` (`src/qaas/registry.py:58-69`) delegates to the workspace:

```python
def resolve_prompt_dirs(ctx: ToolContext | None = None) -> tuple[Path, ...]:
    """The prompt search path: overrides first, packaged last.

    Same shape as `skill_plugins` -- a ToolContext may carry a workspace, and
    anything without one asks the resolver. Prompts used to be read from
    `PROMPTS_DIR` unconditionally, which meant a `pip install` user could not
    change a single line of any prompt without editing site-packages.
    """
    from qaas.paths import Workspace

    ws = getattr(ctx, "workspace", None) or Workspace.resolve()
    return tuple(ws.prompt_dirs)
```

and `Workspace.resolve` builds it at `src/qaas/paths.py:225-229`:

```python
        prompt_dirs = _existing(
            home_path / "prompts" if home_path else None,
            proj_state / "prompts" if proj_state else None,
            packaged_prompts(),
        )
```

| precedence | directory | who sets it |
|---|---|---|
| 1 (highest) | `$QAAS_HOME/prompts` | an organisation, via env var |
| 2 | `<project>/.qaas/prompts` | this repository |
| 3 (lowest) | `src/qaas/prompts` | the wheel |

`_existing` keeps only directories that exist, so a plain `pip install` in an empty directory resolves to one entry and works.

This is the same layering rule as `config/agents/*.yaml`, `config/targets/*.yaml`, and skills — and `src/qaas/paths.py:26-34` says so, along with the one place the rule is deliberately different:

```
Layering granularity differs by kind, and that difference is deliberate:

  * `system.yaml`   first hit wins **whole**. Merging run-mode dictionaries
                    across layers produces a configuration nobody wrote and
                    nobody can read back.
  * `agents/*.yaml` union by filename, higher layer shadows. Someone who wants
                    MENDER's budget raised drops in one file; they do not fork
                    eight and freeze themselves on today's roster.
  * prompts/skills  union by name, higher layer shadows, same reasoning.
```

One more detail that matters at run time. `build_options` passes the resolved dirs explicitly (`src/qaas/registry.py:405-409`):

```python
    return ClaudeAgentOptions(
        # Through the workspace, not `PROMPTS_DIR`: a user's `.qaas/prompts/`
        # override has to reach the agent that actually runs, not just the one
        # `qaas prompts list` describes.
        system_prompt=build_system_prompt(spec, resolve_prompt_dirs(ctx)),
```

`describe()` — the dry-run view — takes `prompt_dirs` for the same reason (`src/qaas/registry.py:444-452`): *"A dry run that silently reports the packaged prompt while the run sends an overridden one is worse than no dry run."*

---

## `<AGENT>.append.md` — adding lines without forking the file

The suffix is defined at `src/qaas/registry.py:27-31`, and the comment is the whole argument for its existence:

```python
#: `CONDUIT.md` -> `CONDUIT.append.md`. The suffix exists because the only other
#: way to add three house lines to a shipped prompt is to fork the whole file,
#: and a forked prompt stops receiving the next release's improvements to it --
#: silently, and in the one part of the system where silence is most expensive.
APPEND_SUFFIX = ".append.md"
```

This is the same reasoning as agent YAMLs layering by filename. If the only override mechanism is "copy the file and edit it", then everyone who wants a two-line change takes a permanent fork of a 2 KB file, freezes on today's version of it, and never sees the next release's improvements. Nobody notices, because a stale prompt does not error — it just gets slightly worse results.

Appends behave differently from every other resource in the system: they **accumulate** rather than shadow. `src/qaas/registry.py:85-100`:

```python
def append_paths(dirs: Sequence[Path], prompt: str) -> list[Path]:
    """Every `<AGENT>.append.md` on the search path, broadest layer first.

    Not first-hit-wins: appends accumulate rather than shadow, so an
    organisation-wide `QAAS_HOME` addendum and a project's own both apply. They
    are ordered lowest-precedence first so the nearest layer speaks last, which
    is both how a reader expects the specific to follow the general and how a
    model weights the end of a block.
    """
    name = append_name(prompt)
    found: list[Path] = []
    for d in reversed(list(dirs)):
        candidate = Path(d) / name
        if candidate.is_file():
            found.append(candidate)
    return found
```

`reversed(list(dirs))` puts the packaged layer first and the highest-precedence layer last. So an org-wide `$QAAS_HOME/prompts/CONDUIT.append.md` and a project's `.qaas/prompts/CONDUIT.append.md` both apply, with the project's speaking last. Two reasons given: it is how a reader expects the specific to follow the general, and it is how a model weights the end of a block.

`append_name` (`src/qaas/registry.py:80-82`) handles subdirectories:

```python
def append_name(prompt: str) -> str:
    """`CONDUIT.md` -> `CONDUIT.append.md`, keeping any subdirectory."""
    return str(PurePosixPath(prompt).with_suffix("")) + APPEND_SUFFIX
```

### Choosing between an append and an eject

| you want to | use |
|---|---|
| add rules, examples, or house conventions | `<AGENT>.append.md` |
| change or remove something the packaged prompt says | `qaas prompts eject <AGENT>` |
| change a rule for *every* agent | eject `_shared` |

Prefer the append. It is a few lines in git, it reads as a diff forever, and you keep receiving upstream improvements to the file it extends.

---

## The three commands

```
$ qaas prompts --help
 Usage: qaas prompts [OPTIONS] COMMAND [ARGS]...

 Inspect and override the agent prompts.

╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ list   Show which prompt file each agent is actually given, and from where.  │
│ eject  Copy a packaged prompt into the project so it can be edited.          │
│ diff   Show local prompt edits against the bytes that shipped.               │
╰──────────────────────────────────────────────────────────────────────────────╯
```

The comment introducing the group (`src/qaas/cli.py:603-610`) says what problem it solves:

```python
# A prompt is where an agent's judgement is set, and it is the first thing a
# real user wants to change. Before this group the only way to do that after a
# `pip install` was to edit site-packages: invisible to git, lost on the next
# upgrade, and impossible to diff. These three commands make the same edit a
# file in the project, and `diff` makes an upgrade's divergence visible instead
# of silent.
```

### `qaas prompts list`

On this checkout, with nothing overridden:

```
$ qaas prompts list
                           Prompts in force
┏━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━┓
┃ agent        ┃ file            ┃ source   ┃ appended ┃ total chars ┃
┡━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━┩
│ ARBITER      │ ARBITER.md      │ packaged │ -        │ 5125        │
│ CARTOGRAPHER │ CARTOGRAPHER.md │ packaged │ -        │ 4817        │
│ CLERK        │ CLERK.md        │ packaged │ -        │ 4711        │
│ CONDUIT      │ CONDUIT.md      │ packaged │ -        │ 4850        │
│ FORGE        │ FORGE.md        │ packaged │ -        │ 4870        │
│ MENDER       │ MENDER.md       │ packaged │ -        │ 5526        │
│ PROOF        │ PROOF.md        │ packaged │ -        │ 4627        │
│ SURFACE      │ SURFACE.md      │ packaged │ -        │ 4817        │
│ every agent  │ _shared.md      │ packaged │ -        │ 2610        │
└──────────────┴─────────────────┴──────────┴──────────┴─────────────┘
*
/Users/.../qa-multi-agent-system/src/qaas/prompts

`qaas prompts eject <AGENT>` to edit one outright, or drop a `<AGENT>.append.md`
beside it to add lines without forking the file.
```

The `*` marks the highest-precedence directory. `total chars` is the *composed* size — `len(build_system_prompt(spec, ws.prompt_dirs))` — not the file's size, so it already includes `_shared.md` and any appends. The `_shared.md` row shows its own raw length instead, because it is not an agent.

`source` comes from `_prompt_origin` (`src/qaas/cli.py:623-630`) and reads `packaged`, `project`, or `override` depending on which layer won.

### `qaas prompts eject`

```
$ qaas prompts eject FORGE
wrote /path/to/project/.qaas/prompts/FORGE.md

edit them, then `qaas prompts diff` to see what you changed.
```

Flags: `--all` to eject every prompt, `--force` to overwrite one already there. The argument accepts `_shared` for the house rules, and it is case-insensitive and tolerant of a `.md` suffix (`_select_prompts`, `src/qaas/cli.py:660-672`).

Two guards in the implementation are worth knowing about.

**It copies the packaged bytes, not whatever currently wins** (`src/qaas/cli.py:740-743`):

```python
        # The *packaged* bytes, deliberately: eject means "give me the house
        # version to edit". Copying whatever already won the search would make
        # a second eject a no-op that looks like it did something.
        src = packaged_prompts() / filename
```

**It refuses to write inside the installed package** (`_eject_dir`, `src/qaas/cli.py:633-648`):

```python
def _eject_dir(ws: Workspace) -> Path:
    """Where `eject` writes. Never inside the installed package.

    Enforcement, not advice. `state_root` comes from QAAS_HOME, the project, or
    the cwd, and nothing else stops one of those from landing in site-packages
    -- where an edit would survive exactly until the next `pip install
    --upgrade` and then vanish with no trace of ever having been made.
    """
```

After ejecting and editing, `list` shows the layer change:

```
│ FORGE        │ FORGE.md        │ project  │ -        │ 4940        │
```

```
*
/path/to/project/.qaas/prompts
  /Users/.../src/qaas/prompts
```

Two directories on the path now, project first.

### `qaas prompts diff`

```
$ qaas prompts diff FORGE

FORGE
--- packaged/FORGE.md
+++ /path/to/project/.qaas/prompts/FORGE.md
@@ -1,4 +1,6 @@
 You are FORGE, the reproduction engineer.
+
+Run every repro three times, not five: this project's suite is slow.

 You are this system's noise filter, and every downstream agent trusts your
 verdict. A finding you pass along becomes a ticket on a real engineer's board.
```

The docstring says when to run it (`src/qaas/cli.py:777-781`):

```python
    """Show local prompt edits against the bytes that shipped.

    Run it after an upgrade: a forked prompt does not conflict, it just quietly
    stops tracking the package, and this is the only place that shows it.
    """
```

An append is reported differently — as an addition, since there is nothing to diff against:

```
$ qaas prompts diff FORGE

FORGE + FORGE.append.md
--- /path/to/project/.qaas/prompts/FORGE.append.md
+## Local addendum
+
+When the failing test needs a fixture, name it in the reproduction environment block.
```

and `list` records it in its own column:

```
│ FORGE        │ FORGE.md        │ packaged │ FORGE.append.md │ 4976        │
```

Packaged file, local addendum, larger composed prompt. That is the state you want: the two-line change is visible in `diff` forever and the underlying prompt still tracks the package.

With nothing overridden:

```
$ qaas prompts diff
no local prompt edits — every prompt is the packaged one
```

One last detail: `diff` deliberately does not report a `_shared.append.md` (`src/qaas/cli.py:793-794`):

```python
        # Only agent prompts take an addendum; `_shared.append.md` is composed
        # by nothing, so reporting one would describe a file that has no effect.
```

`build_system_prompt` only calls `append_paths(dirs, spec.prompt)` — never for `SHARED_PROMPT` — so a `_shared.append.md` would be inert. To change the house rules for everyone, eject `_shared`.

---

## The three-way split: prompt, skill, task

This is the part to get right. The architecture puts three different kinds of instruction in three different places, and putting one in the wrong place is what makes a system like this brittle.

| what | where | changes when |
|---|---|---|
| **role and standards** — who you are, what counts as good work | `src/qaas/prompts/<AGENT>.md` | rarely; it is the agent's identity |
| **procedure** — how to actually do a thing, step by step | `src/qaas/plugin/skills/<name>/SKILL.md` | when the method improves |
| **the task** — which app, which environment, which finding | `src/qaas/tasks.py` | every single run |

`CLAUDE.md` states it in one line:

> Role and standards live in the system prompt; *procedure* lives in `.claude/skills/*/SKILL.md`; the per-run *task* is built in `tasks.py`.

Concretely, for CONDUIT:

- **Prompt** (`CONDUIT.md`): *"You are CONDUIT, the backend, API and contract analyst… Judge by consequence, not by how interesting the bug is."*
- **Skill** (`openapi-diff/SKILL.md`, `severity-rubric/SKILL.md`): the rubric table, the four questions in order, the traps.
- **Task** (`tasks.conduit(config, mode)`): the resolved path of the application, whether the API is reachable, which roles exist, whether this run files tickets.

The system prompt is identical on every run. The task is different every time.

### `tasks.py` builds the user turn

`src/qaas/tasks.py:1-10`:

```python
"""The task each agent is given: the user turn, distinct from its system prompt.

The system prompt says who an agent is and what its standards are; that is
stable across every run and lives in `prompts/`. The task says what to do this
time — which application, which environment, which findings — and is built here
from the target profile.

Nothing in this module may name a specific application. A prompt that mentions
one repository's directory layout or one app's seeded users works exactly once.
"""
```

Eight builders (`src/qaas/tasks.py:95-361`), one per agent, called from the conductor:

```python
        outcome = await self._dispatch(spec, store, budget, report, tasks.cartographer(self.config), None)
```
```python
            (spec, tasks.forge(draft, self.config, self.config.thresholds.flake_runs))
```

Every application-specific fact enters through the target profile. `tasks.conduit` (`src/qaas/tasks.py:133-178`) branches on the profile rather than naming anything:

```python
    spec_line = (
        f"Compare the implementation against `{p.layout.spec}` with `diff_openapi`, "
        "and judge each difference by consumer impact with `classify_breaking`."
        if p.layout.spec
        else "There is no specification to diff against, so the contract is implicit. "
        "Judge each endpoint against what its own code promises and what its "
        "consumers assume: look for handlers that disagree with each other."
    )
    prove = (
        "Prove what you report. Call the endpoint and capture the real request and "
        "response. A finding you have not observed is a hypothesis, not a defect, "
        "and its confidence should say so."
        if p.environment.is_reachable
        else "You cannot call this API, so every finding is a reading of the code. "
        "Quote the lines that support it, and keep confidence honest about the "
        "fact that you did not observe the behaviour."
    )
```

Every one of those branches is a *capability* question, not an application question: does this target declare a spec? is its API reachable? is this an `incident` run that files nothing? The same code produces a coherent task for a repo with an OpenAPI document and a live instance, and for a repo with neither.

`_environment_brief` (`src/qaas/tasks.py:40-72`) is the sharpest example, keyed on `environment.mode`:

```python
    if env.mode == "external":
        return (
            f"The application is already running ({where}) and this system does not "
            "own it. You may read it and exercise it. You may NOT reset it, reseed "
            "it, or destroy state — someone else may be relying on it. Prefer "
            "read-only calls, and never send a request whose side effect you would "
            "not want to explain."
        )
```

And `_where` (`src/qaas/tasks.py:28-37`) carries a bug worth reading, because it is the shape of mistake this whole separation exists to prevent:

```python
def _where(config: SystemConfig) -> str:
    """How to name the application under test to an agent.

    The resolved path, not `profile.root`. An agent's process cwd *is* the
    target root, and `root:` is spelled relative to the qaas project -- so
    telling a run "the application at `target-app`" sent it looking for
    `<target>/target-app`, a directory that does not exist. Resolved, the
    sentence is true from wherever the agent happens to be standing.
    """
    return str(config.target_root())
```

### The rule, and why it holds

> **Nothing in `tasks.py` or a prompt may name a specific application.**

The stated reason is short: *"A prompt that mentions one repository's directory layout or one app's seeded users works exactly once."*

Test your own edits against it. If a line you are adding to `CONDUIT.md` would be false against some other repository, it does not belong in the prompt. It belongs in the target profile (`config/targets/*.yaml`), where `tasks.py` will read it and phrase it for the run.

There is one legitimate exception, and it is in the opposite direction: the *append* mechanism is where an organisation's own conventions go, because an addendum is by definition local and never ships to anyone else.

---

## A worked change

Say your team has a house rule: never file a `blocker` without linking the on-call runbook.

**Wrong:** eject `_shared.md` and add a paragraph. You now own a fork of the file that carries the "do not claim a library behaves a certain way from memory" rule, and the next release's improvement to it never reaches you.

**Right:** add the rule where the agent that files tickets will read it.

```bash
mkdir -p .qaas/prompts
cat > .qaas/prompts/CLERK.append.md <<'EOF'
## House additions

A `blocker` ticket must link the on-call runbook in its description. If you
cannot find the runbook, file at `critical` and say why in the summary.
EOF

qaas prompts list      # CLERK: appended = CLERK.append.md, total chars up
qaas prompts diff      # the addendum, shown as an addition
```

Then measure it. `CLAUDE.md` is direct about this:

> Run `qaas score` after changing any prompt, threshold or model. It is the only way to know whether a change helped.

`qaas score` reports recall and precision against `target-app/defects.yaml`, the golden ledger of 16 seeded defects and 4 planted non-defects. A prompt change with no number attached is a preference, not an improvement.

---

## Quick reference

| task | command / file |
|---|---|
| see what each agent is actually sent | `qaas prompts list` |
| add lines to one agent | `<project>/.qaas/prompts/<AGENT>.append.md` |
| add lines for the whole org | `$QAAS_HOME/prompts/<AGENT>.append.md` |
| change what a prompt says | `qaas prompts eject <AGENT>`, then edit |
| change the house rules | `qaas prompts eject _shared` |
| see local divergence (run after an upgrade) | `qaas prompts diff` |
| check every prompt still resolves | `qaas validate` |
| see the composed size without spending money | `qaas run --mode <mode> --dry-run` |
| know whether the change helped | `qaas score` |
