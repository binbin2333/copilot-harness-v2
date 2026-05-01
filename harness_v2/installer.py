from __future__ import annotations

import json
import stat
from pathlib import Path

from .config import write_default_config


SKILLS: dict[str, str] = {
    "requirements-summary": """# Requirements Summary

Produce `.github/harness-v2/state/workflows/<workflow-id>/artifacts/requirements-summary.md` covering: user request verbatim, task type (feature/bugfix/refactor), explicit constraints (allowed/forbidden paths, tools, references), unknowns, and acceptance criteria.

Acceptance criteria must be testable. Avoid restating the request as the criterion. Examples of good criteria: "package X compiles under tag Y", "test Z passes", "command W returns expected JSON".

Register it with:

```bash
harness-v2 evidence add requirements-summary <path>
```
""",
    "context-map": """# Context Map

Before writing code, build a map of the existing codebase relevant to the change. Cover:

1. Entry points, registries, and call sites the new code must plug into or satisfy.
2. Existing implementations of the same kind (peers, siblings, prior versions). For each, record file path and one-line description.
3. Interfaces and contracts the new code must implement. List required and optional; for optional ones note which existing implementations cover them.
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
    "scope-freeze": """# Scope Freeze

Define in/out scope, expected changed files (concrete paths), public contracts, compatibility constraints, test strategy, rollback/migration concerns, assumptions.

For "add a new peer" tasks (new agent / provider / platform / plugin), the default in-scope baseline is parity with the median peer along these axes:

- Required interface coverage: 100%.
- Optional interface coverage: every interface implemented by at least half of existing peers.
- Configuration surface: every option that the median peer exposes.
- Wiring: registry init() call, build-tagged plugin file, Makefile/build-list update if peers do, config example block if peers have one.
- Tests: at least the same test categories as the median peer (constructor, options parsing, all switchers, lifecycle, error paths, mocked-IO behaviors). Test LoC should be the same order of magnitude as the median peer's tests.

Out-of-scope items must be listed explicitly.

Register it with:

```bash
harness-v2 evidence add scope-freeze <path>
```
""",
    "design-plan": """# Design Plan

Describe the implementation approach concretely:

- File layout (paths, responsibilities).
- Data flow from external SDK / IO into core types.
- Mapping tables: external SDK type/event/enum -> internal core type/event/enum, one row per mapping. Mark unsupported mappings explicitly.
- State machines (session lifecycle, permission flow, streaming dedup) as ordered steps.
- Concurrency model (goroutines, channels, mutexes; what each protects).
- Error handling strategy.
- Backwards-compatibility considerations.
- Reuse: which core helpers are reused vs. reimplemented.
- Alternatives considered and why rejected.

Register it with:

```bash
harness-v2 evidence add design <path>
```
""",
    "sdk-mapping-discipline": """# SDK Mapping Discipline

When wrapping an external SDK:

1. Read the SDK's public type files top to bottom once. Note: client constructor, session lifecycle, event union shape, request/response types, attachment types, capability/usage types, error types.
2. Build a single mapping table in the design doc, one row per SDK type/event/enum -> internal type/event/enum. Mark unsupported fields with TODO + reason. Do not silently drop fields.
3. Prefer SDK-provided enums and helper constructors over string literals.
4. Wrap blocking SDK calls in a goroutine + context-aware timeout when the call may stall.
5. Forward all SDK callbacks/handlers when both create and resume paths exist; resume must register the same handlers as create.
6. When the SDK exposes a structured field (usage, quota, model list, skills) prefer it over parsing free text.
7. Stub the SDK in tests via narrow internal interfaces; never call real network or CLI binaries from unit tests.
""",
    "interactive-shell-discipline": """# Interactive Shell Discipline

Avoid commands that block or loop unproductively in agent shells:

- Do not `cat` files you did not just write in the same session unless you know the size.
- Use `head -N` / `sed -n 'a,bp'` / `wc -l` for inspection; never page interactively.
- Disable pagers explicitly: `git --no-pager`, `| cat`, `--no-color`.
- Use grep with `-r`, `-n`, and a `--include` glob; do not loop "search for X then variant of X then variant of X" more than three times. If the third grep does not find it, switch strategy (read the file table of contents, ask `go doc`, read examples).
- For long-running builds/tests, use `tail -N` on log files instead of streaming.
- Never tail/cat from `/tmp/` paths you did not create in this session.
""",
    "test-depth-parity": """# Test Depth Parity

Tests for a new peer should match the categories and depth of existing peers:

Categories that almost always apply:
- Constructor / `New` defaults.
- Constructor option parsing for every option (one test per option, including invalid input fall-back).
- Each switcher (model/mode/effort/workdir/provider/ask-user-strategy) read-after-write and edge cases.
- Provider list copy-on-set semantics (mutating caller slice must not affect agent).
- Optional interface methods: assert presence and basic behavior.
- Doctor info (binary name, display name).

Session-level categories when relevant:
- Send / SendMidTurn happy path.
- Permission allow/deny mapping including unsupported result fields documented.
- Pending permission/user-input drain on Close.
- Event mapping for every SDK event the design covers (one test per row in the mapping table).
- Streaming dedup / buffering when the design lists it.
- Attachment translation for each attachment kind.
- Usage report parsing.

Test LoC should be the same order of magnitude as median peer test LoC. Use small fakes for the SDK; never depend on a real CLI binary or network.
""",
    "verification-report": """# Verification Report

Record configured commands, command outputs (final lines), failures and fixes, and final passing markers. Include:

- Targeted package tests for changed packages.
- Broad test (with the project's accepted build tags) so regressions in unchanged packages are surfaced.
- Build of any binary that uses the new code.
- Any deliberately skipped suites with rationale.

Register it with:

```bash
harness-v2 evidence add verification-report <path>
```
""",
    "review-lens": """# Review Lens

Review correctness, architecture, compatibility, test adequacy, security, maintainability, documentation. For "new peer" reviews, additionally check the parity matrix produced under `peer-parity-checklist`: every required interface implemented, every >=50% optional interface implemented or documented as out-of-scope, every wiring step replicated, test depth matches.

Register synthesized findings with:

```bash
harness-v2 evidence add review-report <path>
```
""",
    "memory-correction": """# Memory Correction

When the user corrects the agent or an agent-caused failure occurs, record symptom, root cause, prevention rule, retrieval keys, severity, and status:

```bash
harness-v2 memory record-correction --symptom "..." --root-cause "..." --prevention "..." --key key
```

Common prevention rules to seed memory with:
- Prefer SDK enum constants over string literals.
- Forward all SDK handlers in resume path, not only create path.
- Wrap blocking SDK calls in goroutine + context timeout.
- For new peer additions, build the peer parity matrix before writing code.
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

sys.path.insert(0, "{source_root}")

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

- Follow `interactive-shell-discipline` skill: no interactive cats, no
  unbounded grep loops, always disable pagers.
- Prefer SDK enums and helpers over string literals; document any unsupported
  fields inline rather than silently dropping.
- For new peers, build the peer parity matrix BEFORE writing code; iterate the
  matrix instead of iterating the code.

## Stopping criterion

Stop only when:

- `harness-v2 status` shows `"invalidated": []` and all expected artifacts are
  registered.
- The verification report's broad test command passes.
- The review report has no open significant findings.

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
        source_root = str(Path(__file__).resolve().parents[1])
        path.write_text(HOOK_SCRIPT.format(event_name=event_name, source_root=source_root), encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        written.append(path)
    return written


def _write_skills(repo: Path) -> list[Path]:
    written: list[Path] = []
    for name, content in SKILLS.items():
        path = repo / ".github" / "skills" / name / "SKILL.md"
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
