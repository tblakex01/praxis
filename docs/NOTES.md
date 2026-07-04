# NOTES.md — Trajectory Verifier lessons & confirmed facts

One lesson per entry. One-line summary on the top line, detail below. Record corrections, confirmed AIOpsLab interface facts, and decisions made under uncertainty (with the why). Check this file before re-investigating anything.

> **M1 recon complete.** All five questions confirmed against source at `github.com/microsoft/AIOpsLab` (`main`). Several answers differ from the spec's stated assumptions — see "Design deltas forced by recon" at the bottom. Treat this file, not the spec's Section 1 assumptions, as ground truth for interfaces.

---

## M1 recon — CONFIRMED

### Q1. Serialized trace turn schema — CONFIRMED
**A trace item is a Pydantic `SessionItem` with exactly two fields: `role: str` and `content: str`. Nothing else. No timestamp, no structured tool metadata.**

- Source: `aiopslab/session.py`
- `class SessionItem(BaseModel): role: str  # system / user / assistant` ; `content: str`.
- `role` values seen in practice: `"system"`, `"user"`, `"assistant"`, and **`"env"`** (environment/tool results — added by the orchestrator, see Q5; the comment in the model only lists system/user/assistant but `"env"` is the real tool-result role).
- `Session.history: list[SessionItem]` is the live object. `Session.to_dict()["trace"]` is `[item.model_dump() for item in history]` → list of `{"role","content"}` dicts. `service.py` additionally *prepends* two synthetic turns (`system`, `user`) and pops a trailing `env` turn when serializing for `/simulate` — so the API trace and the in-process `history` are **not identical**. Normalize against `history` (what `eval` gets), and treat `to_dict()` shape as a secondary input.
- **Implication:** `normalize.py` must accept `list[SessionItem]` (Pydantic objects) as its primary input, and *may* also accept the `list[dict]` form for fixtures/portability. Do not assume JSON dicts at the `eval` boundary.

### Q2. Read/write discoverability on actions — CONFIRMED (with a sharp edge)
**Decorators live in `aiopslab/utils/actions.py`. They set attributes on the function: `@action` → `is_action=True`; `@read` → `is_action=True, action_type="read"`; `@write` → `is_action=True, action_type="write"`. Classification is discoverable directly off the callable via `getattr(fn, "action_type", None)`.**

- Introspection helper: `get_actions(task, subtype=None)` in the same file — imports `aiopslab.orchestrator.actions.<task>`, grabs `<Task>Actions`, and returns `{name: docstring}` filtered by `is_action`, optionally narrowed to an `action_type` subtype. Use this to build the name→AccessType map instead of hardcoding.
- **SHARP EDGE — the whole verifier premise needs adjusting:** In `actions/base.py`, the read-only telemetry APIs (`get_logs`, `get_metrics`, `get_traces`, `read_metrics`, `read_traces`) are `@read`. **There are NO `@write` actions in the base action set.** The only mutation-capable API is `exec_shell`, which is decorated plain `@action` (so `action_type` is absent → classifies as UNKNOWN), and it is where *all* state change happens (kubectl patch/scale/delete/rollout, etc.). It has only a tiny `BLOCK_LIST` (`kubectl edit`, `port-forward`, `-f` follows).
- **Implication:** "did the agent do a write" cannot be read off `action_type` alone — the decisive signal is **the command string inside `exec_shell`**. `AccessType.WRITE` for the verifier must be derived by inspecting `exec_shell` command verbs (patch/scale/delete/drain/cordon/rollout/apply/restart/rm/kill), not by decorator. The `ShellSafetyPolicy` therefore becomes the *primary* write-detector, not a secondary one. `ReadBeforeWritePolicy` must classify writes the same way.

### Q3. `eval` signature & result-recording contract — CONFIRMED
**`Task` base (`aiopslab/orchestrator/tasks/base.py`): `add_result(self, key, value)` does `self.results[key] = value` (last-write-wins, no type guard). Concrete tasks implement `eval(self, soln, trace, duration)` and call `self.common_eval(trace)`.**

