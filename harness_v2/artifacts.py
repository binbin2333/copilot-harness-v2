from __future__ import annotations

import errno
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .config import ARTIFACT_TYPES, load_config
from .state import (
    CLARIFICATION_WAIVER_PATTERNS,
    HarnessPaths,
    append_artifact_registration,
    append_event,
    artifact_output_path,
    load_state,
    now_utc,
    refresh_workflow_progress,
    save_state,
    select_workflow,
    workflow_confirmation_state,
)
from .v3_parser import parse_v3_blocks


INVALIDATES_BY_ARTIFACT: dict[str, list[str]] = {
    "task-classification": [
        "requirements-summary",
        "context-map",
        "scope-freeze",
        "design",
        "implementation",
        "verification",
        "review",
    ],
    "clarification-memo": [
        "requirements-summary",
        "context-map",
        "scope-freeze",
        "design",
        "implementation",
        "verification",
        "review",
    ],
    "requirements-summary": [
        "context-map",
        "scope-freeze",
        "design",
        "implementation",
        "verification",
        "review",
    ],
    "context-map": ["scope-freeze", "design", "implementation", "verification", "review"],
    "call-chain": ["scope-freeze", "design", "implementation", "verification", "review"],
    "test-map": ["scope-freeze", "design", "implementation", "verification", "review"],
    "scope-freeze": ["design", "implementation", "verification", "review"],
    "design": ["implementation", "verification", "review"],
    "impact-analysis": ["implementation", "verification", "review"],
    "verification-report": [],
    "review-report": [],
    "publish-report": [],
}

SATISFIES_BY_ARTIFACT: dict[str, list[str]] = {
    "task-classification": ["task-classification"],
    "clarification-memo": ["clarification"],
    "requirements-summary": ["requirements-summary"],
    "context-map": ["context-map"],
    "call-chain": ["call-chain"],
    "test-map": ["test-map"],
    "scope-freeze": ["scope-freeze"],
    "design": ["design"],
    "impact-analysis": ["impact-analysis"],
    "verification-report": ["implementation", "verification"],
    "review-report": ["review"],
    "publish-report": ["publish"],
}


@dataclass(frozen=True)
class EvidenceRegistration:
    artifact_id: str
    artifact_type: str
    path: Path
    version: int
    changed: bool
    invalidated: list[str]


def validate_artifact_type(artifact_type: str) -> None:
    if artifact_type not in ARTIFACT_TYPES:
        allowed = ", ".join(ARTIFACT_TYPES)
        raise ValueError(f"unknown artifact type '{artifact_type}'. Allowed: {allowed}")


def artifact_template(artifact_type: str, workflow_id: str) -> str:
    title = artifact_type.replace("-", " ").title()
    return (
        f"# {title}\n\n"
        f"- Workflow: `{workflow_id}`\n"
        f"- Status: draft\n\n"
        "## Summary\n\n"
        "TODO\n\n"
        "## Evidence\n\n"
        "TODO\n\n"
        "## Risks and assumptions\n\n"
        "TODO\n"
        f"{_v3_template_block(artifact_type)}"
    )


def create_evidence(paths: HarnessPaths, artifact_type: str, workflow_id: str | None = None) -> Path:
    validate_artifact_type(artifact_type)
    selected = select_workflow(paths, workflow_id)
    artifact_path = artifact_output_path(paths, selected.workflow_id, artifact_type)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    if not artifact_path.exists():
        artifact_path.write_text(artifact_template(artifact_type, selected.workflow_id), encoding="utf-8")
    append_event(
        paths,
        selected.workflow_id,
        "evidence_created",
        {"artifact_type": artifact_type, "path": str(artifact_path.relative_to(paths.repo))},
    )
    return artifact_path


