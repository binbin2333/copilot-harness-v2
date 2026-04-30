from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PHASES = [
    "intake",
    "exploration",
    "scope",
    "clarification",
    "design",
    "implementation",
    "verification",
    "review",
    "rework",
    "memory",
    "publish",
    "done",
]


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

    def workflow_dir(self, workflow_id: str) -> Path:
        return self.state / "workflows" / workflow_id


@dataclass(frozen=True)
class WorkflowSelection:
    workflow_id: str
    workflow_type: str
    slug: str


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
    state = {
        "version": 1,
        "workflow_id": workflow_id,
        "workflow_type": workflow_type,
        "status": "active",
        "current_phase": "intake",
        "created_at": timestamp,
        "updated_at": timestamp,
        "phases": {"intake": {"status": "active"}},
        "artifacts": {},
        "invalidated": [],
        "open_questions": [],
        "unresolved_failures": [],
        "verification": {},
        "review": {},
    }
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
    active = [item for item in registry["active"] if item.get("status") == "active"]
    if workflow_id_or_slug:
        matches = [
            item
            for item in active
            if item.get("workflow_id") == workflow_id_or_slug or item.get("slug") == workflow_id_or_slug
        ]
    else:
        matches = active
    if not matches:
        raise LookupError("no active workflow found; run `harness-v2 start <type> <slug>` first")
    if len(matches) > 1:
        ids = ", ".join(item["workflow_id"] for item in matches)
        raise LookupError(f"multiple active workflows match; specify one of: {ids}")
    item = matches[0]
    return WorkflowSelection(item["workflow_id"], item["workflow_type"], item["slug"])


def load_state(paths: HarnessPaths, workflow_id: str) -> dict[str, Any]:
    path = paths.workflow_dir(workflow_id) / "state.json"
    if not path.exists():
        raise FileNotFoundError(f"workflow state not found: {workflow_id}")
    state = read_json(path)
    if state.get("version") != 1 or state.get("workflow_id") != workflow_id:
        raise ValueError(f"malformed workflow state: {path}")
    return state


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


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

