from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.contracts import ContractValidationError, load_schema, validate_instance  # noqa: E402
from src.project_intake import (  # noqa: E402
    BRIEF_STATES,
    CODEX_REQUIREMENTS_INTERVIEW,
    NEEDS_MORE_USER_INPUT,
    USER_CONFIRMED_BRIEF,
    BriefState,
    ProjectIntakeError,
    ProjectRequirementsIntake,
    assert_gpt_calls_zero,
    check_intake_file_hygiene,
    natural_language_entry,
    project_identity,
    recognize_intake_request,
    validate_project_brief,
)
from scripts.stage_cli import main as stage_cli_main  # noqa: E402


class RequirementsIntakeScenarios(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="requirements-intake-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_scenario_a_natural_language_rough_requirement_enters_draft_with_one_question(self):
        result = natural_language_entry(self.root, "想做成这个工作流")
        self.assertEqual(result["state"], BriefState.DRAFT.value)
        self.assertEqual(result["status"], NEEDS_MORE_USER_INPUT)
        self.assertEqual(result["question_count"], 1)
        self.assertEqual(len(result["questions"]), 1)
        self.assertEqual(result["project_brief"]["intake_mode"], CODEX_REQUIREMENTS_INTERVIEW)

    def test_scenario_b_both_supported_modes_use_one_canonical_brief(self):
        interview = ProjectRequirementsIntake(self.root)
        draft = interview.initialize(mode=CODEX_REQUIREMENTS_INTERVIEW, rough_requirement="")
        project_id = draft["project_id"]
        self.assertEqual(interview.answer("Make the user's next action observable")["state"], BriefState.DRAFT.value)
        ready = interview.answer("A reviewer can verify one documented acceptance result")
        self.assertEqual(ready["state"], BriefState.WAITING_USER_APPROVAL.value)
        confirmed = interview.initialize(mode=USER_CONFIRMED_BRIEF, brief={"goal": "a revised goal", "success_criteria": ["a check passes"]})
        self.assertEqual(confirmed["project_id"], project_id)
        self.assertEqual(confirmed["state"], BriefState.WAITING_USER_APPROVAL.value)

    def test_scenario_c_interview_exposes_at_most_one_core_question(self):
        service = ProjectRequirementsIntake(self.root)
        result = service.initialize(rough_requirement="")
        self.assertEqual(len(result["questions"]), 1)
        self.assertEqual(result["question"]["kind"], "core")
        result = service.answer("A user-visible project outcome")
        self.assertLessEqual(len(result["questions"]), 1)
        result = service.answer("A measurable acceptance condition")
        self.assertEqual(result["question_count"], 0)

    def test_scenario_d_repo_facts_are_read_only_and_not_asked_as_questions(self):
        (self.root / "README.md").write_text("local facts\n", encoding="utf-8")
        result = natural_language_entry(self.root, "想做成这个工作流")
        facts = result["project_brief"]["repository_facts"]
        self.assertTrue(facts["checked_read_only"])
        self.assertIn("README.md", facts["top_level_entries"])
        self.assertNotIn("file", result["question"]["text"].casefold())
        self.assertNotIn("工具事实", result["question"]["text"])

    def test_scenario_e_revision_preserves_project_identity_and_demotes_approval(self):
        service = ProjectRequirementsIntake(self.root)
        first = service.initialize(mode=USER_CONFIRMED_BRIEF, brief={"goal": "initial", "success_criteria": ["works"]})
        approved = service.approve(rationale="human confirmed")
        self.assertEqual(approved["state"], BriefState.APPROVED.value)
        revised = service.update({"goal": "updated by the user"})
        self.assertEqual(revised["project_id"], first["project_id"])
        self.assertEqual(revised["state"], BriefState.WAITING_USER_APPROVAL.value)
        self.assertEqual(project_identity(self.root)["project_id"], first["project_id"])

    def test_scenario_f_approve_is_explicit_and_has_no_downstream_side_effect(self):
        service = ProjectRequirementsIntake(self.root)
        service.initialize(mode=USER_CONFIRMED_BRIEF, brief={"goal": "goal", "success_criteria": ["criterion"]})
        result = service.approve(actor="test-user", rationale="reviewed")
        self.assertEqual(result["state"], BriefState.APPROVED.value)
        self.assertEqual(result["actions_triggered"], [])
        self.assertFalse(result["side_effects"]["project_discovery_started"])
        self.assertFalse(result["side_effects"]["stage_started"])
        self.assertEqual(result["gpt_calls"], 0)
        self.assertFalse((self.root / ".research" / "stage-state.json").exists())

    def test_scenario_g_cancel_is_terminal_and_fail_closed(self):
        service = ProjectRequirementsIntake(self.root)
        service.initialize(rough_requirement="想做成这个工作流")
        cancelled = service.cancel(reason="user stopped intake")
        self.assertEqual(cancelled["state"], BriefState.CANCELLED.value)
        self.assertTrue(cancelled["fail_closed"])
        with self.assertRaises(ProjectIntakeError) as answer_error:
            service.answer("cannot resume")
        self.assertEqual(answer_error.exception.code, "ANSWER_NOT_ALLOWED")
        with self.assertRaises(ProjectIntakeError) as update_error:
            service.update({"goal": "cannot resume"})
        self.assertEqual(update_error.exception.code, "BRIEF_CANCELLED")
        with self.assertRaises(ProjectIntakeError):
            service.initialize(rough_requirement="new identity is not allowed")

    def test_scenario_h_file_hygiene_has_one_canonical_file_and_no_transcript(self):
        service = ProjectRequirementsIntake(self.root)
        service.initialize(rough_requirement="想做成这个工作流")
        files = [path.relative_to(self.root / ".research").as_posix() for path in (self.root / ".research").rglob("*") if path.is_file()]
        self.assertEqual(files, ["PROJECT_BRIEF.json"])
        hygiene = check_intake_file_hygiene(self.root)
        self.assertTrue(hygiene["passed"])

    def test_scenario_i_schema_secret_and_gpt_guards(self):
        service = ProjectRequirementsIntake(self.root)
        service.initialize(mode=USER_CONFIRMED_BRIEF, brief={"goal": "goal", "success_criteria": ["criterion"]})
        payload = json.loads((self.root / ".research" / "PROJECT_BRIEF.json").read_text(encoding="utf-8"))
        validate_instance(payload, load_schema("project_brief"))
        self.assertEqual(assert_gpt_calls_zero(payload), 0)
        self.assertEqual(payload["gpt_calls"], 0)
        with self.assertRaises(ProjectIntakeError):
            service.update({"confidence": 0.99})
        with self.assertRaises(ProjectIntakeError):
            service.update({"api_key": "sk-" + "proj-123456789012345678901234"})
        self.assertEqual(assert_gpt_calls_zero(), 0)

    def test_scenario_j_thin_cli_and_natural_language_detection(self):
        self.assertTrue(recognize_intake_request("用户说：想做成这个工作流"))
        self.assertFalse(recognize_intake_request("只查看当前状态"))
        output = StringIO()
        with redirect_stdout(output):
            code = stage_cli_main(["--repo", str(self.root), "requirements-init", "--rough-requirement", "想做成这个工作流"])
        self.assertEqual(code, 0)
        body = json.loads(output.getvalue())
        self.assertEqual(body["state"], BriefState.DRAFT.value)
        self.assertEqual(body["question_count"], 1)


class RequirementsIntakeNegativeChecks(unittest.TestCase):
    def test_brief_states_are_exactly_the_four_canonical_values(self):
        self.assertEqual(set(BRIEF_STATES), {"DRAFT", "WAITING_USER_APPROVAL", "APPROVED", "CANCELLED"})

    def test_validate_project_brief_rejects_nonzero_gpt_counter(self):
        with tempfile.TemporaryDirectory(prefix="requirements-intake-invalid-") as directory:
            root = Path(directory)
            service = ProjectRequirementsIntake(root)
            service.initialize(rough_requirement="想做成这个工作流")
            payload = json.loads((root / ".research" / "PROJECT_BRIEF.json").read_text(encoding="utf-8"))
            payload["gpt_calls"] = 1
            with self.assertRaises(ProjectIntakeError):
                validate_project_brief(payload, root)


if __name__ == "__main__":
    unittest.main()
