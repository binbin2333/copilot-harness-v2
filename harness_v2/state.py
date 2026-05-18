from __future__ import annotations

import copy
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ARTIFACT_TYPES
from .v3_schema import v3_state_defaults


PHASES = [
    "clarification",
    "requirements-summary",
    "context-map",
    "call-chain",
    "test-map",
    "scope-freeze",
    "design",
    "impact-analysis",
    "implementation",
    "verification",
    "review",
    "publish",
    "done",
    "intake",
    "exploration",
    "scope",
    "rework",
    "memory",
]

PROGRESS_PHASE_REQUIREMENTS = (
    ("task-classification", "task-classification", False),
    ("clarification", "clarification-memo", True),
    ("requirements-summary", "requirements-summary", True),
    ("context-map", "context-map", True),
    ("call-chain", "call-chain", False),
    ("test-map", "test-map", False),
    ("scope-freeze", "scope-freeze", True),
    ("design", "design", True),
    ("impact-analysis", "impact-analysis", False),
    ("implementation", "verification-report", True),
    ("review", "review-report", True),
)


@dataclass(frozen=True)
class HarnessPaths:
    repo: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "repo", self.repo.resolve())

    @property
    def root(self) -> Path:
        return self.repo / ".github" / "harness-v2"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def memory(self) -> Path:
        return self.root / "memory"

    @property
    def active_workflows(self) -> Path:
        return self.state / "active-workflows.json"

    @property
    def bin_dir(self) -> Path:
        return self.root / "bin"

    @property
    def runtime_dir(self) -> Path:
        return self.root / "runtime" / "harness_v2"

    def workflow_dir(self, workflow_id: str) -> Path:
        return self.state / "workflows" / workflow_id

    def artifact_registrations(self, workflow_id: str) -> Path:
        return self.workflow_dir(workflow_id) / "artifact-registrations.jsonl"


@dataclass(frozen=True)
class WorkflowSelection:
    workflow_id: str
    workflow_type: str
    slug: str


@dataclass(frozen=True)
class ArtifactLocation:
    workflow_id: str
    artifact_type: str
    path: Path


CLARIFICATION_WAIVER_PATTERNS = [
    r"\bno\s+ambigu(?:ity|ities)\b",
    r"\bno\s+clarification\s+needed\b",
    r"\bskip\s+clarification\b",
    r"\brequirements?\s+are\s+clear\b",
    r"无需澄清",
    r"无歧义",
    r"可直接继续",
    r"可以直接继续",
    r"不用确认",
    r"免确认",
]

VERIFICATION_PLAN_PATTERNS = [
    r"`[^`\n]*(pytest|ctest|go test|cargo test|npm test|pnpm test|yarn test|gradle test|mvn test|bazel test|build\.sh|make test|make check|clang-tidy)[^`\n]*`",
    r"```[\s\S]*?(pytest|ctest|go test|cargo test|npm test|pnpm test|yarn test|gradle test|mvn test|bazel test|build\.sh|make test|make check|clang-tidy)[\s\S]*?```",
    r"\btest\s+with\b",
    r"\bverify\s+with\b",
    r"\brun\s+(the\s+)?tests?\b",
    r"\bbuild\s+with\b",
    r"\b用.+(测试|验证|构建|编译)",
]

VERIFICATION_QUESTION_PATTERNS = [
    r"\btest\b",
    r"\btests\b",
    r"\bverification\b",
    r"\bverify\b",
    r"\bbuild\b",
    r"\bcompile\b",
    r"\bclang-tidy\b",
    r"测试",
    r"验证",
    r"构建",
    r"编译",
]

ASK_TOOL_NAMES = {"ask_user", "vscode_askquestions"}


@dataclass(frozen=True)
class WorkflowConfirmationState:
    clarification_asked: bool
    clarification_waived: bool
    verification_asked: bool
    verification_provided: bool

    @property
    def clarification_confirmed(self) -> bool:
        return self.clarification_asked or self.clarification_waived

    @property
    def verification_confirmed(self) -> bool:
        return self.verification_asked or self.verification_provided

    def as_dict(self) -> dict[str, bool]:
        return {
            "clarification_asked": self.clarification_asked,
            "clarification_waived": self.clarification_waived,
            "clarification_confirmed": self.clarification_confirmed,
            "verification_asked": self.verification_asked,
            "verification_provided": self.verification_provided,
            "verification_confirmed": self.verification_confirmed,
        }


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def artifact_output_path(paths: HarnessPaths, workflow_id: str, artifact_type: str) -> Path:
    return paths.workflow_dir(workflow_id) / "artifacts" / f"{artifact_type}.md"


