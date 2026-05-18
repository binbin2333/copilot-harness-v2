from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


ARTIFACT_TYPES = [
    "task-classification",
    "clarification-memo",
    "requirements-summary",
    "context-map",
    "call-chain",
    "test-map",
    "scope-freeze",
    "design",
    "impact-analysis",
    "verification-report",
    "review-report",
    "publish-report",
]

SOURCE_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".mjs",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".swift",
    ".ts",
    ".tsx",
}


DEFAULT_CONFIG_TEXT = """version: 1
gates:
    strict_workflow: true
    require_design_for_non_trivial: true
    require_clarification: true
    protect_state_files: true
    require_verification_commands: true
automation:
    auto_register_artifacts: true
debug:
    fail_fast: false
verification:
    commands: []
workflow:
    # pause_after_phases: [design]
    # List phases after which the agent is allowed to stop and wait for human
    # review before proceeding.  Example: [design] means the agent may stop
    # once the design artifact is registered, so you can review it before
    # implementation begins.  An empty list (the default) means no pauses are
    # allowed mid-workflow.
    pause_after_phases: []
v3:
    enable_v3_gates: true
    v3_gate_mode: enforce
    require_assumption_resolution: true
    require_skip_waivers: true
    require_task_classification: true
    allow_pass_with_gaps: false
"""


@dataclass(frozen=True)
class HarnessConfig:
    strict_workflow: bool = True
    require_design_for_non_trivial: bool = True
    require_clarification: bool = True
    protect_state_files: bool = True
    require_verification_commands: bool = True
    auto_register_artifacts: bool = True
    fail_fast: bool = False
    verification_commands: tuple[str, ...] = ()
    pause_after_phases: tuple[str, ...] = ()
    enable_v3_gates: bool = True
    v3_gate_mode: str = "enforce"
    require_assumption_resolution: bool = True
    require_skip_waivers: bool = True
    require_task_classification: bool = True
    allow_pass_with_gaps: bool = False


def load_config(repo: Path) -> HarnessConfig:
    path = repo / ".github" / "harness-v2" / "config.yaml"
    if not path.exists():
        return HarnessConfig()
    text = path.read_text(encoding="utf-8")
    data = _load_yaml_mapping(text, path)
    gates = _read_section(data, "gates", path)
    automation = _read_section(data, "automation", path)
    debug = _read_section(data, "debug", path)
    verification = _read_section(data, "verification", path)
    workflow = _read_section(data, "workflow", path)
    v3 = _read_section(data, "v3", path)

    v3_gate_mode = _read_string(v3, "v3_gate_mode", "enforce", path, "v3")
    if v3_gate_mode not in {"warn", "enforce"}:
        raise ValueError(f"invalid v3_gate_mode: {v3_gate_mode}. Must be 'warn' or 'enforce'.")
    return HarnessConfig(
        strict_workflow=_read_bool(gates, "strict_workflow", True, path, "gates"),
        require_design_for_non_trivial=_read_bool(gates, "require_design_for_non_trivial", True, path, "gates"),
        require_clarification=_read_bool(gates, "require_clarification", True, path, "gates"),
        protect_state_files=_read_bool(gates, "protect_state_files", True, path, "gates"),
        require_verification_commands=_read_bool(gates, "require_verification_commands", True, path, "gates"),
        auto_register_artifacts=_read_bool(automation, "auto_register_artifacts", True, path, "automation"),
        fail_fast=_read_bool(debug, "fail_fast", False, path, "debug"),
        verification_commands=tuple(_read_string_list(verification, "commands", path, "verification")),
        pause_after_phases=tuple(_read_string_list(workflow, "pause_after_phases", path, "workflow")),
        enable_v3_gates=_read_bool(v3, "enable_v3_gates", True, path, "v3"),
        v3_gate_mode=v3_gate_mode,
        require_assumption_resolution=_read_bool(v3, "require_assumption_resolution", True, path, "v3"),
        require_skip_waivers=_read_bool(v3, "require_skip_waivers", True, path, "v3"),
        require_task_classification=_read_bool(v3, "require_task_classification", True, path, "v3"),
        allow_pass_with_gaps=_read_bool(v3, "allow_pass_with_gaps", False, path, "v3"),
    )


def write_default_config(repo: Path, overwrite: bool = False) -> Path:
    path = repo / ".github" / "harness-v2" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if overwrite or not path.exists():
        path.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")
        return path

    existing = load_config(repo)
    path.write_text(_render_config(existing), encoding="utf-8")
    return path


def _render_config(config: HarnessConfig) -> str:
    payload = {
        "version": 1,
        "gates": {
            "strict_workflow": config.strict_workflow,
            "require_design_for_non_trivial": config.require_design_for_non_trivial,
            "require_clarification": config.require_clarification,
            "protect_state_files": config.protect_state_files,
            "require_verification_commands": config.require_verification_commands,
        },
        "automation": {"auto_register_artifacts": config.auto_register_artifacts},
        "debug": {"fail_fast": config.fail_fast},
        "verification": {"commands": list(config.verification_commands)},
        "workflow": {"pause_after_phases": list(config.pause_after_phases)},
        "v3": {
            "enable_v3_gates": config.enable_v3_gates,
            "v3_gate_mode": config.v3_gate_mode,
            "require_assumption_resolution": config.require_assumption_resolution,
            "require_skip_waivers": config.require_skip_waivers,
            "require_task_classification": config.require_task_classification,
            "allow_pass_with_gaps": config.allow_pass_with_gaps,
        },
    }
    return yaml.safe_dump(payload, sort_keys=False)


def _load_yaml_mapping(text: str, path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML in {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"config root in {path} must be a mapping")
    return data


def _read_section(data: dict[str, Any], key: str, path: Path) -> dict[str, Any]:
    value = data.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"config section '{key}' in {path} must be a mapping")
    return value


def _read_bool(section: dict[str, Any], key: str, default: bool, path: Path, section_name: str) -> bool:
    if key not in section or section[key] is None:
        return default
    value = section[key]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0"}:
            return False
    raise ValueError(f"config key '{section_name}.{key}' in {path} must be a boolean")


def _read_string_list(section: dict[str, Any], key: str, path: Path, section_name: str) -> list[str]:
    if key not in section or section[key] is None:
        return []
    value = section[key]
    if not isinstance(value, list):
        raise ValueError(f"config key '{section_name}.{key}' in {path} must be a list of strings")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError(f"config key '{section_name}.{key}[{index}]' in {path} must be a string")
        result.append(item)
    return result


def _read_string(section: dict[str, Any], key: str, default: str, path: Path, section_name: str) -> str:
    if key not in section or section[key] is None:
        return default
    value = section[key]
    if not isinstance(value, str):
        raise ValueError(f"config key '{section_name}.{key}' in {path} must be a string")
    return value
