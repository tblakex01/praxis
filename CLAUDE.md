# CLAUDE.md — AIOpsLab (Trajectory Verifier work)

Guidance for Claude working in this repo. Scoped to `microsoft/AIOpsLab` and the current task: adding a **semantic trajectory verifier** (see `trajectory-verifier-spec.md`). Written for Claude Fable 5 / Opus 4.8. Kept short on purpose — it sets direction and boundaries; it does not script steps. If an instruction here is wrong for the task in front of you, use judgment and note the deviation in `NOTES.md`.

---

## What this repo is

AIOpsLab is a benchmark harness (MIT, Python ≥3.11, Poetry) where an LLM agent solves cloud-incident **problems** — detection / localization / analysis / mitigation — against live microservice apps (DeathStarBench HotelReservation & SocialNetwork, OTel Astronomy Shop, TiDB, others) with faults injected via Chaos Mesh, K8s operator CRs, and app/OS-level injectors. The Orchestrator runs the agent↔cloud loop, collects telemetry (Prometheus / Jaeger / Elasticsearch), and evaluates the agent's solution.

**We are not changing the harness.** The current work adds a post-hoc, additive evaluation module that scores an agent's *path*, not just its terminal answer. It must not alter any existing metric or the control loop.

---

## Environment & commands

```bash
poetry env use python3.11 && poetry install && eval $(poetry env activate)
git submodule update --init --recursive          # apps live in a submodule; easy to forget

python -m pytest tests/ -q                        # unit tests (no cluster needed)
python -m pytest -m "not integration"             # skip tests needing a live cluster
python cli.py                                      # human-as-agent REPL: start <problem_id>
python clients/gpt.py                              # baseline agent run
```

Cluster options for full runs: local `kind` (`kind/kind-config-{x86,arm}.yaml`) or Azure via `scripts/terraform/deploy.py`. Most verifier work needs **no** cluster — develop against serialized trace fixtures, not a live run.

Copy `aiopslab/config.yml.example` → `config.yml` (`k8s_host: kind` for local). API keys go in `.env` (`.env.example` is the template). Never commit either.

---

## Architecture — the load-bearing pieces (read the real files before coding against them)

- **Orchestrator** `aiopslab/orchestrator/orchestrator.py` — runs `init_problem → start_problem` (agent loop) → calls the problem's `eval(soln, trace, duration)`.
- **Session** `aiopslab/session.py` — records the full interaction; passed to `eval` as `trace`; `to_dict()` serializes it (this is what `service.py /simulate` returns). **Confirm the turn schema yourself** (`role`/`content`/timestamp keys) — don't assume.
- **ResponseParser** `aiopslab/orchestrator/parser.py` — AST-based extraction of `(api_name, args, kwargs)` from an assistant turn. Reuse it; don't hand-roll a second parser.
- **Actions** `aiopslab/orchestrator/actions/` — APIs exposed to agents, decorated `@action` / `@read` / `@write`. `@read` = no state change (`get_logs`, `get_metrics`, `get_traces`, read-only `exec_shell`); `@write` = mutating (scale/patch/restart/redeploy); `submit` is terminal. **Verify how read/write is discoverable on the callable** (attribute vs. registry) and record it in `NOTES.md`.
- **Tasks** `aiopslab/orchestrator/tasks/{detection,localization,analysis,mitigation}.py` — base `eval` computes default metrics and stores via `self.add_result(name, value)` into `self.results`.
- **Problems** `aiopslab/orchestrator/problems/registry.py` — ~60 registered; each = app + task + fault + workload + evaluator.
- **Fault injectors** `aiopslab/generators/fault/` — `FaultInjector` base uses an `inject_<type>()` / `recover_<type>()` reflection pattern; children: `Symptom` (Chaos Mesh), `K8SOperator` (CR misconfig), `Application`, `OS`, `Otel`, `Virtualization`, `Noop`.

---

## Conventions

- Python ≥3.11, `black`, type-annotate everything, `@dataclass` for records.
- Extend, don't fork: new problem → subclass a Task + register in `registry.py`; new app → metadata JSON + `Application` subclass; new agent → implement `init_context` + `async get_action` + register in `clients/registry.py`.
- **For the verifier specifically:** keep the core (`engine.py`, `policies/`, `model.py`) free of `aiopslab.*` imports so it ports to production traces later. Confine all AIOpsLab coupling to `normalize.py` and the integration snippet. The spec (`trajectory-verifier-spec.md`) is the contract — build what it specifies, not more.

---

## Operating discipline

- **Act on sufficient context.** When this file plus the spec give you enough to proceed, proceed. Don't re-plan settled decisions or narrate options you won't take. Pause only for a real scope change, a destructive/irreversible action, or input only the human can give (e.g., an Anthropic API key).
- **Ground every progress claim in a tool result.** Before saying a step is done, point to a command output, test result, or diff from this session. If a test fails, report it with output. Don't claim completion you can't evidence.
- **Stay in scope.** No unrequested refactors, abstractions, feature flags, backups, or compat shims. Validate at real boundaries (untrusted trace input, policy files); trust AIOpsLab's internal guarantees elsewhere.
- **Verify with fresh-context subagents.** At milestone boundaries, dispatch a separate subagent to check output against the spec's acceptance criteria — fresh eyes beat self-critique. Independent subtasks (e.g., the six deterministic policies) can run as parallel subagents; keep working while they run and intervene if one drifts.
- **Keep `NOTES.md`.** One lesson per entry, one-line summary on top: confirmed schema facts, interface quirks, decisions made under uncertainty and why. Check it before re-investigating.
- **Structured output, not transcribed reasoning.** When a component (e.g., the judge policy) needs a verdict from a model, request strict JSON with a `rationale` *field* — never instruct a model to echo its internal reasoning into the response body (can trigger a `reasoning_extraction` refusal and silent fallback).
- **Effort:** `high` for build/verify milestones; `xhigh` for the LLM-judge design. Long turns are expected — a multi-minute build turn is not a stall. Don't stop early citing context limits; continue to acceptance criteria or a genuine block.

---

## Known gotchas (real, from this repo)

- Chaos Mesh **kernel fault is broken** (upstream bug) — don't build on it.
- `inject_pod_kill` deliberately uses the `pod-failure` action (not `pod-kill`) to stop K8s from instantly recreating the pod.
- Poetry ≥2.0 removed `poetry shell` → use `eval $(poetry env activate)`. Don't `apt install python3-poetry` (outdated, breaks the lockfile).
- Submodules must be initialized or Helm charts/apps are missing. Submodule init can fail under WSL (Windows paths in the worktree `.git`) — run from Git Bash.
- `exec_shell` is **non-interactive** — no `kubectl edit`, no `-f` follow. Use the specific telemetry APIs.
- Some fault injectors (`Virtualization`) need Docker on the machine running AIOpsLab (Mode A vs. Mode B in the Terraform deploy).
- NSG rules from the Terraform config open SSH (22) and K8s API (6443) to `*` by default — restrict with `--allowed-ips` before leaving anything running.

---

## Don't

- Don't modify the Orchestrator loop, the agent interface, or existing `eval` default metrics — the verifier is additive.
- Don't add heavy dependencies; core + rules are stdlib-only. Check the lockfile before adding anything.
- Don't touch problems beyond the two demo wirings (one localization, one mitigation) the spec calls for.
- Don't commit `.env`, `config.yml`, telemetry dirs, or `data/` (all already gitignored — keep it that way).