def refresh_workflow_progress(state: dict[str, Any]) -> dict[str, Any]:
    phases = state.setdefault("phases", {})
    invalidated = set(state.get("invalidated", []))
    artifacts = state.get("artifacts", {})
    current_phase = "done"

    for phase, artifact_type, required in PROGRESS_PHASE_REQUIREMENTS:
        artifact = artifacts.get(artifact_type, {})
        if phase in invalidated:
            current_phase = phase
            break
        if required and artifact.get("status") != "current":
            current_phase = phase
            break
        if not required and artifact and artifact.get("status") != "current":
            current_phase = phase
            break

    for meta in phases.values():
        if isinstance(meta, dict) and meta.get("status") == "active":
            meta["status"] = "pending"

    state["current_phase"] = current_phase
    state["status"] = "complete" if current_phase == "done" else "active"

    done_meta = phases.setdefault("done", {})
    if current_phase == "done":
        done_meta["status"] = "complete"
    elif done_meta.get("status") == "complete":
        done_meta["status"] = "pending"

    if current_phase != "done":
        current_meta = phases.setdefault(current_phase, {})
        if current_meta.get("status") not in {"complete", "invalidated"}:
            current_meta["status"] = "active"

    return state


def normalize_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower()).strip("-")
    if not slug:
        raise ValueError("slug must contain at least one alphanumeric character")
    return slug


def ensure_state_layout(paths: HarnessPaths) -> None:
    (paths.state / "workflows").mkdir(parents=True, exist_ok=True)
    paths.memory.mkdir(parents=True, exist_ok=True)
    if not paths.active_workflows.exists():
        write_json(paths.active_workflows, {"version": 1, "active": []})


def start_workflow(paths: HarnessPaths, workflow_type: str, slug: str) -> dict[str, Any]:
    ensure_state_layout(paths)
    normalized_slug = normalize_slug(slug)
    workflow_id = f"{normalize_slug(workflow_type)}-{normalized_slug}"
    workflow_dir = paths.workflow_dir(workflow_id)
    if workflow_dir.exists():
        raise FileExistsError(f"workflow already exists: {workflow_id}")
    timestamp = now_utc()
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "artifacts").mkdir()
    paths.artifact_registrations(workflow_id).touch(exist_ok=True)
    state = {
        "version": 1,
        "workflow_id": workflow_id,
        "workflow_type": workflow_type,
        "status": "active",
        "current_phase": "clarification",
        "created_at": timestamp,
        "updated_at": timestamp,
        "phases": {},
        "artifacts": {},
        "invalidated": [],
        "open_questions": [],
        "unresolved_failures": [],
        "verification": {},
        "review": {},
        **v3_state_defaults(),
    }
    refresh_workflow_progress(state)
    save_state(paths, state)
    registry = load_active_registry(paths)
    registry["active"].append(
        {
            "workflow_id": workflow_id,
            "workflow_type": workflow_type,
            "slug": normalized_slug,
            "status": "active",
            "last_context_keys": [normalized_slug],
        }
    )
    write_json(paths.active_workflows, registry)
    append_event(paths, workflow_id, "workflow_started", {"workflow_type": workflow_type, "slug": normalized_slug})
    return state


def load_active_registry(paths: HarnessPaths) -> dict[str, Any]:
    ensure_state_layout(paths)
    registry = read_json(paths.active_workflows)
    if registry.get("version") != 1 or not isinstance(registry.get("active"), list):
        raise ValueError(f"malformed active workflow registry: {paths.active_workflows}")
    for item in registry["active"]:
        if not isinstance(item, dict) or not item.get("workflow_id") or not item.get("slug"):
            raise ValueError(f"malformed active workflow entry in {paths.active_workflows}")
        if not paths.workflow_dir(item["workflow_id"]).exists():
            raise FileNotFoundError(f"active workflow points to missing directory: {item['workflow_id']}")
    return registry


