from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .artifacts import register_evidence
from .config import load_config
from .gates import GateDecision, evaluate_artifact_write_gate, evaluate_completion_gate, evaluate_implementation_gate
from .state import HarnessPaths, append_event, artifact_location_for_path, is_protected_state_path, resolve_repo_path


GIT_COMMIT_RE = re.compile(r"\bgit\s+commit\b", re.IGNORECASE)
WRITE_TOOL_NAMES = {"create", "edit", "write", "multiedit", "apply_patch"}
PATH_KEY_NAMES = {
    "path",
    "paths",
    "file_path",
    "filepath",
    "file_paths",
    "new_path",
    "old_path",
    "target_path",
    "targetpath",
}
STATE_WRITE_HINTS = (">", ">>", "tee ", "sed -i", "python -c", "python3 -c", "perl -", "node -e", "mv ", "cp ", "touch ")

# Patterns for detecting file-creating redirects in shell commands.
# We look for `> path` or `>> path` that target artifact directories.
_REDIRECT_RE = re.compile(r"(?:^|[^>])>{1,2}\s*[\"']?([^\s\"'|;&]+)")
_HEREDOC_RE = re.compile(r"cat\s*>\s*[\"']?([^\s\"'<|;&]+)")


@dataclass(frozen=True)
class BlockingDecision:
    hook_name: str
    gate: str
    reason: str
    missing: list[str]
    workflow_id: str | None

    @property
    def formatted_reason(self) -> str:
        return _format_reason(self.reason, self.missing)


def handle_hook_event(repo: Path, event_name: str, payload: dict) -> int:
    paths = HarnessPaths(repo)
    append_event(paths, None, f"hook_{event_name}", {"payload": payload})
    if event_name == "permissionRequest":
        return _handle_permission_request(paths, payload)
    if event_name == "preToolUse":
        return _handle_pre_tool_use(paths, payload)
    if event_name == "postToolUse":
        return _handle_post_tool_use(paths, payload)
    if event_name == "postToolUseFailure":
        return _handle_post_tool_use_failure(paths, payload)
    if event_name == "userPromptSubmitted":
        return _handle_user_prompt_submitted(paths, payload)
    if event_name in {"agentStop", "subagentStop"}:
        return _handle_stop(paths, event_name)
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


def _handle_permission_request(paths: HarnessPaths, payload: dict) -> int:
    config = load_config(paths.repo)
    if not config.fail_fast:
        return 0
    if decision := _blocking_decision(paths, payload, "permissionRequest"):
        _append_gate_denied_event(paths, payload, decision)
        _emit_permission_request("deny", decision.formatted_reason, interrupt=True)
    return 0


def _handle_pre_tool_use(paths: HarnessPaths, payload: dict) -> int:
    if decision := _blocking_decision(paths, payload, "preToolUse"):
        _append_gate_denied_event(paths, payload, decision)
        _emit_pre_tool_decision("deny", decision.formatted_reason)
    return 0


def _handle_post_tool_use(paths: HarnessPaths, payload: dict) -> int:
    config = load_config(paths.repo)
    if not config.auto_register_artifacts:
        return 0
    seen: set[str] = set()
    for candidate_path in _extract_candidate_paths(paths, payload):
        location = artifact_location_for_path(paths, candidate_path)
        if location is None:
            continue
        if str(location.path) in seen or not location.path.exists():
            continue
        seen.add(str(location.path))
        try:
            registration = register_evidence(paths, location.artifact_type, location.path, location.workflow_id)
            append_event(
                paths,
                location.workflow_id,
                "artifact_auto_registered",
                {
                    "artifact_type": location.artifact_type,
                    "path": str(location.path.relative_to(paths.repo)),
                    "version": registration.version,
                },
            )
        except Exception as exc:
            append_event(
                paths,
                location.workflow_id,
                "artifact_auto_registration_failed",
                {"artifact_type": location.artifact_type, "path": str(location.path.relative_to(paths.repo)), "error": str(exc)},
            )
    return 0


def _handle_post_tool_use_failure(paths: HarnessPaths, payload: dict) -> int:
    tool_name = _extract_tool_name(payload)
    error_text = str(payload.get("error") or payload.get("tool_error") or "tool execution failed")
    append_event(paths, None, "tool_failure", {"tool_name": tool_name, "error": error_text})
    if load_config(paths.repo).fail_fast:
        print(json.dumps({"additionalContext": f"[harness-v2] {tool_name} failed: {error_text}"}))
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
        return 0
    config = load_config(paths.repo)
    message = (
        "[harness-v2] Before acting, read "
        f"{guide.relative_to(paths.repo)} and every SKILL.md under .github/skills/. "
        "Implementation edits to source files are gated by the harness; create and register evidence artifacts first. "
        "Clarification now requires either an ask-user interaction or an explicit user waiver. "
        "Scope-freeze now requires a user-confirmed verification plan; do not guess verification.commands."
    )
    if config.require_verification_commands and not config.verification_commands:
        message += " Define verification.commands in .github/harness-v2/config.yaml before scope-freeze or implementation."
    if active_count > 1:
        message = (
            "[harness-v2] Multiple active workflows detected. Run `./.github/harness-v2/bin/harness-v2 status` "
            "to select one before proceeding. " + message
        )
    print(json.dumps({"systemMessage": message}))
    return 0


