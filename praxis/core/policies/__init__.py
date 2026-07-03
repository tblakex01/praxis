"""Policy registry and load order.

``default_policies()`` returns the six deterministic policies in spec
order. The optional LLM judge is exported but never part of the default
set — it is opt-in by construction (off by default, no-op without a
client/key).
"""

from praxis.core.policies.base import Policy
from praxis.core.policies.judge import (
    AnthropicJudgeClient,
    JudgeClient,
    JudgePolicy,
)
from praxis.core.policies.rules import (
    MutationBeforeSubmitPolicy,
    ReadBeforeWritePolicy,
    ReadOnlyTaskPolicy,
    RepeatedFailureLoopPolicy,
    ShellSafetyPolicy,
    SubmitDisciplinePolicy,
    default_policies,
)

__all__ = [
    "Policy",
    "ShellSafetyPolicy",
    "ReadBeforeWritePolicy",
    "ReadOnlyTaskPolicy",
    "MutationBeforeSubmitPolicy",
    "RepeatedFailureLoopPolicy",
    "SubmitDisciplinePolicy",
    "default_policies",
    "JudgePolicy",
    "JudgeClient",
    "AnthropicJudgeClient",
]
