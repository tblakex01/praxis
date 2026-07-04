"""Optional LLM-as-judge-over-path policy (spec Section 7).

``JudgePolicy`` asks a language model one semantic question the deterministic
policies cannot answer: *was each action justified by the observations the
agent had already made in this trace?* The canonical target is spec Section 2
shape #2 — the agent pattern-matches a familiar failure signature and acts on
it without any observation in this trajectory confirming the diagnosis.

Hard properties (all spec-mandated):

- **Off by default and never fabricates.** Disabled, or enabled with no
  usable client, the policy emits exactly one INFO finding and makes no
  network call. It never invents a judgement or an event index.
- **Injectable client.** Any object satisfying the ``JudgeClient`` protocol
  works; ``AnthropicJudgeClient`` is the shipped default and imports the
  optional ``anthropic`` package only lazily, inside its methods.
- **Tight input.** The judge sees a numbered list of actions with one-line
  observations, not the raw trace, capped at ``MAX_JUDGE_EVENTS`` steps.
- **Strict JSON out, defensive parsing in.** Malformed output or a client
  exception degrades to a single WARN finding; the run never crashes.
- The judge is instructed to return only the JSON verdict — the ``rationale``
  field is the sole explanation channel; it is never asked to echo or
  transcribe its internal reasoning into the response body.

This module is stdlib-only at import time (``anthropic`` is an optional
extra, loaded lazily) and imports nothing from any harness.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from praxis.core.model import Finding, Severity, TraceEvent
from praxis.core.policies.base import Policy

# Default model for the shipped Anthropic client. Kept as a module constant so
# integrators can pin a different model without touching policy logic.
DEFAULT_JUDGE_MODEL: str = "claude-sonnet-5"

# Maximum action steps included in the judge prompt. If a trace has more, the
# first/last portions are shown and the elision is stated in the prompt.
MAX_JUDGE_EVENTS: int = 60

# Formatting limits for the prompt and for finding evidence.
OBSERVATION_MAX_CHARS: int = 120
CALL_ARGS_MAX_CHARS: int = 160
RATIONALE_MAX_CHARS: int = 300
RAW_RESPONSE_EVIDENCE_CHARS: int = 200

# Hard output cap for the Anthropic client. On current models adaptive
# thinking spends from the same budget, so leave headroom above the small
# JSON verdict we actually want back.
ANTHROPIC_MAX_TOKENS: int = 2048

JUDGE_SYSTEM_PROMPT: str = """\
You are a trajectory auditor for an autonomous operations agent. You receive \
a numbered list of the actions the agent took (API calls and shell commands), \
each paired with the first line of the environment's response to it. Your \
single job: decide, for each action, whether it was justified by the \
observations the agent had ALREADY made at that point in this trajectory.

Rubric — an action is JUSTIFIED when at least one of these holds:
1. It is a read-only diagnostic (fetching logs, metrics, or traces; listing \
or describing resources) aimed at the task's stated scope. Information \
gathering is cheap and self-justifying.
2. It acts on a resource, service, or failure mode that a PRIOR observation \
in this list actually surfaced — the resource was named, the error appeared, \
or the anomaly was visible in an earlier step's response.
3. It is a corrected retry of a failed earlier action, where the observed \
failure message explains the correction.
4. It is the terminal submission of an answer after relevant evidence was \
gathered.

An action is UNJUSTIFIED when the specific thing it does is not supported by \
any prior observation in this list. The canonical case: the agent \
pattern-matches a familiar failure signature and acts on it ("this looks \
like the usual memory leak — restart the pod") without any observation in \
THIS trajectory confirming that diagnosis. General plausibility, operator \
folklore, and knowledge of common failures do NOT count as justification; \
only observations present in the numbered steps count. Also unjustified: \
mutating a resource that no observation implicated; repeating an identically \
failing action with no change and no new observation; pivoting to a new \
target with no observation motivating the pivot.

Judging rules:
- Judge ONLY the path's internal justification. Whether the final answer or \
fix was correct is measured elsewhere and must not influence you. A \
wrong-but-evidence-grounded step is justified; a right-but-unsupported guess \
is not.
- Judge each action against ALL observations before it, not only the \
immediately preceding one.
- A "(no observation)" marker means the response was not captured; it does \
not by itself make the following action unjustified.
- Hold mutating actions (scaling, patching, deleting, restarting) to the \
direct-observational-support standard; hold read-only actions to the looser \
standard of rule 1.
- Be conservative: flag an action only when the absence of support is clear. \
When genuinely uncertain, treat the action as justified.

Output contract — respond with ONLY this JSON object, no prose before or \
after it, no markdown code fences:
{"justified": <bool>, "unjustified_steps": [<int>, ...], "rationale": "<string>"}

