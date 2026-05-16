from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from harness_v2.artifacts import _sha256, create_evidence, register_evidence
from harness_v2.config import load_config
from harness_v2.events import handle_hook_event
from harness_v2.gates import evaluate_completion_gate, evaluate_implementation_gate
from harness_v2.installer import install
from harness_v2.memory import list_memory, record_memory
from harness_v2.state import HarnessPaths, inspect_workflow, load_state, start_workflow


class HarnessCoreTests(unittest.TestCase):
    def test_start_workflow_creates_state_and_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            state = start_workflow(paths, "feature", "Auth Flow")

            self.assertEqual(state["workflow_id"], "feature-auth-flow")
            self.assertEqual(state["current_phase"], "clarification")
            registry = json.loads(paths.active_workflows.read_text(encoding="utf-8"))
            self.assertEqual(registry["active"][0]["workflow_id"], "feature-auth-flow")
            self.assertTrue((paths.workflow_dir("feature-auth-flow") / "events.jsonl").exists())

    def test_registering_core_evidence_advances_current_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "Auth")

            self.assertEqual(load_state(paths, "feature-auth")["current_phase"], "clarification")
            _register_artifact(paths, "clarification-memo", "no questions")
            self.assertEqual(load_state(paths, "feature-auth")["current_phase"], "requirements-summary")
            _register_artifact(paths, "requirements-summary", "requirements")
            self.assertEqual(load_state(paths, "feature-auth")["current_phase"], "context-map")
            _register_artifact(paths, "context-map", "context")
            self.assertEqual(load_state(paths, "feature-auth")["current_phase"], "scope-freeze")
            _register_artifact(paths, "scope-freeze", "scope")
            self.assertEqual(load_state(paths, "feature-auth")["current_phase"], "design")
            _register_artifact(paths, "design", "design")
            self.assertEqual(load_state(paths, "feature-auth")["current_phase"], "implementation")
            _register_artifact(paths, "verification-report", "tests passed")
            self.assertEqual(load_state(paths, "feature-auth")["current_phase"], "review")
            _register_artifact(paths, "review-report", "looks good")
            final_state = load_state(paths, "feature-auth")
            self.assertEqual(final_state["current_phase"], "done")
            self.assertEqual(final_state["status"], "complete")

    def test_clarification_requires_user_interaction_or_explicit_waiver(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "Auth")
            artifact = create_evidence(paths, "clarification-memo")
            artifact.write_text("No open questions", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "requires at least one ask_user interaction"):
                register_evidence(paths, "clarification-memo", artifact)

    def test_clarification_allows_explicit_user_waiver_when_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "Auth")
            _record_user_waiver(paths)
            artifact = create_evidence(paths, "clarification-memo")
            artifact.write_text("User waiver: user explicitly said there is no ambiguity and clarification is waived.", encoding="utf-8")

            registration = register_evidence(paths, "clarification-memo", artifact)

            self.assertEqual(registration.artifact_type, "clarification-memo")

    def test_scope_freeze_requires_user_confirmed_verification_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "Auth")
            _register_artifact(paths, "clarification-memo", "Q: proceed?\nA: yes")
            _register_artifact(paths, "requirements-summary", "requirements")
            _register_artifact(paths, "context-map", "context")
            _configure_verification_commands(paths)
            artifact = create_evidence(paths, "scope-freeze")
            artifact.write_text("scope", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "user-confirmed verification plan"):
                register_evidence(paths, "scope-freeze", artifact)

    def test_scope_freeze_allows_explicit_user_provided_verification_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "Auth")
            _register_artifact(paths, "clarification-memo", "Q: proceed?\nA: yes")
            _register_artifact(paths, "requirements-summary", "requirements")
            _register_artifact(paths, "context-map", "context")
            _record_user_verification_plan(paths)
            _configure_verification_commands(paths, ("pytest -q", "python -m build"))
            artifact = create_evidence(paths, "scope-freeze")
            artifact.write_text("scope", encoding="utf-8")

            registration = register_evidence(paths, "scope-freeze", artifact)

            self.assertEqual(registration.artifact_type, "scope-freeze")

    def test_evidence_registration_hashes_and_invalidates_downstream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "Auth")
            _record_verification_interaction(paths)
            _configure_verification_commands(paths)
            artifact_path = create_evidence(paths, "scope-freeze")
            artifact_path.write_text("# Scope\n\nIn scope: auth.\n", encoding="utf-8")

            registration = register_evidence(paths, "scope-freeze", artifact_path)
            state = load_state(paths, "feature-auth")

            self.assertTrue(registration.changed)
            self.assertEqual(registration.version, 1)
            self.assertIn("scope-freeze", state["artifacts"])
            self.assertIn("implementation", state["invalidated"])
            self.assertEqual(state["phases"]["verification"]["status"], "invalidated")

    def test_evidence_registration_clears_satisfied_invalidated_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "Auth")
            _record_verification_interaction(paths)
            _configure_verification_commands(paths)
            scope_path = create_evidence(paths, "scope-freeze")
            scope_path.write_text("# Scope\n", encoding="utf-8")
            register_evidence(paths, "scope-freeze", scope_path)

            verification_path = create_evidence(paths, "verification-report")
            verification_path.write_text("# Verification\n\nRan: pytest -q\nPassed.\n", encoding="utf-8")
            register_evidence(paths, "verification-report", verification_path)
            state = load_state(paths, "feature-auth")

            self.assertNotIn("implementation", state["invalidated"])
            self.assertNotIn("verification", state["invalidated"])
            self.assertEqual(state["phases"]["implementation"]["status"], "complete")
            self.assertEqual(state["phases"]["verification"]["status"], "complete")

    def test_evidence_registration_rejects_files_outside_repository(self) -> None:
        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.TemporaryDirectory() as outside_tmp:
            paths = HarnessPaths(Path(repo_tmp))
            start_workflow(paths, "feature", "Auth")
            outside_artifact = Path(outside_tmp) / "context-map.md"
            outside_artifact.write_text("# External\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "inside repository"):
                register_evidence(paths, "context-map", outside_artifact)

    def test_hashing_rejects_symlinks_at_open_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target.md"
            target.write_text("# Target\n", encoding="utf-8")
            symlink = Path(tmp) / "artifact.md"
            try:
                symlink.symlink_to(target)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")

            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                _sha256(symlink)

    def test_implementation_gate_denies_business_code_without_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "Auth")

            decision = evaluate_implementation_gate(paths, [Path("src/app.py")])

            self.assertEqual(decision.decision, "deny")
            self.assertIn("context-map", decision.missing)
            self.assertIn("scope-freeze", decision.missing)
            self.assertNotIn("design", decision.missing)

    def test_implementation_gate_requires_design_for_non_trivial_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "Auth")

            decision = evaluate_implementation_gate(paths, [Path("src/app.py"), Path("src/auth.py")])

            self.assertEqual(decision.decision, "deny")
            self.assertIn("design", decision.missing)

    def test_implementation_gate_allows_harness_evidence_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "Auth")

            decision = evaluate_implementation_gate(
                paths,
                [Path(".github/harness-v2/state/workflows/feature-auth/artifacts/context-map.md")],
            )

            self.assertEqual(decision.decision, "allow")

    def test_memory_recording_and_listing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "Auth")

            record = record_memory(
                paths,
                "failure",
                "Skipped tests",
                "Assumed no tests existed",
                "Check configured verification commands",
                ["verification", "tests"],
            )
            records = list_memory(paths, "failure")

            self.assertEqual(record.type, "failure")
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["symptom"], "Skipped tests")
            self.assertEqual(records[0]["workflow_id"], "feature-auth")

    def test_installer_writes_hooks_skills_config_and_gitignore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)

            written = install(repo)

            self.assertIn(repo / ".github" / "hooks" / "harness-v2.json", written)
            self.assertTrue((repo / ".github" / "harness-v2" / "config.yaml").exists())
            self.assertTrue((repo / ".github" / "harness-v2" / "bin" / "harness-v2").exists())
            self.assertTrue((repo / ".github" / "harness-v2" / "hooks" / "permission_request.py").exists())
            self.assertTrue((repo / ".github" / "harness-v2" / "hooks" / "pre_tool_use.py").exists())
            self.assertTrue((repo / ".github" / "harness-v2" / "runtime" / "harness_v2" / "events.py").exists())
            self.assertTrue((repo / ".github" / "harness-v2" / "runtime" / "harness_v2" / "artifacts.py").exists())
            self.assertTrue((repo / ".github" / "harness-v2" / "runtime" / "harness_v2" / "cli.py").exists())
            self.assertTrue((repo / ".github" / "harness-v2" / "runtime" / "harness_v2" / "installer.py").exists())
            self.assertTrue((repo / ".github" / "harness-v2" / "runtime" / "harness_v2" / "memory.py").exists())
            self.assertTrue((repo / ".github" / "skills" / "context-map" / "SKILL.md").exists())
            guide = (repo / ".github" / "harness-v2" / "AGENTS_GUIDE.md").read_text(encoding="utf-8")
            self.assertIn("git commit", guide)
            self.assertIn("doctor", guide)
            gitignore = (repo / ".gitignore").read_text(encoding="utf-8")
            self.assertIn(".github/harness-v2/state/", gitignore)
            hook = (repo / ".github" / "harness-v2" / "hooks" / "pre_tool_use.py").read_text(encoding="utf-8")
            self.assertIn('RUNTIME_ROOT = Path(__file__).resolve().parents[1] / "runtime"', hook)
            self.assertNotIn("/home/", hook)
            hook_config = json.loads((repo / ".github" / "hooks" / "harness-v2.json").read_text(encoding="utf-8"))
            self.assertIn("permissionRequest", hook_config["hooks"])
            self.assertIsInstance(hook_config["hooks"]["preToolUse"], list)

    def test_install_upgrades_existing_config_with_new_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            config_path = repo / ".github" / "harness-v2" / "config.yaml"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                "version: 1\n"
                "gates:\n"
                "  strict_workflow: false\n"
                "  require_design_for_non_trivial: true\n"
                "verification:\n"
                "  commands:\n"
                "    - pytest -q\n",
                encoding="utf-8",
            )

            install(repo)

            upgraded = config_path.read_text(encoding="utf-8")
            self.assertIn("strict_workflow: false", upgraded)
            self.assertIn("require_clarification: true", upgraded)
            self.assertIn("protect_state_files: true", upgraded)
            self.assertIn("require_verification_commands: true", upgraded)
            self.assertIn("auto_register_artifacts: true", upgraded)
            self.assertIn("fail_fast: false", upgraded)
            self.assertIn("- pytest -q", upgraded)

    # --- completion gate tests ---

    def test_completion_gate_allows_when_no_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            decision = evaluate_completion_gate(paths)
            self.assertEqual(decision.decision, "allow")

    def test_completion_gate_denies_missing_both_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "My task")
            decision = evaluate_completion_gate(paths)
            self.assertEqual(decision.decision, "deny")
            self.assertIn("verification-report", decision.missing)
            self.assertIn("review-report", decision.missing)
            self.assertIn("workflow-phase(clarification)", decision.missing)

    def test_completion_gate_denies_before_done_even_with_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "My task")
            _configure_verification_commands(paths)
            vr = create_evidence(paths, "verification-report")
            vr.write_text("Ran: pytest -q\ntests passed", encoding="utf-8")
            register_evidence(paths, "verification-report", vr)
            rr = create_evidence(paths, "review-report")
            rr.write_text("looks good", encoding="utf-8")
            register_evidence(paths, "review-report", rr)
            decision = evaluate_completion_gate(paths)
            self.assertEqual(decision.decision, "deny")
            self.assertIn("workflow-phase(clarification)", decision.missing)

    def test_completion_gate_denies_with_invalidated_phases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "My task")
            _register_full_workflow(paths)
            context_path = create_evidence(paths, "context-map")
            context_path.write_text("context updated", encoding="utf-8")
            register_evidence(paths, "context-map", context_path)
            decision = evaluate_completion_gate(paths)
            self.assertEqual(decision.decision, "deny")
            self.assertTrue(any("resolved-invalidations" in m for m in decision.missing))

    def test_completion_gate_allows_when_all_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "My task")
            _register_full_workflow(paths)
            decision = evaluate_completion_gate(paths)
            self.assertEqual(decision.decision, "allow")

    # --- agentStop hook tests ---

    def test_agent_stop_hook_blocks_without_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "My task")
            output = _capture_hook_output(Path(tmp), "agentStop", {})
            self.assertEqual(output["result"], 0)
            self.assertEqual(output["payload"]["decision"], "block")

            events = _workflow_events(paths, "feature-my-task")
            denial = next(event for event in events if event["type"] == "gate_denied")
            self.assertEqual(denial["payload"]["hook_name"], "agentStop")
            self.assertEqual(denial["payload"]["gate"], "completion")
            self.assertIn("verification-report", denial["payload"]["formatted_reason"])

    def test_agent_stop_hook_allows_with_complete_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "My task")
            _register_full_workflow(paths)
            result = handle_hook_event(Path(tmp), "agentStop", {})
            self.assertEqual(result, 0)

    def test_agent_stop_hook_allows_when_no_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = handle_hook_event(Path(tmp), "agentStop", {})
            self.assertEqual(result, 0)

    def test_subagent_stop_hook_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "My task")
            output = _capture_hook_output(Path(tmp), "subagentStop", {})
            self.assertEqual(output["result"], 0)
            self.assertEqual(output["payload"]["decision"], "block")

    def test_agent_stop_hook_reports_v3_warn_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "My task")
            _configure_verification_commands(paths, enable_v3_gates=True, v3_gate_mode="warn")
            _register_full_workflow_with_v3(paths, scope_block=_v3_scope_block(status="challenged", evidence=[]))

            output = _capture_hook_output(Path(tmp), "agentStop", {})

            self.assertEqual(output["result"], 0)
            self.assertEqual(output["payload"]["decision"], "warn")
            self.assertIn("A1 blocks completion", output["payload"]["reason"])

    def test_status_includes_completion_gate_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "My task")
            _configure_verification_commands(paths, enable_v3_gates=True, v3_gate_mode="warn")
            _register_full_workflow_with_v3(paths, scope_block=_v3_scope_block(status="challenged", evidence=[]))

            from harness_v2.cli import _status

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                _status(paths)
            status = json.loads(buffer.getvalue())

            self.assertEqual(status["completion_gate"]["decision"], "warn")
            self.assertTrue(any("A1 blocks completion" in item for item in status["completion_gate"]["missing"]))

    def test_implementation_gate_denies_when_scope_freeze_invalidated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "My task")
            _record_verification_interaction(paths)
            _configure_verification_commands(paths)
            # register context-map and scope-freeze
            cm = create_evidence(paths, "context-map")
            cm.write_text("context", encoding="utf-8")
            register_evidence(paths, "context-map", cm)
            sf = create_evidence(paths, "scope-freeze")
            sf.write_text("scope v1", encoding="utf-8")
            register_evidence(paths, "scope-freeze", sf)
            # now re-register context-map with changed content → invalidates scope-freeze
            cm.write_text("context updated", encoding="utf-8")
            register_evidence(paths, "context-map", cm)
            # scope-freeze is now invalidated — should block writes
            decision = evaluate_implementation_gate(paths, [Path("src/app.py")])
            self.assertEqual(decision.decision, "deny")
            self.assertTrue(any("scope-freeze" in m for m in decision.missing))

    def test_requirements_summary_invalidates_context_not_clarification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "My task")
            _register_artifact(paths, "clarification-memo", "no questions")
            _register_artifact(paths, "requirements-summary", "requirements v1")
            _register_artifact(paths, "context-map", "context v1")
            req = create_evidence(paths, "requirements-summary")
            req.write_text("requirements v2", encoding="utf-8")
            register_evidence(paths, "requirements-summary", req)
            state2 = load_state(paths, _single_workflow_id(paths))
            self.assertNotIn("clarification", state2.get("invalidated", []))
            self.assertIn("context-map", state2.get("invalidated", []))
            self.assertEqual(state2["current_phase"], "context-map")

    def test_clarification_change_rewinds_to_requirements_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "My task")
            _register_artifact(paths, "clarification-memo", "no questions v1")
            _register_artifact(paths, "requirements-summary", "requirements v1")
            _register_artifact(paths, "context-map", "context v1")
            _record_clarification_interaction(paths)
            memo = create_evidence(paths, "clarification-memo")
            memo.write_text("no questions v2", encoding="utf-8")
            register_evidence(paths, "clarification-memo", memo)
            state = load_state(paths, _single_workflow_id(paths))
            self.assertIn("requirements-summary", state.get("invalidated", []))
            self.assertEqual(state["current_phase"], "requirements-summary")

    def test_pre_tool_use_blocks_git_commit_until_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "My task")
            output = _capture_hook_output(Path(tmp), "preToolUse", {"tool": "bash", "command": "git commit -m test"})
            self.assertEqual(output["result"], 0)
            self.assertEqual(output["payload"]["permissionDecision"], "deny")

    def test_pre_tool_use_logs_workflow_gate_denial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "My task")

            output = _capture_hook_output(Path(tmp), "preToolUse", {"tool": "edit", "path": "src/app.py"})

            self.assertEqual(output["result"], 0)
            self.assertEqual(output["payload"]["permissionDecision"], "deny")
            events = _workflow_events(paths, "feature-my-task")
            denial = next(event for event in events if event["type"] == "gate_denied")
            self.assertEqual(denial["payload"]["hook_name"], "preToolUse")
            self.assertEqual(denial["payload"]["gate"], "implementation")
            self.assertIn("context-map", denial["payload"]["missing"])
            self.assertEqual(denial["payload"]["tool_name"], "edit")

    def test_pre_tool_use_allows_git_commit_when_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "My task")
            _register_full_workflow(paths)
            result = handle_hook_event(Path(tmp), "preToolUse", {"tool": "bash", "command": "git commit -m test"})
            self.assertEqual(result, 0)

    def test_load_state_repairs_cache_from_registration_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "My task")
            _register_artifact(paths, "clarification-memo", "no questions")
            _register_artifact(paths, "requirements-summary", "requirements")
            _register_artifact(paths, "context-map", "context")

            state_file = paths.workflow_dir("feature-my-task") / "state.json"
            state = json.loads(state_file.read_text(encoding="utf-8"))
            state["artifacts"] = {"context-map": str(paths.repo / "context-map.md")}
            state["current_phase"] = "review"
            state_file.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

            repaired = load_state(paths, "feature-my-task")

            self.assertEqual(repaired["current_phase"], "scope-freeze")
            self.assertIsInstance(repaired["artifacts"]["context-map"], dict)

    def test_scope_freeze_requires_verification_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "My task")
            _register_artifact(paths, "clarification-memo", "no questions")
            _register_artifact(paths, "requirements-summary", "requirements")
            _register_artifact(paths, "context-map", "context")
            _record_verification_interaction(paths)
            scope = create_evidence(paths, "scope-freeze")
            scope.write_text("scope", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "verification.commands"):
                register_evidence(paths, "scope-freeze", scope)

    def test_register_evidence_rejects_noncanonical_in_repo_artifact_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "My task")
            wrong_path = paths.repo / ".github" / "harness-v2" / "artifacts" / "feature-my-task" / "context-map.md"
            wrong_path.parent.mkdir(parents=True, exist_ok=True)
            wrong_path.write_text("context", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "artifact path must match active workflow artifact location"):
                register_evidence(paths, "context-map", wrong_path)

    def test_permission_request_interrupts_on_protected_state_edit_when_fail_fast(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "My task")
            _configure_verification_commands(paths, fail_fast=True)
            protected = paths.workflow_dir("feature-my-task") / "state.json"

            output = _capture_hook_output(
                Path(tmp),
                "permissionRequest",
                {"toolName": "edit", "toolArgs": {"path": str(protected)}},
            )

            self.assertEqual(output["result"], 0)
            self.assertEqual(output["payload"]["behavior"], "deny")
            self.assertTrue(output["payload"]["interrupt"])

    def test_post_tool_use_auto_registers_artifact_by_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "My task")
            _record_clarification_interaction(paths)
            artifact = paths.workflow_dir("feature-my-task") / "artifacts" / "clarification-memo.md"
            artifact.write_text("No open questions", encoding="utf-8")

            result = handle_hook_event(
                Path(tmp),
                "postToolUse",
                {"tool_name": "create", "tool_input": {"path": str(artifact)}},
            )
            state = load_state(paths, "feature-my-task")

            self.assertEqual(result, 0)
            self.assertEqual(state["artifacts"]["clarification-memo"]["status"], "current")

    def test_doctor_reports_clean_install_and_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            install(repo)
            paths = HarnessPaths(repo)
            start_workflow(paths, "feature", "My task")
            _register_full_workflow(paths)

            report = inspect_workflow(paths)

            self.assertTrue(report["ok"])
            self.assertEqual(report["workflow_id"], "feature-my-task")

    # ------------------------------------------------------------------
    # Bug fix: design-plan.md filename alias
    # ------------------------------------------------------------------

    def test_artifact_location_recognises_design_plan_filename(self) -> None:
        """design-plan.md must be treated as the 'design' artifact type."""
        from harness_v2.state import artifact_location_for_path
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "Auth")
            wf_id = "feature-auth"
            design_plan = paths.workflow_dir(wf_id) / "artifacts" / "design-plan.md"
            design_plan.parent.mkdir(parents=True, exist_ok=True)
            design_plan.write_text("Design plan content", encoding="utf-8")

            location = artifact_location_for_path(paths, design_plan)

            self.assertIsNotNone(location, "design-plan.md should be recognised as an artifact")
            self.assertEqual(location.artifact_type, "design")
            self.assertEqual(location.workflow_id, wf_id)

    def test_design_plan_md_is_not_a_protected_state_path(self) -> None:
        """design-plan.md in the artifacts dir must NOT be treated as a protected state path."""
        from harness_v2.state import is_protected_state_path
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "Auth")
            design_plan = paths.workflow_dir("feature-auth") / "artifacts" / "design-plan.md"
            design_plan.parent.mkdir(parents=True, exist_ok=True)
            design_plan.write_text("x", encoding="utf-8")

            self.assertFalse(is_protected_state_path(paths, design_plan))

    # ------------------------------------------------------------------
    # Bug fix: select_workflow auto-disambiguates when one workflow is done
    # ------------------------------------------------------------------

    def test_select_workflow_auto_picks_incomplete_when_done_workflow_also_active(self) -> None:
        """When there are two active workflows but one is already done,
        select_workflow should auto-select the incomplete one."""
        from harness_v2.state import select_workflow
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))

            # Create and fully complete the first workflow
            start_workflow(paths, "feature", "Old Work")
            _register_full_workflow(paths)

            # Create a second (incomplete) workflow
            start_workflow(paths, "bugfix", "New Fix")

            selected = select_workflow(paths)
            self.assertEqual(selected.workflow_id, "bugfix-new-fix")

    def test_select_workflow_still_raises_when_both_incomplete(self) -> None:
        """When two incomplete workflows exist, select_workflow must still raise."""
        from harness_v2.state import select_workflow
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "Work A")
            start_workflow(paths, "bugfix", "Work B")

            with self.assertRaisesRegex(LookupError, "multiple active workflows match"):
                select_workflow(paths)

    # ------------------------------------------------------------------
    # Bug fix: pre_tool_use no longer errors when a completed workflow
    # is still present in active-workflows.json
    # ------------------------------------------------------------------

    def test_pre_tool_use_does_not_error_when_completed_workflow_is_still_registered(self) -> None:
        """Regression: writing an artifact to a workflow's state dir must not
        cause a hook error when another (completed) workflow is also active."""
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            _configure_verification_commands(paths)

            # Create and complete a first workflow
            start_workflow(paths, "feature", "Done Feature")
            _register_full_workflow(paths)

            # Start a second (incomplete) workflow
            start_workflow(paths, "bugfix", "Active Fix")
            _register_artifact(paths, "clarification-memo", "all clear")

            artifact_path = paths.workflow_dir("bugfix-active-fix") / "artifacts" / "requirements-summary.md"
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_text("requirements content", encoding="utf-8")

            out = _capture_hook_output(
                paths.repo,
                "preToolUse",
                {"toolName": "create", "toolArgs": {"path": str(artifact_path)}},
            )

            self.assertEqual(out["result"], 0)
            if out["payload"] is not None:
                self.assertNotEqual(out["payload"].get("permissionDecision"), "deny")

    # ------------------------------------------------------------------
    # Bug fix: _read_string_list skips comment lines
    # ------------------------------------------------------------------

    def test_config_verification_commands_ignores_comment_lines(self) -> None:
        """# comment lines inside a YAML list must be skipped, not break the list."""
        from harness_v2.config import load_config
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            config_path = paths.repo / ".github" / "harness-v2" / "config.yaml"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                "version: 1\n"
                "verification:\n"
                "  commands:\n"
                "    # Run the full build first\n"
                "    - ./build.sh --target x64\n"
                "    # Then run tests\n"
                "    - pytest -q\n",
                encoding="utf-8",
            )

            config = load_config(paths.repo)

            self.assertEqual(config.verification_commands, ("./build.sh --target x64", "pytest -q"))

    def test_config_rejects_invalid_v3_gate_mode(self) -> None:
        from harness_v2.config import load_config

        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            config_path = paths.repo / ".github" / "harness-v2" / "config.yaml"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                "version: 1\n"
                "v3:\n"
                "  enable_v3_gates: true\n"
                "  v3_gate_mode: enforced\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "v3_gate_mode"):
                load_config(paths.repo)

    # ------------------------------------------------------------------
    # v3 assumption/evidence workflow
    # ------------------------------------------------------------------

    def test_v3_block_absent_is_backward_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "My task")

            _register_artifact(paths, "requirements-summary", "requirements without structured v3 block")
            state = load_state(paths, "feature-my-task")

            self.assertEqual(state["task_classification"], {})
            self.assertEqual(state["assumptions"], [])
            self.assertEqual(state["v3_parse_errors"], [])

    def test_v3_block_parses_state_from_registered_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "My task")
            _configure_verification_commands(paths, enable_v3_gates=True)

            _register_artifact(paths, "requirements-summary", _v3_classification_block(level=2))
            _register_artifact(paths, "scope-freeze", _v3_scope_block())
            state = load_state(paths, "feature-my-task")

            self.assertEqual(state["task_classification"]["level"], 2)
            self.assertEqual(state["assumptions"][0]["id"], "A1")
            self.assertEqual(state["decisions"][0]["linked_assumptions"], ["A1"])

    def test_v3_parse_error_is_status_visible_in_warn_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "My task")
            _configure_verification_commands(paths, enable_v3_gates=True, v3_gate_mode="warn")

            _register_artifact(paths, "requirements-summary", _v3_classification_block(level=2))
            _register_artifact(paths, "scope-freeze", _v3_malformed_assumption_block())
            state = load_state(paths, "feature-my-task")

            self.assertEqual(state["artifacts"]["scope-freeze"]["v3_status"], "parse-error")
            self.assertTrue(any(error["artifact_type"] == "scope-freeze" for error in state["v3_parse_errors"]))

            from harness_v2.cli import _status

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                _status(paths)
            status = json.loads(buffer.getvalue())
            self.assertIn("v3_parse_errors", status)
            self.assertEqual(status["v3_gate_mode"], "warn")

    def test_v3_parse_error_is_status_visible_and_blocks_completion_in_enforce_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "My task")
            _configure_verification_commands(paths, enable_v3_gates=True, v3_gate_mode="enforce")
            _register_artifact(paths, "clarification-memo", "no questions")
            _register_artifact(paths, "requirements-summary", _v3_classification_block(level=2))
            _register_artifact(paths, "context-map", "context")
            _register_artifact(paths, "scope-freeze", _v3_malformed_assumption_block())
            _register_artifact(paths, "design", "design")
            _register_artifact(paths, "verification-report", _v3_evidence_block())
            _register_artifact(paths, "review-report", _v3_review_verdict_block("PASS"))

            state = load_state(paths, "feature-my-task")
            decision = evaluate_completion_gate(paths)

            self.assertEqual(state["artifacts"]["scope-freeze"]["v3_status"], "parse-error")
            self.assertEqual(decision.decision, "deny")
            self.assertTrue(any("parse error" in item for item in decision.missing))

    def test_v3_unresolved_high_risk_assumption_warns_or_blocks_by_mode(self) -> None:
        for mode, expected in (("warn", "warn"), ("enforce", "deny")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                paths = HarnessPaths(Path(tmp))
                start_workflow(paths, "feature", "My task")
                _configure_verification_commands(paths, enable_v3_gates=True, v3_gate_mode=mode)
                _register_full_workflow_with_v3(paths, scope_block=_v3_scope_block(status="challenged", evidence=[]))

                decision = evaluate_completion_gate(paths)

                self.assertEqual(decision.decision, expected)
                self.assertTrue(any("A1 blocks completion" in item for item in decision.missing))

    def test_v3_skipped_evidence_requires_waiver(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "My task")
            _configure_verification_commands(paths, enable_v3_gates=True, v3_gate_mode="enforce")
            _register_full_workflow_with_v3(paths, verification_block=_v3_evidence_block(result="skipped"))

            decision = evaluate_completion_gate(paths)

            self.assertEqual(decision.decision, "deny")
            self.assertTrue(any("E1 blocks completion" in item for item in decision.missing))

    def test_v3_deferred_item_requires_owner_and_exit_criteria(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "My task")
            _configure_verification_commands(paths, enable_v3_gates=True, v3_gate_mode="enforce")
            _register_full_workflow_with_v3(paths, review_block=_v3_deferred_block(owner="", exit_criteria=""))

            decision = evaluate_completion_gate(paths)

            self.assertEqual(decision.decision, "deny")
            self.assertTrue(any("F1 blocks completion" in item for item in decision.missing))

    def test_v3_pass_with_gaps_requires_waiver_or_deferred_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "My task")
            _configure_verification_commands(paths, enable_v3_gates=True, v3_gate_mode="enforce", allow_pass_with_gaps=True)
            _register_full_workflow_with_v3(paths, review_block=_v3_review_verdict_block("PASS-WITH-GAPS", gaps=["A1"]))

            decision = evaluate_completion_gate(paths)

            self.assertEqual(decision.decision, "deny")
            self.assertTrue(any("A1 blocks completion" in item for item in decision.missing))

    def test_v3_level_two_requires_structured_planning_before_implementation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "My task")
            _configure_verification_commands(paths, enable_v3_gates=True, v3_gate_mode="enforce", require_task_classification=True)
            _register_artifact(paths, "clarification-memo", "no questions")
            _register_artifact(paths, "requirements-summary", _v3_classification_block(level=2))
            _register_artifact(paths, "context-map", "context")
            _register_artifact(paths, "scope-freeze", "scope without assumptions")
            _register_artifact(paths, "design", "design")

            decision = evaluate_implementation_gate(paths, [Path("src/app.py")])

            self.assertEqual(decision.decision, "deny")
            self.assertTrue(any("A*" in item or "assumption" in item for item in decision.missing))

    def test_v3_level_one_uses_lightweight_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "My task")
            _configure_verification_commands(paths, enable_v3_gates=True, v3_gate_mode="enforce", require_task_classification=True)
            _register_artifact(paths, "clarification-memo", "no questions")
            _register_artifact(paths, "requirements-summary", _v3_classification_block(level=1))
            _register_artifact(paths, "context-map", "context")
            _register_artifact(paths, "scope-freeze", "scope without assumptions")
            _register_artifact(paths, "design", "design")

            decision = evaluate_implementation_gate(paths, [Path("src/app.py")])

            self.assertEqual(decision.decision, "allow")

    def test_v3_review_evidence_cannot_prove_high_risk_assumption(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "My task")
            _configure_verification_commands(paths, enable_v3_gates=True, v3_gate_mode="enforce")
            artifact = create_evidence(paths, "review-report")
            artifact.write_text(_v3_review_proves_high_risk_block(), encoding="utf-8")

            register_evidence(paths, "review-report", artifact)
            state = load_state(paths, "feature-my-task")

            self.assertTrue(
                any("cannot be proven by review evidence alone" in error["message"] for error in state["v3_parse_errors"])
            )

    def test_v3_valid_waiver_allows_pass_with_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "My task")
            _configure_verification_commands(paths, enable_v3_gates=True, v3_gate_mode="enforce", allow_pass_with_gaps=True)
            _register_full_workflow_with_v3(
                paths,
                scope_block=_v3_scope_block(status="accepted-risk", evidence=[]),
                review_block=_v3_review_verdict_block("PASS-WITH-GAPS", gaps=["A1"]) + "\n" + _v3_waiver_block(),
            )

            decision = evaluate_completion_gate(paths)

            self.assertEqual(decision.decision, "allow")

    def test_v3_deferred_high_risk_assumption_requires_followup_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "My task")
            _configure_verification_commands(paths, enable_v3_gates=True, v3_gate_mode="enforce")
            _register_full_workflow_with_v3(paths, scope_block=_v3_scope_block(status="deferred", evidence=[]))

            decision = evaluate_completion_gate(paths)

            self.assertEqual(decision.decision, "deny")
            self.assertTrue(any("A1 blocks completion" in item and "deferred" in item for item in decision.missing))

    def test_explicit_gate_can_evaluate_completed_workflow_by_id(self) -> None:
        from harness_v2.gates import evaluate_completion_gate

        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "Done Work")
            _register_full_workflow(paths)
            start_workflow(paths, "bugfix", "Active Work")

            decision = evaluate_completion_gate(paths, "feature-done-work")

            self.assertEqual(decision.decision, "allow")
            self.assertEqual(decision.workflow_id, "feature-done-work")


