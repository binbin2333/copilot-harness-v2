from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import SOURCE_EXTENSIONS, load_config
from .state import HarnessPaths, artifact_location_for_path, is_protected_state_path, load_state, refresh_workflow_progress, resolve_repo_path, select_workflow
from .v3_schema import NON_REVIEW_PROOF_TYPES


ARTIFACT_PHASE_BY_TYPE = {
    "task-classification": "task-classification",
    "clarification-memo": "clarification",
    "requirements-summary": "requirements-summary",
    "context-map": "context-map",
    "call-chain": "call-chain",
    "test-map": "test-map",
    "scope-freeze": "scope-freeze",
    "design": "design",
    "impact-analysis": "impact-analysis",
    "verification-report": "verification",
    "review-report": "review",
    "publish-report": "publish",
}

ARTIFACT_PREREQUISITES = {
    "task-classification": [],
    "clarification-memo": [],
    "requirements-summary": ["clarification-memo"],
    "context-map": ["clarification-memo", "requirements-summary"],
    "call-chain": ["clarification-memo", "requirements-summary", "context-map"],
    "test-map": ["clarification-memo", "requirements-summary", "context-map"],
    "scope-freeze": ["clarification-memo", "requirements-summary", "context-map"],
    "design": ["clarification-memo", "requirements-summary", "context-map", "scope-freeze"],
    "impact-analysis": ["clarification-memo", "requirements-summary", "context-map", "scope-freeze"],
    "verification-report": ["clarification-memo", "requirements-summary", "context-map", "scope-freeze", "design"],
    "review-report": ["clarification-memo", "requirements-summary", "context-map", "scope-freeze", "design", "verification-report"],
    "publish-report": [
        "clarification-memo",
        "requirements-summary",
        "context-map",
        "scope-freeze",
        "design",
        "verification-report",
        "review-report",
    ],
}


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


def evaluate_artifact_write_gate(
    paths: HarnessPaths,
    candidate_paths: list[Path] | None = None,
    workflow_id: str | None = None,
) -> GateDecision:
    selected = select_workflow(paths, workflow_id)
    state = load_state(paths, selected.workflow_id)
    refresh_workflow_progress(state)
    config = load_config(paths.repo)
    artifact_targets = [
        location
        for raw_path in candidate_paths or []
        if (location := artifact_location_for_path(paths, raw_path)) is not None
    ]
    if not artifact_targets:
        return GateDecision(
            gate="artifact-write",
            decision="allow",
            reason="no artifact files targeted",
            workflow_id=selected.workflow_id,
        )

    required_artifact_types: list[str] = []
    for target in artifact_targets:
        required_artifact_types.extend(ARTIFACT_PREREQUISITES.get(target.artifact_type, []))
    required_artifact_types = sorted(set(required_artifact_types))
    if not config.require_clarification:
        required_artifact_types = [artifact for artifact in required_artifact_types if artifact != "clarification-memo"]
    if not config.require_design_for_non_trivial:
        required_artifact_types = [artifact for artifact in required_artifact_types if artifact != "design"]

    return _evaluate_artifact_requirements(
        gate="artifact-write",
        reason="artifact updates require current upstream evidence",
        state=state,
        required_artifact_types=required_artifact_types,
        workflow_id=selected.workflow_id,
        strict_workflow=config.strict_workflow,
        verification_commands=config.verification_commands,
        require_verification_commands=config.require_verification_commands,
        target_artifact_types=[target.artifact_type for target in artifact_targets],
    )


def evaluate_implementation_gate(
    paths: HarnessPaths,
    candidate_paths: list[Path] | None = None,
    workflow_id: str | None = None,
) -> GateDecision:
    selected = select_workflow(paths, workflow_id)
    state = load_state(paths, selected.workflow_id)
    refresh_workflow_progress(state)
    config = load_config(paths.repo)
    if _only_harness_paths(paths, candidate_paths or []):
        return GateDecision(
            gate="implementation",
            decision="allow",
            reason="harness evidence and runtime paths are writable",
            workflow_id=selected.workflow_id,
        )

    required_artifact_types = ["requirements-summary", "context-map", "scope-freeze"]
    if config.require_clarification:
        required_artifact_types.insert(0, "clarification-memo")
    if _non_trivial(candidate_paths or []) and config.require_design_for_non_trivial:
        required_artifact_types.append("design")

    decision = _evaluate_artifact_requirements(
        gate="implementation",
        reason="implementation requires current evidence before editing business code",
        state=state,
        required_artifact_types=required_artifact_types,
        workflow_id=selected.workflow_id,
        strict_workflow=config.strict_workflow,
        verification_commands=config.verification_commands,
        require_verification_commands=config.require_verification_commands,
        target_artifact_types=[],
    )
    if decision.decision == "deny":
        return decision
    v3_missing = _evaluate_v3_implementation_requirements(state, config)
    if v3_missing:
        return GateDecision(
            gate="implementation",
            decision=_v3_decision(config),
            reason="v3 implementation requirements are incomplete",
            missing=v3_missing,
            workflow_id=selected.workflow_id,
        )
    return decision


