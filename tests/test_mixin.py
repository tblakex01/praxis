"""Tests for :mod:`praxis.adapters.aiopslab.mixin`.

These tests replicate the confirmed AIOpsLab Task contract (docs/NOTES.md,
recon Q3) with plain stand-in classes — no ``aiopslab`` import anywhere:
every task's ``eval`` calls ``common_eval(self, trace)`` where ``trace`` is a
list of items with ``role``/``content``, and the base implementation records
``steps``/``in_tokens``/``out_tokens`` via ``add_result`` into ``results``.

The headline acceptance criterion (spec Section 11.2) is proven here: mixing
in ``TrajectoryEvalMixin`` leaves every pre-existing metric identical and
only ever adds ``trajectory_``-prefixed, JSON-safe keys.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

# The mixin lazy-imports the AIOpsLab normalizer inside ``common_eval``, so
# these tests cannot avoid it. If the concurrently-built normalize module is
# not on disk yet, skip cleanly instead of failing.
pytest.importorskip("praxis.adapters.aiopslab.normalize")

from praxis.adapters.aiopslab.mixin import TrajectoryEvalMixin
from praxis.core.model import Finding, Severity, TraceEvent
from praxis.core.policies.base import Policy

# ---------------------------------------------------------------------------
# Stand-ins for the AIOpsLab contract (no aiopslab import).
# ---------------------------------------------------------------------------


class FakeSessionItem:
    """Duck-type of AIOpsLab's ``SessionItem``: exactly ``role``/``content``."""

    def __init__(self, role: str, content: str) -> None:
        self.role = role
        self.content = content


def _role(item: Any) -> str:
    return item["role"] if isinstance(item, dict) else item.role


def _content(item: Any) -> str:
    return item["content"] if isinstance(item, dict) else item.content


class FakeTaskBase:
    """Replicates the recon'd Task base contract.

    ``add_result`` is last-write-wins into ``results`` (no type guard), and
    ``common_eval(trace)`` records ``steps``, ``in_tokens``, ``out_tokens``
    exactly as AIOpsLab's base task does.
    """

    def __init__(self) -> None:
        self.results: dict[str, Any] = {}

    def add_result(self, key: str, value: Any) -> None:
        self.results[key] = value

    def common_eval(self, trace: Sequence[Any]) -> None:
        assistant = [t for t in trace if _role(t) == "assistant"]
        other = [t for t in trace if _role(t) != "assistant"]
        self.add_result("steps", len(assistant))
        self.add_result("in_tokens", sum(len(_content(t)) for t in other))
        self.add_result("out_tokens", sum(len(_content(t)) for t in assistant))


class FakeLocalizationTask(FakeTaskBase):
    task_desc = "Localize the faulty service in the hotel-reservation app."


class FakeMitigationTask(FakeTaskBase):
    pass  # deliberately no task_desc — absence must be handled


# The two demo wirings, adapted to this standalone repo: mixin first so its
# common_eval wraps the base one.


class VerifiedLocalizationTask(TrajectoryEvalMixin, FakeLocalizationTask):
    pass


class VerifiedMitigationTask(TrajectoryEvalMixin, FakeMitigationTask):
    pass


# ---------------------------------------------------------------------------
# Inline stub policies — keep most tests independent of rules.py.
# ---------------------------------------------------------------------------


class SilentPolicy(Policy):
    """Never fires."""

    def check(
        self, events: Sequence[TraceEvent], context: Mapping[str, Any]
    ) -> list[Finding]:
        return []


class OneWarningPolicy(Policy):
    """Pins a single WARN finding to the first event."""

    def check(
        self, events: Sequence[TraceEvent], context: Mapping[str, Any]
    ) -> list[Finding]:
        if not events:
            return []
        return [
            Finding(
                policy=self.name,
                severity=Severity.WARN,
                message="stub warning",
                event_indices=[events[0].index],
                evidence="stub evidence for the first trace turn",
            )
        ]


