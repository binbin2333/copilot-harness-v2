from __future__ import annotations

import json
import shutil
import stat
from pathlib import Path

from .config import write_default_config


SKILLS: dict[str, str] = {
    "harness-workflow": """---
name: harness-workflow
description: Starts the harness-v2 evidence-gated workflow. Invoke this skill explicitly to begin a tracked implementation task.
allowed-tools: shell
---

# Harness v2 Workflow

Invoking this skill starts a harness-v2 workflow. Run the following command first:

```bash
harness-v2 start <type> '<short title>'
```

`<type>` is one of: `feature`, `bugfix`, `refactor`, `chore`.

Then proceed through the phases in order. Read `.github/harness-v2/AGENTS_GUIDE.md` for the full guide.
`harness-v2 status` derives `current_phase` from the latest registered evidence and rewinds to the earliest downstream phase that must be redone when upstream artifacts change.

## Phases

0. **Clarification** — use the `/clarification-memo` skill to ask the user any ambiguous questions with `ask_user` before planning.
1. **Requirements summary** → `harness-v2 evidence add requirements-summary <path>`
2. **Context map** → `harness-v2 evidence add context-map <path>`
3. **Scope freeze** (write `verification.commands` to `.github/harness-v2/config.yaml`) → `harness-v2 evidence add scope-freeze <path>`
4. **Design plan** → `harness-v2 evidence add design <path>`
5. **Implementation** — harness blocks file writes until phases 2–4 are current.
6. **Verification report** (run every command in `verification.commands`) → `harness-v2 evidence add verification-report <path>`
7. **Review report** → `harness-v2 evidence add review-report <path>`

## Completion gate

`agentStop` and `git commit` are blocked by the harness until:
- `harness-v2 status` shows `"current_phase": "done"`,
- `verification-report` and `review-report` are registered, and
- `harness-v2 status` shows `"invalidated": []`.
""",
    "clarification-memo": """---
name: clarification-memo
description: Use before writing any planning document when the user's request has ambiguities. Ask the user clarifying questions interactively using ask_user, then document the answers.
---

# Clarification Memo

Before writing requirements or planning documents, surface and resolve ambiguities in the user's request.

## Process

1. Read the user's request carefully. List every requirement that has two or more plausible interpretations.
2. For each ambiguity, call `ask_user` with a focused, single question.
   - Ask **one question at a time** — do not bundle multiple questions.
   - Provide **multiple-choice options** whenever there are clear alternatives (the tool adds a freeform fallback automatically).
   - Wait for the answer before asking the next question.
3. If the requirements are completely unambiguous, write a single line: "No open questions — requirements are clear: [brief reason]."

## Output

Write a short memo (no template required) covering:
- Each question asked and the answer received.
- Any assumption made when the user was unavailable.
- One-sentence confirmed scope.

Register it with:

```bash
harness-v2 evidence add clarification-memo <path>
```
""",
    "requirements-summary": """---
name: requirements-summary
description: 'Write a requirements summary artifact for the active harness workflow. Use when starting a new task to document the user request, constraints, and testable acceptance criteria.'
---

# Requirements Summary

Produce `.github/harness-v2/state/workflows/<workflow-id>/artifacts/requirements-summary.md` covering: user request verbatim, task type (feature/bugfix/refactor), explicit constraints (allowed/forbidden paths, tools, references), unknowns, and acceptance criteria.

Acceptance criteria must be testable. Avoid restating the request as the criterion. Examples of good criteria: "package X compiles under tag Y", "test Z passes", "command W returns expected JSON".

Register it with:

```bash
harness-v2 evidence add requirements-summary <path>
```
""",
    "context-map": """---
name: context-map
description: 'Build a context map of the existing codebase relevant to the current change. Use before writing code to discover entry points, peer implementations, interfaces, configs, and tests.'
---

# Context Map

Before writing code, build a map of the existing codebase relevant to the change. Cover:

1. Entry points, registries, and call sites the new code must plug into or satisfy.
2. Existing implementations of the same kind (peers, siblings, prior versions).
   - **Inspect ALL existing peers** — list every file in the peer directory (`agent/`, `platform/`, etc.) and read each one. Do not sample a subset. The peer list sets the denominator for the parity calculation in the next step.
   - For each peer, record file path and one-line description.
3. Interfaces and contracts the new code must implement.
   - **Start from the interface definitions file** (e.g. `core/interfaces.go` or equivalent) to enumerate ALL defined optional interfaces. Do NOT rely on reading peer code alone to discover interfaces — peers may only implement a subset.
   - For each optional interface found in the definitions file, check which of the ALL peers implement it and record that in a matrix with one column per interface. A matrix with fewer columns than the actual number of defined optional interfaces is incomplete.
4. Public types the new code consumes (events, messages, configs, sessions, permissions, etc.).
5. Configuration surfaces: example config files, schema, env vars.
6. Build/wiring files (plugin files, Makefile entries, build tags).
7. Tests: how existing siblings are tested; rough test count and categories.
8. External references the user explicitly allowed (SDKs, vendor docs).
9. Unknowns and risks.

Register it with:

```bash
harness-v2 evidence add context-map <path>
```
""",
    "scope-freeze": """---
name: scope-freeze
description: 'Define in/out scope, expected changed files, test strategy, and verification commands for the active harness workflow. Use after context-map and before writing code.'
---

# Scope Freeze

Define in/out scope, expected changed files (concrete paths), public contracts, compatibility constraints, test strategy, rollback/migration concerns, assumptions.

For "add a new peer" tasks (new agent / provider / platform / plugin), the default in-scope baseline is parity with the median peer along these axes:

- Required interface coverage: 100%.
- Optional interface coverage: every interface implemented by at least half of existing peers.
- Configuration surface: every option that the median peer exposes.
- Wiring: registry init call, build system integration if peers do it, config example block if peers have one.
- Tests: at least the same test categories as the median peer (constructor, options parsing, all switchers, lifecycle, error paths, mocked-IO behaviors). Test LoC should be the same order of magnitude as the median peer's tests.

Out-of-scope items must be listed explicitly.

**Verification commands**: At the end of the scope-freeze document, list every shell command that constitutes acceptance. Then write those exact commands into `.github/harness-v2/config.yaml` under `verification.commands` so the harness knows the full test bar. Example fragment:

```yaml
verification:
  commands:
    - <test command for this project>
    - <build or compile command>
```

Register it with:

```bash
harness-v2 evidence add scope-freeze <path>
```
""",
    "design-plan": """---
name: design-plan
description: 'Write a design plan artifact covering file layout, data flow, mapping tables, state machines, and error handling. Use after scope-freeze and before implementation.'
---

# Design Plan

Describe the implementation approach concretely:

- File layout (paths, responsibilities).
- Data flow from external dependencies / IO into internal types.
- Mapping tables: external type/event/enum -> internal type/event/enum, one row per mapping. Mark unsupported mappings with TODO + reason. Do not silently drop fields.
- State machines (session lifecycle, permission flow, streaming) as ordered steps.
- Concurrency model (threads, async tasks, actors, locks — what each protects and why).
- Error handling strategy.
- Backwards-compatibility considerations.
- Reuse: which existing helpers are reused vs. reimplemented.
- Alternatives considered and why rejected.

When integrating an external library or SDK:
- Read its public type files top to bottom once before writing any code.
- Prefer library-provided enums and helper constructors over string literals.
- Wrap blocking library calls with an appropriate timeout mechanism for the language/framework when the call may stall.
- When both a "create" and a "resume/reconnect" path exist, forward all callbacks/handlers in both; never register handlers only on the create path.
- Stub the library in tests via narrow internal interfaces; never call real network endpoints or external binaries from unit tests.

Register it with:

```bash
harness-v2 evidence add design <path>
```
""",
    "verification-report": """---
name: verification-report
description: 'Record verification command outputs and test results for the active harness workflow. Use after implementation to prove all acceptance criteria pass.'
---

# Verification Report

Record configured commands, command outputs (final lines), failures and fixes, and final passing markers. Include:

- Every command listed under `verification.commands` in `.github/harness-v2/config.yaml` (these were agreed at scope-freeze; run them all).
- Targeted tests for changed modules/packages.
- Broad test run so regressions in unchanged modules are surfaced.
- Build or compile step for any binary/artifact that uses the new code.
- Any deliberately skipped suites with rationale.

Test categories to cover (adapt to the task; skip inapplicable ones):
- Constructor / factory defaults and option parsing (one case per option, including invalid inputs).
- Each switchable option — read-after-write and edge cases.
- Copy-on-set semantics for collection options (mutating caller collection must not affect stored state).
- Optional interface methods: assert presence and basic behavior.
- Session lifecycle: send happy path, error path, close/drain.
- Event mapping: one test per row in the design-plan mapping table.
- Permission / user-input bridge: allow and deny paths, drain on close.
- Attachment translation for each attachment kind handled.

Use small in-process fakes for external dependencies; never call real networks or CLI binaries from unit tests.

Register it with:

```bash
harness-v2 evidence add verification-report <path>
```
""",
    "review-lens": """---
name: review-lens
description: 'Review correctness, architecture, test adequacy, security, and maintainability of the implementation. Use after verification to produce a review-report artifact.'
---

# Review Lens

Review correctness, architecture, compatibility, test adequacy, security, maintainability, documentation.

Checklist:
- All required interfaces / contracts implemented.
- Optional interfaces implemented where the design committed to them; gaps documented as out-of-scope with rationale.
- All wiring steps replicated (registry init, build system integration, config example).
- Test categories from the verification-report skill are covered; no category silently omitted.
- No silent field drops in external-to-internal mappings (check design-plan mapping table).
- Blocking external calls wrapped with appropriate timeout mechanism.
- Both create and resume/reconnect paths register the same handlers.
- No real network or binary calls in unit tests.

Register synthesized findings with:

```bash
harness-v2 evidence add review-report <path>
```
""",
    "memory-correction": """---
name: memory-correction
description: 'Record a failure or correction memory entry when the user corrects the agent or an agent-caused failure occurs. Use to persist lessons learned for future retrieval.'
---

# Memory Correction

When the user corrects the agent or an agent-caused failure occurs, record symptom, root cause, prevention rule, retrieval keys, severity, and status:

```bash
harness-v2 memory record-correction --symptom "..." --root-cause "..." --prevention "..." --key key
```
""",
}


