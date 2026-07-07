<h1 align="center">praxis</h1>

<p align="center">
  <strong>🧭 A semantic trajectory verifier for LLM-agent traces.</strong><br>
  It grades the <em>path</em> an agent took — not just whether it landed on the right answer.
</p>

<p align="center">
  <img alt="status" src="https://img.shields.io/badge/status-feature--complete-brightgreen">
  <img alt="tests" src="https://img.shields.io/badge/tests-160%20passing-brightgreen">
  <img alt="python" src="https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue">
  <img alt="core" src="https://img.shields.io/badge/core-stdlib--only-blueviolet">
  <img alt="black" src="https://img.shields.io/badge/code%20style-black-000000">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-green">
</p>

---

## Why

An agent can reach the correct terminal answer through an unsafe or nonsensical path — mutating a resource before it ever looked at it, running a state-changing command during a read-only diagnostic task, or acting on a cause it pattern-matched but never confirmed. Every individual tool call returns `200 OK`; every step looks fine in isolation; the final answer is graded correct. The failure lives in the *sequence*.

Terminal-correctness metrics (exact-match, time-to-detect/localize/mitigate) and answer-only LLM judges can't see this. `praxis` does. The name is the thesis: **praxis** is enacted action — what an agent actually *did*, as distinct from the conclusion it stated. A right answer reached through unsound doing is still unsound. `praxis` judges the doing: it passes a trajectory when each action follows from the evidence the agent had, and flags the steps that don't.

