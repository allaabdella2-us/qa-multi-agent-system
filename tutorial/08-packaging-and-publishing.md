# 08 — From source to `pip install qaas-python`

This chapter covers how this repository becomes a distributable package: what
`pyproject.toml` declares, what ends up in the wheel and what does not, the
resource-resolution bug that made an installed CLI completely dead, and the
build-then-verify sequence that catches it happening again.

---

## The name

```toml
[project]
# Distribution name is `qaas-python` (`qaas` was taken); the import package and
# the CLI are both `qaas`.
name = "qaas-python"
```

Three names, and only one of them is `qaas-python`:

| | Name | Where it appears |
|---|---|---|
| Distribution | `qaas-python` | `pip install qaas-python`, PyPI, `dist/qaas_python-0.1.0-*` |
| Import package | `qaas` | `from qaas.envelope import DefectEnvelope` |
| Console script | `qaas` | `qaas run --mode nightly` |

PyPI's `qaas` was taken, so the distribution took a suffix. Nothing else changed:
you install one name and type another, which is ordinary enough on PyPI
(`pip install pillow`, `import PIL`) but worth stating once so nobody goes
looking for a package called `qaas-python` on their filesystem.

The rename has one recorded consequence, covered in chapter 07: it made `uv.lock`
stale, which is why CI invokes `.venv/bin/pytest` rather than `uv run pytest`.

---

## The build backend

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

Hatchling, with a `src/` layout. The console script is one line:

```toml
[project.scripts]
qaas = "qaas.cli:app"
```

`qaas.cli:app` is the Typer application object. That entry point is what
`pip install` turns into an executable, and you can read it back out of a built
wheel:

```
$ unzip -p dist/qaas_python-0.1.0-py3-none-any.whl qaas_python-0.1.0.dist-info/entry_points.txt
[console_scripts]
qaas = qaas.cli:app
```

Runtime dependencies are five (`pyproject.toml:22-28`) — `claude-agent-sdk`,
`pydantic`, `pyyaml`, `typer`, `rich` — and the test tooling is an extra:

```toml
[project.optional-dependencies]
dev = ["pytest>=8.3", "pytest-asyncio>=0.24"]
```

Which is why the setup command in `CLAUDE.md` is `uv pip install -e ".[dev]"`,
and why an ordinary user installing from PyPI does not get pytest.

---

## The wheel/sdist split

This is the interesting part of the file, and both halves carry their reasoning
in a comment.

```toml
[tool.hatch.build.targets.wheel]
# Non-.py files under this tree ship too, which is how prompts/ already reaches
# users and how skills/ and defaults/ will.
packages = ["src/qaas"]
exclude = ["**/__pycache__", "**/*.pyc"]

[tool.hatch.build.targets.sdist]
# The sdist carries the calibration corpus (target-app/ + its golden ledger) so a
# contributor can run `qaas score`. The wheel does not: it is 69M, and it is a
# deliberately vulnerable application that has no business in site-packages.
include = [
    "src/", "config/", "target-app/", "tests/", ".claude/skills/",
    "README.md", "LICENSE", "ARCHITECTURE.md", "CLAUDE.md", "BUILD_PLAN.md",
    "pyproject.toml",
]
```

The wheel rule is a single line: *everything under `src/qaas`, whatever its
extension.* Not just `.py`. That is how prompts, agent YAML defaults and 30
skill markdown files reach an installed user — they live inside the package
directory, so they are package data by construction rather than by an
`include` list somebody has to remember to update.

### Why the demo app is excluded from the wheel

