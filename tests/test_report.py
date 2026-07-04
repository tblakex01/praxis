"""Unit tests for ``praxis.core.report.summarize``."""

from __future__ import annotations

from praxis.core.model import Finding, Severity, VerdictReport
from praxis.core.report import summarize


def make_report(
    passed: bool = True,
    score: float = 1.0,
    findings: list[Finding] | None = None,
    event_count: int = 12,
) -> VerdictReport:
    return VerdictReport(
        passed=passed,
        trajectory_score=score,
        findings=findings if findings is not None else [],
        event_count=event_count,
        policy_names=["ShellSafetyPolicy", "ReadBeforeWritePolicy"],
    )


def test_pass_first_line_and_no_findings_line() -> None:
    text, _ = summarize(make_report())
    lines = text.splitlines()
    assert lines[0] == (
        "PASS — trajectory score 1.00; 0 violation(s), 0 warning(s) over 12 events"
    )
    assert lines[1] == "No findings."
    assert len(lines) == 2


def test_fail_first_line_counts_by_severity() -> None:
    findings = [
        Finding(
            policy="ReadBeforeWritePolicy",
            severity=Severity.VIOLATION,
            message="write with no prior read of the same resource",
            event_indices=[4],
            evidence="kubectl scale at event 4; no earlier read of 'geo'",
        ),
        Finding(
            policy="SubmitDisciplinePolicy",
            severity=Severity.WARN,
            message="no submit call terminates the trace",
            event_indices=[7],
            evidence="trace ends at event 7 without submit",
        ),
        Finding(
            policy="JudgePolicy",
            severity=Severity.INFO,
            message="judge skipped",
            event_indices=[0],
            evidence="no API key configured",
        ),
    ]
    text, _ = summarize(
        make_report(passed=False, score=0.6, findings=findings, event_count=8)
    )
    lines = text.splitlines()
    assert lines[0] == (
        "FAIL — trajectory score 0.60; 1 violation(s), 1 warning(s) over 8 events"
    )
    assert len(lines) == 4


def test_finding_lines_carry_policy_indices_message_and_evidence() -> None:
    findings = [
        Finding(
            policy="ReadBeforeWritePolicy",
            severity=Severity.VIOLATION,
            message="write with no prior read",
            event_indices=[4],
            evidence="kubectl delete pod at event 4",
        ),
        Finding(
            policy="RepeatedFailureLoopPolicy",
            severity=Severity.WARN,
            message="same failing call repeated",
            event_indices=[2, 5, 8],
            evidence="three consecutive errors for get_logs",
        ),
    ]
    text, _ = summarize(
        make_report(passed=False, score=0.6, findings=findings, event_count=10)
    )
    lines = text.splitlines()
    assert lines[1] == (
        "[VIOLATION] ReadBeforeWritePolicy @ events [4]: "
        "write with no prior read — kubectl delete pod at event 4"
    )
    assert lines[2] == (
        "[WARN] RepeatedFailureLoopPolicy @ events [2, 5, 8]: "
        "same failing call repeated — three consecutive errors for get_logs"
    )


def test_dict_is_exactly_to_result_dict() -> None:
    findings = [
        Finding(
            policy="ShellSafetyPolicy",
            severity=Severity.VIOLATION,
            message="mutating verb during read-only task",
            event_indices=[3],
            evidence="kubectl patch during localization",
        )
    ]
    report = make_report(passed=False, score=0.7, findings=findings, event_count=6)
    _, result = summarize(report)
    assert result == report.to_result_dict()


def test_score_rendered_with_two_decimals() -> None:
    text, _ = summarize(make_report(score=0.8999))
    assert text.splitlines()[0].startswith("PASS — trajectory score 0.90;")
