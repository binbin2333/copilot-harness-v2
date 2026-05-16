from __future__ import annotations

import re
from typing import Any

from .v3_schema import ASSUMPTION_STATUSES, EVIDENCE_RESULTS, EVIDENCE_TYPES, REVIEW_VERDICTS, RISKS, V3ParseError, V3ParseResult


START = "<!-- harness:v3:start -->"
END = "<!-- harness:v3:end -->"
TOP_LEVEL_RE = re.compile(r"^([a-z_]+):\s*$")


def parse_v3_blocks(text: str, artifact_type: str, artifact_path: str = "") -> V3ParseResult:
    result = V3ParseResult()
    blocks = re.findall(re.escape(START) + r"(.*?)" + re.escape(END), text, flags=re.DOTALL)
    if not blocks:
        return result
    seen_ids: set[str] = set()
    for block in blocks:
        _parse_block(block, artifact_type, artifact_path, result, seen_ids)
    _validate_review_proof_limit(result, artifact_type, artifact_path)
    return result


def _parse_block(block: str, artifact_type: str, artifact_path: str, result: V3ParseResult, seen_ids: set[str]) -> None:
    sections = _split_sections(block)
    for name, lines in sections.items():
        if name == "task_classification":
            parsed = _parse_mapping(lines)
            _validate_task_classification(parsed, artifact_type, artifact_path, result)
            if parsed:
                result.task_classification = parsed
        elif name == "assumptions":
            result.assumptions.extend(_parse_entries(name, lines, artifact_type, artifact_path, result, seen_ids))
        elif name == "decisions":
            result.decisions.extend(_parse_entries(name, lines, artifact_type, artifact_path, result, seen_ids))
        elif name == "evidence":
            result.evidence_items.extend(_parse_entries(name, lines, artifact_type, artifact_path, result, seen_ids))
        elif name == "waivers":
            result.waivers.extend(_parse_entries(name, lines, artifact_type, artifact_path, result, seen_ids))
        elif name == "deferred_items":
            result.deferred_items.extend(_parse_entries(name, lines, artifact_type, artifact_path, result, seen_ids))
        elif name == "review":
            parsed = _parse_mapping(lines)
            verdict = str(parsed.get("verdict", "")).upper()
            if verdict and verdict not in REVIEW_VERDICTS:
                _add_error(result, artifact_type, artifact_path, name, f"invalid review verdict: {verdict}")
            if verdict:
                parsed["verdict"] = verdict
            result.review = parsed
        else:
            _add_error(result, artifact_type, artifact_path, name, f"unknown v3 section: {name}")