- **Better integration hook than the spec assumed:** `common_eval(self, trace: list[SessionItem])` is already called by every task's `eval` and already records `steps`, `in_tokens`, `out_tokens`, and (if `config.qualitative_eval`) an LLM `reasoning_score`. **This is the natural seam** — the verifier should hook here (or be called adjacent to it), receiving the same `trace: list[SessionItem]`. Prefer extending/wrapping `common_eval` over editing each task's `eval`.
- Result values must be JSON-safe (session is written via `json.dump` in `to_json`). Keep `to_result_dict()` outputs to scalars / lists / strings. No enums, no dataclasses in the results dict — serialize `Severity` etc. to strings.
- `add_result` is last-write-wins on key collision → namespace all verifier keys with a `trajectory_` prefix to avoid clobbering `steps`/`reasoning_score`/`TTD`/etc.

### Q4. ResponseParser reuse — CONFIRMED (reusable, with a catch)
**`aiopslab/orchestrator/parser.py` `ResponseParser().parse(response) -> {"api_name","args","kwargs","context"}`. Importable and directly callable. Uses regex to find the ```-fenced block and `ast.parse` for args.**

- Catch 1: `parse()` first calls `validate()`, which **raises `ResponseParsingError` if there is not exactly one ```-fenced block** in the turn. Agent turns that are pure reasoning with no action, or malformed, will raise — so wrap per-turn parsing in try/except and treat a raise as "non-action turn" (emit no event, or a NULL event), never let it crash the verifier.
- Catch 2: `exec_shell` is special-cased (strips `command=`, requires a quoted string, unescapes) — reuse the parser rather than re-implementing this.
- The parser only runs on **assistant** turns. `env` turns are raw result strings and are not parsed.

### Q5. Error/failure signal in env turns — CONFIRMED (must be token-matched)
**There is NO structured error field. Env/tool results are plain strings appended by the orchestrator as `{"role": "env", "content": <string>}`. Errors are indistinguishable from success except by their text.**

- Source: `aiopslab/orchestrator/orchestrator.py` `ask_env()`. Failure paths all stringify: `ResponseParsingError` → `str(e)`; `InvalidActionError` → `str(e)`; any other exception → `str(e)`; and action methods themselves return error *strings* (e.g., base `get_logs` returns `"Error: Your service/namespace does not exist..."`). There is a vestigial `if hasattr(env_response, "error")` branch but it also just stringifies.
- **Implication:** `RepeatedFailureLoopPolicy` (and any error detection) must **token-match on the `content` of `env` turns** — anchor on `"Error"`, `"error"`, `"Traceback"`, `"does not exist"`, `"Format validation failure"`, `"Unhandled exception"`, `"No API call found"`. There is no clean boolean to key on. Record the exact anchor set used and treat it as tunable.
- Loop detection: pair each `env` error with the immediately preceding `assistant` turn's parsed `api_name` (Q4) to detect "same failing call repeated N times."

---

## Design deltas forced by recon (update the build against these)

1. **Trace type at the boundary is `list[SessionItem]` (Pydantic), not `list[dict]`.** `normalize.py`: accept `SessionItem` primarily; also accept `{"role","content"}` dicts for fixtures. Two-field schema only — do not depend on timestamps (there are none) or structured tool metadata (there is none).
2. **Write detection is command-string-based, not decorator-based.** The action layer has no `@write` methods; `exec_shell` (`@action`, so UNKNOWN by decorator) carries every mutation. `ShellSafetyPolicy` is promoted to the primary write detector; `ReadBeforeWritePolicy` classifies writes by parsing `exec_shell` verbs, not `action_type`. Keep `AccessType.WRITE` inferred from command verbs; reserve decorator-derived `READ` for the telemetry APIs and `SUBMIT` for `submit`.
3. **Integration seam is `common_eval(trace)`, not per-task `eval`.** Hook the verifier there (or immediately adjacent) so all four task types get it for free with one wiring point. Still demonstrate on one localization + one mitigation problem per the spec, but the wiring lands in/around `common_eval`.
4. **Errors are token-matched strings.** No structured error object anywhere. `RepeatedFailureLoopPolicy` and any failure signal rely on a tunable anchor-token set over `env` turn content.
5. **Reuse `ResponseParser` but guard it.** `parse()` raises on any turn without exactly one fenced code block; wrap per-turn and treat raises as non-action turns. Reuse its `exec_shell` special-casing rather than re-implementing.
6. **Results must be JSON-safe and `trajectory_`-prefixed.** `add_result` is last-write-wins with no type guard and the session is `json.dump`-ed; serialize enums to strings and namespace keys to avoid clobbering existing metrics.
7. **`get_actions()` builds the name→AccessType map.** Use the existing helper for the READ/SUBMIT classification instead of hardcoding action names; layer the command-verb WRITE inference on top for `exec_shell`.

