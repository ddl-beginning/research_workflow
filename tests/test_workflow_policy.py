"""Focused, deterministic checks for the independent workflow policy contract."""

from __future__ import annotations

import unittest

from src.workflow_policy import (
    ACTION_ALLOWLIST,
    ActionMap,
    ActionMapError,
    ArtifactPolicy,
    ArtifactPolicyError,
    CANONICAL_HUMAN_ARTIFACTS,
    ConsultationBudget,
    ConsultationBudgetError,
    NodeOwner,
    TransitionPolicy,
    TransitionPolicyError,
    run_policy_self_test,
)


class ActionMapTests(unittest.TestCase):
    def test_execution_is_false_until_validation(self) -> None:
        action_map = ActionMap(
            actions={"run_local_checks": NodeOwner.CODEX},
            execute=True,
        )
        self.assertFalse(action_map.validated)
        self.assertFalse(action_map.can_execute())
        with self.assertRaises(ActionMapError):
            action_map.dispatch(lambda *_: None)

        action_map.validate()
        self.assertTrue(action_map.can_execute())
        calls = action_map.dispatch(lambda name, spec: (name, spec.owner))
        self.assertEqual(calls, [("run_local_checks", NodeOwner.CODEX)])

    def test_unknown_action_is_rejected_by_allowlist(self) -> None:
        self.assertIn("run_local_checks", ACTION_ALLOWLIST)
        with self.assertRaises(ActionMapError):
            ActionMap({"delete_repository": NodeOwner.CODEX}).validate()

    def test_gpt_cannot_directly_own_local_side_effect(self) -> None:
        with self.assertRaises(ActionMapError):
            ActionMap(
                {"request_fresh_consultation": NodeOwner.GPT},
                execute=True,
            ).validate()

    def test_human_gated_action_requires_approval(self) -> None:
        action_map = ActionMap(
            {
                "transition_stage": {
                    "owner": NodeOwner.CODEX,
                    "requires_human": True,
                }
            },
            execute=True,
        ).validate()
        self.assertFalse(action_map.can_execute())
        action_map.approve(actor="reviewer", rationale="explicitly approved")
        self.assertTrue(action_map.can_execute())


class ConsultationBudgetTests(unittest.TestCase):
    def test_fixed_default_limits(self) -> None:
        budget = ConsultationBudget()
        self.assertEqual((budget.fresh, budget.normal, budget.total), (1, 1, 2))
        self.assertEqual((budget.retry, budget.gpt_triggered), (0, 0))

    def test_fresh_normal_and_total_limits(self) -> None:
        budget = ConsultationBudget()
        budget.request("FRESH")
        budget.request("NORMAL")
        self.assertEqual(budget.total_count, 2)
        with self.assertRaises(ConsultationBudgetError):
            budget.request("NORMAL")

    def test_second_gpt_suggestion_cannot_trigger_a_request(self) -> None:
        budget = ConsultationBudget()
        budget.request("FRESH")
        with self.assertRaises(ConsultationBudgetError):
            budget.request("NORMAL", triggered_by_gpt=True)
        self.assertEqual(budget.total_count, 1)
        self.assertEqual(budget.gpt_triggered_count, 0)

    def test_retry_is_disabled(self) -> None:
        with self.assertRaises(ConsultationBudgetError):
            ConsultationBudget().request("FRESH", retry=True)


class TransitionPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = TransitionPolicy()

    def test_key_human_gates_are_explicit(self) -> None:
        self.assertTrue(self.policy.requires_human("PLANNED", "ACTIVE"))
        self.assertTrue(self.policy.requires_human("STAGE_READY", "APPROVED"))
        self.assertTrue(self.policy.requires_human("HUMAN_GATE", "ACTIVE"))
        with self.assertRaises(TransitionPolicyError):
            self.policy.validate_transition("STAGE_READY", "APPROVED")
        self.assertEqual(
            self.policy.validate_transition(
                "STAGE_READY", "APPROVED", human_approved=True, actor="reviewer"
            ),
            ("STAGE_READY", "APPROVED"),
        )

    def test_illegal_transition_is_rejected(self) -> None:
        self.assertFalse(self.policy.can_transition("APPROVED", "ACTIVE"))
        with self.assertRaises(TransitionPolicyError):
            self.policy.validate_transition("APPROVED", "ACTIVE")


class ArtifactPolicyTests(unittest.TestCase):
    def test_policy_has_exactly_three_fixed_files(self) -> None:
        policy = ArtifactPolicy()
        self.assertEqual(policy.FILES, CANONICAL_HUMAN_ARTIFACTS)
        self.assertEqual(policy.validate(CANONICAL_HUMAN_ARTIFACTS), CANONICAL_HUMAN_ARTIFACTS)
        self.assertTrue(policy.allows("HUMAN_REVIEW.md"))
        self.assertFalse(policy.allows("extra-notes.md"))

    def test_artifact_scope_rejects_extra_or_nested_paths(self) -> None:
        policy = ArtifactPolicy()
        with self.assertRaises(ArtifactPolicyError):
            policy.validate((*CANONICAL_HUMAN_ARTIFACTS, "extra.md"))
        with self.assertRaises(ArtifactPolicyError):
            policy.validate(("STAGE_EXECUTION_PLAN.md", "RESEARCH_DECISION_LOG.md", "nested/HUMAN_REVIEW.md"))


class ControlledSelfTest(unittest.TestCase):
    def test_controlled_self_test(self) -> None:
        self.assertEqual(
            run_policy_self_test(),
            {
                "action_map": "PASS",
                "consultation_budget": "PASS",
                "transition_policy": "PASS",
                "artifact_policy": "PASS",
            },
        )


if __name__ == "__main__":
    unittest.main()
