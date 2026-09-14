"""Small, read-only identity and payload diagnostics for the V2 contract seam."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Mapping

from .contracts import canonical_json


CONTRACT_VERSION = "stage.v2"
SCHEMA_ID = "workflow_v2/stage.schema.json"
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / SCHEMA_ID
STAGE_PROPAGATION_INVARIANT = "STAGE_SCOPED_ACTION_MUST_USE_CANONICAL_STAGE"
_MUTABLE_STAGE_FIELDS = frozenset(
    {"status", "owner_stage_id", "current_iteration_id", "current_assessment_id"}
)
_RUNTIME_IDENTITY_FILES = (
    "src/contract_handshake.py",
    "src/product_workflow_runtime.py",
    "src/workflow_mcp.py",
    "src/workflow_v2_controller.py",
    "src/workflow_repair.py",
    "schemas/workflow_v2/stage.schema.json",
)
_STAGE_ACTION_HANDSHAKE = {
    "REGISTER_STAGE": {
        "wire_action": "REGISTER_STAGE",
        "stage_location": "payload.stage",
        "canonical_required": True,
        "canonical_source": "planner canonical stage.v2",
    },
    "PLAN_STAGE": {
        "wire_action": "PLAN_STAGE",
        "stage_location": "request.stage",
        "canonical_required": True,
        "canonical_source": "request.stage or resolve_canonical_stage(stage_id) when already registered",
    },
    "START_STAGE": {
        "wire_action": "START",
        "stage_location": "request.stage",
        "canonical_required": True,
        "canonical_source": "resolve_canonical_stage(stage_id)",
    },
    "REQUEST_EXECUTION": {
        "wire_action": "REQUEST_EXECUTION",
        "stage_location": "request.stage",
        "canonical_required": True,
        "canonical_source": "resolve_canonical_stage(stage_id)",
    },
    "RESOLVE_LEGACY_ORPHAN": {
        "wire_action": "RESOLVE_LEGACY_ORPHAN",
        "stage_location": "request.stage",
        "canonical_required": True,
        "canonical_source": "resolve_canonical_stage(stage_id)",
    },
    "OBSERVE_RESULT": {
        "wire_action": "RECORD_OBSERVATION",
        "stage_location": "request.stage",
        "canonical_required": True,
        "canonical_source": "resolve_canonical_stage(stage_id)",
    },
    "ASSESS_RESULT": {
        "wire_action": "ASSESS_RESULT",
        "stage_location": "request.stage",
        "canonical_required": True,
        "canonical_source": "resolve_canonical_stage(stage_id)",
    },
    "ADVANCE_ITERATION": {
        "wire_action": "ADVANCE_ITERATION",
        "stage_location": "request.stage",
        "canonical_required": True,
        "canonical_source": "resolve_canonical_stage(stage_id)",
    },
    "APPLY_GPT_DECISION": {
        "wire_action": "APPLY_GPT_DECISION",
        "stage_location": "request.stage",
        "canonical_required": True,
        "canonical_source": "resolve_canonical_stage(stage_id)",
    },
    "REQUEST_DECISION": {
        "wire_action": "REQUEST_DECISION",
        "stage_location": "request.stage",
        "canonical_required": True,
        "canonical_source": "resolve_canonical_stage(stage_id)",
    },
    "APPLY_DECISION": {
        "wire_action": "APPLY_DECISION",
        "stage_location": "request.stage",
        "canonical_required": True,
        "canonical_source": "resolve_canonical_stage(stage_id)",
    },
    "RESOLVE_BLOCKER": {
        "wire_action": "RESOLVE_BLOCKER",
        "stage_location": "request.stage",
        "canonical_required": True,
        "canonical_source": "resolve_canonical_stage(stage_id)",
    },
    "APPLY_RECEIPT": {
        "wire_action": "APPLY_RECEIPT",
        "stage_location": "request.stage",
        "canonical_required": True,
        "canonical_source": "resolve_canonical_stage(stage_id)",
    },
    "COMMIT_INTEGRATION": {
        "wire_action": "COMMIT_INTEGRATION",
        "stage_location": "request.stage",
        "canonical_required": True,
        "canonical_source": "resolve_canonical_stage(stage_id)",
    },
    "STOP": {
        "wire_action": "STOP",
        "stage_location": "request.stage",
        "canonical_required": True,
        "canonical_source": "resolve_canonical_stage(stage_id)",
    },
    "ADD_DEPENDENCY": {
        "wire_action": "ADD_DEPENDENCY",
        "stage_location": "request.stage",
        "canonical_required": True,
        "canonical_source": "resolve_canonical_stage(parent_stage_id); child_stage remains a pre-registration candidate",
    },
    "SATISFY_DEPENDENCY": {
        "wire_action": "SATISFY_DEPENDENCY",
        "stage_location": "request.stage",
        "canonical_required": True,
        "canonical_source": "resolve_canonical_stage(parent_stage_id)",
    },
    "CLOSE_STAGE": {
        "wire_action": "CLOSEOUT",
        "stage_location": "request.stage",
        "canonical_required": True,
        "canonical_source": "resolve_canonical_stage(stage_id)",
    },
}
_SENSITIVE_KEYS = frozenset(
    {"authorization", "cookie", "cookies", "password", "prompt", "raw_response", "session", "token", "tokens"}
)


def _schema_digest(path: Path = SCHEMA_PATH) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _runtime_commit(root: Path | None = None) -> str | None:
    directory = (root or SCHEMA_PATH.parents[2]).resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _runtime_source_digest(root: Path | None = None) -> str | None:
    """Fingerprint loaded Engine source so a stale process cannot look current."""

    directory = (root or SCHEMA_PATH.parents[2]).resolve()
    digest = hashlib.sha256()
    try:
        for relative in _RUNTIME_IDENTITY_FILES:
            path = directory / relative
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    except OSError:
        return None
    return digest.hexdigest()


def supervisor_handshake() -> dict[str, Any]:
    """Return identity loaded by this supervisor process."""

    return {
        "contract_version": CONTRACT_VERSION,
        "schema_id": SCHEMA_ID,
        "schema_path": str(SCHEMA_PATH.resolve()),
        "schema_digest": _schema_digest(),
        "runtime_commit": _runtime_commit(),
        "runtime_source_digest": _runtime_source_digest(),
        "stage_action_handshake": stage_action_handshake(),
    }


def compare_handshake(supervisor: Mapping[str, Any] | None) -> dict[str, Any]:
    """Compare the client authority with a fresh supervisor authority."""

    client_digest = _schema_digest()
    client_commit = _runtime_commit()
    supervisor = supervisor if isinstance(supervisor, Mapping) else {}
    supervisor_version = supervisor.get("contract_version")
    supervisor_digest = supervisor.get("schema_digest")
    client_source_digest = _runtime_source_digest()
    supervisor_source_digest = supervisor.get("runtime_source_digest")
    supervisor_commit = supervisor.get("runtime_commit")
    compatible = (
        supervisor_version == CONTRACT_VERSION
        and supervisor_digest == client_digest
        and supervisor.get("schema_id") == SCHEMA_ID
        and (client_commit is None or supervisor_commit == client_commit)
        and supervisor_source_digest == client_source_digest
    )
    return {
        "client_contract_version": CONTRACT_VERSION,
        "supervisor_contract_version": supervisor_version,
        "client_schema_digest": client_digest,
        "supervisor_schema_digest": supervisor_digest,
        "client_schema_path": str(SCHEMA_PATH.resolve()),
        "supervisor_schema_path": supervisor.get("schema_path"),
        "supervisor_runtime_commit": supervisor_commit,
        "client_runtime_commit": client_commit,
        "runtime_commit_equal": client_commit is None or supervisor_commit == client_commit,
        "client_runtime_source_digest": client_source_digest,
        "supervisor_runtime_source_digest": supervisor_source_digest,
        "compatible": compatible,
    }


def stage_action_handshake() -> dict[str, Any]:
    """Return the one shared Stage action boundary description.

    This is metadata only.  It deliberately does not execute a business
    action, create a Stage, or construct a second Stage representation.
    """

    return {
        "invariant": STAGE_PROPAGATION_INVARIANT,
        "actions": copy.deepcopy(_STAGE_ACTION_HANDSHAKE),
    }


def compare_stage_action_handshake(supervisor: Mapping[str, Any] | None) -> dict[str, Any]:
    """Compare the Stage action boundary exposed by a fresh supervisor."""

    expected = stage_action_handshake()
    received = supervisor.get("stage_action_handshake") if isinstance(supervisor, Mapping) else None
    received_actions = received.get("actions") if isinstance(received, Mapping) else None
    action_results: dict[str, dict[str, Any]] = {}
    for action, contract in expected["actions"].items():
        actual = received_actions.get(action) if isinstance(received_actions, Mapping) else None
        action_results[action] = {
            "expected": copy.deepcopy(contract),
            "actual": copy.deepcopy(actual) if isinstance(actual, Mapping) else None,
            "compatible": isinstance(actual, Mapping) and dict(actual) == contract,
        }
    compatible = (
        isinstance(received, Mapping)
        and received.get("invariant") == expected["invariant"]
        and all(item["compatible"] for item in action_results.values())
    )
    return {
        "invariant": expected["invariant"],
        "compatible": compatible,
        "actions": action_results,
    }


def stage_digest(stage: Mapping[str, Any]) -> str:
    """Digest one canonical, full Stage object without projecting fields."""

    return hashlib.sha256(canonical_json(dict(stage)).encode("utf-8")).hexdigest()


def stage_identity_digest(stage: Mapping[str, Any]) -> str:
    """Digest immutable Stage identity while lifecycle fields evolve."""

    identity = {key: copy.deepcopy(value) for key, value in stage.items() if key not in _MUTABLE_STAGE_FIELDS}
    return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "<redacted>" if str(key).casefold() in _SENSITIVE_KEYS else _redact(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return copy.deepcopy(value)


def payload_snapshot(value: Any) -> dict[str, Any]:
    """Return complete, bounded root diagnostics without retaining secrets."""

    safe = _redact(value)
    encoded = canonical_json(safe)
    root = safe if isinstance(safe, Mapping) else {}
    return {
        "top_level_keys": sorted(str(key) for key in root),
        "canonical_json": encoded,
        "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "root_has_schema_version": isinstance(root, Mapping) and "schema_version" in root,
        "schema_version_value": root.get("schema_version") if isinstance(root, Mapping) else None,
        "request_stage_exists": isinstance(root.get("stage"), Mapping),
        "request_stage_schema_version": root.get("stage", {}).get("schema_version") if isinstance(root.get("stage"), Mapping) else None,
        "request_stage_sha256": stage_digest(root["stage"]) if isinstance(root.get("stage"), Mapping) else None,
    }


__all__ = [
    "CONTRACT_VERSION",
    "SCHEMA_ID",
    "SCHEMA_PATH",
    "STAGE_PROPAGATION_INVARIANT",
    "compare_handshake",
    "compare_stage_action_handshake",
    "payload_snapshot",
    "stage_action_handshake",
    "stage_digest",
    "stage_identity_digest",
    "supervisor_handshake",
]