def _single_workflow_id(paths: HarnessPaths) -> str:
    """Return the single active workflow id (helper for tests that create exactly one)."""
    from harness_v2.state import select_workflow
    return select_workflow(paths).workflow_id


def _register_artifact(paths: HarnessPaths, artifact_type: str, content: str) -> None:
    if artifact_type == "clarification-memo":
        _record_clarification_interaction(paths)
    if artifact_type == "scope-freeze":
        _record_verification_interaction(paths)
    if artifact_type in {"scope-freeze", "verification-report"}:
        existing = load_config(paths.repo)
        _configure_verification_commands(
            paths,
            commands=existing.verification_commands or ("pytest -q",),
            fail_fast=existing.fail_fast,
            pause_after_phases=existing.pause_after_phases,
            enable_v3_gates=existing.enable_v3_gates,
            v3_gate_mode=existing.v3_gate_mode,
            require_assumption_resolution=existing.require_assumption_resolution,
            require_skip_waivers=existing.require_skip_waivers,
            require_task_classification=existing.require_task_classification,
            allow_pass_with_gaps=existing.allow_pass_with_gaps,
        )
    if artifact_type == "verification-report" and "pytest -q" not in content:
        content = f"Ran: pytest -q\n{content}"
    artifact_path = create_evidence(paths, artifact_type)
    artifact_path.write_text(content, encoding="utf-8")
    register_evidence(paths, artifact_type, artifact_path)