def evaluate_completion_gate(paths: HarnessPaths, workflow_id: str | None = None) -> GateDecision:
    try:
        selected = select_workflow(paths, workflow_id)
    except LookupError as exc:
        msg = str(exc)
        if "multiple active workflows" in msg:
            return GateDecision(
                gate="completion",
                decision="deny",
                reason="multiple active workflows exist; complete or close them before stopping",
                workflow_id=None,
            )
        return GateDecision(
            gate="completion",
            decision="allow",
            reason="no active workflow; nothing to enforce",
            workflow_id=None,
        )
    state = load_state(paths, selected.workflow_id)
    refresh_workflow_progress(state)
    config = load_config(paths.repo)

    # Check if the workflow is at a configured pause checkpoint.
    # A checkpoint is reached when current_phase is the phase immediately
    # *after* a phase listed in pause_after_phases (i.e. that phase's artifact
    # was just registered and the next phase is about to begin).
    if config.pause_after_phases:
        current_phase = state.get("current_phase", "")
        paused_at = _pause_checkpoint_phase(current_phase, config.pause_after_phases, state)
        if paused_at:
            return GateDecision(
                gate="completion",
                decision="allow",
                reason=(
                    f"workflow paused at '{paused_at}' checkpoint as configured in "
                    f"workflow.pause_after_phases; agent may stop here for human review. "
                    f"Continue by sending any message."
                ),
                workflow_id=selected.workflow_id,
            )

    artifacts = state.get("artifacts", {})
    missing: list[str] = []
    if config.require_verification_commands and not config.verification_commands:
        missing.append("verification.commands")
    if artifacts.get("verification-report", {}).get("status") != "current":
        missing.append("verification-report")
    if artifacts.get("review-report", {}).get("status") != "current":
        missing.append("review-report")
    invalidated = state.get("invalidated", [])
    if invalidated:
        missing.append(f"resolved-invalidations({', '.join(invalidated)})")
    if state.get("unresolved_failures"):
        missing.append("resolved-unresolved-failures")
    if state.get("current_phase") != "done":
        missing.append(f"workflow-phase({state.get('current_phase')})")
    if missing:
        return GateDecision(
            gate="completion",
            decision="deny",
            reason="task cannot complete until verification and review evidence are registered and no phases are invalidated",
            missing=missing,
            workflow_id=selected.workflow_id,
        )
    v3_missing = _evaluate_v3_completion_requirements(state, config)
    if v3_missing:
        return GateDecision(
            gate="completion",
            decision=_v3_decision(config),
            reason="v3 assumption/evidence requirements are incomplete",
            missing=v3_missing,
            workflow_id=selected.workflow_id,
        )
    return GateDecision(
        gate="completion",
        decision="allow",
        reason="all completion evidence is current",
        workflow_id=selected.workflow_id,
    )


def _evaluate_v3_implementation_requirements(state: dict, config) -> list[str]:
    if not config.enable_v3_gates or not config.require_task_classification:
        return []
    classification = state.get("task_classification") or {}
    if not classification:
        return ["task-classification blocks implementation: add task_classification level 0-3 in requirements-summary or task-classification artifact."]
    level = int(classification.get("level", 0))
    if level >= 2 and not state.get("assumptions"):
        return ["A* assumption ledger blocks implementation: Level 2/3 tasks require assumptions before editing implementation files."]
    return []


def _evaluate_v3_completion_requirements(state: dict, config) -> list[str]:
    if not config.enable_v3_gates:
        return []
    missing: list[str] = []
    missing.extend(_v3_parse_error_messages(state))
    missing.extend(_v3_skipped_evidence_messages(state, config))
    missing.extend(_v3_deferred_messages(state))
    missing.extend(_v3_assumption_messages(state, config))
    missing.extend(_v3_review_messages(state, config))
    return missing


def _v3_parse_error_messages(state: dict) -> list[str]:
    return [
        f"{error.get('artifact_type', '<artifact>')} parse error blocks completion: {error.get('block_name')}: {error.get('message')}; fix the structured harness:v3 block and re-register the artifact."
        for error in state.get("v3_parse_errors", [])
    ]