def select_workflow(paths: HarnessPaths, workflow_id_or_slug: str | None = None) -> WorkflowSelection:
    registry = load_active_registry(paths)
    if workflow_id_or_slug:
        matches = [
            item
            for item in registry["active"]
            if item.get("workflow_id") == workflow_id_or_slug or item.get("slug") == workflow_id_or_slug
        ]
        if not matches and paths.workflow_dir(workflow_id_or_slug).exists():
            state = read_json(paths.workflow_dir(workflow_id_or_slug) / "state.json")
            return WorkflowSelection(
                workflow_id_or_slug,
                str(state.get("workflow_type", "feature")),
                str(state.get("slug", workflow_id_or_slug)),
            )
    else:
        active = [item for item in registry["active"] if item.get("status") == "active"]
        matches = [
            item for item in active
        ]
    if not matches:
        raise LookupError(
            "no active workflow found; run `./.github/harness-v2/bin/harness-v2 start <type> <slug>` first"
        )
    if len(matches) > 1 and not workflow_id_or_slug:
        # Auto-disambiguate: if exactly one workflow is not yet done, prefer it.
        incomplete = [item for item in matches if not _workflow_is_done(paths, item["workflow_id"])]
        if len(incomplete) == 1:
            matches = incomplete
        elif len(incomplete) == 0:
            # All registry entries say "active" but every state.json is done.
            # This is a stale registry: treat as no active workflow.
            raise LookupError(
                "no active workflow found; run `./.github/harness-v2/bin/harness-v2 start <type> <slug>` first"
            )
    if len(matches) > 1:
        ids = ", ".join(item["workflow_id"] for item in matches)
        raise LookupError(f"multiple active workflows match; specify one of: {ids}")
    item = matches[0]
    return WorkflowSelection(item["workflow_id"], item["workflow_type"], item["slug"])


def _workflow_is_done(paths: HarnessPaths, workflow_id: str) -> bool:
    """Return True if the workflow's state.json shows current_phase == 'done'."""
    try:
        state_path = paths.workflow_dir(workflow_id) / "state.json"
        if not state_path.exists():
            return False
        return read_json(state_path).get("current_phase") == "done"
    except Exception:
        return False


def load_state(paths: HarnessPaths, workflow_id: str) -> dict[str, Any]:
    path = paths.workflow_dir(workflow_id) / "state.json"
    if not path.exists():
        raise FileNotFoundError(f"workflow state not found: {workflow_id}")
    state = read_json(path)
    _validate_state_shape(state, path, workflow_id)
    reconciled, issues = reconcile_state(paths, state)
    if _state_cache_differs(state, reconciled):
        write_json(path, reconciled)
        append_event(paths, workflow_id, "state_reconciled", {"issues": issues})
    return reconciled


def save_state(paths: HarnessPaths, state: dict[str, Any]) -> None:
    workflow_id = state["workflow_id"]
    path = paths.workflow_dir(workflow_id) / "state.json"
    write_json(path, state)


def set_phase(paths: HarnessPaths, phase: str, workflow_id: str | None = None) -> dict[str, Any]:
    if phase not in PHASES:
        raise ValueError(f"unknown phase '{phase}'")
    selected = select_workflow(paths, workflow_id)
    state = load_state(paths, selected.workflow_id)
    state["current_phase"] = phase
    state.setdefault("phases", {}).setdefault(phase, {})["status"] = "active"
    state["updated_at"] = now_utc()
    save_state(paths, state)
    append_event(paths, selected.workflow_id, "phase_set", {"phase": phase})
    return state


