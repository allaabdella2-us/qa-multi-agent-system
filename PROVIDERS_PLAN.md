# Providers plan: make the model a choice — provider seam, OpenAI / OpenRouter / Ollama, model matrix

_Working plan for the README roadmap (`README.md` → Roadmap). Built stage by stage; each stage lands with its verify step. Milestones M8–M11 in `BUILD_PLAN.md` are derived from this file._

## Context

`qaas` runs on the Claude Agent SDK only. The README roadmap (`README.md:363-395`) asks for
the model to become a choice: a thin provider seam in `runner.py`, an OpenAI-SDK
implementation behind it, OpenRouter and Ollama on top, and a scored per-agent model matrix
with `qaas score` as the referee. Everything else — envelope, guardrails, ledger, phase
machine — is already provider-agnostic; the coupling is `ClaudeAgentOptions`, the hook
events, and `query()`.

**Decisions taken with the user (2026-09-10):**
- **Wire API: both, Chat Completions first.** One loop, two thin transports. Chat ships
  first (universal across OpenAI / OpenRouter / Ollama); Responses follows and becomes the
  default for the `openai` builtin (OpenAI's newest models are Responses-only for tools).
- **Unpriced models are refused unless opted in** — at *validate* time. A USD governor that
  reads $0 is decorative. `free: true` (Ollama) or `allow_unpriced: true` opts in.
- **All five roadmap items, staged**; each stage lands on its own with a verify step.

**Rejected alternatives (one line each):**
- OpenAI Agents SDK as the loop — its hooks are observational-only, cannot deny a tool call;
  the PreToolUse veto is the guardrail model. Rejected.
- Native `ollama` package — Ollama serves an OpenAI-compatible `/v1` (chat, responses,
  tools); one provider with a `base_url` covers it.
- `model: "openrouter/…"` prefix syntax — an explicit `provider:` field validates cleanly
  under `extra="forbid"`.
- Decoupling the seven MCP servers from the SDK `@tool` decorator now — every server already
  has `build_tools(ctx)` returning objects with `.name .description .input_schema .handler`,
  which is all the OpenAI provider needs. Deferred to a "SDK becomes optional" milestone.

## What exists that this reuses (verified in code)

- SDK contact is ~40 lines: `runner.py:14-22,166-183`; `registry.py:19,33-38,419-472`;
  `guardrails.py:40-44,176-187`; `sdk_compat.py`; `mcp/*.py` `build()` wrappers.
- **Hooks are already SDK-neutral callables** `(input: dict, tool_use_id, context) -> dict`
  (`registry.build_hooks`, 270-365); outputs are plain dicts (PreToolUse deny =
  `hookSpecificOutput.permissionDecision`, PostToolUse `systemMessage`, Stop
  `decision: block` honouring `stop_hook_active`). `TurnRecord` counts `must_call`.
  `Guardrail.check(tool_name, input_data)` is pure and keys on
  `file_path|path|notebook_path` / `command`. → **one hook implementation, two dispatchers.**
- **MCP handlers are plain async callables**; `mcp/context.handlers()` and `tests/mcp/*`
  already call them without a transport. Result shape `ok()/err()`
  (`content[].text`, `structuredContent`, `isError`) is what `on_post_tool` reads.
- Router reads only `result.cost_usd / .error / .subtype` (`router.py:561-576`); findings
  come off disk (envelope diff, `runner.py:153,189`). → the seam sits *inside* `run_agent`.
- Precedents: `SystemConfig.mcp_servers` + `_agents_name_real_servers` (`config.py:202-226`)
  for a `providers:` block; `QAAS_TRACKER` env override (`config.py:266-272,332-336`);
  `qaas tracker-check` + `_env_display` (`cli.py:1334-1400`) for a masked credentials check;
  `tests/test_hooks_and_skills.py` `wire`/`fire`, `tests/test_router.py:29-77` `fake_agents`.
- `mcp` 2.2.0 installed (transitive). Its client API is **not** 1.x: `mcp.Client(target)`
  (`mcp/client/client.py`) accepts a `StdioServerParameters`, a URL string, a `Transport`, or
  an in-process `Server`; `mcp.client.streamable_http.streamable_http_client(url, *,
  http_client: httpx2.AsyncClient | None)`; `mcp.client.sse.sse_client(url, headers=...)`;
  `Tool.input_schema`, `CallToolResult.content/.structured_content/.is_error`.
- `tests/test_trace.py:118` — "Cost is data, not display": `qaas trace` must stay `$`-free.
- `openai` 3.11 (httpx2-based; `chat.completions` / `responses` shapes unchanged) is not
  installed — becomes an extra.
- Nothing in `src/qaas/prompts/*.md` names ToolSearch/Skill/Task, so prompts are
  provider-neutral as they stand.

---

## Stage 1 — Provider seam (M8): zero behaviour change

New:
- `src/qaas/providers/__init__.py` — `get_provider(name, cfg) -> Provider`; re-exports.
- `src/qaas/providers/base.py`
  ```python
  @dataclass
  class ProviderRun:
      subtype: str = "success"      # success | failure | error_max_turns | error_max_budget_usd
      error: str | None = None
      cost_usd: float = 0.0
      priced: bool = True           # False = tokens known, price unknown
      num_turns: int = 0            # model calls
      input_tokens: int = 0
      output_tokens: int = 0
      final_text: str = ""
      tool_calls: int = 0
      model: str = ""               # what actually ran

  class Provider(Protocol):
      name: str
      async def run(self, spec, ctx, task, *, max_budget_usd, emit) -> ProviderRun: ...
      def describe(self, spec) -> dict          # extras for --dry-run (base_url, api_key_env, priced)
      def check(self, specs) -> list[CheckRow]  # offline rows for validate / provider-check
  ```
- `src/qaas/providers/claude.py` — today's `query()` loop (`runner.py:165-183`,
  `_check_skills_loaded`, `_error_text`) and `build_options` (`registry.py:419-472`) moved
  verbatim, plus the `CanUseToolShadowedWarning` filter; wraps the neutral hook lists in
  `HookMatcher`; wraps `module.build_tools(ctx)` in `create_sdk_mcp_server`; calls
  `sdk_compat.check()` once, lazily. `ResultMessage.usage` → tokens when present.
