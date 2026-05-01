from __future__ import annotations

import json
import sys
from pathlib import Path

from .artifacts import register_evidence
from .gates import evaluate_completion_gate, evaluate_implementation_gate
from .state import HarnessPaths, append_event


def handle_hook_event(repo: Path, event_name: str, payload: dict) -> int:
    paths = HarnessPaths(repo)
    append_event(paths, None, f"hook_{event_name}", {"payload": payload})
    if event_name == "preToolUse":
        return _handle_pre_tool_use(paths, payload)
    if event_name == "postToolUse":
        return _handle_post_tool_use(paths, payload)
    if event_name == "userPromptSubmitted":
        return _handle_user_prompt_submitted(paths, payload)
    if event_name == "agentStop":
        return _handle_agent_stop(paths, payload)
    return 0


def hook_main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) < 2:
        print("usage: harness_v2.events <repo> <event-name>", file=sys.stderr)
        return 2
    repo = Path(args[0]).resolve()
    event_name = args[1]
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        return handle_hook_event(repo, event_name, payload)
    except Exception as exc:
        print(f"harness-v2 hook error: {exc}", file=sys.stderr)
        return 1


def _handle_pre_tool_use(paths: HarnessPaths, payload: dict) -> int:
    tool_name = str(payload.get("tool") or payload.get("tool_name") or "")
    path_value = payload.get("path") or payload.get("file_path")
    paths_to_check = [Path(path_value)] if path_value else []
    if tool_name.lower() in {"edit", "write", "multiedit", "apply_patch"}:
        decision = evaluate_implementation_gate(paths, paths_to_check)
        if decision.decision == "deny":
            print(json.dumps({"decision": "deny", "reason": decision.reason}))
            return 1
    return 0


def _handle_post_tool_use(paths: HarnessPaths, payload: dict) -> int:
    artifact_type = payload.get("artifact_type")
    artifact_path = payload.get("artifact_path")
    if artifact_type and artifact_path:
        register_evidence(paths, str(artifact_type), Path(str(artifact_path)))
    return 0


def _handle_user_prompt_submitted(paths: HarnessPaths, payload: dict) -> int:
    guide = paths.repo / ".github" / "harness-v2" / "AGENTS_GUIDE.md"
    if not guide.exists():
        return 0
    try:
        active_count = _count_active_workflows(paths)
    except Exception:
        return 0
    if active_count == 0:
        # No active workflow — the harness-workflow skill handles passive discovery.
        return 0
    message = (
        "[harness-v2] Before acting, read "
        f"{guide.relative_to(paths.repo)} and every SKILL.md under "
        ".github/skills/. Implementation edits to source files are gated by "
        "the harness; create and register evidence artifacts first when the "
        "task is non-trivial. For new-peer tasks, build the peer parity "
        "matrix in context-map.md before writing code."
    )
    if active_count > 1:
        message = (
            "[harness-v2] Multiple active workflows detected. "
            "Run `harness-v2 status` to list them and close or select one "
            "before proceeding. " + message
        )
    print(json.dumps({"systemMessage": message}))
    return 0


def _count_active_workflows(paths: HarnessPaths) -> int:
    if not paths.active_workflows.exists():
        return 0
    import json as _json
    registry = _json.loads(paths.active_workflows.read_text(encoding="utf-8"))
    return len(registry.get("active", []))


def _handle_agent_stop(paths: HarnessPaths, payload: dict) -> int:
    decision = evaluate_completion_gate(paths)
    if decision.decision == "deny":
        parts = [decision.reason]
        if decision.missing:
            parts.append("still needed: " + ", ".join(decision.missing))
        print(json.dumps({"decision": "block", "reason": " — ".join(parts)}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(hook_main())

