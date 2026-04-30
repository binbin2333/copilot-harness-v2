from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness_v2.artifacts import _sha256, create_evidence, register_evidence
from harness_v2.gates import evaluate_implementation_gate
from harness_v2.installer import install
from harness_v2.memory import list_memory, record_memory
from harness_v2.state import HarnessPaths, load_state, start_workflow


class HarnessCoreTests(unittest.TestCase):
    def test_start_workflow_creates_state_and_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            state = start_workflow(paths, "feature", "Auth Flow")

            self.assertEqual(state["workflow_id"], "feature-auth-flow")
            self.assertEqual(state["current_phase"], "intake")
            registry = json.loads(paths.active_workflows.read_text(encoding="utf-8"))
            self.assertEqual(registry["active"][0]["workflow_id"], "feature-auth-flow")
            self.assertTrue((paths.workflow_dir("feature-auth-flow") / "events.jsonl").exists())

    def test_evidence_registration_hashes_and_invalidates_downstream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = HarnessPaths(Path(tmp))
            start_workflow(paths, "feature", "Auth")
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
            scope_path = create_evidence(paths, "scope-freeze")
            scope_path.write_text("# Scope\n", encoding="utf-8")
            register_evidence(paths, "scope-freeze", scope_path)

            verification_path = create_evidence(paths, "verification-report")
            verification_path.write_text("# Verification\n\nPassed.\n", encoding="utf-8")
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
            self.assertTrue((repo / ".github" / "harness-v2" / "hooks" / "pre_tool_use.py").exists())
            self.assertTrue((repo / ".github" / "skills" / "context-map" / "SKILL.md").exists())
            gitignore = (repo / ".gitignore").read_text(encoding="utf-8")
            self.assertIn(".github/harness-v2/state/", gitignore)


if __name__ == "__main__":
    unittest.main()
