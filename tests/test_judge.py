"""Tests for the LLM-as-judge policy (praxis/core/policies/judge.py).

No network, ever: every test uses a MockClient or exercises a skip path.
The ``anthropic`` package is not installed in this environment, so the
module-import and ``from_env`` package-missing paths are exercised for real.
"""

from __future__ import annotations

import sys

import pytest

from praxis.core.model import AccessType, Finding, Severity, TraceEvent
from praxis.core.policies.judge import (
    JUDGE_SYSTEM_PROMPT,
    MAX_JUDGE_EVENTS,
    AnthropicJudgeClient,
    JudgePolicy,
)


class MockClient:
    """Records calls; returns a canned string or raises a canned exception."""

    def __init__(self, response: str = "", exc: Exception | None = None) -> None:
        self.response = response
        self.exc = exc
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, prompt: str) -> str:
        self.calls.append((system, prompt))
        if self.exc is not None:
            raise self.exc
        return self.response


def _action(
    index: int,
    api_name: str,
    args: list | None = None,
    kwargs: dict | None = None,
    access: AccessType = AccessType.UNKNOWN,
) -> TraceEvent:
    return TraceEvent(
        index=index,
        role="assistant",
        api_name=api_name,
        args=args or [],
        kwargs=kwargs or {},
        access=access,
        raw="",
    )


def _env(index: int, raw: str) -> TraceEvent:
    return TraceEvent(index=index, role="env", raw=raw)


def _events() -> list[TraceEvent]:
    """A small trace: real action at index 4 (exec_shell), none at 999."""
    return [
        TraceEvent(index=0, role="user", raw="Localize the fault."),
        _action(
            1,
            "get_logs",
            args=["test-ns", "user-service"],
            access=AccessType.READ,
        ),
        _env(2, "no errors found in log tail\nsecond line should not appear"),
        TraceEvent(index=3, role="assistant", raw="thinking out loud..."),
        _action(
            4,
            "exec_shell",
            kwargs={
                "command": "kubectl -n test-ns scale deploy user-service "
                "--replicas=3"
            },
            access=AccessType.WRITE,
        ),
        _env(5, "deployment.apps/user-service scaled"),
        _action(6, "submit", args=["user-service"], access=AccessType.SUBMIT),
        _env(7, "submission received"),
    ]


CONTEXT = {
    "task_type": "localization",
    "task_description": "Find the faulty service in test-social-network.",
}


def _single(findings: list[Finding]) -> Finding:
    assert len(findings) == 1, findings
    return findings[0]


# ---------------------------------------------------------------------------
# Disabled / no-client paths
# ---------------------------------------------------------------------------


def test_disabled_returns_single_info_and_never_calls_client() -> None:
    mock = MockClient(response='{"justified": true}')
    policy = JudgePolicy(client=mock, enabled=False)
    finding = _single(policy.check(_events(), CONTEXT))
    assert finding.severity is Severity.INFO
    assert finding.message == "judge disabled; skipped"
    assert finding.event_indices == []
    assert mock.calls == []


def test_default_construction_is_disabled() -> None:
    finding = _single(JudgePolicy().check(_events(), CONTEXT))
    assert finding.severity is Severity.INFO
    assert "disabled" in finding.message


def test_enabled_no_client_no_key_skips_with_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    policy = JudgePolicy(enabled=True)
    finding = _single(policy.check(_events(), CONTEXT))
    assert finding.severity is Severity.INFO
    assert "skipped" in finding.message
    assert finding.event_indices == []


def test_from_env_returns_none_without_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert AnthropicJudgeClient.from_env() is None


def test_from_env_returns_none_when_package_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    # None in sys.modules makes `import anthropic` raise ImportError, which
    # simulates package absence even if it were somehow installed.
    monkeypatch.setitem(sys.modules, "anthropic", None)
    assert AnthropicJudgeClient.from_env() is None


def test_no_action_events_skips_without_calling_client() -> None:
    mock = MockClient(response='{"justified": true}')
    policy = JudgePolicy(client=mock, enabled=True)
    events = [TraceEvent(index=0, role="user", raw="hi"), _env(1, "ok")]
    finding = _single(policy.check(events, CONTEXT))
    assert finding.severity is Severity.INFO
    assert "no action events" in finding.message
    assert mock.calls == []


