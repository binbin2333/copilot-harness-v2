from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


ARTIFACT_TYPES = [
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
verification:
  commands: []
"""


@dataclass(frozen=True)
class HarnessConfig:
    strict_workflow: bool = True
    require_design_for_non_trivial: bool = True
    verification_commands: tuple[str, ...] = ()


def load_config(repo: Path) -> HarnessConfig:
    path = repo / ".github" / "harness-v2" / "config.yaml"
    if not path.exists():
        return HarnessConfig()
    text = path.read_text(encoding="utf-8")
    return HarnessConfig(
        strict_workflow=_read_bool(text, "strict_workflow", True),
        require_design_for_non_trivial=_read_bool(text, "require_design_for_non_trivial", True),
        verification_commands=tuple(_read_string_list(text, "commands")),
    )


def write_default_config(repo: Path, overwrite: bool = False) -> Path:
    path = repo / ".github" / "harness-v2" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if overwrite or not path.exists():
        path.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")
    return path


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
                if not stripped:
                    continue
                if not stripped.startswith("- "):
                    break
                result.append(stripped[2:].strip().strip('"').strip("'"))
            return result
    return []

