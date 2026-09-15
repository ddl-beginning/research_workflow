"""Bounded adapter from the thin workflow runtime into Core V1.

This module is intentionally an integration seam, not a second workflow
engine.  A caller must provide an already-created, active
``StageIntegrationAdapter`` and one or more provider implementations of the
existing ``ExecutionRequest -> ExecutionResult`` contract.  The adapter uses
the Core V1 integration methods for executor gating, result validation, and
Stage bookkeeping; it never edits controller state directly.

There is no safe default Stage/contract discovery here.  A project-specific
factory must explicitly construct the Core V1 objects and providers.  That
boundary is important for the local MCP server: without it, ``workflow_run``
fails closed instead of inventing a Stage or claiming execution completed.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Callable

from .contracts import ContractValidationError, sha256_json
from .executor import (
    ExecutionRequest,
    ExecutionResult,
    ExecutorProvider,
    discover_executors,
    select_executor,
    validate_execution_result,
)
from .stage_controller import StageState
from .stage_integration import StageIntegrationAdapter, StageIntegrationError


_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "access_token",
        "auth_token",
        "cookie",
        "cookies",
        "dom",
        "local_storage",
        "password",
        "prompt",
        "raw_dom",
        "raw_response",
        "secret",
        "session",
        "session_id",
        "session_storage",
        "storage_state",
        "token",
        "tokens",
    }
)


class CoreV1RunnerError(RuntimeError):
    """Bounded configuration or adapter failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        super().__init__(message)


def _bounded_copy(value: Any, *, depth: int = 0) -> Any:
    """Copy adapter metadata without retaining transport text or secrets."""

    if depth > 8:
        raise CoreV1RunnerError("CORE_RUNNER_RESULT_INVALID", "Core V1 result is too deeply nested")
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in value.items():
            name = str(key)
            if name.casefold().replace("-", "_") in _SENSITIVE_KEYS:
                raise CoreV1RunnerError("CORE_RUNNER_RESULT_INVALID", "Core V1 result contains a sensitive field")
            result[name] = _bounded_copy(child, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_bounded_copy(child, depth=depth + 1) for child in value]
    if isinstance(value, str):
        if len(value) > 4000 or "\x00" in value:
            raise CoreV1RunnerError("CORE_RUNNER_RESULT_INVALID", "Core V1 result contains oversized text")
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise CoreV1RunnerError("CORE_RUNNER_RESULT_INVALID", "Core V1 result must be JSON-safe")


