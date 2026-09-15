"""Static and import-boundary checks for the V2 migration surface."""

from __future__ import annotations

import unittest
from pathlib import Path

from src.workflow_runtime import WorkflowRuntimeV2 as RuntimeModuleV2
from src.workflow_v2_contracts import STAGE_STATES
from src.workflow_v2_controller import PUBLIC_COMMANDS, StageController
from src.workflow_v2_runtime import WorkflowRuntimeV2


class WorkflowV2MigrationBoundary(unittest.TestCase):
    def test_runtime_module_exports_the_explicit_v2_entrypoint(self):
        self.assertIs(RuntimeModuleV2, WorkflowRuntimeV2)

    def test_v2_route_contains_no_superseded_recovery_or_bootstrap_entrypoints(self):
        root = Path(__file__).resolve().parents[1] / "src"
        modules = [root / name for name in ("workflow_v2_contracts.py", "workflow_v2_controller.py", "workflow_v2_runtime.py", "workflow_v2_adapters.py")]
        forbidden = (
            "bind_existing_execution_result",
            "reconcile_retry_semantics",
            "authorize_recovery_bootstrap",
            "complete_recovery_bootstrap",
            "review_only",
        )
        for module in modules:
            source = module.read_text(encoding="utf-8")
            for token in forbidden:
                self.assertNotIn(token, source, f"{token} leaked into {module.name}")

    def test_v2_route_has_exact_state_and_command_surface(self):
        self.assertEqual(STAGE_STATES, ("PLANNED", "ACTIVE", "READY", "CLOSED", "STOPPED"))
        self.assertEqual(len(PUBLIC_COMMANDS), 18)
        self.assertEqual(len(set(PUBLIC_COMMANDS)), 18)
        self.assertTrue(callable(StageController.dispatch))


__all__ = ["WorkflowV2MigrationBoundary"]