def _v3_skipped_evidence_messages(state: dict, config) -> list[str]:
    if not config.require_skip_waivers:
        return []
    covered = _covered_ids(state)
    messages = []
    for evidence in state.get("evidence_items", []):
        evidence_id = evidence.get("id", "<unknown>")
        if evidence.get("result") == "skipped" and evidence_id not in covered:
            messages.append(
                f"{evidence_id} blocks completion: result=skipped; add waiver W* covering {evidence_id} with owner, risk, and exit_criteria, or run the evidence."
            )
    return messages


def _v3_deferred_messages(state: dict) -> list[str]:
    messages = []
    for item in state.get("deferred_items", []):
        item_id = item.get("id", "<unknown>")
        if not item.get("owner") or not item.get("exit_criteria"):
            messages.append(f"{item_id} blocks completion: deferred item needs owner and exit_criteria.")
    return messages


def _v3_assumption_messages(state: dict, config) -> list[str]:
    if not config.require_assumption_resolution:
        return []
    covered = _covered_ids(state)
    evidence_by_id = {item.get("id"): item for item in state.get("evidence_items", [])}
    messages = []
    for assumption in state.get("assumptions", []):
        if assumption.get("risk") != "high":
            continue
        assumption_id = assumption.get("id", "<unknown>")
        status = assumption.get("status")
        evidence_ids = list(assumption.get("evidence", []))
        if status == "proven":
            proving = [
                evidence_by_id.get(evidence_id)
                for evidence_id in evidence_ids
                if evidence_by_id.get(evidence_id, {}).get("result") == "pass"
                and evidence_by_id.get(evidence_id, {}).get("type") in NON_REVIEW_PROOF_TYPES
            ]
            if not proving:
                messages.append(
                    f"{assumption_id} blocks completion: risk=high status=proven evidence={evidence_ids}; add non-review PASS evidence linked to {assumption_id}."
                )
            continue
        if status == "accepted-risk":
            if assumption_id not in covered:
                messages.append(f"{assumption_id} blocks completion: status=accepted-risk requires waiver W* covering {assumption_id}.")
            continue
        if status == "deferred":
            if not _has_valid_deferred_item_for(state, assumption_id):
                messages.append(
                    f"{assumption_id} blocks completion: status=deferred requires deferred item F* covering {assumption_id} with owner and exit_criteria."
                )
            continue
        if status == "rejected":
            continue
        messages.append(
            f"{assumption_id} blocks completion: risk=high status={status} evidence={evidence_ids}; add non-review PASS evidence linked to {assumption_id}, add waiver W* to accept risk, or defer as F* with owner and exit_criteria."
        )
    return messages


def _v3_review_messages(state: dict, config) -> list[str]:
    review = state.get("review", {})
    verdict = review.get("verdict")
    if not verdict:
        return []
    if verdict == "BLOCKED":
        return ["review verdict BLOCKED blocks completion: resolve review blockers and re-register review-report."]
    gaps = list(review.get("gaps", []))
    if verdict == "PASS-WITH-GAPS":
        if not config.allow_pass_with_gaps:
            return ["review verdict PASS-WITH-GAPS blocks completion: enable allow_pass_with_gaps or resolve all gaps."]
        covered = _covered_ids(state)
        invalid_gaps = [gap for gap in gaps if gap not in covered and not _valid_deferred_gap(state, gap)]
        return [
            f"{gap} blocks completion: PASS-WITH-GAPS gap must map to waiver W* or deferred item F* with owner and exit_criteria."
            for gap in invalid_gaps
        ]
    if verdict == "PASS":
        assumption_messages = _v3_assumption_messages(state, config)
        if assumption_messages:
            return ["review PASS blocks completion: high-risk gaps remain; resolve assumptions or change verdict to PASS-WITH-GAPS with mapped waivers/deferred items."]
    return []


def _covered_ids(state: dict) -> set[str]:
    covered: set[str] = set()
    for waiver in state.get("waivers", []):
        if waiver.get("owner") and waiver.get("risk") and waiver.get("exit_criteria"):
            covered.update(waiver.get("covers", []))
    return covered


def _valid_deferred_gap(state: dict, gap: str) -> bool:
    for item in state.get("deferred_items", []):
        if gap == item.get("id") and item.get("owner") and item.get("exit_criteria"):
            return True
    return False


def _has_valid_deferred_item_for(state: dict, assumption_id: str) -> bool:
    for item in state.get("deferred_items", []):
        covers = item.get("covers", [])
        if item.get("owner") and item.get("exit_criteria") and (not covers or assumption_id in covers):
            return True
    return False


