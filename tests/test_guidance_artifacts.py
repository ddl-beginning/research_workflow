from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.guidance_artifacts import (
    GuidanceArtifactError,
    accepted_intent_digest,
    digest_json,
    validate_markdown_chain,
    render_closeout,
    render_handoff,
    render_template,
    validate_plan_binding,
    validate_spec_binding,
    validate_tasks_binding,
)


class GuidanceArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.brief = {"state": "APPROVED", "project_id": "project-test", "goal": "bounded guidance"}
        self.spec = {
            "spec_version": 1,
            "accepted_intent_digest": accepted_intent_digest(self.brief),
            "what": "portable guidance",
        }
        self.plan = {"plan_version": 1, "spec_digest": digest_json(self.spec), "allowed_paths": ["templates", "src/guidance_artifacts.py"]}
        self.tasks = {
            "tasks_version": 1,
            "plan_digest": digest_json(self.plan),
            "tasks": [
                {"id": "T001", "paths": ["templates"], "depends_on": [], "requirement_refs": ["FR-001"]},
                {"id": "T002", "paths": ["src/guidance_artifacts.py"], "depends_on": ["T001"], "requirement_refs": ["FR-002"]},
            ],
        }

    def test_templates_render_deterministically_and_strictly(self) -> None:
        values = {"constitution_version": 1, "governance": "Human", "authority_principles": "Controller", "project_constraints": "No dual authority", "change_rules": "Version", "title": "X", "spec_version": 1, "intent_digest": "digest", "why": "why", "what": "what", "acceptance_criteria": "- pass", "non_goals": "- cleanup", "clarified_decisions": "- none", "plan_version": 1, "spec_digest": "digest", "technical_approach": "bounded", "research_rationale": "official", "allowed_paths": "templates", "verification_strategy": "tests", "tasks": "- T001", "plan_digest": "digest"}
        first = render_template("spec.md", values)
        self.assertEqual(first, render_template("spec.md", values))
        self.assertNotIn("{{", first)
        with self.assertRaises(GuidanceArtifactError):
            render_template("spec.md", {"title": "missing"})

    def test_all_five_templates_render_byte_identically(self) -> None:
        values = {"constitution_version": 1, "governance": "Human", "authority_principles": "Controller", "project_constraints": "No dual authority", "change_rules": "Version", "title": "X", "spec_version": 1, "intent_digest": "digest", "why": "why", "what": "what", "acceptance_criteria": "- pass", "non_goals": "- cleanup", "clarified_decisions": "- none", "plan_version": 1, "spec_digest": "digest", "technical_approach": "bounded", "research_rationale": "official", "allowed_paths": "templates", "verification_strategy": "tests", "tasks_version": 1, "tasks": "- T001", "plan_digest": "digest", "stage_id": "stage", "stage_status": "CLOSED", "journal_revision": 1, "result": "closed", "evidence": "- receipt", "remaining_gaps": "- none"}
        for name in ("constitution.md", "spec.md", "plan.md", "tasks.md", "CLOSEOUT.md"):
            self.assertEqual(render_template(name, values), render_template(name, values))

    def test_spec_digest_binding_fails_closed(self) -> None:
        self.assertEqual(validate_spec_binding(self.spec, self.brief)["spec_version"], 1)
        bad = dict(self.spec, accepted_intent_digest="wrong-digest")
        with self.assertRaises(GuidanceArtifactError):
            validate_spec_binding(bad, self.brief)

    def test_plan_and_task_scope_and_references(self) -> None:
        self.assertEqual(validate_plan_binding(self.plan, self.spec, allowed_paths=["templates", "src"])["plan_version"], 1)
        self.assertEqual(validate_tasks_binding(self.tasks, self.plan, allowed_paths=["templates", "src"])["task_ids"], ["T001", "T002"])
        with self.assertRaises(GuidanceArtifactError):
            validate_plan_binding(dict(self.plan, allowed_paths=["outside"]), self.spec, allowed_paths=["templates", "src"])
        with self.assertRaises(GuidanceArtifactError):
            validate_tasks_binding({**self.tasks, "tasks": [{"id": "T001", "paths": ["outside"], "depends_on": []}]}, self.plan, allowed_paths=["templates", "src"])

    def test_duplicate_and_unstable_task_ids_fail(self) -> None:
        duplicate = {**self.tasks, "tasks": [{"id": "T001", "paths": ["templates"], "depends_on": [], "requirement_refs": ["FR-001"]}, {"id": "T001", "paths": ["templates"], "depends_on": [], "requirement_refs": ["FR-001"]}]}
        with self.assertRaises(GuidanceArtifactError):
            validate_tasks_binding(duplicate, self.plan, allowed_paths=["templates", "src"])
        unstable = {**self.tasks, "tasks": [{"id": "task-one", "paths": ["templates"], "depends_on": [], "requirement_refs": ["FR-001"]}]}
        with self.assertRaises(GuidanceArtifactError):
            validate_tasks_binding(unstable, self.plan, allowed_paths=["templates", "src"])

    def test_closeout_requires_canonical_closed(self) -> None:
        journal = {"revision": 1, "stages": {"stage": {"status": "ACTIVE"}}, "stage_runtime": {"stage": {"closeout": {"integration_operation_id": "op", "verification_digest": "v"}}}}
        with self.assertRaises(GuidanceArtifactError):
            render_closeout(journal=journal, stage_id="stage", title="x", evidence=[], remaining_gaps=[])
        journal["stages"]["stage"]["status"] = "CLOSED"
        rendered = render_closeout(journal=journal, stage_id="stage", title="x", evidence=["receipt"], remaining_gaps=["future"], integration_operation_id="op", verification_digest="v")
        self.assertIn("Canonical state: `CLOSED`", rendered)

    def test_handoff_is_projection_only(self) -> None:
        projection = {"stage": {"stage_id": "stage", "status": "ACTIVE", "next_action": "REQUEST_EXECUTION"}}
        rendered = render_handoff(projection=projection, links={"spec": "spec.md"}, recent_receipt="receipt.json")
        self.assertIn("Canonical next action: `REQUEST_EXECUTION`", rendered)
        self.assertNotIn("Requirement", rendered)
        self.assertNotIn("lifecycle state:", rendered.lower())

    def test_old_guidance_remains_readable(self) -> None:
        self.assertTrue(Path("README.md").is_file())
        self.assertTrue(Path("ARCHITECTURE.md").is_file())

    def test_parent_traversal_and_dependency_cycles_fail_closed(self) -> None:
        with self.assertRaises(GuidanceArtifactError):
            validate_plan_binding({**self.plan, "allowed_paths": ["templates/../outside"]}, self.spec, allowed_paths=["templates", "src"])
        cyclic = {**self.tasks, "tasks": [
            {"id": "T001", "paths": ["templates"], "depends_on": ["T002"], "requirement_refs": ["FR-001"]},
            {"id": "T002", "paths": ["src/guidance_artifacts.py"], "depends_on": ["T001"], "requirement_refs": ["FR-002"]},
        ]}
        with self.assertRaises(GuidanceArtifactError):
            validate_tasks_binding(cyclic, self.plan, allowed_paths=["templates", "src"])

    def test_actual_markdown_digest_chain(self) -> None:
        root = Path("specs/spec-kit-adoption-and-repository-architecture-v1")
        # This is a derived human-facing bundle.  Its committed V2 binding is
        # the authority for this validation; the old V1 workflow-state.json
        # checkpoint is intentionally not part of a fresh Product workspace.
        binding = json.loads((root / "bindings.json").read_text(encoding="utf-8"))
        intent = binding["intent"]
        brief = {
            "state": "APPROVED",
            "project_id": intent["project_id"],
            "revision": intent["brief_revision"],
        }
        # Git may materialize the tracked Markdown with CRLF on Windows while
        # its committed digest headers are LF-based.  Validate a byte-stable
        # isolated copy so this derived-artifact test does not depend on the
        # checkout's line-ending policy.
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            for name in ("spec.md", "plan.md", "tasks.md"):
                fixture.joinpath(name).write_bytes(
                    (root / name).read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8")
                )
            result = validate_markdown_chain(
                fixture,
                accepted_brief=brief,
                workflow_state=intent,
                allowed_paths=["templates", "src/guidance_artifacts.py"],
                requirement_ids=["FR-001", "FR-002", "FR-003", "FR-004", "FR-005"],
            )
        self.assertEqual(result["intent_digest"], intent["brief_digest"])
        self.assertEqual(len(result["tasks_digest"]), 64)


if __name__ == "__main__":
    unittest.main()
