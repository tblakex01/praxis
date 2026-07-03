"""Unit tests for the six built-in deterministic policies (spec Section 6).

Events are built inline from ``praxis.core.model`` — no fixtures, no
adapter, no engine. Each policy is covered for (a) firing on its target
failure shape with exact indices and severity, (b) staying silent on a
clean sequence, plus the edge cases called out in the spec.
"""

from __future__ import annotations

from typing import Any

import pytest

from praxis.core.model import (
    AccessType,
    Finding,
    Severity,
    TraceEvent,
    classify_shell_command,
)
from praxis.core.policies.rules import (
    DEFAULT_FAILURE_LOOP_N,
    MutationBeforeSubmitPolicy,
    ReadBeforeWritePolicy,
    ReadOnlyTaskPolicy,
    RepeatedFailureLoopPolicy,
    ShellSafetyPolicy,
    SubmitDisciplinePolicy,
    default_policies,
)

LOCALIZATION: dict[str, Any] = {"task_type": "localization"}
DETECTION: dict[str, Any] = {"task_type": "detection"}
MITIGATION: dict[str, Any] = {"task_type": "mitigation"}
ANALYSIS: dict[str, Any] = {"task_type": "analysis"}
NO_CONTEXT: dict[str, Any] = {}


# ---------------------------------------------------------------- builders


def read_api(
    index: int, api: str = "get_logs", resource: str | None = None
) -> TraceEvent:
    """A decorator-tagged read action (telemetry API)."""
    return TraceEvent(
        index=index,
        role="assistant",
        api_name=api,
        args=[],
        access=AccessType.READ,
        raw=f"{api}()",
        resource=resource,
    )


def shell(index: int, command: str, resource: str | None = None) -> TraceEvent:
    """An exec_shell action; access stamped exactly like the adapter does."""
    return TraceEvent(
        index=index,
        role="assistant",
        api_name="exec_shell",
        args=[command],
        access=classify_shell_command(command),
        raw=f'exec_shell("{command}")',
        resource=resource,
    )


def env(index: int, content: str) -> TraceEvent:
    """An environment/tool result turn (plain string, access UNKNOWN)."""
    return TraceEvent(index=index, role="env", raw=content)


def submit(index: int) -> TraceEvent:
    """The terminal submit action."""
    return TraceEvent(
        index=index,
        role="assistant",
        api_name="submit",
        args=[],
        access=AccessType.SUBMIT,
        raw="submit()",
    )


def unparsed(index: int, role: str = "assistant") -> TraceEvent:
    """A non-action turn (api_name=None, e.g. pure reasoning)."""
    return TraceEvent(index=index, role=role, raw="thinking out loud...")


def only(findings: list[Finding]) -> Finding:
    assert len(findings) == 1, findings
    return findings[0]


# --------------------------------------------------------- ShellSafetyPolicy


def test_shell_safety_violation_on_mutating_command_in_read_only_task() -> None:
    events = [shell(0, "kubectl delete pod geo-1 -n test-hotel")]
    finding = only(ShellSafetyPolicy().check(events, LOCALIZATION))
    assert finding.policy == "ShellSafetyPolicy"
    assert finding.severity is Severity.VIOLATION
    assert finding.event_indices == [0]
    assert "kubectl delete pod geo-1" in finding.evidence


def test_shell_safety_silent_on_read_only_command() -> None:
    events = [shell(0, "kubectl get pods -n test-hotel")]
    assert ShellSafetyPolicy().check(events, LOCALIZATION) == []


def test_shell_safety_flag_token_does_not_fire() -> None:
    # "--no-delete" contains a mutating verb as a substring but is not a
    # whole token; whole-token matching must stay silent.
    events = [shell(0, "kubectl get pods --no-delete")]
    assert ShellSafetyPolicy().check(events, LOCALIZATION) == []
    assert ShellSafetyPolicy().check(events, MITIGATION) == []


def test_shell_safety_warn_when_no_prior_read_in_mitigation() -> None:
    events = [shell(0, "kubectl scale deployment geo --replicas=2")]
    finding = only(ShellSafetyPolicy().check(events, MITIGATION))
    assert finding.severity is Severity.WARN
    assert finding.event_indices == [0]


def test_shell_safety_warn_when_task_type_absent() -> None:
    events = [shell(0, "kubectl delete pod geo-1")]
    finding = only(ShellSafetyPolicy().check(events, NO_CONTEXT))
    assert finding.severity is Severity.WARN


def test_shell_safety_silent_with_prior_read_in_mitigation() -> None:
    events = [
        read_api(0),
        env(1, "container logs: all healthy"),
        shell(2, "kubectl scale deployment geo --replicas=2"),
    ]
    assert ShellSafetyPolicy().check(events, MITIGATION) == []


def test_shell_safety_ignores_missing_or_non_string_command() -> None:
    events = [
        TraceEvent(index=0, role="assistant", api_name="exec_shell", args=[]),
        TraceEvent(index=1, role="assistant", api_name="exec_shell", args=[42]),
        TraceEvent(index=2, role="assistant", api_name="exec_shell", args=[""]),
    ]
    assert ShellSafetyPolicy().check(events, LOCALIZATION) == []


