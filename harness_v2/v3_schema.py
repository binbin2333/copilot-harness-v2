from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


ASSUMPTION_STATUSES = {"assumed", "challenged", "proven", "rejected", "deferred", "accepted-risk"}
EVIDENCE_RESULTS = {"pass", "fail", "skipped"}
EVIDENCE_TYPES = {"code-trace", "spec", "test", "build", "benchmark", "review", "owner-signoff"}
RISKS = {"high", "medium", "low"}
REVIEW_VERDICTS = {"PASS", "PASS-WITH-GAPS", "BLOCKED"}
NON_REVIEW_PROOF_TYPES = {"code-trace", "spec", "test", "build", "benchmark", "owner-signoff"}


@dataclass(frozen=True)
class V3ParseError:
    artifact_type: str
    artifact_path: str
    block_name: str
    message: str
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_path": self.artifact_path,
            "block_name": self.block_name,
            "message": self.message,
            "line": self.line,
        }


@dataclass
class V3ParseResult:
    task_classification: dict[str, Any] = field(default_factory=dict)
    assumptions: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    evidence_items: list[dict[str, Any]] = field(default_factory=list)
    waivers: list[dict[str, Any]] = field(default_factory=list)
    deferred_items: list[dict[str, Any]] = field(default_factory=list)
    review: dict[str, Any] = field(default_factory=dict)
    errors: list[V3ParseError] = field(default_factory=list)

    @property
    def has_content(self) -> bool:
        return any(
            (
                self.task_classification,
                self.assumptions,
                self.decisions,
                self.evidence_items,
                self.waivers,
                self.deferred_items,
                self.review,
            )
        )


def v3_state_defaults() -> dict[str, Any]:
    return {
        "task_classification": {},
        "assumptions": [],
        "decisions": [],
        "evidence_items": [],
        "waivers": [],
        "deferred_items": [],
        "v3_parse_errors": [],
    }

