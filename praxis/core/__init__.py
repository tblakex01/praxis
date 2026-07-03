"""Portable verifier core — framework-free, stdlib-only.

Public surface is re-exported here once the submodules exist; imports stay
lazy-free and lightweight (dataclasses and enums only at import time).
"""

from praxis.core.model import (
    AccessType,
    Finding,
    Severity,
    TraceEvent,
    VerdictReport,
)

__all__ = [
    "AccessType",
    "Finding",
    "Severity",
    "TraceEvent",
    "VerdictReport",
]