- `src/qaas/providers/catalog.py` — `BUILTIN_PROVIDERS` + `resolve_provider_spec(name, cfg)`
  (declared beats builtin, like `build_mcp_servers`). Must not import `registry` (cycle).
  ```python
  "anthropic":  ProviderSpec(kind="anthropic", cost_from_response=True)
  "openai":     ProviderSpec(kind="openai_compat", api="chat"→"responses" after Stage 2b,
                             api_key_env="OPENAI_API_KEY", send_reasoning_effort=True, pricing=OPENAI_PRICES)
  "openrouter": ProviderSpec(kind="openai_compat", base_url="https://openrouter.ai/api/v1",
                             api_key_env="OPENROUTER_API_KEY", cost_from_response=True,
                             default_headers={"HTTP-Referer": <repo url>, "X-Title": "qaas"})
  "ollama":     ProviderSpec(kind="openai_compat", api_key_required=False, free=True, timeout_s=1800)
                # base_url from $OLLAMA_HOST (default http://localhost:11434) + "/v1", read in the provider
  ```
  `OPENAI_PRICES` is filled from the vendor price page at implementation time with an
  `as_of` on every entry — no numbers invented in this plan.

Existing:
- `runner.py` — drop SDK imports and the `options=` kwarg (no caller); `run_agent` keeps
  `_record_task`, `agent_started` (+`provider=`), envelope diff, `AgentResult`
  (+`provider, model, input_tokens, output_tokens, priced`), `put_result`, `finished` emit,
  and one `usage` ledger line per invocation. The try/except that captured
  `build_options` failures now captures provider construction + `run()`.
