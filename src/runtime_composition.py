"""Configuration-driven assembly for the local workflow runtime.

This module is deliberately a composition seam, not another workflow engine.
It discovers and validates the existing Stage, integration adapter, executor
providers, and Core V1 adapter, then returns them as one bounded object.  It
never creates an implicit Stage, chooses a model on behalf of a request, or
mutates Stage state outside the existing controller methods.

The default provider is the existing native OpenAI Codex provider.  A small
provider-factory argument exists for offline tests and future installers; the
production path does not use it and remains configuration/discovery driven.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .contracts import ContractValidationError, validate_against_schema
from .bridge_adapter import ProjectScopedBridgeConsultant
from .executor import ExecutorProvider, discover_executors
from .openai_codex_executor import OpenAICodexExecutor, OpenAICodexUnavailable
from .stage_controller import StageController, StageControllerError, validate_stage_contract_v1
from .stage_integration import StageIntegrationAdapter, subprocess_bridge_runner
from .workflow_core_adapter import CoreV1RunnerAdapter, CoreV1RunnerError, build_core_v1_runner_adapter
from .workflow_orchestrator import WorkflowOrchestrator, WorkflowOrchestratorDependencies


RUNTIME_COMPOSITION_SCHEMA_VERSION = "runtime_composition.v1"
DEFAULT_RUNTIME_CONFIG_RELATIVE = Path(".research") / "runtime-composition.json"
DEFAULT_STAGE_STATE_RELATIVE = Path(".research") / "stage-state.json"
DEFAULT_STAGE_RECEIPT_RELATIVE = Path(".research") / "integration"
RUNTIME_CONFIG_ENV = "RESEARCH_WORKFLOW_RUNTIME_CONFIG"

_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "access_token",
        "auth_token",
        "cookie",
        "cookies",
        "password",
        "secret",
        "token",
        "tokens",
    }
)


class RuntimeCompositionError(RuntimeError):
    """A bounded, fail-closed composition diagnostic."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        missing: Sequence[Mapping[str, Any]] = (),
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = str(code)
        self.missing = tuple(_bounded_missing(item) for item in missing)
        self.details = copy.deepcopy(dict(details or {}))
        super().__init__(message)

    def bounded_view(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": str(self),
            "missing": copy.deepcopy(list(self.missing)),
        }
        if self.details:
            payload["details"] = copy.deepcopy(self.details)
        return payload


def _bounded_missing(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"component": "composition", "code": "INVALID_DIAGNOSTIC", "message": "invalid diagnostic"}
    component = value.get("component", "composition")
    code = value.get("code", "UNAVAILABLE")
    message = value.get("message", "component is unavailable")
    result = {
        "component": str(component)[:128],
        "code": str(code)[:128],
        "message": str(message)[:512],
    }
    path = value.get("path")
    if isinstance(path, str) and path.strip():
        result["path"] = path[:2000]
    return result