def _register_full_workflow(paths: HarnessPaths) -> None:
    _register_artifact(paths, "clarification-memo", "no questions")
    _register_artifact(paths, "requirements-summary", "requirements")
    _register_artifact(paths, "context-map", "context")
    _register_artifact(paths, "scope-freeze", "scope")
    _register_artifact(paths, "design", "design")
    _register_artifact(paths, "verification-report", "tests passed")
    _register_artifact(paths, "review-report", "looks good")


def _configure_verification_commands(
    paths: HarnessPaths,
    commands: tuple[str, ...] = ("pytest -q",),
    *,
    fail_fast: bool = False,
    pause_after_phases: tuple[str, ...] = (),
    enable_v3_gates: bool = False,
    v3_gate_mode: str = "warn",
    require_assumption_resolution: bool = True,
    require_skip_waivers: bool = True,
    require_task_classification: bool = False,
    allow_pass_with_gaps: bool = False,
) -> None:
    config_path = paths.repo / ".github" / "harness-v2" / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "version: 1",
        "gates:",
        "  strict_workflow: true",
        "  require_design_for_non_trivial: true",
        "  require_clarification: true",
        "  protect_state_files: true",
        "  require_verification_commands: true",
        "automation:",
        "  auto_register_artifacts: true",
        "debug:",
        f"  fail_fast: {'true' if fail_fast else 'false'}",
        "verification:",
        "  commands:",
    ]
    for command in commands:
        lines.append(f"    - {command}")
    lines.append("workflow:")
    if pause_after_phases:
        lines.append("  pause_after_phases:")
        for phase in pause_after_phases:
            lines.append(f"    - {phase}")
    else:
        lines.append("  pause_after_phases: []")
    lines.extend(
        [
            "v3:",
            f"  enable_v3_gates: {'true' if enable_v3_gates else 'false'}",
            f"  v3_gate_mode: {v3_gate_mode}",
            f"  require_assumption_resolution: {'true' if require_assumption_resolution else 'false'}",
            f"  require_skip_waivers: {'true' if require_skip_waivers else 'false'}",
            f"  require_task_classification: {'true' if require_task_classification else 'false'}",
            f"  allow_pass_with_gaps: {'true' if allow_pass_with_gaps else 'false'}",
        ]
    )
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _register_full_workflow_with_v3(
    paths: HarnessPaths,
    *,
    scope_block: str | None = None,
    verification_block: str | None = None,
    review_block: str | None = None,
) -> None:
    _register_artifact(paths, "clarification-memo", "no questions")
    _register_artifact(paths, "requirements-summary", _v3_classification_block(level=2))
    _register_artifact(paths, "context-map", "context")
    _register_artifact(paths, "scope-freeze", scope_block or _v3_scope_block(status="proven", evidence=["E1"]))
    _register_artifact(paths, "design", "design")
    _register_artifact(paths, "verification-report", verification_block or _v3_evidence_block(result="pass"))
    _register_artifact(paths, "review-report", review_block or _v3_review_verdict_block("PASS"))


