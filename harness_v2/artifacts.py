from __future__ import annotations

import hashlib
import errno
import os
from dataclasses import dataclass
from pathlib import Path

from .config import ARTIFACT_TYPES
from .state import HarnessPaths, append_event, load_state, now_utc, save_state, select_workflow


INVALIDATES_BY_ARTIFACT: dict[str, list[str]] = {
    "clarification-memo": [
        "context-map",
        "scope-freeze",
        "design",
        "implementation",
        "verification",
        "review",
    ],
    "requirements-summary": [
        "clarification",
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
    "clarification-memo": ["clarification"],
    "requirements-summary": ["intake"],
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
    )


def create_evidence(paths: HarnessPaths, artifact_type: str, workflow_id: str | None = None) -> Path:
    validate_artifact_type(artifact_type)
    selected = select_workflow(paths, workflow_id)
    artifact_path = paths.workflow_dir(selected.workflow_id) / "artifacts" / f"{artifact_type}.md"
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
    artifact_path = artifact_path.resolve()
    if not artifact_path.exists():
        raise FileNotFoundError(f"artifact does not exist: {artifact_path}")
    if not _is_relative_to(artifact_path, paths.repo):
        raise ValueError(f"artifact must be inside repository: {artifact_path}")
    digest = _sha256(artifact_path)
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
    state["updated_at"] = timestamp
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
    save_state(paths, state)
    append_event(
        paths,
        selected.workflow_id,
        "evidence_registered",
        {
            "artifact_type": artifact_type,
            "path": str(relative),
            "changed": changed,
            "invalidated": invalidated,
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