def _split_sections(block: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in block.splitlines():
        if not raw_line.strip():
            continue
        match = TOP_LEVEL_RE.match(raw_line)
        if match:
            current = match.group(1)
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(raw_line)
    return sections


def _parse_mapping(lines: list[str]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for raw_line in lines:
        stripped = raw_line.strip()
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        parsed[key.strip()] = _parse_value(value.strip())
    return parsed


def _parse_entries(
    section: str,
    lines: list[str],
    artifact_type: str,
    artifact_path: str,
    result: V3ParseResult,
    seen_ids: set[str],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in lines:
        stripped = raw_line.strip()
        if stripped.startswith("- "):
            if current is not None:
                _finish_entry(section, current, artifact_type, artifact_path, result, seen_ids, entries)
            current = {}
            stripped = stripped[2:].strip()
        if current is None:
            continue
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        current[key.strip()] = _parse_value(value.strip())
    if current is not None:
        _finish_entry(section, current, artifact_type, artifact_path, result, seen_ids, entries)
    return entries


def _finish_entry(
    section: str,
    entry: dict[str, Any],
    artifact_type: str,
    artifact_path: str,
    result: V3ParseResult,
    seen_ids: set[str],
    entries: list[dict[str, Any]],
) -> None:
    required = {
        "assumptions": ["id", "statement", "risk", "status", "falsification", "evidence_required", "evidence", "owner"],
        "decisions": ["id", "statement", "linked_assumptions", "alternatives", "rationale"],
        "evidence": ["id", "type", "supports", "result", "source"],
        "waivers": ["id", "covers", "risk", "owner", "exit_criteria"],
        "deferred_items": ["id", "reason", "owner", "exit_criteria"],
    }[section]
    missing = [key for key in required if key not in entry]
    if missing:
        _add_error(result, artifact_type, artifact_path, section, f"{entry.get('id', '<unknown>')} missing required field: {', '.join(missing)}")
    entry_id = str(entry.get("id", ""))
    prefix_by_section = {"assumptions": "A", "decisions": "D", "evidence": "E", "waivers": "W", "deferred_items": "F"}
    if entry_id and not re.fullmatch(prefix_by_section[section] + r"\d+", entry_id):
        _add_error(result, artifact_type, artifact_path, section, f"invalid id for {section}: {entry_id}")
    if entry_id:
        if entry_id in seen_ids:
            _add_error(result, artifact_type, artifact_path, section, f"duplicate id: {entry_id}")
        seen_ids.add(entry_id)
    _validate_entry_enums(section, entry, artifact_type, artifact_path, result)
    entry["_artifact_type"] = artifact_type
    entries.append(entry)


def _validate_entry_enums(section: str, entry: dict[str, Any], artifact_type: str, artifact_path: str, result: V3ParseResult) -> None:
    if section == "assumptions":
        if entry.get("risk") not in RISKS:
            _add_error(result, artifact_type, artifact_path, section, f"{entry.get('id', '<unknown>')} invalid risk: {entry.get('risk')}")
        if entry.get("status") not in ASSUMPTION_STATUSES:
            _add_error(result, artifact_type, artifact_path, section, f"{entry.get('id', '<unknown>')} invalid status: {entry.get('status')}")
    if section == "evidence":
        if entry.get("type") not in EVIDENCE_TYPES:
            _add_error(result, artifact_type, artifact_path, section, f"{entry.get('id', '<unknown>')} invalid evidence type: {entry.get('type')}")
        if entry.get("result") not in EVIDENCE_RESULTS:
            _add_error(result, artifact_type, artifact_path, section, f"{entry.get('id', '<unknown>')} invalid evidence result: {entry.get('result')}")


def _validate_task_classification(parsed: dict[str, Any], artifact_type: str, artifact_path: str, result: V3ParseResult) -> None:
    if not parsed:
        return
    if "level" not in parsed:
        _add_error(result, artifact_type, artifact_path, "task_classification", "missing required field: level")
        return
    try:
        level = int(parsed["level"])
    except (TypeError, ValueError):
        _add_error(result, artifact_type, artifact_path, "task_classification", f"invalid level: {parsed.get('level')}")
        return
    if level not in {0, 1, 2, 3}:
        _add_error(result, artifact_type, artifact_path, "task_classification", f"invalid level: {level}")
    parsed["level"] = level
    parsed.setdefault("labels", [])
    parsed.setdefault("rationale", "")


def _validate_review_proof_limit(result: V3ParseResult, artifact_type: str, artifact_path: str) -> None:
    for assumption in result.assumptions:
        if assumption.get("risk") != "high" or assumption.get("status") != "proven":
            continue
        linked = set(assumption.get("evidence", []))
        if not linked:
            continue
        evidence = [item for item in result.evidence_items if item.get("id") in linked and assumption.get("id") in item.get("supports", [])]
        if evidence and all(item.get("type") == "review" for item in evidence):
            _add_error(
                result,
                artifact_type,
                artifact_path,
                "assumptions",
                f"{assumption.get('id')}: high-risk assumption cannot be proven by review evidence alone; need non-review PASS evidence",
            )


def _parse_value(value: str) -> Any:
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [item.strip().strip('"').strip("'") for item in inner.split(",") if item.strip()]
    return value.strip().strip('"').strip("'")


def _add_error(result: V3ParseResult, artifact_type: str, artifact_path: str, block_name: str, message: str) -> None:
    result.errors.append(V3ParseError(artifact_type=artifact_type, artifact_path=artifact_path, block_name=block_name, message=message))

