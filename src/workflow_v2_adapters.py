"""External-effect adapters for the V2 command protocol.

Adapters return observed receipts and submit them by operation identity.  They
do not decide Stage state, settle effects locally, or write the journal.
"""

from __future__ import annotations

from typing import Any, Mapping

from .workflow_v2_controller import StageController


class WorkflowV2ReceiptAdapter:
    def __init__(self, controller: StageController) -> None:
        self.controller = controller

    def apply_observed_receipt(
        self,
        *,
        stage_id: str,
        operation_id: str,
        effect_state: str,
        receipt: Mapping[str, Any],
        command_id: str,
    ) -> dict[str, Any]:
        return self.controller.apply_receipt(
            stage_id,
            operation_id=operation_id,
            effect_state=effect_state,
            receipt=receipt,
            command_id=command_id,
        )


__all__ = ["WorkflowV2ReceiptAdapter"]