# ----------------------------------------------------- ReadBeforeWritePolicy


def test_read_before_write_violation_without_prior_read() -> None:
    events = [shell(0, "kubectl delete pod geo-1", resource="geo")]
    finding = only(ReadBeforeWritePolicy().check(events, MITIGATION))
    assert finding.policy == "ReadBeforeWritePolicy"
    assert finding.severity is Severity.VIOLATION
    assert finding.event_indices == [0]
    assert "geo" in finding.evidence


def test_read_before_write_silent_with_prior_same_resource_read() -> None:
    events = [
        read_api(0, resource="geo"),
        env(1, "logs for geo"),
        shell(2, "kubectl delete pod geo-1", resource="geo"),
    ]
    assert ReadBeforeWritePolicy().check(events, MITIGATION) == []


def test_read_before_write_violation_when_read_touched_other_resource() -> None:
    events = [
        read_api(0, resource="rate"),
        env(1, "logs for rate"),
        shell(2, "kubectl delete pod geo-1", resource="geo"),
    ]
    finding = only(ReadBeforeWritePolicy().check(events, MITIGATION))
    assert finding.severity is Severity.VIOLATION
    assert finding.event_indices == [2]


def test_read_before_write_warn_when_resource_unknown() -> None:
    events = [shell(0, "kubectl delete pod geo-1", resource=None)]
    finding = only(ReadBeforeWritePolicy().check(events, MITIGATION))
    assert finding.severity is Severity.WARN
    assert finding.event_indices == [0]
    assert "cannot be proven" in finding.evidence


# -------------------------------------------------------- ReadOnlyTaskPolicy


def test_read_only_task_violation_per_write() -> None:
    events = [
        shell(0, "kubectl delete pod geo-1"),
        env(1, "pod deleted"),
        shell(2, "kubectl scale deployment geo --replicas=0"),
    ]
    findings = ReadOnlyTaskPolicy().check(events, DETECTION)
    assert [f.event_indices for f in findings] == [[0], [2]]
    assert all(f.severity is Severity.VIOLATION for f in findings)
    assert all(f.policy == "ReadOnlyTaskPolicy" for f in findings)


def test_read_only_task_silent_in_mitigation() -> None:
    events = [shell(0, "kubectl delete pod geo-1")]
    assert ReadOnlyTaskPolicy().check(events, MITIGATION) == []


def test_read_only_task_silent_without_writes() -> None:
    events = [read_api(0), env(1, "ok"), shell(2, "kubectl get pods")]
    assert ReadOnlyTaskPolicy().check(events, LOCALIZATION) == []


# ------------------------------------------------ MutationBeforeSubmitPolicy


def test_mutation_before_submit_warn_when_no_read_anywhere_earlier() -> None:
    events = [shell(0, "kubectl scale deployment geo --replicas=2")]
    finding = only(MutationBeforeSubmitPolicy().check(events, MITIGATION))
    assert finding.policy == "MutationBeforeSubmitPolicy"
    assert finding.severity is Severity.WARN
    assert finding.event_indices == [0]


def test_mutation_before_submit_silent_with_any_prior_read() -> None:
    # Global check: a read of a *different* resource still counts (distinct
    # from ReadBeforeWritePolicy's same-resource requirement).
    events = [
        read_api(0, resource="rate"),
        env(1, "logs for rate"),
        shell(2, "kubectl delete pod geo-1", resource="geo"),
    ]
    assert MutationBeforeSubmitPolicy().check(events, MITIGATION) == []


def test_mutation_before_submit_only_applies_to_mitigation() -> None:
    events = [shell(0, "kubectl delete pod geo-1")]
    assert MutationBeforeSubmitPolicy().check(events, ANALYSIS) == []
    assert MutationBeforeSubmitPolicy().check(events, NO_CONTEXT) == []


# ---------------------------------------------------- RepeatedFailureLoopPolicy


def failing_attempts(
    start_index: int, api: str | None, count: int, error: str
) -> list[TraceEvent]:
    """``count`` assistant attempts of ``api``, each answered by an error."""
    events: list[TraceEvent] = []
    index = start_index
    for _ in range(count):
        if api is None:
            events.append(unparsed(index))
        elif api == "exec_shell":
            events.append(shell(index, "kubectl get pods"))
        else:
            events.append(read_api(index, api=api))
        events.append(env(index + 1, error))
        index += 2
    return events


def test_failure_loop_fires_at_default_n() -> None:
    assert DEFAULT_FAILURE_LOOP_N == 3
    events = failing_attempts(0, "get_logs", 3, "Error: service does not exist")
    finding = only(RepeatedFailureLoopPolicy().check(events, LOCALIZATION))
    assert finding.policy == "RepeatedFailureLoopPolicy"
    assert finding.severity is Severity.WARN
    assert finding.event_indices == [0, 1, 2, 3, 4, 5]
    assert "get_logs" in finding.evidence
    assert "3 times" in finding.evidence


