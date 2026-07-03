"""AIOpsLab adapter — the only place harness coupling lives.

``to_events`` normalizes an AIOpsLab ``session.history`` trace (live
``SessionItem`` objects or their ``{"role", "content"}`` dict form) into
core ``TraceEvent``s; ``TrajectoryEvalMixin`` wires the verifier into a
task's ``common_eval`` seam.
"""

from praxis.adapters.aiopslab.mixin import TrajectoryEvalMixin
from praxis.adapters.aiopslab.normalize import TraceFormatError, to_events

__all__ = ["TrajectoryEvalMixin", "TraceFormatError", "to_events"]
