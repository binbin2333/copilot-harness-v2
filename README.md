# copilot-harness-v2

`copilot-harness-v2` adds a lightweight workflow guardrail for Copilot CLI tasks inside a repository.

It provides:

- a CLI: `harness-v2`
- a repo-local wrapper: `./.github/harness-v2/bin/harness-v2`
- repo-local workflow state in `.github/harness-v2/state/`
- artifact registration ledgers in `.github/harness-v2/state/workflows/<id>/artifact-registrations.jsonl`
- evidence registration for planning, verification, and review artifacts
- implementation, verification, and completion gates
- fail-fast hook enforcement and protected state files
- a `doctor` command for install and workflow integrity checks
- workflow memory stored as JSONL files

## Install

```bash
uv sync
```

## Basic usage

```bash
uv run harness-v2 install .
./.github/harness-v2/bin/harness-v2 --repo . doctor
./.github/harness-v2/bin/harness-v2 --repo . start feature auth-flow
./.github/harness-v2/bin/harness-v2 --repo . status
./.github/harness-v2/bin/harness-v2 --repo . gate implementation --path harness_v2/cli.py
./.github/harness-v2/bin/harness-v2 --repo . gate completion
```

`state.json` is now treated as a cache. The authoritative workflow facts come from:

- `.github/harness-v2/state/workflows/<id>/artifact-registrations.jsonl` for evidence registrations
- `.github/harness-v2/state/workflows/<id>/events.jsonl` for append-only workflow and hook events

When `state.json` drifts from the registration ledger, the harness reconciles it automatically on load.

## v3 assumption/evidence workflow

v3 moves completion from "artifact exists" to "important uncertainty is closed".
Artifacts may include structured markdown blocks:

```markdown
<!-- harness:v3:start -->
task_classification:
  level: 2
  labels: [behavior, state]
  rationale: Changes runtime behavior.
assumptions:
  - id: A1
    statement: Existing ownership model supports the new state.
    risk: high
    status: assumed
    falsification: Trace state creation and reset paths.
    evidence_required: [E1]
    evidence: []
    owner: agent
evidence:
  - id: E1
    type: test
    supports: [A1]
    result: pass
    source: pytest -q
<!-- harness:v3:end -->
```

Stable IDs:

| Prefix | Meaning |
|---|---|
| `A*` | assumptions |
| `D*` | decisions |
| `E*` | evidence |
| `W*` | waivers / accepted risks |
| `F*` | deferred follow-ups |

## v3 config

Defaults are strict for any task that enters the harness workflow: v3 gates are
enabled and enforced. If a task is too small for the full v3 process, do not
start a harness workflow for it.

Before implementation, every harness workflow must include task classification
and an A*/D*/E* assumption/evidence ledger. Completion enforces parse errors,
unresolved high-risk assumptions, skipped evidence without waivers, invalid
deferred items, and invalid review verdicts.

```yaml
v3:
  enable_v3_gates: true
  v3_gate_mode: enforce
  require_assumption_resolution: true
  require_skip_waivers: true
  require_task_classification: true
  allow_pass_with_gaps: false
```

In `warn` mode, issues are reported but do not deny. In `enforce` mode, parse
errors, unresolved high-risk assumptions, skipped evidence without waivers,
invalid deferred items, and invalid review verdicts block completion.

`harness-v2 status` includes parsed v3 state: task classification, unresolved
high-risk assumptions, skipped evidence, waivers, deferred items, review verdict,
gate mode, and parse errors.