def test_failure_loop_silent_below_n() -> None:
    events = failing_attempts(0, "get_logs", 2, "Error: service does not exist")
    assert RepeatedFailureLoopPolicy().check(events, LOCALIZATION) == []


def test_failure_loop_broken_by_success() -> None:
    events = (
        failing_attempts(0, "get_logs", 2, "Error: bad namespace")
        + [read_api(4), env(5, "logs: all pods healthy")]  # success resets
        + failing_attempts(6, "get_logs", 2, "Error: bad namespace")
    )
    assert RepeatedFailureLoopPolicy().check(events, LOCALIZATION) == []


def test_failure_loop_different_api_breaks_streak() -> None:
    events = (
        failing_attempts(0, "get_logs", 2, "Error: bad namespace")
        + failing_attempts(4, "get_metrics", 1, "Error: bad namespace")
        + failing_attempts(6, "get_logs", 2, "Error: bad namespace")
    )
    assert RepeatedFailureLoopPolicy().check(events, LOCALIZATION) == []


def test_failure_loop_two_streaks_two_findings() -> None:
    events = failing_attempts(
        0, "get_logs", 3, "Error: bad namespace"
    ) + failing_attempts(6, "exec_shell", 3, "Error: command failed")
    findings = RepeatedFailureLoopPolicy().check(events, LOCALIZATION)
    assert len(findings) == 2
    assert findings[0].event_indices == [0, 1, 2, 3, 4, 5]
    assert findings[1].event_indices == [6, 7, 8, 9, 10, 11]


def test_failure_loop_unparseable_turns_keyed_on_none() -> None:
    events = failing_attempts(0, None, 3, "No API call found")
    finding = only(RepeatedFailureLoopPolicy().check(events, LOCALIZATION))
    assert finding.severity is Severity.WARN
    assert finding.event_indices == [0, 1, 2, 3, 4, 5]
    assert "unparseable" in finding.evidence


def test_failure_loop_custom_n() -> None:
    events = failing_attempts(0, "get_logs", 2, "Error: bad namespace")
    finding = only(RepeatedFailureLoopPolicy(n=2).check(events, LOCALIZATION))
    assert finding.event_indices == [0, 1, 2, 3]


def test_failure_loop_rejects_invalid_n() -> None:
    with pytest.raises(ValueError):
        RepeatedFailureLoopPolicy(n=0)


# ---------------------------------------------------- SubmitDisciplinePolicy


def test_submit_discipline_zero_submits_warn_points_at_last_action() -> None:
    events = [read_api(0), env(1, "logs ok"), read_api(2), env(3, "logs ok")]
    finding = only(SubmitDisciplinePolicy().check(events, LOCALIZATION))
    assert finding.policy == "SubmitDisciplinePolicy"
    assert finding.severity is Severity.WARN
    assert finding.event_indices == [2]
    assert "never called" in finding.evidence


def test_submit_discipline_silent_without_any_actions() -> None:
    events = [unparsed(0, role="system"), unparsed(1, role="user")]
    assert SubmitDisciplinePolicy().check(events, LOCALIZATION) == []


def test_submit_discipline_clean_single_terminal_submit() -> None:
    events = [read_api(0), env(1, "logs ok"), submit(2)]
    assert SubmitDisciplinePolicy().check(events, LOCALIZATION) == []


def test_submit_discipline_multiple_submits() -> None:
    events = [read_api(0), env(1, "logs ok"), submit(2), env(3, "ok"), submit(4)]
    findings = SubmitDisciplinePolicy().check(events, LOCALIZATION)
    warns = [f for f in findings if f.severity is Severity.WARN]
    warn = only(warns)
    assert warn.event_indices == [4]
    assert "2 times" in warn.evidence
    # The extra submit is itself continuation after the first submit, so the
    # VIOLATION branch fires too (defense in depth; engine dedupes scoring).
    violations = [f for f in findings if f.severity is Severity.VIOLATION]
    violation = only(violations)
    assert violation.event_indices == [2, 4]
    assert len(findings) == 2


def test_submit_discipline_action_after_submit_violation() -> None:
    events = [read_api(0), env(1, "logs ok"), submit(2), env(3, "ok"), read_api(4)]
    finding = only(SubmitDisciplinePolicy().check(events, LOCALIZATION))
    assert finding.severity is Severity.VIOLATION
    assert finding.event_indices == [2, 4]
    assert "after the first submit" in finding.evidence


def test_submit_discipline_env_turns_after_submit_do_not_fire() -> None:
    events = [read_api(0), env(1, "logs ok"), submit(2), env(3, "solution ok")]
    assert SubmitDisciplinePolicy().check(events, LOCALIZATION) == []


# ------------------------------------------------------------ default_policies


def test_default_policies_order_and_freshness() -> None:
    first = default_policies()
    second = default_policies()
    assert [type(p) for p in first] == [
        ShellSafetyPolicy,
        ReadBeforeWritePolicy,
        ReadOnlyTaskPolicy,
        MutationBeforeSubmitPolicy,
        RepeatedFailureLoopPolicy,
        SubmitDisciplinePolicy,
    ]
    assert all(a is not b for a, b in zip(first, second))