HOOK_CONFIG = {
    "version": 1,
    "hooks": {
        "userPromptSubmitted": ".github/harness-v2/hooks/user_prompt_submitted.py",
        "preToolUse": ".github/harness-v2/hooks/pre_tool_use.py",
        "postToolUse": ".github/harness-v2/hooks/post_tool_use.py",
        "agentStop": ".github/harness-v2/hooks/agent_stop.py",
        "subagentStop": ".github/harness-v2/hooks/subagent_stop.py",
        "sessionEnd": ".github/harness-v2/hooks/session_end.py",
    },
}


HOOK_SCRIPT = """#!/usr/bin/env python3
from pathlib import Path
import sys

RUNTIME_ROOT = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(RUNTIME_ROOT))

from harness_v2.events import hook_main

EVENT_NAME = "{event_name}"

if __name__ == "__main__":
    repo = Path(__file__).resolve().parents[3]
    raise SystemExit(hook_main([str(repo), EVENT_NAME, *sys.argv[1:]]))
"""


GITIGNORE_BLOCK = """# copilot-harness-v2 runtime
.github/harness-v2/state/
.github/harness-v2/memory/
.github/harness-v2/logs/
.github/harness-v2/runs/
"""


AGENTS_GUIDE = """# Harness v2 Agents Guide

This repository has copilot-harness-v2 installed. Follow this workflow on every
non-trivial task before editing source files. The harness will gate writes to
non-harness paths until evidence is in place.

## Required reading order

1. This guide (`.github/harness-v2/AGENTS_GUIDE.md`).
2. Every `SKILL.md` under `.github/skills/`.
3. The active workflow state under
   `.github/harness-v2/state/workflows/<id>/state.json` (use
   `harness-v2 status` if uncertain).

## Workflow

0. **Clarification memo** — before writing any planning document, identify
   ambiguities. Ask the user if anything is unclear. Document questions and
   answers (or "no open questions") and register with
   `harness-v2 evidence add clarification-memo <path>`.
1. **Requirements summary** — write the user's request, constraints, allowed
   references, forbidden references, and testable acceptance criteria. Save to
   `.../artifacts/requirements-summary.md` and register with
   `harness-v2 evidence add requirements-summary <path>`.
2. **Context map** — for any task that adds a new peer to a registry-like
   codebase (new agent, provider, plugin, platform, command), follow the
   `peer-parity-checklist` skill and capture the peer matrix here. Otherwise
   capture the candidate files, callers, configs, and tests. Register with
   `harness-v2 evidence add context-map <path>`.
3. **Scope freeze** — define in-scope, out-of-scope, expected changed files,
   compatibility constraints, test strategy, assumptions. For new-peer tasks
   the default parity bar from `peer-parity-checklist` applies. Register.
4. **Design plan** — file layout, mapping tables, state machines, concurrency
   model, error handling. Use `sdk-mapping-discipline` when wrapping an SDK.
   Register.
5. **Implementation** — write code. The harness allows writes inside
   `.github/harness-v2/`, `.github/skills/`, `.github/hooks/` always; for other
   paths the gate requires the design evidence above.
6. **Verification report** — record commands and final outputs. Use targeted
   tests for changed packages plus the project's accepted broad-test command.
   Register.
7. **Review report** — review correctness, parity, tests, security,
   maintainability. Address any findings, then register.

## Tool discipline

Avoid commands that block or loop unproductively:

- Do not `cat` files you did not just write unless you know the size; use `head -N` / line-range reads / `wc -l`.
- Disable pagers explicitly: `git --no-pager`, `| cat`, `--no-color`.
- Do not loop the same grep search more than three times. If the third attempt fails, switch strategy: read the file's table of contents, use the language's documentation tool, or read examples.
- For long-running builds/tests use `tail -N` on log files instead of streaming.
- Prefer library enums and helpers over string literals; document unsupported fields inline rather than silently dropping.

## Stopping criterion

Stop only when:

- `harness-v2 status` shows `"current_phase": "done"`.
- `harness-v2 status` shows `"invalidated": []` and all expected artifacts are
  registered.
- The verification report covers every command listed under
  `verification.commands` in `.github/harness-v2/config.yaml` and all pass.
- The review report has no open significant findings.

The harness enforces this at `agentStop` and before `git commit`: it will block
termination or commit if the workflow has not reached `done`, if
`verification-report` or `review-report` is not registered, or if any phase is
still in the `invalidated` list. You must resolve those before the session can
end.

## What this guide intentionally does NOT contain

It does not list specific features for any specific task. Feature scope must be
derived from the user's request plus the peer matrix and SDK reading you do
during the workflow. Do not look for hidden checklists; build them yourself
through the skills.
"""


