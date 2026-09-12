"""Read/submit adapter for the canonical V2 StageController.

`WorkflowRuntimeV2` owns no lifecycle state.  Its properties are projections
of the controller journal and its only mutating operation is command
submission to that controller.
"""

from __future__ import annotations

from typing import Any, Mapping

from .workflow_v2_controller import StageController


class WorkflowRuntimeV2:
    def __init__(self, controller: StageController, *, legacy_checkpoint: Mapping[str, Any] | None = None) -> None:
        self.controller = controller
        self._legacy_checkpoint = dict(legacy_checkpoint or {})

    def resume(self) -> dict[str, Any]:
        return self.controller.resume_projection()

    @property
    def phase(self) -> str:
        return self.resume()["phase"]

    @property
    def next_action(self) -> str | None:
        return self.resume()["next_action"]

    @property
    def next_actor(self) -> str:
        return self.resume()["next_actor"]

    @property
    def current_stage(self) -> dict[str, Any]:
        return self.resume()["stage"]

    @property
    def pending_gate(self) -> list[str]:
        return list(self.current_stage.get("pending_decisions", []))

    @property
    def attempt_count(self) -> int:
        return int(self.current_stage.get("attempt_count", 0))

    def submit(self, command_type: str, *, subject_id: str, payload: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
        return self.controller.dispatch(command_type, subject_id=subject_id, payload=payload, **kwargs)

    def read_legacy_checkpoint(self) -> dict[str, Any]:
        """Return legacy values for display only; none participate in routing."""

        return {"legacy_checkpoint": dict(self._legacy_checkpoint), "canonical": self.resume()}


__all__ = ["WorkflowRuntimeV2"]
