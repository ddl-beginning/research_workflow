"""Bounded recovery owner for the Product's newline-delimited MCP launcher.

The Product MCP server is deliberately a small request handler.  This module
owns only the process boundary around that handler: it starts a fresh worker
for each JSON-RPC input, probes a replacement worker after an unexpected
close, and replays a request only when its semantics are read-only or carry an
explicit canonical command identity.  Workflow state, receipts, and command
idempotency remain owned by the Product runtime/controller.

An absent response after a worker close is intentionally not treated as
proof that the request was not delivered.  Requests without a replay-safe
identity therefore receive ``MCP_TRANSPORT_EFFECT_UNRESOLVED``.  Persistent
worker/health failures are bounded by the launcher's one recovery attempt and
surface ``TRANSIENT_TRANSPORT_RECOVERY_EXHAUSTED``.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TextIO


DEFAULT_MAX_RECOVERY_ATTEMPTS = 1
DEFAULT_WORKER_TIMEOUT_SECONDS = 300.0
HEALTH_PROBE_ID = "mcp-supervisor-health"


@dataclass(frozen=True)
class WorkerResult:
    """Bounded subprocess outcome used at the supervisor seam."""

    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    started: bool = True


@dataclass(frozen=True)
class _RequestIdentity:
    digest: str
    replay_safe: bool
    reason: str


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()[:16]


def _identity(message: Mapping[str, Any]) -> _RequestIdentity:
    """Derive a metadata-only operation identity and replay policy."""

    method = message.get("method")
    params = message.get("params")
    tool = None
    arguments: Mapping[str, Any] = {}
    if isinstance(params, Mapping):
        tool = params.get("name")
        raw_arguments = params.get("arguments", {})
        if isinstance(raw_arguments, Mapping):
            arguments = raw_arguments

    operation: Mapping[str, Any] | None = None
    if tool == "workflow_run" and isinstance(arguments.get("request"), Mapping):
        operation = arguments["request"]

    if operation is not None:
        identity_value: Any = {
            "operation": operation.get("operation"),
            "command_id": operation.get("command_id"),
            "operation_id": operation.get("operation_id"),
            "subject_id": operation.get("subject_id"),
            "planning_revision": operation.get("planning_revision"),
            "review_revision": operation.get("review_revision"),
        }
        if any(value is not None for value in identity_value.values()):
            digest = _digest(identity_value)
        else:
            digest = _digest(operation)
        operation_name = operation.get("operation")
        if operation_name == "DOCTOR":
            return _RequestIdentity(digest, True, "read_only_doctor")
        if operation_name == "COMMAND" and isinstance(operation.get("command_id"), str) and operation["command_id"].strip():
            return _RequestIdentity(digest, True, "canonical_command_id")
        return _RequestIdentity(digest, False, "external_effect_identity_not_replay_safe")

    if method == "tools/call" and tool in {"workflow_resume", "workflow_status"}:
        return _RequestIdentity(_digest({"tool": tool, "arguments": arguments}), True, "read_only_tool")
    if method in {"initialize", "tools/list"}:
        return _RequestIdentity(_digest({"method": method}), True, "protocol_probe")
    if method is not None:
        return _RequestIdentity(_digest({"method": method, "tool": tool}), False, "no_canonical_operation_id")
    return _RequestIdentity(_digest(message), False, "invalid_request_shape")


_EXPECTED_ID_UNSET = object()


def _response_lines(stdout: str, *, expected_id: Any = _EXPECTED_ID_UNSET) -> list[str]:
    """Accept only JSON-RPC response lines; never forward worker logs."""

    result: list[str] = []
    for line in stdout.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        try:
            message = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(message, Mapping)
            and message.get("jsonrpc") == "2.0"
            and "id" in message
            and (expected_id is _EXPECTED_ID_UNSET or message.get("id") == expected_id)
        ):
            result.append(candidate)
    return result


def _request_has_id(message: Mapping[str, Any]) -> bool:
    return "id" in message


def _transport_error(
    message: Mapping[str, Any],
    *,
    code: str,
    reason: str,
    recovery_attempts: int,
    health_probe: str,
    operation_digest: str,
) -> str:
    payload = {
        "schema_version": "workflow_mcp_error.v1",
        "error": {
            "code": code,
            "message": (
                "MCP transport recovery could not prove the request effect; "
                "inspect the canonical operation journal before retrying"
                if code == "MCP_TRANSPORT_EFFECT_UNRESOLVED"
                else "MCP transport recovery exhausted its bounded restart/health probe"
            ),
        },
        "details": {
            "operation_identity_digest": operation_digest,
            "recovery_attempts": recovery_attempts,
            "health_probe": health_probe,
            "reason": reason,
        },
    }
    if code == "TRANSIENT_TRANSPORT_RECOVERY_EXHAUSTED":
        payload["next_action"] = "BLOCKED"
    elif code == "MCP_TRANSPORT_NOT_DELIVERED_PROVEN":
        payload["next_action"] = "RETRY"
    return json.dumps(
        {"jsonrpc": "2.0", "id": message.get("id"), "result": {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, sort_keys=True)}],
            "structuredContent": payload,
            "isError": True,
        }},
        ensure_ascii=False,
        sort_keys=True,
    )


class MCPTransportSupervisor:
    """Run Product MCP workers with bounded, identity-aware recovery."""

    def __init__(
        self,
        worker_command: Sequence[str] | None = None,
        *,
        worker_cwd: str | os.PathLike[str] | None = None,
        worker_environment: Mapping[str, str] | None = None,
        max_recovery_attempts: int = DEFAULT_MAX_RECOVERY_ATTEMPTS,
        worker_timeout_seconds: float = DEFAULT_WORKER_TIMEOUT_SECONDS,
        invoke: Callable[[str], WorkerResult] | None = None,
    ) -> None:
        if isinstance(max_recovery_attempts, bool) or not isinstance(max_recovery_attempts, int) or not 0 <= max_recovery_attempts <= 3:
            raise ValueError("max_recovery_attempts must be between 0 and 3")
        if not isinstance(worker_timeout_seconds, (int, float)) or worker_timeout_seconds <= 0:
            raise ValueError("worker_timeout_seconds must be positive")
        if invoke is None and not worker_command:
            raise ValueError("worker_command or invoke is required")
        self.worker_command = tuple(worker_command or ())
        self.worker_cwd = None if worker_cwd is None else str(Path(worker_cwd).expanduser().resolve())
        self.worker_environment = dict(worker_environment or {})
        self.max_recovery_attempts = max_recovery_attempts
        self.worker_timeout_seconds = float(worker_timeout_seconds)
        self._invoke_hook = invoke

    def _invoke(self, line: str) -> WorkerResult:
        if self._invoke_hook is not None:
            result = self._invoke_hook(line)
            if not isinstance(result, WorkerResult):
                raise TypeError("invoke must return WorkerResult")
            return result
        environment = os.environ.copy()
        environment.update(self.worker_environment)
        try:
            completed = subprocess.run(
                list(self.worker_command),
                cwd=self.worker_cwd,
                input=line,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.worker_timeout_seconds,
                check=False,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            return WorkerResult(124, timed_out=True, stderr=type(exc).__name__)
        except OSError as exc:
            return WorkerResult(127, stderr=type(exc).__name__, started=False)
        return WorkerResult(completed.returncode, completed.stdout or "", completed.stderr or "")

    def _probe_health(self) -> bool:
        probe = json.dumps({"jsonrpc": "2.0", "id": HEALTH_PROBE_ID, "method": "initialize", "params": {}}, ensure_ascii=False) + "\n"
        result = self._invoke(probe)
        responses = _response_lines(result.stdout, expected_id=HEALTH_PROBE_ID)
        return result.returncode == 0 and any(HEALTH_PROBE_ID == json.loads(line).get("id") for line in responses)

    def _run_request(self, line: str, message: Mapping[str, Any], identity: _RequestIdentity) -> str | None:
        result = self._invoke(line)
        responses = (
            _response_lines(result.stdout, expected_id=message.get("id"))
            if _request_has_id(message)
            else []
        )
        # A flushed authoritative response wins even if a worker exits while
        # closing its pipes.  Never retry after a response was observed.
        if responses:
            return responses[0]
        if result.returncode == 0 and not _request_has_id(message):
            return None

        recovery_attempts = 0
        health_probe = "NOT_RUN"
        replay_attempted = False
        while recovery_attempts < self.max_recovery_attempts:
            recovery_attempts += 1
            health_probe = "PASS" if self._probe_health() else "FAIL"
            if health_probe != "PASS":
                continue
            # A failed process start proves that the original request never
            # reached a worker, so even an otherwise effectful operation may
            # be sent once after the replacement worker is healthy.  A worker
            # that did start leaves delivery/effect unknown and is never
            # blindly replayed without canonical identity.
            if not identity.replay_safe and result.started:
                return _transport_error(
                    message,
                    code="MCP_TRANSPORT_EFFECT_UNRESOLVED",
                    reason=identity.reason,
                    recovery_attempts=recovery_attempts,
                    health_probe=health_probe,
                    operation_digest=identity.digest,
                ) if _request_has_id(message) else None
            replay_attempted = True
            replay = self._invoke(line)
            replay_responses = (
                _response_lines(replay.stdout, expected_id=message.get("id"))
                if _request_has_id(message)
                else []
            )
            if replay_responses:
                return replay_responses[0]
            if replay.returncode == 0 and not _request_has_id(message):
                return None

        if not _request_has_id(message):
            return None
        if not identity.replay_safe and (replay_attempted or result.started):
            return _transport_error(
                message,
                code="MCP_TRANSPORT_EFFECT_UNRESOLVED",
                reason="replay_response_missing" if replay_attempted else "worker_close_before_authoritative_response",
                recovery_attempts=recovery_attempts,
                health_probe=health_probe,
                operation_digest=identity.digest,
            )
        if not result.started:
            return _transport_error(
                message,
                code="MCP_TRANSPORT_NOT_DELIVERED_PROVEN",
                reason="worker_process_not_started",
                recovery_attempts=recovery_attempts,
                health_probe=health_probe,
                operation_digest=identity.digest,
            )
        return _transport_error(
            message,
            code="TRANSIENT_TRANSPORT_RECOVERY_EXHAUSTED",
            reason="worker_close_or_missing_response",
            recovery_attempts=recovery_attempts,
            health_probe=health_probe,
            operation_digest=identity.digest,
        )

    def serve_stdio(self, reader: TextIO, writer: TextIO) -> None:
        """Forward one request at a time while keeping the client wire clean."""

        for raw_line in reader:
            if not raw_line.strip():
                continue
            try:
                message = json.loads(raw_line)
            except json.JSONDecodeError:
                message = {"jsonrpc": "2.0", "id": None, "method": "<invalid-json>"}
            if not isinstance(message, Mapping):
                message = {"jsonrpc": "2.0", "id": None, "method": "<invalid-request>"}
            identity = _identity(message)
            response = self._run_request(raw_line, message, identity)
            if response is not None:
                try:
                    writer.write(response + "\n")
                    writer.flush()
                except BrokenPipeError:
                    return


__all__ = [
    "DEFAULT_MAX_RECOVERY_ATTEMPTS",
    "DEFAULT_WORKER_TIMEOUT_SECONDS",
    "MCPTransportSupervisor",
    "WorkerResult",
]
