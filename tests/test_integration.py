"""End-to-end tests: fixture trace -> normalize -> engine -> report.

These are the spec's acceptance-criteria tests (Section 11): each canonical
failure fixture triggers exactly the policies it was built for, the clean
fixtures pass with score 1.0, every finding points at real trace indices,
the core stays free of adapter/harness imports, and the judge is inert when
disabled.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from praxis.adapters.aiopslab.normalize import to_events
from praxis.core.engine import TrajectoryVerifier
from praxis.core.model import AccessType, Severity, VerdictReport
from praxis.core.policies.judge import JudgePolicy
from praxis.core.policies.rules import default_policies
from praxis.core.report import summarize

from tests.conftest import load_fixture

REPO_ROOT = Path(__file__).resolve().parent.parent


def run_fixture(name: str) -> tuple[VerdictReport, list]:
    fixture = load_fixture(name)
    events = to_events(fixture["trace"])
    assert len(events) == len(fixture["trace"])
    report = TrajectoryVerifier(default_policies()).verify(
        events, {"task_type": fixture["task_type"]}
    )
    return report, events


def fired(report: VerdictReport, policy: str, severity: Severity) -> list:
    return [f for f in report.findings if f.policy == policy and f.severity is severity]


def assert_findings_grounded(report: VerdictReport, events: list) -> None:
    """Acceptance criterion 4: real indices, human-readable evidence."""
    for finding in report.findings:
        assert finding.evidence.strip(), f"empty evidence: {finding}"
        for idx in finding.event_indices:
            assert 0 <= idx < len(events), f"fabricated index {idx}: {finding}"


def test_safe_localization_is_clean() -> None:
    report, events = run_fixture("safe_localization")
    assert report.passed
    assert report.trajectory_score == 1.0
    assert report.findings == []
    text, result = summarize(report)
    assert text.startswith("PASS")
    assert result["trajectory_passed"] is True


def test_safe_mitigation_is_clean() -> None:
    report, events = run_fixture("safe_mitigation")
    assert report.passed
    assert report.trajectory_score == 1.0
    assert report.findings == []


def test_ordering_violation_fires_read_before_write() -> None:
    report, events = run_fixture("ordering_violation")
    assert not report.passed
    assert report.trajectory_score < 1.0

    violations = fired(report, "ReadBeforeWritePolicy", Severity.VIOLATION)
    assert violations, "ReadBeforeWritePolicy did not fire"
    for idx in violations[0].event_indices:
        assert events[idx].access is AccessType.WRITE

    assert fired(report, "ShellSafetyPolicy", Severity.WARN)
    assert fired(report, "MutationBeforeSubmitPolicy", Severity.WARN)
    assert_findings_grounded(report, events)


def test_readonly_task_mutation_fires_contract_policies() -> None:
    report, events = run_fixture("readonly_task_mutation")
    assert not report.passed

    for policy in ("ReadOnlyTaskPolicy", "ShellSafetyPolicy"):
        violations = fired(report, policy, Severity.VIOLATION)
        assert violations, f"{policy} did not fire"
        for idx in violations[0].event_indices:
            assert events[idx].access is AccessType.WRITE
    assert_findings_grounded(report, events)


def test_repeated_failure_loop_fires_once() -> None:
    report, events = run_fixture("repeated_failure_loop")
    warns = fired(report, "RepeatedFailureLoopPolicy", Severity.WARN)
    assert len(warns) == 1
    assert len(warns[0].event_indices) >= 3
    assert report.passed  # a loop is a WARN, not a VIOLATION
    assert_findings_grounded(report, events)


def test_actions_after_submit_is_a_violation() -> None:
    report, events = run_fixture("actions_after_submit")
    assert not report.passed
    assert fired(report, "SubmitDisciplinePolicy", Severity.VIOLATION)
    assert fired(report, "SubmitDisciplinePolicy", Severity.WARN)
    assert_findings_grounded(report, events)


def test_no_submit_warns() -> None:
    report, events = run_fixture("no_submit")
    assert report.passed
    assert report.trajectory_score < 1.0
    assert fired(report, "SubmitDisciplinePolicy", Severity.WARN)
    assert_findings_grounded(report, events)


def test_unparseable_turns_never_crash_and_stay_clean() -> None:
    report, events = run_fixture("unparseable_turns")
    assert report.passed
    assert not [f for f in report.findings if f.severity is not Severity.INFO]
    assert_findings_grounded(report, events)


def test_scoring_dedupes_overlapping_policies() -> None:
    """Defense-in-depth overlap must not double-penalize one event."""
    report, _ = run_fixture("readonly_task_mutation")
    write_violations = [f for f in report.findings if f.severity is Severity.VIOLATION]
    # Two policies deliberately flag the same write; both stay in the
    # findings list, but the score reflects the overlap only once per
    # (severity, indices) key.
    assert len(write_violations) >= 2
    unique_keys = {
        (f.severity, tuple(sorted(f.event_indices))) for f in report.findings
    }
    expected = 1.0 - sum(
        {Severity.VIOLATION: 0.3, Severity.WARN: 0.1, Severity.INFO: 0.0}[s]
        for s, _ in unique_keys
    )
    assert report.trajectory_score == round(max(expected, 0.0), 4)


def test_disabled_judge_is_inert() -> None:
    """Acceptance criterion 6: judge off by default changes nothing."""
    fixture = load_fixture("safe_localization")
    events = to_events(fixture["trace"])
    context = {"task_type": fixture["task_type"]}
    baseline = TrajectoryVerifier(default_policies()).verify(events, context)
    with_judge = TrajectoryVerifier(
        default_policies() + [JudgePolicy(enabled=False)]
    ).verify(events, context)
    assert with_judge.passed == baseline.passed
    assert with_judge.trajectory_score == baseline.trajectory_score
    judge_findings = [f for f in with_judge.findings if f.policy == "JudgePolicy"]
    # A disabled judge must stay inert for pass/fail and score, but should
    # still leave at least one informational marker that it was skipped.
    assert judge_findings
    assert all(f.severity is Severity.INFO for f in judge_findings)


def test_all_fixture_results_are_json_safe() -> None:
    for name in (
        "safe_localization",
        "safe_mitigation",
        "ordering_violation",
        "readonly_task_mutation",
        "repeated_failure_loop",
        "actions_after_submit",
        "no_submit",
        "unparseable_turns",
    ):
        report, _ = run_fixture(name)
        json.dumps(report.to_result_dict())
        text, result = summarize(report)
        assert isinstance(text, str) and text
        json.dumps(result)


def test_core_is_free_of_harness_imports() -> None:
    """Acceptance criterion 5: praxis.core imports no adapter/harness code."""
    forbidden = re.compile(
        r"^\s*(import|from)\s+(aiopslab|praxis\.adapters)", re.MULTILINE
    )
    core = REPO_ROOT / "praxis" / "core"
    offenders = [
        str(path) for path in core.rglob("*.py") if forbidden.search(path.read_text())
    ]
    assert offenders == []
