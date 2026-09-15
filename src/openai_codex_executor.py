"""Native OpenAI Codex CLI executor.

This module is a deliberately small provider implementation for the existing
``ExecutionRequest -> ExecutionResult`` seam.  It does not know about
``StageController`` and it never mutates workflow state.  The default auth
path is the saved ChatGPT Codex session; an API key is never read or supplied
by this provider.

The CLI is used for both bounded model discovery (``codex debug models``) and
turns (``codex exec --json``).  Raw prompts, responses, tokens, cookies, and
auth files are intentionally excluded from evidence.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .contracts import ContractValidationError, path_is_allowed
from .executor import ExecutionRequest, ExecutionResult, ExecutorDescriptor, validate_execution_result
from .execution_receipt_contract import (
    build_execution_receipt_contract,
    build_runner_execution_receipt,
    persist_execution_receipt,
)
from .executor_model_routing import RoutingDecision, derive_routing_decision


class OpenAICodexUnavailable(ContractValidationError):
    """The configured native Codex runtime cannot be used safely."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class RuntimeSnapshot:
    executable: str | None
    version: str | None
    auth_mode: str | None
    authenticated: bool
    models: tuple[dict[str, Any], ...]
    sdk_available: bool
    available: bool
    reason: str

    def bounded_view(self) -> dict[str, Any]:
        return {
            "executable_present": self.executable is not None,
            "version": self.version,
            "auth_mode": self.auth_mode,
            "authenticated": self.authenticated,
            "models": [
                {
                    "id": item.get("id"),
                    "model": item.get("model"),
                    "display_name": item.get("display_name"),
                    "hidden": item.get("hidden"),
                    "is_default": item.get("is_default"),
                }
                for item in self.models
            ],
            "model_count": len(self.models),
            "sdk_available": self.sdk_available,
            "available": self.available,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ExecObservation:
    terminal_status: str
    thread_seen: bool
    turn_seen: bool
    event_counts: Mapping[str, int]
    tests: tuple[dict[str, str], ...]
    required_test_seen: bool = False
    required_test_passed: bool = False
    event_summaries: tuple[dict[str, Any], ...] = ()


def _safe_text(value: Any, field: str, maximum: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(f"{field} must be a non-empty string")
    value = value.strip()
    if len(value) > maximum or "\x00" in value:
        raise ContractValidationError(f"{field} is invalid or too long")
    return value


def _run_subprocess(
    argv: Sequence[str],
    *,
    cwd: str | None = None,
    input_text: str | None = None,
    timeout: float = 30.0,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    try:
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            input=input_text,
            text=True,
            # Codex emits UTF-8 even on Windows, where the process locale can
            # otherwise default to GBK.  Replacement keeps diagnostics and
            # catalog discovery bounded/fail-closed instead of raising a
            # UnicodeDecodeError before the provider can report its status.
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            env=dict(env) if env is not None else None,
            check=False,
        )
    except FileNotFoundError:
        return CommandResult(127, "", "executable not found")
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", "command timed out")
    return CommandResult(completed.returncode, completed.stdout or "", completed.stderr or "")


def parse_auth_status(stdout: str, stderr: str = "", returncode: int = 0) -> str | None:
    """Parse ``codex login status`` without retaining account details."""

    text = f"{stdout}\n{stderr}".lower()
    if "chatgpt" in text and ("logged in" in text or "authenticated" in text or returncode == 0):
        return "chatgpt"
    if "api key" in text or "apikey" in text or "api_key" in text:
        return "api_key"
    if "not logged" in text or "not authenticated" in text or "logged out" in text:
        return None
    return None


def parse_model_catalog(payload: Any) -> tuple[dict[str, Any], ...]:
    """Normalize an official Codex model catalog into bounded model records.

    ``codex debug models`` currently emits a top-level ``models`` array whose
    stable identifier is ``slug``.  The older app-server-shaped ``data`` array
    is accepted as a compatibility input for injected/offline providers, but
    this parser deliberately copies only bounded identity and capability
    fields.  In particular, catalog ``base_instructions`` and descriptions are
    never retained in provider state or evidence.
    """

    value = payload
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ContractValidationError("model/list response is not valid JSON") from exc
    if isinstance(value, Mapping) and isinstance(value.get("result"), Mapping):
        value = value["result"]
    if isinstance(value, Mapping):
        value = value.get("data", value.get("models", []))
    if not isinstance(value, list):
        raise ContractValidationError("model/list response must contain a data array")
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ContractValidationError(f"model/list data[{index}] must be an object")
        model_id = item.get("id") or item.get("model") or item.get("slug")
        if not isinstance(model_id, str) or not model_id.strip():
            raise ContractValidationError(f"model/list data[{index}] has no model id")
        model_id = model_id.strip()
        if len(model_id) > 256 or "\x00" in model_id:
            raise ContractValidationError(f"model/list data[{index}] has an invalid model id")
        if model_id in seen:
            continue
        seen.add(model_id)
        display = item.get("displayName", item.get("display_name", model_id))
        if not isinstance(display, str) or not display.strip():
            display = model_id
        display = display.strip()
        if len(display) > 256 or "\x00" in display:
            display = model_id
        model_name = item.get("model") or model_id
        if not isinstance(model_name, str) or not model_name.strip() or len(model_name) > 256 or "\x00" in model_name:
            model_name = model_id
        hidden_value = item.get("hidden")
        if hidden_value is None and "visibility" in item:
            hidden_value = str(item.get("visibility", "")).lower() not in {"list", "visible", "public"}

        reasoning = item.get("supportedReasoningEfforts", item.get("supported_reasoning_levels", []))
        bounded_reasoning: list[str] = []
        if isinstance(reasoning, list):
            for entry in reasoning:
                effort = entry if isinstance(entry, str) else entry.get("effort") if isinstance(entry, Mapping) else None
                if isinstance(effort, str) and effort.strip() and len(effort.strip()) <= 64 and "\x00" not in effort:
                    if effort.strip() not in bounded_reasoning:
                        bounded_reasoning.append(effort.strip())
        models.append(
            {
                "id": model_id,
                "model": model_name.strip(),
                "display_name": display,
                "hidden": bool(hidden_value or False),
                "is_default": bool(item.get("isDefault", item.get("is_default", False))),
                "supported_reasoning_efforts": bounded_reasoning,
            }
        )
    return tuple(models)


def _norm_model_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def select_available_model(
    models: Sequence[Mapping[str, Any]],
    preference: str,
    *,
    fallback: str | None = None,
) -> str:
    """Resolve an explicit preference against the observed model catalog."""

    wanted = _safe_text(preference, "model_preference", 128)
    normalized = _norm_model_token(wanted)

    def match(target: str) -> str | None:
        target_norm = _norm_model_token(target)
        for item in models:
            values = [item.get("id"), item.get("model"), item.get("display_name")]
            if any(isinstance(value, str) and target_norm == _norm_model_token(value) for value in values):
                return str(item.get("id") or item.get("model"))
        for item in models:
            values = [item.get("id"), item.get("model"), item.get("display_name")]
            if any(isinstance(value, str) and target_norm and target_norm in _norm_model_token(value) for value in values):
                return str(item.get("id") or item.get("model"))
        return None

    selected = match(normalized)
    if selected is None:
        # Some Codex CLI catalogs expose an account-scoped id and a generic
        # model field while retaining a stable family display name (for
        # example ``acct-luna`` / ``Luna``).  Treat that observed family name
        # as an alias of the canonical route; this is catalog resolution, not
        # a fallback to another route.
        for family in ("luna", "astra"):
            if family not in normalized:
                continue
            selected = match(family)
            if selected:
                break
    if selected:
        return selected
    if fallback is not None:
        checked_fallback = _safe_text(fallback, "fallback_model", 128)
        selected = match(checked_fallback)
        if selected:
            return selected
    available = [str(item.get("id") or item.get("model")) for item in models]
    suffix = f"; explicit fallback {fallback!r} was also unavailable" if fallback else "; no explicit fallback configured"
    raise OpenAICodexUnavailable(
        f"requested Codex model {wanted!r} is unavailable; observed models={available!r}{suffix}"
    )


def parse_exec_jsonl(stdout: str, *, required_test: str | None = None) -> ExecObservation:
    """Extract bounded execution facts from ``codex exec --json``.

    Raw JSONL is intentionally not retained.  A caller may provide one
    required test command; the parser records only whether a matching
    ``command_execution`` completed with exit code zero.  Event summaries are
    deliberately metadata-only so they can be persisted as review evidence.
    """

    if not isinstance(stdout, str):
        raise ContractValidationError("Codex JSONL output must be text")
    counts: dict[str, int] = {}
    thread_seen = False
    turn_seen = False
    terminal = "failed"
    tests: list[dict[str, str]] = []
    required_norm = _normalise_command_text(required_test) if isinstance(required_test, str) and required_test.strip() else ""
    required_test_seen = False
    required_test_passed = False
    event_summaries: list[dict[str, Any]] = []
    for index, raw in enumerate(stdout.splitlines()):
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, Mapping):
            continue
        kind = event.get("type")
        if isinstance(kind, str):
            counts[kind] = counts.get(kind, 0) + 1
        summary: dict[str, Any] = {
            "index": index,
            "type": kind[:64] if isinstance(kind, str) else "unknown",
        }
        if kind == "thread.started":
            thread_seen = True
            summary["thread_id_present"] = bool(event.get("thread_id") or event.get("threadId"))
        elif kind == "turn.started":
            turn_seen = True
            summary["turn_id_present"] = bool(event.get("turn_id") or event.get("turnId"))
        elif kind == "turn.completed":
            terminal = "completed"
            summary["terminal"] = True
        elif kind in {"turn.failed", "error"}:
            terminal = "failed"
            summary["terminal"] = True
            summary["error_present"] = True
            summary["error"] = _event_error_summary(event)
        if kind != "item.completed":
            if len(event_summaries) < 512:
                event_summaries.append(summary)
            continue
        item = event.get("item")
        if not isinstance(item, Mapping):
            if len(event_summaries) < 512:
                event_summaries.append(summary)
            continue
        item_type = item.get("type")
        summary["item_type"] = item_type[:64] if isinstance(item_type, str) else "unknown"
        if item_type != "command_execution":
            if len(event_summaries) < 512:
                event_summaries.append(summary)
            continue
        command = str(item.get("command", ""))
        command_norm = _normalise_command_text(command)
        is_test_command = any(
            token in command_norm
            for token in ("pytest", "npm test", "npm run test", "cargo test", "go test", "dotnet test")
        )
        is_required_test = bool(required_norm and required_norm in command_norm)
        code = item.get("exit_code", item.get("exitCode"))
        status = "PASS" if code == 0 else "FAIL" if isinstance(code, int) else "ERROR"
        summary["command_class"] = "test" if is_test_command else "command"
        summary["required_test"] = is_required_test
        summary["exit_code"] = code if isinstance(code, int) else None
        summary["command_status"] = (
            str(item.get("status")).strip().lower()[:32]
            if isinstance(item.get("status"), str)
            else status.lower()
        )
        summary["command_sha256"] = _digest_text(command)
        summary["command_preview"] = _redact_sensitive(command.replace("\x00", ""), maximum=256)
        summary["command_truncated"] = len(command) > 256
        if is_test_command:
            tests.append(
                {
                    "name": required_test.strip() if is_required_test and isinstance(required_test, str) else "Codex-reported local test command",
                    "status": status,
                }
            )
        if is_required_test:
            # A bounded task may first run the requested command as an
            # environment/diagnostic probe and then rerun it after fixing the
            # workspace.  Preserve every attempt above, but judge the
            # requirement by the terminal result of the last matching
            # invocation.  Earlier failures must not permanently veto a later
            # successful required test; a later failure must still revoke an
            # earlier pass.
            required_test_seen = True
            required_test_passed = status == "PASS"
        if len(event_summaries) < 512:
            event_summaries.append(summary)
    return ExecObservation(
        terminal,
        thread_seen,
        turn_seen,
        counts,
        tuple(tests),
        required_test_seen,
        required_test_passed,
        tuple(event_summaries),
    )


def _status_paths(output: str) -> tuple[str, ...]:
    paths: list[str] = []
    for line in output.splitlines():
        if len(line) < 4:
            continue
        value = line[3:].strip()
        if " -> " in value:
            value = value.split(" -> ", 1)[1]
        value = value.replace("\\", "/")
        if value and value not in paths:
            paths.append(value)
    return tuple(paths)


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _operation_ids(request: ExecutionRequest) -> tuple[str, str]:
    """Return stable parent/child identities for one isolated Codex turn."""

    metadata = request.metadata
    parent = metadata.get("parent_operation_id") or metadata.get("operation_id") or metadata.get("request_id") or request.task_id
    if not isinstance(parent, str) or not parent.strip():
        raise ContractValidationError("parent_operation_id must be a bounded identity")
    parent = parent.strip()[:256]
    explicit_child = metadata.get("child_operation_id")
    if explicit_child is not None:
        if not isinstance(explicit_child, str) or not explicit_child.strip():
            raise ContractValidationError("child_operation_id must be a bounded identity")
        child = explicit_child.strip()[:256]
    else:
        seed = json.dumps(
            {
                "parent": parent,
                "task_id": request.task_id,
                "attempt_index": request.attempt_index,
                "execution_profile": metadata.get("execution_profile", "STANDARD"),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        child = "child-" + _digest_text(seed)[:32]
    return parent, child


def _result_identity(
    *,
    child_operation_id: str,
    returncode: int,
    terminal_status: str,
    changed_files: Sequence[str],
    tests: Sequence[Mapping[str, Any]],
    diff_digest: str,
) -> str:
    payload = {
        "child_operation_id": child_operation_id,
        "returncode": returncode,
        "terminal_status": terminal_status,
        "changed_files": list(changed_files),
        "tests": [dict(item) for item in tests],
        "diff_sha256": diff_digest,
    }
    return "result-" + _digest_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))[:40]


def _normalise_command_text(value: str) -> str:
    """Normalize shell wrappers without changing the command's evidence hash."""

    unescaped = value.replace('\\"', '"').replace("\\'", "'")
    return re.sub(r"\s+", " ", unescaped.lower()).strip()


_MAX_ARTIFACT_TEXT = 200_000


def _stream_evidence(value: str) -> dict[str, Any]:
    encoded = value.encode("utf-8", errors="replace")
    return {
        "present": bool(value),
        "bytes": len(encoded),
        "sha256": _digest_text(value),
        "truncated_for_storage": len(encoded) > _MAX_ARTIFACT_TEXT,
    }


def _redact_sensitive(value: str, *, maximum: int = _MAX_ARTIFACT_TEXT) -> str:
    """Return bounded diagnostic text with common credential forms removed."""

    bounded = value[:maximum]
    patterns = (
        (r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+", r"\1<REDACTED>"),
        (r"(?i)(bearer\s+)[^\s]+", r"\1<REDACTED>"),
        (r"(?i)((?:api[_-]?key|token|secret|password)\s*[:=]\s*)[^\s,;]+", r"\1<REDACTED>"),
        (r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{12,}\b", "<REDACTED_KEY>"),
        (r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{12,}\b", "<REDACTED_TOKEN>"),
    )
    for pattern, replacement in patterns:
        bounded = re.sub(pattern, replacement, bounded)
    if len(value) > maximum:
        bounded += "\n[TRUNCATED]\n"
    return bounded


def _event_error_summary(event: Mapping[str, Any]) -> dict[str, Any]:
    """Extract only bounded, redacted error identity from one CLI event."""

    candidate = event.get("error")
    if isinstance(candidate, Mapping):
        raw_code = candidate.get("code") or candidate.get("type")
        raw_message = candidate.get("message") or candidate.get("detail")
    else:
        raw_code = event.get("code") or event.get("error_code")
        raw_message = event.get("message")
    code = None
    if raw_code is not None:
        code = re.sub(r"[^a-zA-Z0-9_.:-]", "_", str(raw_code))[:64] or None
    message = None
    if raw_message is not None:
        message = _redact_sensitive(str(raw_message).replace("\x00", ""), maximum=512)
    return {
        "code": code,
        "message": message,
        "message_sha256": _digest_text(str(raw_message)) if raw_message is not None else None,
    }


def _first_error_summary(observation: ExecObservation) -> dict[str, Any] | None:
    for event in observation.event_summaries:
        error = event.get("error")
        if isinstance(error, Mapping):
            return dict(error)
    return None


def _classify_provider_failure(reason: Any, *, auth_mode: str | None = None) -> tuple[str, bool]:
    """Map bounded runtime diagnostics to provider-neutral failure codes."""

    text = str(reason or "").lower()
    if (
        "createprocesswithlogonw failed" in text
        or "sandbox spawn failed" in text
        or ("sandbox" in text and "spawn" in text and "failed" in text)
    ):
        return "SANDBOX_UNAVAILABLE", False
    if any(token in text for token in ("model_at_capacity", "at capacity", "model capacity", "capacity exceeded")):
        return "MODEL_AT_CAPACITY", True
    if (auth_mode is not None and auth_mode != "chatgpt") or any(
        token in text
        for token in (
            "auth",
            "logged out",
            "not logged",
            "login required",
            "unauthorized",
            "forbidden",
            "credential",
            "session unavailable",
            "api key",
        )
    ):
        return "AUTH_UNAVAILABLE", False
    if any(token in text for token in ("model catalog", "model/list", "model unavailable", "requested codex model")):
        return "MODEL_UNAVAILABLE", False
    return "PROVIDER_UNAVAILABLE", "timeout" in text or "timed out" in text


def _observation_provider_failure(
    observation: ExecObservation,
    run: CommandResult,
) -> tuple[str, bool, str] | None:
    """Recognize transport/provider failures without relabeling evidence gaps.

    A completed turn with missing diff/test evidence remains a normal FAILED
    result and therefore keeps the existing Stage REPLAN semantics.  Provider
    classification is limited to an explicit bounded error, or a command that
    could not start a turn at all.
    """

    first_error = _first_error_summary(observation)
    if first_error is not None:
        code = first_error.get("code")
        message = first_error.get("message")
        diagnostic = " ".join(item for item in (str(code or ""), str(message or "")) if item)
        lower = diagnostic.lower()
        recognized = any(
            token in lower
            for token in (
                "capacity",
                "rate limit",
                "quota",
                "auth",
                "login",
                "logged out",
                "unauthorized",
                "forbidden",
                "provider unavailable",
                "model unavailable",
                "not available",
                "createprocesswithlogonw failed",
                "sandbox spawn failed",
            )
        )
        if recognized:
            failure_code, retryable = _classify_provider_failure(diagnostic)
            return failure_code, retryable, diagnostic
        # A failed process before a turn is created is a bounded transport
        # failure even if the CLI did not provide a useful error code.
        if not observation.thread_seen and not observation.turn_seen:
            return "PROVIDER_UNAVAILABLE", False, diagnostic
        return None
    if run.returncode != 0 and not observation.thread_seen and not observation.turn_seen:
        diagnostic = run.stderr or f"codex exec exited with code {run.returncode}"
        failure_code, retryable = _classify_provider_failure(diagnostic)
        return failure_code, retryable, diagnostic
    return None


class OpenAICodexExecutor:
    """One native Codex provider; Luna and Sol are only model selections."""

    def __init__(
        self,
        *,
        codex_executable: str | None = None,
        workspace_root: str | None = None,
        preferred_model: str = "luna",
        fallback_model: str | None = None,
        auth_mode: str = "chatgpt",
        timeout_seconds: float = 900.0,
        artifact_dir: str | None = None,
        command_runner: Callable[..., CommandResult] | None = None,
        model_discovery: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
    ) -> None:
        if auth_mode.strip().lower() != "chatgpt":
            raise OpenAICodexUnavailable(
                "OpenAICodexExecutor only supports saved ChatGPT auth; API-key auth is an explicit future provider"
            )
        self.codex_executable = codex_executable
        self.workspace_root = workspace_root
        self.preferred_model = _safe_text(preferred_model, "preferred_model", 128)
        self.fallback_model = fallback_model
        self.auth_mode = "chatgpt"
        self.timeout_seconds = timeout_seconds
        self.artifact_dir = artifact_dir
        self._runner = command_runner or _run_subprocess
        self._model_discovery = model_discovery
        self._runtime = self.discover_runtime()
        self._descriptor = self._make_descriptor()

    def _make_descriptor(self) -> ExecutorDescriptor:
        return ExecutorDescriptor(
            provider_id="openai-codex",
            capabilities=("coding", "execution_evidence", "native_codex", "model_discovery", "chatgpt_auth"),
            execution_modes=("PRIMARY", "MAJOR_CHALLENGER"),
            available=self._runtime.available,
            reliability=0.85,
            cost=0.0,
            metadata={
                "transport": "codex_cli",
                "auth_mode": self.auth_mode,
                "preferred_model": self.preferred_model,
                "mutates_stage": False,
                "runtime_reason": self._runtime.reason,
                "sdk_available": self._runtime.sdk_available,
            },
            priority=10,
        )

    @property
    def descriptor(self) -> ExecutorDescriptor:
        return self._descriptor

    @property
    def runtime(self) -> RuntimeSnapshot:
        return self._runtime

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        # ChatGPT subscription mode uses the saved Codex session.  Do not
        # pass ambient API keys/tokens/secrets to the provider process, and do
        # not inspect or log their values.  CODEX_HOME/PATH remain available
        # so the signed-in session and executable can be discovered.
        sensitive_fragments = ("API_KEY", "AUTH_TOKEN", "ACCESS_TOKEN", "SECRET", "PASSWORD", "PRIVATE_KEY")
        for key in list(env):
            normalized = key.upper()
            if any(fragment in normalized for fragment in sensitive_fragments):
                env.pop(key, None)
        # The disposable coding fixture runs pytest; avoiding bytecode files
        # keeps changed-file evidence limited to the requested source path.
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    def _resolve_executable(self) -> str | None:
        if self.codex_executable:
            candidate = Path(self.codex_executable)
            if candidate.exists() and candidate.is_file():
                return str(candidate)
            return None
        return shutil.which("codex")

    def _discover_models_default(self, executable: str) -> tuple[dict[str, Any], ...]:
        """Discover the runtime catalog through the official bounded CLI.

        Codex CLI 0.145.0 exposes ``debug models`` as the supported catalog
        surface.  Earlier code used ``app-server --stdio`` here, but its
        framing/handshake is not stable across CLI versions and can leave a
        provider turn waiting forever before ``codex exec`` is reached.  A
        normal subprocess invocation gives us explicit exit/timeout handling
        and keeps discovery on the same saved ChatGPT session as execution.
        """

        # Discovery must never inherit the long execution timeout.  The
        # command runner converts a subprocess timeout into exit code 124.
        discovery_timeout = min(max(float(self.timeout_seconds), 1.0), 30.0)
        result = self._runner(
            [executable, "debug", "models"],
            timeout=discovery_timeout,
            env=self._env(),
        )
        if result.returncode != 0:
            if result.returncode == 124:
                raise OpenAICodexUnavailable("codex debug models timed out")
            raise OpenAICodexUnavailable(f"codex debug models failed (exit {result.returncode})")
        if not result.stdout.strip():
            raise OpenAICodexUnavailable("codex debug models returned no catalog")
        return parse_model_catalog(result.stdout)

    def discover_runtime(self) -> RuntimeSnapshot:
        executable = self._resolve_executable()
        sdk_available = importlib.util.find_spec("openai_codex") is not None
        if executable is None:
            return RuntimeSnapshot(None, None, None, False, (), sdk_available, False, "codex executable not found on PATH")
        version = self._runner([executable, "--version"], timeout=30.0, env=self._env())
        if version.returncode != 0:
            return RuntimeSnapshot(executable, None, None, False, (), sdk_available, False, "codex --version failed")
        version_text = next((line.strip() for line in version.stdout.splitlines() if line.strip()), None)
        auth = self._runner([executable, "login", "status"], timeout=30.0, env=self._env())
        auth_mode = parse_auth_status(auth.stdout, auth.stderr, auth.returncode)
        if auth_mode != "chatgpt":
            reason = "saved ChatGPT Codex session unavailable" if auth_mode is None else "Codex is authenticated with API key; ChatGPT auth required"
            return RuntimeSnapshot(executable, version_text, auth_mode, False, (), sdk_available, False, reason)
        try:
            models = tuple(self._model_discovery() if self._model_discovery else self._discover_models_default(executable))
            models = parse_model_catalog(list(models))
        except Exception as exc:
            return RuntimeSnapshot(executable, version_text, auth_mode, True, (), sdk_available, False, str(exc))
        if not models:
            return RuntimeSnapshot(executable, version_text, auth_mode, True, (), sdk_available, False, "Codex model catalog is empty")
        return RuntimeSnapshot(executable, version_text, auth_mode, True, models, sdk_available, True, "ready")

    def _workspace(self, request: ExecutionRequest) -> str:
        root = request.workspace_root or self.workspace_root
        if not root:
            raise OpenAICodexUnavailable("OpenAICodexExecutor requires an explicit workspace_root")
        path = Path(root).resolve()
        if not path.is_dir():
            raise OpenAICodexUnavailable("configured workspace_root is not a directory")
        return str(path)

    def _status(self, workspace: str) -> CommandResult:
        return self._runner(
            ["git", "-C", workspace, "status", "--porcelain=v1", "--untracked-files=all"],
            timeout=30.0,
            env=self._env(),
        )

    def _diff(self, workspace: str) -> CommandResult:
        """Collect tracked workspace changes for deterministic evidence."""

        return self._runner(
            ["git", "-C", workspace, "diff", "HEAD", "--no-ext-diff", "--no-color", "--binary"],
            timeout=30.0,
            env=self._env(),
        )

    def _diff_path(self, workspace: str, path: str) -> CommandResult:
        """Collect one tracked path's diff for baseline-aware boundary checks."""

        return self._runner(
            [
                "git",
                "-C",
                workspace,
                "diff",
                "HEAD",
                "--no-ext-diff",
                "--no-color",
                "--binary",
                "--",
                path,
            ],
            timeout=30.0,
            env=self._env(),
        )

    def _requirements(self, request: ExecutionRequest) -> dict[str, Any]:
        metadata = request.metadata
        required_test = metadata.get("required_test_command")
        if required_test is not None:
            required_test = _safe_text(required_test, "required_test_command", 1000)
        required_path = metadata.get("required_changed_path")
        if required_path is not None:
            required_path = _safe_text(required_path, "required_changed_path", 1000).replace("\\", "/")
        return {
            "required_test_command": required_test,
            "required_changed_path": required_path,
            "require_nonempty_diff": True,
            "execution_receipt_owner": "RUNNER"
            if isinstance(required_path, str) and required_path.lower().endswith(".json")
            else None,
        }

    def _receipt_contract(self, request: ExecutionRequest, workspace: str) -> dict[str, Any] | None:
        """Infer the explicit receipt seam from the stage's required JSON path."""

        required_path = self._requirements(request).get("required_changed_path")
        declared = request.metadata.get("execution_receipt_path") or required_path
        if not isinstance(declared, str) or not declared.strip():
            return None
        if not declared.lower().replace("\\", "/").endswith(".json"):
            return None
        return build_execution_receipt_contract(
            workspace_root=workspace,
            relative_path=declared,
            allowed_paths=request.allowed_paths,
            protected_paths=request.protected_paths,
        )

    def _artifact_root(self, workspace: str, request: ExecutionRequest) -> Path | None:
        configured = request.metadata.get("artifact_dir") or self.artifact_dir
        if configured is None:
            return None
        root = Path(_safe_text(configured, "artifact_dir", 2000)).expanduser().resolve()
        workspace_path = Path(workspace).resolve()
        try:
            root.relative_to(workspace_path)
        except ValueError:
            return root
        raise OpenAICodexUnavailable("artifact_dir must be outside the Codex workspace")

    def _persist_artifacts(
        self,
        *,
        root: Path,
        request: ExecutionRequest,
        command: Sequence[str],
        workspace: str,
        prompt: str,
        run: CommandResult,
        observation: ExecObservation,
        before_status: CommandResult,
        after_status: CommandResult,
        before_diff: CommandResult,
        after_diff: CommandResult,
        requirements: Mapping[str, Any],
        acceptance: Mapping[str, Any],
        changed_files: Sequence[str],
        problems: Sequence[str],
        model: str,
        routing: RoutingDecision,
        execution_binding: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Persist only bounded, metadata-only run evidence outside workspace."""

        try:
            root.mkdir(parents=True, exist_ok=True)
            request_view = request.bounded_view()
            request_view["execution_requirements"] = dict(requirements)
            request_payload = {
                "schema_version": "codex_request_artifacts.v1",
                "request": request_view,
                "action_map": request_view.get("action_map", {}),
                "raw_prompt_stored": False,
            }
            (root / "request.json").write_text(
                _redact_sensitive(json.dumps(request_payload, ensure_ascii=False, sort_keys=True, indent=2)) + "\n",
                encoding="utf-8",
            )

            events_path = root / "events.jsonl"
            events_path.write_text(
                "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in observation.event_summaries),
                encoding="utf-8",
            )
            diff_path = root / "workspace.diff"
            diff_path.write_text(_redact_sensitive(after_diff.stdout), encoding="utf-8")

            run_payload = {
                "schema_version": "codex_run_artifacts.v1",
                "provider": "openai-codex",
                "model": model,
                "actual_model": model,
                "execution_profile": routing.execution_profile,
                "reasoning_effort": routing.reasoning_effort,
                **dict(execution_binding),
                "auth_mode": "chatgpt",
                "profile_derivation_reason": routing.profile_derivation_reason,
                "executor_request_id": routing.executor_request_id,
                "argv": [Path(command[0]).name, *list(command[1:])],
                "cwd": workspace,
                "sandbox": "workspace-write",
                "prompt": {
                    "bounded_request": request_view,
                    "sha256": _digest_text(prompt),
                    "raw_prompt_stored": False,
                },
                "stdout": _stream_evidence(run.stdout),
                "stderr": _stream_evidence(run.stderr),
                "returncode": run.returncode,
                "first_error": _first_error_summary(observation),
                "events": {
                    "summary_count": len(observation.event_summaries),
                    "file": events_path.name,
                    "raw_jsonl_stored": False,
                },
                "workspace": {
                    "before_status": _stream_evidence(before_status.stdout),
                    "after_status": _stream_evidence(after_status.stdout),
                    "before_diff": _stream_evidence(before_diff.stdout),
                    "after_diff": _stream_evidence(after_diff.stdout),
                    "before_diff_returncode": before_diff.returncode,
                    "after_diff_returncode": after_diff.returncode,
                    "before_diff_stderr": _stream_evidence(before_diff.stderr),
                    "after_diff_stderr": _stream_evidence(after_diff.stderr),
                    "changed_files": list(changed_files),
                    "diff_file": diff_path.name,
                },
                "requirements": dict(requirements),
                "acceptance": dict(acceptance),
                "problems": list(problems),
            }
            (root / "run.json").write_text(
                _redact_sensitive(json.dumps(run_payload, ensure_ascii=False, sort_keys=True, indent=2)) + "\n",
                encoding="utf-8",
            )
        except (OSError, TypeError, ValueError):
            return {
                "saved": False,
                "format": "metadata_only",
                "error": "artifact_write_failed",
                "raw_prompt_stored": False,
                "raw_jsonl_stored": False,
            }
        return {
            "saved": True,
            "format": "metadata_only",
            "directory": str(root),
            "files": {
                "request": "request.json",
                "events": "events.jsonl",
                "workspace_diff": "workspace.diff",
                "run": "run.json",
            },
            "raw_prompt_stored": False,
            "raw_jsonl_stored": False,
        }

    def _prompt(
        self,
        request: ExecutionRequest,
        workspace: str | None = None,
        receipt_contract: Mapping[str, Any] | None = None,
    ) -> str:
        view = request.bounded_view()
        requirements = self._requirements(request)
        if receipt_contract is not None:
            requirements["require_nonempty_diff"] = False
            view["execution_receipt_contract"] = dict(receipt_contract)
        view["execution_requirements"] = requirements
        if receipt_contract is not None:
            instruction = (
                "The runner owns the execution receipt. Return the structured provider result; "
                "do not create or modify the receipt path. The runner will persist a validated "
                "receipt at the exact contract path after the provider result is returned.\n"
                f"RECEIPT_OWNER={receipt_contract['owner']} "
                f"RECEIPT_SCHEMA={receipt_contract['schema_version']} "
                f"RECEIPT_RELATIVE_PATH={receipt_contract['relative_path']} "
                f"RECEIPT_ABSOLUTE_PATH={receipt_contract['absolute_path']}\n"
            )
        else:
            instruction = (
                "Modify the required changed path only, fix the requested bug, run the exact required "
                "test command, and do not claim success unless that test passes and the workspace has a "
                "non-empty diff.\n"
            )
        return (
            "Execute the validated Stage task in the configured workspace.\n"
            "Respect allowed_paths and never modify protected_paths. Use only the validated ActionMap.\n"
            + instruction
            + "\n"
            + json.dumps(view, ensure_ascii=False, sort_keys=True)
        )

    @staticmethod
    def _exec_command(
        executable: str,
        model: str,
        workspace: str,
        *,
        reasoning_effort: str = "low",
        is_windows: bool | None = None,
    ) -> list[str]:
        """Build the bounded CLI argv without weakening the sandbox policy.

        The installed Windows runtime may have a global ``windows.sandbox``
        setting of ``elevated`` that cannot spawn a child process in this
        environment.  Override only that platform-specific setting for this
        invocation; the Codex execution sandbox remains ``workspace-write``.
        ``is_windows`` exists solely for deterministic offline tests.
        """

        if is_windows is None:
            is_windows = os.name == "nt"
        command = [executable]
        if is_windows:
            # ``-c`` is a global option and must precede the ``exec`` command.
            command.extend(["-c", 'windows.sandbox="unelevated"'])
        if not isinstance(reasoning_effort, str) or not reasoning_effort.strip():
            raise ContractValidationError("reasoning_effort must be non-empty")
        command.extend(["-c", f'model_reasoning_effort="{reasoning_effort.strip()}"'])
        command.extend(
            [
                "exec",
                "--json",
                "--ephemeral",
                "--model",
                model,
                "--cd",
                workspace,
                "--sandbox",
                "workspace-write",
                "--skip-git-repo-check",
                "-",
            ]
        )
        return command

    def _provider_failure_result(
        self,
        request: ExecutionRequest,
        *,
        code: str,
        reason: Any,
        retryable: bool,
        model: str | None = None,
        routing: RoutingDecision | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> ExecutionResult:
        """Return a schema-valid, provider-neutral availability outcome."""

        bounded_reason = _redact_sensitive(str(reason or "provider unavailable"), maximum=512)
        failure = {
            "kind": "PROVIDER_FAILURE",
            "code": str(code)[:64],
            "retryable": bool(retryable),
            "reason": bounded_reason,
        }
        routing_view = routing.bounded_view() if routing is not None else {
            "execution_profile": None,
            "actual_model": model,
            "reasoning_effort": None,
            "auth_mode": self.auth_mode,
            "profile_derivation_reason": None,
            "executor_request_id": request.metadata.get("executor_request_id") or request.metadata.get("request_id") or request.task_id,
        }
        if routing is not None:
            parent_operation_id, child_operation_id = _operation_ids(request)
            routing_view.update(
                {
                    "requested_route": routing.execution_profile,
                    "requested_model": routing.model,
                    "requested_reasoning_effort": routing.reasoning_effort,
                    "actual_model": model,
                    "actual_reasoning_effort": routing.reasoning_effort if model is not None else None,
                    "parent_operation_id": parent_operation_id,
                    "child_operation_id": child_operation_id,
                    "result_identity": "result-" + _digest_text(f"{child_operation_id}:{code}:{bounded_reason}")[:40],
                }
            )
        measurements = {
            "openai_codex": {
                "model": model,
                **routing_view,
                "provider_id": self.descriptor.provider_id,
                "executable": self._runtime.executable,
                "auth_mode": self.auth_mode,
                "runtime_available": self._runtime.available,
                "runtime_reason": _redact_sensitive(self._runtime.reason, maximum=256),
                "failure_kind": "PROVIDER_FAILURE",
                "failure_code": failure["code"],
                "failure_retryable": failure["retryable"],
                "failure_reason": bounded_reason,
            }
        }
        result: dict[str, Any] = {
            "schema_version": "codex_result.v1",
            "plan_id": request.plan_id,
            "stage_id": request.stage_id,
            "task_id": request.task_id,
            "iteration_index": request.iteration_index,
            "stage_iteration_index": request.iteration_index,
            "attempt_index": request.attempt_index,
            "attempt_number": request.attempt_index,
            "status": "ERROR",
            "summary": "Native OpenAI Codex provider was unavailable; no research evidence was produced.",
            "changed_files": [],
            "tests": [],
            "measurements": measurements,
            "evidence_refs": ["codex://provider-status"],
            "review_artifacts": [],
            "problems_discovered": [failure["code"]],
            "abstraction_layer": "openai-codex",
            "stage_ready": False,
            "user_visible_failure": True,
            "human_gate_required": False,
            "decision_reason": "Provider availability is returned for deterministic StageController handling.",
            "stop_reason": "openai_codex_provider_unavailable",
            "execution_failure": failure,
        }
        if request.baseline_digest:
            result["baseline_digest"] = request.baseline_digest
        if request.retry_budget is not None:
            result["retry_budget"] = request.retry_budget
        checked = validate_execution_result(result)
        bounded_evidence = {
            "provider": "openai-codex",
            "auth_mode": self.auth_mode,
            "executable": self._runtime.executable,
            "runtime_available": self._runtime.available,
            "runtime_reason": _redact_sensitive(self._runtime.reason, maximum=256),
            "model": model,
            **routing_view,
            "failure": dict(failure),
        }
        if evidence:
            bounded_evidence.update(dict(evidence))
        return ExecutionResult(
            provider_id=self.descriptor.provider_id,
            result=checked,
            evidence=bounded_evidence,
        )

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if not isinstance(request, ExecutionRequest):
            raise ContractValidationError("OpenAI Codex executor requires an ExecutionRequest")
        if not request.plan_id or not request.task_id:
            raise ContractValidationError("OpenAI Codex executor request requires plan_id and task_id")
        if request.action_map and (request.action_map.get("validated") is not True or request.action_map.get("execute") is not True):
            raise ContractValidationError("OpenAI Codex executor requires a validated executable ActionMap")
        workspace = self._workspace(request)
        routing = derive_routing_decision(
            request.metadata,
            executor_request_id=request.metadata.get("executor_request_id") or request.metadata.get("request_id") or request.task_id,
            auth_mode=self.auth_mode,
        )
        self._runtime = self.discover_runtime()
        self._descriptor = self._make_descriptor()
        if not self._runtime.available or not self._runtime.executable:
            code, retryable = _classify_provider_failure(
                self._runtime.reason,
                auth_mode=self._runtime.auth_mode,
            )
            return self._provider_failure_result(
                request,
                code=code,
                retryable=retryable,
                reason=self._runtime.reason,
                routing=routing,
            )
        # The profile binding is authoritative.  The legacy preference remains
        # only as a compatibility fallback for injected/offline catalogs that
        # predate the V1 model ids; it cannot replace a FRONTIER binding.
        preference = routing.model
        fallback = None
        if routing.execution_profile == "STANDARD":
            # A fallback is allowed only when explicitly configured by the
            # caller/provider.  The implicit preferred-model fallback would
            # hide a missing canonical model and misstate the route.
            fallback = request.metadata.get("fallback_model") or self.fallback_model
        try:
            model = select_available_model(self._runtime.models, preference, fallback=fallback)
        except OpenAICodexUnavailable as exc:
            code, retryable = _classify_provider_failure(str(exc), auth_mode=self._runtime.auth_mode)
            return self._provider_failure_result(
                request,
                code=code,
                retryable=retryable,
                reason=str(exc),
                routing=routing,
            )
        parent_operation_id, child_operation_id = _operation_ids(request)
        execution_binding = {
            "requested_route": routing.execution_profile,
            "requested_model": routing.model,
            "requested_reasoning_effort": routing.reasoning_effort,
            "actual_model": model,
            "actual_reasoning_effort": routing.reasoning_effort,
            "parent_operation_id": parent_operation_id,
            "child_operation_id": child_operation_id,
        }
        requirements = self._requirements(request)
        receipt_contract = self._receipt_contract(request, workspace)
        if receipt_contract is not None:
            requirements["require_nonempty_diff"] = False
            requirements["execution_receipt_contract"] = {
                key: receipt_contract[key]
                for key in (
                    "owner",
                    "authority",
                    "schema_version",
                    "relative_path",
                    "absolute_path",
                )
            }
        artifact_root = self._artifact_root(workspace, request)
        before = self._status(workspace)
        before_diff = self._diff(workspace)
        before_paths = set(_status_paths(before.stdout))
        protected_before_diffs = {
            path: self._diff_path(workspace, path)
            for path in request.protected_paths
            if path in before_paths
        }
        # Keep this argv aligned with the installed ``codex exec --help``
        # contract.  In CLI 0.145.0 approvals are not exposed as a
        # ``--ask-for-approval`` flag; passing that older/unsupported option
        # makes argument parsing fail before a thread or turn is created.
        command = self._exec_command(
            self._runtime.executable,
            model,
            workspace,
            reasoning_effort=routing.reasoning_effort,
        )
        prompt = self._prompt(request, workspace, receipt_contract)
        run = self._runner(command, cwd=workspace, input_text=prompt, timeout=self.timeout_seconds, env=self._env())
        observation = parse_exec_jsonl(run.stdout, required_test=requirements.get("required_test_command"))
        after = self._status(workspace)
        after_diff = self._diff(workspace)
        changed_files = list(_status_paths(after.stdout))
        # The Stage runtime may already have modified protected control files
        # before the provider turn (for example, registering and starting the
        # Stage). Validate only newly changed paths, plus protected paths whose
        # tracked diff changed during this turn; otherwise a valid worker run
        # is rejected merely because the baseline is not a clean checkout.
        newly_changed_files = [path for path in changed_files if path not in before_paths]
        protected_changed_files = [
            path
            for path, before_path_diff in protected_before_diffs.items()
            if self._diff_path(workspace, path).stdout != before_path_diff.stdout
        ]
        paths_to_validate = list(dict.fromkeys([*newly_changed_files, *protected_changed_files]))
        for path in paths_to_validate:
            if not path_is_allowed(path, request.allowed_paths, protected_paths=request.protected_paths, workspace_root=workspace):
                raise ContractValidationError(f"Codex changed file outside allowed_paths/protected_paths: {path!r}")

        required_path = requirements.get("required_changed_path")
        required_path_changed = bool(required_path and required_path in changed_files)
        allowed_changed_files = [
            path
            for path in changed_files
            if path_is_allowed(path, request.allowed_paths, protected_paths=request.protected_paths, workspace_root=workspace)
        ]
        diff_nonempty = (
            before_diff.returncode == 0
            and after_diff.returncode == 0
            and (
                (bool(after_diff.stdout.strip()) and after_diff.stdout != before_diff.stdout)
                or bool(newly_changed_files)
            )
        )
        turn_completed = (
            run.returncode == 0
            and observation.terminal_status == "completed"
            and observation.thread_seen
            and observation.turn_seen
        )
        provider_failure = _observation_provider_failure(observation, run)
        problems: list[str] = []
        if not turn_completed:
            problems.append("codex_turn_not_completed")
        if receipt_contract is None:
            if not allowed_changed_files:
                problems.append("no_allowed_changed_file")
            if required_path is None:
                problems.append("required_changed_path_not_configured")
            elif not required_path_changed:
                problems.append("required_changed_path_missing")
        if not requirements.get("required_test_command"):
            problems.append("required_test_not_configured")
        elif not observation.required_test_passed:
            problems.append("required_test_not_passed")
        if receipt_contract is None:
            if before_diff.returncode != 0 or after_diff.returncode != 0:
                problems.append("git_diff_unavailable")
            elif not diff_nonempty:
                problems.append("nonempty_diff_required")

        if provider_failure is not None:
            failure_code, retryable, failure_reason = provider_failure
            problems.insert(0, failure_code)

        acceptance = {
            "coding_e2e_accepted": not problems,
            "turn_completed": turn_completed,
            "allowed_changed_file": bool(allowed_changed_files),
            "required_changed_path": required_path_changed,
            "required_test_passed": observation.required_test_passed,
            "nonempty_diff": diff_nonempty,
        }
        if receipt_contract is not None:
            acceptance["allowed_changed_file"] = False
            acceptance["required_changed_path"] = False
            acceptance["nonempty_diff"] = False
            acceptance["execution_receipt"] = False
        tests = list(observation.tests)
        diff_digest = _digest_text(after_diff.stdout)
        execution_binding["result_identity"] = _result_identity(
            child_operation_id=child_operation_id,
            returncode=run.returncode,
            terminal_status=observation.terminal_status,
            changed_files=changed_files,
            tests=tests,
            diff_digest=diff_digest,
        )
        artifact_info: dict[str, Any] | None = None
        if artifact_root is not None:
            artifact_info = self._persist_artifacts(
                root=artifact_root,
                request=request,
                command=command,
                workspace=workspace,
                prompt=prompt,
                run=run,
                observation=observation,
                before_status=before,
                after_status=after,
                before_diff=before_diff,
                after_diff=after_diff,
                requirements=requirements,
                acceptance=acceptance,
                changed_files=changed_files,
                problems=problems,
                model=model,
                routing=routing,
                execution_binding=execution_binding,
            )
            if not artifact_info.get("saved", False):
                problems.append("artifact_persistence_failed")
                acceptance["coding_e2e_accepted"] = False

        status = "SUCCEEDED" if not problems else ("ERROR" if provider_failure is not None else "FAILED")
        diff_bytes = len(after_diff.stdout.encode("utf-8", errors="replace"))
        evidence_refs = ["codex://jsonl-events", "workspace://git-status", "workspace://git-diff"]
        review_artifacts: list[dict[str, Any]] = []
        if artifact_info and artifact_info.get("saved"):
            evidence_refs.extend(
                [
                    "artifact://native-codex/request.json",
                    "artifact://native-codex/events.jsonl",
                    "artifact://native-codex/workspace.diff",
                    "artifact://native-codex/run.json",
                ]
            )
            review_artifacts.append({"name": "native-codex-run-artifacts", "status": "AVAILABLE"})
        result: dict[str, Any] = {
            "schema_version": "codex_result.v1",
            "plan_id": request.plan_id,
            "stage_id": request.stage_id,
            "task_id": request.task_id,
            "iteration_index": request.iteration_index,
            "stage_iteration_index": request.iteration_index,
            "attempt_index": request.attempt_index,
            "attempt_number": request.attempt_index,
            "status": status,
            "summary": "Native OpenAI Codex coding E2E evidence passed." if status == "SUCCEEDED" else "Native OpenAI Codex coding E2E evidence is incomplete.",
            "changed_files": changed_files,
            "tests": tests,
            "measurements": {
                "openai_codex": {
                    "model": model,
                    "actual_model": model,
                    "execution_profile": routing.execution_profile,
                    "reasoning_effort": routing.reasoning_effort,
                    **execution_binding,
                    "provider_id": self.descriptor.provider_id,
                    "executable": self._runtime.executable,
                    "auth_mode": "chatgpt",
                    "profile_derivation_reason": routing.profile_derivation_reason,
                    "executor_request_id": routing.executor_request_id,
                    "terminal_status": observation.terminal_status,
                    "thread_seen": observation.thread_seen,
                    "turn_seen": observation.turn_seen,
                    "event_counts": dict(observation.event_counts),
                    "workspace_before_digest": _digest_text(before.stdout),
                    "workspace_after_digest": _digest_text(after.stdout),
                    "newly_changed_files": newly_changed_files,
                    "allowed_changed_file_count": len(allowed_changed_files),
                    "required_changed_path": required_path,
                    "required_changed_path_changed": required_path_changed,
                    "provider_changed_files": list(changed_files),
                    "receipt_owner": receipt_contract.get("owner") if receipt_contract else None,
                    "receipt_authority": receipt_contract.get("authority") if receipt_contract else None,
                    "receipt_schema_version": receipt_contract.get("schema_version") if receipt_contract else None,
                    "receipt_path": receipt_contract.get("relative_path") if receipt_contract else None,
                    "receipt_absolute_path": receipt_contract.get("absolute_path") if receipt_contract else None,
                    "receipt_persisted": False,
                    "required_test_command": requirements.get("required_test_command"),
                    "required_test_seen": observation.required_test_seen,
                    "required_test_passed": observation.required_test_passed,
                    "before_diff_digest": _digest_text(before_diff.stdout),
                    "after_diff_digest": diff_digest,
                    "before_diff_returncode": before_diff.returncode,
                    "after_diff_returncode": after_diff.returncode,
                    "diff_bytes": diff_bytes,
                    "diff_nonempty": diff_nonempty,
                    "stdout": _stream_evidence(run.stdout),
                    "stderr": _stream_evidence(run.stderr),
                    "first_error": _first_error_summary(observation),
                    "acceptance": acceptance,
                }
            },
            "evidence_refs": evidence_refs,
            "review_artifacts": review_artifacts,
            "problems_discovered": problems,
            "abstraction_layer": "openai-codex",
            "stage_ready": False,
            "user_visible_failure": status != "SUCCEEDED",
            "human_gate_required": False,
            "decision_reason": "Native Codex evidence is returned for deterministic StageController validation.",
            "stop_reason": "openai_codex_turn_terminal",
        }
        if provider_failure is not None:
            failure_code, retryable, failure_reason = provider_failure
            result["measurements"]["openai_codex"].update(
                {
                    "failure_kind": "PROVIDER_FAILURE",
                    "failure_code": failure_code,
                    "failure_retryable": retryable,
                    "failure_reason": _redact_sensitive(failure_reason, maximum=512),
                }
            )
            result["execution_failure"] = {
                "kind": "PROVIDER_FAILURE",
                "code": failure_code,
                "retryable": retryable,
                "reason": _redact_sensitive(failure_reason, maximum=512),
            }
            result["summary"] = "Native OpenAI Codex provider failed before producing complete execution evidence."
            result["stop_reason"] = "openai_codex_provider_failure"

        if receipt_contract is not None:
            try:
                receipt = build_runner_execution_receipt(
                    contract=receipt_contract,
                    request=request,
                    result=result,
                    provider_id=self.descriptor.provider_id,
                    invocation={
                        "model": model,
                        "request_id": routing.executor_request_id,
                        "workdir": workspace,
                        "prompt_digest": _digest_text(prompt),
                        "allowed_paths": list(request.allowed_paths),
                    },
                )
                persisted_path = persist_execution_receipt(receipt_contract, receipt)
            except (ContractValidationError, OSError, TypeError, ValueError):
                problems.append("runner_receipt_persistence_failed")
                result["status"] = "ERROR" if provider_failure is not None else "FAILED"
                result["summary"] = "Runner could not persist the required execution receipt."
                result["user_visible_failure"] = True
                result["problems_discovered"] = problems
                acceptance["coding_e2e_accepted"] = False
                acceptance["execution_receipt"] = False
            else:
                acceptance["execution_receipt"] = True
                acceptance["allowed_changed_file"] = True
                acceptance["required_changed_path"] = True
                acceptance["nonempty_diff"] = True
                acceptance["coding_e2e_accepted"] = not problems
                result["measurements"]["openai_codex"].update(
                    {
                        "receipt_persisted": True,
                        "receipt_actual_path": persisted_path,
                    }
                )
                result["evidence_refs"].append("workspace://execution-receipt")
                result["review_artifacts"].append({"name": "workflow-v2-execution-receipt", "status": "AVAILABLE"})
                result["problems_discovered"] = problems
        if request.baseline_digest:
            result["baseline_digest"] = request.baseline_digest
        if request.retry_budget is not None:
            result["retry_budget"] = request.retry_budget
        checked = validate_execution_result(result)
        return ExecutionResult(
            provider_id=self.descriptor.provider_id,
            result=checked,
            evidence={
                "provider": "openai-codex",
                "model": model,
                "actual_model": model,
                "execution_profile": routing.execution_profile,
                "reasoning_effort": routing.reasoning_effort,
                **execution_binding,
                "executable": self._runtime.executable,
                "auth_mode": "chatgpt",
                "profile_derivation_reason": routing.profile_derivation_reason,
                "executor_request_id": routing.executor_request_id,
                "cli_version": self._runtime.version,
                "thread_seen": observation.thread_seen,
                "turn_seen": observation.turn_seen,
                "changed_file_count": len(changed_files),
                "test_count": len(tests),
                "required_test_command": requirements.get("required_test_command"),
                "required_test_seen": observation.required_test_seen,
                "required_test_passed": observation.required_test_passed,
                "diff_nonempty": diff_nonempty,
                "diff_bytes": diff_bytes,
                "diff_sha256": diff_digest,
                "receipt_owner": receipt_contract.get("owner") if receipt_contract else None,
                "receipt_authority": receipt_contract.get("authority") if receipt_contract else None,
                "receipt_schema_version": receipt_contract.get("schema_version") if receipt_contract else None,
                "receipt_path": receipt_contract.get("relative_path") if receipt_contract else None,
                "receipt_actual_path": (
                    result.get("measurements", {}).get("openai_codex", {}).get("receipt_actual_path")
                    if receipt_contract
                    else None
                ),
                "receipt_persisted": bool(
                    result.get("measurements", {}).get("openai_codex", {}).get("receipt_persisted", False)
                )
                if receipt_contract
                else False,
                "before_diff_returncode": before_diff.returncode,
                "after_diff_returncode": after_diff.returncode,
                "stdout": _stream_evidence(run.stdout),
                "stderr": _stream_evidence(run.stderr),
                "first_error": _first_error_summary(observation),
                "acceptance": acceptance,
                "artifacts": artifact_info,
                "execution_failure": result.get("execution_failure"),
            },
        )


__all__ = [
    "CommandResult",
    "ExecObservation",
    "OpenAICodexExecutor",
    "OpenAICodexUnavailable",
    "RuntimeSnapshot",
    "parse_auth_status",
    "parse_exec_jsonl",
    "parse_model_catalog",
    "select_available_model",
]