def _handle_stop(paths: HarnessPaths, hook_name: str) -> int:
    decision = evaluate_completion_gate(paths)
    if decision.decision == "deny":
        blocked = BlockingDecision(hook_name, decision.gate, decision.reason, decision.missing, decision.workflow_id)
        _append_gate_denied_event(paths, {}, blocked)
        _emit_stop_decision("block", blocked.formatted_reason)
    elif decision.decision == "warn":
        warning = BlockingDecision(hook_name, decision.gate, decision.reason, decision.missing, decision.workflow_id)
        _append_gate_warning_event(paths, {}, warning)
        _emit_stop_decision("warn", warning.formatted_reason)
    return 0


def _blocking_decision(paths: HarnessPaths, payload: dict, hook_name: str) -> BlockingDecision | None:
    tool_name = _extract_tool_name(payload)
    candidate_paths = _extract_candidate_paths(paths, payload)
    command_text = _extract_command_text(payload)
    config = load_config(paths.repo)

    if config.protect_state_files:
        protected = [path for path in candidate_paths if is_protected_state_path(paths, path)]
        if protected:
            relative = ", ".join(str(resolve_repo_path(paths, path).relative_to(paths.repo)) for path in protected)
            return BlockingDecision(
                hook_name,
                "protected-state",
                f"direct edits to harness state are forbidden; use registered evidence or the repo-local harness CLI instead ({relative})",
                [],
                _workflow_id_from_paths(paths, protected),
            )
        if _looks_like_state_write(command_text):
            return BlockingDecision(
                hook_name,
                "protected-state",
                "direct writes to .github/harness-v2/state are forbidden; use registered evidence or the repo-local harness CLI instead",
                [],
                _workflow_id_from_paths(paths, candidate_paths),
            )

    if tool_name in WRITE_TOOL_NAMES:
        artifact_targets = [path for path in candidate_paths if artifact_location_for_path(paths, path) is not None]
        # Derive the workflow context from the paths being written; this lets the
        # gates resolve the correct workflow even when multiple are active.
        path_workflow_id = _workflow_id_from_paths(paths, candidate_paths)
        if artifact_targets:
            decision = evaluate_artifact_write_gate(paths, artifact_targets, workflow_id=path_workflow_id)
        else:
            decision = evaluate_implementation_gate(paths, candidate_paths, workflow_id=path_workflow_id)
        if decision.decision == "deny":
            return _to_blocking_decision(hook_name, decision)

    # Detect bash/shell commands that redirect output to artifact or source paths.
    # This closes the gap where `cat > artifacts/foo.md << 'EOF'` bypasses the
    # create/edit tool hooks entirely.
    if tool_name in {"bash", "shell", "terminal"} and command_text:
        redirect_paths = _extract_redirect_paths(paths, command_text)
        # Only gate redirects targeting paths inside the repo.
        redirect_paths = [p for p in redirect_paths if _is_relative_to_repo(paths, p)]
        if redirect_paths:
            artifact_targets = [p for p in redirect_paths if artifact_location_for_path(paths, p) is not None]
            path_workflow_id = _workflow_id_from_paths(paths, redirect_paths)
            if artifact_targets:
                decision = evaluate_artifact_write_gate(paths, artifact_targets, workflow_id=path_workflow_id)
            else:
                decision = evaluate_implementation_gate(paths, redirect_paths, workflow_id=path_workflow_id)
            if decision.decision == "deny":
                return _to_blocking_decision(hook_name, decision)

    if GIT_COMMIT_RE.search(command_text):
        decision = evaluate_completion_gate(paths)
        if decision.decision == "deny":
            return _to_blocking_decision(hook_name, decision)
    return None


def _count_active_workflows(paths: HarnessPaths) -> int:
    if not paths.active_workflows.exists():
        return 0
    import json as _json

    registry = _json.loads(paths.active_workflows.read_text(encoding="utf-8"))
    return len(registry.get("active", []))


def _extract_tool_name(payload: dict) -> str:
    return str(payload.get("toolName") or payload.get("tool_name") or payload.get("tool") or "").lower()


def _extract_tool_args(payload: dict) -> dict:
    for key in ("toolArgs", "tool_args", "tool_input", "arguments"):
        candidate = payload.get(key)
        if isinstance(candidate, dict):
            return candidate
    return payload if isinstance(payload, dict) else {}


def _extract_candidate_paths(paths: HarnessPaths, payload: dict) -> list[Path]:
    found: list[Path] = []
    seen: set[str] = set()
    for candidate in (payload, _extract_tool_args(payload)):
        _collect_paths(paths, candidate, "", found, seen)
    return found


