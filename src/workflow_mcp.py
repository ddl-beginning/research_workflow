"""Local STDIO MCP façade for the thin :mod:`src.workflow_runtime` seam.

The façade only exposes five high-level tools.  It does not implement Stage
lifecycle, GPT transport, provider selection, or filesystem execution.  A
caller embeds an existing Core V1 runner into :class:`WorkflowMCPServer`; the
``workflow_run`` tool delegates to that runner unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO

from .workflow_core_adapter import CoreV1RunnerAdapter, CoreV1RunnerError
from .workflow_runtime import WorkflowRuntime, WorkflowRuntimeError
from .runtime_composition import (
    RuntimeComposition,
    RuntimeCompositionError,
    build_runtime_composition,
    build_workflow_orchestrator,
)


MCP_PROTOCOL_VERSION = "2024-11-05"
MCP_SERVER_NAME = "research-supervisor-workflow"
MCP_SERVER_VERSION = "0.1.0"
# Keep this short and self-contained: MCP clients may display only the first
# part of initialize.instructions.  The order is intentional and mirrors the
# fail-closed runtime protocol.
MCP_INITIALIZE_INSTRUCTIONS = (
    "Use workflow_resume first. Only on WORKFLOW_NOT_FOUND call workflow_start. "
    "Follow next_action/next_tool and ask at most one requirement question. "
    "Use workflow_answer for the current question, bounded updates, approval, or design review. "
    "DESIGN_ACCEPT means Step 15 is READY, not PLANNED; workflow_run uses Core V1 for legacy config or the sole V2 StageController for explicit v2 config, "
    "and PLANNED requires validated Stage evidence. Do not bypass Core or mutate state directly."
)


def _workspace_schema() -> dict[str, Any]:
    return {
        "type": "string",
        "description": "Existing local workspace directory; must be supplied or configured on the server.",
    }


def _tool_definitions() -> list[dict[str, Any]]:
    workspace = _workspace_schema()
    return [
        {
            "name": "workflow_start",
            "description": "Start or revise deterministic project requirements intake.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "workspace": workspace,
                    "mode": {"type": "string", "enum": ["USER_CONFIRMED_BRIEF", "CODEX_REQUIREMENTS_INTERVIEW"]},
                    "rough_requirement": {"type": "string"},
                    "raw_requirement": {"type": "string"},
                    "original_requirement": {"type": "string"},
                    "user_requirement": {"type": "string"},
                    "brief": {"type": "object"},
                    "problem": {"type": "string"},
                    "problem_statement": {"type": "string"},
                    "goal": {"type": "string"},
                    "desired_outcome": {"type": "string"},
                    "project_goal": {"type": "string"},
                    "observable_outcome": {"type": "string"},
                    "input": {"type": ["string", "array", "object"]},
                    "inputs": {"type": ["string", "array", "object"]},
                    "expected_output": {"type": ["string", "array", "object"]},
                    "expected_outputs": {"type": ["string", "array", "object"]},
                    "output": {"type": ["string", "array", "object"]},
                    "outputs": {"type": ["string", "array", "object"]},
                    "scope": {"type": ["array", "string"]},
                    "non_goals": {"type": ["array", "string"]},
                    "constraints": {"type": ["array", "string"]},
                    "available_assets": {"type": ["array", "object"]},
                    "success_criteria": {"type": ["array", "string"]},
                    "acceptance_criteria": {"type": ["array", "string"]},
                    "acceptance": {"type": ["array", "string"]},
                    "human_preferences": {"type": ["array", "string"]},
                    "preferences": {"type": ["array", "string"]},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "workflow_answer",
            "description": "Answer the one current intake question, apply a bounded update, or explicitly approve the brief.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "workspace": workspace,
                    "question_id": {"type": "string"},
                    "answer": {"type": ["string", "object"]},
                    "update": {"type": "object"},
                    "approve": {"type": "boolean"},
                    "mode": {"type": "string", "enum": ["ANSWER", "UPDATE", "APPROVE", "DESIGN_SUBMIT", "DESIGN_ACCEPT", "DESIGN_FEEDBACK", "DESIGN_REVIEW"]},
                    "feedback": {"type": ["string", "object"]},
                    "design_summary": {"type": "object"},
                    "trigger": {"type": "string", "enum": ["INITIAL_ARCHITECTURE", "MAJOR_REPLAN"]},
                    "consultation_ref": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "workflow_run",
            "description": "Delegate one approved workflow run to the configured existing Core V1 runner.",
            "inputSchema": {
                "type": "object",
                "required": ["request"],
                "properties": {
                    "workspace": workspace,
                    "request": {"type": "object"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "workflow_status",
            "description": "Read the project brief and runtime checkpoint without mutating workflow state.",
            "inputSchema": {
                "type": "object",
                "properties": {"workspace": workspace},
                "additionalProperties": False,
            },
        },
        {
            "name": "workflow_resume",
            "description": "Rehydrate the runtime from the project-local brief and checkpoint.",
            "inputSchema": {
                "type": "object",
                "properties": {"workspace": workspace},
                "additionalProperties": False,
            },
        },
    ]


TOOL_DEFINITIONS = tuple(_tool_definitions())
TOOL_NAMES = frozenset(item["name"] for item in TOOL_DEFINITIONS)
TRANSPORT_TRACE_SCHEMA_VERSION = "workflow_mcp_transport_trace.v1"


def _bounded_trace_text(value: Any, *, limit: int = 80) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:limit]


def _trace_id(identifier: Any, *, present: bool) -> str | None:
    if not present:
        return None
    try:
        encoded = json.dumps(identifier, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        encoded = type(identifier).__name__
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _trace_message_fields(message: Any) -> dict[str, Any]:
    if not isinstance(message, Mapping):
        return {"method": "<non-object>", "id_present": False}
    method = message.get("method")
    fields: dict[str, Any] = {
        "method": _bounded_trace_text(method) or "<invalid>",
        "id_present": "id" in message,
    }
    if "id" in message:
        fields["id_digest"] = _trace_id(message.get("id"), present=True)
    if method == "tools/call" and isinstance(message.get("params"), Mapping):
        tool_name = message["params"].get("name")
        bounded_tool = _bounded_trace_text(tool_name)
        if bounded_tool is not None:
            fields["tool"] = bounded_tool
    return fields


def _trace_response_fields(response: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {"response_present": response is not None}
    if not isinstance(response, Mapping):
        return fields
    result = response.get("result")
    if isinstance(result, Mapping):
        fields["is_error"] = bool(result.get("isError", False))
        structured = result.get("structuredContent")
        if isinstance(structured, Mapping):
            error = structured.get("error")
            if isinstance(error, Mapping):
                code = _bounded_trace_text(error.get("code"))
                if code is not None:
                    fields["error_code"] = code
    error = response.get("error")
    if isinstance(error, Mapping):
        fields["rpc_error_code"] = error.get("code")
    return fields


class _TransportTrace:
    """Best-effort, metadata-only trace for the launcher transport boundary."""

    def __init__(self, path: str | os.PathLike[str] | None) -> None:
        self._handle: TextIO | None = None
        if path is None:
            return
        candidate = Path(path)
        if not candidate.is_absolute():
            raise ValueError("transport trace path must be absolute")
        if not candidate.parent.is_dir():
            raise ValueError("transport trace parent directory must already exist")
        try:
            self._handle = candidate.open("a", encoding="utf-8", newline="\n")
        except OSError as exc:
            raise ValueError("transport trace path is not writable") from exc
        self.emit("process_start", pid=os.getpid())

    @property
    def enabled(self) -> bool:
        return self._handle is not None

    def emit(self, event: str, **fields: Any) -> None:
        handle = self._handle
        if handle is None:
            return
        payload: dict[str, Any] = {
            "schema_version": TRANSPORT_TRACE_SCHEMA_VERSION,
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
        }
        payload.update(fields)
        try:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
        except (OSError, TypeError, ValueError):
            # Diagnostics must never change or contaminate the JSON-RPC wire.
            try:
                handle.close()
            except OSError:
                pass
            self._handle = None

    def close(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            handle.close()
        except OSError:
            pass


class WorkflowMCPServer:
    """Minimal JSON-RPC/STDIO MCP server around :class:`WorkflowRuntime`."""

    def __init__(
        self,
        *,
        runner: Callable[[Mapping[str, Any]], Mapping[str, Any]] | Any | None = None,
        core_runner: CoreV1RunnerAdapter | None = None,
        core_runner_factory: Callable[[str], CoreV1RunnerAdapter] | None = None,
        default_workspace: str | None = None,
        checkpoint_path: str | None = None,
        runtime_factory: Callable[..., WorkflowRuntime] = WorkflowRuntime,
        composition_factory: Callable[[str], RuntimeComposition | CoreV1RunnerAdapter] | None = None,
        orchestrator_factory: Callable[[str], Any] | None = None,
        transport_trace_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self._transport_trace = _TransportTrace(transport_trace_path)
        self.runner = runner
        if core_runner is not None and not isinstance(core_runner, CoreV1RunnerAdapter):
            raise WorkflowRuntimeError("RUNNER_INVALID", "core_runner must be a CoreV1RunnerAdapter")
        if core_runner_factory is not None and not callable(core_runner_factory):
            raise WorkflowRuntimeError("RUNNER_INVALID", "core_runner_factory must be callable")
        self.core_runner = core_runner
        self.core_runner_factory = core_runner_factory
        self.default_workspace = default_workspace
        self.checkpoint_path = checkpoint_path
        self.runtime_factory = runtime_factory
        if composition_factory is not None and not callable(composition_factory):
            raise WorkflowRuntimeError("RUNNER_INVALID", "composition_factory must be callable")
        self.composition_factory = composition_factory
        if orchestrator_factory is not None and not callable(orchestrator_factory):
            raise WorkflowRuntimeError("ORCHESTRATOR_INVALID", "orchestrator_factory must be callable")
        self.orchestrator_factory = orchestrator_factory

    def _default_orchestrator_enabled(self) -> bool:
        return (
            self.orchestrator_factory is not None
            or (
                self.runner is None
                and self.core_runner is None
                and self.core_runner_factory is None
                and self.composition_factory is None
            )
        )

    def _configured_orchestrator_factory(self) -> Callable[[str], Any] | None:
        if self.orchestrator_factory is not None:
            return self.orchestrator_factory
        if not self._default_orchestrator_enabled():
            return None
        return build_workflow_orchestrator

    def _runtime(self, arguments: Mapping[str, Any], *, runner: Any | None = None) -> WorkflowRuntime:
        workspace = arguments.get("workspace", arguments.get("workspace_root", self.default_workspace))
        if not isinstance(workspace, str) or not workspace.strip():
            raise WorkflowRuntimeError("WORKSPACE_REQUIRED", "workspace must be supplied or configured")
        # Explicit product composition selects the frozen V2 command adapter.
        # Keep injected legacy/test runtimes and the default v1 path unchanged.
        if (self.runtime_factory is WorkflowRuntime and self.runner is None
                and self.core_runner is None and self.core_runner_factory is None
                and self.composition_factory is None and self.orchestrator_factory is None):
            from .runtime_composition import load_runtime_composition_config
            try:
                config = load_runtime_composition_config(workspace)
            except RuntimeCompositionError as exc:
                raise WorkflowRuntimeError(exc.code, str(exc), details=exc.bounded_view()) from exc
            if config.lifecycle_version == "v2":
                from .product_workflow_runtime import ProductWorkflowRuntime
                return ProductWorkflowRuntime(workspace, config=config)
        kwargs: dict[str, Any] = {"runner": self.runner if runner is None else runner}
        if self.checkpoint_path is not None:
            kwargs["checkpoint_path"] = self.checkpoint_path
        orchestrator_factory = self._configured_orchestrator_factory()
        if orchestrator_factory is not None:
            kwargs["orchestrator_factory"] = lambda root: orchestrator_factory(str(root))
        return self.runtime_factory(workspace, **kwargs)

    def _configured_core_runner(self, arguments: Mapping[str, Any]) -> CoreV1RunnerAdapter | None:
        """Resolve an explicit adapter or the default runtime composition."""

        workspace = arguments.get("workspace", arguments.get("workspace_root", self.default_workspace))
        if not isinstance(workspace, str) or not workspace.strip():
            raise WorkflowRuntimeError("WORKSPACE_REQUIRED", "workspace must be supplied or configured")
        if self.core_runner is not None:
            try:
                if self.core_runner.repository_root != Path(workspace).expanduser().resolve():
                    raise WorkflowRuntimeError("WORKSPACE_IDENTITY_MISMATCH", "Core V1 runner workspace does not match request")
            except (OSError, RuntimeError, ValueError) as exc:
                raise WorkflowRuntimeError("WORKSPACE_IDENTITY_MISMATCH", "Core V1 runner workspace is invalid") from exc
            return self.core_runner
        if self.core_runner_factory is not None:
            try:
                candidate = self.core_runner_factory(workspace)
            except CoreV1RunnerError as exc:
                raise WorkflowRuntimeError(exc.code, str(exc)) from exc
            except Exception as exc:  # noqa: BLE001 - explicit host factory boundary
                raise WorkflowRuntimeError("RUNNER_INVALID", "Core V1 runner factory failed") from exc
            if not isinstance(candidate, CoreV1RunnerAdapter):
                raise WorkflowRuntimeError("RUNNER_INVALID", "Core V1 runner factory must return CoreV1RunnerAdapter")
        else:
            try:
                composed = (
                    self.composition_factory(workspace)
                    if self.composition_factory is not None
                    else build_runtime_composition(workspace)
                )
            except RuntimeCompositionError as exc:
                raise WorkflowRuntimeError(
                    "RUNNER_NOT_CONFIGURED",
                    "default runtime composition is unavailable",
                    details={"composition": exc.bounded_view()},
                ) from exc
            except Exception as exc:  # noqa: BLE001 - composition boundary
                raise WorkflowRuntimeError("RUNNER_NOT_CONFIGURED", "default runtime composition failed") from exc
            if isinstance(composed, RuntimeComposition):
                candidate = composed.runner
            elif isinstance(composed, CoreV1RunnerAdapter):
                candidate = composed
            else:
                raise WorkflowRuntimeError("RUNNER_INVALID", "runtime composition must return CoreV1RunnerAdapter")
        try:
            if candidate.repository_root != Path(workspace).expanduser().resolve():
                raise WorkflowRuntimeError("WORKSPACE_IDENTITY_MISMATCH", "Core V1 runner workspace does not match request")
        except (OSError, RuntimeError, ValueError) as exc:
            raise WorkflowRuntimeError("WORKSPACE_IDENTITY_MISMATCH", "Core V1 runner workspace is invalid") from exc
        return candidate

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip():
            raise WorkflowRuntimeError("TOOL_INVALID", "tool name must be non-empty")
        if name not in TOOL_NAMES:
            raise WorkflowRuntimeError("TOOL_NOT_FOUND", f"unknown workflow tool: {name}")
        args = dict(arguments or {})
        runner = self.runner
        runtime = self._runtime(args, runner=runner)
        if name == "workflow_run" and getattr(runtime, "lifecycle_version", None) == "v2":
            return runtime.run(args.get("request"))
        if name == "workflow_run" and runner is None:
            # Rehydrate/validate the persisted workflow before discovering a
            # runtime composition.  A missing workflow must report the stable
            # WORKFLOW_NOT_FOUND protocol error, even when the optional Core
            # provider is not configured on this host.  The default bootstrap
            # seam runs before a Stage composition exists; only a planned
            # Stage falls through to the execution composition.
            current = runtime.status()
            phase = current.get("phase")
            if phase in {"INTAKE", "WAITING_USER_APPROVAL", "AWAITING_DESIGN_REVIEW"}:
                # Let WorkflowRuntime return the stable lifecycle gate rather
                # than constructing an execution composition prematurely.
                return runtime.run(request)
            if not (self._configured_orchestrator_factory() is not None and phase in {"READY", "DESIGN_ACCEPTED"}):
                runner = self._configured_core_runner(args)
                runtime = self._runtime(args, runner=runner)
        if name == "workflow_start":
            return runtime.start(**args)
        if name == "workflow_answer":
            return runtime.answer(
                answer=args.get("answer"),
                question_id=args.get("question_id"),
                update=args.get("update"),
                approve=args.get("approve", False),
                mode=args.get("mode"),
                feedback=args.get("feedback"),
                design_summary=args.get("design_summary"),
                trigger=args.get("trigger"),
                consultation_ref=args.get("consultation_ref"),
            )
        if name == "workflow_run":
            request = args.get("request")
            if not isinstance(request, Mapping):
                raise WorkflowRuntimeError("RUN_REQUEST_INVALID", "workflow_run requires a request object")
            return runtime.run(request)
        if name == "workflow_status":
            return runtime.status()
        if name == "workflow_resume":
            return runtime.resume()

    @staticmethod
    def _success(identifier: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
        value = deepcopy(dict(payload))
        return {
            "jsonrpc": "2.0",
            "id": identifier,
            "result": {
                "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, sort_keys=True)}],
                "structuredContent": value,
                "isError": False,
            },
        }

    @staticmethod
    def _rpc_success(identifier: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": identifier, "result": deepcopy(dict(payload))}

    @staticmethod
    def _tool_error(identifier: Any, error: WorkflowRuntimeError) -> dict[str, Any]:
        payload = {
            "schema_version": "workflow_mcp_error.v1",
            "error": {"code": error.code, "message": str(error)},
        }
        if getattr(error, "details", None):
            payload["details"] = deepcopy(dict(error.details))
        if error.code == "RUNNER_NOT_CONFIGURED":
            payload["next_action"] = "BLOCKED"
        elif error.code in {"DESIGN_REVIEW_REQUIRED", "DESIGN_REVIEW_NOT_PENDING", "DESIGN_REVIEW_RECONSULT_REQUIRED"}:
            payload["next_action"] = "REQUEST_DESIGN_REVIEW"
        return {
            "jsonrpc": "2.0",
            "id": identifier,
            "result": {
                "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, sort_keys=True)}],
                "structuredContent": payload,
                "isError": True,
            },
        }

    @staticmethod
    def _rpc_error(identifier: Any, code: int, message: str, *, data: Mapping[str, Any] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            body["data"] = deepcopy(dict(data))
        return {"jsonrpc": "2.0", "id": identifier, "error": body}

    def handle_message(self, message: Mapping[str, Any]) -> dict[str, Any] | None:
        if not isinstance(message, Mapping) or message.get("jsonrpc") != "2.0":
            return self._rpc_error(message.get("id") if isinstance(message, Mapping) else None, -32600, "invalid JSON-RPC request")
        identifier_present = "id" in message
        identifier = message.get("id")
        method = message.get("method")
        params = message.get("params", {})
        # JSON-RPC notifications have no id and never receive a response.
        # This includes MCP lifecycle/cancellation notifications as well as
        # unknown notification methods.  A request carrying an explicit id
        # still receives the normal method-not-found response below.
        if not identifier_present:
            return None
        if not isinstance(method, str):
            return self._rpc_error(identifier, -32600, "method must be a string")
        if method == "initialize":
            return self._rpc_success(
                identifier,
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": MCP_SERVER_NAME, "version": MCP_SERVER_VERSION},
                    "instructions": MCP_INITIALIZE_INSTRUCTIONS,
                },
            )
        if method == "tools/list":
            return self._rpc_success(identifier, {"tools": deepcopy(list(TOOL_DEFINITIONS))})
        if method != "tools/call":
            return self._rpc_error(identifier, -32601, "method not found")
        if not isinstance(params, Mapping):
            return self._rpc_error(identifier, -32602, "tools/call params must be an object")
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or not isinstance(arguments, Mapping):
            return self._rpc_error(identifier, -32602, "tools/call requires name and object arguments")
        try:
            return self._success(identifier, self.call_tool(name, arguments))
        except WorkflowRuntimeError as exc:
            return self._tool_error(identifier, exc)
        except Exception:
            return self._tool_error(identifier, WorkflowRuntimeError("MCP_INTERNAL_ERROR", "workflow tool failed"))

    def serve_stdio(self, reader: TextIO | None = None, writer: TextIO | None = None) -> None:
        """Serve newline-delimited JSON-RPC without writing diagnostics to stdout."""

        source = reader or sys.stdin
        target = writer or sys.stdout
        status = "completed"
        try:
            for line in source:
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    trace_fields = {"method": "<invalid-json>", "id_present": False}
                    self._transport_trace.emit("input_received", **trace_fields)
                    self._transport_trace.emit("handler_started", **trace_fields)
                    response = self._rpc_error(None, -32700, "invalid JSON")
                    self._transport_trace.emit(
                        "handler_completed",
                        **trace_fields,
                        elapsed_ms=0,
                        **_trace_response_fields(response),
                    )
                else:
                    trace_fields = _trace_message_fields(message)
                    self._transport_trace.emit("input_received", **trace_fields)
                    started = time.monotonic()
                    self._transport_trace.emit("handler_started", **trace_fields)
                    try:
                        response = self.handle_message(message)
                    except Exception as exc:  # noqa: BLE001 - trace then preserve failure semantics
                        self._transport_trace.emit(
                            "exception",
                            stage="handler",
                            exception_type=type(exc).__name__,
                            **trace_fields,
                        )
                        status = "exception"
                        raise
                    self._transport_trace.emit(
                        "handler_completed",
                        **trace_fields,
                        elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
                        **_trace_response_fields(response),
                    )
                    if response is None and not trace_fields["id_present"]:
                        self._transport_trace.emit("notification_ignored", reason="jsonrpc_notification", **trace_fields)
                if response is None:
                    continue
                serialized = json.dumps(response, ensure_ascii=False, sort_keys=True) + "\n"
                response_bytes = len(serialized.encode("utf-8"))
                self._transport_trace.emit(
                    "response_write",
                    response_bytes=response_bytes,
                    id_digest=trace_fields.get("id_digest"),
                )
                try:
                    target.write(serialized)
                    target.flush()
                except BrokenPipeError:
                    status = "broken_pipe"
                    self._transport_trace.emit(
                        "broken_pipe",
                        stage="response_flush",
                        exception_type="BrokenPipeError",
                        id_digest=trace_fields.get("id_digest"),
                    )
                    return
                self._transport_trace.emit(
                    "response_flushed",
                    response_bytes=response_bytes,
                    id_digest=trace_fields.get("id_digest"),
                )
            self._transport_trace.emit("eof")
        except BrokenPipeError:
            status = "broken_pipe"
            self._transport_trace.emit("broken_pipe", stage="serve_stdio", exception_type="BrokenPipeError")
        except Exception as exc:  # noqa: BLE001 - preserve existing process failure semantics
            status = "exception"
            self._transport_trace.emit("exception", stage="serve_stdio", exception_type=type(exc).__name__)
            raise
        finally:
            self._transport_trace.emit("process_exit", status=status)
            self._transport_trace.close()


__all__ = [
    "MCP_PROTOCOL_VERSION",
    "MCP_SERVER_NAME",
    "MCP_SERVER_VERSION",
    "MCP_INITIALIZE_INSTRUCTIONS",
    "TOOL_DEFINITIONS",
    "WorkflowMCPServer",
]