def install(repo: Path) -> list[Path]:
    repo = repo.resolve()
    repo.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    written.append(write_default_config(repo))
    written.extend(_write_runtime(repo))
    written.extend(_ensure_runtime_dirs(repo))
    written.append(_write_hook_config(repo))
    written.extend(_write_hooks(repo))
    written.extend(_write_skills(repo))
    written.append(_write_agents_guide(repo))
    written.append(_update_gitignore(repo))
    return written


def _ensure_runtime_dirs(repo: Path) -> list[Path]:
    paths = [
        repo / ".github" / "harness-v2" / "state" / "workflows",
        repo / ".github" / "harness-v2" / "memory",
        repo / ".github" / "harness-v2" / "logs",
        repo / ".github" / "harness-v2" / "runs",
    ]
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
    active = repo / ".github" / "harness-v2" / "state" / "active-workflows.json"
    if not active.exists():
        active.write_text(json.dumps({"version": 1, "active": []}, indent=2) + "\n", encoding="utf-8")
    for name in ("failures.jsonl", "corrections.jsonl", "lessons.jsonl", "project-facts.jsonl"):
        path = repo / ".github" / "harness-v2" / "memory" / name
        path.touch(exist_ok=True)
        paths.append(path)
    paths.append(active)
    return paths


def _write_hook_config(repo: Path) -> Path:
    path = repo / ".github" / "hooks" / "harness-v2.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(HOOK_CONFIG, indent=2) + "\n", encoding="utf-8")
    return path


