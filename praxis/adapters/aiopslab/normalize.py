"""AIOpsLab trace adapter: ``session.history`` -> ``list[TraceEvent]``.

This is the ONLY place in praxis that knows anything about AIOpsLab's trace
shape — but it must never import ``aiopslab`` (or pydantic): AIOpsLab is not
installable alongside this package, so the adapter *duck-types* the trace.
A trace item is either

- an object with ``.role`` / ``.content`` string attributes (AIOpsLab's live
  Pydantic ``SessionItem`` — exactly two fields, roles
  ``system|user|assistant|env``; confirmed in docs/NOTES.md Q1), or
- a mapping with ``"role"`` / ``"content"`` keys (the ``to_dict()`` /
  fixture form).

This module is an untrusted-input boundary: malformed *items* (missing
role/content) raise :class:`TraceFormatError` with the offending index, but a
malformed *assistant turn* (no fenced block, several blocks, unparseable
call) never raises — it normalizes to a non-action event, mirroring how the
harness itself survives such turns.

The assistant-turn mini-parser below is a behavioral clone of AIOpsLab's
``ResponseParser`` (``aiopslab/orchestrator/parser.py``), which is not
importable here. Its documented contract (docs/NOTES.md Q4): find exactly one
```-fenced block, special-case ``exec_shell`` command extraction, otherwise
``ast``-parse a single call expression. Where the real parser raises
``ResponseParsingError``, this clone yields a non-action event.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping, Sequence
from typing import Any

from praxis.core.model import AccessType, TraceEvent, classify_shell_command

__all__ = [
    "READ_ACTIONS",
    "SUBMIT_ACTIONS",
    "TraceFormatError",
    "to_events",
]


class TraceFormatError(ValueError):
    """A trace item is structurally malformed (missing role/content)."""


# Confirmed @read action set from AIOpsLab recon (docs/NOTES.md Q2): the
# telemetry APIs are the only @read-decorated actions, and there are NO
# @write actions in the harness — every mutation flows through exec_shell.
# An in-tree AIOpsLab integration would build this map dynamically via
# ``aiopslab.utils.actions.get_actions()`` instead of hardcoding it; the
# hardcoded set exists only because aiopslab is not importable here.
READ_ACTIONS: frozenset[str] = frozenset(
    {"get_logs", "get_metrics", "get_traces", "read_metrics", "read_traces"}
)

# ``submit`` is the single terminal action in every AIOpsLab task.
SUBMIT_ACTIONS: frozenset[str] = frozenset({"submit"})

# kwargs consulted (in priority order) when deriving a best-effort resource
# name from an API call. Tunable heuristic, not part of any harness contract.
_RESOURCE_KWARGS: tuple[str, ...] = (
    "service",
    "deployment",
    "pod",
    "pod_name",
    "name",
    "app",
)

# Kubernetes kind keywords recognized by the shell-resource heuristic, both
# as ``kind name`` token pairs and as the prefix of ``kind/name`` tokens.
_KIND_TOKENS: frozenset[str] = frozenset(
    {
        "deployment",
        "deploy",
        "pod",
        "pods",
        "svc",
        "service",
        "statefulset",
        "sts",
        "daemonset",
        "node",
        "namespace",
        "ns",
        "replicaset",
        "rs",
    }
)

# Flags whose *next* token is a value, not a resource. The -n/--namespace
# value doubles as the last-resort resource fallback (see _shell_resource).
_NAMESPACE_FLAGS: frozenset[str] = frozenset({"-n", "--namespace"})

# Same fence contract as AIOpsLab's ResponseParser: one ```-fenced block,
# optional language tag on the opening fence.
_FENCE_RE = re.compile(r"```(?:[a-zA-Z0-9_]*\n)?(.*?)```", re.DOTALL)

_EXEC_SHELL_CALL_RE = re.compile(r"exec_shell\s*\(")

# exec_shell's command must be a single ("|')-quoted string, optionally
# passed as ``command=``. Backslash escapes are consumed pairwise so an
# escaped closing quote does not end the match.
_EXEC_SHELL_COMMAND_RE = re.compile(
    r"exec_shell\s*\(\s*(?:command\s*=\s*)?"
    r"(?P<quote>[\"'])(?P<command>(?:\\.|(?!(?P=quote))[^\\])*)(?P=quote)"
    r"\s*\)",
    re.DOTALL,
)

# Unescape backslash-escaped quotes and backslashes (only those — the real
# parser does not interpret \n and friends).
_UNESCAPE_RE = re.compile(r"\\([\"'\\])")

_MISSING = object()


def to_events(trace: Sequence[Any]) -> list[TraceEvent]:
    """Normalize an AIOpsLab trace into one :class:`TraceEvent` per turn.

    Accepts both live ``SessionItem``-like objects and ``{"role","content"}``
    mappings, in any mix. Emits exactly one event per input turn, with
    ``event.index`` equal to the turn's position in ``trace`` — findings
    downstream point at these positions, so the 1:1 mapping is load-bearing.

    Raises:
        TraceFormatError: if an item lacks a role or content (the only
            structural requirement; everything else degrades gracefully).
    """
    events: list[TraceEvent] = []
    for index, item in enumerate(trace):
        role, content = _role_and_content(item, index)
        if role != "assistant":
            # system / user / env (or anything unexpected): carry the raw
            # string for token-matching and judge context; nothing to parse.
            events.append(TraceEvent(index=index, role=role, raw=content))
            continue
        parsed = _parse_assistant_content(content)
        if parsed is None:
            # Pure-reasoning or malformed assistant turn: a non-action event.
            events.append(TraceEvent(index=index, role=role, raw=content))
            continue
        api_name, args, kwargs = parsed
        events.append(
            TraceEvent(
                index=index,
                role=role,
                api_name=api_name,
                args=args,
                kwargs=kwargs,
                access=_classify_access(api_name, args),
                raw=content,
                resource=_extract_resource(api_name, args, kwargs),
            )
        )
    return events


def _role_and_content(item: Any, index: int) -> tuple[str, str]:
    """Duck-type one trace item into ``(role, content)`` strings.

    Mappings are checked for the two keys; any other object for the two
    attributes. Content (and role) are coerced via ``str()`` — the harness
    guarantees strings, but fixtures and future producers may not.
    """
    if isinstance(item, Mapping):
        if "role" not in item or "content" not in item:
            raise TraceFormatError(
                f"trace item {index}: mapping is missing a 'role' or "
                f"'content' key (got keys {sorted(map(str, item.keys()))})"
            )
        return str(item["role"]), str(item["content"])
    role = getattr(item, "role", _MISSING)
    content = getattr(item, "content", _MISSING)
    if role is _MISSING or content is _MISSING:
        raise TraceFormatError(
            f"trace item {index}: object of type "
            f"{type(item).__name__!r} lacks a 'role' or 'content' attribute"
        )
    return str(role), str(content)


def _parse_assistant_content(
    content: str,
) -> tuple[str, list[Any], dict[str, Any]] | None:
    """Extract ``(api_name, args, kwargs)`` from an assistant turn.

    Behavioral clone of AIOpsLab ``ResponseParser.parse`` (docs/NOTES.md Q4),
    with one deliberate difference: wherever the real parser raises
    ``ResponseParsingError`` (block count != 1, unquoted exec_shell command,
    unparseable call), this returns ``None`` so the turn normalizes to a
    non-action event instead of crashing the verifier.
    """
    blocks = _FENCE_RE.findall(content)
    if len(blocks) != 1:
        return None
    code = blocks[0].strip()
    if _EXEC_SHELL_CALL_RE.search(code):
        match = _EXEC_SHELL_COMMAND_RE.search(code)
        if match is None:
            return None
        command = _UNESCAPE_RE.sub(r"\1", match.group("command"))
        return "exec_shell", [command], {}
    try:
        module = ast.parse(code)
    except (SyntaxError, ValueError):
        return None
    if len(module.body) != 1 or not isinstance(module.body[0], ast.Expr):
        return None
    call = module.body[0].value
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
        return None
    args = [_eval_node(node) for node in call.args]
    kwargs = {
        keyword.arg: _eval_node(keyword.value)
        for keyword in call.keywords
        if keyword.arg is not None  # skip **splats
    }
    return call.func.id, args, kwargs


def _eval_node(node: ast.expr) -> Any:
    """Evaluate an argument node as a literal, else fall back to its source."""
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return ast.unparse(node)


def _classify_access(api_name: str, args: list[Any]) -> AccessType:
    """Map an api call to an :class:`AccessType`.

    READ/SUBMIT come from the confirmed decorator sets above. WRITE can only
    arise from ``exec_shell`` command inspection (there are no @write actions
    in the harness — docs/NOTES.md Q2), via the shared
    ``classify_shell_command`` so adapter and policies can never disagree.
    """
    if api_name in READ_ACTIONS:
        return AccessType.READ
    if api_name in SUBMIT_ACTIONS:
        return AccessType.SUBMIT
    if api_name == "exec_shell":
        if args and isinstance(args[0], str) and args[0]:
            return classify_shell_command(args[0])
        return AccessType.UNKNOWN
    return AccessType.UNKNOWN


def _extract_resource(
    api_name: str, args: list[Any], kwargs: dict[str, Any]
) -> str | None:
    """Best-effort resource target for an action; ``None`` when underivable."""
    if api_name == "exec_shell":
        if args and isinstance(args[0], str) and args[0]:
            return _shell_resource(args[0])
        return None
    return _api_resource(api_name, args, kwargs)


def _api_resource(api_name: str, args: list[Any], kwargs: dict[str, Any]) -> str | None:
    """Resource heuristic for non-shell API calls (tunable, best-effort).

    Priority: the first string-valued kwarg from ``_RESOURCE_KWARGS``; else
    ``args[1]`` when the first two positionals are strings (AIOpsLab's
    telemetry convention is ``(namespace, service)``); else a single string
    positional on a known READ/SUBMIT action; else ``None``.
    """
    for key in _RESOURCE_KWARGS:
        value = kwargs.get(key)
        if isinstance(value, str) and value:
            return value
    if len(args) >= 2 and isinstance(args[0], str) and isinstance(args[1], str):
        return args[1]
    if (
        len(args) == 1
        and isinstance(args[0], str)
        and api_name in (READ_ACTIONS | SUBMIT_ACTIONS)
    ):
        return args[0]
    return None


def _shell_resource(command: str) -> str | None:
    """Resource heuristic for kubectl-style shell commands (tunable).

    Scans whitespace tokens left to right for the first of:

    - a ``kind/name`` token whose kind prefix (before an optional API-group
      suffix, e.g. ``deployment.apps``) is a known kind → the name part;
    - a known kind keyword followed by a non-flag token → that next token.

    Flag tokens (leading ``-``) are never resource names. ``-n``/
    ``--namespace`` consume their following token as the namespace, which is
    used only as a last-resort fallback when no resource name was found —
    so ``kubectl get pods -n test`` resolves to ``"test"`` (documented
    decision: a namespace-scoped identity beats ``None`` for same-resource
    read-before-write matching). Non-kubectl commands (``echo hello``)
    resolve to ``None``.
    """
    tokens = command.split()
    namespace_fallback: str | None = None
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.startswith("-"):
            if token in _NAMESPACE_FLAGS and i + 1 < len(tokens):
                namespace_fallback = tokens[i + 1]
                i += 2
                continue
            if token.startswith("--namespace="):
                namespace_fallback = token.split("=", 1)[1] or None
            i += 1
            continue
        if "/" in token:
            kind, _, name = token.partition("/")
            if kind.split(".", 1)[0].lower() in _KIND_TOKENS and name:
                return name
            i += 1
            continue
        if token.lower() in _KIND_TOKENS:
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                return tokens[i + 1]
        i += 1
    return namespace_fallback
