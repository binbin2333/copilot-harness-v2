from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from .state import HarnessPaths, now_utc, select_workflow


MEMORY_FILES = {
    "failure": "failures.jsonl",
    "correction": "corrections.jsonl",
    "lesson": "lessons.jsonl",
    "project_fact": "project-facts.jsonl",
}


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    type: str
    path: Path


def record_memory(
    paths: HarnessPaths,
    memory_type: str,
    symptom: str,
    root_cause: str,
    prevention: str,
    retrieval_keys: list[str],
    severity: str = "medium",
    module: str | None = None,
    trigger: str | None = None,
    workflow_id: str | None = None,
) -> MemoryRecord:
    if memory_type not in {"failure", "correction"}:
        raise ValueError("record_memory supports failure and correction records")
    selected = _maybe_select(paths, workflow_id)
    record_id = f"{memory_type[:4]}-{uuid.uuid4().hex[:12]}"
    record = {
        "id": record_id,
        "type": memory_type,
        "project": paths.repo.name,
        "workflow_id": selected,
        "module": module,
        "trigger": trigger,
        "symptom": symptom,
        "root_cause": root_cause,
        "prevention": prevention,
        "retrieval_keys": retrieval_keys,
        "severity": severity,
        "status": "active",
        "created_at": now_utc(),
        "resolved_at": None,
    }
    path = paths.memory / MEMORY_FILES[memory_type]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return MemoryRecord(record_id, memory_type, path)


def list_memory(paths: HarnessPaths, memory_type: str | None = None) -> list[dict]:
    memory_types = [memory_type] if memory_type else list(MEMORY_FILES)
    records: list[dict] = []
    for item_type in memory_types:
        if item_type not in MEMORY_FILES:
            raise ValueError(f"unknown memory type: {item_type}")
        path = paths.memory / MEMORY_FILES[item_type]
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


def draft_correction(symptom: str = "", root_cause: str = "", prevention: str = "") -> str:
    return (
        "# Correction Memory Draft\n\n"
        f"- Symptom: {symptom or 'TODO'}\n"
        f"- Root cause: {root_cause or 'TODO'}\n"
        f"- Prevention: {prevention or 'TODO'}\n"
        "- Retrieval keys: TODO\n"
        "- Severity: medium\n"
        "- Status: active\n"
    )


def _maybe_select(paths: HarnessPaths, workflow_id: str | None) -> str | None:
    try:
        return select_workflow(paths, workflow_id).workflow_id
    except Exception:
        return workflow_id

