from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .artifacts import create_evidence, register_evidence
from .gates import evaluate_completion_gate, evaluate_implementation_gate, evaluate_verification_gate
from .installer import install
from .memory import draft_correction, list_memory, record_memory
from .state import HarnessPaths, load_state, refresh_workflow_progress, select_workflow, set_phase, start_workflow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harness-v2")
    parser.add_argument("--repo", default=".", help="target repository path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    install_parser = subparsers.add_parser("install", help="install hooks, skills, config, and runtime dirs")
    install_parser.add_argument("repo_path")

    subparsers.add_parser("status", help="show active workflow status")

    start_parser = subparsers.add_parser("start", help="start a workflow")
    start_parser.add_argument("workflow_type")
    start_parser.add_argument("slug")

    phase_parser = subparsers.add_parser("phase", help="show or set workflow phase")
    phase_parser.add_argument("phase", nargs="?")
    phase_parser.add_argument("--workflow")

    evidence_parser = subparsers.add_parser("evidence", help="manage evidence artifacts")
    evidence_subparsers = evidence_parser.add_subparsers(dest="evidence_command", required=True)
    evidence_create = evidence_subparsers.add_parser("create", help="create an evidence template")
    evidence_create.add_argument("artifact_type")
    evidence_create.add_argument("--workflow")
    evidence_add = evidence_subparsers.add_parser("add", help="register an evidence artifact")
    evidence_add.add_argument("artifact_type")
    evidence_add.add_argument("path")
    evidence_add.add_argument("--workflow")

    gate_parser = subparsers.add_parser("gate", help="evaluate gates")
    gate_subparsers = gate_parser.add_subparsers(dest="gate_command", required=True)
    gate_implementation = gate_subparsers.add_parser("implementation", help="evaluate implementation gate")
    gate_implementation.add_argument("--workflow")
    gate_implementation.add_argument("--path", action="append", default=[])
    gate_completion = gate_subparsers.add_parser("completion", help="evaluate completion gate")
    gate_completion.add_argument("--workflow")
    gate_verification = gate_subparsers.add_parser("verification", help="evaluate verification gate")
    gate_verification.add_argument("--workflow")

    memory_parser = subparsers.add_parser("memory", help="manage structured memory")
    memory_subparsers = memory_parser.add_subparsers(dest="memory_command", required=True)
    draft_parser = memory_subparsers.add_parser("draft-correction", help="print a correction-memory draft")
    draft_parser.add_argument("--symptom", default="")
    draft_parser.add_argument("--root-cause", default="")
    draft_parser.add_argument("--prevention", default="")
    record_failure = memory_subparsers.add_parser("record-failure", help="record failure memory")
    _add_memory_record_args(record_failure)
    record_correction = memory_subparsers.add_parser("record-correction", help="record correction memory")
    _add_memory_record_args(record_correction)
    list_parser = memory_subparsers.add_parser("list", help="list memory records")
    list_parser.add_argument("--type", choices=["failure", "correction", "lesson", "project_fact"])

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "install":
        written = install(Path(args.repo_path))
        print(json.dumps({"installed": [str(path) for path in written]}, indent=2))
        return 0

    paths = HarnessPaths(Path(args.repo))

    if args.command == "status":
        return _status(paths)
    if args.command == "start":
        state = start_workflow(paths, args.workflow_type, args.slug)
        print(json.dumps(state, indent=2, sort_keys=True))
        return 0
    if args.command == "phase":
        return _phase(paths, args)
    if args.command == "evidence":
        return _evidence(paths, args)
    if args.command == "gate":
        return _gate(paths, args)
    if args.command == "memory":
        return _memory(paths, args)
    raise AssertionError(f"unhandled command: {args.command}")


def _status(paths: HarnessPaths) -> int:
    if not paths.active_workflows.exists():
        print(json.dumps({"active": []}, indent=2, sort_keys=True))
        return 0
    try:
        selected = select_workflow(paths)
        state = load_state(paths, selected.workflow_id)
        refresh_workflow_progress(state)
        payload = {
            "workflow_id": selected.workflow_id,
            "workflow_type": selected.workflow_type,
            "slug": selected.slug,
            "status": state.get("status"),
            "current_phase": state.get("current_phase"),
            "invalidated": state.get("invalidated", []),
            "artifacts": sorted(state.get("artifacts", {}).keys()),
        }
    except LookupError:
        payload = {"active": []}
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _phase(paths: HarnessPaths, args: argparse.Namespace) -> int:
    if args.phase:
        state = set_phase(paths, args.phase, args.workflow)
    else:
        selected = select_workflow(paths, args.workflow)
        state = load_state(paths, selected.workflow_id)
        refresh_workflow_progress(state)
    print(json.dumps({"workflow_id": state["workflow_id"], "current_phase": state["current_phase"]}, indent=2))
    return 0


def _evidence(paths: HarnessPaths, args: argparse.Namespace) -> int:
    if args.evidence_command == "create":
        artifact_path = create_evidence(paths, args.artifact_type, args.workflow)
        print(str(artifact_path))
        return 0
    if args.evidence_command == "add":
        registration = register_evidence(paths, args.artifact_type, Path(args.path), args.workflow)
        print(
            json.dumps(
                {
                    "artifact_id": registration.artifact_id,
                    "artifact_type": registration.artifact_type,
                    "path": str(registration.path),
                    "version": registration.version,
                    "changed": registration.changed,
                    "invalidated": registration.invalidated,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    raise AssertionError(f"unhandled evidence command: {args.evidence_command}")


def _gate(paths: HarnessPaths, args: argparse.Namespace) -> int:
    if args.gate_command == "implementation":
        decision = evaluate_implementation_gate(paths, [Path(value) for value in args.path], args.workflow)
    elif args.gate_command == "completion":
        decision = evaluate_completion_gate(paths, args.workflow)
    elif args.gate_command == "verification":
        decision = evaluate_verification_gate(paths, args.workflow)
    else:
        raise AssertionError(f"unhandled gate command: {args.gate_command}")
    print(json.dumps(decision.as_dict(), indent=2, sort_keys=True))
    return 2 if decision.decision == "deny" else 0


def _memory(paths: HarnessPaths, args: argparse.Namespace) -> int:
    if args.memory_command == "draft-correction":
        print(draft_correction(args.symptom, args.root_cause, args.prevention))
        return 0
    if args.memory_command in {"record-failure", "record-correction"}:
        memory_type = "failure" if args.memory_command == "record-failure" else "correction"
        record = record_memory(
            paths,
            memory_type,
            args.symptom,
            args.root_cause,
            args.prevention,
            args.key,
            args.severity,
            args.module,
            args.trigger,
            args.workflow,
        )
        print(json.dumps({"id": record.id, "type": record.type, "path": str(record.path)}, indent=2))
        return 0
    if args.memory_command == "list":
        print(json.dumps(list_memory(paths, args.type), indent=2, sort_keys=True))
        return 0
    raise AssertionError(f"unhandled memory command: {args.memory_command}")


def _add_memory_record_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--symptom", required=True)
    parser.add_argument("--root-cause", required=True)
    parser.add_argument("--prevention", required=True)
    parser.add_argument("--key", action="append", default=[], dest="key")
    parser.add_argument("--severity", default="medium")
    parser.add_argument("--module")
    parser.add_argument("--trigger")
    parser.add_argument("--workflow")


if __name__ == "__main__":
    raise SystemExit(main())