def append_event(paths: HarnessPaths, workflow_id: str | None, event_type: str, payload: dict[str, Any]) -> None:
    ensure_state_layout(paths)
    event = {"id": f"evt-{uuid.uuid4().hex[:12]}", "type": event_type, "created_at": now_utc(), "payload": payload}
    if workflow_id:
        event["workflow_id"] = workflow_id
        event_path = paths.workflow_dir(workflow_id) / "events.jsonl"
    else:
        event_path = paths.state / "events.jsonl"
    event_path.parent.mkdir(parents=True, exist_ok=True)
    with event_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def append_artifact_registration(paths: HarnessPaths, workflow_id: str, payload: dict[str, Any]) -> None:
    ensure_state_layout(paths)
    record = {
        "id": f"areg-{uuid.uuid4().hex[:12]}",
        "workflow_id": workflow_id,
        "registered_at": now_utc(),
        **payload,
    }
    registration_path = paths.artifact_registrations(workflow_id)
    registration_path.parent.mkdir(parents=True, exist_ok=True)
    with registration_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def load_artifact_registrations(paths: HarnessPaths, workflow_id: str) -> list[dict[str, Any]]:
    registration_path = paths.artifact_registrations(workflow_id)
    if not registration_path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(registration_path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON in {registration_path}:{line_number}: {exc}") from exc
        if not isinstance(record, dict) or record.get("workflow_id") != workflow_id:
            raise ValueError(f"malformed artifact registration in {registration_path}:{line_number}")
        records.append(record)
    return records


def reconcile_state(paths: HarnessPaths, cached_state: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    workflow_id = str(cached_state["workflow_id"])
    issues: list[str] = []
    registrations = load_artifact_registrations(paths, workflow_id)
    if not registrations:
        _validate_artifact_metadata(cached_state.get("artifacts", {}), paths.workflow_dir(workflow_id) / "state.json")
        refreshed = _base_state_from_cache(cached_state)
        refresh_workflow_progress(refreshed)
        return refreshed, issues

    latest = registrations[-1]
    snapshot = latest.get("state_snapshot")
    if not isinstance(snapshot, dict):
        raise ValueError(f"malformed artifact registration snapshot for {workflow_id}")
    _validate_artifact_metadata(snapshot.get("artifacts", {}), paths.artifact_registrations(workflow_id))

    reconciled = _base_state_from_cache(cached_state)
    for key in (
        "artifacts",
        "invalidated",
        "phases",
        "verification",
        "review",
        "unresolved_failures",
        "open_questions",
        "task_classification",
        "assumptions",
        "decisions",
        "evidence_items",
        "waivers",
        "deferred_items",
        "v3_parse_errors",
    ):
        reconciled[key] = copy.deepcopy(snapshot.get(key, reconciled.get(key)))
    reconciled["updated_at"] = snapshot.get("updated_at", cached_state.get("updated_at", now_utc()))
    reconciled["status"] = snapshot.get("status", cached_state.get("status", "active"))
    reconciled["current_phase"] = snapshot.get("current_phase", cached_state.get("current_phase", "clarification"))
    refresh_workflow_progress(reconciled)

    if _normalize_state_cache(cached_state) != _normalize_state_cache(reconciled):
        issues.append("state.json cache drifted from artifact registrations")
    return reconciled, issues


def resolve_repo_path(paths: HarnessPaths, raw_path: Path | str) -> Path:
    path = raw_path if isinstance(raw_path, Path) else Path(str(raw_path))
    if not path.is_absolute():
        path = paths.repo / path
    return path.resolve()


# Accepted filename stems that map to a canonical artifact type.
# Agents sometimes use descriptive names (e.g. "design-plan.md") that don't
# exactly match the registered type ("design").  List aliases here so the
# harness can still recognise and gate them correctly.
_ARTIFACT_STEM_ALIASES: dict[str, str] = {
    "design-plan": "design",
}


def artifact_location_for_path(paths: HarnessPaths, raw_path: Path | str) -> ArtifactLocation | None:
    resolved = resolve_repo_path(paths, raw_path)
    try:
        relative = resolved.relative_to(paths.state)
    except ValueError:
        return None
    parts = relative.parts
    if len(parts) != 4 or parts[0] != "workflows" or parts[2] != "artifacts":
        return None
    filename = Path(parts[3])
    if filename.suffix != ".md":
        return None
    artifact_type = _ARTIFACT_STEM_ALIASES.get(filename.stem, filename.stem)
    if artifact_type not in ARTIFACT_TYPES:
        return None
    return ArtifactLocation(parts[1], artifact_type, resolved)


def workflow_confirmation_state(paths: HarnessPaths, workflow_id: str) -> WorkflowConfirmationState:
    state = load_state(paths, workflow_id)
    created_at = str(state.get("created_at", ""))
    events = _iter_harness_events(paths)
    active_session_ids = {
        session_id
        for event in events
        if (not created_at or str(event.get("created_at", "")) >= created_at)
        and (session_id := _extract_session_id(event))
    }
    clarification_asked = False
    clarification_waived = False
    verification_asked = False
    verification_provided = False

    for event in events:
        session_id = _extract_session_id(event)
        if active_session_ids:
            if session_id not in active_session_ids:
                continue
        elif created_at and str(event.get("created_at", "")) < created_at:
            continue
        payload = event.get("payload", {})
        hook_payload = payload.get("payload", {}) if isinstance(payload, dict) else {}
        event_type = str(event.get("type", ""))

        if event_type == "hook_userPromptSubmitted":
            prompt_text = _extract_prompt_text(hook_payload)
            if _matches_any(prompt_text, CLARIFICATION_WAIVER_PATTERNS):
                clarification_waived = True
            if _matches_any(prompt_text, VERIFICATION_PLAN_PATTERNS):
                verification_provided = True
            continue

        if event_type != "hook_postToolUse":
            continue
        tool_name = str(
            hook_payload.get("toolName")
            or hook_payload.get("tool_name")
            or hook_payload.get("tool")
            or ""
        ).lower()
        if tool_name not in ASK_TOOL_NAMES:
            continue
        question_text = _extract_question_text(hook_payload)
        clarification_asked = True
        if _matches_any(question_text, VERIFICATION_QUESTION_PATTERNS):
            verification_asked = True

    return WorkflowConfirmationState(
        clarification_asked=clarification_asked,
        clarification_waived=clarification_waived,
        verification_asked=verification_asked,
        verification_provided=verification_provided,
    )


def is_protected_state_path(paths: HarnessPaths, raw_path: Path | str) -> bool:
    resolved = resolve_repo_path(paths, raw_path)
    try:
        resolved.relative_to(paths.state)
    except ValueError:
        return False
    return artifact_location_for_path(paths, resolved) is None


def runtime_health(paths: HarnessPaths) -> list[str]:
    required_files = [
        paths.runtime_dir / name
        for name in (
            "__init__.py",
            "artifacts.py",
            "cli.py",
            "config.py",
            "events.py",
            "gates.py",
            "installer.py",
            "memory.py",
            "state.py",
            "v3_parser.py",
            "v3_schema.py",
        )
    ]
    required_files.append(paths.root / "runtime" / "yaml" / "__init__.py")
    required_files.extend(
        [
            paths.root / "hooks" / name
            for name in (
                "permission_request.py",
                "pre_tool_use.py",
                "post_tool_use.py",
                "post_tool_use_failure.py",
                "user_prompt_submitted.py",
                "agent_stop.py",
                "subagent_stop.py",
                "session_start.py",
                "session_end.py",
                "error_occurred.py",
            )
        ]
    )
    required_files.extend([paths.bin_dir / "harness-v2", paths.repo / ".github" / "hooks" / "harness-v2.json"])
    return [str(path.relative_to(paths.repo)) for path in required_files if not path.exists()]


def inspect_workflow(paths: HarnessPaths, workflow_id: str | None = None) -> dict[str, Any]:
    report: dict[str, Any] = {"ok": True, "repo_issues": [], "workflow_issues": [], "workflow_id": None}
    missing_runtime = runtime_health(paths)
    if missing_runtime:
        report["ok"] = False
        report["repo_issues"].append("missing runtime files: " + ", ".join(missing_runtime))
    try:
        selected = select_workflow(paths, workflow_id)
    except LookupError as exc:
        if "no active workflow found" not in str(exc):
            report["repo_issues"].append(str(exc))
            report["ok"] = False
        return report

    report["workflow_id"] = selected.workflow_id
    report["confirmation"] = workflow_confirmation_state(paths, selected.workflow_id).as_dict()
    raw_state_path = paths.workflow_dir(selected.workflow_id) / "state.json"
    try:
        raw_state = read_json(raw_state_path)
        _validate_state_shape(raw_state, raw_state_path, selected.workflow_id)
    except Exception as exc:
        report["ok"] = False
        report["workflow_issues"].append(str(exc))
        return report

    try:
        _, issues = reconcile_state(paths, raw_state)
    except Exception as exc:
        report["ok"] = False
        report["workflow_issues"].append(str(exc))
        return report

    if issues:
        report["workflow_issues"].extend(issues)
    if not load_artifact_registrations(paths, selected.workflow_id) and raw_state.get("artifacts"):
        report["workflow_issues"].append("artifact registration ledger is missing; state.json is the only source of truth")
    report["ok"] = not report["repo_issues"] and not report["workflow_issues"]
    return report


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _base_state_from_cache(cached_state: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": cached_state.get("version", 1),
        "workflow_id": cached_state["workflow_id"],
        "workflow_type": cached_state.get("workflow_type", "feature"),
        "status": cached_state.get("status", "active"),
        "current_phase": cached_state.get("current_phase", "clarification"),
        "created_at": cached_state.get("created_at", now_utc()),
        "updated_at": cached_state.get("updated_at", now_utc()),
        "phases": copy.deepcopy(cached_state.get("phases", {})),
        "artifacts": copy.deepcopy(cached_state.get("artifacts", {})),
        "invalidated": copy.deepcopy(cached_state.get("invalidated", [])),
        "open_questions": copy.deepcopy(cached_state.get("open_questions", [])),
        "unresolved_failures": copy.deepcopy(cached_state.get("unresolved_failures", [])),
        "verification": copy.deepcopy(cached_state.get("verification", {})),
        "review": copy.deepcopy(cached_state.get("review", {})),
        "task_classification": copy.deepcopy(cached_state.get("task_classification", {})),
        "assumptions": copy.deepcopy(cached_state.get("assumptions", [])),
        "decisions": copy.deepcopy(cached_state.get("decisions", [])),
        "evidence_items": copy.deepcopy(cached_state.get("evidence_items", [])),
        "waivers": copy.deepcopy(cached_state.get("waivers", [])),
        "deferred_items": copy.deepcopy(cached_state.get("deferred_items", [])),
        "v3_parse_errors": copy.deepcopy(cached_state.get("v3_parse_errors", [])),
    }


def _validate_state_shape(state: dict[str, Any], path: Path, workflow_id: str) -> None:
    if state.get("version") != 1 or state.get("workflow_id") != workflow_id:
        raise ValueError(f"malformed workflow state: {path}")
    artifacts = state.get("artifacts", {})
    if not isinstance(artifacts, dict):
        raise ValueError(f"malformed workflow artifacts in {path}")
    for key, expected_type in v3_state_defaults().items():
        if key in state and not isinstance(state[key], type(expected_type)):
            raise ValueError(f"malformed v3 state field '{key}' in {path}")


def _validate_artifact_metadata(artifacts: dict[str, Any], path: Path) -> None:
    if not isinstance(artifacts, dict):
        raise ValueError(f"malformed artifact metadata in {path}")
    for artifact_type, metadata in artifacts.items():
        if artifact_type not in ARTIFACT_TYPES:
            raise ValueError(f"unknown artifact type '{artifact_type}' in {path}")
        if not isinstance(metadata, dict):
            raise ValueError(f"artifact '{artifact_type}' in {path} must be a metadata object")
        required = {"id", "type", "path", "version", "status", "hash", "updated_at"}
        missing = sorted(required.difference(metadata))
        if missing:
            raise ValueError(f"artifact '{artifact_type}' in {path} is missing metadata fields: {', '.join(missing)}")


def _normalize_state_cache(state: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(state.get(key))
        for key in (
            "artifacts",
            "current_phase",
            "invalidated",
            "open_questions",
            "phases",
            "review",
            "status",
            "unresolved_failures",
            "updated_at",
            "verification",
            "task_classification",
            "assumptions",
            "decisions",
            "evidence_items",
            "waivers",
            "deferred_items",
            "v3_parse_errors",
        )
    }


def _state_cache_differs(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return _normalize_state_cache(left) != _normalize_state_cache(right)


def _iter_harness_events(paths: HarnessPaths) -> list[dict[str, Any]]:
    event_path = paths.state / "events.jsonl"
    if not event_path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(event_path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON in {event_path}:{line_number}: {exc}") from exc
        if isinstance(event, dict):
            events.append(event)
    return events


def _extract_prompt_text(payload: dict[str, Any]) -> str:
    candidates = [payload.get("prompt"), payload.get("initialPrompt")]
    return "\n".join(str(candidate) for candidate in candidates if isinstance(candidate, str))


def _extract_question_text(payload: dict[str, Any]) -> str:
    texts: list[str] = []
    for candidate in (payload, payload.get("toolArgs"), payload.get("tool_args"), payload.get("toolInput"), payload.get("tool_input")):
        if not isinstance(candidate, dict):
            continue
        for key in ("question", "prompt", "message", "header", "intent"):
            value = candidate.get(key)
            if isinstance(value, str):
                texts.append(value)
        questions = candidate.get("questions")
        if isinstance(questions, list):
            for item in questions:
                if not isinstance(item, dict):
                    continue
                for key in ("question", "message", "header"):
                    value = item.get(key)
                    if isinstance(value, str):
                        texts.append(value)
    return "\n".join(texts)


def _matches_any(text: str, patterns: list[str]) -> bool:
    if not text:
        return False
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def _extract_session_id(event: dict[str, Any]) -> str | None:
    payload = event.get("payload", {})
    if not isinstance(payload, dict):
        return None
    hook_payload = payload.get("payload", {})
    if not isinstance(hook_payload, dict):
        return None
    session_id = hook_payload.get("sessionId") or hook_payload.get("session_id")
    return str(session_id) if isinstance(session_id, str) and session_id else None