def _v3_block(body: str) -> str:
    return "<!-- harness:v3:start -->\n" + body.strip() + "\n<!-- harness:v3:end -->\n"


def _v3_classification_block(level: int) -> str:
    return _v3_block(
        f"""
task_classification:
  level: {level}
  labels: [api, state]
  rationale: Test classification.
"""
    )


def _v3_scope_block(status: str = "assumed", evidence: list[str] | None = None) -> str:
    evidence_text = "[" + ", ".join(evidence or []) + "]"
    return _v3_block(
        f"""
assumptions:
  - id: A1
    statement: Existing state ownership is correct.
    risk: high
    status: {status}
    falsification: Run targeted state ownership test.
    evidence_required: [E1]
    evidence: {evidence_text}
    owner: tester
decisions:
  - id: D1
    statement: Store v3 state in workflow state.
    linked_assumptions: [A1]
    alternatives: [sidecar-file]
    rationale: Keeps status and gates consistent.
"""
    )


def _v3_malformed_assumption_block() -> str:
    return _v3_block(
        """
assumptions:
  - id: A1
    statement: Missing risk should be invalid.
    status: assumed
"""
    )


def _v3_evidence_block(result: str = "pass") -> str:
    return "Ran: pytest -q\n" + _v3_block(
        f"""
evidence:
  - id: E1
    type: test
    supports: [A1, D1]
    result: {result}
    source: pytest -q
"""
    )