def _required_capabilities(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise CoreV1RunnerError("CORE_RUNNER_REQUEST_INVALID", "required_capabilities must be an array")
    checked: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > 128:
            raise CoreV1RunnerError("CORE_RUNNER_REQUEST_INVALID", "required_capabilities contains an invalid value")
        if item.strip() not in checked:
            checked.append(item.strip())
    return tuple(checked)


class CoreV1RunnerAdapter:
    """Run one provider execution through an existing Core V1 Stage seam.

    The adapter is provider-neutral.  Selection is delegated to the existing
    deterministic executor policy; the selected provider receives only the
    immutable request built from the controller-authorized request.  The
    resulting normalized report is then handed to
    ``StageIntegrationAdapter.execute_local_action`` so StageController owns
    all lifecycle and decision validation.
    """

    def __init__(
        self,
        *,
        stage_integration: StageIntegrationAdapter,
        providers: Sequence[ExecutorProvider],
        preferred_provider: str | None = None,
    ) -> None:
        if not isinstance(stage_integration, StageIntegrationAdapter):
            raise CoreV1RunnerError(
                "RUNNER_NOT_CONFIGURED",
                "Core V1 runner requires an existing StageIntegrationAdapter",
            )
        if isinstance(providers, (str, bytes, bytearray)):
            raise CoreV1RunnerError("RUNNER_NOT_CONFIGURED", "Core V1 runner providers must be an array")
        try:
            provider_tuple = tuple(providers)
            descriptors = discover_executors(provider_tuple)
        except (TypeError, ContractValidationError) as exc:
            raise CoreV1RunnerError("RUNNER_NOT_CONFIGURED", "Core V1 executor providers are invalid") from exc
        if not provider_tuple:
            raise CoreV1RunnerError("RUNNER_NOT_CONFIGURED", "Core V1 runner has no configured executor providers")
        if preferred_provider is not None and (
            not isinstance(preferred_provider, str) or not preferred_provider.strip()
        ):
            raise CoreV1RunnerError("CORE_RUNNER_REQUEST_INVALID", "preferred_provider must be a non-empty string")
        self.stage_integration = stage_integration
        self.providers = provider_tuple
        self._providers_by_id = {
            descriptor.provider_id: provider for descriptor, provider in zip(descriptors, provider_tuple, strict=True)
        }
        self.preferred_provider = preferred_provider.strip() if isinstance(preferred_provider, str) else None

    @property
    def repository_root(self) -> Path:
        return Path(self.stage_integration.repository_root).resolve()

    def _request_options(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action_map = request.get("action_map")
        if not isinstance(action_map, Mapping) or action_map.get("validated") is not True or action_map.get("execute") is not True:
            raise CoreV1RunnerError(
                "CORE_RUNNER_REQUEST_INVALID",
                "workflow_run requires a validated and enabled ActionMap",
            )
        raw_metadata = request.get("metadata", {})
        if not isinstance(raw_metadata, Mapping):
            raise CoreV1RunnerError("CORE_RUNNER_REQUEST_INVALID", "metadata must be an object")
        stage_hint = request.get("stage_request", request.get("executor_request", {}))
        if stage_hint is None:
            stage_hint = {}
        if not isinstance(stage_hint, Mapping):
            raise CoreV1RunnerError("CORE_RUNNER_REQUEST_INVALID", "stage_request must be an object")
        return {
            "action_map": copy.deepcopy(dict(action_map)),
            "required_capabilities": _required_capabilities(request.get("required_capabilities")),
            "preferred_provider": request.get("preferred_provider", self.preferred_provider),
            "plan_id": request.get("plan_id"),
            "task_id": request.get("task_id"),
            "metadata": _bounded_copy(dict(raw_metadata)),
            "objective": request.get("objective", stage_hint.get("objective")),
            "mode": request.get("mode", stage_hint.get("mode", "PRIMARY")),
            "lane": request.get("lane", stage_hint.get("lane", "PRIMARY")),
            "abstraction_layer": request.get("abstraction_layer", "core-v1-executor"),
            "user_visible_improvement": request.get("user_visible_improvement"),
        }

    def run(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Execute one request through Core V1 and return a validated outcome."""

        if not isinstance(request, Mapping):
            raise CoreV1RunnerError("CORE_RUNNER_REQUEST_INVALID", "workflow_run request must be an object")
        options = self._request_options(request)
        selected_provider: dict[str, Any] = {}
        normalized_result: dict[str, Any] = {}

        def execute_provider(_response_view: Mapping[str, Any] | None, controller_request: Mapping[str, Any]) -> Mapping[str, Any]:
            nonlocal selected_provider, normalized_result
            try:
                execution_request = ExecutionRequest.from_stage_request(
                    controller_request,
                    plan_id=options["plan_id"],
                    task_id=options["task_id"],
                    required_capabilities=options["required_capabilities"],
                    preferred_provider=options["preferred_provider"],
                    action_map=options["action_map"],
                    workspace_root=str(self.repository_root),
                    metadata=options["metadata"],
                )
                selection = select_executor(execution_request, self.providers)
                provider = self._providers_by_id[selection.provider_id]
                execution_result = provider.execute(execution_request)
                if not isinstance(execution_result, ExecutionResult):
                    raise CoreV1RunnerError("CORE_RUNNER_RESULT_INVALID", "executor must return ExecutionResult")
                if execution_result.provider_id != selection.provider_id:
                    raise CoreV1RunnerError("CORE_RUNNER_RESULT_INVALID", "executor result provider identity mismatches selection")
                normalized_result = validate_execution_result(execution_result.to_stage_result())
                # Make provider/model/executable provenance part of the
                # bounded result handed to StageController.  Providers keep
                # transport details in ExecutionResult.evidence; only these
                # conventional audit fields cross the Stage boundary.
                provenance: dict[str, Any] = {
                    "provider_id": execution_result.provider_id,
                    "adapter": "core_v1",
                }
                for field in (
                    "provider", "model", "actual_model", "execution_profile",
                    "reasoning_effort", "auth_mode", "profile_derivation_reason",
                    "executor_request_id", "executable", "cli_version", "receipt_path",
                    "receipt_actual_path", "receipt_owner", "receipt_authority", "receipt_schema_version",
                ):
                    candidate = execution_result.evidence.get(field)
                    if isinstance(candidate, str) and candidate.strip():
                        provenance[field] = candidate.strip()[:512]
                if "actual_model" not in provenance and isinstance(provenance.get("model"), str):
                    provenance["actual_model"] = provenance["model"]
                normalized_result["provider_id"] = execution_result.provider_id
                normalized_result["attempt_index"] = controller_request.get("attempt_index", 1)
                normalized_result["stage_iteration_index"] = controller_request.get(
                    "stage_iteration_index", controller_request.get("iteration_index")
                )
                normalized_result["provenance"] = provenance
                selected_provider = {
                    "provider_id": execution_result.provider_id,
                    "selection": selection.bounded_view(),
                }
                return normalized_result
            except CoreV1RunnerError:
                raise
            except (ContractValidationError, KeyError) as exc:
                raise CoreV1RunnerError("CORE_RUNNER_EXECUTION_REJECTED", "Core V1 rejected provider execution") from exc

        try:
            core_result = self.stage_integration.execute_local_action(
                execute_provider,
                objective=options["objective"],
                abstraction_layer=options["abstraction_layer"],
                user_visible_improvement=options["user_visible_improvement"],
                mode=options["mode"],
                lane=options["lane"],
            )
        except StageIntegrationError as exc:
            raise CoreV1RunnerError("CORE_STAGE_EXECUTION_REJECTED", "Core V1 StageIntegration rejected execution") from exc

        controller_result = core_result.get("controller") if isinstance(core_result, Mapping) else None
        stage = controller_result.get("stage") if isinstance(controller_result, Mapping) else None
        if not isinstance(stage, Mapping):
            raise CoreV1RunnerError("CORE_RUNNER_RESULT_INVALID", "Core V1 did not return a Stage result")
        status = str(stage.get("status", "")).upper()
        decision = controller_result.get("decision") if isinstance(controller_result, Mapping) else None
        if status == StageState.STAGE_READY.value:
            outcome = "COMPLETE"
        elif status == StageState.BLOCKED.value:
            outcome = "BLOCKED"
        elif status == StageState.ACTIVE.value:
            outcome = "CONTINUE_WORKFLOW"
        else:
            raise CoreV1RunnerError("CORE_RUNNER_RESULT_INVALID", "Core V1 returned an unsupported Stage status")
        safe_execution = _bounded_copy(normalized_result)
        safe_evidence = _bounded_copy(core_result.get("evidence")) if core_result.get("evidence") is not None else None
        return {
            "status": normalized_result.get("status"),
            "provider_id": selected_provider.get("provider_id"),
            "decision": decision,
            "stage_id": stage.get("contract", {}).get("stage_id") if isinstance(stage.get("contract"), Mapping) else None,
            "iteration_index": stage.get("iteration_index"),
            "execution_result": safe_execution,
            "core_evidence": safe_evidence,
            "provider_selection": selected_provider.get("selection"),
            "result_digest": sha256_json(normalized_result),
            "runner_outcome": {
                "schema_version": "workflow_runner_outcome.v1",
                "outcome": outcome,
                "validated": True,
                "evidence_validated": True,
            },
        }


def build_core_v1_runner_adapter(
    *,
    stage_integration: StageIntegrationAdapter | None,
    providers: Sequence[ExecutorProvider] | None,
    preferred_provider: str | None = None,
) -> CoreV1RunnerAdapter:
    """Explicit factory used by a configured runtime/MCP deployment.

    No workspace or Stage is inferred.  Callers must supply both the existing
    Core V1 integration and provider list discovered/configured by the host.
    """

    if stage_integration is None:
        raise CoreV1RunnerError("RUNNER_NOT_CONFIGURED", "Core V1 StageIntegration is not configured")
    if providers is None:
        raise CoreV1RunnerError("RUNNER_NOT_CONFIGURED", "Core V1 executor providers are not configured")
    return CoreV1RunnerAdapter(
        stage_integration=stage_integration,
        providers=providers,
        preferred_provider=preferred_provider,
    )


__all__ = [
    "CoreV1RunnerAdapter",
    "CoreV1RunnerError",
    "build_core_v1_runner_adapter",
]