class ContextSpyPolicy(Policy):
    """Records the context it was handed."""

    def __init__(self) -> None:
        self.seen_context: dict[str, Any] | None = None

    def check(
        self, events: Sequence[TraceEvent], context: Mapping[str, Any]
    ) -> list[Finding]:
        self.seen_context = dict(context)
        return []


# ---------------------------------------------------------------------------
# Fixtures (modeled on session.history: role/content only, per NOTES.md).
# ---------------------------------------------------------------------------


def make_trace_dicts() -> list[dict[str, str]]:
    return [
        {
            "role": "user",
            "content": "Problem: one service in test-hotel is unhealthy.",
        },
        {
            "role": "assistant",
            "content": (
                "Checking service logs first.\n"
                '```\nget_logs("test-hotel-reservation", "geo")\n```'
            ),
        },
        {
            "role": "env",
            "content": "geo | connection refused to memcached-profile",
        },
        {"role": "assistant", "content": '```\nsubmit("geo")\n```'},
        {"role": "env", "content": "Solution submitted."},
    ]


def make_trace_items() -> list[FakeSessionItem]:
    return [FakeSessionItem(d["role"], d["content"]) for d in make_trace_dicts()]


# ---------------------------------------------------------------------------
# Acceptance criterion (spec Section 11.2): existing metrics untouched.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("plain_cls", "mixed_cls"),
    [
        (FakeLocalizationTask, VerifiedLocalizationTask),
        (FakeMitigationTask, VerifiedMitigationTask),
    ],
)
def test_existing_metrics_untouched_and_only_trajectory_keys_added(
    plain_cls: type[FakeTaskBase], mixed_cls: type[FakeTaskBase]
) -> None:
    trace = make_trace_dicts()

    plain = plain_cls()
    plain.common_eval(trace)

    mixed = mixed_cls()
    mixed.trajectory_policies = [SilentPolicy(), OneWarningPolicy()]
    mixed.common_eval(trace)

    # Every pre-existing key survives with an identical value, and the only
    # additions are trajectory_-prefixed: stripping the trajectory_ keys from
    # the mixed results must reproduce the plain results exactly.
    non_trajectory = {
        k: v for k, v in mixed.results.items() if not k.startswith("trajectory_")
    }
    assert non_trajectory == plain.results

    added = set(mixed.results) - set(plain.results)
    assert added, "mixin must add trajectory results"
    assert all(k.startswith("trajectory_") for k in added)

    # AIOpsLab json.dump's the results verbatim — must stay JSON-safe.
    json.dumps(mixed.results)


def test_trajectory_result_keys_and_values() -> None:
    mixed = VerifiedLocalizationTask()
    mixed.trajectory_policies = [SilentPolicy(), OneWarningPolicy()]
    mixed.common_eval(make_trace_dicts())

    expected_keys = {
        "trajectory_passed",
        "trajectory_score",
        "trajectory_violations",
        "trajectory_warnings",
        "trajectory_event_count",
        "trajectory_policies",
        "trajectory_findings",
    }
    assert expected_keys <= set(mixed.results)
    assert mixed.results["trajectory_passed"] is True  # WARN only, no VIOLATION
    assert mixed.results["trajectory_warnings"] == 1
    assert mixed.results["trajectory_violations"] == 0
    assert mixed.results["trajectory_score"] == pytest.approx(0.9)
    assert mixed.results["trajectory_policies"] == [
        "SilentPolicy",
        "OneWarningPolicy",
    ]


def test_accepts_session_item_objects() -> None:
    """The eval boundary carries SessionItem objects, not dicts (NOTES Q1)."""
    task = VerifiedMitigationTask()
    task.trajectory_policies = [SilentPolicy()]
    task.common_eval(make_trace_items())
    assert task.results["trajectory_passed"] is True
    assert task.results["steps"] == 2  # base metrics recorded as before


# ---------------------------------------------------------------------------
# Context construction.
# ---------------------------------------------------------------------------


def test_context_gets_task_type_and_description() -> None:
    spy = ContextSpyPolicy()
    task = VerifiedLocalizationTask()  # has task_desc
    task.trajectory_policies = [spy]
    task.common_eval(make_trace_dicts())
    assert spy.seen_context is not None
    assert spy.seen_context["task_type"] == "localization"
    assert spy.seen_context["task_description"] == FakeLocalizationTask.task_desc