def _v3_deferred_block(owner: str, exit_criteria: str) -> str:
    return _v3_block(
        f"""
deferred_items:
  - id: F1
    reason: Benchmark requires production data.
    owner: {owner}
    exit_criteria: {exit_criteria}
"""
    )


def _v3_waiver_block() -> str:
    return _v3_block(
        """
waivers:
  - id: W1
    covers: [A1]
    risk: Accepted until benchmark data is available.
    owner: tester
    exit_criteria: Revisit before release.
"""
    )


def _v3_review_verdict_block(verdict: str, gaps: list[str] | None = None) -> str:
    gaps_text = "[" + ", ".join(gaps or []) + "]"
    return _v3_block(
        f"""
review:
  verdict: {verdict}
  gaps: {gaps_text}
"""
    )


def _v3_review_proves_high_risk_block() -> str:
    return _v3_block(
        """
assumptions:
  - id: A1
    statement: Review alone proves this.
    risk: high
    status: proven
    falsification: Need non-review evidence.
    evidence_required: [E1]
    evidence: [E1]
    owner: tester
evidence:
  - id: E1
    type: review
    supports: [A1]
    result: pass
    source: review-report
"""
    )


def _capture_hook_output(repo: Path, event_name: str, payload: dict) -> dict[str, object]:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        result = handle_hook_event(repo, event_name, payload)
    raw_output = buffer.getvalue().strip()
    parsed = json.loads(raw_output) if raw_output else None
    return {"result": result, "payload": parsed}


