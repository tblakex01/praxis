"""Opt-in AIOpsLab integration: trajectory scoring hooked into ``common_eval``.

This module (together with ``normalize``) is the only place AIOpsLab coupling
is allowed to live — and even here the coupling is duck-typed: there is no
``aiopslab`` import. The mixin targets the confirmed Task contract
(docs/NOTES.md, recon Q3): every task's ``eval`` calls
``common_eval(self, trace)`` with ``trace: list[SessionItem]`` (objects with
``.role``/``.content``), and the base implementation records ``steps``,
``in_tokens``, and ``out_tokens`` via ``self.add_result(key, value)`` into
``self.results`` (last-write-wins, ``json.dump``-ed later).

Usage — mix into a task class, mixin first so its ``common_eval`` wraps the
base one::

    class VerifiedKillPodLocalization(TrajectoryEvalMixin, KillPodLocalization):
        pass

The mixin is strictly additive: ``super().common_eval(trace)`` runs first and
untouched, so every existing metric is recorded exactly as before. The
verifier's results are then added under ``trajectory_``-prefixed keys only
(guaranteed by :meth:`praxis.core.model.VerdictReport.to_result_dict`), so no
pre-existing key can ever be clobbered.

All verifier imports happen lazily inside ``common_eval``: importing this
module has no side effects, and a task that never calls ``common_eval`` never
touches the verifier.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from praxis.core.policies.base import Policy

# Known AIOpsLab task types, used for class-name inference (checked in this
# order; first substring match wins).
_TASK_TYPES: tuple[str, ...] = (
    "detection",
    "localization",
    "analysis",
    "mitigation",
)


class TrajectoryEvalMixin:
    """Opt-in trajectory scoring for AIOpsLab tasks.

    Additive by construction: ``super().common_eval()`` runs untouched first,
    and the verifier only ever writes ``trajectory_``-prefixed result keys.
    """

    #: Policies to run. ``None`` (the default) resolves to
    #: :func:`praxis.core.policies.rules.default_policies` lazily at call
    #: time, keeping the default policy set decoupled from this module's
    #: import. Override on a subclass (or instance) to inject custom
    #: policies.
    trajectory_policies: ClassVar[Sequence[Policy] | None] = None

    def common_eval(self, trace: Sequence[Any]) -> Any:
        """Record the base metrics, then add trajectory verdict results.

        ``trace`` is whatever the host task's ``eval`` passes through —
        AIOpsLab's ``list[SessionItem]`` in production, or plain
        ``{"role", "content"}`` dicts in fixtures. The base class's return
        value (``None`` in AIOpsLab today) is preserved and returned.
        """
        base_result = super().common_eval(trace)

        from praxis.adapters.aiopslab.normalize import to_events
        from praxis.core.engine import TrajectoryVerifier

        events = to_events(trace)

        context: dict[str, Any] = {"task_type": self._trajectory_task_type()}
        task_description = getattr(self, "task_desc", None)
        if task_description is not None:
            context["task_description"] = task_description

        policies = self.trajectory_policies
        if policies is None:
            from praxis.core.policies.rules import default_policies

            policies = default_policies()

        report = TrajectoryVerifier(policies).verify(events, context)
        for key, value in report.to_result_dict().items():
            # Every key is trajectory_-prefixed by construction
            # (VerdictReport.to_result_dict) — existing metrics are safe.
            self.add_result(key, value)

        return base_result

    def _trajectory_task_type(self) -> str:
        """Best-effort task type for the policy context.

        An explicit, non-empty ``task_type`` string attribute on the task
        wins; otherwise infer from the class name (first known task type
        found as a substring, lowercased); otherwise ``"unknown"``.
        """
        declared = getattr(self, "task_type", None)
        if isinstance(declared, str) and declared:
            return declared
        class_name = type(self).__name__.lower()
        for task_type in _TASK_TYPES:
            if task_type in class_name:
                return task_type
        return "unknown"