This is the operational form of a finding that recurs across the current agentic-ops literature: production agent failures are trajectory-level and semantic, not terminal (see [Further reading](#further-reading)).

## What it does

Given a completed agent trace, `praxis`:

- normalizes it into a typed event sequence with each action classified as `READ` / `WRITE` / `SUBMIT` / `UNKNOWN`,
- runs a set of deterministic **policies** over the sequence (ordering, safety, task-contract, failure-loop, submit-discipline),
- optionally runs an **LLM-as-judge-over-the-path** for the semantic "was this step justified?" call that regex can't make,
- emits structured `Finding`s (each pinned to real trace indices with human-readable evidence), a pass/fail, and a `0.0–1.0` trajectory score.

The output is data — designed to sit alongside existing evaluation metrics as a first-class signal, or to run post-hoc over production agent traces as a governance check.

## Design

A framework-free core with pluggable adapters. The core never imports a harness; a harness is just the first thing that feeds it a trace.

```
praxis/
├── core/                    # portable engine — no framework imports, stdlib-only
│   ├── model.py             # TraceEvent, Finding, VerdictReport, Severity, AccessType
│   ├── engine.py            # TrajectoryVerifier: policies over events -> VerdictReport
│   ├── report.py            # VerdictReport -> concise text + JSON-safe dict
│   └── policies/
│       ├── base.py          # Policy ABC: check(events, context) -> list[Finding]
│       ├── rules.py         # the six deterministic policies
│       └── judge.py         # optional LLM-as-judge-over-path (off by default)
└── adapters/
    └── aiopslab/            # the ONLY place harness coupling lives
        ├── normalize.py     # SessionItem trace -> list[TraceEvent]
        └── mixin.py         # TrajectoryEvalMixin: hooks into eval, adds trajectory_* metrics
```

The pipeline:

```
trace ──▶ adapter.normalize ──▶ list[TraceEvent] ──▶ engine.verify ──▶ VerdictReport ──▶ report.summarize
                                                          ▲
                                                    policies (+ optional judge)
```

**Portability is the point.** [AIOpsLab](https://github.com/microsoft/AIOpsLab) is the reference adapter, not the home. Writing an adapter for another agent framework — or for production traces from a live multi-agent platform — means implementing one `normalize` function that maps that source's turns onto `TraceEvent`; the core and every policy come along unchanged.

## Policies

| Policy | Catches | Default severity |
|---|---|---|
| `ShellSafetyPolicy` | Mutating shell commands (`patch`/`scale`/`delete`/`drain`/`rollout`/…) — the primary write detector | VIOLATION in read-only tasks, else WARN |
| `ReadBeforeWritePolicy` | A write on a resource with no prior diagnostic read of that resource | VIOLATION (WARN if resource unresolved) |
| `ReadOnlyTaskPolicy` | Any mutation during a detection/localization task whose contract is diagnostic-only | VIOLATION |
| `MutationBeforeSubmitPolicy` | Acting before looking — a write with no diagnostic read anywhere earlier | WARN |
| `RepeatedFailureLoopPolicy` | The "apologize and re-issue the same failing call" loop | WARN |
| `SubmitDisciplinePolicy` | Zero/multiple submits, or actions after submit | WARN / VIOLATION |
| `JudgePolicy` *(optional)* | Semantically unjustified steps — action not supported by preceding observations | WARN (advisory) |

Severity weights, the mutating-verb denylist, and the failure-token set are all tunable module constants. Findings never fabricate indices: if a policy can't localize its trigger, that's a bug in the policy, not a reason to point somewhere plausible.

## Status

**✅ Feature-complete against the spec — all milestones implemented, 160 tests green.** The full build contract in [`docs/trajectory-verifier-spec.md`](docs/trajectory-verifier-spec.md) is satisfied end to end: the core engine, six deterministic policies, the optional judge, and the AIOpsLab adapter all exist with fixture-driven tests. CI runs the suite on Python 3.11 / 3.12 / 3.13 and enforces core purity and `black` formatting on every push.

| Milestone | Scope | Status |
|---|---|---|
| **M1 — Recon** | AIOpsLab's real interfaces confirmed against source (trace type, action decorators, `eval`/`common_eval` seam, parser, error signaling). Findings and design deltas in [`docs/NOTES.md`](docs/NOTES.md). | ✅ Done |
| **M2 — Core model + normalize** | `model.py` (typed `TraceEvent`/`Finding`/`VerdictReport`) and the AIOpsLab `normalize.py`, covering telemetry-API classification and `exec_shell` verb-based `WRITE` inference. | ✅ Done |
| **M3 — Deterministic policies + engine** | All six policies and `engine.verify()`; every failure mode has a dedicated fixture, and safe-path fixtures produce zero violations at score `1.0`. | ✅ Done |
| **M4 — Report + integration** | `report.summarize()` and the `TrajectoryEvalMixin` hooking `common_eval` — proven additive (existing metrics preserved via `super()`, new `trajectory_*` keys added). | ✅ Done |
| **M5 — LLM judge** *(optional)* | `JudgePolicy`, off by default, inert without `ANTHROPIC_API_KEY` (emits a single `INFO` finding, no network call), strict-JSON contract with defensive parsing. | ✅ Done |

Acceptance criteria (spec §11) are met with a single deliberate caveat, below.

> **One deliberate gap.** M4's integration is validated against stub task classes (`FakeLocalizationTask` / `FakeMitigationTask` in `tests/test_mixin.py`), not a live AIOpsLab checkout — AIOpsLab is intentionally not vendored here, to keep the core framework-free. The spec's literal "wire it onto one real localization and one real mitigation problem" step is therefore not exercised in CI. Closing it fully means adding an integration test that installs AIOpsLab and mixes the verifier onto two real problems.

The full build contract, milestones, and acceptance criteria live in [`docs/trajectory-verifier-spec.md`](docs/trajectory-verifier-spec.md). Repo-scoped agent guidance is in [`CLAUDE.md`](CLAUDE.md).

## Usage

Install (core is dependency-free; the judge extra is optional):

```bash
pip install -e ".[dev]"      # core + test tooling
pip install -e ".[judge]"    # optional: LLM-as-judge (requires ANTHROPIC_API_KEY)

python -m pytest -q          # run the 160-test suite (no cluster needed)
```

As an evaluation add-on (AIOpsLab adapter):

```python
from praxis.adapters.aiopslab import TrajectoryEvalMixin

# Mix into a task; existing metrics are preserved, trajectory_* keys are added.
class MyLocalizationTask(TrajectoryEvalMixin, LocalizationTask):
    ...
```

As a standalone check over any normalized trace:

```python
from praxis.core import TrajectoryVerifier, default_policies

report = TrajectoryVerifier(policies=default_policies()).verify(events, context)
print(report.passed, report.trajectory_score)
for f in report.findings:
    print(f.severity, f.policy, f.event_indices, "—", f.evidence)
```

## Repo layout

```
.
├── praxis/                  # package (see Design)
├── tests/                   # fixture-driven unit tests
├── docs/
│   ├── trajectory-verifier-spec.md
│   └── NOTES.md             # M1 recon record + design deltas
├── CLAUDE.md                # agent working guidance, scoped to this repo
├── pyproject.toml
├── LICENSE
└── README.md
```

## Further reading

The trajectory-level-failure thesis this tool operationalizes:

- *Measuring Agents in Production (MAP)* — empirical study of deployed agents; bounded autonomy and human-checkpointed writes dominate real systems. arXiv:2512.04123.
- *Agent System Operations (AgentOps): Categorization, Challenges, and Future Directions* — agent failures are trajectory-level and semantic, invisible to conventional log/latency/trace anomaly detection. arXiv:2606.01581.
- *AIOpsLab* — the benchmark harness this repo's reference adapter targets. arXiv:2501.06706.

## License

MIT — see [`LICENSE`](LICENSE).