- `registry.py` — remove `ClaudeAgentOptions`/`HookMatcher`/warning filter; `build_hooks`
  returns `{event: [callable, ...]}`; **`build_options` stays as a lazy shim** delegating to
  `providers.claude` so `tests/test_registry.py:148-178`, `test_guardrails.py:493`,
  `test_hooks_and_skills.py:197` are untouched; `build_mcp_servers` returns neutral entries
  (`{"kind":"inprocess","tools":[…]}` / `{"kind":"stdio"|"http"|"sse", …}`) and the Claude
  provider converts; `describe()` gains `provider`.
- `sdk_compat.py` — `_EVENTS` computed inside `check()`; module-level call (line 52) removed.
- `guardrails.py:40-44` — left alone this roadmap (SDK stays a hard dependency); noted as
  the one remaining SDK contact outside `providers/claude.py`.
- `config.py` — `AgentSpec.provider: str = "anthropic"`; `ModelPrice(input_per_mtok,
  output_per_mtok, as_of)`; `ProviderSpec` (extra=forbid):
  `kind: Literal["anthropic","openai_compat"]`, `api: Literal["chat","responses"]="chat"`,
  `base_url`, `api_key_env`, `api_key_required=True`, `default_headers`, `timeout_s=600`,
  `max_retries=2`, `send_reasoning_effort=False`, `cost_from_response=False`, `free=False`,
  `allow_unpriced=False`, `pricing: dict[str, ModelPrice]`, `tool_result_max_chars=30_000`,
  `extra_body: dict`. `SystemConfig.providers: dict[str, ProviderSpec] = {}`; validators
  `_agents_name_real_providers` and `_agents_are_priced` (rule below). Env overrides
  `QAAS_PROVIDER` + `QAAS_MODEL` applied to every agent, **both or neither** (a provider
  switch with `claude-opus-5` still in the YAML is a guaranteed 404 after the first paid
  call); `load_config(provider=, model=)` kwargs; `qaas run/sweep/score --provider/--model`.
- `store.py` — `LedgerKind.USAGE = "usage"` (new member, never repurposed); `AgentResult`
  new fields with defaults so old result JSON still loads; `put_result` logs provider/model
  on `agent_finished`; `RunStore.unpriced_agents()`.
- `trace.py:34-36` — `provider`, `model` on agent_started/finished; `USAGE` detail fields;
  still no `$` formatting.
- `cli.py` — `validate` agents table (`:585-600`) gains `provider`; `--dry-run` line prints
  `provider/model`.
- `tests/test_hooks_and_skills.py:47` iterate `hooks[event]` directly;
  `tests/test_router.py:522-528` patch `qaas.providers.claude.query`.

**Verify:** `pytest` green (767 + new `tests/providers/test_catalog.py`: builtins resolve,
declared overrides builtin, unknown provider error text, `QAAS_PROVIDER` without
`QAAS_MODEL` refused, old `AgentResult` JSON loads). `qaas validate` shows the provider
column; `qaas run --mode pr-check --dry-run` unchanged apart from `anthropic/claude-opus-5`.
`grep -rn claude_agent_sdk src/qaas/runner.py src/qaas/registry.py` → empty.

---

## Stage 2 — `openai_compat` provider (M9): Chat first, Responses second

New `src/qaas/providers/openai_compat.py` — `AsyncOpenAI` imported inside `__init__`
(`ImportError` → `ProviderUnavailable("pip install 'qaas-python[openai]'")`, captured by the
runner as a failure); missing required key raises before any spend. Injectable `client=` for
tests. One loop, a `Transport` per wire API.