def _safe_config_walk(value: Any, *, path: str = "$") -> None:
    """Reject obvious secret fields before config is retained or echoed."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().casefold().replace("-", "_")
            if normalized in _SENSITIVE_KEYS:
                raise RuntimeCompositionError("RUNTIME_CONFIG_INVALID", f"sensitive config field is not allowed at {path}")
            _safe_config_walk(child, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _safe_config_walk(child, path=f"{path}[{index}]")
        return
    if isinstance(value, str) and len(value) > 4000:
        raise RuntimeCompositionError("RUNTIME_CONFIG_INVALID", f"config value is too long at {path}")
    if value is None or isinstance(value, (str, bool, int, float)):
        return
    raise RuntimeCompositionError("RUNTIME_CONFIG_INVALID", f"config contains an unsupported value at {path}")


def _workspace_root(value: str | os.PathLike[str]) -> Path:
    candidate = Path(value).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RuntimeCompositionError(
            "RUNTIME_COMPOSITION_NOT_READY",
            "workspace_root must be an existing directory",
            missing=(
                {"component": "workspace", "code": "WORKSPACE_INVALID", "message": "workspace is not an existing directory"},
            ),
        ) from exc
    if not resolved.is_dir():
        raise RuntimeCompositionError(
            "RUNTIME_COMPOSITION_NOT_READY",
            "workspace_root must be an existing directory",
            missing=(
                {"component": "workspace", "code": "WORKSPACE_INVALID", "message": "workspace is not an existing directory"},
            ),
        )
    return resolved


def _workspace_path(root: Path, value: str | os.PathLike[str], *, field: str, must_exist: bool = False) -> Path:
    candidate = Path(value).expanduser()
    try:
        resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeCompositionError(
            "RUNTIME_CONFIG_INVALID",
            f"{field} must remain inside workspace",
            missing=(
                {"component": field, "code": "PATH_OUTSIDE_WORKSPACE", "message": "configured path is outside workspace"},
            ),
        ) from exc
    if must_exist and not resolved.is_file():
        raise RuntimeCompositionError(
            "RUNTIME_COMPOSITION_NOT_READY",
            f"{field} does not exist",
            missing=(
                {"component": field, "code": "FILE_NOT_FOUND", "message": "configured file does not exist", "path": str(resolved)},
            ),
        )
    return resolved


def _external_path(value: str | os.PathLike[str], *, field: str) -> Path:
    candidate = Path(value).expanduser()
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as exc:
        raise RuntimeCompositionError(
            "RUNTIME_CONFIG_INVALID",
            f"{field} is not a valid path",
            missing=(
                {"component": field, "code": "PATH_INVALID", "message": "configured path is invalid"},
            ),
        ) from exc
    return resolved


def _optional_string(value: Any, field: str, *, maximum: int = 2000) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum or "\x00" in value:
        raise RuntimeCompositionError("RUNTIME_CONFIG_INVALID", f"{field} must be a bounded string or null")
    return value.strip()


@dataclass(frozen=True)
class RuntimeCompositionConfig:
    """Normalized, non-secret runtime composition settings."""

    schema_version: str = RUNTIME_COMPOSITION_SCHEMA_VERSION
    config_path: Path | None = None
    stage_state_path: str = DEFAULT_STAGE_STATE_RELATIVE.as_posix()
    stage_contract_path: str | None = None
    stage_id: str | None = None
    receipt_root: str = DEFAULT_STAGE_RECEIPT_RELATIVE.as_posix()
    providers: tuple[str, ...] = ("openai-codex",)
    preferred_provider: str | None = None
    codex_executable: str | None = None
    preferred_model: str = "luna"
    fallback_model: str | None = None
    auth_mode: str = "chatgpt"
    timeout_seconds: float = 900.0
    artifact_dir: str | None = None
    bridge_enabled: bool = False
    bridge_root: str | None = None
    node_executable: str = "node"
    project_url: str | None = None

    def bounded_view(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "config_path": str(self.config_path) if self.config_path is not None else None,
            "stage": {
                "state_path": self.stage_state_path,
                "contract_path": self.stage_contract_path,
                "stage_id": self.stage_id,
                "receipt_root": self.receipt_root,
            },
            "executor": {
                "providers": list(self.providers),
                "preferred_provider": self.preferred_provider,
                "codex_executable_configured": self.codex_executable is not None,
                "preferred_model": self.preferred_model,
                "fallback_model": self.fallback_model,
                "auth_mode": self.auth_mode,
                "timeout_seconds": self.timeout_seconds,
                "artifact_dir_configured": self.artifact_dir is not None,
            },
            "bridge": {
                "enabled": self.bridge_enabled,
                "root_configured": self.bridge_root is not None,
                "node_executable": self.node_executable,
                "project_url_configured": self.project_url is not None,
            },
        }


@dataclass(frozen=True)
class RuntimeCompositionDiagnostic:
    workspace_root: Path
    config_path: Path
    config_loaded: bool
    ready: bool
    components: Mapping[str, Any] = field(default_factory=dict)
    missing: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()

    def bounded_view(self) -> dict[str, Any]:
        return {
            "workspace_root": self.workspace_root.as_posix(),
            "config_path": self.config_path.as_posix(),
            "config_loaded": self.config_loaded,
            "ready": self.ready,
            "components": copy.deepcopy(dict(self.components)),
            "missing": copy.deepcopy(list(self.missing)),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class RuntimeComposition:
    workspace_root: Path
    config: RuntimeCompositionConfig
    controller: StageController
    stage_integration: StageIntegrationAdapter
    providers: tuple[ExecutorProvider, ...]
    runner: CoreV1RunnerAdapter
    diagnostic: RuntimeCompositionDiagnostic

    def bounded_view(self) -> dict[str, Any]:
        stage = self.controller.show_stage(self.stage_integration.stage_id)
        return {
            "workspace_root": self.workspace_root.as_posix(),
            "config": self.config.bounded_view(),
            "stage": {
                "stage_id": stage.get("contract", {}).get("stage_id"),
                "status": stage.get("status"),
                "iteration_index": stage.get("iteration_index"),
                "repository_root": stage.get("contract", {}).get("repository_root"),
            },
            "providers": [getattr(provider, "descriptor").bounded_view() for provider in self.providers],
            "diagnostic": self.diagnostic.bounded_view(),
        }


def _raw_config_path(workspace: Path, config_path: str | os.PathLike[str] | None) -> Path:
    selected = config_path
    if selected is None:
        selected = os.environ.get(RUNTIME_CONFIG_ENV)
    if selected is None:
        return workspace / DEFAULT_RUNTIME_CONFIG_RELATIVE
    path = Path(selected).expanduser()
    return path.resolve() if path.is_absolute() else (workspace / path).resolve()


def _config_from_raw(raw: Mapping[str, Any], *, path: Path) -> RuntimeCompositionConfig:
    try:
        validate_against_schema(dict(raw), "runtime_composition.v1")
    except ContractValidationError as exc:
        raise RuntimeCompositionError("RUNTIME_CONFIG_INVALID", "runtime composition config does not match schema") from exc
    _safe_config_walk(raw)
    stage = raw.get("stage", {})
    executor = raw.get("executor", {})
    bridge = raw.get("bridge", {})
    if not isinstance(stage, Mapping) or not isinstance(executor, Mapping) or not isinstance(bridge, Mapping):
        raise RuntimeCompositionError("RUNTIME_CONFIG_INVALID", "runtime composition sections must be objects")
    providers_value = executor.get("providers", ["openai-codex"])
    if not isinstance(providers_value, list) or not providers_value or any(not isinstance(item, str) or not item.strip() for item in providers_value):
        raise RuntimeCompositionError("RUNTIME_CONFIG_INVALID", "executor.providers must be a non-empty string array")
    providers = tuple(dict.fromkeys(item.strip() for item in providers_value))
    preferred_provider = _optional_string(executor.get("preferred_provider"), "executor.preferred_provider", maximum=128)
    preferred_model = executor.get("preferred_model", "luna")
    if not isinstance(preferred_model, str) or not preferred_model.strip():
        raise RuntimeCompositionError("RUNTIME_CONFIG_INVALID", "executor.preferred_model must be non-empty")
    fallback_model = _optional_string(executor.get("fallback_model"), "executor.fallback_model", maximum=128)
    auth_mode = executor.get("auth_mode", "chatgpt")
    if auth_mode != "chatgpt":
        raise RuntimeCompositionError("RUNTIME_CONFIG_INVALID", "executor.auth_mode must be chatgpt")
    timeout_seconds = executor.get("timeout_seconds", 900.0)
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds < 1 or timeout_seconds > 86400:
        raise RuntimeCompositionError("RUNTIME_CONFIG_INVALID", "executor.timeout_seconds is outside the bounded range")
    stage_state_path = stage.get("state_path", DEFAULT_STAGE_STATE_RELATIVE.as_posix())
    if not isinstance(stage_state_path, str) or not stage_state_path.strip():
        raise RuntimeCompositionError("RUNTIME_CONFIG_INVALID", "stage.state_path must be non-empty")
    receipt_root = stage.get("receipt_root", DEFAULT_STAGE_RECEIPT_RELATIVE.as_posix())
    if not isinstance(receipt_root, str) or not receipt_root.strip():
        raise RuntimeCompositionError("RUNTIME_CONFIG_INVALID", "stage.receipt_root must be non-empty")
    stage_contract_path = _optional_string(stage.get("contract_path"), "stage.contract_path", maximum=2000)
    stage_id = _optional_string(stage.get("stage_id"), "stage.stage_id", maximum=256)
    bridge_enabled = bridge.get("enabled", False)
    if not isinstance(bridge_enabled, bool):
        raise RuntimeCompositionError("RUNTIME_CONFIG_INVALID", "bridge.enabled must be boolean")
    bridge_root = _optional_string(bridge.get("root"), "bridge.root", maximum=2000)
    node_executable = bridge.get("node_executable", "node")
    if not isinstance(node_executable, str) or not node_executable.strip():
        raise RuntimeCompositionError("RUNTIME_CONFIG_INVALID", "bridge.node_executable must be non-empty")
    return RuntimeCompositionConfig(
        schema_version=RUNTIME_COMPOSITION_SCHEMA_VERSION,
        config_path=path,
        stage_state_path=stage_state_path.strip(),
        stage_contract_path=stage_contract_path,
        stage_id=stage_id,
        receipt_root=receipt_root.strip(),
        providers=providers,
        preferred_provider=preferred_provider,
        codex_executable=_optional_string(executor.get("codex_executable"), "executor.codex_executable", maximum=2000),
        preferred_model=preferred_model.strip(),
        fallback_model=fallback_model,
        auth_mode="chatgpt",
        timeout_seconds=float(timeout_seconds),
        artifact_dir=_optional_string(executor.get("artifact_dir"), "executor.artifact_dir", maximum=2000),
        bridge_enabled=bridge_enabled,
        bridge_root=bridge_root,
        node_executable=node_executable.strip(),
        project_url=_optional_string(bridge.get("project_url"), "bridge.project_url", maximum=2000),
    )


def load_runtime_composition_config(
    workspace: str | os.PathLike[str],
    *,
    config_path: str | os.PathLike[str] | None = None,
) -> RuntimeCompositionConfig:
    """Load an optional project-local config or return safe defaults."""

    root = _workspace_root(workspace)
    path = _raw_config_path(root, config_path)
    explicit = config_path is not None or os.environ.get(RUNTIME_CONFIG_ENV) is not None
    if not path.is_file():
        if explicit:
            raise RuntimeCompositionError(
                "RUNTIME_CONFIG_INVALID",
                "configured runtime composition file does not exist",
                missing=(
                    {"component": "runtime.config", "code": "FILE_NOT_FOUND", "message": "configured runtime config is missing", "path": str(path)},
                ),
            )
        return RuntimeCompositionConfig(config_path=path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeCompositionError("RUNTIME_CONFIG_INVALID", "runtime composition config is unreadable") from exc
    if not isinstance(raw, Mapping):
        raise RuntimeCompositionError("RUNTIME_CONFIG_INVALID", "runtime composition config must be an object")
    return _config_from_raw(raw, path=path)


def _normalize_config_input(
    workspace: Path,
    config: RuntimeCompositionConfig | Mapping[str, Any] | None,
    config_path: str | os.PathLike[str] | None,
) -> RuntimeCompositionConfig:
    if config is None:
        return load_runtime_composition_config(workspace, config_path=config_path)
    if isinstance(config, RuntimeCompositionConfig):
        return config
    if isinstance(config, Mapping):
        return _config_from_raw(config, path=_raw_config_path(workspace, config_path))
    raise RuntimeCompositionError("RUNTIME_CONFIG_INVALID", "config must be a RuntimeCompositionConfig or object")


def _resolve_codex(config: RuntimeCompositionConfig) -> str | None:
    if config.codex_executable:
        configured = Path(config.codex_executable).expanduser()
        if configured.is_file():
            return str(configured.resolve())
        return shutil.which(config.codex_executable)
    return shutil.which("codex")


def _resolve_node(config: RuntimeCompositionConfig) -> str | None:
    configured = Path(config.node_executable).expanduser()
    if configured.is_file():
        return str(configured.resolve())
    return shutil.which(config.node_executable)


def _configured_bridge_runner(
    root: Path,
    config: RuntimeCompositionConfig,
    *,
    bridge_runner_factory: Callable[..., Callable[..., Mapping[str, Any]]] | None = None,
) -> Callable[..., Mapping[str, Any]] | None:
    """Build the optional configured bridge callable without selecting a workflow."""

    if not config.bridge_enabled:
        return None
    if config.bridge_root is None:
        raise RuntimeCompositionError(
            "RUNTIME_COMPOSITION_NOT_READY",
            "bridge root is required when bridge.enabled is true",
            missing=(
                {"component": "bridge.root", "code": "BRIDGE_ROOT_REQUIRED", "message": "bridge root is required when bridge.enabled is true"},
            ),
        )
    bridge_root = _external_path(config.bridge_root, field="bridge.root")
    node = _resolve_node(config)
    if node is None:
        raise RuntimeCompositionError(
            "RUNTIME_COMPOSITION_NOT_READY",
            "Node executable is unavailable",
            missing=(
                {"component": "bridge.node", "code": "NODE_NOT_FOUND", "message": "configured Node executable was not found"},
            ),
        )
    if bridge_runner_factory is None:
        runner = partial(
            subprocess_bridge_runner,
            bridge_root=str(bridge_root),
            node_executable=node,
            project_url=config.project_url,
        )
    else:
        try:
            runner = bridge_runner_factory(
                bridge_root=bridge_root,
                node_executable=node,
                project_url=config.project_url,
                workspace_root=root,
            )
        except Exception as exc:  # noqa: BLE001 - explicit composition seam
            raise RuntimeCompositionError(
                "RUNTIME_COMPOSITION_NOT_READY",
                "bridge runner factory failed",
                missing=(
                    {"component": "bridge", "code": "FACTORY_FAILED", "message": "configured bridge runner could not be created"},
                ),
            ) from exc
    if not callable(runner):
        raise RuntimeCompositionError(
            "RUNTIME_COMPOSITION_NOT_READY",
            "bridge runner factory returned a non-callable",
            missing=(
                {"component": "bridge", "code": "FACTORY_INVALID", "message": "configured bridge runner is not callable"},
            ),
        )
    return runner


def _stage_paths(root: Path, config: RuntimeCompositionConfig) -> tuple[Path, Path]:
    state_path = _workspace_path(root, config.stage_state_path, field="stage.state_path")
    contract_path = (
        _workspace_path(root, config.stage_contract_path, field="stage.contract_path")
        if config.stage_contract_path is not None
        else root / "__no_contract_configured__"
    )
    return state_path, contract_path


def doctor_runtime_composition(
    workspace: str | os.PathLike[str],
    *,
    config: RuntimeCompositionConfig | Mapping[str, Any] | None = None,
    config_path: str | os.PathLike[str] | None = None,
    provider_factory: Callable[..., Any] | None = None,
) -> RuntimeCompositionDiagnostic:
    """Read-only bounded diagnosis for the default runtime composition."""

    root = _workspace_root(workspace)
    normalized = _normalize_config_input(root, config, config_path)
    path = normalized.config_path or _raw_config_path(root, config_path)
    components: dict[str, Any] = {
        "workspace": "ready",
        "config": "loaded" if path.is_file() else "defaults",
        "bridge": "disabled" if not normalized.bridge_enabled else "pending",
    }
    missing: list[dict[str, Any]] = []
    try:
        state_path, contract_path = _stage_paths(root, normalized)
    except RuntimeCompositionError as exc:
        missing.extend(exc.missing)
        state_path = root / "__invalid_state_path__"
        contract_path = root / "__invalid_contract_path__"
    stage_ready = False
    if state_path.is_file():
        try:
            controller = StageController.from_state(state_path)
            selected = normalized.stage_id or controller.state.get("current_stage_id")
            if not isinstance(selected, str) or not selected:
                raise StageControllerError("stage state has no current stage")
            record = controller.show_stage(selected)
            repository_root = Path(str(record["contract"]["repository_root"])).expanduser().resolve()
            if repository_root != root:
                missing.append({"component": "stage.identity", "code": "WORKSPACE_IDENTITY_MISMATCH", "message": "Stage contract repository_root does not match workspace"})
            else:
                stage_ready = True
                components["stage"] = {"state": "loadable", "stage_id": selected, "status": record.get("status")}
        except (OSError, RuntimeError, ValueError, StageControllerError, ContractValidationError, KeyError, TypeError):
            missing.append({"component": "stage.state", "code": "STATE_INVALID", "message": "stage state is not loadable"})
    elif normalized.stage_contract_path is not None and contract_path.is_file():
        try:
            raw_contract = json.loads(contract_path.read_text(encoding="utf-8"))
            contract = validate_stage_contract_v1(raw_contract)
            repository_root = Path(contract["repository_root"]).expanduser().resolve()
            if repository_root != root:
                missing.append({"component": "stage.identity", "code": "WORKSPACE_IDENTITY_MISMATCH", "message": "Stage contract repository_root does not match workspace"})
            else:
                stage_ready = True
                components["stage"] = {"state": "contract-ready", "stage_id": contract["stage_id"], "status": "PLANNED"}
        except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError, ValueError, ContractValidationError, KeyError, TypeError):
            missing.append({"component": "stage.contract", "code": "CONTRACT_INVALID", "message": "stage contract is not loadable"})
    else:
        missing.append({"component": "stage", "code": "STAGE_STATE_OR_CONTRACT_MISSING", "message": "an existing stage state or explicit stage contract is required"})
    if not stage_ready:
        components.setdefault("stage", "missing")

    unknown = [provider_id for provider_id in normalized.providers if provider_id != "openai-codex" and provider_factory is None]
    for provider_id in unknown:
        missing.append({"component": f"executor.{provider_id}", "code": "PROVIDER_UNSUPPORTED", "message": "no built-in provider is registered for this id"})
    if provider_factory is None:
        executable = _resolve_codex(normalized)
        if executable is None:
            missing.append({"component": "executor.codex_cli", "code": "EXECUTABLE_NOT_FOUND", "message": "Codex CLI was not found by configured path or PATH"})
        else:
            components["executor.codex_cli"] = {"state": "discovered", "path_present": True}
        if normalized.auth_mode != "chatgpt":
            missing.append({"component": "executor.auth", "code": "AUTH_MODE_UNSUPPORTED", "message": "only saved ChatGPT auth is supported"})
    else:
        components["executor.provider_factory"] = "injected-test-or-installer-seam"

    if normalized.artifact_dir is not None:
        artifact = _external_path(normalized.artifact_dir, field="executor.artifact_dir")
        try:
            artifact.relative_to(root)
        except ValueError:
            components["executor.artifact_dir"] = {"state": "external-configured"}
        else:
            missing.append({"component": "executor.artifact_dir", "code": "ARTIFACT_PATH_INSIDE_WORKSPACE", "message": "Codex evidence artifact_dir must be outside workspace"})

    if normalized.bridge_enabled:
        if normalized.bridge_root is None:
            missing.append({"component": "bridge.root", "code": "BRIDGE_ROOT_REQUIRED", "message": "bridge root is required when bridge.enabled is true"})
        else:
            bridge_root = _external_path(normalized.bridge_root, field="bridge.root")
            if not bridge_root.is_dir():
                missing.append({"component": "bridge.root", "code": "BRIDGE_ROOT_NOT_FOUND", "message": "configured bridge root is not a directory"})
            elif not (bridge_root / "scripts" / "consult-pack.mjs").is_file():
                missing.append({"component": "bridge.consult_pack", "code": "BRIDGE_SCRIPT_NOT_FOUND", "message": "bridge consult-pack script is missing"})
            if _resolve_node(normalized) is None:
                missing.append({"component": "bridge.node", "code": "NODE_NOT_FOUND", "message": "configured Node executable was not found"})
            else:
                components["bridge"] = {"state": "ready"}
    return RuntimeCompositionDiagnostic(
        workspace_root=root,
        config_path=path,
        config_loaded=path.is_file(),
        ready=not missing,
        components=components,
        missing=tuple(_bounded_missing(item) for item in missing),
    )


def _normalize_provider_factory_result(value: Any) -> tuple[ExecutorProvider, ...]:
    if isinstance(value, (str, bytes, bytearray)) or value is None:
        raise RuntimeCompositionError("RUNTIME_COMPOSITION_NOT_READY", "provider factory must return one or more providers")
    if isinstance(value, Sequence):
        providers = tuple(value)
    else:
        providers = (value,)
    try:
        discover_executors(providers)
    except (TypeError, ContractValidationError) as exc:
        raise RuntimeCompositionError("RUNTIME_COMPOSITION_NOT_READY", "provider factory returned invalid providers") from exc
    return providers


def build_workflow_orchestrator(
    workspace: str | os.PathLike[str],
    *,
    config: RuntimeCompositionConfig | Mapping[str, Any] | None = None,
    config_path: str | os.PathLike[str] | None = None,
    consultant_factory: Callable[[str, Path], Any] | None = None,
    bridge_runner_factory: Callable[..., Callable[..., Mapping[str, Any]]] | None = None,
    dependencies: WorkflowOrchestratorDependencies | None = None,
) -> WorkflowOrchestrator:
    """Build the optional Bootstrap orchestrator from explicit runtime config.

    This seam is deliberately separate from :func:`build_runtime_composition`:
    Bootstrap runs before a Stage exists, while the execution composition
    requires a validated Stage state/contract.  No consultant is invented
    when the project has not configured one; project context may be produced
    only by the existing deterministic local context builder.
    """

    root = _workspace_root(workspace)
    normalized = _normalize_config_input(root, config, config_path)
    missing: list[dict[str, Any]] = []

    selected_consultant_factory = consultant_factory
    if selected_consultant_factory is None:
        bridge_runner = _configured_bridge_runner(
            root,
            normalized,
            bridge_runner_factory=bridge_runner_factory,
        )
        if bridge_runner is None:
            missing.append(
                {
                    "component": "bootstrap.consultant",
                    "code": "CONSULTANT_REQUIRED",
                    "message": "a configured consultant factory or enabled bridge is required",
                }
            )
        else:
            bridge_root = _external_path(normalized.bridge_root, field="bridge.root") if normalized.bridge_root else None

            def _factory(phase: str, project_root: Path) -> ProjectScopedBridgeConsultant:
                return ProjectScopedBridgeConsultant(
                    project_root,
                    bridge_runner=bridge_runner,
                    bridge_root=bridge_root,
                )

            # ``ProjectScopedBridgeConsultant`` receives project_url at call
            # time; keep the factory free of URL/profile secrets.
            selected_consultant_factory = _factory
    if missing:
        raise RuntimeCompositionError(
            "RUNTIME_COMPOSITION_NOT_READY",
            "Bootstrap orchestrator is not ready",
            missing=tuple(missing),
            details={"config": normalized.bounded_view()},
        )
    assert selected_consultant_factory is not None
    try:
        return WorkflowOrchestrator(
            root,
            consultant_factory=selected_consultant_factory,
            dependencies=dependencies,
            auto_build_context=True,
        )
    except Exception as exc:  # noqa: BLE001 - bounded factory boundary
        if isinstance(exc, RuntimeCompositionError):
            raise
        raise RuntimeCompositionError(
            "RUNTIME_COMPOSITION_NOT_READY",
            "Bootstrap orchestrator could not be constructed",
            missing=(
                {"component": "bootstrap.orchestrator", "code": "FACTORY_INVALID", "message": "orchestrator construction failed"},
            ),
        ) from exc


def build_runtime_composition(
    workspace: str | os.PathLike[str],
    *,
    config: RuntimeCompositionConfig | Mapping[str, Any] | None = None,
    config_path: str | os.PathLike[str] | None = None,
    provider_factory: Callable[..., Any] | None = None,
    bridge_runner_factory: Callable[..., Callable[..., Mapping[str, Any]]] | None = None,
) -> RuntimeComposition:
    """Assemble the existing runtime chain, failing closed on any missing seam."""

    root = _workspace_root(workspace)
    normalized = _normalize_config_input(root, config, config_path)
    diagnostic = doctor_runtime_composition(root, config=normalized, provider_factory=provider_factory)
    if not diagnostic.ready:
        raise RuntimeCompositionError(
            "RUNTIME_COMPOSITION_NOT_READY",
            "default runtime composition is not ready",
            missing=diagnostic.missing,
            details={"diagnostic": diagnostic.bounded_view()},
        )
    state_path, contract_path = _stage_paths(root, normalized)
    try:
        if state_path.is_file():
            controller = StageController.from_state(state_path)
        else:
            raw_contract = json.loads(contract_path.read_text(encoding="utf-8"))
            controller = StageController(raw_contract, state_path=state_path)
        selected_stage_id = normalized.stage_id or controller.state.get("current_stage_id")
        if not isinstance(selected_stage_id, str) or not selected_stage_id:
            raise StageControllerError("no stage_id is available")
        stage = controller.show_stage(selected_stage_id)
        if Path(str(stage["contract"]["repository_root"])).expanduser().resolve() != root:
            raise RuntimeCompositionError(
                "RUNTIME_COMPOSITION_NOT_READY",
                "Stage contract repository_root does not match workspace",
                missing=(
                    {"component": "stage.identity", "code": "WORKSPACE_IDENTITY_MISMATCH", "message": "Stage contract repository_root does not match workspace"},
                ),
            )
    except RuntimeCompositionError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError, ValueError, ContractValidationError, KeyError, TypeError) as exc:
        raise RuntimeCompositionError(
            "RUNTIME_COMPOSITION_NOT_READY",
            "Stage state or contract could not be loaded",
            missing=(
                {"component": "stage", "code": "STATE_OR_CONTRACT_INVALID", "message": "Stage state or contract could not be loaded"},
            ),
        ) from exc

    bridge_runner = _configured_bridge_runner(
        root,
        normalized,
        bridge_runner_factory=bridge_runner_factory,
    )

    receipt_root = _workspace_path(root, normalized.receipt_root, field="stage.receipt_root")
    integration = StageIntegrationAdapter(
        controller,
        stage_id=selected_stage_id,
        repository_root=root,
        bridge_runner=bridge_runner,
        receipt_root=receipt_root,
    )

    if provider_factory is None:
        executable = _resolve_codex(normalized)
        if executable is None:
            raise RuntimeCompositionError("RUNTIME_COMPOSITION_NOT_READY", "Codex CLI was not found", missing=({"component": "executor.codex_cli", "code": "EXECUTABLE_NOT_FOUND", "message": "Codex CLI was not found by configured path or PATH"},))
        try:
            providers = (OpenAICodexExecutor(
                codex_executable=executable,
                workspace_root=str(root),
                preferred_model=normalized.preferred_model,
                fallback_model=normalized.fallback_model,
                auth_mode=normalized.auth_mode,
                timeout_seconds=normalized.timeout_seconds,
                artifact_dir=normalized.artifact_dir,
            ),)
        except OpenAICodexUnavailable as exc:
            raise RuntimeCompositionError("RUNTIME_COMPOSITION_NOT_READY", "native OpenAI Codex provider is unavailable", missing=({"component": "executor.openai-codex", "code": "EXECUTOR_UNAVAILABLE", "message": str(exc)[:512]},)) from exc
    else:
        try:
            # The injected seam has one explicit signature.  Do not retry a
            # factory after TypeError: a provider constructor may already have
            # performed bounded setup, and retrying would hide its real error.
            produced = provider_factory(normalized, root)
            providers = _normalize_provider_factory_result(produced)
        except RuntimeCompositionError:
            raise
        except Exception as exc:  # noqa: BLE001 - explicit composition seam
            raise RuntimeCompositionError("RUNTIME_COMPOSITION_NOT_READY", "executor provider factory failed", missing=({"component": "executor.providers", "code": "FACTORY_FAILED", "message": "configured provider factory failed"},)) from exc
    configured_ids = set(normalized.providers)
    descriptors = discover_executors(providers)
    missing_provider_ids = configured_ids - {item.provider_id for item in descriptors}
    unavailable = [item.provider_id for item in descriptors if not item.available]
    if missing_provider_ids:
        raise RuntimeCompositionError("RUNTIME_COMPOSITION_NOT_READY", "configured executor provider is missing", missing=tuple({"component": f"executor.{item}", "code": "PROVIDER_MISSING", "message": "configured provider was not returned"} for item in sorted(missing_provider_ids)))
    if unavailable:
        raise RuntimeCompositionError("RUNTIME_COMPOSITION_NOT_READY", "configured executor provider is unavailable", missing=tuple({"component": f"executor.{item}", "code": "PROVIDER_UNAVAILABLE", "message": "provider discovery reported unavailable"} for item in unavailable))

    try:
        runner = build_core_v1_runner_adapter(
            stage_integration=integration,
            providers=providers,
            preferred_provider=normalized.preferred_provider,
        )
    except CoreV1RunnerError as exc:
        raise RuntimeCompositionError("RUNTIME_COMPOSITION_NOT_READY", "Core V1 runner could not be constructed", missing=({"component": "core_v1_runner", "code": exc.code, "message": str(exc)[:512]},)) from exc
    final_components = dict(diagnostic.components)
    final_components["core_v1_runner"] = {"state": "ready", "provider_count": len(providers)}
    final_diagnostic = RuntimeCompositionDiagnostic(
        workspace_root=root,
        config_path=diagnostic.config_path,
        config_loaded=diagnostic.config_loaded,
        ready=True,
        components=final_components,
        missing=(),
        warnings=diagnostic.warnings,
    )
    return RuntimeComposition(
        workspace_root=root,
        config=normalized,
        controller=controller,
        stage_integration=integration,
        providers=providers,
        runner=runner,
        diagnostic=final_diagnostic,
    )


__all__ = [
    "DEFAULT_RUNTIME_CONFIG_RELATIVE",
    "DEFAULT_STAGE_RECEIPT_RELATIVE",
    "DEFAULT_STAGE_STATE_RELATIVE",
    "RUNTIME_COMPOSITION_SCHEMA_VERSION",
    "RUNTIME_CONFIG_ENV",
    "RuntimeComposition",
    "RuntimeCompositionConfig",
    "RuntimeCompositionDiagnostic",
    "RuntimeCompositionError",
    "build_runtime_composition",
    "build_workflow_orchestrator",
    "doctor_runtime_composition",
    "load_runtime_composition_config",
]