def _workflow_events(paths: HarnessPaths, workflow_id: str) -> list[dict]:
    event_path = paths.workflow_dir(workflow_id) / "events.jsonl"
    return [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _record_clarification_interaction(paths: HarnessPaths) -> None:
    handle_hook_event(
        paths.repo,
        "postToolUse",
        {
            "toolName": "vscode_askQuestions",
            "toolArgs": {
                "questions": [{"header": "Need clarification", "question": "Should I proceed with the documented scope?"}]
            },
        },
    )


def _record_verification_interaction(paths: HarnessPaths) -> None:
    handle_hook_event(
        paths.repo,
        "postToolUse",
        {
            "toolName": "vscode_askQuestions",
            "toolArgs": {
                "questions": [{"header": "Verification plan", "question": "Which tests and build commands should I run for verification?"}]
            },
        },
    )


def _record_user_waiver(paths: HarnessPaths) -> None:
    handle_hook_event(
        paths.repo,
        "userPromptSubmitted",
        {"prompt": "No ambiguity here, clarification is waived and you can continue directly."},
    )


def _record_user_verification_plan(paths: HarnessPaths) -> None:
    handle_hook_event(
        paths.repo,
        "userPromptSubmitted",
        {"prompt": "Test with `pytest -q` and `python -m build` before you finish."},
    )


class BashRedirectGateTests(unittest.TestCase):
    """Tests for Fix 1: bash redirects to artifact paths are now gated."""

    def test_bash_cat_heredoc_to_artifact_is_gated(self) -> None:
        """cat > artifacts/design.md << EOF should be gated like create tool."""
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "test-redirect")
            _configure_verification_commands(paths, fail_fast=True)

            wf_id = "feature-test-redirect"
            artifact_dir = f".github/harness-v2/state/workflows/{wf_id}/artifacts"
            command = f'cat > {artifact_dir}/design.md << \'EOF\'\n# Design\nEOF'

            payload = {"toolName": "bash", "toolArgs": {"command": command}}
            output = _capture_hook_output(paths.repo, "preToolUse", payload)
            # Should deny because upstream artifacts aren't registered yet
            self.assertIsNotNone(output["payload"])
            self.assertEqual(output["payload"]["permissionDecision"], "deny")

    def test_bash_redirect_with_variable_expansion(self) -> None:
        """ARTIFACTS_DIR=...; cat > $ARTIFACTS_DIR/design.md should be detected."""
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "test-var")
            _configure_verification_commands(paths, fail_fast=True)

            wf_id = "feature-test-var"
            artifact_dir = f".github/harness-v2/state/workflows/{wf_id}/artifacts"
            command = f'ARTIFACTS_DIR="{artifact_dir}" && cat > "$ARTIFACTS_DIR/design.md" << \'EOF\'\n# Design\nEOF'

            payload = {"toolName": "bash", "toolArgs": {"command": command}}
            output = _capture_hook_output(paths.repo, "preToolUse", payload)
            self.assertIsNotNone(output["payload"])
            self.assertEqual(output["payload"]["permissionDecision"], "deny")

    def test_bash_redirect_to_non_artifact_is_not_gated(self) -> None:
        """Redirecting to /tmp/foo.txt should not trigger artifact gate."""
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "test-safe")
            _configure_verification_commands(paths, fail_fast=True)

            command = 'echo "hello" > /tmp/test-output.txt'
            payload = {"toolName": "bash", "toolArgs": {"command": command}}
            output = _capture_hook_output(paths.repo, "preToolUse", payload)
            # Should not emit any deny decision (payload is None for allow)
            if output["payload"] is not None:
                self.assertNotEqual(output["payload"].get("permissionDecision"), "deny")


