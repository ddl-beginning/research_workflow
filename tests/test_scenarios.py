from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.contracts import (  # noqa: E402
    ContractValidationError,
    compare_to_baseline,
    load_schema,
    path_is_allowed,
    validate_against_schema,
    validate_result_report,
    validate_scoped_task,
    validate_stage_contract,
)
from src.supervisor import (  # noqa: E402
    DecisionType,
    Supervisor,
    SupervisorError,
    architecture_check,
    fresh_critic_packet,
    planner_decision,
)


FIXTURES = ROOT / "fixtures"
SCHEMAS = ROOT / "schemas"


def read_json(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def contract(*, critic_mode: str | None = None) -> dict:
    value = read_json("stage_contract.json")
    if critic_mode is not None:
        value["critic_mode"] = critic_mode
    return value


def result_for(
    task: dict,
    *,
    status: str = "SUCCEEDED",
    artifact_score: float = 0.7,
    runtime_ms: int = 82,
    scientific_result: str = "SUPPORTED",
    stage_ready: bool = False,
    user_visible_failure: bool = False,
    human_gate_required: bool = False,
    changed_files: list[str] | None = None,
    user_visible_improvement: bool | None = None,
    major_uncertainty: bool | None = None,
    representation_replacement: bool | None = None,
    representative_set: list[str] | None = None,
    comparison_mode: str | None = None,
) -> dict:
    result = {
        "schema_version": "codex_result.v1",
        "plan_id": task["plan_id"],
        "stage_id": task["stage_id"],
        "task_id": task["task_id"],
        "iteration_index": task["iteration_index"],
        "status": status,
        "summary": f"fake result for iteration {task['iteration_index']}",
        "changed_files": ["src/demo/sampling.txt"] if changed_files is None else changed_files,
        "tests": [{"name": "scoped smoke", "status": "PASS" if status == "SUCCEEDED" else "FAIL"}],
        "measurements": {"artifact_score": artifact_score, "runtime_ms": runtime_ms},
        "evidence_refs": [f"artifact://result/{task['iteration_index']}"],
        "review_artifacts": (
            [{"uri": f"artifact://stage/{task['stage_id']}/review-{task['iteration_index']}.png"}]
            if stage_ready
            else []
        ),
        "abstraction_layer": task["abstraction_layer"],
        "scientific_result": scientific_result,
        "stage_ready": stage_ready,
        "user_visible_failure": user_visible_failure,
        "human_gate_required": human_gate_required,
    }
    if user_visible_improvement is not None:
        result["user_visible_improvement"] = user_visible_improvement
    if major_uncertainty is not None:
        result["major_uncertainty"] = major_uncertainty
    if representation_replacement is not None:
        result["representation_replacement"] = representation_replacement
    if representative_set is not None:
        result["representative_set"] = representative_set
    if comparison_mode is not None:
        result["comparison_mode"] = comparison_mode
    return result


class StageOrientedScenarios(unittest.TestCase):
    def test_scenario_a_improvement_continue_ready_review_artifacts_and_accept_milestone(self):
        """A: two improvements end in a gated ready review and acceptance milestone."""
        supervisor = Supervisor()
        started = supervisor.start_stage(
            contract(), {"artifact_score": 1.0, "runtime_ms": 80}, ["artifact://baseline/1"]
        )
        self.assertEqual(started["architecture_check"]["trigger"], "stage_start")
        self.assertTrue(started["architecture_check"]["triggered"])
        self.assertEqual(started["architecture_check"]["route"], "KEEP_COURSE")
        baseline_digest = started["baseline"]["digest"]
        task1 = supervisor.create_scoped_task()
        first = supervisor.submit_result(task1, result_for(task1))
        self.assertEqual(first["decision"]["decision"], DecisionType.CONTINUE.value)
        self.assertTrue(first["decision"]["silent"])
        self.assertIsNone(first["notification"])
        self.assertEqual(supervisor.status, "ACTIVE")

        task2 = supervisor.create_scoped_task()
        self.assertEqual(task2["architecture_check"]["trigger"], "second_same_abstraction_repair")
        self.assertEqual(task2["architecture_check"]["route"], "KEEP_COURSE")
        second = supervisor.submit_result(
            task2,
            result_for(task2, artifact_score=0.5, stage_ready=True),
        )
        self.assertEqual(second["decision"]["decision"], DecisionType.STAGE_READY.value)
        self.assertEqual(supervisor.status, "STAGE_READY")
        self.assertEqual(second["notification"]["type"], "STAGE_READY")
        self.assertTrue(second["notification"]["review_artifacts"])
        accepted = supervisor.accept_stage(reviewer="test-user", rationale="measurements pass")
        self.assertEqual(accepted["status"], "ACCEPTED")
        self.assertIsNotNone(accepted["accepted_milestone"])
        self.assertEqual(accepted["accepted_milestone"]["stage_id"], contract()["stage_id"])
        self.assertEqual(supervisor.state["accepted_milestones"][0]["accepted"], True)
        self.assertEqual(supervisor.state["baseline"]["digest"], baseline_digest)
        self.assertTrue(supervisor.state["baseline"]["frozen"])
        self.assertEqual(second["comparison"]["metric_deltas"]["artifact_score"], -0.5)

    def test_scenario_b_two_same_abstraction_no_improvement_architecture_replan_keeps_goal(self):
        """B: the bounded architecture check asks for REPLAN at the same stage."""
        supervisor = Supervisor()
        started = supervisor.start_stage(contract(), {"artifact_score": 1.0, "runtime_ms": 80})
        original_goal = started["stage_id"], supervisor.state["contract"]["user_visible_goal"]
        events = []
        for _ in range(2):
            task = supervisor.create_scoped_task()
            events.append(
                supervisor.submit_result(
                    task,
                    result_for(
                        task,
                        artifact_score=1.2,
                        user_visible_failure=True,
                        user_visible_improvement=False,
                    ),
                )
            )
        self.assertEqual(events[0]["decision"]["decision"], DecisionType.CONTINUE.value)
        self.assertEqual(events[1]["decision"]["decision"], DecisionType.REPLAN.value)
        self.assertTrue(events[1]["architecture_check"]["triggered"])
        self.assertEqual(events[1]["architecture_check"]["trigger"], "two_same_abstraction_no_improvement")
        self.assertEqual(events[1]["architecture_check"]["route"], "REPLAN")
        self.assertTrue(events[1]["decision"]["silent"])
        self.assertIsNone(events[1]["notification"])
        self.assertEqual(events[1]["status"], "ACTIVE")
        self.assertEqual((supervisor.state["contract"]["stage_id"], supervisor.state["contract"]["user_visible_goal"]), original_goal)
        self.assertEqual(supervisor.state["iteration_index"], 2)

    def test_scenario_c_major_representation_replacement_bounded_challenger_and_winner(self):
        """C: major replacement compares one primary/challenger set, then keeps one lane."""
        supervisor = Supervisor(architecture_threshold=3)
        supervisor.start_stage(contract(), {"artifact_score": 1.0, "runtime_ms": 80})
        comparison_started = supervisor.start_representation_comparison(["rep-a", "rep-b"])
        self.assertTrue(comparison_started["architecture_check"]["triggered"])
        self.assertEqual(comparison_started["architecture_check"]["trigger"], "core_representation_replacement")
        self.assertEqual(comparison_started["architecture_check"]["route"], "REPLAN")
        self.assertEqual(comparison_started["architecture_check"]["threshold"], 3)
        task = supervisor.create_scoped_task()
        event = supervisor.submit_result(
            task,
            result_for(
                task,
                artifact_score=0.8,
                representation_replacement=True,
                representative_set=["rep-a", "rep-b"],
            ),
        )
        self.assertEqual(event["comparison_mode"], "MAJOR_CHALLENGER")
        packet = event["critic_packet"]
        self.assertEqual(packet["comparison_mode"], "MAJOR_CHALLENGER")
        self.assertEqual([item["role"] for item in packet["critic_tasks"]], ["primary", "challenger"])
        self.assertLessEqual(len(packet["critic_tasks"]), 2)
        self.assertEqual(packet["critic_tasks"][0]["representative_set"], ["rep-a", "rep-b"])
        self.assertEqual(packet["critic_tasks"][1]["representative_set"], ["rep-a", "rep-b"])
        selected = supervisor.select_winner("challenger", actor="test-user", rationale="challenger wins")
        self.assertEqual(selected["active_lanes"], ["challenger"])
        self.assertEqual(selected["architecture_check"]["threshold"], 3)
        self.assertEqual(supervisor.state["active_lanes"], ["challenger"])
        self.assertEqual(supervisor.state["winner"], "challenger")
        self.assertEqual(supervisor.state["representative_set"], ["rep-a", "rep-b"])

    def test_scenario_d_major_uncertainty_opens_human_gate_and_pauses(self):
        """D: a major uncertainty pauses execution until a human chooses a route."""
        supervisor = Supervisor()
        supervisor.start_stage(contract(), {"artifact_score": 1.0, "runtime_ms": 80})
        task = supervisor.create_scoped_task()
        gated = supervisor.submit_result(task, result_for(task, major_uncertainty=True))
        self.assertEqual(gated["decision"]["decision"], DecisionType.HUMAN_GATE.value)
        self.assertEqual(gated["notification"]["type"], "HUMAN_GATE")
        self.assertEqual(supervisor.status, "HUMAN_GATE")
        with self.assertRaises(SupervisorError):
            supervisor.create_scoped_task()
        resolved = supervisor.resolve_human_gate(approved=True, actor="test-user", rationale="route selected")
        self.assertEqual(resolved["status"], "ACTIVE")
        self.assertIsNone(supervisor.state["pending_gate"])

    def test_scenario_e_reject_ready_keeps_same_stage_active_without_milestone(self):
        """E: rejecting a ready stage does not accept it or start another stage."""
        supervisor = Supervisor()
        start = supervisor.start_stage(contract(), {"artifact_score": 1.0, "runtime_ms": 80})
        task1 = supervisor.create_scoped_task()
        supervisor.submit_result(task1, result_for(task1, artifact_score=0.7))
        task2 = supervisor.create_scoped_task()
        ready = supervisor.submit_result(task2, result_for(task2, artifact_score=0.5, stage_ready=True))
        self.assertEqual(ready["status"], "STAGE_READY")
        rejected = supervisor.reject_stage(reviewer="test-user", rationale="evidence needs another comparison")
        self.assertEqual(rejected["status"], "ACTIVE")
        self.assertTrue(rejected["architecture_check"]["triggered"])
        self.assertEqual(rejected["architecture_check"]["trigger"], "stage_closeout")
        self.assertEqual(rejected["architecture_check"]["route"], "KEEP_COURSE")
        self.assertEqual(supervisor.state["contract"]["stage_id"], start["stage_id"])
        self.assertIsNone(rejected["accepted_milestone"])
        self.assertEqual(supervisor.state["accepted_milestones"], [])
        self.assertIsNone(supervisor.state["accepted_milestone"])
        with self.assertRaises(SupervisorError):
            # No implicit next-stage creation/transition exists.
            supervisor.start_stage(contract(), {"artifact_score": 1.0, "runtime_ms": 80})

    def test_safety_scope_contract_and_blocked_state_fail_closed(self):
        """Safety negative checks remain covered independently of A-E semantics."""
        invalid = contract()
        for bad_scope in ["../outside", "a/../../outside", "a/../b", "a/..", r"foo\..\bar", "/tmp/outside", r"C:\\outside"]:
            invalid["allowed_paths"] = [bad_scope]
            with self.subTest(bad_scope=bad_scope):
                with self.assertRaises(ContractValidationError):
                    validate_stage_contract(invalid)
        self.assertFalse(path_is_allowed("src/demo/../outside.txt", ["src/demo"]))
        self.assertFalse(path_is_allowed("src/demo/file.txt", ["src/demo"], protected_paths=["src/demo"]))

        supervisor = Supervisor()
        supervisor.start_stage(contract(), {"artifact_score": 1.0, "runtime_ms": 80})
        task = supervisor.create_scoped_task()
        out_of_scope = result_for(task, changed_files=["outside.txt"])
        with self.assertRaises(ContractValidationError):
            supervisor.submit_result(task, out_of_scope)
        self.assertEqual(supervisor.state["iteration_index"], 0)

        blocked_task = supervisor.create_scoped_task()
        blocked = supervisor.submit_result(
            blocked_task,
            result_for(
                blocked_task,
                status="BLOCKED",
                scientific_result="AMBIGUOUS",
                changed_files=[],
            ),
        )
        self.assertEqual(blocked["decision"]["decision"], DecisionType.BLOCKED.value)
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertEqual(blocked["notification"]["type"], "BLOCKED")
        with self.assertRaises(SupervisorError):
            supervisor.create_scoped_task()


class ContractAndPolicyChecks(unittest.TestCase):
    def test_all_stage_schemas_load_and_fixture_contracts_validate(self):
        names = [
            "stage_contract.schema.json",
            "stage_context_pack.schema.json",
            "planner_decision.schema.json",
            "stage_review.schema.json",
            "codex_task.schema.json",
            "codex_result.schema.json",
        ]
        for name in names:
            with self.subTest(name=name):
                schema = load_schema(name)
                self.assertEqual(schema["type"], "object")
        fixture_contract = read_json("stage_contract.json")
        checked_contract = validate_stage_contract(fixture_contract)
        self.assertEqual(checked_contract["stage_id"], "stage-geometry-001")
        fixture_task = read_json("fake_codex_task.json")
        fixture_result = read_json("fake_result_reports.json")["reports"][0]
        validate_scoped_task(fixture_task)
        validate_result_report(fixture_result, fixture_task)

    def test_modern_stage_contract_requires_execution_fields(self):
        modern = {
            "project_goal": "Reduce the visible contour artifact.",
            "plan_id": "plan-modern-001",
            "schema_version": "stage_contract.v1",
            "stage_id": "stage-modern-001",
            "stage_name": "Contour sampling",
            "user_visible_goal": "Reduce the visible contour artifact.",
            "stage_scope": "contour-sampling",
            "baseline_ref": "artifact://baseline/modern",
            "acceptance_description": "Artifact score improves against baseline.",
            "review_artifacts": [],
            "protected_paths": [],
            "allowed_paths": ["src/demo"],
            "status": "ACTIVE",
            "iteration_count": 0,
            "same_abstraction_iteration_count": 0,
            "hypothesis": "Sampling is the source of the artifact.",
            "experiment": "Run one bounded sampling change.",
            "falsifier": "The artifact does not improve.",
            "abstraction_layer": "contour-sampling",
            "acceptance": ["artifact score improves"],
            "stop_rules": ["stop after 5 iterations"],
            "critic_mode": "NONE",
            "max_iterations": 5,
        }
        validate_stage_contract(modern)
        for execution_field in (
            "plan_id", "schema_version", "hypothesis", "experiment", "falsifier",
            "abstraction_layer", "acceptance", "stop_rules", "critic_mode", "max_iterations",
        ):
            incomplete = copy.deepcopy(modern)
            incomplete.pop(execution_field)
            with self.subTest(execution_field=execution_field):
                with self.assertRaises(ContractValidationError):
                    validate_stage_contract(incomplete)

    def test_five_planner_states_are_deterministic(self):
        cases = [
            ({"result_status": "SUCCEEDED"}, "CONTINUE"),
            ({"result_status": "FAILED", "scientific_result": "FALSIFIED"}, "REPLAN"),
            ({"result_status": "SUCCEEDED", "stage_ready": True}, "STAGE_READY"),
            ({"result_status": "SUCCEEDED", "human_gate_required": True}, "HUMAN_GATE"),
            ({"result_status": "BLOCKED"}, "BLOCKED"),
        ]
        for inputs, expected in cases:
            with self.subTest(expected=expected):
                first = planner_decision(**inputs)
                second = planner_decision(**inputs)
                self.assertEqual(first, second)
                self.assertEqual(first["decision"], expected)

    def test_failed_or_error_result_cannot_be_promoted_to_stage_ready(self):
        for status in ("FAILED", "ERROR"):
            with self.subTest(status=status):
                decision = planner_decision(result_status=status, stage_ready=True)
                self.assertEqual(decision["decision"], DecisionType.REPLAN.value)
        self.assertEqual(
            planner_decision(result_status="BLOCKED", stage_ready=True)["decision"],
            DecisionType.BLOCKED.value,
        )

    def test_stage_ready_requires_success_and_nonempty_review_evidence(self):
        supervisor = Supervisor()
        supervisor.start_stage(contract(), {"artifact_score": 1.0, "runtime_ms": 80})
        task = supervisor.create_scoped_task()
        empty_ready = result_for(task, stage_ready=True)
        empty_ready.update(
            {
                "evidence_refs": [],
                "measurements": {},
                "metrics": {},
                "review_artifacts": [],
            }
        )
        with self.assertRaises(ContractValidationError):
            supervisor.submit_result(task, empty_ready)
        self.assertEqual(supervisor.state["iteration_index"], 0)

        failed_ready = result_for(task, status="FAILED", stage_ready=True)
        with self.assertRaises(ContractValidationError):
            supervisor.submit_result(task, failed_ready)
        self.assertEqual(supervisor.state["iteration_index"], 0)

    def test_architecture_check_requires_two_same_layer_no_improvement_results(self):
        self.assertFalse(architecture_check(1, True)["triggered"])
        self.assertFalse(architecture_check(2, False)["triggered"])
        triggered = architecture_check(2, True)
        self.assertTrue(triggered["triggered"])
        self.assertEqual(triggered["route"], "REPLAN")
        self.assertEqual(triggered["legacy_route"], "ARCHITECTURE_RESET")
        self.assertEqual(triggered["planner_decision"], "REPLAN")

    def test_lifecycle_architecture_checkpoint_uses_configured_threshold(self):
        supervisor = Supervisor(architecture_threshold=3)
        started = supervisor.start_stage(contract(), {"artifact_score": 1.0})
        self.assertEqual(started["architecture_check"]["threshold"], 3)
        comparison = supervisor.start_representation_comparison(["rep-a"])
        self.assertEqual(comparison["architecture_check"]["threshold"], 3)
        selected = supervisor.select_winner("primary")
        self.assertEqual(selected["architecture_check"]["threshold"], 3)

    def test_compare_to_baseline_does_not_mutate_frozen_input(self):
        supervisor = Supervisor()
        started = supervisor.start_stage(contract(), {"artifact_score": 1.0, "runtime_ms": 80})
        baseline = copy.deepcopy(started["baseline"])
        comparison = compare_to_baseline(
            baseline,
            {"measurements": {"artifact_score": 0.25, "runtime_ms": 81}, "evidence_refs": []},
        )
        self.assertEqual(comparison["metric_deltas"]["artifact_score"], -0.75)
        self.assertEqual(baseline, started["baseline"])


if __name__ == "__main__":
    unittest.main()