def _collect_paths(paths: HarnessPaths, value: object, key_name: str, found: list[Path], seen: set[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            _collect_paths(paths, child, str(key).lower(), found, seen)
        return
    if isinstance(value, list):
        for child in value:
            _collect_paths(paths, child, key_name, found, seen)
        return
    if key_name not in PATH_KEY_NAMES or not isinstance(value, str) or not value.strip():
        return
    resolved = resolve_repo_path(paths, value)
    marker = str(resolved)
    if marker not in seen:
        seen.add(marker)
        found.append(resolved)


def _extract_command_text(payload: dict) -> str:
    for candidate in (payload, _extract_tool_args(payload)):
        if not isinstance(candidate, dict):
            continue
        for key in ("command", "cmd", "input", "script"):
            value = candidate.get(key)
            if isinstance(value, str):
                return value
    return ""


def _looks_like_state_write(command_text: str) -> bool:
    lowered = command_text.lower()
    if ".github/harness-v2/state/" not in lowered:
        return False
    if "/artifacts/" in lowered:
        return False
    return any(marker in lowered for marker in STATE_WRITE_HINTS)


def _extract_redirect_paths(paths: HarnessPaths, command_text: str) -> list[Path]:
    """Extract file paths that a bash command will write to via redirect or heredoc."""
    found: list[Path] = []
    seen: set[str] = set()
    for match in _HEREDOC_RE.finditer(command_text):
        raw = _expand_simple_vars(command_text, match.group(1))
        resolved = resolve_repo_path(paths, raw)
        marker = str(resolved)
        if marker not in seen:
            seen.add(marker)
            found.append(resolved)
    for match in _REDIRECT_RE.finditer(command_text):
        raw = _expand_simple_vars(command_text, match.group(1))
        if not raw or raw.startswith("/dev/") or raw == "&1" or raw == "&2":
            continue
        resolved = resolve_repo_path(paths, raw)
        marker = str(resolved)
        if marker not in seen:
            seen.add(marker)
            found.append(resolved)
    return found


def _expand_simple_vars(command_text: str, path_str: str) -> str:
    """Resolve simple $VAR or ${VAR} references that are assigned in the same command."""
    if "$" not in path_str:
        return path_str
    import re as _re
    for match in _re.finditer(r'\b([A-Z_][A-Z0-9_]*)=["\']?([^"\';\n&|]+)', command_text):
        var_name, var_value = match.group(1), match.group(2).strip().strip('"').strip("'")
        path_str = path_str.replace(f"${{{var_name}}}", var_value)
        path_str = path_str.replace(f"${var_name}", var_value)
    return path_str


def _format_reason(reason: str, missing: list[str]) -> str:
    if not missing:
        return reason
    return reason + " — still needed: " + ", ".join(missing)


def _append_gate_denied_event(paths: HarnessPaths, payload: dict, decision: BlockingDecision) -> None:
    append_event(
        paths,
        decision.workflow_id,
        "gate_denied",
        {
            "hook_name": decision.hook_name,
            "gate": decision.gate,
            "reason": decision.reason,
            "missing": decision.missing,
            "formatted_reason": decision.formatted_reason,
            "tool_name": _extract_tool_name(payload),
            "command": _extract_command_text(payload),
            "candidate_paths": [str(path.relative_to(paths.repo)) for path in _extract_candidate_paths(paths, payload) if _is_relative_to_repo(paths, path)],
        },
    )


def _append_gate_warning_event(paths: HarnessPaths, payload: dict, decision: BlockingDecision) -> None:
    append_event(
        paths,
        decision.workflow_id,
        "gate_warning",
        {
            "hook_name": decision.hook_name,
            "gate": decision.gate,
            "reason": decision.reason,
            "missing": decision.missing,
            "formatted_reason": decision.formatted_reason,
            "tool_name": _extract_tool_name(payload),
            "command": _extract_command_text(payload),
            "candidate_paths": [str(path.relative_to(paths.repo)) for path in _extract_candidate_paths(paths, payload) if _is_relative_to_repo(paths, path)],
        },
    )


def _to_blocking_decision(hook_name: str, decision: GateDecision) -> BlockingDecision:
    return BlockingDecision(hook_name, decision.gate, decision.reason, decision.missing, decision.workflow_id)


def _workflow_id_from_paths(paths: HarnessPaths, candidate_paths: list[Path]) -> str | None:
    for raw_path in candidate_paths:
        resolved = resolve_repo_path(paths, raw_path)
        try:
            relative = resolved.relative_to(paths.state)
        except ValueError:
            continue
        parts = relative.parts
        if len(parts) >= 2 and parts[0] == "workflows":
            return parts[1]
    return None


def _is_relative_to_repo(paths: HarnessPaths, path: Path) -> bool:
    try:
        resolve_repo_path(paths, path).relative_to(paths.repo)
    except ValueError:
        return False
    return True


def _emit_pre_tool_decision(decision: str, reason: str) -> None:
    print(json.dumps({"permissionDecision": decision, "permissionDecisionReason": reason}))


def _emit_permission_request(behavior: str, message: str, interrupt: bool) -> None:
    print(json.dumps({"behavior": behavior, "message": message, "interrupt": interrupt}))


def _emit_stop_decision(decision: str, reason: str) -> None:
    print(json.dumps({"decision": decision, "reason": reason}))


if __name__ == "__main__":
    raise SystemExit(hook_main())