class VerificationNormalizedMatchTests(unittest.TestCase):
    """Tests for Fix 3: verification command matching with normalized whitespace."""

    def test_command_with_line_break_in_report_still_matches(self) -> None:
        """Commands split across lines in the report should match."""
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "test-norm")
            long_command = 'cd Lepus && ./build.sh --build-target x64 2>&1 | tee /tmp/build.log'
            # Register prerequisites using default commands first
            _register_artifact(paths, "clarification-memo", "no questions")
            _register_artifact(paths, "requirements-summary", "requirements")
            _register_artifact(paths, "context-map", "context")
            _register_artifact(paths, "scope-freeze", "scope")
            _register_artifact(paths, "design", "design")

            # Now reconfigure with the long command for the actual test
            _configure_verification_commands(paths, commands=(long_command,))

            # Report contains command broken across lines
            report_content = (
                "# Verification Report\n\n"
                "## Commands Executed\n\n"
                f"```\ncd Lepus &&\n./build.sh --build-target x64\n2>&1 | tee /tmp/build.log\n```\n"
                "Result: PASS\n"
            )
            artifact_path = create_evidence(paths, "verification-report")
            artifact_path.write_text(report_content, encoding="utf-8")
            # Should NOT raise (normalized matching)
            register_evidence(paths, "verification-report", artifact_path)
            state = load_state(paths, "feature-test-norm")
            self.assertEqual(state["current_phase"], "review")

    def test_literal_match_still_works(self) -> None:
        """Simple literal inclusion still works."""
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "test-literal")
            _configure_verification_commands(paths, commands=("pytest -q",))
            _register_artifact(paths, "clarification-memo", "no questions")
            _register_artifact(paths, "requirements-summary", "requirements")
            _register_artifact(paths, "context-map", "context")
            _register_artifact(paths, "scope-freeze", "scope")
            _register_artifact(paths, "design", "design")

            report_content = "# Verification Report\nRan: pytest -q\nAll passed."
            artifact_path = create_evidence(paths, "verification-report")
            artifact_path.write_text(report_content, encoding="utf-8")
            register_evidence(paths, "verification-report", artifact_path)
            state = load_state(paths, "feature-test-literal")
            self.assertEqual(state["current_phase"], "review")