---

## Confirmed interface facts (quick reference)

- Trace item: `SessionItem(role: str, content: str)`; roles: `system|user|assistant|env`.
- `eval(self, soln, trace, duration)`; `trace` = `session.history` = `list[SessionItem]`.
- `common_eval(self, trace)` records `steps`, `in_tokens`, `out_tokens`, optional `reasoning_score` — the hook point.
- `add_result(key, value)` → `self.results[key] = value`, JSON-serialized later.
- Decorators (`utils/actions.py`): `is_action` + `action_type in {"read","write"}` (absent on plain `@action`).
- Only mutation path in base actions = `exec_shell` (decorated `@action`, BLOCK_LIST guards only edit/port-forward/-f).
- `ResponseParser().parse()` → `{api_name, args, kwargs, context}`; raises `ResponseParsingError` if != 1 fenced block.
- Env errors = plain strings; no structured error field.

---

## Decisions made under uncertainty

- **`exec_shell` classified as UNKNOWN-by-decorator, WRITE-by-command-inspection.** Chosen because decorator metadata genuinely cannot distinguish a read `kubectl get` from a write `kubectl patch` inside the same `@action` function. Alternative (treat all `exec_shell` as WRITE) rejected: too many false positives on diagnostic `kubectl get`/`describe`. Verb-denylist inference is the least-bad signal available.
- **Hook `common_eval` rather than each `eval`.** Chosen for one-point wiring across all task types; the spec's per-`eval` snippet still works but duplicates wiring four times.

---

## Corrections & gotchas discovered during the build

- (M1) Spec Section 1 assumed the trace might be serialized dicts with a possible timestamp field — **wrong on both counts** at the `eval` boundary: it's Pydantic `SessionItem` objects, two fields only. Corrected in delta #1.
- (M1) Spec assumed `@read`/`@write` would cleanly tag every action — **there are no `@write` actions**; mutation hides inside `exec_shell`. Corrected in delta #2. This is the single most important recon finding.
- (M1) `service.py`'s `/simulate` trace ≠ in-process `history` (it prepends 2 turns and pops a trailing env turn). Fixtures modeled on API output will be subtly off; model fixtures on `history`.

---

## M2–M5 build record (standalone `praxis` repo)

One entry per decision made while implementing the spec in this repository. The single structural adaptation: the spec's Section 4 layout (`aiopslab/orchestrator/verifier/`) was written for an in-tree AIOpsLab drop-in; this repo is the standalone re-homing the README locks in (`praxis/core/` portable engine + `praxis/adapters/aiopslab/`), so AIOpsLab is duck-typed at the adapter boundary, never imported. Spec semantics are otherwise unchanged.