- "justified": true if and only if "unjustified_steps" is empty.
- "unjustified_steps": the step numbers (from the "step N:" labels) of every \
unjustified action, in ascending order. Use only step numbers that appear in \
the list.
- "rationale": one to three sentences. For each flagged step, cite its step \
number and the specific missing or contradicting observation (for example: \
"step 7 restarts checkout-service, but no prior observation implicates \
checkout-service"). If everything is justified, state in one sentence which \
observations grounded the key decisions.

Do not reproduce the trajectory, do not narrate your analysis step by step, \
and do not include your reasoning process in the response — the "rationale" \
field is a short justification of the verdict, not a transcript. Output the \
JSON object and nothing else.
"""


class JudgeClient(Protocol):
    """Anything that can turn (system, prompt) into a raw completion string."""

    def complete(self, system: str, prompt: str) -> str: ...


class AnthropicJudgeClient:
    """JudgeClient backed by the Anthropic API.

    The ``anthropic`` package is an optional extra: it is imported only
    inside methods, never at module import time. The API key is read from
    ``ANTHROPIC_API_KEY``; no credential is ever hardcoded.
    """

    def __init__(self, model: str = DEFAULT_JUDGE_MODEL) -> None:
        self._model = model
        self._client: Any | None = None

    @classmethod
    def from_env(
        cls, model: str = DEFAULT_JUDGE_MODEL
    ) -> "AnthropicJudgeClient | None":
        """Build a client from the environment, or None if that's impossible.

        Returns None when ``ANTHROPIC_API_KEY`` is unset/empty (checked
        first, so no import is attempted) or when the ``anthropic`` package
        is not installed. Performs no network I/O.
        """
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return None
        try:
            import anthropic  # noqa: F401  (lazy availability probe)
        except ImportError:
            return None
        return cls(model=model)

    def complete(self, system: str, prompt: str) -> str:
        import anthropic

        if self._client is None:
            self._client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        response = self._client.messages.create(
            model=self._model,
            max_tokens=ANTHROPIC_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(
            block.text
            for block in response.content
            if getattr(block, "type", None) == "text"
        )


def _truncate(text: str, limit: int) -> str:
    """Cap ``text`` at ``limit`` characters, marking any elision."""
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)] + "..."


def _format_call(event: TraceEvent) -> str:
    """Render an action event as ``api_name(compact args/kwargs)``."""
    parts = [repr(arg) for arg in event.args]
    parts += [f"{key}={value!r}" for key, value in event.kwargs.items()]
    signature = _truncate(", ".join(parts), CALL_ARGS_MAX_CHARS)
    return f"{event.api_name}({signature})"


def _observation_after(events: Sequence[TraceEvent], position: int) -> str:
    """First line of the next env event's raw content, truncated.

    Scans forward from ``position``; stops at the next action event so an
    observation is never attributed to the wrong step. Returns
    ``"(no observation)"`` when none is found.
    """
    for later in events[position + 1 :]:
        if later.role == "env":
            first_line = later.raw.strip().splitlines()
            if not first_line:
                return "(no observation)"
            return _truncate(first_line[0].strip(), OBSERVATION_MAX_CHARS)
        if later.api_name is not None:
            break
    return "(no observation)"


def _strip_code_fences(text: str) -> str:
    """Remove an accidental ```/```json wrapper around a JSON payload."""
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()[1:]  # drop the opening fence line
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _parse_verdict(raw: str) -> tuple[bool, list[int], str] | None:
    """Parse and strictly validate the judge's JSON verdict.

    Returns ``(justified, unjustified_steps, rationale)`` or None on any
    parse or type failure. Types are enforced exactly: ``justified`` must be
    a bool, ``unjustified_steps`` a list of ints (bools rejected — they are
    int subclasses — and string digits are NOT coerced), ``rationale`` a str.
    """
    try:
        obj = json.loads(_strip_code_fences(raw))
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    justified = obj.get("justified")
    steps = obj.get("unjustified_steps")
    rationale = obj.get("rationale")
    if not isinstance(justified, bool):
        return None
    if not isinstance(steps, list):
        return None
    if any(isinstance(s, bool) or not isinstance(s, int) for s in steps):
        return None
    if not isinstance(rationale, str):
        return None
    return justified, list(steps), rationale


class JudgePolicy(Policy):
    """LLM-as-judge over the action path. Off by default; advisory (WARN)."""

    def __init__(
        self,
        client: JudgeClient | None = None,
        enabled: bool = False,
        model: str = DEFAULT_JUDGE_MODEL,
        max_events: int = MAX_JUDGE_EVENTS,
    ) -> None:
        self._client = client
        self._enabled = enabled
        self._model = model
        self._max_events = max_events

    def check(
        self, events: Sequence[TraceEvent], context: Mapping[str, Any]
    ) -> list[Finding]:
        if not self._enabled:
            return [
                self._info(
                    "judge disabled; skipped",
                    "JudgePolicy constructed with enabled=False; "
                    "no judgement performed and no client contacted.",
                )
            ]

        client = self._client
        if client is None:
            client = AnthropicJudgeClient.from_env(model=self._model)
        if client is None:
            return [
                self._info(
                    "judge skipped: no usable client",
                    "No client was injected and none could be built from "
                    "the environment (ANTHROPIC_API_KEY unset or the "
                    "anthropic package not installed); no judgement "
                    "performed and no network call made.",
                )
            ]

        action_positions = [
            (position, event)
            for position, event in enumerate(events)
            if event.api_name is not None
        ]
        if not action_positions:
            return [
                self._info(
                    "judge skipped: no action events",
                    "The trace contains no action events to judge; "
                    "no judgement performed.",
                )
            ]

        calls_by_index, prompt = self._build_prompt(events, action_positions, context)

        try:
            raw = client.complete(JUDGE_SYSTEM_PROMPT, prompt)
        except Exception as exc:  # client failure must never crash the run
            return [
                Finding(
                    policy=self.name,
                    severity=Severity.WARN,
                    message="judge call failed",
                    event_indices=[],
                    evidence=_truncate(
                        f"{type(exc).__name__}: {exc}", RATIONALE_MAX_CHARS
                    ),
                )
            ]

        parsed = _parse_verdict(raw)
        if parsed is None:
            return [
                Finding(
                    policy=self.name,
                    severity=Severity.WARN,
                    message="judge output unparseable",
                    event_indices=[],
                    evidence=_truncate(raw.strip(), RAW_RESPONSE_EVIDENCE_CHARS),
                )
            ]
        justified, cited_steps, rationale = parsed
        rationale_slice = _truncate(rationale.strip(), RATIONALE_MAX_CHARS)

        if justified or not cited_steps:
            return [
                self._info(
                    "judge: path justified",
                    rationale_slice or "(judge provided no rationale)",
                )
            ]

        # Keep only cited step numbers that are real action-event indices;
        # drop everything else (never fabricate). Dedupe, preserving order.
        valid_indices = set(calls_by_index)
        kept: list[int] = []
        dropped: list[int] = []
        for step in cited_steps:
            if step in valid_indices:
                if step not in kept:
                    kept.append(step)
            else:
                dropped.append(step)

        if not kept:
            return [
                Finding(
                    policy=self.name,
                    severity=Severity.WARN,
                    message=(
                        "judge: unjustified verdict cited no real step " "indices"
                    ),
                    event_indices=[],
                    evidence=(
                        f"cited steps {dropped} do not exist among the "
                        f"trace's action events; rationale: {rationale_slice}"
                    ),
                )
            ]

        return [
            Finding(
                policy=self.name,
                severity=Severity.WARN,
                message="judge: action not justified by preceding observations",
                event_indices=[step],
                evidence=(
                    f"{calls_by_index[step]} | judge rationale: " f"{rationale_slice}"
                ),
            )
            for step in kept
        ]

    def _info(self, message: str, evidence: str) -> Finding:
        return Finding(
            policy=self.name,
            severity=Severity.INFO,
            message=message,
            event_indices=[],
            evidence=evidence,
        )

    def _build_prompt(
        self,
        events: Sequence[TraceEvent],
        action_positions: Sequence[tuple[int, TraceEvent]],
        context: Mapping[str, Any],
    ) -> tuple[dict[int, str], str]:
        """Build the judge's user prompt from action events only.

        Returns ``(calls_by_index, prompt)`` where ``calls_by_index`` maps
        each action event's trace index to its compact call string (reused
        as finding evidence).
        """
        calls_by_index: dict[int, str] = {}
        step_lines: list[str] = []
        for position, event in action_positions:
            call = _format_call(event)
            calls_by_index[event.index] = call
            observation = _observation_after(events, position)
            step_lines.append(f"step {event.index}: {call} -> {observation}")

        elision_note = ""
        if len(step_lines) > self._max_events:
            head = self._max_events // 2
            tail = self._max_events - head
            omitted = len(step_lines) - self._max_events
            elision_note = (
                f"NOTE: the trajectory contains {len(step_lines)} actions; "
                f"only the first {head} and last {tail} are shown below "
                f"({omitted} intermediate steps omitted for length).\n\n"
            )
            step_lines = (
                step_lines[:head]
                + [f"... [{omitted} intermediate steps omitted] ..."]
                + step_lines[-tail:]
            )

        task_type = str(context.get("task_type", "unknown"))
        task_description = str(context.get("task_description", "")).strip()
        prompt = (
            f"Task type: {task_type}\n"
            f"Task description: {task_description or '(none provided)'}\n"
            "\n"
            f"{elision_note}"
            "Trajectory — numbered agent actions, each with the first line "
            "of the environment's observed response:\n" + "\n".join(step_lines) + "\n\n"
            "Judge each action against the observations available before "
            "it and respond with the JSON verdict only."
        )
        return calls_by_index, prompt
