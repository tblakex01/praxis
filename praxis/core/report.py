"""Render a :class:`VerdictReport` as concise text plus a flat metrics dict."""

from __future__ import annotations

from typing import Any

from praxis.core.model import Severity, VerdictReport


def summarize(report: VerdictReport) -> tuple[str, dict[str, Any]]:
    """Summarize ``report`` as a human-readable block and a result dict.

    The text block leads with the outcome line::

        PASS — trajectory score 1.00; 0 violation(s), 0 warning(s) over 12 events

    followed by one plain-language line per finding::

        [VIOLATION] ReadBeforeWritePolicy @ events [4]: <message> — <evidence>

    or ``No findings.`` when the report is clean. The dict is exactly
    ``report.to_result_dict()`` (flat, JSON-safe, ``trajectory_``-prefixed).
    """
    violations = sum(1 for f in report.findings if f.severity is Severity.VIOLATION)
    warnings = sum(1 for f in report.findings if f.severity is Severity.WARN)
    outcome = "PASS" if report.passed else "FAIL"
    lines = [
        f"{outcome} — trajectory score {report.trajectory_score:.2f}; "
        f"{violations} violation(s), {warnings} warning(s) "
        f"over {report.event_count} events"
    ]
    if report.findings:
        for finding in report.findings:
            lines.append(
                f"[{finding.severity.value.upper()}] {finding.policy} "
                f"@ events {list(finding.event_indices)}: "
                f"{finding.message} — {finding.evidence}"
            )
    else:
        lines.append("No findings.")
    return "\n".join(lines), report.to_result_dict()