`target-app/` is a deliberately buggy FastAPI + React application, and
`target-app/defects.yaml` is the golden ledger that says which bugs are seeded
where. `qaas score` compares a run's findings against it — that is how recall and
precision get measured instead of asserted (`src/qaas/scorecard.py:29`: "One
seeded defect, as recorded in target-app/defects.yaml").

A contributor needs it. A user running `pip install qaas-python` to point the
system at their own repository does not, and would be actively harmed by getting
it, for two reasons:

**Size.** It is 69M in the working tree:

```
$ du -sh target-app
 69M	target-app

$ du -sh target-app/* | sort -h | tail -3
 16K	target-app/defects.yaml
112K	target-app/api
 69M	target-app/web
```

(Almost all of that is `target-app/web/node_modules`, which is gitignored and so
does not travel in the sdist either — the sdist ships 42 tracked files from
`target-app/`. The 69M is what a contributor's checkout costs, not what the
tarball weighs. The comment's number describes the tree.)

**Content.** It is intentionally vulnerable — cross-tenant reads, a missing role
check on refunds, a traceback leak, mass assignment on order creation. Putting
that in `site-packages` on every user's machine has no upside and several
plausible downsides.

So: sdist yes, wheel no. And because that distinction lives only in a config file
that nothing type-checks, CI asserts it explicitly (chapter 07):

```yaml
      - name: The wheel must not carry the demo app
        run: |
          if unzip -l dist/*.whl | grep -q "target-app/"; then
            echo "::error::the wheel contains target-app/"; exit 1
          fi
```

> **One stale entry.** The sdist `include` list still names `.claude/skills/`,
> but that directory no longer exists — commit `e0b5c84` deleted it when skills
> moved into the package as a plugin (see below). Hatchling ignores a missing
> path, so the build is unaffected and no `.claude/skills/` entry appears in the
> tarball. It is dead config, not a bug, but it will mislead the next reader.

---

## What actually ships in the wheel

Built from a clean checkout:

```
$ uv build
Building source distribution...
Building wheel from source distribution...
Successfully built dist/qaas_python-0.1.0.tar.gz
Successfully built dist/qaas_python-0.1.0-py3-none-any.whl

$ ls -lh dist/
-rw-r--r--  1 ...  248K ...  qaas_python-0.1.0-py3-none-any.whl
-rw-r--r--  1 ...  403K ...  qaas_python-0.1.0.tar.gz
```

81 files, 673KB uncompressed. The five groups:

```
$ unzip -l dist/qaas_python-0.1.0-py3-none-any.whl
Archive:  dist/qaas_python-0.1.0-py3-none-any.whl
  Length      Date    Time    Name
---------  ---------- -----   ----
    65477  02-02-2020 00:00   qaas/cli.py
    22773  02-02-2020 00:00   qaas/conductor.py
    17528  02-02-2020 00:00   qaas/config.py
     8524  02-02-2020 00:00   qaas/discover.py
     9095  02-02-2020 00:00   qaas/envelope.py
    18100  02-02-2020 00:00   qaas/guardrails.py
    12703  02-02-2020 00:00   qaas/paths.py
    19664  02-02-2020 00:00   qaas/registry.py
     7417  02-02-2020 00:00   qaas/runner.py
    15809  02-02-2020 00:00   qaas/scorecard.py
     1678  02-02-2020 00:00   qaas/sdk_compat.py
    11038  02-02-2020 00:00   qaas/store.py
    10378  02-02-2020 00:00   qaas/target.py
    15125  02-02-2020 00:00   qaas/tasks.py
    11461  02-02-2020 00:00   qaas/trace.py
      882  02-02-2020 00:00   qaas/adapters/__init__.py
    54348  02-02-2020 00:00   qaas/adapters/tracker.py
    19430  02-02-2020 00:00   qaas/adapters/vcs.py
     2807  02-02-2020 00:00   qaas/defaults/config/system.yaml
      739  02-02-2020 00:00   qaas/defaults/config/agents/arbiter.yaml
      772  02-02-2020 00:00   qaas/defaults/config/agents/cartographer.yaml
      768  02-02-2020 00:00   qaas/defaults/config/agents/clerk.yaml
      728  02-02-2020 00:00   qaas/defaults/config/agents/conduit.yaml
      879  02-02-2020 00:00   qaas/defaults/config/agents/forge.yaml
     2182  02-02-2020 00:00   qaas/defaults/config/agents/mender.yaml
      853  02-02-2020 00:00   qaas/defaults/config/agents/proof.yaml
      532  02-02-2020 00:00   qaas/defaults/config/agents/surface.yaml
        0  02-02-2020 00:00   qaas/mcp/__init__.py
     2551  02-02-2020 00:00   qaas/mcp/context.py
    43942  02-02-2020 00:00   qaas/mcp/contract_diff.py
    19829  02-02-2020 00:00   qaas/mcp/defect_memory.py
    38442  02-02-2020 00:00   qaas/mcp/env_control.py
    20371  02-02-2020 00:00   qaas/mcp/envelope_server.py
    30562  02-02-2020 00:00   qaas/mcp/test_runner.py
    18199  02-02-2020 00:00   qaas/mcp/tracker.py
    20757  02-02-2020 00:00   qaas/mcp/vcs.py
      263  02-02-2020 00:00   qaas/plugin/.claude-plugin/plugin.json
     2634  02-02-2020 00:00   qaas/plugin/skills/a11y-audit/SKILL.md
     8589  02-02-2020 00:00   qaas/plugin/skills/adversarial-review/SKILL.md
     ... (30 skills in total)
     2455  02-02-2020 00:00   qaas/plugin/skills/verification-protocol/SKILL.md
     2524  02-02-2020 00:00   qaas/prompts/ARBITER.md
     2226  02-02-2020 00:00   qaas/prompts/CARTOGRAPHER.md
     2106  02-02-2020 00:00   qaas/prompts/CLERK.md
     2253  02-02-2020 00:00   qaas/prompts/CONDUIT.md
     2269  02-02-2020 00:00   qaas/prompts/FORGE.md
     2921  02-02-2020 00:00   qaas/prompts/MENDER.md
     2026  02-02-2020 00:00   qaas/prompts/PROOF.md
     2220  02-02-2020 00:00   qaas/prompts/SURFACE.md
     2620  02-02-2020 00:00   qaas/prompts/_shared.md
    16293  02-02-2020 00:00   qaas_python-0.1.0.dist-info/METADATA
       87  02-02-2020 00:00   qaas_python-0.1.0.dist-info/WHEEL
       38  02-02-2020 00:00   qaas_python-0.1.0.dist-info/entry_points.txt
     1069  02-02-2020 00:00   qaas_python-0.1.0.dist-info/licenses/LICENSE
     7223  02-02-2020 00:00   qaas_python-0.1.0.dist-info/RECORD
---------                     -------
   673092                     81 files
```

| Group | Contents | Why it must ship |
|---|---|---|
| `qaas/*.py`, `qaas/adapters/`, `qaas/mcp/` | The code | Obvious |
| `qaas/defaults/config/` | `system.yaml` + 8 agent YAMLs | Otherwise every command that loads config dies |
| `qaas/prompts/` | 8 agent prompts + `_shared.md` | An agent with no system prompt is not an agent |
| `qaas/plugin/` | `.claude-plugin/plugin.json` + 30 `SKILL.md` files | Procedure lives in skills; without them agents run degraded |
| `dist-info/` | Metadata, entry point, licence | Packaging |

Note the tag:

```
$ unzip -p dist/qaas_python-0.1.0-py3-none-any.whl qaas_python-0.1.0.dist-info/WHEEL
Wheel-Version: 1.0
Generator: hatchling 1.32.0
Root-Is-Purelib: true
Tag: py3-none-any
```

Pure Python, no compiled extensions, one wheel for every platform. That is also
why the `build` job in CI needs no matrix.

And what is **not** there: no `target-app/`, no `tests/`, no `config/targets/`.

```
$ unzip -l dist/*.whl | grep -q "target-app/" && echo FOUND || echo "no target-app in wheel"
no target-app in wheel
```

### The sdist, by contrast

```
$ tar -tzf dist/qaas_python-0.1.0.tar.gz | awk -F/ '{print $2}' | sort -u
.gitignore
ARCHITECTURE.md
BUILD_PLAN.md
CLAUDE.md
LICENSE
PKG-INFO
README.md
config
pyproject.toml
src
target-app
tests

$ tar -tzf dist/qaas_python-0.1.0.tar.gz | wc -l
     155

$ tar -tzf dist/qaas_python-0.1.0.tar.gz | grep -c "target-app/"
      42
```

The sdist is the contributor's copy: source, tests, the demo application, the
golden ledger, the design docs. `config/` here holds only `config/targets/corvid.yaml`
— the bundled demo profile, which is separate from the packaged defaults in
`src/qaas/defaults/config/` precisely because it points at `target-app/`:

```yaml
# The bundled demo application. This profile is also the worked example: copy it
# and edit for your own repository, or generate one with `qaas init <path>`.
name: corvid
root: target-app
```

---

## The bug that made an installed CLI dead

Everything above about "the wheel contains prompts, config and skills" is recent.
It was not true at first, and the failure it produced is worth studying because
of *how* it failed rather than that it did.

`src/qaas/paths.py` opens with the post-mortem (`src/qaas/paths.py:1-17`):

```python
"""Where qaas finds its own resources: config, prompts, skills, state.

This module exists because the package used to assume it was running from its
own git checkout. `config/` and `.claude/skills/` sat at the repo root, outside
the wheel, and were looked up relative to the process CWD or by climbing
`Path(__file__).parents[2]` -- which, once installed, lands in
`site-packages/../..`. A `pip install` therefore produced a CLI where every
command that needed config died, and `qaas validate` failed *always*, because
none of the 30 skills it checks for were anywhere on disk.

Three different ideas had been collapsed into `Path.cwd()`:

  1. where qaas's own resources live      -> packaged, or overridden by the user
  2. where the user's project state lives -> `.qaas/`
  3. where the application under test is  -> the target profile's root

This module owns the first two. The third belongs to `TargetProfile.root_path`.
"""
```

### The offending line

It was one line, in `cli.py`, and you can still read it in git history:

```
$ git grep -n "SKILLS_DIR" 291d106^ -- src/qaas/cli.py
291d106^:src/qaas/cli.py:14:SKILLS_DIR = Path(__file__).resolve().parents[2] / ".claude" / "skills"
```

Trace `parents[2]` from `src/qaas/cli.py` in each of the two worlds:

| Where `__file__` is | `parents[0]` | `parents[1]` | `parents[2]` | Result |
|---|---|---|---|---|
| `<repo>/src/qaas/cli.py` | `<repo>/src/qaas` | `<repo>/src` | `<repo>` | `<repo>/.claude/skills` ✓ |
| `site-packages/qaas/cli.py` | `site-packages/qaas` | `site-packages` | `<venv>/lib/python3.12` | `.../python3.12/.claude/skills` ✗ |

In a checkout it climbs out of `src/` and lands on the repo root. Installed,
there is no `src/` layer, so the same climb overshoots by one directory and lands
somewhere with no `.claude/` in it at all.

### Why this was worse than a crash

From the fix commit (`291d106`, "Make the wheel self-sufficient: `pip install`
now produces a working CLI"):

> It did not before. Only Python and prompts/ reached the wheel; config/ and
> .claude/skills/ sat at the repo root, outside it. Every command that loaded
> config died with FileNotFoundError, and `qaas validate` failed *always*, because
> SKILLS_DIR climbed Path(__file__).parents[2] -- the repo root from a checkout,
> site-packages/../.. from an install. **Worse than the crash: agents ran with no
> skills at all and said nothing, because a missing skill is an empty listing
> rather than an error.**

That last sentence is the real lesson. `FileNotFoundError` is loud, gets reported
and gets fixed. A `glob()` over a nonexistent directory returns `[]`, and an
agent that receives zero skills runs perfectly happily — just without the
severity rubric, the dedupe strategy or the review order. It produces findings.
They are worse. Nothing says so.

The companion commit `e0b5c84` measured the loss:

> Past runs show 135 Skill invocations across the eight agents, so what
> disappeared was the severity rubric, the dedupe strategy, the review order:
> every procedure the system has.

### The second bug, in the fix

The obvious repair — use `importlib.resources` — was tried first and was also
wrong. `src/qaas/paths.py:59-73` records it:

```python
def package_root() -> Path:
    """The installed package directory -- the one `__file__` seam in the codebase.

    `Path(__file__).parent` rather than `importlib.resources.files("qaas")`,
    which was tried first and is wrong here: under a src-layout editable install
    the package resolves to a `MultiplexedPath`, and `Path(str(...))` on one of
    those yields the literal string `MultiplexedPath('/...')` -- a path that
    exists nowhere. Every resource lookup then silently found nothing, which is
    the same failure shape as the bug this module was written to fix.

    This file lives inside the package, so its parent *is* the package, in an
    editable install and a wheel alike. Zip-safety is not a consideration: the
    skills directory is handed to a subprocess as a real filesystem path.
    """
    return Path(__file__).resolve().parent
```

Same failure *shape* — a path that exists nowhere, resolving to silence rather
than an error — reached by a completely different route.

The fix is `parent`, not `parents[2]`. `paths.py` lives inside the package, so
its parent *is* the package, in both worlds. The module comment calls it "the one
`__file__` seam in the codebase": every other resource lookup goes through
`package_root()` rather than doing its own climbing.

### What replaced it

One precedence order, applied to every kind of resource
(`src/qaas/paths.py:19-24`):

```
    1. explicit  --config / QAAS_CONFIG_DIR
    2. project   <project>/.qaas/config, and the source-checkout <project>/config
    3. packaged  src/qaas/defaults/config, src/qaas/prompts, src/qaas/skills
```

with granularity that differs by kind, deliberately
(`src/qaas/paths.py:26-34`):

```
  * `system.yaml`   first hit wins **whole**. Merging run-mode dictionaries
                    across layers produces a configuration nobody wrote and
                    nobody can read back.
  * `agents/*.yaml` union by filename, higher layer shadows. Someone who wants
                    MENDER's budget raised drops in one file; they do not fork
                    eight and freeze themselves on today's roster.
  * prompts/skills  union by name, higher layer shadows, same reasoning.
```

`Workspace.resolve()` (`src/qaas/paths.py:195`) builds those search paths, and
its docstring names the state that has to be legal:

```python
        """Build the search paths. Never raises: a missing project is legitimate.

        `pip install qaas-python` then `qaas --help` in an empty directory is a
        supported state, and it resolves to packaged defaults only.
        """
```

### Skills ship as a plugin, and the layout is not a matter of taste

The other half of making the wheel self-sufficient was skills. They now live at
`src/qaas/plugin/`, and `packaged_plugin()` (`src/qaas/paths.py:84-98`) explains
the shape:

```python
def packaged_plugin() -> Path:
    """The skills plugin that ships in the wheel.

    Skills reach an agent as a Claude Code *plugin* (`--plugin-dir`), not through
    filesystem settings, so they travel in the package instead of depending on a
    `.claude/skills/` directory in whatever repository the user happens to be in.

    The layout is not optional and was established by testing the CLI rather
    than by reading about it. A directory of bare `<skill>/SKILL.md` folders
    loads NOTHING -- silently. A directory containing `skills/<name>/SKILL.md`
    loads, but takes its namespace from the directory name. Only
    `.claude-plugin/plugin.json` + `skills/<name>/SKILL.md` gives a stable
    namespace, and it comes from the manifest's `name`.
    """
    return package_root() / "plugin"
```

Which is why the wheel contains this file:

```
$ unzip -p dist/qaas_python-0.1.0-py3-none-any.whl qaas/plugin/.claude-plugin/plugin.json
{
  "name": "qaas",
  "description": "Procedures for the qaas QA agents: how to triage, reproduce, review and verify.",
  "version": "0.1.0",
  "author": {
    "name": "Alla Abdella"
  },
  "homepage": "https://github.com/allaabdella2-us/qa-multi-agent-system"
}
```

`"name": "qaas"` is what makes skills resolve as `qaas:severity-rubric`, which
`Workspace.qualify()` (`src/qaas/paths.py:297`) produces and which the agent
allowlists must match exactly.

---

## Build and verify

`uv build` and `twine check` tell you the artifacts are well-formed. They cannot
tell you the CLI works once installed — that was exactly the state the repository
was in for a while. So the verification is: install the wheel into a throwaway
venv, in a directory that is not this repository, and run the commands that need
resources.

### 1. Build

```bash
uv build
```

```
Building source distribution...
Building wheel from source distribution...
Successfully built dist/qaas_python-0.1.0.tar.gz
Successfully built dist/qaas_python-0.1.0-py3-none-any.whl
```

Note that hatchling builds the wheel *from the sdist*, so a file missing from the
sdist cannot appear in the wheel.

### 2. Check the metadata

```bash
uvx twine check dist/*
```

```
Checking dist/qaas_python-0.1.0-py3-none-any.whl: PASSED
Checking dist/qaas_python-0.1.0.tar.gz: PASSED
```

### 3. Assert the demo app stayed out

```bash
unzip -l dist/*.whl | grep -q "target-app/" && echo BAD || echo ok
```

This is the same check CI runs.

### 4. Install into a clean venv, somewhere else entirely

The "somewhere else" is the point. Run this from inside the repository and
`paths.py` will find the project and its `config/`, which is precisely the
condition that hid the original bug.

```bash
cd $(mktemp -d)
python3 -m venv .venv
.venv/bin/pip install /path/to/qa-multi-agent-system/dist/qaas_python-0.1.0-py3-none-any.whl
```

### 5. Run the three commands that need packaged resources

```bash
mkdir proj && cd proj
../.venv/bin/qaas init ../some-repo
../.venv/bin/qaas validate
../.venv/bin/qaas run --mode pr-check --dry-run
```

Real output from doing exactly that against a trivial two-file repository:

```
$ ../.venv/bin/qaas init ../sample
┏━━━━━━━━━━━━━┳━━━━━━━┓
┃ detected    ┃ value ┃
┡━━━━━━━━━━━━━╇━━━━━━━┩
│ backend     │ -     │
│ frontend    │ -     │
│ tests       │ -     │
│ api spec    │ -     │
│ ownership   │ -     │
│ environment │ none  │
└─────────────┴───────┘
note: No OpenAPI document found. ...
next
  1. Read .../proj/.qaas/config/targets/sample.yaml and correct anything wrong.
  2. If the app runs somewhere, set environment.mode and the URLs, and fill in auth.
  3. `qaas doctor` to check readiness, then `qaas run --mode pr-check --dry-run`.
```

```
$ ../.venv/bin/qaas validate
┏━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━┓
┃ agent     ┃ layer     ┃ model     ┃ servers ┃ skills ┃ must call ┃ writes    ┃
┡━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━┩
│ ARBITER   │ remediat… │ claude-o… │ 4/6     │ 4      │ record_r… │ read-only │
│ CARTOGRA… │ control   │ claude-s… │ 1/6     │ 4      │ put_syst… │ read-only │
│ CLERK     │ triage    │ claude-s… │ 3/6     │ 5      │ search_s… │ tickets<… │
│ CONDUIT   │ discovery │ claude-o… │ 4/6     │ 5      │ -         │ read-only │
│ FORGE     │ triage    │ claude-o… │ 4/6     │ 4      │ record_r… │ paths:qa… │
│ MENDER    │ remediat… │ claude-o… │ 6/6     │ 4      │ open_pr   │ paths:ap… │
│ PROOF     │ remediat… │ claude-o… │ 5/6     │ 4      │ record_v… │ transiti… │
│ SURFACE   │ discovery │ claude-o… │ 3/6     │ 6      │ -         │ read-only │
└───────────┴───────────┴───────────┴─────────┴────────┴───────────┴───────────┘
...
config ok
```

The `skills` column is the one to read. Every agent shows 4-6 skills resolved
from `qaas/plugin/skills/`, inside `site-packages`. Under the old `parents[2]`
lookup none of them would have resolved, and `qaas validate` would have failed —
it checks every named skill and records a problem for each one it cannot find
(`src/qaas/cli.py:491-493`):

```python
        for skill in spec.skills:
            if _skill_path(skill) is None:
                problems.append(f"{name}: names skill '{skill}' with no SKILL.md")
```

The lookup those three lines go through carries its own tombstone
(`src/qaas/cli.py:33-42`):

```python
#: Where skills are found, in precedence order. This used to be
#: `Path(__file__).resolve().parents[2] / ".claude" / "skills"` -- a climb that
#: lands on the repo root from a source checkout and on
#: `site-packages/../..` from an install. So `qaas validate` failed for every
#: pip user (it checks all 30 skills exist), and agents ran with no skills at
#: all, silently, because a missing skill is an empty listing rather than an
#: error. Resolved through the workspace now, which searches the project first
#: and the packaged copy last.
def _skill_dirs() -> tuple[Path, ...]:
    return Workspace.resolve().skill_dirs
```

```
$ ../.venv/bin/qaas run --mode pr-check --dry-run
target: sample (none)
pr-check — 5 agents, budget $16.00, concurrency 2
  CARTOGRAPHER   claude-sonnet-5    effort=medium  turns<=60  $2.00
    tools: Read, Grep, Glob, Agent, Skill, Task, TodoWrite, ToolSearch, mcp__envelope
    prompt: 4817 chars
  ...
  CLERK          claude-sonnet-5    effort=high    turns<=50  $2.00
    tools: Read, Agent, Skill, Task, TodoWrite, ToolSearch, mcp__envelope, mcp__tracker, mcp__defect_memory
    prompt: 4711 chars
```

`prompt: 4817 chars` is the second thing to check. It is non-zero, which means
`qaas/prompts/CARTOGRAPHER.md` plus `_shared.md` were found inside the package.

All three commands exit 0, from a directory that has never heard of this
repository. That is the whole verification.

### 6. Publish

Nothing in CI publishes. Uploading is a manual act:

```bash
uvx twine upload dist/*
```

Do the clean-venv check first. The two commits this chapter is built on both went
out because someone read a config file instead of executing it.

---

## Summary

| | |
|---|---|
| Distribution | `qaas-python` (PyPI's `qaas` was taken) |
| Import package / CLI | `qaas` / `qaas` |
| Backend | hatchling, `src/` layout, `py3-none-any` |
| Entry point | `qaas = "qaas.cli:app"` |
| Wheel | 81 files, 248K — code, `defaults/config/`, `prompts/`, `plugin/` (30 skills) |
| Wheel excludes | `target-app/` (69M, deliberately vulnerable), `tests/`, `config/targets/` |
| Sdist includes | All of the above plus `target-app/`, `tests/`, the design docs |
| Why the split | Contributors need the calibration corpus for `qaas score`; users do not |
| Enforced by | The `build` job's `unzip -l | grep target-app/` assertion |
| The resource bug | `SKILLS_DIR = Path(__file__).resolve().parents[2]` → `site-packages/../..` |
| Why it was severe | A missing skill is an empty listing, not an error — agents ran degraded and silent |
| The fix | `paths.py`, one `__file__` seam (`package_root()`), one precedence order |
| Verification | Build → `twine check` → wheel assertion → install in a clean venv elsewhere → `init`, `validate`, `--dry-run` |