class ReviewBlockerGateTests(unittest.TestCase):
    """Tests for Fix 2: review-report with unresolved blockers is rejected."""

    def test_review_with_unresolved_blocker_is_rejected(self) -> None:
        """review-report containing an open blocker should be rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "test-blocker")
            _configure_verification_commands(paths)
            _register_artifact(paths, "clarification-memo", "no questions")
            _register_artifact(paths, "requirements-summary", "requirements")
            _register_artifact(paths, "context-map", "context")
            _register_artifact(paths, "scope-freeze", "scope")
            _register_artifact(paths, "design", "design")
            _register_artifact(paths, "verification-report", "tests passed")

            report_with_blocker = (
                "# Review Report\n\n"
                "## Findings\n\n"
                "- **Blocker**: cache not cleared in Reset() — unresolved\n"
                "- Minor: variable naming inconsistency\n"
            )
            artifact_path = create_evidence(paths, "review-report")
            artifact_path.write_text(report_with_blocker, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unresolved blocker/critical"):
                register_evidence(paths, "review-report", artifact_path)

    def test_review_with_resolved_blocker_is_accepted(self) -> None:
        """review-report where all blockers are marked resolved should pass."""
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "test-resolved")
            _configure_verification_commands(paths)
            _register_artifact(paths, "clarification-memo", "no questions")
            _register_artifact(paths, "requirements-summary", "requirements")
            _register_artifact(paths, "context-map", "context")
            _register_artifact(paths, "scope-freeze", "scope")
            _register_artifact(paths, "design", "design")
            _register_artifact(paths, "verification-report", "tests passed")

            report_resolved = (
                "# Review Report\n\n"
                "All blockers resolved.\n\n"
                "## Findings\n\n"
                "- **Blocker**: cache not cleared in Reset() — fixed in commit abc123\n"
            )
            artifact_path = create_evidence(paths, "review-report")
            artifact_path.write_text(report_resolved, encoding="utf-8")
            # Should NOT raise
            register_evidence(paths, "review-report", artifact_path)
            state = load_state(paths, "feature-test-resolved")
            self.assertEqual(state["current_phase"], "done")

    def test_review_without_blockers_is_accepted(self) -> None:
        """review-report with no blocker markers passes normally."""
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "test-clean")
            _configure_verification_commands(paths)
            _register_artifact(paths, "clarification-memo", "no questions")
            _register_artifact(paths, "requirements-summary", "requirements")
            _register_artifact(paths, "context-map", "context")
            _register_artifact(paths, "scope-freeze", "scope")
            _register_artifact(paths, "design", "design")
            _register_artifact(paths, "verification-report", "tests passed")

            report_clean = "# Review Report\n\nAll good. No issues found.\n"
            artifact_path = create_evidence(paths, "review-report")
            artifact_path.write_text(report_clean, encoding="utf-8")
            register_evidence(paths, "review-report", artifact_path)
            state = load_state(paths, "feature-test-clean")
            self.assertEqual(state["current_phase"], "done")

    def test_review_with_critical_severity_open_is_rejected(self) -> None:
        """Severity: critical + Status: open pattern is caught."""
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "test-critical")
            _configure_verification_commands(paths)
            _register_artifact(paths, "clarification-memo", "no questions")
            _register_artifact(paths, "requirements-summary", "requirements")
            _register_artifact(paths, "context-map", "context")
            _register_artifact(paths, "scope-freeze", "scope")
            _register_artifact(paths, "design", "design")
            _register_artifact(paths, "verification-report", "tests passed")

            report = (
                "# Review Report\n\n"
                "| Issue | Severity: critical | Status: open |\n"
                "| Race condition in handler | | |\n"
            )
            artifact_path = create_evidence(paths, "review-report")
            artifact_path.write_text(report, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unresolved blocker/critical"):
                register_evidence(paths, "review-report", artifact_path)


class PauseAfterPhasesTests(unittest.TestCase):
    """Tests for pause_after_phases config: allow agentStop at configured checkpoints."""

    def _setup_workflow_at_design(self, paths: HarnessPaths, pause_after_phases: tuple[str, ...] = ()) -> None:
        start_workflow(paths, "feature", "pause-test")
        _configure_verification_commands(paths, pause_after_phases=pause_after_phases)
        _register_artifact(paths, "clarification-memo", "no questions")
        _register_artifact(paths, "requirements-summary", "requirements")
        _register_artifact(paths, "context-map", "context")
        _register_artifact(paths, "scope-freeze", "scope")
        _register_artifact(paths, "design", "design plan")

    def test_pause_after_design_allows_stop_at_implementation_phase(self) -> None:
        """With pause_after_phases: [design], agentStop after design is allowed."""
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            self._setup_workflow_at_design(paths, pause_after_phases=("design",))

            result = evaluate_completion_gate(paths)
            self.assertEqual(result.decision, "allow")
            self.assertIn("design", result.reason)
            self.assertIn("pause", result.reason)

    def test_no_pause_config_blocks_stop_at_implementation_phase(self) -> None:
        """Without pause_after_phases, agentStop after design is still blocked."""
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            self._setup_workflow_at_design(paths)

            result = evaluate_completion_gate(paths)
            self.assertEqual(result.decision, "deny")
            self.assertIn("verification-report", result.missing)

    def test_pause_only_triggers_when_artifact_is_current(self) -> None:
        """Pause does not trigger if the pause phase artifact hasn't been registered yet."""
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "pause-test2")
            _configure_verification_commands(paths, pause_after_phases=("design",))
            _register_artifact(paths, "clarification-memo", "no questions")
            _register_artifact(paths, "requirements-summary", "requirements")
            _register_artifact(paths, "context-map", "context")
            _register_artifact(paths, "scope-freeze", "scope")
            # design NOT registered yet

            result = evaluate_completion_gate(paths)
            self.assertEqual(result.decision, "deny")

    def test_pause_after_phases_empty_by_default(self) -> None:
        """Default config has pause_after_phases empty."""
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            install(paths.repo)
            config_text = (paths.repo / ".github" / "harness-v2" / "config.yaml").read_text()
            self.assertIn("pause_after_phases: []", config_text)


class StaleRegistryCompletionGateTests(unittest.TestCase):
    """Regression tests for the stale-registry bug: all workflows done but
    registry entries still carry status='active'."""

    def _start_and_complete(self, paths: HarnessPaths, slug: str) -> None:
        start_workflow(paths, "bugfix", slug)
        _register_full_workflow(paths)

    def test_completion_gate_allows_stop_when_all_registry_entries_are_done(self) -> None:
        """Three completed workflows with stale registry entries must not block stop."""
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            _configure_verification_commands(paths)
            # Start and complete three separate workflows.
            for slug in ("task-a", "task-b", "task-c"):
                self._start_and_complete(paths, slug)
            # Manually corrupt the registry back to 'active' for all three
            # (simulates the pre-fix state for existing repos).
            import json as _json
            reg_path = paths.active_workflows
            reg = _json.loads(reg_path.read_text())
            for entry in reg["active"]:
                entry["status"] = "active"
            reg_path.write_text(_json.dumps(reg), encoding="utf-8")

            result = evaluate_completion_gate(paths)
            self.assertEqual(result.decision, "allow")
            self.assertIn("no active workflow", result.reason)

    def test_completion_gate_denies_when_one_of_many_is_incomplete(self) -> None:
        """Two done workflows + one in-progress must still block stop."""
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            _configure_verification_commands(paths)
            self._start_and_complete(paths, "done-a")
            self._start_and_complete(paths, "done-b")
            # Third workflow: started but not completed.
            start_workflow(paths, "bugfix", "in-progress")

            result = evaluate_completion_gate(paths)
            self.assertEqual(result.decision, "deny")


if __name__ == "__main__":
    unittest.main()
