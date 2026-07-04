"""Portable verifier core — framework-free, stdlib-only.

Public surface: the data model, the engine, the report formatter, and the
policy registry. Nothing here imports a harness or ``praxis.adapters``.
"""

from praxis.core.engine import TrajectoryVerifier
from praxis.core.model import (
    AccessType,
    Finding,
    Severity,
    TraceEvent,
    VerdictReport,
)
from praxis.core.policies import JudgePolicy, Policy, default_policies
from praxis.core.report import summarize

__all__ = [
    "AccessType",
    "Finding",
    "Severity",
    "TraceEvent",
    "VerdictReport",
    "TrajectoryVerifier",
    "Policy",
    "JudgePolicy",
    "default_policies",
    "summarize",
]
