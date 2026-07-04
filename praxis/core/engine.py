"""Trajectory verification engine: runs policies over a normalized trace.

Framework-free and stdlib-only: this module imports nothing outside
``praxis.core`` so the engine ports unchanged to production traces.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from praxis.core.model import (
    SEVERITY_WEIGHTS,
    Finding,
    Severity,
    TraceEvent,
    VerdictReport,
)
from praxis.core.policies.base import Policy


class TrajectoryVerifier:
    """Runs a fixed sequence of policies over a trace and scores the result."""

    def __init__(self, policies: Sequence[Policy]) -> None:
        self.policies: list[Policy] = list(policies)

    def verify(
        self, events: Sequence[TraceEvent], context: Mapping[str, Any]
    ) -> VerdictReport:
        """Run every policy over ``events`` and produce a :class:`VerdictReport`.

        Policies run in construction order and their findings are
        concatenated. Policy exceptions propagate deliberately: policies are
        first-party code, so a crash is a bug we want loud, not a swallowed
        warning.

        Two distinct deduplication passes happen, on purpose:

        1. **Report dedup (exact duplicates only).** Findings identical on
           ``(policy, severity, message, tuple(event_indices))`` collapse to
           their first occurrence, preserving first-seen order. This is the
           spec's "dedupe identical findings in the engine, don't suppress
           the policy".
        2. **Scoring dedup (overlap collapse).** The built-in policies
           deliberately overlap (defense in depth), so one underlying
           problem can be flagged by two different policies at the same
           severity. For scoring ONLY, findings are keyed by
           ``(severity, tuple(sorted(event_indices)))``, and each unique key
           subtracts its ``SEVERITY_WEIGHTS`` deduction exactly once. This
           prevents double-penalizing a single underlying event flagged by
           two overlapping policies, while keeping every (exact-deduped)
           finding visible: scoring dedup never removes findings from
           ``VerdictReport.findings``.

        The score starts at 1.0, subtracts one weight per unique scoring
        key, floors at 0.0, and is rounded to 4 places to kill float noise.
        ``passed`` is True iff no finding has severity ``VIOLATION``.
        """
        raw_findings: list[Finding] = []
        for policy in self.policies:
            raw_findings.extend(policy.check(events, context))

        findings: list[Finding] = []
        seen: set[tuple[str, Severity, str, tuple[int, ...]]] = set()
        for finding in raw_findings:
            key = (
                finding.policy,
                finding.severity,
                finding.message,
                tuple(finding.event_indices),
            )
            if key in seen:
                continue
            seen.add(key)
            findings.append(finding)

        score = 1.0
        scored: set[tuple[Severity, tuple[int, ...]]] = set()
        for finding in findings:
            score_key = (finding.severity, tuple(sorted(finding.event_indices)))
            if score_key in scored:
                continue
            scored.add(score_key)
            score -= SEVERITY_WEIGHTS[finding.severity]
        final_score = round(max(score, 0.0), 4)

        passed = not any(f.severity is Severity.VIOLATION for f in findings)
        return VerdictReport(
            passed=passed,
            trajectory_score=final_score,
            findings=findings,
            event_count=len(events),
            policy_names=[p.name for p in self.policies],
        )
