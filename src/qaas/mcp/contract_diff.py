"""The `contract_diff` MCP server — what changed for the people calling you.

A text diff of two OpenAPI documents tells an agent that lines moved. It does
not tell it that `currency` vanished from the Invoice response and every
consumer reading that field now gets a KeyError. This server answers the second
question: it walks both documents structurally, resolves `$ref`s, and reports
changes as consumer-visible facts with a breaking/non-breaking verdict attached.

Two things follow from CONDUIT's brief (§4.5):

* **The declared contract is the reference.** `spec_a` defaults to
  `target-app/openapi.yaml` and `spec_b` to the running app's `/openapi.json`,
  so "drift" here means the implementation disagrees with the published spec —
  which is the defect, not the other way round.
* **A finding ships with a failing test.** `generate_contract_test` emits a
  standalone pytest module that asserts the spec's promises against a live
  server. That file is the evidence an envelope cites; it must fail on the
  violating implementation and pass on a conforming one, or it is worthless.

Classification follows one fixed rule set, applied identically by `diff_openapi`
and `classify_breaking` so the two can never disagree: losing a guarantee is
breaking (a removed endpoint or field, a dropped required-ness, a narrowed type,
a newly required request input, a changed status code); gaining an optional one
is not.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

import yaml
from claude_agent_sdk import create_sdk_mcp_server, tool

from qaas.mcp.context import ToolContext, err, ok

DEFAULT_SPEC_FILE = "openapi.yaml"
DEFAULT_LIVE_SPEC_URL = "http://localhost:8000/openapi.json"
FETCH_TIMEOUT_S = 10
GENERATED_DIR = "generated"

HTTP_METHODS = ("get", "put", "post", "delete", "patch", "options", "head", "trace")

#: Source extensions worth grepping for call sites. Everything else is noise.
CONSUMER_SUFFIXES = frozenset(
    {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte",
     ".py", ".go", ".rb", ".java", ".kt", ".rs", ".php", ".cs", ".swift"}
)
SKIP_DIRS = frozenset({".git", ".venv", "node_modules", "__pycache__", "dist", "build", ".qaas", ".pytest_cache", ".mypy_cache"})
MAX_CONSUMER_HITS = 200

#: kind -> (verdict, why). The single source of truth for breaking-ness: every
#: change `diff_openapi` emits carries a kind from this table, and
#: `classify_breaking` is a lookup into it. One table, one answer.
RULES: dict[str, tuple[str, str]] = {
    "endpoint_removed": ("breaking", "Consumers calling this endpoint now get a 404."),
    "endpoint_added": ("non_breaking", "New surface; no existing caller is affected."),
    "status_code_removed": ("breaking", "A documented outcome disappeared; consumers branching on it are wrong."),
    "status_code_added": ("non_breaking", "An additional documented outcome; existing handling still applies."),
    "response_field_removed": ("breaking", "Consumers reading this field get nothing back."),
    "response_field_renamed": ("breaking", "A rename is a removal and an addition; every reader of the old name breaks."),
    "response_field_added": ("non_breaking", "An added optional response field is ignored by existing consumers."),
    "response_required_dropped": ("breaking", "The field was guaranteed present and no longer is."),
    "response_required_added": ("non_breaking", "A field that was optional is now always present; that only helps."),
    "response_type_narrowed": ("breaking", "The value set shrank; consumers may receive nothing they can use."),
    "response_type_widened": ("non_breaking", "The declared value set grew; previously valid values still arrive."),
    "response_enum_narrowed": ("breaking", "Documented values were withdrawn."),
    "response_enum_widened": ("non_breaking", "Additional documented values. Consumers with exhaustive switches should still be told."),
    "request_field_removed": ("breaking", "Requests that carried this field may now be rejected."),
    "request_field_added_required": ("breaking", "Existing requests omit it and will now fail validation."),
    "request_field_added_optional": ("non_breaking", "Existing requests remain valid."),
    "request_required_added": ("breaking", "A previously optional input is now mandatory."),
    "request_required_dropped": ("non_breaking", "Fewer inputs are mandatory; existing requests still validate."),
    "request_type_narrowed": ("breaking", "Values that used to validate no longer do."),
    "request_type_widened": ("non_breaking", "More values validate than before."),
    "parameter_removed": ("breaking", "Callers passing this parameter silently lose the behaviour it controlled."),
    "parameter_added_required": ("breaking", "Existing callers omit it and will now fail."),
    "parameter_added_optional": ("non_breaking", "Existing callers are unaffected."),
    "parameter_required_added": ("breaking", "A previously optional parameter is now mandatory."),
    "parameter_type_narrowed": ("breaking", "Values callers already send may now be rejected."),
    "parameter_type_widened": ("non_breaking", "More values are accepted than before."),
    "security_added": ("breaking", "An endpoint that accepted anonymous calls now requires credentials."),
    "security_removed": ("non_breaking", "Compatibility is unaffected, but dropping auth is a security finding in its own right."),
}


# --------------------------------------------------------------------------
# spec loading
# --------------------------------------------------------------------------


def _is_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def _fetch_spec(url: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as resp:  # noqa: S310 - http(s) only, checked by caller
            body = resp.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code} from {url}"
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        return None, f"could not reach {url} ({exc})"
    try:
        doc = json.loads(body)
    except json.JSONDecodeError:
        try:
            doc = yaml.safe_load(body)
        except yaml.YAMLError as exc:
            return None, f"{url} returned something that is neither JSON nor YAML ({exc})"
    return (doc, None) if isinstance(doc, dict) else (None, f"{url} did not return an object")


def _read_spec(ref: str, repo_root: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Load a spec from a URL or a path. Returns (doc, error)."""
    if _is_url(ref):
        return _fetch_spec(ref)
    path = Path(ref)
    if not path.is_absolute():
        path = repo_root / path
    if not path.exists():
        return None, f"no such spec file: {path}"
    try:
        doc = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        return None, f"could not parse {path}: {exc}"
    return (doc, None) if isinstance(doc, dict) else (None, f"{path} does not contain an OpenAPI object")