Tool table `_assemble_tools(spec, ctx)`: builtins filtered by `spec.builtin_tools` (+`Skill`
when `spec.skills`); in-process servers via `module.build_tools(ctx)` named
`mcp_tool_name(server, t.name)`; remote servers in Stage 3. Names asserted against
`[a-zA-Z0-9_-]{1,64}` at validate time for non-anthropic agents. Only the agent's own tools
are offered (the tool table, not the allowlist, decides what the model sees).

System prompt: `build_system_prompt(...)` + (off-Claude only) a `## Skills` block listing
`spec.skills` name + frontmatter description with "call `Skill` to read the procedure", and a
one-line `Working directory: <target_root>`.

Loop (per `run`):
```
messages = [system, user(task)]; record = TurnRecord(); hooks = build_hooks(Guardrail(ctx), ctx, record)
while True:
    if turns >= spec.max_turns: subtype = "error_max_turns"; break
    resp = await transport.complete(messages, tools, effort)     # non-streaming; no tool_choice, no parallel_tool_calls (Ollama rejects both)
    turns += 1; tokens += resp.usage; cost, priced &= price(prov, model, resp.usage)
    ctx.store.log("usage", turn=turns, input_tokens, output_tokens, cost_usd, priced)
    if max_budget_usd and priced and cost >= max_budget_usd: subtype = "error_max_budget_usd"; break
    messages.append(hand_built_assistant_dict)     # role/content/tool_calls only — never model_dump()
    if not resp.tool_calls:
        out = await stop_hook({"stop_hook_active": record.stop_blocks > 0})
        if out.get("decision") == "block": messages.append(user(out["reason"])); continue
        final_text = resp.content; break
    notes = []
    for call in resp.tool_calls:                    # returned order; sequential — in-process servers share ctx.counters
        emit("tool", …); args = json.loads(...)      # bad JSON → error tool message, not an exception
        deny = first PreToolUse hook returning permissionDecision == "deny"
        if deny: messages.append(tool_msg(call.id, f"Denied: {reason}")); continue     # denials never kill the turn
        result = await entry.call(args) if entry else err("unknown tool …")            # handler exceptions → err()
        for h in POST hooks: out = await h(post_payload); notes += [out["systemMessage"]] if any
        messages.append(tool_msg(call.id, render(result, cap)))
    if notes: messages.append(user("\n\n".join(notes)))   # AFTER the contiguous tool messages, or the API rejects the sequence
```
`render()` joins `content[].text`, prefixes `Error:` on `isError`, appends compact
`structuredContent` JSON (small models otherwise miss `fileable: false`), truncates head+tail.
Context-length errors (`BadRequestError.code == "context_length_exceeded"` or prose match;
Ollama 4xx/5xx with the same prose) → `failure` naming the model. `can_use_tool` is *not*
replicated: the allowlist auto-approval that justified it (`guardrails.py:8-17`) does not
exist when qaas owns the loop — say so in the docstring.

Transports:
- `ChatTransport`: `client.chat.completions.create(model, messages, tools=[{"type":"function",
  "function":{name,description,parameters}}] or NOT_GIVEN, reasoning_effort=…, extra_body)`;
  effort map `xhigh/max → high`; tool results `{"role":"tool","tool_call_id","content"}`;
  `usage.prompt_tokens/completion_tokens`; OpenRouter `usage.cost` via `usage.model_extra`.
- `ResponsesTransport` (2b, same stage): `client.responses.create(model, instructions=system,
  input=items, tools=[{"type":"function",name,description,parameters}], store=False,
  reasoning={"effort": spec.effort})`; **every output item is echoed back untouched**
  (reasoning items included — required for stateless reasoning models) followed by
  `{"type":"function_call_output","call_id","output"}`; `usage.input_tokens/output_tokens`.
  Becomes the `openai` builtin's default `api`.

New `src/qaas/providers/builtin_tools.py` — parameter names chosen so `Guardrail.check`
and `_check_bash` work unchanged; all paths resolved against and contained in
`ctx.target_root` (second belt behind the guardrail):

