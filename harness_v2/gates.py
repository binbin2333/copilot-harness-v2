from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import SOURCE_EXTENSIONS, load_config
from .state import HarnessPaths, load_state, select_workflow


@dataclass(frozen=True)
class GateDecision:
    gate: str
    decision: str
    reason: str
    missing: list[str] = field(default_factory=list)
    workflow_id: str | None = None

    def as_dict(self) -> dict:
        return {
            "gate": self.gate,
            "decision": self.decision,
            "reason": self.reason,
            "missing": self.missing,
            "workflow_id": self.workflow_id,
        }


def evaluate_implementation_gate(
    paths: HarnessPaths,
    candidate_paths: list[Path] | None = None,
    workflow_id: str | None = None,
) -> GateDecision:
    selected = select_workflow(paths, workflow_id)
    state = load_state(paths, selected.workflow_id)
    config = load_config(paths.repo)
    if _only_harness_paths(paths.repo, candidate_paths or []):
        return GateDecision(
            gate="implementation",
            decision="allow",
            reason="harness evidence and runtime paths are always writable",
            workflow_id=selected.workflow_id,
        )
    artifacts = state.get("artifacts", {})
    missing = [
        artifact_type
        for artifact_type in ("context-map", "scope-freeze")
        if artifacts.get(artifact_type, {}).get("status") != "current"
    ]
    if _non_trivial(candidate_paths or []) and config.require_design_for_non_trivial:
        if artifacts.get("design", {}).get("status") != "current":
            missing.append("design")
    if state.get("open_questions"):
        missing.append("resolved-open-questions")
    if state.get("unresolved_failures"):
        missing.append("memory-for-unresolved-failures")
    if missing and config.strict_workflow:
        return GateDecision(
            gate="implementation",
            decision="deny",
            reason="implementation requires current evidence before editing business code",
            missing=missing,
            workflow_id=selected.workflow_id,
        )
    if missing:
        return GateDecision(
            gate="implementation",
            decision="warn",
            reason="implementation evidence is incomplete",
            missing=missing,
            workflow_id=selected.workflow_id,
        )
    return GateDecision(
        gate="implementation",
        decision="allow",
        reason="required implementation evidence is current",
        workflow_id=selected.workflow_id,
    )


def evaluate_verification_gate(paths: HarnessPaths, workflow_id: str | None = None) -> GateDecision:
    selected = select_workflow(paths, workflow_id)
    state = load_state(paths, selected.workflow_id)
    artifacts = state.get("artifacts", {})
    missing: list[str] = []
    if artifacts.get("verification-report", {}).get("status") != "current":
        missing.append("verification-report")
    if state.get("invalidated") and "verification" in state.get("invalidated", []):
        missing.append("current-verification")
    if missing:
        return GateDecision(
            gate="verification",
            decision="deny",
            reason="finishing or committing code requires current verification evidence",
            missing=missing,
            workflow_id=selected.workflow_id,
        )
    return GateDecision(
        gate="verification",
        decision="allow",
        reason="verification evidence is current",
        workflow_id=selected.workflow_id,
    )


def _only_harness_paths(repo: Path, paths: list[Path]) -> bool:
    if not paths:
        return False
    harness_root = repo / ".github" / "harness-v2"
    skills_root = repo / ".github" / "skills"
    hooks_root = repo / ".github" / "hooks"
    for raw_path in paths:
        path = raw_path if raw_path.is_absolute() else repo / raw_path
        resolved = path.resolve()
        if not (_is_relative_to(resolved, harness_root) or _is_relative_to(resolved, skills_root) or _is_relative_to(resolved, hooks_root)):
            return False
    return True


def _non_trivial(paths: list[Path]) -> bool:
    source_paths = [path for path in paths if path.suffix in SOURCE_EXTENSIONS]
    if len(source_paths) > 1:
        return True
    if not paths:
        return True
    sensitive_parts = {"hooks", "installer", "config", "schema", "gate", "gates", "memory", "state"}
    return any(sensitive_parts.intersection(path.parts) for path in paths)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent.resolve())
    except ValueError:
        return False
    return True