# --------------------------------------------------------------------------
# schema walking
# --------------------------------------------------------------------------


def _resolve(node: Any, doc: dict[str, Any], seen: frozenset[str] = frozenset()) -> tuple[Any, frozenset[str]]:
    """Follow local `$ref`s. Cycles stop at the second visit rather than recurse."""
    guard = 0
    while isinstance(node, dict) and "$ref" in node and guard < 20:
        ref = str(node["$ref"])
        if not ref.startswith("#/") or ref in seen:
            return {}, seen
        seen = seen | {ref}
        target: Any = doc
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, dict) or part not in target:
                return {}, seen
            target = target[part]
        node = target
        guard += 1
    return node, seen


def _merged(schema: Any, doc: dict[str, Any], seen: frozenset[str]) -> tuple[dict[str, Any], frozenset[str]]:
    """Resolve a schema and flatten a single level of allOf into it."""
    schema, seen = _resolve(schema, doc, seen)
    if not isinstance(schema, dict):
        return {}, seen
    if "allOf" not in schema:
        return schema, seen
    merged: dict[str, Any] = {k: v for k, v in schema.items() if k != "allOf"}
    props: dict[str, Any] = dict(merged.get("properties") or {})
    required: list[str] = list(merged.get("required") or [])
    for part in schema["allOf"]:
        sub, seen = _merged(part, doc, seen)
        props.update(sub.get("properties") or {})
        required.extend(sub.get("required") or [])
        for key, value in sub.items():
            if key not in ("properties", "required"):
                merged.setdefault(key, value)
    merged["properties"] = props
    merged["required"] = sorted(set(required))
    return merged, seen


def _types(schema: dict[str, Any]) -> frozenset[str]:
    """The JSON types a schema admits, unioned across type lists and anyOf/oneOf."""
    out: set[str] = set()
    raw = schema.get("type")
    if isinstance(raw, str):
        out.add(raw)
    elif isinstance(raw, list):
        out.update(str(t) for t in raw)
    for key in ("anyOf", "oneOf"):
        for part in schema.get(key) or []:
            if isinstance(part, dict):
                out |= _types(part)
    if not out and "properties" in schema:
        out.add("object")
    if not out and "items" in schema:
        out.add("array")
    return frozenset(out)


def _enum(schema: dict[str, Any]) -> frozenset[str] | None:
    values = schema.get("enum")
    if isinstance(values, list):
        return frozenset(json.dumps(v, sort_keys=True) for v in values)
    for key in ("anyOf", "oneOf"):
        collected: set[str] = set()
        for part in schema.get(key) or []:
            sub = _enum(part) if isinstance(part, dict) else None
            if sub:
                collected |= set(sub)
        if collected:
            return frozenset(collected)
    return None


def flatten_schema(schema: Any, doc: dict[str, Any], prefix: str = "", *, depth: int = 0,
                   seen: frozenset[str] = frozenset()) -> dict[str, dict[str, Any]]:
    """Field path -> {types, enum, required}. Arrays flatten as `items[].field`.

    Dotted paths are what a consumer actually reads (`items[].currency`), so a
    diff expressed over them lands in the same vocabulary as the bug report.
    """
    out: dict[str, dict[str, Any]] = {}
    if depth > 12:
        return out
    node, seen = _merged(schema, doc, seen)
    if not isinstance(node, dict):
        return out

    if "items" in node:
        out.update(flatten_schema(node["items"], doc, f"{prefix}[]", depth=depth + 1, seen=seen))

    required = set(node.get("required") or [])
    for name, sub in (node.get("properties") or {}).items():
        path = f"{prefix}.{name}" if prefix else str(name)
        resolved, sub_seen = _merged(sub, doc, seen)
        out[path] = {
            "types": _types(resolved),
            "enum": _enum(resolved),
            "required": name in required,
        }
        out.update(flatten_schema(resolved, doc, path, depth=depth + 1, seen=sub_seen))
    return out