### Adapter (normalize.py)
- **`READ_ACTIONS`/`SUBMIT_ACTIONS` are hardcoded adapter constants** (`get_logs`, `get_metrics`, `get_traces`, `read_metrics`, `read_traces` / `submit`), taken from the M1 recon of `@read` decorators. An in-tree AIOpsLab integration would build this map via `aiopslab.utils.actions.get_actions()` instead (delta #7); standalone, the recon set is the ground truth available.
- **The fenced-block parser is a behavioral clone of `ResponseParser`**, not a reuse — the real one is not importable here. It replicates the documented contract (exactly one ```-fenced block or the turn is non-action; `exec_shell` special-case strips `command=`, requires a quoted string, unescapes quotes/backslashes only). Any parse failure yields a non-action event, never an exception.
- **`to_events(trace)` takes no `task_type` param** (the spec's mixin sketch passed one); task type flows through the verify `context` instead — normalization doesn't depend on it.
- **One `TraceEvent` per input turn, `index` = trace position** — including system/user/env and unparseable assistant turns — so `event_indices` in findings always map 1:1 to real trace turns.
- **Resource heuristics:** kwargs priority `service`>`deployment`>`pod`>`pod_name`>`name`>`app`; two positional strings → `args[1]` (AIOpsLab `(namespace, service)` convention). For kubectl commands, `kind/name` tokens require a known kind prefix (avoids matching file paths); namespace (`-n`/`--namespace`) is a last-resort fallback when no named resource is found (`kubectl get pods -n test` → `"test"`), chosen because a namespace-scoped identity beats `None` for same-resource read-before-write matching. All documented as tunable.

### Policies (rules.py)
- **Multiple submits double-flag by design:** a second `submit` is both a multiple-submit WARN and an action-after-submit VIOLATION (it *is* an action after the first submit). Implemented literally per spec section 6 item 6.
- **Zero-submit WARN points at the last action event's real index** — no triggering event exists and fabricating indices is forbidden.
- **Failure-loop pairing:** an attempt's response is the first `env` event after the assistant turn and before the next assistant turn; an unanswered assistant turn neither extends nor breaks a streak; a non-error response or a different-api attempt breaks it. Unparseable turns (api `None`) can form their own streak (the "No API call found" loop).
- **Ordering checks use sequence position** (`events[:i]`), not `.index` arithmetic; `.index` appears only in emitted findings.

### Engine
- **Two-pass dedupe:** exact duplicates (`policy, severity, message, indices`) are removed from the findings list; scoring additionally dedupes by (`severity, sorted indices`) so the deliberate ShellSafety/ReadOnlyTask overlap (defense in depth) never double-penalizes one event. Findings are never dropped by the scoring pass — "dedupe identical findings in the engine, don't suppress the policy."

### Judge (judge.py)
- **`justified: true` wins over a contradictory non-empty `unjustified_steps`** (boolean is authoritative) → INFO "path justified". A `false` verdict whose cited indices are all invalid degrades to a WARN with empty indices carrying the dropped indices in evidence — the negative signal survives without fabricated indices.
- **Judge emits an INFO finding in every terminal state** (disabled / no client / justified) for auditability at zero score weight.
- **Strict verdict types** — no coercion of `"4"` to 4; malformed output is a WARN "judge output unparseable", client exceptions a WARN "judge call failed". Default model `claude-sonnet-5`, `max_tokens=2048` (adaptive thinking shares the output budget).

### Mixin + demo wiring
- **`task_desc` is read `getattr`-defensively** and omitted from context when absent — the attribute name is UNVERIFIED against AIOpsLab source (M1 didn't cover it); flagged assumption, cheap to correct in-tree.
- **`trajectory_policies` uses an `is None` check**, so an injected empty list is respected rather than silently replaced by `default_policies()`.
- **The spec's "two demo problems" (M4) are adapted** to stub Localization/Mitigation tasks in `tests/test_mixin.py` that replicate the confirmed `common_eval`/`add_result` contract — AIOpsLab problems don't exist in this repo. The acceptance diff (criterion 11.2) is proven there: stripping `trajectory_` keys from mixed-in results reproduces the plain task's results dict exactly.

### Review-driven changes
- **`classify_shell_command` matches case-insensitively** (sourcery-ai suggestion, accepted): production traces can come from case-insensitive shells (e.g. Windows), and `ShellSafetyPolicy` inherits the fix automatically since it delegates to the shared helper.