def test_context_omits_task_description_when_absent() -> None:
    spy = ContextSpyPolicy()
    task = VerifiedMitigationTask()  # no task_desc attribute
    task.trajectory_policies = [spy]
    task.common_eval(make_trace_dicts())
    assert spy.seen_context is not None
    assert spy.seen_context["task_type"] == "mitigation"
    assert "task_description" not in spy.seen_context


# ---------------------------------------------------------------------------
# _trajectory_task_type inference.
# ---------------------------------------------------------------------------


def test_task_type_explicit_attribute_wins() -> None:
    task = VerifiedLocalizationTask()
    task.task_type = "analysis"
    assert task._trajectory_task_type() == "analysis"


def test_task_type_inferred_from_class_name() -> None:
    assert VerifiedLocalizationTask()._trajectory_task_type() == "localization"
    assert VerifiedMitigationTask()._trajectory_task_type() == "mitigation"

    class VerifiedDetectionThing(TrajectoryEvalMixin, FakeTaskBase):
        pass

    class SomeAnalysisTask(TrajectoryEvalMixin, FakeTaskBase):
        pass

    assert VerifiedDetectionThing()._trajectory_task_type() == "detection"
    assert SomeAnalysisTask()._trajectory_task_type() == "analysis"


def test_task_type_unknown_fallback() -> None:
    class VerifiedMysteryTask(TrajectoryEvalMixin, FakeTaskBase):
        pass

    assert VerifiedMysteryTask()._trajectory_task_type() == "unknown"


def test_task_type_non_string_or_empty_attribute_falls_through() -> None:
    task = VerifiedLocalizationTask()
    task.task_type = ""
    assert task._trajectory_task_type() == "localization"
    task.task_type = 42
    assert task._trajectory_task_type() == "localization"


# ---------------------------------------------------------------------------
# No-mixin and no-side-effect guarantees.
# ---------------------------------------------------------------------------


def test_plain_task_results_are_exactly_the_base_keys() -> None:
    """A task WITHOUT the mixin behaves exactly as before, even with the
    mixin module imported (as it is, at the top of this file)."""
    plain = FakeLocalizationTask()
    trace = make_trace_dicts()
    plain.common_eval(trace)
    assert set(plain.results) == {"steps", "in_tokens", "out_tokens"}
    assert plain.results["steps"] == 2
    assert plain.results["out_tokens"] == sum(
        len(t["content"]) for t in trace if t["role"] == "assistant"
    )


def test_importing_mixin_module_has_no_side_effects() -> None:
    """Importing the mixin module itself must not pull in the engine, the
    default policies, the normalizer, or aiopslab — all imports are lazy,
    call-time imports. Verified in a fresh interpreter so this file's own
    imports don't pollute the check."""
    code = (
        "import importlib, sys\n"
        "import praxis.adapters.aiopslab  # exclude package __init__ effects\n"
        "before = set(sys.modules)\n"
        "importlib.import_module('praxis.adapters.aiopslab.mixin')\n"
        "added = set(sys.modules) - before\n"
        "forbidden = {m for m in added if m in {\n"
        "    'praxis.core.engine',\n"
        "    'praxis.core.policies.rules',\n"
        "    'praxis.adapters.aiopslab.normalize',\n"
        "} or m.startswith('aiopslab')}\n"
        "assert not forbidden, sorted(forbidden)\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


# ---------------------------------------------------------------------------
# Default policy resolution (needs the concurrently-built rules module).
# ---------------------------------------------------------------------------


def test_default_policies_resolved_when_unset() -> None:
    pytest.importorskip("praxis.core.policies.rules")
    task = VerifiedLocalizationTask()  # trajectory_policies stays None
    task.common_eval(make_trace_dicts())
    assert "trajectory_passed" in task.results
    assert isinstance(task.results["trajectory_policies"], list)
    assert task.results["trajectory_policies"], "default policy set is non-empty"
    json.dumps(task.results)