def _json_schema(container: Any, doc: dict[str, Any]) -> Any:
    """The JSON body schema out of a responses/requestBody entry, if there is one."""
    node, _ = _resolve(container, doc)
    if not isinstance(node, dict):
        return None
    content = node.get("content")
    if not isinstance(content, dict):
        return None
    for media, spec in content.items():
        if "json" in str(media):
            return (spec or {}).get("schema")
    first = next(iter(content.values()), None)
    return (first or {}).get("schema") if isinstance(first, dict) else None


def _operations(doc: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """(path, METHOD) -> operation, with path-level parameters folded in."""
    ops: dict[tuple[str, str], dict[str, Any]] = {}
    for path, item in (doc.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        shared = item.get("parameters") or []
        for method in HTTP_METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            merged = dict(op)
            merged["parameters"] = list(shared) + list(op.get("parameters") or [])
            ops[(str(path), method.upper())] = merged
    return ops


def _requires_auth(op: dict[str, Any], doc: dict[str, Any]) -> bool:
    security = op.get("security", doc.get("security", []))
    return bool(security) and any(bool(entry) for entry in security)


def _parameters(op: dict[str, Any], doc: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in op.get("parameters") or []:
        param, _ = _resolve(raw, doc)
        if not isinstance(param, dict) or "name" not in param:
            continue
        schema, _ = _merged(param.get("schema") or {}, doc, frozenset())
        out[(str(param.get("in", "query")), str(param["name"]))] = {
            "required": bool(param.get("required", False)),
            "types": _types(schema),
            "enum": _enum(schema),
        }
    return out


# --------------------------------------------------------------------------
# the diff itself
# --------------------------------------------------------------------------


def _change(kind: str, path: str, method: str, detail: str) -> dict[str, Any]:
    verdict, _ = RULES.get(kind, ("unknown", ""))
    return {"kind": kind, "path": path, "method": method, "detail": detail, "breaking": verdict == "breaking"}


def _kind(prefix: str, facet: str, direction: str) -> str:
    """`response_enum_widened` if that rule exists, else the `_type_` equivalent.

    Only responses get their own enum rules; for requests and parameters an enum
    change is just a change to the admissible value space, which is what the
    type rules already say.
    """
    candidate = f"{prefix}_{facet}_{direction}"
    return candidate if candidate in RULES else f"{prefix}_type_{direction}"


def _compare_value_space(a: dict[str, Any], b: dict[str, Any], prefix: str) -> str | None:
    """The widened/narrowed kind for a field's value space, or None if unchanged.

    Direction is decided by subset relation, not by name: only a strict shrink of
    the admissible values can break a consumer that already works. An unrelated
    change (string -> integer, one enum swapped for another) counts as narrowing,
    because at least one value the consumer handled is now impossible.
    """
    enum_a, enum_b = a.get("enum"), b.get("enum")
    if enum_a != enum_b:
        if enum_a is None:  # was unconstrained, now restricted
            return _kind(prefix, "enum", "narrowed")
        if enum_b is None:  # was restricted, now open
            return _kind(prefix, "enum", "widened")
        return _kind(prefix, "enum", "widened" if enum_a < enum_b else "narrowed")

    types_a, types_b = a.get("types") or frozenset(), b.get("types") or frozenset()
    if types_a == types_b:
        return None
    if types_a and types_b and types_a < types_b:
        return f"{prefix}_type_widened"
    return f"{prefix}_type_narrowed"


def _detect_renames(removed: list[str], added: list[str], fields_a: dict[str, dict[str, Any]],
                    fields_b: dict[str, dict[str, Any]]) -> list[tuple[str, str]]:
    """Pair a removal with an addition when the sibling and shape both match.

    Heuristic, deliberately conservative: a pair is only a rename when exactly
    one candidate matches, so an object that lost two fields and gained two is
    reported as four changes rather than two invented renames.
    """
    pairs: list[tuple[str, str]] = []
    taken: set[str] = set()
    for old in removed:
        parent = old.rsplit(".", 1)[0] if "." in old else ""
        shape = (fields_a[old]["types"], fields_a[old]["enum"], fields_a[old]["required"])
        candidates = [
            new for new in added
            if new not in taken
            and (new.rsplit(".", 1)[0] if "." in new else "") == parent
            and (fields_b[new]["types"], fields_b[new]["enum"], fields_b[new]["required"]) == shape
        ]
        if len(candidates) == 1:
            taken.add(candidates[0])
            pairs.append((old, candidates[0]))
    return pairs


def diff_specs(spec_a: dict[str, Any], spec_b: dict[str, Any]) -> list[dict[str, Any]]:
    """Structural diff of two OpenAPI documents, A being the reference."""
    ops_a, ops_b = _operations(spec_a), _operations(spec_b)
    changes: list[dict[str, Any]] = []

    for key in sorted(set(ops_a) - set(ops_b)):
        changes.append(_change("endpoint_removed", key[0], key[1], f"{key[1]} {key[0]} is declared in A but absent from B."))
    for key in sorted(set(ops_b) - set(ops_a)):
        changes.append(_change("endpoint_added", key[0], key[1], f"{key[1]} {key[0]} exists in B but is undeclared in A."))

    for key in sorted(set(ops_a) & set(ops_b)):
        path, method = key
        op_a, op_b = ops_a[key], ops_b[key]
        changes.extend(_diff_security(op_a, op_b, spec_a, spec_b, path, method))
        changes.extend(_diff_parameters(op_a, op_b, spec_a, spec_b, path, method))
        changes.extend(_diff_request(op_a, op_b, spec_a, spec_b, path, method))
        changes.extend(_diff_responses(op_a, op_b, spec_a, spec_b, path, method))

    return changes


def _diff_security(op_a: dict[str, Any], op_b: dict[str, Any], doc_a: dict[str, Any], doc_b: dict[str, Any],
                   path: str, method: str) -> list[dict[str, Any]]:
    auth_a, auth_b = _requires_auth(op_a, doc_a), _requires_auth(op_b, doc_b)
    if auth_a == auth_b:
        return []
    kind = "security_added" if auth_b else "security_removed"
    verb = "now requires" if auth_b else "no longer requires"
    return [_change(kind, path, method, f"{method} {path} {verb} authentication.")]


def _diff_parameters(op_a: dict[str, Any], op_b: dict[str, Any], doc_a: dict[str, Any], doc_b: dict[str, Any],
                     path: str, method: str) -> list[dict[str, Any]]:
    params_a, params_b = _parameters(op_a, doc_a), _parameters(op_b, doc_b)
    changes: list[dict[str, Any]] = []
    for key in sorted(set(params_a) - set(params_b)):
        changes.append(_change("parameter_removed", path, method, f"{key[0]} parameter '{key[1]}' was removed."))
    for key in sorted(set(params_b) - set(params_a)):
        kind = "parameter_added_required" if params_b[key]["required"] else "parameter_added_optional"
        changes.append(_change(kind, path, method, f"{key[0]} parameter '{key[1]}' was added."))
    for key in sorted(set(params_a) & set(params_b)):
        a, b = params_a[key], params_b[key]
        if not a["required"] and b["required"]:
            changes.append(_change("parameter_required_added", path, method, f"{key[0]} parameter '{key[1]}' became required."))
        kind = _compare_value_space(a, b, "parameter")
        if kind:
            changes.append(_change(kind, path, method,
                                   f"{key[0]} parameter '{key[1]}': {_describe(a)} -> {_describe(b)}."))
    return changes


def _diff_request(op_a: dict[str, Any], op_b: dict[str, Any], doc_a: dict[str, Any], doc_b: dict[str, Any],
                  path: str, method: str) -> list[dict[str, Any]]:
    schema_a = _json_schema(op_a.get("requestBody") or {}, doc_a)
    schema_b = _json_schema(op_b.get("requestBody") or {}, doc_b)
    if schema_a is None and schema_b is None:
        return []
    fields_a = flatten_schema(schema_a or {}, doc_a)
    fields_b = flatten_schema(schema_b or {}, doc_b)
    changes: list[dict[str, Any]] = []
    for name in sorted(set(fields_a) - set(fields_b)):
        changes.append(_change("request_field_removed", path, method, f"request field '{name}' was removed."))
    for name in sorted(set(fields_b) - set(fields_a)):
        kind = "request_field_added_required" if fields_b[name]["required"] else "request_field_added_optional"
        changes.append(_change(kind, path, method, f"request field '{name}' was added."))
    for name in sorted(set(fields_a) & set(fields_b)):
        a, b = fields_a[name], fields_b[name]
        if not a["required"] and b["required"]:
            changes.append(_change("request_required_added", path, method, f"request field '{name}' became required."))
        elif a["required"] and not b["required"]:
            changes.append(_change("request_required_dropped", path, method, f"request field '{name}' is no longer required."))
        kind = _compare_value_space(a, b, "request")
        if kind:
            changes.append(_change(kind, path, method, f"request field '{name}': {_describe(a)} -> {_describe(b)}."))
    return changes


def _diff_responses(op_a: dict[str, Any], op_b: dict[str, Any], doc_a: dict[str, Any], doc_b: dict[str, Any],
                    path: str, method: str) -> list[dict[str, Any]]:
    responses_a = {str(k): v for k, v in (op_a.get("responses") or {}).items()}
    responses_b = {str(k): v for k, v in (op_b.get("responses") or {}).items()}
    changes: list[dict[str, Any]] = []

    for code in sorted(set(responses_a) - set(responses_b)):
        changes.append(_change("status_code_removed", path, method, f"documented status {code} is gone."))
    for code in sorted(set(responses_b) - set(responses_a)):
        changes.append(_change("status_code_added", path, method, f"status {code} is documented in B only."))

    for code in sorted(set(responses_a) & set(responses_b)):
        schema_a = _json_schema(responses_a[code], doc_a)
        schema_b = _json_schema(responses_b[code], doc_b)
        if schema_a is None and schema_b is None:
            continue
        fields_a = flatten_schema(schema_a or {}, doc_a)
        fields_b = flatten_schema(schema_b or {}, doc_b)
        removed = sorted(set(fields_a) - set(fields_b))
        added = sorted(set(fields_b) - set(fields_a))
        renamed = _detect_renames(removed, added, fields_a, fields_b)
        renamed_old = {old for old, _ in renamed}
        renamed_new = {new for _, new in renamed}

        for old, new in renamed:
            changes.append(_change("response_field_renamed", path, method,
                                   f"{code} response field '{old}' appears to have been renamed to '{new}'."))
        for name in removed:
            if name in renamed_old:
                continue
            qualifier = "required " if fields_a[name]["required"] else ""
            changes.append(_change("response_field_removed", path, method,
                                   f"{code} response is missing the {qualifier}field '{name}' the reference declares."))
        for name in added:
            if name in renamed_new:
                continue
            qualifier = "required" if fields_b[name]["required"] else "optional"
            changes.append(_change("response_field_added", path, method,
                                   f"{code} response gained the {qualifier} field '{name}'."))
        for name in sorted(set(fields_a) & set(fields_b)):
            a, b = fields_a[name], fields_b[name]
            if a["required"] and not b["required"]:
                changes.append(_change("response_required_dropped", path, method,
                                       f"{code} response field '{name}' is no longer guaranteed present."))
            elif not a["required"] and b["required"]:
                changes.append(_change("response_required_added", path, method,
                                       f"{code} response field '{name}' is now always present."))
            kind = _compare_value_space(a, b, "response")
            if kind:
                changes.append(_change(kind, path, method,
                                       f"{code} response field '{name}': {_describe(a)} -> {_describe(b)}."))
    return changes


def _describe(field: dict[str, Any]) -> str:
    types = "/".join(sorted(field.get("types") or [])) or "any"
    enum = field.get("enum")
    if enum:
        # Enum members are stored as canonical JSON so they can be set-compared;
        # decode them again for a message a human reads.
        values = sorted(str(json.loads(v)) for v in enum)
        return f"{types} enum[{', '.join(values)}]"
    return types


# --------------------------------------------------------------------------
# consumers and generated tests
# --------------------------------------------------------------------------


def _split_endpoint(raw: str) -> tuple[str | None, str]:
    """'GET /v1/orders' -> ('GET', '/v1/orders'); a bare path -> (None, path)."""
    parts = raw.strip().split()
    if len(parts) == 2 and parts[0].upper() in {m.upper() for m in HTTP_METHODS}:
        return parts[0].upper(), parts[1]
    return None, parts[-1] if parts else raw.strip()


def _search_terms(path: str) -> list[str]:
    """Literal needles for a templated path: the whole thing and its stable prefix."""
    terms = {path}
    head = path.split("{", 1)[0].rstrip("/")
    if head and head != path:
        terms.add(head)
    return sorted(terms, key=len, reverse=True)


def _walk_sources(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if Path(name).suffix in CONSUMER_SUFFIXES:
                yield Path(dirpath) / name


def _identifier(method: str, path: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", path.lower()).strip("_")
    return f"{method.lower()}_{slug}" or "endpoint"


def _required_fields(schema: Any, doc: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(top-level required fields, required fields of `items[]`) for a response schema."""
    fields = flatten_schema(schema or {}, doc)
    top = sorted(n for n, f in fields.items() if f["required"] and "." not in n and "[]" not in n)
    item = sorted(
        n.split("items[].", 1)[1]
        for n, f in fields.items()
        if f["required"] and n.startswith("items[].") and "." not in n.split("items[].", 1)[1]
    )
    return top, item


TEST_TEMPLATE = '''"""Contract test for {method} {path} — generated by CONDUIT from {spec_name}.

{why}

Runs against a live server: set QAAS_TARGET_BASE_URL (default {default_base}).
Supply QAAS_TARGET_TOKEN to skip the login round-trip. Standard library only, so
it runs anywhere pytest does — including in a fix branch's CI.
"""

import json
import os
import urllib.error
import urllib.request

import pytest

BASE_URL = os.environ.get("QAAS_TARGET_BASE_URL", "{default_base}").rstrip("/")
PATH = {path!r}
METHOD = {method!r}
EXPECTED_STATUS = {expected_status}
REQUIRED_TOP_LEVEL_FIELDS = {required_top!r}
REQUIRED_ITEM_FIELDS = {required_item!r}
AUTH_REQUIRED = {auth_required!r}
LOGIN_EMAIL = "admin@northwind.test"
LOGIN_PASSWORD = "password123"


def _call(path, method="GET", token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {{"Accept": "application/json"}}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")
    except (urllib.error.URLError, OSError) as exc:
        pytest.fail("target app unreachable at " + BASE_URL + ": " + str(exc))


@pytest.fixture(scope="module")
def token():
    if not AUTH_REQUIRED:
        return None
    preset = os.environ.get("QAAS_TARGET_TOKEN")
    if preset:
        return preset
    status, body = _call("/v1/auth/login", "POST", body={{"email": LOGIN_EMAIL, "password": LOGIN_PASSWORD}})
    assert status == 200, "could not log in to fetch a token: HTTP %s %s" % (status, body[:300])
    return json.loads(body)["access_token"]


@pytest.fixture(scope="module")
def response(token):
    status, body = _call(PATH, METHOD, token=token)
    return status, body


def test_status_code_matches_the_spec(response):
    status, body = response
    assert status == EXPECTED_STATUS, (
        "%s %s: spec declares %s, server returned %s. Body: %s"
        % (METHOD, PATH, EXPECTED_STATUS, status, body[:300])
    )


def test_response_is_json(response):
    _, body = response
    try:
        json.loads(body)
    except json.JSONDecodeError:
        pytest.fail("%s %s did not return JSON: %s" % (METHOD, PATH, body[:300]))


def test_required_response_fields_are_present(response):
    if not REQUIRED_TOP_LEVEL_FIELDS:
        pytest.skip("the spec declares no required top-level response fields")
    _, body = response
    payload = json.loads(body)
    missing = [name for name in REQUIRED_TOP_LEVEL_FIELDS if name not in payload]
    assert not missing, (
        "%s %s response is missing spec-required field(s): %s"
        % (METHOD, PATH, ", ".join(missing))
    )


def test_collection_items_carry_required_fields(response):
    if not REQUIRED_ITEM_FIELDS:
        pytest.skip("this response is not a collection with a declared item schema")
    _, body = response
    payload = json.loads(body)
    items = payload.get("items") if isinstance(payload, dict) else None
    if not items:
        pytest.skip("no items returned; seed the fixture to exercise this assertion")
    missing = sorted({{name for item in items for name in REQUIRED_ITEM_FIELDS if name not in item}})
    assert not missing, (
        "%s %s items are missing spec-required field(s): %s"
        % (METHOD, PATH, ", ".join(missing))
    )


def test_endpoint_requires_authentication():
    if not AUTH_REQUIRED:
        pytest.skip("the spec marks this endpoint as public")
    status, body = _call(PATH, METHOD)
    assert status == 401, (
        "%s %s is declared as authenticated but answered an anonymous call with %s"
        % (METHOD, PATH, status)
    )
'''


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------


def build_tools(ctx: ToolContext) -> list:
    """The contract_diff tools, bound to one agent's run context.

    Split from `build` so tests can call the handlers directly without standing
    up an MCP transport.
    """

    default_spec = ctx.target_app / DEFAULT_SPEC_FILE

    @tool(
        "diff_openapi",
        "Compare two OpenAPI documents semantically. Defaults to the declared contract "
        "(target-app/openapi.yaml) against the running app's /openapi.json, so a reported change "
        "means the implementation disagrees with the spec. Pass file paths when the app is down.",
        {
            "type": "object",
            "properties": {
                "spec_a": {"type": "string", "description": "Reference spec: a path or an http(s) URL. Default: the declared openapi.yaml."},
                "spec_b": {"type": "string", "description": "Spec under test. Default: the running app's /openapi.json."},
                "only_breaking": {"type": "boolean", "description": "Return only changes classified breaking."},
            },
        },
    )
    async def diff_openapi(args: dict[str, Any]) -> dict[str, Any]:
        ref_a = str(args.get("spec_a") or default_spec)
        live_default = os.environ.get("QAAS_TARGET_BASE_URL")
        live_default = f"{live_default.rstrip('/')}/openapi.json" if live_default else DEFAULT_LIVE_SPEC_URL
        ref_b = str(args.get("spec_b") or live_default)

        doc_a, problem = _read_spec(ref_a, ctx.repo_root)
        if problem:
            return err(f"Could not load spec_a: {problem}")
        doc_b, problem = _read_spec(ref_b, ctx.repo_root)
        if problem:
            hint = (
                " The target app does not appear to be running. Start it with env_control.spin_up, "
                "or pass spec_b as a file path to compare two documents on disk."
                if _is_url(ref_b) else ""
            )
            return err(f"Could not load spec_b: {problem}.{hint}")

        assert doc_a is not None and doc_b is not None
        try:
            changes = diff_specs(doc_a, doc_b)
        except RecursionError:
            return err("The specs contain a recursion this differ cannot follow; compare a subset of paths.")

        if args.get("only_breaking"):
            changes = [c for c in changes if c["breaking"]]
        breaking = [c for c in changes if c["breaking"]]

        if not changes:
            return ok(f"No semantic differences between {ref_a} and {ref_b}.", changes=[], breaking_count=0)

        lines = [f"{'BREAKING' if c['breaking'] else 'compatible'}  {c['method']} {c['path']}  [{c['kind']}] {c['detail']}"
                 for c in changes[:120]]
        more = f"\n(+{len(changes) - 120} more)" if len(changes) > 120 else ""
        return ok(
            f"{len(changes)} change(s), {len(breaking)} breaking, comparing {ref_a} (reference) "
            f"against {ref_b}:\n" + "\n".join(lines) + more,
            changes=changes, breaking_count=len(breaking), spec_a=ref_a, spec_b=ref_b,
        )

    @tool(
        "classify_breaking",
        "Classify one change as breaking, non_breaking or unknown, with the consumer-impact reason. "
        "Pass a change object from diff_openapi.",
        {
            "type": "object",
            "required": ["change"],
            "properties": {
                "change": {
                    "type": "object",
                    "description": "A change from diff_openapi: {kind, path, method, detail}.",
                    "properties": {
                        "kind": {"type": "string"},
                        "path": {"type": "string"},
                        "method": {"type": "string"},
                        "detail": {"type": "string"},
                    },
                }
            },
        },
    )
    async def classify_breaking(args: dict[str, Any]) -> dict[str, Any]:
        change = args.get("change")
        if not isinstance(change, dict):
            change = {k: v for k, v in args.items() if k != "change"}
        kind = str(change.get("kind") or "").strip()
        if not kind:
            return err(
                "The change has no 'kind'. Pass a change object exactly as diff_openapi returned it; "
                f"recognised kinds are: {', '.join(sorted(RULES))}."
            )
        verdict, reason = RULES.get(kind, (
            "unknown",
            f"'{kind}' is not a kind this server emits, so its consumer impact cannot be decided here. "
            "Judge it by hand, or re-run diff_openapi and classify one of its changes.",
        ))
        where = f"{change.get('method', '')} {change.get('path', '')}".strip()
        return ok(
            f"{kind}{f' on {where}' if where else ''}: {verdict}. {reason}",
            verdict=verdict, reason=reason, kind=kind, breaking=verdict == "breaking",
        )

    @tool(
        "find_consumers",
        "Find likely call sites of an endpoint in this repository (notably target-app/web/src). "
        "Heuristic: it matches the literal path string and the stable prefix before any {param}, "
        "so a client that assembles its URL from fragments will be missed and an unrelated string "
        "that happens to contain the path will be reported.",
        {
            "type": "object",
            "required": ["endpoint"],
            "properties": {
                "endpoint": {"type": "string", "description": "'GET /v1/orders' or just '/v1/orders'."},
                "root": {"type": "string", "description": "Subdirectory to search, repo-relative. Default: the whole repo."},
            },
        },
    )
    async def find_consumers(args: dict[str, Any]) -> dict[str, Any]:
        _, path = _split_endpoint(str(args["endpoint"]))
        if not path.startswith("/"):
            return err(f"'{args['endpoint']}' does not name a path. Use 'GET /v1/orders' or '/v1/orders'.")

        root = ctx.repo_root
        if args.get("root"):
            candidate = (ctx.repo_root / str(args["root"])).resolve()
            if not candidate.is_relative_to(ctx.repo_root.resolve()):
                return err("root must stay inside the repository.")
            if not candidate.exists():
                return err(f"No such directory: {candidate}.")
            root = candidate

        terms = _search_terms(path)
        hits: list[dict[str, Any]] = []
        for file in _walk_sources(root):
            try:
                text = file.read_text(errors="replace")
            except OSError:
                continue
            if not any(term in text for term in terms):
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if any(term in line for term in terms):
                    hits.append({
                        "file": str(file.relative_to(ctx.repo_root)) if file.is_relative_to(ctx.repo_root) else str(file),
                        "line": number,
                        "text": line.strip()[:200],
                    })
                    if len(hits) >= MAX_CONSUMER_HITS:
                        break
            if len(hits) >= MAX_CONSUMER_HITS:
                break

        if not hits:
            return ok(
                f"No call sites matched {path} under {root}. That is weak evidence: a client that builds "
                "the URL from fragments would not match. Check by hand before claiming no consumers exist.",
                consumers=[], searched=str(root), terms=terms,
            )
        listing = "\n".join(f"{h['file']}:{h['line']}  {h['text']}" for h in hits)
        return ok(f"{len(hits)} possible call site(s) for {path} (heuristic):\n{listing}",
                  consumers=hits, searched=str(root), terms=terms)

    @tool(
        "generate_contract_test",
        "Emit a runnable pytest module asserting the spec's contract for one endpoint: status code, "
        "required response fields, and the auth requirement. Written under .qaas/generated/ and stored "
        "as an artifact so the envelope can cite it. Run it before filing — a contract test that does "
        "not fail against the current implementation is not evidence.",
        {
            "type": "object",
            "required": ["endpoint", "method"],
            "properties": {
                "endpoint": {"type": "string", "description": "Spec path, e.g. '/v1/invoices'."},
                "method": {"type": "string", "description": "HTTP method, e.g. 'GET'."},
                "expectation": {"type": "string", "description": "Why you are generating it — what you expect to break. Recorded in the test's docstring."},
                "spec": {"type": "string", "description": "Spec to read the contract from. Default: target-app/openapi.yaml."},
                "expected_status": {"type": "integer", "description": "Override the success status. Default: the first 2xx in the spec."},
            },
        },
    )
    async def generate_contract_test(args: dict[str, Any]) -> dict[str, Any]:
        ref = str(args.get("spec") or default_spec)
        doc, problem = _read_spec(ref, ctx.repo_root)
        if problem:
            return err(f"Could not load the spec: {problem}")
        assert doc is not None

        _, path = _split_endpoint(str(args["endpoint"]))
        method = str(args["method"]).upper()
        if "{" in path:
            return err(
                f"{path} has path parameters this generator cannot fill in — it would have to invent "
                "an id, and a test that 404s proves nothing. Generate a test for a collection endpoint, "
                "or write the parameterised case by hand."
            )
        ops = _operations(doc)
        op = ops.get((path, method))
        if op is None:
            known = ", ".join(f"{m} {p}" for p, m in sorted(ops)[:25])
            return err(f"{method} {path} is not in {ref}. It declares: {known}")

        responses = {str(k): v for k, v in (op.get("responses") or {}).items()}
        override = args.get("expected_status")
        if override is not None:
            expected = int(override)
            if str(expected) not in responses:
                return err(f"{method} {path} does not declare status {expected}; it declares {', '.join(sorted(responses))}.")
        else:
            success = sorted(c for c in responses if c.isdigit() and 200 <= int(c) < 300)
            if not success:
                return err(f"{method} {path} declares no 2xx response, so there is no success contract to assert.")
            expected = int(success[0])

        required_top, required_item = _required_fields(_json_schema(responses[str(expected)], doc), doc)
        source = TEST_TEMPLATE.format(
            method=method,
            path=path,
            spec_name=Path(ref).name if not _is_url(ref) else ref,
            # The expectation is free text from an agent and lands inside a
            # docstring, so a stray triple quote would produce a file that will
            # not import. Neutralise it rather than reject the call.
            why=str(args.get("expectation") or "Asserts the published contract for this endpoint.").strip().replace('"""', "'''").replace("\\", "/"),
            default_base=os.environ.get("QAAS_TARGET_BASE_URL", "http://localhost:8000").rstrip("/"),
            expected_status=expected,
            required_top=required_top,
            required_item=required_item,
            auth_required=_requires_auth(op, doc),
        )

        out_dir = ctx.store.root / GENERATED_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        filename = f"test_contract_{_identifier(method, path)}.py"
        target = out_dir / filename
        target.write_text(source)
        uri = ctx.store.put_artifact(filename, source)
        ctx.store.log("contract_test", agent=ctx.agent.name, endpoint=f"{method} {path}", path=str(target))

        return ok(
            f"Wrote {target}. It asserts HTTP {expected}"
            + (f", required response fields {', '.join(required_top)}" if required_top else "")
            + (f", required item fields {', '.join(required_item)}" if required_item else "")
            + (", and that anonymous calls get 401" if _requires_auth(op, doc) else "")
            + f". Run it with QAAS_TARGET_BASE_URL set, then cite {uri} as evidence.\n\n{source}",
            path=str(target), uri=uri, source=source, expected_status=expected,
            required_fields=required_top, required_item_fields=required_item,
        )

    return [diff_openapi, classify_breaking, find_consumers, generate_contract_test]


def build(ctx: ToolContext):
    """Construct the contract_diff MCP server bound to one agent's run context."""
    return create_sdk_mcp_server(name="contract_diff", version="1.0.0", tools=build_tools(ctx))