# ---------------------------------------------------------------------------
# Verdict mapping
# ---------------------------------------------------------------------------


def test_unjustified_step_maps_to_warn_and_invalid_index_dropped() -> None:
    mock = MockClient(
        response='{"justified": false, "unjustified_steps": [4, 999], '
        '"rationale": "step 4 scales user-service but no prior observation '
        'implicates it."}'
    )
    policy = JudgePolicy(client=mock, enabled=True)
    findings = policy.check(_events(), CONTEXT)
    finding = _single(findings)
    assert finding.severity is Severity.WARN
    assert finding.message == "judge: action not justified by preceding observations"
    assert finding.event_indices == [4]
    assert "999" not in str(finding.event_indices)
    # Evidence carries the step's command plus the rationale slice.
    assert "kubectl" in finding.evidence
    assert "no prior observation" in finding.evidence


def test_justified_true_maps_to_single_info_with_rationale() -> None:
    mock = MockClient(
        response='{"justified": true, "unjustified_steps": [], '
        '"rationale": "all mutations follow observed evidence."}'
    )
    policy = JudgePolicy(client=mock, enabled=True)
    finding = _single(policy.check(_events(), CONTEXT))
    assert finding.severity is Severity.INFO
    assert finding.message == "judge: path justified"
    assert finding.event_indices == []
    assert "observed evidence" in finding.evidence


def test_justified_true_wins_over_nonempty_step_list() -> None:
    # Contradictory verdict: the boolean wins per the spec's mapping.
    mock = MockClient(
        response='{"justified": true, "unjustified_steps": [4], '
        '"rationale": "contradictory."}'
    )
    finding = _single(JudgePolicy(client=mock, enabled=True).check(_events(), CONTEXT))
    assert finding.severity is Severity.INFO
    assert finding.message == "judge: path justified"


def test_duplicate_cited_indices_deduped_to_one_warn() -> None:
    mock = MockClient(
        response='{"justified": false, "unjustified_steps": [4, 4], '
        '"rationale": "step 4 repeated."}'
    )
    findings = JudgePolicy(client=mock, enabled=True).check(_events(), CONTEXT)
    finding = _single(findings)
    assert finding.event_indices == [4]


def test_all_cited_indices_invalid_yields_warn_with_no_indices() -> None:
    mock = MockClient(
        response='{"justified": false, "unjustified_steps": [999], '
        '"rationale": "phantom step."}'
    )
    finding = _single(JudgePolicy(client=mock, enabled=True).check(_events(), CONTEXT))
    assert finding.severity is Severity.WARN
    assert finding.event_indices == []
    assert "999" in finding.evidence


def test_multiple_valid_steps_yield_one_warn_each() -> None:
    mock = MockClient(
        response='{"justified": false, "unjustified_steps": [1, 4], '
        '"rationale": "steps 1 and 4 unsupported."}'
    )
    findings = JudgePolicy(client=mock, enabled=True).check(_events(), CONTEXT)
    assert len(findings) == 2
    assert [f.event_indices for f in findings] == [[1], [4]]
    assert all(f.severity is Severity.WARN for f in findings)


# ---------------------------------------------------------------------------
# Defensive parsing
# ---------------------------------------------------------------------------


def test_malformed_json_yields_unparseable_warn_with_raw_evidence() -> None:
    mock = MockClient(response="I think the path looks fine overall.")
    finding = _single(JudgePolicy(client=mock, enabled=True).check(_events(), CONTEXT))
    assert finding.severity is Severity.WARN
    assert finding.message == "judge output unparseable"
    assert finding.event_indices == []
    assert "I think the path" in finding.evidence
    assert len(finding.evidence) <= 200


def test_fenced_json_is_parsed() -> None:
    mock = MockClient(
        response='```json\n{"justified": true, "unjustified_steps": [], '
        '"rationale": "fenced but fine."}\n```'
    )
    finding = _single(JudgePolicy(client=mock, enabled=True).check(_events(), CONTEXT))
    assert finding.severity is Severity.INFO
    assert finding.message == "judge: path justified"