def _write_hooks(repo: Path) -> list[Path]:
    hooks_dir = repo / ".github" / "harness-v2" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    names = {
        "user_prompt_submitted.py": "userPromptSubmitted",
        "pre_tool_use.py": "preToolUse",
        "post_tool_use.py": "postToolUse",
        "agent_stop.py": "agentStop",
        "subagent_stop.py": "subagentStop",
        "session_end.py": "sessionEnd",
    }
    written: list[Path] = []
    for filename, event_name in names.items():
        path = hooks_dir / filename
        path.write_text(HOOK_SCRIPT.format(event_name=event_name), encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        written.append(path)
    return written


def _write_runtime(repo: Path) -> list[Path]:
    source_dir = Path(__file__).resolve().parent
    runtime_dir = repo / ".github" / "harness-v2" / "runtime" / "harness_v2"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename in ("__init__.py", "config.py", "events.py", "gates.py", "state.py"):
        source = source_dir / filename
        target = runtime_dir / filename
        shutil.copy2(source, target)
        written.append(target)
    return written


def _write_skills(repo: Path) -> list[Path]:
    skills_dir = repo / ".github" / "skills"
    # Remove any skill directories not in the current SKILLS set so stale skills
    # from previous harness versions don't persist alongside the new ones.
    if skills_dir.exists():
        import shutil
        for existing in skills_dir.iterdir():
            if existing.is_dir() and existing.name not in SKILLS:
                shutil.rmtree(existing)
    written: list[Path] = []
    for name, content in SKILLS.items():
        path = skills_dir / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


def _write_agents_guide(repo: Path) -> Path:
    path = repo / ".github" / "harness-v2" / "AGENTS_GUIDE.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(AGENTS_GUIDE, encoding="utf-8")
    return path


def _update_gitignore(repo: Path) -> Path:
    path = repo / ".gitignore"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if GITIGNORE_BLOCK not in existing:
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        path.write_text(existing + prefix + GITIGNORE_BLOCK, encoding="utf-8")
    return path
