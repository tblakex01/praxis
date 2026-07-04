"""Unit tests for ``praxis.core.engine.TrajectoryVerifier``.

Uses small inline ``Policy`` stubs returning canned findings — no imports
from ``rules.py`` or ``judge.py``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from praxis.core.engine import TrajectoryVerifier
from praxis.core.model import Finding, Severity, TraceEvent
from praxis.core.policies.base import Policy


def make_events(n: int) -> list[TraceEvent]:
    return [TraceEvent(index=i, role="assistant") for i in range(n)]


def make_finding(
    policy: str = "StubPolicy",
    severity: Severity = Severity.VIOLATION,
    message: str = "mutating command before any read",
    indices: list[int] | None = None,
    evidence: str = "kubectl delete at event 4 with no prior read",
) -> Finding:
    return Finding(
        policy=policy,
        severity=severity,
        message=message,
        event_indices=indices if indices is not None else [4],
        evidence=evidence,
    )


class StubPolicy(Policy):
    """Returns canned findings under an optional custom name."""

    def __init__(self, findings: list[Finding], name: str | None = None) -> None:
        self._findings = findings
        self._name = name

    @property
    def name(self) -> str:
        return self._name if self._name is not None else type(self).__name__

    def check(
        self, events: Sequence[TraceEvent], context: Mapping[str, Any]
    ) -> list[Finding]:
        return list(self._findings)


class CrashingPolicy(Policy):
    def check(
        self, events: Sequence[TraceEvent], context: Mapping[str, Any]
    ) -> list[Finding]:
        raise RuntimeError("policy bug")


def test_empty_policies_clean_run() -> None:
    report = TrajectoryVerifier([]).verify(make_events(3), {})
    assert report.passed is True
    assert report.trajectory_score == 1.0
    assert report.findings == []
    assert report.event_count == 3
    assert report.policy_names == []


def test_clean_policies_produce_pass_and_full_score() -> None:
    verifier = TrajectoryVerifier([StubPolicy([], name="A"), StubPolicy([], name="B")])
    report = verifier.verify(make_events(5), {"task_type": "localization"})
    assert report.passed is True
    assert report.trajectory_score == 1.0
    assert report.findings == []


def test_violation_fails_and_subtracts_weight() -> None:
    verifier = TrajectoryVerifier(
        [StubPolicy([make_finding(severity=Severity.VIOLATION)])]
    )
    report = verifier.verify(make_events(6), {})
    assert report.passed is False
    assert report.trajectory_score == pytest.approx(0.7)
    assert len(report.findings) == 1


def test_warn_weight_subtracted_but_still_passes() -> None:
    verifier = TrajectoryVerifier([StubPolicy([make_finding(severity=Severity.WARN)])])
    report = verifier.verify(make_events(6), {})
    assert report.passed is True
    assert report.trajectory_score == pytest.approx(0.9)


def test_info_weight_is_zero() -> None:
    verifier = TrajectoryVerifier([StubPolicy([make_finding(severity=Severity.INFO)])])
    report = verifier.verify(make_events(6), {})
    assert report.passed is True
    assert report.trajectory_score == 1.0
    assert len(report.findings) == 1


def test_exact_duplicate_findings_collapse_to_one() -> None:
    duplicate = make_finding(policy="P", indices=[2])
    verifier = TrajectoryVerifier(
        [StubPolicy([duplicate, make_finding(policy="P", indices=[2])], name="P")]
    )
    report = verifier.verify(make_events(4), {})
    assert len(report.findings) == 1
    assert report.trajectory_score == pytest.approx(0.7)


def test_cross_policy_overlap_kept_in_findings_but_scored_once() -> None:
    """Two policies flag the same events at the same severity: both findings
    stay in the report, but the weight is subtracted only once. Scoring keys
    sort the indices, so [5, 2] and [2, 5] collide."""
    a = StubPolicy(
        [make_finding(policy="A", message="shell mutation", indices=[2, 5])],
        name="A",
    )
    b = StubPolicy(
        [make_finding(policy="B", message="write in read-only task", indices=[5, 2])],
        name="B",
    )
    report = TrajectoryVerifier([a, b]).verify(make_events(7), {})
    assert len(report.findings) == 2
    assert report.trajectory_score == pytest.approx(0.7)
    assert report.passed is False


def test_score_floors_at_zero() -> None:
    findings = [
        make_finding(policy="P", message=f"v{i}", indices=[i]) for i in range(5)
    ]
    report = TrajectoryVerifier([StubPolicy(findings, name="P")]).verify(
        make_events(6), {}
    )
    assert report.trajectory_score == 0.0
    assert len(report.findings) == 5


def test_score_rounding_kills_float_noise() -> None:
    findings = [
        make_finding(policy="P", severity=Severity.WARN, message=f"w{i}", indices=[i])
        for i in range(3)
    ]
    report = TrajectoryVerifier([StubPolicy(findings, name="P")]).verify(
        make_events(4), {}
    )
    # 1.0 - 3 * 0.1 accumulates float noise without rounding.
    assert report.trajectory_score == 0.7


def test_policy_order_preserved_in_policy_names() -> None:
    verifier = TrajectoryVerifier(
        [
            StubPolicy([], name="Alpha"),
            StubPolicy([], name="Beta"),
            StubPolicy([], name="Gamma"),
        ]
    )
    report = verifier.verify(make_events(1), {})
    assert report.policy_names == ["Alpha", "Beta", "Gamma"]


def test_policy_exceptions_propagate() -> None:
    verifier = TrajectoryVerifier([CrashingPolicy()])
    with pytest.raises(RuntimeError, match="policy bug"):
        verifier.verify(make_events(2), {})


def test_result_dict_json_round_trips() -> None:
    verifier = TrajectoryVerifier(
        [
            StubPolicy(
                [
                    make_finding(policy="P", severity=Severity.VIOLATION, indices=[1]),
                    make_finding(
                        policy="P",
                        severity=Severity.WARN,
                        message="acted before looking",
                        indices=[3],
                    ),
                ],
                name="P",
            )
        ]
    )
    report = verifier.verify(make_events(5), {})
    result = report.to_result_dict()
    round_tripped = json.loads(json.dumps(result))
    assert round_tripped == result
    severities = {f["severity"] for f in round_tripped["trajectory_findings"]}
    assert severities == {"violation", "warn"}