def _v3_decision(config) -> str:
    return "deny" if config.v3_gate_mode == "enforce" else "warn"


def evaluate_verification_gate(paths: HarnessPaths, workflow_id: str | None = None) -> GateDecision:
    selected = select_workflow(paths, workflow_id)
    state = load_state(paths, selected.workflow_id)
    refresh_workflow_progress(state)
    config = load_config(paths.repo)
    artifacts = state.get("artifacts", {})
    missing: list[str] = []
    if config.require_verification_commands and not config.verification_commands:
        missing.append("verification.commands")
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


def _evaluate_artifact_requirements(
    *,
    gate: str,
    reason: str,
    state: dict,
    required_artifact_types: list[str],
    workflow_id: str,
    strict_workflow: bool,
    verification_commands: tuple[str, ...],
    require_verification_commands: bool,
    target_artifact_types: list[str],
) -> GateDecision:
    artifacts = state.get("artifacts", {})
    invalidated = set(state.get("invalidated", []))
    blocking = []
    for artifact_type in required_artifact_types:
        phase_name = ARTIFACT_PHASE_BY_TYPE.get(artifact_type)
        if phase_name and phase_name in invalidated:
            blocking.append(artifact_type)
    if blocking and strict_workflow:
        return GateDecision(
            gate=gate,
            decision="deny",
            reason="upstream phases were invalidated by a recent change; re-register before proceeding",
            missing=[f"re-register:{artifact}" for artifact in sorted(blocking)],
            workflow_id=workflow_id,
        )

    missing = [
        artifact_type
        for artifact_type in required_artifact_types
        if artifacts.get(artifact_type, {}).get("status") != "current"
    ]
    if require_verification_commands and ("scope-freeze" in target_artifact_types or gate == "implementation") and not verification_commands:
        missing.append("verification.commands")
    if state.get("unresolved_failures"):
        missing.append("memory-for-unresolved-failures")
    if missing and strict_workflow:
        return GateDecision(
            gate=gate,
            decision="deny",
            reason=reason,
            missing=missing,
            workflow_id=workflow_id,
        )
    if missing:
        return GateDecision(
            gate=gate,
            decision="warn",
            reason="workflow evidence is incomplete",
            missing=missing,
            workflow_id=workflow_id,
        )
    return GateDecision(
        gate=gate,
        decision="allow",
        reason="required workflow evidence is current",
        workflow_id=workflow_id,
    )


def _only_harness_paths(paths: HarnessPaths, candidate_paths: list[Path]) -> bool:
    if not candidate_paths:
        return False
    harness_root = paths.repo / ".github" / "harness-v2"
    skills_root = paths.repo / ".github" / "skills"
    hooks_root = paths.repo / ".github" / "hooks"
    for raw_path in candidate_paths:
        resolved = resolve_repo_path(paths, raw_path)
        if is_protected_state_path(paths, resolved):
            return False
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


# The ordered sequence of phases for pause-checkpoint resolution.
# Each entry is (phase_name, required_artifact_type).  When current_phase is
# the phase AFTER a configured pause phase and the pause phase's artifact is
# "current", the workflow is at a valid checkpoint.
_PHASE_SEQUENCE = [
    ("clarification", "clarification-memo"),
    ("requirements-summary", "requirements-summary"),
    ("context-map", "context-map"),
    ("scope-freeze", "scope-freeze"),
    ("design", "design"),
    ("implementation", "verification-report"),
    ("review", "review-report"),
]


def _pause_checkpoint_phase(
    current_phase: str,
    pause_after_phases: tuple[str, ...],
    state: dict,
) -> str | None:
    """Return the pause phase name if the workflow is sitting at a configured
    checkpoint, or None otherwise.

    A checkpoint is active when:
    - ``current_phase`` is the phase immediately *after* a ``pause_after_phases``
      entry in the ordered phase sequence, AND
    - that pause phase's required artifact has status "current" (i.e. it was
      just registered this session).
    """
    if not pause_after_phases:
        return None
    artifacts = state.get("artifacts", {})
    phase_names = [p for p, _ in _PHASE_SEQUENCE]
    for pause_phase, required_artifact in _PHASE_SEQUENCE:
        if pause_phase not in pause_after_phases:
            continue
        artifact = artifacts.get(required_artifact, {})
        if artifact.get("status") != "current":
            continue
        # Find the phase immediately after pause_phase in the sequence.
        try:
            idx = phase_names.index(pause_phase)
        except ValueError:
            continue
        next_phase = phase_names[idx + 1] if idx + 1 < len(phase_names) else "done"
        if current_phase == next_phase:
            return pause_phase
    return None
