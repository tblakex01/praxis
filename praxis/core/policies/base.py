"""Policy ABC: a pure check over a normalized event sequence."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any

from praxis.core.model import Finding, TraceEvent


class Policy(ABC):
    """A single trajectory policy.

    Implementations must be pure functions of ``(events, context)``: no
    mutation of the events, no I/O (the optional LLM judge is the one
    sanctioned exception, and it is off by default).

    ``context`` is a plain mapping. The one well-known key is
    ``"task_type"`` — one of ``"detection" | "localization" | "analysis" |
    "mitigation"`` (or absent/other for non-benchmark traces). Adapters may
    add further metadata keys (e.g. ``"task_description"``).
    """

    @property
    def name(self) -> str:
        """Policy name used in findings; defaults to the class name."""
        return type(self).__name__

    @abstractmethod
    def check(
        self, events: Sequence[TraceEvent], context: Mapping[str, Any]
    ) -> list[Finding]:
        """Return findings for this policy over the full event sequence."""
        raise NotImplementedError
