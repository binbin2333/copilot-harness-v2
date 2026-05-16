from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


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
    enable_v3_gates: false
    v3_gate_mode: warn
    require_assumption_resolution: false
    require_skip_waivers: false
    require_task_classification: false
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
    enable_v3_gates: bool = False
    v3_gate_mode: str = "warn"
    require_assumption_resolution: bool = False
    require_skip_waivers: bool = False
    require_task_classification: bool = False
    allow_pass_with_gaps: bool = False


def load_config(repo: Path) -> HarnessConfig:
    path = repo / ".github" / "harness-v2" / "config.yaml"
    if not path.exists():
        return HarnessConfig()
    text = path.read_text(encoding="utf-8")
    v3_gate_mode = _read_string(text, "v3_gate_mode", "warn")
    if v3_gate_mode not in {"warn", "enforce"}:
        raise ValueError(f"invalid v3_gate_mode: {v3_gate_mode}. Must be 'warn' or 'enforce'.")
    return HarnessConfig(
        strict_workflow=_read_bool(text, "strict_workflow", True),
        require_design_for_non_trivial=_read_bool(text, "require_design_for_non_trivial", True),
        require_clarification=_read_bool(text, "require_clarification", True),
        protect_state_files=_read_bool(text, "protect_state_files", True),
        require_verification_commands=_read_bool(text, "require_verification_commands", True),
        auto_register_artifacts=_read_bool(text, "auto_register_artifacts", True),
        fail_fast=_read_bool(text, "fail_fast", False),
        verification_commands=tuple(_read_string_list(text, "commands")),
        pause_after_phases=tuple(_read_string_list(text, "pause_after_phases")),
        enable_v3_gates=_read_bool(text, "enable_v3_gates", False),
        v3_gate_mode=v3_gate_mode,
        require_assumption_resolution=_read_bool(text, "require_assumption_resolution", False),
        require_skip_waivers=_read_bool(text, "require_skip_waivers", False),
        require_task_classification=_read_bool(text, "require_task_classification", False),
        allow_pass_with_gaps=_read_bool(text, "allow_pass_with_gaps", False),
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
    lines = [
        "version: 1",
        "gates:",
        f"  strict_workflow: {_format_bool(config.strict_workflow)}",
        f"  require_design_for_non_trivial: {_format_bool(config.require_design_for_non_trivial)}",
        f"  require_clarification: {_format_bool(config.require_clarification)}",
        f"  protect_state_files: {_format_bool(config.protect_state_files)}",
        f"  require_verification_commands: {_format_bool(config.require_verification_commands)}",
        "automation:",
        f"  auto_register_artifacts: {_format_bool(config.auto_register_artifacts)}",
        "debug:",
        f"  fail_fast: {_format_bool(config.fail_fast)}",
        "verification:",
    ]
    if config.verification_commands:
        lines.append("  commands:")
        for command in config.verification_commands:
            lines.append(f"    - {command}")
    else:
        lines.append("  commands: []")
    lines.append("workflow:")
    if config.pause_after_phases:
        lines.append("  pause_after_phases:")
        for phase in config.pause_after_phases:
            lines.append(f"    - {phase}")
    else:
        lines.append("  pause_after_phases: []")
    lines.extend(
        [
            "v3:",
            f"  enable_v3_gates: {_format_bool(config.enable_v3_gates)}",
            f"  v3_gate_mode: {config.v3_gate_mode}",
            f"  require_assumption_resolution: {_format_bool(config.require_assumption_resolution)}",
            f"  require_skip_waivers: {_format_bool(config.require_skip_waivers)}",
            f"  require_task_classification: {_format_bool(config.require_task_classification)}",
            f"  allow_pass_with_gaps: {_format_bool(config.allow_pass_with_gaps)}",
        ]
    )
    return "\n".join(lines) + "\n"


def _format_bool(value: bool) -> str:
    return "true" if value else "false"


def _read_bool(text: str, key: str, default: bool) -> bool:
    prefix = f"{key}:"
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith(prefix):
            value = line[len(prefix) :].strip().lower()
            if value in {"true", "yes", "1"}:
                return True
            if value in {"false", "no", "0"}:
                return False
            raise ValueError(f"invalid boolean for {key}: {value}")
    return default


def _read_string_list(text: str, key: str) -> list[str]:
    lines = text.splitlines()
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if line == f"{key}: []":
            return []
        if line == f"{key}:":
            result: list[str] = []
            for child in lines[index + 1 :]:
                stripped = child.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if not stripped.startswith("- "):
                    break
                result.append(stripped[2:].strip().strip('"').strip("'"))
            return result
    return []


def _read_string(text: str, key: str, default: str) -> str:
    prefix = f"{key}:"
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith(prefix):
            return line[len(prefix) :].strip().strip('"').strip("'")
    return default