| tool | parameters | notes |
|---|---|---|
| `Read` | `file_path`*, `offset`, `limit` | `cat -n` style, 2000-line default, byte cap |
| `Write` | `file_path`*, `content`* | creates parents |
| `Edit` | `file_path`*, `old_string`*, `new_string`*, `replace_all` | exactly-once rule, else `err()` naming the count |
| `Grep` | `pattern`*, `path`, `glob`, `case_insensitive` | pure-Python `re` walk, skips `.git`/`node_modules`, 200-match cap |
| `Glob` | `pattern`*, `path` | mtime-sorted, 500 cap |
| `Bash` | `command`*, `timeout` (s, default 120, cap 600) | `create_subprocess_exec("bash","-c",…, cwd=target_root)` — `-c` not `-lc` (login shells source profiles); kill on timeout; merged output, tail-kept cap; exit code in `structuredContent` |
| `Skill` | `skill`* | resolves `name` / `plugin:name` via `Workspace.plugin_dirs`; body minus frontmatter; unknown → `err()` listing declared skills |

Not provided, documented: `ToolSearch`, `TodoWrite`, `Task`, `Agent` (no subagents
off-Claude), `NotebookRead/Edit`, `MultiEdit`.

New `src/qaas/providers/pricing.py` — `OPENAI_PRICES`, `price(prov, model, usage) ->
(cost, priced)`: `cost_from_response` → `usage.model_extra["cost"]` (absent → unpriced);
`free` → `(0, True)`; `pricing[model]` → tokens × per-Mtok; else `(0, False)`.
`estimate_context(spec, ctx)` (chars/4 of prompt + task + tool schemas) for `provider-check`.

**Pricing rule, precisely:**
- Validate time (`_agents_are_priced`): every enabled agent's provider must satisfy
  `free or cost_from_response or allow_unpriced or model in pricing`, else
  `ValueError("<agent> runs <model> on <provider>, which has no price for it: add
  pricing.<model> under providers.<provider>, set free: true, or allow_unpriced: true (then
  max_budget_usd cannot be enforced)")`. `allow_unpriced` prints a validate *note*.
- Runtime: never fail the agent (the money is spent). `priced=False` lands on `usage`,
  `agent_finished`, `AgentResult`; `Budget` (`router.py:110`) gains `unpriced: list[str]`,
  `_dispatch` calls `budget.spend(cost, priced=, agent=)`, and `Budget.check()` refuses the
  *next* dispatch with "spend cap cannot be enforced: <agents> ran unpriced" **only when
  `max_usd` is set**. `RunReport.summary()` adds `unpriced_agents`.

CLI `qaas provider-check [--provider] [--agent]` (after `tracker_check`, `cli.py:1706`):
masked key presence, `GET {base_url}/models` reachability, each agent's model present,
priced/free/unpriced, estimated context vs. window (Ollama `GET /api/show`), warn > 20
tools on a `free` provider. Add to `EXPECTED_COMMANDS` in `tests/test_cli.py:36`.

Packaging: `pyproject.toml` extra `openai = ["openai>=3,<4"]`; `mcp>=2.2,<3` promoted to a
direct dependency pinned at the already-resolved version; markers `ollama` (free, local
daemon) and `openai` (paid) added to `addopts`.