def register_evidence(
    paths: HarnessPaths,
    artifact_type: str,
    artifact_path: Path,
    workflow_id: str | None = None,
) -> EvidenceRegistration:
    validate_artifact_type(artifact_type)
    selected = select_workflow(paths, workflow_id)
    state = load_state(paths, selected.workflow_id)
    config = load_config(paths.repo)
    artifact_path = artifact_path.resolve()
    expected_path = artifact_output_path(paths, selected.workflow_id, artifact_type).resolve()
    if not artifact_path.exists():
        raise FileNotFoundError(f"artifact does not exist: {artifact_path}")
    if not _is_relative_to(artifact_path, paths.repo):
        raise ValueError(f"artifact must be inside repository: {artifact_path}")
    if artifact_path != expected_path:
        raise ValueError(
            "artifact path must match active workflow artifact location: "
            f"expected {expected_path.relative_to(paths.repo)}, got {artifact_path.relative_to(paths.repo)}"
        )
    _validate_registration_requirements(paths, selected.workflow_id, config, artifact_type, artifact_path)
    digest = _sha256(artifact_path)
    v3_result = parse_v3_blocks(artifact_path.read_text(encoding="utf-8"), artifact_type, str(artifact_path.relative_to(paths.repo)))
    artifacts = state.setdefault("artifacts", {})
    previous = artifacts.get(artifact_type)
    changed = previous is None or previous.get("hash") != digest
    version = int(previous.get("version", 0)) + 1 if changed and previous else int(previous.get("version", 1)) if previous else 1
    invalidated = INVALIDATES_BY_ARTIFACT.get(artifact_type, []) if changed else []
    timestamp = now_utc()
    relative = artifact_path.relative_to(paths.repo)
    artifacts[artifact_type] = {
        "id": f"artifact-{artifact_type}-{version}",
        "type": artifact_type,
        "path": str(relative),
        "version": version,
        "status": "current",
        "hash": digest,
        "created_at": previous.get("created_at") if previous else timestamp,
        "updated_at": timestamp,
        "depends_on": [],
        "invalidates": invalidated,
    }
    if v3_result.has_content or v3_result.errors:
        artifacts[artifact_type]["v3_status"] = "parse-error" if v3_result.errors else "ok"
    _apply_v3_parse_result(state, artifact_type, v3_result)
    state["updated_at"] = timestamp
    verification = state.setdefault("verification", {})
    review = state.setdefault("review", {})
    if artifact_type == "scope-freeze":
        verification["planned_commands"] = list(config.verification_commands)
    if artifact_type == "verification-report":
        verification["status"] = "current"
        verification["commands"] = list(config.verification_commands)
    if artifact_type == "review-report":
        review["status"] = "current"
    satisfied_phases = SATISFIES_BY_ARTIFACT.get(artifact_type, [])
    if satisfied_phases:
        existing_invalidated = set(state.setdefault("invalidated", []))
        for satisfied in satisfied_phases:
            existing_invalidated.discard(satisfied)
            state.setdefault("phases", {}).setdefault(satisfied, {})["status"] = "complete"
        state["invalidated"] = sorted(existing_invalidated)
    if changed:
        existing_invalidated = set(state.setdefault("invalidated", []))
        for phase in invalidated:
            existing_invalidated.add(phase)
            state.setdefault("phases", {}).setdefault(phase, {})["status"] = "invalidated"
        state["invalidated"] = sorted(existing_invalidated)
    refresh_workflow_progress(state)
    save_state(paths, state)
    append_artifact_registration(
        paths,
        selected.workflow_id,
        {
            "artifact": {
                "changed": changed,
                "hash": digest,
                "path": str(relative),
                "type": artifact_type,
                "version": version,
            },
            "invalidated": invalidated,
            "state_snapshot": {
                "artifacts": state.get("artifacts", {}),
                "current_phase": state.get("current_phase"),
                "invalidated": state.get("invalidated", []),
                "open_questions": state.get("open_questions", []),
                "phases": state.get("phases", {}),
                "review": state.get("review", {}),
                "status": state.get("status"),
                "unresolved_failures": state.get("unresolved_failures", []),
                "updated_at": state.get("updated_at"),
                "verification": state.get("verification", {}),
                "task_classification": state.get("task_classification", {}),
                "assumptions": state.get("assumptions", []),
                "decisions": state.get("decisions", []),
                "evidence_items": state.get("evidence_items", []),
                "waivers": state.get("waivers", []),
                "deferred_items": state.get("deferred_items", []),
                "v3_parse_errors": state.get("v3_parse_errors", []),
            },
        },
    )
    append_event(
        paths,
        selected.workflow_id,
        "evidence_registered",
        {
            "artifact_type": artifact_type,
            "path": str(relative),
            "changed": changed,
            "invalidated": invalidated,
            "version": version,
        },
    )
    return EvidenceRegistration(
        artifact_id=artifacts[artifact_type]["id"],
        artifact_type=artifact_type,
        path=artifact_path,
        version=version,
        changed=changed,
        invalidated=invalidated,
    )


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError(f"artifact must not be a symlink: {path}") from exc
        raise
    with os.fdopen(fd, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return f"sha256:{hasher.hexdigest()}"


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _validate_registration_requirements(
    paths: HarnessPaths,
    workflow_id: str,
    config,
    artifact_type: str,
    artifact_path: Path,
) -> None:
    confirmation = workflow_confirmation_state(paths, workflow_id)
    if artifact_type == "clarification-memo":
        if confirmation.clarification_asked:
            return
        if not confirmation.clarification_waived:
            raise ValueError(
                "clarification-memo requires at least one ask_user interaction unless the user explicitly waived clarification"
            )
        memo_text = artifact_path.read_text(encoding="utf-8")
        if not _memo_records_clarification_waiver(memo_text):
            raise ValueError(
                "clarification-memo must explicitly record the user's clarification waiver when no ask_user interaction occurred"
            )
        return

    if artifact_type == "scope-freeze" and not confirmation.verification_confirmed:
        raise ValueError(
            "scope-freeze requires a user-confirmed verification plan; ask the user for test commands unless they already provided them explicitly"
        )
    if artifact_type == "scope-freeze" and config.require_verification_commands and not config.verification_commands:
        raise ValueError("scope-freeze requires non-empty verification.commands in .github/harness-v2/config.yaml")

    if artifact_type == "verification-report":
        if config.require_verification_commands and not config.verification_commands:
            raise ValueError("verification-report requires non-empty verification.commands in .github/harness-v2/config.yaml")
        if config.verification_commands:
            report_text = artifact_path.read_text(encoding="utf-8")
            missing_commands = [command for command in config.verification_commands if not _command_mentioned(command, report_text)]
            if missing_commands:
                raise ValueError(
                    "verification-report must mention every configured verification command: " + ", ".join(missing_commands)
                )
        return

    if artifact_type == "review-report":
        report_text = artifact_path.read_text(encoding="utf-8")
        if _review_has_unresolved_blockers(report_text):
            raise ValueError(
                "review-report contains unresolved blocker/critical findings; "
                "fix the issues and update the report before registering, "
                "or explicitly mark all blockers as resolved/waived"
            )
        return


def _apply_v3_parse_result(state: dict, artifact_type: str, result) -> None:
    state.setdefault("task_classification", {})
    for key in ("assumptions", "decisions", "evidence_items", "waivers", "deferred_items", "v3_parse_errors"):
        state.setdefault(key, [])

    state["v3_parse_errors"] = [
        error for error in state.get("v3_parse_errors", []) if error.get("artifact_type") != artifact_type
    ]
    state["v3_parse_errors"].extend(error.to_dict() for error in result.errors)

    if result.task_classification:
        state["task_classification"] = result.task_classification

    _replace_artifact_entries(state, "assumptions", artifact_type, result.assumptions)
    _replace_artifact_entries(state, "decisions", artifact_type, result.decisions)
    _replace_artifact_entries(state, "evidence_items", artifact_type, result.evidence_items)
    _replace_artifact_entries(state, "waivers", artifact_type, result.waivers)
    _replace_artifact_entries(state, "deferred_items", artifact_type, result.deferred_items)

    if result.review:
        state.setdefault("review", {}).update(result.review)


def _replace_artifact_entries(state: dict, key: str, artifact_type: str, entries: list[dict]) -> None:
    state[key] = [entry for entry in state.get(key, []) if entry.get("_artifact_type") != artifact_type]
    state[key].extend(entries)


def _v3_template_block(artifact_type: str) -> str:
    if artifact_type not in {"task-classification", "requirements-summary", "scope-freeze", "verification-report", "review-report"}:
        return ""
    return (
        "\n<!-- harness:v3:start -->\n"
        "task_classification:\n"
        "  level: 1\n"
        "  labels: []\n"
        "  rationale: TODO\n"
        "<!-- harness:v3:end -->\n"
        if artifact_type in {"task-classification", "requirements-summary"}
        else "\n<!-- harness:v3:start -->\n<!-- Add task-specific A*/D*/E*/W*/F* entries here when v3 gates are enabled. -->\n<!-- harness:v3:end -->\n"
    )


def _memo_records_clarification_waiver(text: str) -> bool:
    explicit_markers = [
        r"user\s+waiv(?:ed|er)",
        r"waived\s+clarification",
        r"user\s+explicitly\s+said",
        r"用户免确认",
        r"用户明确表示",
        r"无需澄清",
        r"无歧义",
    ]
    patterns = list(CLARIFICATION_WAIVER_PATTERNS) + explicit_markers
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def _normalize_whitespace(text: str) -> str:
    """Collapse all whitespace runs (including newlines) into single spaces."""
    return re.sub(r"\s+", " ", text).strip()


def _command_mentioned(command: str, report_text: str) -> bool:
    """Check if a verification command is mentioned in the report.

    Uses normalized whitespace comparison so that line-wrapping or minor
    formatting differences don't cause spurious failures.
    """
    if command in report_text:
        return True
    normalized_cmd = _normalize_whitespace(command)
    normalized_report = _normalize_whitespace(report_text)
    return normalized_cmd in normalized_report


# Patterns that signal an unresolved blocker in a review-report.
_REVIEW_BLOCKER_PATTERNS = [
    re.compile(r"\*\*(?:blocker|critical)\b.*?(?:unresolved|open|must\s+fix)", re.IGNORECASE),
    re.compile(r"(?:^|\n)\s*[-*]\s*\[[ ]\]\s*.*\b(?:blocker|critical)\b", re.IGNORECASE),
    re.compile(r"\bseverity\s*:\s*(?:blocker|critical)\b.*\bstatus\s*:\s*(?:open|unresolved)\b", re.IGNORECASE),
    re.compile(r"\bstatus\s*:\s*(?:open|unresolved)\b.*\bseverity\s*:\s*(?:blocker|critical)\b", re.IGNORECASE),
]

# Patterns indicating blockers are resolved/waived (override above).
_REVIEW_RESOLVED_PATTERNS = [
    re.compile(r"(?:all|both)\s+blockers?\s+(?:resolved|fixed|addressed)", re.IGNORECASE),
    re.compile(r"no\s+(?:open|unresolved)\s+blockers?", re.IGNORECASE),
    re.compile(r"blockers?\s+(?:resolved|fixed|addressed|waived)", re.IGNORECASE),
]


def _review_has_unresolved_blockers(text: str) -> bool:
    """Return True if the review-report contains unresolved blocker/critical findings."""
    if any(pattern.search(text) for pattern in _REVIEW_RESOLVED_PATTERNS):
        return False
    return any(pattern.search(text) for pattern in _REVIEW_BLOCKER_PATTERNS)