@pytest.mark.parametrize(
    "response",
    [
        # unjustified_steps holds string digits — no lenient coercion.
        '{"justified": false, "unjustified_steps": ["4"], "rationale": "x"}',
        # justified is a string, not a bool.
        '{"justified": "true", "unjustified_steps": [], "rationale": "x"}',
        # bools are int subclasses; must still be rejected in the list.
        '{"justified": false, "unjustified_steps": [true], "rationale": "x"}',
        # rationale is not a string.
        '{"justified": true, "unjustified_steps": [], "rationale": 7}',
        # missing keys.
        '{"justified": true}',
        # top-level is not an object.
        "[1, 2, 3]",
    ],
)
def test_type_invalid_json_yields_unparseable_warn(response: str) -> None:
    mock = MockClient(response=response)
    finding = _single(JudgePolicy(client=mock, enabled=True).check(_events(), CONTEXT))
    assert finding.severity is Severity.WARN
    assert finding.message == "judge output unparseable"


def test_client_exception_yields_call_failed_warn() -> None:
    mock = MockClient(exc=RuntimeError("boom"))
    finding = _single(JudgePolicy(client=mock, enabled=True).check(_events(), CONTEXT))
    assert finding.severity is Severity.WARN
    assert finding.message == "judge call failed"
    assert finding.event_indices == []
    assert "boom" in finding.evidence


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def test_prompt_contains_apis_observations_and_task_description() -> None:
    mock = MockClient(
        response='{"justified": true, "unjustified_steps": [], ' '"rationale": "ok"}'
    )
    JudgePolicy(client=mock, enabled=True).check(_events(), CONTEXT)
    assert len(mock.calls) == 1
    system, prompt = mock.calls[0]

    # API names and step labels from action events only.
    assert "step 1: get_logs(" in prompt
    assert "step 4: exec_shell(" in prompt
    assert "step 6: submit(" in prompt
    assert "thinking out loud" not in prompt  # non-action turn excluded

    # Observations: first line of the next env turn only.
    assert "no errors found in log tail" in prompt
    assert "second line should not appear" not in prompt
    assert "deployment.apps/user-service scaled" in prompt

    # Task metadata.
    assert "localization" in prompt
    assert "Find the faulty service in test-social-network." in prompt

    # System prompt requests the exact JSON schema and forbids extras.
    assert system == JUDGE_SYSTEM_PROMPT
    assert '"justified"' in system
    assert '"unjustified_steps"' in system
    assert '"rationale"' in system
    assert "no markdown code fences" in system


def test_prompt_observation_truncated_to_120_chars() -> None:
    long_first_line = "E" * 300
    events = [
        _action(0, "get_logs", args=["ns", "svc"]),
        _env(1, long_first_line + "\ntail line"),
    ]
    mock = MockClient(
        response='{"justified": true, "unjustified_steps": [], ' '"rationale": "ok"}'
    )
    JudgePolicy(client=mock, enabled=True).check(events, CONTEXT)
    _, prompt = mock.calls[0]
    assert "E" * 117 + "..." in prompt
    assert "E" * 130 not in prompt


def test_prompt_marks_missing_observation() -> None:
    events = [
        _action(0, "get_logs", args=["ns", "svc"]),
        _action(1, "get_metrics", args=["ns", "svc"]),  # no env in between
        _env(2, "metrics ok"),
    ]
    mock = MockClient(
        response='{"justified": true, "unjustified_steps": [], ' '"rationale": "ok"}'
    )
    JudgePolicy(client=mock, enabled=True).check(events, CONTEXT)
    _, prompt = mock.calls[0]
    assert "step 0: get_logs('ns', 'svc') -> (no observation)" in prompt
    assert "step 1: get_metrics('ns', 'svc') -> metrics ok" in prompt


def test_prompt_caps_steps_and_notes_elision() -> None:
    events: list[TraceEvent] = []
    total_actions = MAX_JUDGE_EVENTS + 10
    for i in range(total_actions):
        events.append(_action(2 * i, "get_logs", args=[f"svc-{i}"]))
        events.append(_env(2 * i + 1, f"log tail {i}"))
    mock = MockClient(
        response='{"justified": true, "unjustified_steps": [], ' '"rationale": "ok"}'
    )
    JudgePolicy(client=mock, enabled=True).check(events, CONTEXT)
    _, prompt = mock.calls[0]
    assert "omitted" in prompt
    assert prompt.count("step ") == MAX_JUDGE_EVENTS
    assert "step 0: " in prompt  # head retained
    assert f"step {2 * (total_actions - 1)}: " in prompt  # tail retained
    assert f"step {2 * (total_actions // 2)}: " not in prompt  # middle cut