Tests (offline) — `tests/providers/fake_openai.py` scripted client recording every
`messages` argument; `tests/providers/test_openai_loop.py`: (1) tool call → handler → tool
message with matching id, contiguous, assistant dict has exactly role/content/tool_calls;
(2) denied `Write` → `Denied:` tool message, `tool_call(allowed=False)` + `denial` ledger,
`must_call` unsatisfied; (3) `fileable: false` → `systemMessage` as a user message after the
tool messages; (4) Stop block once, then `contract_unmet`; (5) `error_max_turns`, `ok` false;
(6) cost paths: `usage.cost`, pricing table, free, unknown → `priced=False`; (7)
`error_max_budget_usd` only when priced; (8) context-length → failure naming the model; (9)
bad JSON / unknown tool → error messages, no exception; (10) two tool_calls answered in order;
(11) `reasoning_effort` only when enabled, `xhigh→high`, `tool_choice` never sent; (12)
Responses transport echoes reasoning items and uses `function_call_output`.
`tests/providers/test_builtin_tools.py`: round-trips, guardrail parity (`Write` outside
`write_paths` denied with the same reason as on Claude), Bash timeout kill, caps, `Edit`
ambiguity, `Skill` body / unknown, `Read` outside root refused. `tests/test_config.py`:
unpriced fails validate, `allow_unpriced` passes with note, `pricing:` override wins.
`tests/test_router.py`: unpriced + cap → `BudgetExceeded` before next dispatch; no cap → run
completes and `summary()["unpriced_agents"]` lists it.

**Verify:** `pytest` green offline. `qaas validate` on a fixture with `provider: openai,
model: not-a-model` exits 1 with the pricing message. `QAAS_PROVIDER=ollama
QAAS_MODEL=qwen3 qaas run --mode pr-check --dry-run` renders `ollama/qwen3` and lists
`Skill`. `pytest -m openai tests/providers/test_live.py` runs MAPPER on `openai` (asserts a
`system_map` line and a priced `usage` line) and on `openrouter` (asserts `usage.cost`
landed — never assumed, since the fallback silently makes every agent unpriced).

---

## Stage 3 — Remote MCP off-Claude + Ollama profile (M10)

New `src/qaas/providers/mcp_client.py` against the installed `mcp` 2.2:
```python
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
# stdio → StdioServerParameters(command, args, env={**os.environ, **env}, cwd=target_root)
# http  → streamable_http_client(url, http_client=httpx2.AsyncClient(headers=headers))
# sse   → sse_client(url, headers=headers)
client = await stack.enter_async_context(Client(target, read_timeout_seconds=timeout))
tools  = (await client.list_tools()).tools          # Tool.name/.description/.input_schema
r      = await client.call_tool(name, args)         # .content/.structured_content/.is_error → ok()/err() shape
```
One `AsyncExitStack` per `run`, entered before the first model call, closed in `finally`
(Playwright must die with the agent). Entries come from `registry.build_mcp_servers`, so
user-declared servers and `expand_env` work unchanged. Check `StdioServerParameters.env`
replace-vs-merge semantics (`mcp/client/stdio.py` `_get_default_environment`) at
implementation time.

Ollama: `OLLAMA_HOST` read in the provider; `num_ctx` **cannot** be set through the
OpenAI-compatible endpoint — `provider-check` estimates context and warns; MANUAL documents
`OLLAMA_CONTEXT_LENGTH` (verify the env-var name against the installed daemon) / a Modelfile
`PARAMETER num_ctx`. Models that emit tool calls as text (`<tool_call>` in content) are not
parsed: Stop fires, `must_call` blocks once, result is `contract_unmet` — documented, no
text parser.

Tests: `tests/providers/fake_mcp.py` `FakeClient`; naming `mcp__playwright__<tool>` with the
server's schema; `is_error` → `isError`; stack closes on provider exception; one real
in-process end-to-end via `mcp.Client(server)` against a tiny `mcp.server.Server` fixture so
the transport code runs offline.

**Verify:** `pytest`; `pytest -m ollama` runs MAPPER against a local `qwen3` and asserts a
`system_map` line and `usage` with `cost_usd == 0, priced: true`;
`QAAS_PROVIDER=openai QAAS_MODEL=<model> qaas run --dry-run --only BROWSER` lists Playwright.

---

## Stage 4 — Cost/provider surfaced, `score --by-agent`, `qaas matrix` (M11)

- `cli.runs` (`:860`): columns `run, envelopes, agents, providers, cost` — `cost` shows
  `unpriced` when any result has `priced=False`. `cli.show` (`:890`): cost header and a
  per-agent block `MAPPER  ollama/qwen3  12 turns  0.00`. `trace` stays `$`-free.
- `scorecard.py`: `score(..., results=)` joins `envelope.discovered_by` to `AgentResult`s →
  `Scorecard.by_agent` (provider, model, envelopes, matched, false_positives, cost, priced);
  `qaas score --by-agent`.
- `qaas matrix <matrix.yaml> [--mode] [--dry-run] [--json]`: `MatrixSpec(mode, rows:
  [MatrixRow(name, provider, model, agents: {AGENT: {provider, model}}, only: [])])` —
  `provider`/`model` both-or-neither everywhere. Per row: `load_config(...)` with overrides
  → re-validate (pricing rule runs per row) → `Router(cfg).run(mode)` via a
  `_run_and_score()` shared with `sweep` (`cli.py:1282-1332`) → table row: recall,
  precision, false-positives, severity agreement, cost|unpriced, wall clock. Sequential
  (target app state). Writes `.qaas/matrix/<timestamp>.json`; exits 1 if a row failed.
- `tests/test_cli.py` `EXPECTED_COMMANDS` += `matrix`, `provider-check`; matrix dry-run
  with a two-row fixture; a matrix run using `fake_agents`.

**Verify:** `pytest`; `qaas matrix tests/fixtures/matrix.yaml --dry-run` prints two rows;
`qaas runs` on a fixture store shows `unpriced` for a `priced=False` result.

---

## Stage 5 — Documentation (lands with each stage)

- `README.md:363-395` roadmap ticks + a "Providers" section (`provider:` per agent,
  `--provider/--model`, `qaas matrix`, what off-Claude agents cannot do); auth line `:97`.
- `MANUAL.md`: install extra; env table (`QAAS_PROVIDER`, `QAAS_MODEL`, `OPENAI_API_KEY`,
  `OPENROUTER_API_KEY`, `OLLAMA_HOST`); `providers:` block with a pricing override; commands
  `provider-check`, `matrix`, `score --by-agent`; "Keeping runs bounded" → unpriced rule.
- `ARCHITECTURE.md` §5.2 (`:141-168`): `providers.get_provider(...).run` with two branches;
  §13 gaps: OSS models with small context truncate silently; no subagents off-Claude.
- `CLAUDE.md`: "Two things" #2 reworded; new "Providers" subsection (seam at `run_agent`,
  hooks are the contract, unpriced rule, `providers:` grants nothing until an agent names it,
  `build_hooks` returns plain callables, trace stays money-free); commands + markers.
- `BUILD_PLAN.md`: rewrite "Cost and auth" (`:277-291`); milestones M8–M11 in the
  `### Mn — … **Verify:**` format with the verify lines above.
- `tutorial/01-code-structure.md:11,152,164`, `tutorial/03-skills-and-hooks.md`, `CHANGELOG.md`
  (`0.1.0 — the model is a choice`).

**Verify:** `qaas validate`, `qaas run --mode pr-check --dry-run`, `pytest`;
`grep -n build_options ARCHITECTURE.md tutorial/*.md` → only the shim mention.

---

## Risks carried into implementation

- Small/OSS models vs. tool count: §5.3's six-server cap was set for Claude; `provider-check`
  warns > 20 tools on `free` providers; the matrix shows it as precision — that is the point.
- Context growth without SDK compaction: tool results capped; context-length is a clean
  `failure`, never a hang.
- `reasoning_effort` is not OpenRouter's native parameter (`reasoning: {effort}`) — shipped
  off for that builtin, opt-in per provider.
- `openai` SDK shapes: build the assistant dict by hand; `usage.model_extra` for `cost`;
  `NOT_GIVEN` not `None` for omitted `tools`.
- Supply chain unchanged: `setting_sources=[]` stays; nothing new is loaded from the target;
  builtin tools are anchored on `target_root` and pass through `Guardrail.check` exactly as
  the SDK path does.
