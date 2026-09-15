"""Canonical Workflow V2 StageController journal and reducer.

The controller is intentionally small and explicit: every lifecycle mutation
is a command applied to one durable journal.  Providers, GPT, Human input and
integration adapters are event sources only; none of them can write Stage
state directly.
"""

from __future__ import annotations

import copy
import json
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import ContractValidationError, canonical_json, sha256_json
from .contract_handshake import STAGE_PROPAGATION_INVARIANT, payload_snapshot, stage_digest, stage_identity_digest
from .execution_receipt_contract import (
    build_execution_receipt_contract,
    resolve_execution_receipt_path as _resolve_execution_receipt_path,
    validate_execution_receipt,
)
from .workflow_v2_contracts import (
    PUBLIC_COMMANDS,
    STAGE_STATES,
    assess_observation,
    BLOCKER_RECOVERABILITIES,
    BLOCKER_STILL_TRUE,
    classify_blocker,
    dependency_authorization_digest,
    validate_command_envelope,
    validate_correction_receipt,
    validate_decision,
    validate_decision_subject,
    validate_dependency,
    validate_dependency_graph,
    validate_evidence_manifest,
    validate_execution_attempt,
    validate_operation_envelope,
    validate_provider_handoff_manifest,
    validate_provider_observation,
    validate_blocker_record,
    validate_genuine_blocked_evidence,
    validate_human_gate,
    observation_identity,
    validate_semantic_iteration,
    validate_stage,
    validate_stage_assessment,
    validate_typed_replan,
    stage_objective_identity,
    PROVIDER_HANDOFF_INVARIANT_VERSION,
)


class WorkflowV2ControllerError(RuntimeError):
    """Raised when a canonical V2 command cannot be committed."""


def resolve_stage_execution_receipt_path(
    workspace_root: str | Path,
    relative_path: str,
    allowed_paths: Sequence[str],
    protected_paths: Sequence[str] = (),
) -> Path:
    """Use the shared receipt path authority at the supervisor boundary."""

    return _resolve_execution_receipt_path(
        workspace_root,
        relative_path,
        allowed_paths,
        protected_paths,
    )


def ingest_stage_execution_receipt(
    workspace_root: str | Path,
    relative_path: str,
    allowed_paths: Sequence[str],
    protected_paths: Sequence[str] = (),
) -> dict[str, Any]:
    """Read and validate the runner-owned receipt at the canonical boundary."""

    contract = build_execution_receipt_contract(
        workspace_root=workspace_root,
        relative_path=relative_path,
        allowed_paths=allowed_paths,
        protected_paths=protected_paths,
    )
    path = resolve_stage_execution_receipt_path(
        workspace_root,
        relative_path,
        allowed_paths,
        protected_paths,
    )
    if not path.is_file():
        raise ContractValidationError(f"execution receipt is missing: {path}")
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractValidationError(f"execution receipt cannot be read: {path}") from exc
    return validate_execution_receipt(receipt, contract=contract)


def _copy(value: Any) -> Any:
    return copy.deepcopy(value)


def _decision_envelope(value: Mapping[str, Any]) -> dict[str, Any]:
    """Strip journal-only resolution metadata before contract validation."""

    return {key: _copy(item) for key, item in value.items() if key != "resolution"}


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowV2ControllerError(f"{field} must be a non-empty string")
    return value.strip()


def _id(prefix: str, value: Any) -> str:
    return f"{prefix}-{value}" if not str(value).startswith(prefix + "-") else str(value)


def _new_command_id() -> str:
    return "command-" + uuid.uuid4().hex


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_provider_handoff_manifest(
    *,
    operation_id: str,
    stage_id: str,
    iteration_id: str,
    attempt_id: str,
    request_id: str,
    request: Mapping[str, Any],
    provenance: Mapping[str, Any],
    purpose: str,
    project_id: str | None = None,
    objective_identity: str | None = None,
) -> dict[str, Any]:
    """Build the durable, secret-free handoff record for one execution intent."""

    request_value = _copy(dict(request))
    request_digest = sha256_json(request_value)
    provider_owner = str(provenance.get("provider") or "fixture-provider")
    provider_route = str(provenance.get("provider_route") or provider_owner)
    provider_request_identity = str(provenance.get("provider_request_id") or request_id)
    descriptor = {
        "schema_version": "request_descriptor.v1",
        "operation": "REQUEST_EXECUTION",
        "purpose": str(purpose),
        "request_digest": request_digest,
        "request": request_value,
        "provider_args": {
            "provider_owner": provider_owner,
            "provider_route": provider_route,
            "provider_request_identity": provider_request_identity,
        },
    }
    if project_id is not None:
        descriptor["project_id"] = _text(project_id, "project_id")
    if objective_identity is not None:
        descriptor["objective_identity"] = _text(objective_identity, "objective_identity")
    manifest = {
        "workflow_operation_id": operation_id,
        "stage_id": stage_id,
        "iteration_id": iteration_id,
        "attempt_id": attempt_id,
        "provider_owner": provider_owner,
        "provider_route": provider_route,
        "provider_request_identity": provider_request_identity,
        "request_digest": request_digest,
        "reconstructible_request_descriptor": descriptor,
        "idempotency_key": "idempotency-" + sha256_json({"operation_id": operation_id, "request_digest": request_digest})[:40],
        "reconciliation_identity": "reconcile-" + sha256_json({"operation_id": operation_id, "provider_request_identity": provider_request_identity})[:40],
        "dispatch_state": "PREPARED",
    }
    if project_id is not None:
        manifest["project_id"] = _text(project_id, "project_id")
    if objective_identity is not None:
        manifest["objective_identity"] = _text(objective_identity, "objective_identity")
    return validate_provider_handoff_manifest(manifest)


def build_registration_payload(stage: Mapping[str, Any]) -> dict[str, Any]:
    """Build the one canonical V2 registration payload envelope."""

    return {"stage": validate_stage(stage)}


TERMINAL_EFFECTS = {"NOT_SENT_PROVEN", "SETTLED"}
UNSETTLED_EFFECTS = {"INTENT_COMMITTED", "SENT_UNSETTLED", "UNKNOWN", "CONFLICT"}
LEGACY_ORPHAN_CLASSIFICATION = "ORPHANED_UNRECOVERABLE_PROVIDER_HANDOFF"
LEGACY_ORPHAN_RESOLUTION = "ABANDON_OLD_OPERATION_AND_CREATE_NEW_ATTEMPT"


def continue_iteration_change_digest(
    *,
    decision_id: str,
    assessment_id: str,
    iteration_id: str,
    objective_identity: str,
) -> str:
    """Bind an automatic CONTINUE handoff to its immutable review identity."""

    return sha256_json(
        {
            "kind": "CONTINUE_NEXT_ITERATION",
            "decision_id": decision_id,
            "assessment_id": assessment_id,
            "iteration_id": iteration_id,
            "objective_identity": objective_identity,
        }
    )


class StageController:
    """Single transition authority for the Workflow V2 lifecycle schema."""

    JOURNAL_SCHEMA = "workflow_v2.journal.v1"

    def __init__(self, *, workspace_id: str, state_path: str | Path | None = None) -> None:
        self.workspace_id = _text(workspace_id, "workspace_id")
        self.state_path = Path(state_path).resolve() if state_path is not None else None
        self._lock = threading.RLock()
        self._journal = self._empty_journal()
        self._last_registration_trace: dict[str, Any] | None = None
        if self.state_path is not None and self.state_path.is_file():
            self._journal = self._read_journal(self.state_path)
            if self._journal["workspace_id"] != self.workspace_id:
                raise WorkflowV2ControllerError("state workspace identity does not match controller")

    @classmethod
    def from_state(cls, state_path: str | Path) -> "StageController":
        path = Path(state_path).resolve()
        if not path.is_file():
            raise WorkflowV2ControllerError(f"journal not found: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        workspace_id = payload.get("workspace_id")
        if not isinstance(workspace_id, str):
            raise WorkflowV2ControllerError("journal has no workspace identity")
        return cls(workspace_id=workspace_id, state_path=path)

    def initialize(self) -> dict[str, Any]:
        """Persist an empty, identity-bound journal for a fresh runtime.

        Initialization is deliberately not a lifecycle command: it creates no
        Stage, iteration, attempt, or decision and therefore cannot consume a
        budget or bypass a gate.
        """

        with self._lock:
            with self._process_lock():
                self._reload_persisted_journal()
                if self._journal["workspace_id"] not in (None, self.workspace_id):
                    raise WorkflowV2ControllerError("journal workspace identity does not match controller")
                if self._journal["workspace_id"] == self.workspace_id:
                    return self.state
                draft = _copy(self._journal)
                draft["workspace_id"] = self.workspace_id
                self._validate_journal(draft)
                self._persist(draft)
                self._journal = draft
                return self.state

    @staticmethod
    def _empty_journal() -> dict[str, Any]:
        return {
            "schema_version": StageController.JOURNAL_SCHEMA,
            "workspace_id": None,
            "revision": 0,
            "events": [],
            "commands": {},
            "stages": {},
            "stage_runtime": {},
            "iterations": {},
            "attempts": {},
            "observations": {},
            "assessments": {},
            "assessment_supersessions": {},
            "decisions": {},
            "dependencies": {},
            "operations": {},
            "legacy_resolutions": {},
            "correction_decisions": {},
            "blockers": {},
            # These collections were added as an optional maintenance seam.
            # Older journals remain loadable and acquire them on the first
            # maintenance command; no historical record is rewritten.
            "blocker_records": [],
            "blocker_revalidations": [],
            "human_gates": [],
        }

    def _read_journal(self, path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkflowV2ControllerError(f"cannot read lifecycle journal: {path}") from exc
        self._validate_journal(payload)
        return payload

    @contextmanager
    def _process_lock(self):
        """Serialize writers across controller processes for one journal path."""

        if self.state_path is None:
            yield
            return
        lock_path = self.state_path.with_name(self.state_path.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _reload_persisted_journal(self) -> None:
        if self.state_path is None or not self.state_path.is_file():
            return
        latest = self._read_journal(self.state_path)
        if latest["workspace_id"] != self.workspace_id:
            raise WorkflowV2ControllerError("state workspace identity does not match controller")
        self._journal = latest

    @staticmethod
    def _validate_journal(payload: Mapping[str, Any]) -> None:
        if payload.get("schema_version") != StageController.JOURNAL_SCHEMA:
            raise WorkflowV2ControllerError("unsupported V2 journal schema")
        if not isinstance(payload.get("workspace_id"), str) or not payload["workspace_id"]:
            raise WorkflowV2ControllerError("journal workspace identity is invalid")
        revision = payload.get("revision")
        if not isinstance(revision, int) or revision < 0:
            raise WorkflowV2ControllerError("journal revision is invalid")
        events = payload.get("events")
        if not isinstance(events, list) or len(events) != revision:
            raise WorkflowV2ControllerError("journal revision/event count mismatch")
        expected_revision = 1
        for event in events:
            if not isinstance(event, Mapping) or event.get("revision") != expected_revision:
                raise WorkflowV2ControllerError("journal event sequence is invalid")
            validate_command_envelope(event.get("command", {}))
            body = {key: event.get(key) for key in ("revision", "command", "payload_digest", "result")}
            if "projection_digest" in event:
                if not isinstance(event["projection_digest"], str) or len(event["projection_digest"]) < 8:
                    raise WorkflowV2ControllerError("journal event projection identity is invalid")
                body["projection_digest"] = event["projection_digest"]
            if event.get("event_id") != "event-" + sha256_json(body):
                raise WorkflowV2ControllerError("journal event identity is invalid")
            expected_revision += 1
        for key in ("commands", "stages", "stage_runtime", "iterations", "attempts", "observations", "assessments", "decisions", "dependencies", "operations", "correction_decisions", "blockers"):
            if not isinstance(payload.get(key), dict):
                raise WorkflowV2ControllerError(f"journal collection is invalid: {key}")
        for key in ("blocker_records", "blocker_revalidations", "human_gates"):
            if key in payload and not isinstance(payload[key], list):
                raise WorkflowV2ControllerError(f"journal collection is invalid: {key}")
        for record in payload.get("blocker_records", []):
            validate_blocker_record(record)
        for record in payload.get("blocker_revalidations", []):
            if not isinstance(record, Mapping):
                raise WorkflowV2ControllerError("blocker revalidation record is invalid")
            for field in ("revalidation_id", "blocker_id", "stage_id", "assessment_id", "blocker_still_true", "recoverability"):
                if field not in record:
                    raise WorkflowV2ControllerError(f"blocker revalidation record is missing {field}")
            if not all(isinstance(record[field], str) and record[field] for field in ("revalidation_id", "blocker_id", "stage_id", "assessment_id")):
                raise WorkflowV2ControllerError("blocker revalidation identity is invalid")
            if record["blocker_still_true"] not in BLOCKER_STILL_TRUE or record["recoverability"] not in BLOCKER_RECOVERABILITIES:
                raise WorkflowV2ControllerError("blocker revalidation classification is invalid")
        for record in payload.get("human_gates", []):
            if not isinstance(record, Mapping) or not isinstance(record.get("decision_id"), str) or not isinstance(record.get("gate"), Mapping):
                raise WorkflowV2ControllerError("human gate record is invalid")
        supersessions = payload.get("assessment_supersessions", {})
        if not isinstance(supersessions, dict):
            raise WorkflowV2ControllerError("journal collection is invalid: assessment_supersessions")
        for supersession_id, supersession in supersessions.items():
            StageController._validate_assessment_supersession(supersession, supersession_id=supersession_id)
        legacy_resolutions = payload.get("legacy_resolutions", {})
        if not isinstance(legacy_resolutions, dict):
            raise WorkflowV2ControllerError("journal collection is invalid: legacy_resolutions")
        for stage in payload["stages"].values():
            checked_stage = validate_stage(stage)
            if checked_stage["workspace_id"] != payload["workspace_id"]:
                raise WorkflowV2ControllerError("persisted Stage workspace identity is invalid")
        for iteration in payload["iterations"].values():
            validate_semantic_iteration(iteration)
        for attempt in payload["attempts"].values():
            validate_execution_attempt(attempt)
        for observation in payload["observations"].values():
            validate_provider_observation(observation)
        for assessment in payload["assessments"].values():
            validate_stage_assessment(assessment)
        for decision in payload["decisions"].values():
            validate_decision(_decision_envelope(decision))
        dependency_fields = (
            "schema_version", "parent_id", "child_id", "predicate", "input_binding",
            "output_contract_digest", "authorization_decision_id", "child_status",
        )
        dependencies = [
            {key: edge[key] for key in dependency_fields}
            for edge in payload["dependencies"].values()
        ]
        for edge in dependencies:
            validate_dependency(edge)
        if dependencies:
            validate_dependency_graph(dependencies, max_nodes=10_000, max_depth=10_000)
        for operation in payload["operations"].values():
            validate_operation_envelope(operation)
        for resolution_id, resolution in legacy_resolutions.items():
            if not isinstance(resolution, Mapping) or resolution.get("resolution_id") != resolution_id:
                raise WorkflowV2ControllerError("legacy resolution identity is invalid")
            required = (
                "schema_version", "resolution_id", "operation_id", "attempt_id", "stage_id",
                "iteration_id", "classification", "reason", "resolution", "effect_class",
                "side_effect_evidence", "old_operation_preserved", "old_operation_redispatched",
                "human_intervention_count",
            )
            if any(not isinstance(resolution.get(field), str) or not resolution[field] for field in required[:-3]):
                raise WorkflowV2ControllerError("legacy resolution record is incomplete")
            if resolution.get("schema_version") != "legacy_operation_resolution.v1":
                raise WorkflowV2ControllerError("legacy resolution schema is invalid")
            if resolution.get("classification") != LEGACY_ORPHAN_CLASSIFICATION or resolution.get("resolution") != LEGACY_ORPHAN_RESOLUTION:
                raise WorkflowV2ControllerError("legacy resolution classification is invalid")
            if resolution.get("effect_class") != "REVERSIBLE_LOCAL_RESEARCH" or resolution.get("side_effect_evidence") != "NONE":
                raise WorkflowV2ControllerError("legacy resolution effect audit is invalid")
            if resolution.get("old_operation_preserved") is not True or resolution.get("old_operation_redispatched") is not False:
                raise WorkflowV2ControllerError("legacy resolution preservation proof is invalid")
            if resolution.get("human_intervention_count") != 0:
                raise WorkflowV2ControllerError("legacy resolution cannot add a Human gate")
            operation = payload["operations"].get(resolution["operation_id"])
            attempt = payload["attempts"].get(resolution["attempt_id"])
            if operation is None or attempt is None or operation.get("subject_id") != resolution["attempt_id"]:
                raise WorkflowV2ControllerError("legacy resolution does not bind an existing operation attempt")
            if attempt.get("stage_id") != resolution["stage_id"] or attempt.get("iteration_id") != resolution["iteration_id"]:
                raise WorkflowV2ControllerError("legacy resolution attempt identity is invalid")
            if operation.get("handoff_invariant_version") is not None or operation.get("provider_handoff_manifest") is not None:
                raise WorkflowV2ControllerError("post-invariant operation cannot be a legacy orphan")
        command_event_ids: set[str] = set()
        for command_id, command_record in payload["commands"].items():
            if not isinstance(command_record, Mapping):
                raise WorkflowV2ControllerError("journal command index entry is invalid")
            if command_record.get("subject_id") is not None and not isinstance(command_record["subject_id"], str):
                raise WorkflowV2ControllerError("journal command subject identity is invalid")
            receipt = command_record.get("receipt")
            if not isinstance(receipt, Mapping):
                raise WorkflowV2ControllerError("journal command receipt is invalid")
            command = receipt.get("command")
            if not isinstance(command, Mapping):
                raise WorkflowV2ControllerError("journal command envelope is missing")
            checked_command = validate_command_envelope(command)
            if checked_command["command_id"] != command_id:
                raise WorkflowV2ControllerError("journal command index identity is invalid")
            if command_record.get("command_type") != checked_command["command_type"] or command_record.get("payload_digest") != checked_command["payload_digest"]:
                raise WorkflowV2ControllerError("journal command index envelope is invalid")
            if command_record.get("subject_id") is not None and command_record["subject_id"] != checked_command["subject_id"]:
                raise WorkflowV2ControllerError("journal command subject index is invalid")
            event = receipt.get("event")
            if not isinstance(event, Mapping) or event.get("revision") != receipt.get("revision"):
                raise WorkflowV2ControllerError("journal command receipt event binding is invalid")
            revision_index = event.get("revision", 0) - 1
            if not isinstance(revision_index, int) or revision_index < 0 or revision_index >= len(events):
                raise WorkflowV2ControllerError("journal command receipt revision is invalid")
            authoritative_event = events[revision_index]
            if canonical_json(authoritative_event) != canonical_json(event):
                raise WorkflowV2ControllerError("journal command receipt is not bound to its event")
            expected_receipt = {"command": authoritative_event["command"], "revision": authoritative_event["revision"], "event": authoritative_event, **_copy(authoritative_event["result"])}
            if canonical_json(receipt) != canonical_json(expected_receipt):
                raise WorkflowV2ControllerError("journal command receipt result is tampered")
            command_event_ids.add(authoritative_event["event_id"])
        if command_event_ids != {event["event_id"] for event in events}:
            raise WorkflowV2ControllerError("journal event/command index coverage is incomplete")
        projection_digests = [event.get("projection_digest") for event in events]
        if any(item is not None for item in projection_digests):
            if not all(item is not None for item in projection_digests):
                raise WorkflowV2ControllerError("journal projection identities are incomplete")
            if projection_digests[-1] != StageController._projection_digest(payload):
                raise WorkflowV2ControllerError("journal projection digest does not match persisted state")

    @staticmethod
    def _projection_digest(journal: Mapping[str, Any]) -> str:
        keys = [
            "schema_version", "workspace_id", "stages", "stage_runtime", "iterations",
            "attempts", "observations", "assessments", "decisions", "dependencies",
            "operations", "correction_decisions", "blockers", "executable_owner_stage_id",
        ]
        if "legacy_resolutions" in journal:
            keys.insert(len(keys) - 2, "legacy_resolutions")
        if "assessment_supersessions" in journal:
            keys.insert(len(keys) - 2, "assessment_supersessions")
        if "blocker_records" in journal:
            keys.insert(len(keys) - 2, "blocker_records")
        if "blocker_revalidations" in journal:
            keys.insert(len(keys) - 2, "blocker_revalidations")
        if "human_gates" in journal:
            keys.insert(len(keys) - 2, "human_gates")
        return sha256_json({key: _copy(journal.get(key)) for key in keys})

    def _persist(self, payload: Mapping[str, Any]) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(self.state_path.name + ".tmp")
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.state_path)

    @property
    def revision(self) -> int:
        return int(self._journal["revision"])

    @property
    def state(self) -> dict[str, Any]:
        """Return a detached authoritative journal view."""

        return _copy(self._journal)

    def snapshot(self) -> dict[str, Any]:
        return self.state

    def _record_event(self, journal: dict[str, Any], command: Mapping[str, Any], payload_digest: str, result: Mapping[str, Any]) -> dict[str, Any]:
        revision = int(journal["revision"]) + 1
        body = {
            "revision": revision,
            "command": _copy(command),
            "payload_digest": payload_digest,
            "result": _copy(result),
            "projection_digest": self._projection_digest(journal),
        }
        event = {**body, "event_id": "event-" + sha256_json(body)}
        journal["events"].append(event)
        journal["revision"] = revision
        return event

    def dispatch(
        self,
        command_type: str,
        *,
        subject_id: str,
        payload: Mapping[str, Any],
        command_id: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        command_type = _text(command_type, "command_type").upper()
        if command_type not in PUBLIC_COMMANDS:
            raise WorkflowV2ControllerError(f"unsupported canonical command: {command_type}")
        command_id = command_id or _new_command_id()
        payload_digest = sha256_json(payload)
        requested_subject_id = _text(subject_id, "subject_id")
        with self._lock:
            with self._process_lock():
                self._reload_persisted_journal()
                effective_revision = self.revision if expected_revision is None else expected_revision
                envelope = {
                    "schema_version": "command_envelope.v2",
                    "workspace_id": self.workspace_id,
                    "command_id": command_id,
                    "expected_revision": effective_revision,
                    "command_type": command_type,
                    "subject_id": requested_subject_id,
                    "payload_digest": payload_digest,
                }
                validate_command_envelope(envelope)
                existing = self._journal["commands"].get(command_id)
                if existing is not None:
                    if (
                        existing.get("payload_digest") != envelope["payload_digest"]
                        or existing.get("command_type") != command_type
                        or existing.get("subject_id") != envelope["subject_id"]
                    ):
                        raise WorkflowV2ControllerError("command id collision with a different payload, type, or subject")
                    return _copy(existing["receipt"])
                if effective_revision != self.revision:
                    raise WorkflowV2ControllerError(
                        f"stale revision: expected {effective_revision}, current {self.revision}"
                    )
                draft = _copy(self._journal)
                draft["workspace_id"] = self.workspace_id
                result = self._apply(draft, command_type, envelope, _copy(dict(payload)))
                event = self._record_event(draft, envelope, envelope["payload_digest"], result)
                receipt = {"command": envelope, "revision": draft["revision"], "event": event, **_copy(result)}
                draft["commands"][command_id] = {
                    "command_type": command_type,
                    "subject_id": envelope["subject_id"],
                    "payload_digest": envelope["payload_digest"],
                    "receipt": _copy(receipt),
                }
                self._validate_journal(draft)
                self._persist(draft)
                self._journal = draft
                return _copy(receipt)

    def _stage(self, journal: Mapping[str, Any], stage_id: str) -> dict[str, Any]:
        try:
            return journal["stages"][stage_id]
        except KeyError as exc:
            raise WorkflowV2ControllerError(f"unknown Stage: {stage_id}") from exc

    def resolve_canonical_stage(self, stage_id: str, *, journal: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Resolve the authoritative Stage object for a Stage-scoped action.

        The journal is the sole lifecycle authority.  This method validates and
        detaches that stored object; it never rebuilds a Stage from projected
        fields or accepts the legacy ``stage_contract.v1`` shape.
        """

        source = self._journal if journal is None else journal
        stage = validate_stage(self._stage(source, _text(stage_id, "stage_id")))
        if stage.get("schema_version") != "stage.v2":
            raise WorkflowV2ControllerError(f"{STAGE_PROPAGATION_INVARIANT}: canonical Stage is not stage.v2")
        return stage

    def canonical_stage_digests(self, stage_id: str, *, journal: Mapping[str, Any] | None = None) -> dict[str, str]:
        """Return full and immutable-identity digests for one canonical Stage."""

        stage = self.resolve_canonical_stage(stage_id, journal=journal)
        return {"stage_sha256": stage_digest(stage), "identity_sha256": stage_identity_digest(stage)}

    def _runtime(self, journal: dict[str, Any], stage_id: str) -> dict[str, Any]:
        runtime = journal["stage_runtime"].setdefault(
            stage_id,
            {"current_attempt_id": None, "in_flight_operation_id": None, "execution_authorized": False, "integration_operation_id": None, "closeout": None},
        )
        runtime.setdefault("current_assessment_epoch_id", None)
        return runtime

    def _require_status(self, stage: Mapping[str, Any], allowed: set[str]) -> None:
        if stage.get("status") not in allowed:
            raise WorkflowV2ControllerError(f"Stage {stage.get('stage_id')} is {stage.get('status')}, expected {sorted(allowed)}")

    @staticmethod
    def _validate_assessment_supersession(value: Mapping[str, Any], *, supersession_id: str | None = None) -> dict[str, Any]:
        """Validate the append-only maintenance record without adding a Stage state."""

        if not isinstance(value, Mapping):
            raise WorkflowV2ControllerError("assessment supersession record is invalid")
        required = (
            "schema_version", "supersession_id", "project_id", "stage_id", "objective_identity",
            "assessment_id", "consultation_id", "reason", "status", "misalignment_evidence_digest",
            "maintenance_authority", "new_assessment_epoch_id", "old_observation_preserved",
            "old_assessment_preserved", "old_gpt_decision_preserved",
        )
        if any(not isinstance(value.get(field), str) or not value[field].strip() for field in required[:-3]):
            raise WorkflowV2ControllerError("assessment supersession record is incomplete")
        if supersession_id is not None and value.get("supersession_id") != supersession_id:
            raise WorkflowV2ControllerError("assessment supersession identity is invalid")
        if value.get("schema_version") != "assessment_supersession.v1":
            raise WorkflowV2ControllerError("assessment supersession schema is invalid")
        if value.get("reason") != "STAGE_OBJECTIVE_EVIDENCE_MISALIGNMENT":
            raise WorkflowV2ControllerError("assessment supersession reason is invalid")
        if value.get("status") != "NOT_APPLICABLE_TO_CURRENT_OBJECTIVE":
            raise WorkflowV2ControllerError("assessment supersession status is invalid")
        for field in ("assessment_id", "objective_identity", "misalignment_evidence_digest", "new_assessment_epoch_id"):
            if not isinstance(value.get(field), str) or len(value[field]) < 8:
                raise WorkflowV2ControllerError(f"assessment supersession {field} identity is invalid")
        for field in ("old_observation_preserved", "old_assessment_preserved", "old_gpt_decision_preserved"):
            if value.get(field) is not True:
                raise WorkflowV2ControllerError(f"assessment supersession {field} proof is invalid")
        if value.get("new_attempt_id") is not None and (not isinstance(value["new_attempt_id"], str) or len(value["new_attempt_id"]) < 8):
            raise WorkflowV2ControllerError("assessment supersession new_attempt_id is invalid")
        return _copy(dict(value))

    def _pending_decisions(self, journal: Mapping[str, Any], subject_id: str) -> list[dict[str, Any]]:
        return [
            decision
            for decision in journal["decisions"].values()
            if decision.get("subject_id") == subject_id and decision.get("resolution") is None
        ]

    def _open_dependencies(self, journal: Mapping[str, Any], parent_id: str) -> list[dict[str, Any]]:
        return [
            edge
            for edge in journal["dependencies"].values()
            if edge.get("parent_id") == parent_id and edge.get("status") == "OPEN"
        ]

    def _attempts_for(self, journal: Mapping[str, Any], stage_id: str, iteration_id: str | None = None) -> list[dict[str, Any]]:
        return [
            attempt
            for attempt in journal["attempts"].values()
            if attempt.get("stage_id") == stage_id and (iteration_id is None or attempt.get("iteration_id") == iteration_id)
        ]

    def _legacy_resolution_for_attempt(self, journal: Mapping[str, Any], attempt_id: str) -> dict[str, Any] | None:
        return next(
            (
                resolution
                for resolution in journal.get("legacy_resolutions", {}).values()
                if resolution.get("attempt_id") == attempt_id
            ),
            None,
        )

    def _budgeted_attempts_for(self, journal: Mapping[str, Any], stage_id: str, iteration_id: str | None = None) -> list[dict[str, Any]]:
        released_attempts = {
            item.get("released_attempt_id")
            for item in journal.get("blocker_revalidations", [])
            if isinstance(item, Mapping) and item.get("released_attempt_id")
        }
        return [
            attempt
            for attempt in self._attempts_for(journal, stage_id, iteration_id)
            if attempt.get("attempt_id") not in released_attempts
            if self._legacy_resolution_for_attempt(journal, attempt["attempt_id"]) is None
            and not any(
                supersession.get("assessment_id") == assessment.get("assessment_id")
                and assessment.get("attempt_id") == attempt.get("attempt_id")
                for supersession in journal.get("assessment_supersessions", {}).values()
                for assessment in journal.get("assessments", {}).values()
            )
        ]

    def _legacy_orphan_operation(self, journal: Mapping[str, Any], stage_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        runtime = journal.get("stage_runtime", {}).get(stage_id, {})
        operation_id = runtime.get("in_flight_operation_id")
        if not operation_id:
            return None
        operation = journal.get("operations", {}).get(operation_id)
        if not isinstance(operation, Mapping):
            return None
        attempt = journal.get("attempts", {}).get(operation.get("subject_id"))
        if not isinstance(attempt, Mapping):
            return None
        if operation.get("handoff_invariant_version") == PROVIDER_HANDOFF_INVARIANT_VERSION and operation.get("provider_handoff_manifest") is None:
            raise WorkflowV2ControllerError("CRITICAL_PROVIDER_HANDOFF_INVARIANT_VIOLATION")
        if operation.get("handoff_invariant_version") is None and operation.get("provider_handoff_manifest") is None:
            return _copy(dict(operation)), _copy(dict(attempt))
        return None

    def audit_legacy_side_effects(self, stage_id: str | None = None, *, search_roots: list[str | Path] | None = None) -> dict[str, Any]:
        """Run one bounded, metadata-only audit before legacy abandonment."""

        stage = self.show_stage(stage_id)
        selected_stage_id = stage.get("stage_id")
        if not isinstance(selected_stage_id, str):
            raise WorkflowV2ControllerError("legacy side-effect audit requires an active Stage")
        orphan = self._legacy_orphan_operation(self._journal, selected_stage_id)
        if orphan is None:
            raise WorkflowV2ControllerError("current Stage has no pre-invariant orphan operation")
        operation_id, _attempt = orphan
        operation_token = operation_id["operation_id"]
        roots: list[Path] = []
        if self.state_path is not None:
            roots.append(self.state_path.parent)
        roots.extend(Path(item).expanduser().resolve() for item in (search_roots or []))
        matches: list[str] = []
        inaccessible: list[str] = []
        seen: set[Path] = set()
        for root in roots:
            root = root.resolve()
            if root in seen or not root.is_dir():
                continue
            seen.add(root)
            try:
                candidates = root.rglob("*")
            except OSError as exc:
                inaccessible.append(str(root))
                continue
            for candidate in candidates:
                if not candidate.is_file() or candidate.name in {self.state_path.name if self.state_path else "", (self.state_path.name + ".lock") if self.state_path else ""}:
                    continue
                if ".git" in candidate.parts:
                    continue
                try:
                    if candidate.stat().st_size > 2 * 1024 * 1024:
                        continue
                    if operation_token.encode("utf-8") in candidate.read_bytes():
                        matches.append(str(candidate))
                except OSError:
                    inaccessible.append(str(candidate))
        classification = "AMBIGUOUS" if inaccessible else "PRESENT" if matches else "NONE"
        return {
            "schema_version": "side_effect_audit.v1",
            "operation_id": operation_token,
            "classification": classification,
            "complete": not inaccessible,
            "checked_scopes": [str(root) for root in roots],
            "matches": sorted(set(matches)),
        }

    def _descendant_ids(self, journal: Mapping[str, Any], stage_id: str) -> set[str]:
        children: dict[str, set[str]] = {}
        for edge in journal["dependencies"].values():
            children.setdefault(edge["parent_id"], set()).add(edge["child_id"])
        found: set[str] = set()
        pending = list(children.get(stage_id, set()))
        while pending:
            child_id = pending.pop()
            if child_id in found:
                continue
            found.add(child_id)
            pending.extend(children.get(child_id, set()))
        return found

    def _ancestor_ids(self, journal: Mapping[str, Any], stage_id: str) -> set[str]:
        parents = {edge["child_id"]: edge["parent_id"] for edge in journal["dependencies"].values()}
        found: set[str] = set()
        current = stage_id
        while current in parents:
            current = parents[current]
            if current in found:
                break
            found.add(current)
        return found

    def _apply(self, journal: dict[str, Any], command_type: str, command: Mapping[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        handlers = {
            "REGISTER_STAGE": self._register_stage,
            "START": self._start,
            "REQUEST_EXECUTION": self._request_execution,
            "RESOLVE_LEGACY_ORPHAN": self._resolve_legacy_orphan,
            "RECORD_OBSERVATION": self._record_observation,
            "ASSESS_RESULT": self._assess_result,
            "SUPERSEDE_ASSESSMENT": self._supersede_assessment,
            "APPLY_GPT_DECISION": self._apply_gpt_decision,
            "ADVANCE_ITERATION": self._advance_iteration,
            "REQUEST_DECISION": self._request_decision,
            "APPLY_DECISION": self._apply_decision,
            "ADD_DEPENDENCY": self._add_dependency,
            "SATISFY_DEPENDENCY": self._satisfy_dependency,
            "RESOLVE_BLOCKER": self._resolve_blocker,
            "APPLY_RECEIPT": self._apply_receipt,
            "COMMIT_INTEGRATION": self._commit_integration,
            "CLOSEOUT": self._closeout,
            "STOP": self._stop,
        }
        return handlers[command_type](journal, command, payload)

    def _register_stage(self, journal: dict[str, Any], command: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping) or not isinstance(payload.get("stage"), Mapping):
            raise WorkflowV2ControllerError(
                "REGISTER_STAGE requires the canonical payload envelope {'stage': <stage.v2>}"
            )
        stage_payload = payload["stage"]
        self._last_registration_trace = {
            "action": "REGISTER_STAGE",
            "canonical_stage": payload_snapshot(stage_payload),
            "canonical_stage_digest": stage_digest(stage_payload),
            "canonical_stage_identity_digest": stage_identity_digest(stage_payload),
            "transport_payload": payload_snapshot(payload),
            "supervisor_received_payload": payload_snapshot(stage_payload),
        }
        stage = validate_stage(stage_payload)
        if stage["workspace_id"] != self.workspace_id:
            raise WorkflowV2ControllerError("Stage workspace identity does not match journal")
        if stage["stage_id"] in journal["stages"]:
            existing = journal["stages"][stage["stage_id"]]
            if canonical_json(existing) != canonical_json(stage):
                raise WorkflowV2ControllerError("Stage identity collision with a different baseline")
            return {"stage": self.show_stage(stage["stage_id"], journal=journal)}
        if stage["status"] != "PLANNED":
            raise WorkflowV2ControllerError("REGISTER_STAGE requires PLANNED")
        journal["stages"][stage["stage_id"]] = _copy(stage)
        journal["stage_runtime"][stage["stage_id"]] = {
            "current_attempt_id": None,
            "in_flight_operation_id": None,
            "execution_authorized": False,
            "integration_operation_id": None,
            "closeout": None,
        }
        return {"stage": self.show_stage(stage["stage_id"], journal=journal)}

    def _start(self, journal: dict[str, Any], command: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        stage_id = command["subject_id"]
        stage = self._stage(journal, stage_id)
        self._require_status(stage, {"PLANNED"})
        if self._pending_decisions(journal, stage_id):
            raise WorkflowV2ControllerError("unresolved Decision blocks START")
        if self._open_dependencies(journal, stage_id):
            raise WorkflowV2ControllerError("unresolved Dependency blocks START")
        owner = journal.get("executable_owner_stage_id")
        if owner not in (None, stage_id):
            raise WorkflowV2ControllerError("executable owner is reserved by another Stage")
        if stage["budgets"]["max_iterations"] < 1:
            raise WorkflowV2ControllerError("iteration budget is exhausted")
        iteration_id = _id("iteration", command["command_id"])
        iteration = {
            "schema_version": "semantic_iteration.v2",
            "iteration_id": iteration_id,
            "stage_id": stage_id,
            "index": 1,
            "solution_fingerprint": payload.get("solution_fingerprint", stage["objective_fingerprint"]),
            "opened_by": "START",
            "requires_next_iteration": False,
            "review_identity": None,
            "technical_change_digest": None,
        }
        validate_semantic_iteration(iteration)
        journal["iterations"][iteration_id] = iteration
        stage["status"] = "ACTIVE"
        stage["current_iteration_id"] = iteration_id
        stage["owner_stage_id"] = stage_id
        journal["executable_owner_stage_id"] = stage_id
        return {"stage": self.show_stage(stage_id, journal=journal), "iteration": _copy(iteration)}

    def _request_execution(self, journal: dict[str, Any], command: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        stage_id = command["subject_id"]
        stage = self._stage(journal, stage_id)
        self._require_status(stage, {"ACTIVE"})
        runtime = self._runtime(journal, stage_id)
        if journal.get("executable_owner_stage_id") != stage_id:
            raise WorkflowV2ControllerError("Stage does not own the executable token")
        if self._pending_decisions(journal, stage_id) or self._open_dependencies(journal, stage_id):
            raise WorkflowV2ControllerError("unresolved gate/dependency blocks execution")
        if runtime["in_flight_operation_id"] is not None:
            raise WorkflowV2ControllerError("an execution operation is already in flight")
        iteration_id = stage.get("current_iteration_id")
        if not iteration_id or iteration_id not in journal["iterations"]:
            raise WorkflowV2ControllerError("execution requires an opened semantic iteration")
        iteration_attempts = self._attempts_for(journal, stage_id, iteration_id)
        budgeted_iteration_attempts = self._budgeted_attempts_for(journal, stage_id, iteration_id)
        if len(budgeted_iteration_attempts) >= stage["budgets"]["max_attempts_per_iteration"]:
            raise WorkflowV2ControllerError("per-iteration attempt budget exhausted")
        if len(self._budgeted_attempts_for(journal, stage_id)) >= stage["budgets"]["max_attempts_total"]:
            raise WorkflowV2ControllerError("total attempt budget exhausted")
        for ancestor_id in self._ancestor_ids(journal, stage_id):
            ancestor = self._stage(journal, ancestor_id)
            descendant_attempts = sum(
                len(self._budgeted_attempts_for(journal, descendant_id))
                for descendant_id in self._descendant_ids(journal, ancestor_id)
            )
            if descendant_attempts >= ancestor["budgets"]["max_descendant_attempts"]:
                raise WorkflowV2ControllerError("descendant attempt budget exhausted")
        reason = str(payload.get("reason", "INITIAL")).upper()
        if iteration_attempts and reason not in {"RETRY", "ENGINEERING_FIX", "CONTINUE", "LEGACY_ORPHAN_RECOVERY", "OBJECTIVE_SUPERSESSION_RECOVERY"}:
            raise WorkflowV2ControllerError("a later attempt needs an explicit typed retry/review reason")
        if reason == "LEGACY_ORPHAN_RECOVERY":
            if not any(
                resolution.get("stage_id") == stage_id
                and resolution.get("resolution") == LEGACY_ORPHAN_RESOLUTION
                for resolution in journal.get("legacy_resolutions", {}).values()
            ):
                raise WorkflowV2ControllerError("LEGACY_ORPHAN_RECOVERY requires a committed orphan resolution")
        if reason == "RETRY" and iteration_attempts:
            latest_attempt = max(iteration_attempts, key=lambda item: item["attempt_index"])
            latest_observation = next(
                (
                    observation
                    for observation in journal["observations"].values()
                    if observation.get("attempt_id") == latest_attempt["attempt_id"]
                ),
                None,
            )
            if latest_observation is None or latest_observation.get("failure", {}).get("retryability") != "RETRYABLE":
                raise WorkflowV2ControllerError("RETRY requires a controller-typed RETRYABLE failure")
        if reason == "OBJECTIVE_SUPERSESSION_RECOVERY":
            if not self._runtime(journal, stage_id).get("current_assessment_epoch_id"):
                raise WorkflowV2ControllerError("OBJECTIVE_SUPERSESSION_RECOVERY requires an active assessment epoch")
            if not any(
                item.get("stage_id") == stage_id
                and item.get("new_assessment_epoch_id") == self._runtime(journal, stage_id).get("current_assessment_epoch_id")
                for item in journal.get("assessment_supersessions", {}).values()
            ):
                raise WorkflowV2ControllerError("OBJECTIVE_SUPERSESSION_RECOVERY requires a committed assessment supersession")
        if reason in {"ENGINEERING_FIX", "CONTINUE"} and not runtime["execution_authorized"]:
            raise WorkflowV2ControllerError(f"{reason} requires a typed GPT decision")
        request = _copy(payload.get("request", {}))
        request_id = payload.get("request_id") or _id("request", command["command_id"])
        request_digest = payload.get("request_digest") or sha256_json(request)
        provenance = _copy(payload.get("provenance", {"provider": "fixture-provider", "engine_digest": "engine-fixed-v2"}))
        purpose = payload.get("purpose", stage["purpose"])
        operation_id = payload.get("operation_id") or _id("operation", command["command_id"])
        recovery_epoch_id = self._runtime(journal, stage_id).get("current_assessment_epoch_id")
        recovery_supersession = next(
            (
                item for item in journal.get("assessment_supersessions", {}).values()
                if item.get("stage_id") == stage_id and item.get("new_assessment_epoch_id") == recovery_epoch_id
            ),
            None,
        ) if reason == "OBJECTIVE_SUPERSESSION_RECOVERY" else None
        expected_recovery_attempt_id = recovery_supersession.get("new_attempt_id") if recovery_supersession else None
        attempt_id = payload.get("attempt_id") or expected_recovery_attempt_id or _id("attempt", command["command_id"])
        if expected_recovery_attempt_id is not None and attempt_id != expected_recovery_attempt_id:
            raise WorkflowV2ControllerError("OBJECTIVE_SUPERSESSION_RECOVERY attempt identity does not match the assessment epoch")
        handoff = payload.get("provider_handoff_manifest")
        if not isinstance(handoff, Mapping):
            raise WorkflowV2ControllerError("INTENT_COMMIT_REQUIRES_RECOVERABLE_PROVIDER_HANDOFF")
        handoff = validate_provider_handoff_manifest(handoff)
        if request_digest != sha256_json(request):
            raise WorkflowV2ControllerError("provider handoff request_digest does not match request bytes")
        expected_handoff = {
            "workflow_operation_id": operation_id,
            "stage_id": stage_id,
            "iteration_id": iteration_id,
            "attempt_id": attempt_id,
            "request_digest": request_digest,
        }
        if any(handoff.get(field) != value for field, value in expected_handoff.items()):
            raise WorkflowV2ControllerError("provider handoff manifest does not bind the execution identity")
        if handoff.get("dispatch_state") != "PREPARED":
            raise WorkflowV2ControllerError("provider handoff must be durably PREPARED before intent commit")
        objective_identity = stage_objective_identity(stage)
        if handoff.get("project_id") != stage["project_id"] or handoff.get("objective_identity") != objective_identity:
            raise WorkflowV2ControllerError("PROVIDER_REQUEST_OBJECTIVE_MISMATCH")
        descriptor = handoff.get("reconstructible_request_descriptor", {})
        if descriptor.get("project_id") != stage["project_id"] or descriptor.get("objective_identity") != objective_identity:
            raise WorkflowV2ControllerError("PROVIDER_REQUEST_OBJECTIVE_MISMATCH")
        assessment_epoch_id = self._runtime(journal, stage_id).get("current_assessment_epoch_id")
        attempt = {
            "schema_version": "execution_attempt.v2",
            "attempt_id": attempt_id,
            "project_id": stage["project_id"],
            "stage_id": stage_id,
            "objective_identity": objective_identity,
            "iteration_id": iteration_id,
            "request_id": request_id,
            "request_digest": request_digest,
            "provenance": provenance,
            "purpose": purpose,
            "effect_state": "INTENT_COMMITTED",
            "attempt_index": len(self._attempts_for(journal, stage_id)) + 1,
            "committed": True,
            "status": "REQUESTED",
        }
        if assessment_epoch_id is not None:
            attempt["assessment_epoch_id"] = assessment_epoch_id
        validate_execution_attempt(attempt)
        operation = {
            "schema_version": "operation_envelope.v2",
            "operation_id": operation_id,
            "workspace_id": self.workspace_id,
            "subject_id": attempt_id,
            "intent_digest": request_digest,
            "effect_state": "INTENT_COMMITTED",
            "capability_manifest": _copy(payload.get("capability_manifest", {"query_by_operation_id": False, "idempotent_submit": False, "fence": False, "prove_not_sent": False})),
            "handoff_invariant_version": PROVIDER_HANDOFF_INVARIANT_VERSION,
            "provider_handoff_manifest": _copy(handoff),
            "status": "INTENT",
        }
        validate_operation_envelope(operation)
        journal["attempts"][attempt_id] = attempt
        journal["operations"][operation_id] = operation
        runtime["current_attempt_id"] = attempt_id
        runtime["in_flight_operation_id"] = operation_id
        runtime["execution_authorized"] = False
        if assessment_epoch_id is not None:
            runtime["current_assessment_epoch_id"] = assessment_epoch_id
        stage["current_assessment_id"] = None
        return {"stage": self.show_stage(stage_id, journal=journal), "attempt": _copy(attempt), "operation": _copy(operation)}

    def _resolve_legacy_orphan(self, journal: dict[str, Any], command: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        """Append-only resolution for a pre-invariant orphaned intent.

        The historical attempt and operation are deliberately not rewritten.
        Only the canonical runtime pointer is released and a resolution record
        is appended, allowing the normal REQUEST_EXECUTION command to create a
        distinct replacement identity.
        """

        stage_id = command["subject_id"]
        stage = self._stage(journal, stage_id)
        self._require_status(stage, {"ACTIVE"})
        operation_id = _text(payload.get("operation_id"), "operation_id")
        operation = journal["operations"].get(operation_id)
        if operation is None:
            raise WorkflowV2ControllerError("legacy resolution requires an existing operation")
        attempt_id = operation.get("subject_id")
        attempt = journal["attempts"].get(attempt_id)
        if attempt is None or attempt.get("stage_id") != stage_id:
            raise WorkflowV2ControllerError("legacy resolution operation is not bound to this Stage")
        if operation.get("handoff_invariant_version") == PROVIDER_HANDOFF_INVARIANT_VERSION:
            if operation.get("provider_handoff_manifest") is None:
                raise WorkflowV2ControllerError("CRITICAL_PROVIDER_HANDOFF_INVARIANT_VIOLATION")
            raise WorkflowV2ControllerError("legacy resolution is only valid for pre-invariant operations")
        if operation.get("provider_handoff_manifest") is not None:
            raise WorkflowV2ControllerError("legacy resolution cannot classify a partially formed handoff")
        if operation.get("effect_state") != "INTENT_COMMITTED" or operation.get("status") != "INTENT":
            raise WorkflowV2ControllerError("legacy resolution requires an unresolved intent")
        if attempt.get("effect_state") != "INTENT_COMMITTED" or attempt.get("status") != "REQUESTED":
            raise WorkflowV2ControllerError("legacy resolution requires an unobserved attempt")
        if self._runtime(journal, stage_id).get("in_flight_operation_id") != operation_id:
            raise WorkflowV2ControllerError("legacy resolution operation is not the current in-flight operation")
        if any(observation.get("attempt_id") == attempt_id for observation in journal["observations"].values()):
            raise WorkflowV2ControllerError("legacy resolution cannot abandon an observed attempt")
        audit = payload.get("side_effect_audit")
        if not isinstance(audit, Mapping) or audit.get("operation_id") != operation_id or audit.get("classification") != "NONE" or audit.get("complete") is not True:
            raise WorkflowV2ControllerError("legacy resolution requires a complete NONE side-effect audit")
        if payload.get("effect_class") != "REVERSIBLE_LOCAL_RESEARCH":
            raise WorkflowV2ControllerError("legacy resolution is only automatic for reversible local research")
        resolution_id = payload.get("resolution_id") or _id("legacy-resolution", command["command_id"])
        resolution = {
            "schema_version": "legacy_operation_resolution.v1",
            "resolution_id": resolution_id,
            "operation_id": operation_id,
            "attempt_id": attempt_id,
            "stage_id": stage_id,
            "iteration_id": attempt["iteration_id"],
            "classification": LEGACY_ORPHAN_CLASSIFICATION,
            "reason": "provider identity/request/receipt absent in pre-invariant operation",
            "resolution": LEGACY_ORPHAN_RESOLUTION,
            "effect_class": "REVERSIBLE_LOCAL_RESEARCH",
            "side_effect_evidence": "NONE",
            "side_effect_audit": _copy(dict(audit)),
            "old_operation_preserved": True,
            "old_operation_redispatched": False,
            "human_intervention_count": 0,
        }
        journal.setdefault("legacy_resolutions", {})[resolution_id] = resolution
        runtime = self._runtime(journal, stage_id)
        runtime["current_attempt_id"] = None
        runtime["in_flight_operation_id"] = None
        runtime["execution_authorized"] = False
        stage["current_assessment_id"] = None
        return {
            "stage": self.show_stage(stage_id, journal=journal),
            "legacy_resolution": _copy(resolution),
            "old_operation": _copy(operation),
            "old_attempt": _copy(attempt),
        }

    def _supersede_assessment(self, journal: dict[str, Any], command: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        """Record a bounded objective-mismatch recovery on the same Stage.

        This command never rewrites the historical observation, assessment, or
        GPT decision.  It only releases the current assessment projection and
        opens an assessment epoch whose next canonical action is execution.
        """

        stage_id = command["subject_id"]
        stage = self._stage(journal, stage_id)
        self._require_status(stage, {"ACTIVE"})
        assessment_id = _text(payload.get("assessment_id"), "assessment_id")
        assessment = journal["assessments"].get(assessment_id)
        if assessment is None or assessment.get("stage_id") != stage_id:
            raise WorkflowV2ControllerError("assessment supersession requires an assessment from the current Stage")
        current_objective = stage_objective_identity(stage)
        supersession_id = payload.get("supersession_id") or _id("assessment-supersession", command["command_id"])
        existing = journal.setdefault("assessment_supersessions", {}).get(supersession_id)
        if existing is not None:
            self._validate_assessment_supersession(existing, supersession_id=supersession_id)
            supplied_reason = str(payload.get("reason", "")).upper()
            if (
                existing.get("stage_id") != stage_id
                or existing.get("project_id") != stage.get("project_id")
                or existing.get("objective_identity") != current_objective
                or existing.get("assessment_id") != assessment_id
                or existing.get("consultation_id") != payload.get("consultation_id")
                or existing.get("reason") != supplied_reason
                or existing.get("maintenance_authority") != payload.get("maintenance_authority")
                or existing.get("misalignment_evidence_digest") != payload.get("misalignment_evidence_digest")
                or (
                    payload.get("new_assessment_epoch_id") is not None
                    and existing.get("new_assessment_epoch_id") != payload.get("new_assessment_epoch_id")
                )
                or (
                    payload.get("new_attempt_id") is not None
                    and existing.get("new_attempt_id") != payload.get("new_attempt_id")
                )
            ):
                raise WorkflowV2ControllerError("assessment supersession identity collision")
            return {"stage": self.show_stage(stage_id, journal=journal), "supersession": _copy(existing)}
        if stage.get("current_assessment_id") != assessment_id:
            raise WorkflowV2ControllerError("assessment supersession requires the current assessment")
        old_objective = assessment.get("objective_identity")
        if old_objective == current_objective:
            raise WorkflowV2ControllerError("TRUE_SCIENTIFIC_BLOCKED_NOT_SUPERSEDABLE")
        consultation_id = _text(payload.get("consultation_id"), "consultation_id")
        maintenance_authority = _text(payload.get("maintenance_authority"), "maintenance_authority")
        reason = _text(payload.get("reason"), "reason").upper()
        if reason != "STAGE_OBJECTIVE_EVIDENCE_MISALIGNMENT":
            raise WorkflowV2ControllerError("assessment supersession reason is not an approved maintenance reason")
        proof = payload.get("misalignment_proof")
        if not isinstance(proof, Mapping):
            raise WorkflowV2ControllerError("assessment supersession requires explicit misalignment proof")
        if proof.get("assessment_id") != assessment_id or proof.get("consultation_id") != consultation_id:
            raise WorkflowV2ControllerError("assessment supersession proof is not bound to the historical review")
        if proof.get("current_objective_identity") != current_objective:
            raise WorkflowV2ControllerError("assessment supersession proof does not bind the current objective")
        if proof.get("old_objective_identity") != old_objective:
            raise WorkflowV2ControllerError("assessment supersession proof does not bind the old objective")
        if proof.get("objective_mismatch_proven") is not True:
            raise WorkflowV2ControllerError("assessment supersession requires proven objective mismatch")
        if proof.get("validation_evidence_only") is not True:
            raise WorkflowV2ControllerError("assessment supersession requires validation-only evidence proof")
        if proof.get("real_current_objective_scientific_result_present") is not False:
            raise WorkflowV2ControllerError("assessment supersession cannot invalidate current-objective scientific evidence")
        evidence_digest = _text(payload.get("misalignment_evidence_digest"), "misalignment_evidence_digest")
        if evidence_digest != sha256_json(proof):
            raise WorkflowV2ControllerError("assessment supersession evidence digest does not match proof")
        blocked = self._resolved_gpt_decision(journal, assessment_id)
        if blocked is None or blocked.get("resolution") != "BLOCKED":
            raise WorkflowV2ControllerError("assessment supersession requires the preserved GPT BLOCKED decision")
        epoch_id = payload.get("new_assessment_epoch_id") or _id("assessment-epoch", supersession_id)
        new_attempt_id = payload.get("new_attempt_id") or _id("attempt", f"{epoch_id}-recovery")
        record = {
            "schema_version": "assessment_supersession.v1",
            "supersession_id": supersession_id,
            "project_id": stage["project_id"],
            "stage_id": stage_id,
            "objective_identity": current_objective,
            "assessment_id": assessment_id,
            "consultation_id": consultation_id,
            "reason": reason,
            "status": "NOT_APPLICABLE_TO_CURRENT_OBJECTIVE",
            "misalignment_evidence_digest": evidence_digest,
            "maintenance_authority": maintenance_authority,
            "new_assessment_epoch_id": epoch_id,
            "new_attempt_id": new_attempt_id,
            "old_observation_preserved": True,
            "old_assessment_preserved": True,
            "old_gpt_decision_preserved": True,
        }
        self._validate_assessment_supersession(record, supersession_id=supersession_id)
        for existing_record in journal["assessment_supersessions"].values():
            if existing_record.get("assessment_id") == assessment_id:
                raise WorkflowV2ControllerError("assessment already has a different supersession")
        journal["assessment_supersessions"][supersession_id] = record
        runtime = self._runtime(journal, stage_id)
        if runtime.get("in_flight_operation_id") is not None:
            raise WorkflowV2ControllerError("assessment supersession requires no in-flight provider operation")
        runtime["current_attempt_id"] = None
        runtime["in_flight_operation_id"] = None
        runtime["execution_authorized"] = False
        runtime["current_assessment_epoch_id"] = epoch_id
        stage["current_assessment_id"] = None
        return {"stage": self.show_stage(stage_id, journal=journal), "supersession": _copy(record)}

    def _record_observation(self, journal: dict[str, Any], command: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        raw_observation = payload.get("observation", {})
        if not isinstance(raw_observation, Mapping):
            raise WorkflowV2ControllerError("observation must be an object")
        observation = _copy(dict(raw_observation))
        if command["subject_id"] != observation.get("stage_id"):
            raise WorkflowV2ControllerError("observation command subject does not match Stage")
        stage = self._stage(journal, observation["stage_id"])
        attempt = journal["attempts"].get(observation["attempt_id"])
        if attempt is None or attempt["stage_id"] != observation["stage_id"] or attempt["iteration_id"] != observation["iteration_id"]:
            raise WorkflowV2ControllerError("observation is not linked to the committed attempt")
        operation_id = self._runtime(journal, stage["stage_id"])["in_flight_operation_id"]
        if operation_id is None or operation_id not in journal["operations"]:
            raise WorkflowV2ControllerError("observation requires the attempt operation intent")
        operation = journal["operations"][operation_id]
        handoff = operation.get("provider_handoff_manifest") or {}
        expected_objective = attempt.get("objective_identity") or handoff.get("objective_identity")
        if expected_objective is not None:
            if observation.get("objective_identity") is not None and observation["objective_identity"] != expected_objective:
                raise WorkflowV2ControllerError("OBSERVATION_OBJECTIVE_MISMATCH")
            if observation.get("objective_identity") is None:
                observation["objective_identity"] = expected_objective
        expected_provider_operation = operation.get("operation_id")
        if expected_provider_operation is not None:
            if observation.get("provider_operation_id") is not None and observation["provider_operation_id"] != expected_provider_operation:
                raise WorkflowV2ControllerError("OBSERVATION_PROVIDER_OPERATION_MISMATCH")
            if observation.get("provider_operation_id") is None:
                observation["provider_operation_id"] = expected_provider_operation
        if observation.get("result_identity") is None and observation.get("provider_result_digest") is not None:
            observation["result_identity"] = observation["provider_result_digest"]
        elif observation.get("result_identity") != observation.get("provider_result_digest"):
            raise WorkflowV2ControllerError("OBSERVATION_RESULT_IDENTITY_MISMATCH")
        if observation.get("objective_identity") is not None and observation["objective_identity"] != stage_objective_identity(stage):
            raise WorkflowV2ControllerError("OBSERVATION_OBJECTIVE_MISMATCH")
        if any(key not in raw_observation for key in ("objective_identity", "provider_operation_id", "result_identity")):
            observation["observation_id"] = "pending"
            observation["observation_id"] = observation_identity(observation)
        validate_provider_observation(observation, require_objective_identity=expected_objective is not None)
        if observation["observation_id"] in journal["observations"]:
            existing = journal["observations"][observation["observation_id"]]
            if canonical_json(existing) != canonical_json(observation):
                raise WorkflowV2ControllerError("observation identity collision")
            return {"stage": self.show_stage(stage["stage_id"], journal=journal), "observation": _copy(existing)}
        effect_state = payload.get("effect_state")
        if effect_state is None:
            effect_state = observation.get("failure", {}).get("effect_state") if observation.get("failure") else "UNKNOWN"
        effect_state = _text(effect_state, "effect_state").upper()
        operation["effect_state"] = effect_state
        operation["status"] = "BLOCKED" if effect_state in {"UNKNOWN", "CONFLICT"} else "RECEIPT_OBSERVED"
        validate_operation_envelope(operation)
        attempt["effect_state"] = effect_state
        attempt["status"] = "OBSERVED" if observation["provider_terminal_status"] == "SUCCEEDED" else "FAILED"
        journal["observations"][observation["observation_id"]] = _copy(observation)
        self._runtime(journal, stage["stage_id"])["in_flight_operation_id"] = None
        return {"stage": self.show_stage(stage["stage_id"], journal=journal), "observation": _copy(observation), "operation": _copy(operation)}

    def _assess_result(self, journal: dict[str, Any], command: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        stage_id = command["subject_id"]
        stage = self._stage(journal, stage_id)
        self._require_status(stage, {"ACTIVE"})
        assessment = validate_stage_assessment(payload.get("assessment", {}))
        if assessment["stage_id"] != stage_id:
            raise WorkflowV2ControllerError("assessment Stage subject mismatch")
        if assessment["iteration_id"] != stage.get("current_iteration_id"):
            raise WorkflowV2ControllerError("assessment is not bound to the current semantic iteration")
        expected_objective = stage_objective_identity(stage)
        if assessment["correction_receipt_digest"] is not None:
            if assessment["revalidation"] is not True:
                raise WorkflowV2ControllerError("a correction receipt is only valid for revalidation")
            correction = validate_correction_receipt(payload.get("correction_receipt", {}))
            if sha256_json(correction) != assessment["correction_receipt_digest"]:
                raise WorkflowV2ControllerError("assessment correction receipt identity does not match")
            decision = journal["decisions"].get(correction["human_decision_id"])
            if (
                decision is None
                or decision.get("actor_kind") != "HUMAN"
                or decision.get("boundary") != "TECHNICAL_REVIEW"
                or decision.get("subject_type") != "VALIDATOR_CORRECTION"
                or decision.get("resolution") != "ACCEPT_CORRECTION"
            ):
                raise WorkflowV2ControllerError("validator correction lacks the exact accepted Human Decision")
        runtime = self._runtime(journal, stage_id)
        attempt = journal["attempts"].get(assessment["attempt_id"])
        observation = next(
            (
                item
                for item in journal["observations"].values()
                if item.get("provider_result_digest") == assessment["provider_result_digest"]
                and item.get("attempt_id") == assessment["attempt_id"]
                and item.get("stage_id") == assessment["stage_id"]
            ),
            None,
        )
        if attempt is None or observation is None:
            raise WorkflowV2ControllerError("assessment references an unknown attempt")
        if observation.get("objective_identity") is not None and observation["objective_identity"] != expected_objective:
            raise WorkflowV2ControllerError("OBSERVATION_OBJECTIVE_MISMATCH")
        if assessment.get("objective_identity") is not None:
            if assessment["objective_identity"] != expected_objective or (
                observation.get("objective_identity") is not None
                and assessment["objective_identity"] != observation["objective_identity"]
            ):
                raise WorkflowV2ControllerError("ASSESSMENT_OBJECTIVE_MISMATCH")
        if runtime["current_attempt_id"] != assessment["attempt_id"]:
            raise WorkflowV2ControllerError("only the latest committed attempt is the live candidate")
        stage_assessments = [
            item for item in journal["assessments"].values()
            if item.get("stage_id") == stage_id
        ]
        validator_versions = {item["validator_code_digest"] for item in stage_assessments}
        if assessment["validator_code_digest"] not in validator_versions and len(validator_versions) >= stage["budgets"]["max_validator_revisions"]:
            raise WorkflowV2ControllerError("validator revision budget exhausted")
        if assessment["revalidation"]:
            revalidation_count = sum(1 for item in stage_assessments if item.get("revalidation") is True)
            if revalidation_count >= stage["budgets"]["max_revalidation_ops"]:
                raise WorkflowV2ControllerError("revalidation budget exhausted")
            if assessment["supersedes_assessment_id"] is not None:
                superseded = journal["assessments"].get(assessment["supersedes_assessment_id"])
                if superseded is None or superseded["stage_id"] != stage_id or superseded["attempt_id"] != assessment["attempt_id"]:
                    raise WorkflowV2ControllerError("revalidation supersedes an unrelated assessment")
            elif any(item.get("attempt_id") == assessment["attempt_id"] for item in stage_assessments):
                raise WorkflowV2ControllerError("revalidation must identify the assessment it supersedes")
        operation_id = next((key for key, item in journal["operations"].items() if item["subject_id"] == assessment["attempt_id"]), None)
        if operation_id is not None and journal["operations"][operation_id]["effect_state"] in {"UNKNOWN", "CONFLICT"}:
            raise WorkflowV2ControllerError("unknown/conflicting external effect blocks assessment")
        if assessment["assessment_id"] in journal["assessments"]:
            existing = journal["assessments"][assessment["assessment_id"]]
            if canonical_json(existing) != canonical_json(assessment):
                raise WorkflowV2ControllerError("assessment identity collision")
            # A replay can arrive after a prior CONTINUE decision cleared the
            # projection, while the immutable assessment and latest attempt
            # are still the live candidate.  Re-bind that existing identity
            # so recovery can continue through the canonical review path;
            # never replace a different live assessment.
            if (
                stage.get("current_assessment_id") is None
                and runtime.get("current_attempt_id") == assessment["attempt_id"]
            ):
                stage["current_assessment_id"] = assessment["assessment_id"]
            return {"stage": self.show_stage(stage_id, journal=journal), "assessment": _copy(existing)}
        journal["assessments"][assessment["assessment_id"]] = _copy(assessment)
        stage["current_assessment_id"] = assessment["assessment_id"]
        return {"stage": self.show_stage(stage_id, journal=journal), "assessment": _copy(assessment), "observation_present": observation is not None}

    def _auto_advance_continue(
        self,
        journal: dict[str, Any],
        command: Mapping[str, Any],
        stage: Mapping[str, Any],
        decision: Mapping[str, Any],
        assessment: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Use the canonical iteration transition for an exhausted CONTINUE.

        This is deliberately a narrow projection seam.  A CONTINUE decision
        never creates an unbounded retry loop: both the per-iteration and
        total-attempt budgets, as well as the Stage iteration ceiling, remain
        authoritative.
        """

        if not isinstance(assessment, Mapping):
            return None
        stage_id = command["subject_id"]
        runtime = self._runtime(journal, stage_id)
        current = journal["iterations"].get(stage.get("current_iteration_id"))
        if current is None or runtime.get("in_flight_operation_id") is not None:
            return None
        if self._pending_decisions(journal, stage_id) or self._open_dependencies(journal, stage_id):
            return None
        if any(
            item.get("stage_id") == stage_id and item.get("resolved") is not True
            for item in journal.get("human_gates", [])
        ):
            return None
        if current["index"] >= stage["budgets"]["max_iterations"]:
            return None
        if len(self._budgeted_attempts_for(journal, stage_id, current["iteration_id"])) < stage["budgets"]["max_attempts_per_iteration"]:
            return None
        if len(self._budgeted_attempts_for(journal, stage_id)) >= stage["budgets"]["max_attempts_total"]:
            return None
        if assessment.get("stage_id") != stage_id or assessment.get("iteration_id") != current["iteration_id"]:
            return None
        if assessment.get("objective_identity") not in (None, stage_objective_identity(stage)):
            return None

        observation = next(
            (
                item for item in journal.get("observations", {}).values()
                if item.get("stage_id") == stage_id
                and item.get("attempt_id") == assessment.get("attempt_id")
                and item.get("provider_result_digest") == assessment.get("provider_result_digest")
            ),
            None,
        )
        if not isinstance(observation, Mapping):
            return None
        failure = observation.get("failure") if isinstance(observation.get("failure"), Mapping) else None
        try:
            blocker = classify_blocker(
                assessment=assessment,
                observation=observation,
                failure=failure,
                missing_thing_owner=assessment.get("missing_thing_owner") or observation.get("missing_thing_owner"),
                evidence_refs=[observation.get("evidence_manifest_digest")],
            )
        except (ContractValidationError, KeyError, TypeError, ValueError):
            return None
        if blocker.get("failure_class") == "EXTERNAL_BLOCKER" or blocker.get("recoverability") == "EXTERNAL_UNAVAILABLE":
            return None

        decision_id = str(decision["decision_id"])
        change_digest = continue_iteration_change_digest(
            decision_id=decision_id,
            assessment_id=str(assessment["assessment_id"]),
            iteration_id=current["iteration_id"],
            objective_identity=stage_objective_identity(stage),
        )
        return self._advance_iteration(
            journal,
            command,
            {
                "auto_advance": True,
                "requires_next_iteration": True,
                "review_identity": decision_id,
                "technical_change_digest": change_digest,
                "solution_fingerprint": stage_objective_identity(stage),
            },
        )

    def _apply_gpt_decision(self, journal: dict[str, Any], command: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        stage_id = command["subject_id"]
        stage = self._stage(journal, stage_id)
        self._require_status(stage, {"ACTIVE"})
        decision = validate_decision(payload.get("decision", {}))
        if decision["actor_kind"] != "GPT" or decision["boundary"] != "TECHNICAL_REVIEW":
            raise WorkflowV2ControllerError("APPLY_GPT_DECISION requires a Technical Review decision")
        assessment_id = stage.get("current_assessment_id")
        if assessment_id is None:
            # A maintenance revalidation may release the projection before a
            # newly obtained technical review is applied.  The old assessment
            # remains immutable and is recorded on the runtime pointer solely
            # to bind that one already-authorized review.
            assessment_id = self._runtime(journal, stage_id).get("last_blocker_assessment_id")
        if assessment_id is None or decision["subject_id"] != assessment_id:
            raise WorkflowV2ControllerError("GPT decision is not bound to the current assessment")
        assessment = journal["assessments"].get(assessment_id)
        expected_objective = stage_objective_identity(stage)
        if isinstance(assessment, Mapping) and assessment.get("objective_identity") is not None:
            if decision.get("objective_identity") != assessment["objective_identity"] or decision.get("objective_identity") != expected_objective:
                raise WorkflowV2ControllerError("GPT_PACKET_OBJECTIVE_MISMATCH")
        choice = _text(payload.get("choice"), "choice").upper()
        typed = None
        if choice == "REPLAN":
            subtype = _text(payload.get("replan_subtype"), "replan_subtype").upper()
            if f"REPLAN:{subtype}" not in decision["allowed_choices"]:
                raise WorkflowV2ControllerError("GPT decision does not authorize this exact typed REPLAN")
            typed = validate_typed_replan(decision, subtype)
        elif choice not in decision["allowed_choices"]:
            raise WorkflowV2ControllerError("GPT choice is outside the decision envelope")
        human_gate = None
        if choice == "HUMAN_GATE":
            human_gate = validate_human_gate(payload.get("human_gate", {}))
        if choice == "BLOCKED" and payload.get("strict_blocked_validation") is True:
            validate_genuine_blocked_evidence(payload.get("blocked_validation", {}))
        decision_record = {**_copy(decision), "resolution": choice}
        existing_decision = journal["decisions"].get(decision["decision_id"])
        if existing_decision is not None:
            if canonical_json(existing_decision) != canonical_json(decision_record):
                raise WorkflowV2ControllerError("Decision identity collision or conflicting resolution")
            return {"stage": self.show_stage(stage_id, journal=journal), "decision": _copy(existing_decision), "choice": choice}
        journal["decisions"][decision["decision_id"]] = decision_record
        if human_gate is not None:
            journal.setdefault("human_gates", []).append({
                "decision_id": decision["decision_id"],
                "stage_id": stage_id,
                "gate": _copy(dict(human_gate)),
                "resolved": False,
            })
        if choice == "STAGE_READY":
            assessment = journal["assessments"].get(assessment_id)
            if assessment is None or assessment.get("verdict") != "ADMISSIBLE":
                raise WorkflowV2ControllerError("STAGE_READY requires an ADMISSIBLE assessment")
            if self._pending_decisions(journal, stage_id) or self._open_dependencies(journal, stage_id):
                raise WorkflowV2ControllerError("pending Decision/Dependency blocks READY")
            stage["status"] = "READY"
            journal["executable_owner_stage_id"] = stage_id
        elif choice == "REPLAN":
            assert typed is not None
            subtype = typed["replan_subtype"]
            if subtype == "ENGINEERING_FIX":
                self._runtime(journal, stage_id)["execution_authorized"] = True
                stage["current_assessment_id"] = None
            elif subtype == "NEXT_ITERATION":
                self._advance_iteration(journal, command, {
                    "review_identity": decision["decision_id"],
                    "technical_change_digest": payload.get("technical_change_digest"),
                    "solution_fingerprint": payload.get("solution_fingerprint", stage["objective_fingerprint"]),
                    "requires_next_iteration": True,
                })
            else:
                self._runtime(journal, stage_id)["baseline_change_requested"] = True
                stage["current_assessment_id"] = None
            self._runtime(journal, stage_id).pop("last_blocker_assessment_id", None)
            return {"stage": self.show_stage(stage_id, journal=journal), "decision": typed}
        elif choice == "CONTINUE":
            stage["current_assessment_id"] = None
            self._runtime(journal, stage_id)["execution_authorized"] = True
            self._runtime(journal, stage_id).pop("last_blocker_assessment_id", None)
            advanced = self._auto_advance_continue(journal, command, stage, decision, assessment)
            result = {"stage": self.show_stage(stage_id, journal=journal), "decision": _copy(decision), "choice": choice}
            if advanced is not None:
                result["iteration"] = advanced["iteration"]
                result["auto_next_iteration"] = True
                result["stage"] = advanced["stage"]
            return result
        return {"stage": self.show_stage(stage_id, journal=journal), "decision": _copy(decision), "choice": choice}

    def _advance_iteration(self, journal: dict[str, Any], command: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        stage_id = command["subject_id"]
        stage = self._stage(journal, stage_id)
        self._require_status(stage, {"ACTIVE"})
        runtime = self._runtime(journal, stage_id)
        if runtime["in_flight_operation_id"] is not None:
            raise WorkflowV2ControllerError("in-flight operation blocks iteration advance")
        current = journal["iterations"].get(stage.get("current_iteration_id"))
        if current is None:
            raise WorkflowV2ControllerError("no current iteration")
        if current["index"] >= stage["budgets"]["max_iterations"]:
            raise WorkflowV2ControllerError("iteration budget exhausted")
        if payload.get("requires_next_iteration") is not True:
            raise WorkflowV2ControllerError("ADVANCE_ITERATION requires explicit semantic change")
        review_identity = payload.get("review_identity")
        technical_change_digest = payload.get("technical_change_digest")
        if not review_identity or not technical_change_digest:
            raise WorkflowV2ControllerError("iteration advance requires review and technical-change identities")
        review = journal["decisions"].get(review_identity)
        if review is None or review.get("actor_kind") != "GPT" or review.get("boundary") != "TECHNICAL_REVIEW":
            raise WorkflowV2ControllerError("iteration advance requires a resolved GPT technical review")
        resolution = review.get("resolution")
        automatic_continue = resolution == "CONTINUE" and payload.get("auto_advance") is True
        if resolution != "REPLAN" and not automatic_continue:
            raise WorkflowV2ControllerError("iteration advance requires a resolved GPT REPLAN review or automatic CONTINUE")
        if automatic_continue:
            assessment_id = review.get("subject_id")
            expected_digest = continue_iteration_change_digest(
                decision_id=review_identity,
                assessment_id=str(assessment_id),
                iteration_id=current["iteration_id"],
                objective_identity=stage_objective_identity(stage),
            )
            if technical_change_digest != expected_digest:
                raise WorkflowV2ControllerError("automatic CONTINUE iteration change is not bound to the review")
            if len(self._budgeted_attempts_for(journal, stage_id, current["iteration_id"])) < stage["budgets"]["max_attempts_per_iteration"]:
                raise WorkflowV2ControllerError("automatic CONTINUE requires an exhausted iteration attempt budget")
            if len(self._budgeted_attempts_for(journal, stage_id)) >= stage["budgets"]["max_attempts_total"]:
                raise WorkflowV2ControllerError("automatic CONTINUE exceeds the total attempt budget")
        iteration_id = _id("iteration", command["command_id"] + "-" + str(current["index"] + 1))
        iteration = {
            "schema_version": "semantic_iteration.v2",
            "iteration_id": iteration_id,
            "stage_id": stage_id,
            "index": current["index"] + 1,
            "solution_fingerprint": payload.get("solution_fingerprint", stage["objective_fingerprint"]),
            "opened_by": "ADVANCE_ITERATION",
            "requires_next_iteration": True,
            "review_identity": review_identity,
            "technical_change_digest": technical_change_digest,
        }
        validate_semantic_iteration(iteration)
        journal["iterations"][iteration_id] = iteration
        stage["current_iteration_id"] = iteration_id
        stage["current_assessment_id"] = None
        if automatic_continue:
            runtime["execution_authorized"] = True
        return {"stage": self.show_stage(stage_id, journal=journal), "iteration": _copy(iteration)}

    def _request_decision(self, journal: dict[str, Any], command: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        decision = validate_decision(payload.get("decision", {}))
        if decision["actor_kind"] != "HUMAN":
            raise WorkflowV2ControllerError("REQUEST_DECISION is reserved for Human decisions")
        if command["subject_id"] != decision["subject_id"]:
            raise WorkflowV2ControllerError("Decision command subject does not match the decision subject")
        if decision["decision_id"] in journal["decisions"]:
            existing = journal["decisions"][decision["decision_id"]]
            if canonical_json(_decision_envelope(existing)) != canonical_json(decision):
                raise WorkflowV2ControllerError("Decision identity collision")
            return {"decision": _copy(existing)}
        journal["decisions"][decision["decision_id"]] = {**_copy(decision), "resolution": None}
        return {"decision": _copy(journal["decisions"][decision["decision_id"]])}

    def _apply_decision(self, journal: dict[str, Any], command: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        decision_id = _text(payload.get("decision_id"), "decision_id")
        decision = journal["decisions"].get(decision_id)
        if decision is None:
            raise WorkflowV2ControllerError("Decision is not pending in the journal")
        if command["subject_id"] != decision["subject_id"]:
            raise WorkflowV2ControllerError("Decision apply subject does not match the decision subject")
        if decision.get("resolution") is not None:
            if decision["resolution"] == payload.get("choice"):
                return {"decision": _copy(decision)}
            raise WorkflowV2ControllerError("conflicting Decision resolution")
        subject_digest = _text(payload.get("subject_digest"), "subject_digest")
        subject_version = payload.get("subject_version")
        validate_decision_subject(_decision_envelope(decision), subject_id=decision["subject_id"], subject_digest=subject_digest, subject_version=subject_version)
        choice = _text(payload.get("choice"), "choice").upper()
        if choice not in decision["allowed_choices"]:
            raise WorkflowV2ControllerError("Human choice is outside the decision envelope")
        decision["resolution"] = choice
        if decision["boundary"] == "TECHNICAL_REVIEW" and decision.get("subject_type") == "VALIDATOR_CORRECTION":
            if choice == "ACCEPT_CORRECTION":
                journal["correction_decisions"][decision["subject_id"]] = _copy(decision)
        return {"decision": _copy(decision)}

    def _add_dependency(self, journal: dict[str, Any], command: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        edge = validate_dependency(payload.get("dependency", {}))
        if command["subject_id"] != edge["parent_id"]:
            raise WorkflowV2ControllerError("dependency command subject does not match the parent Stage")
        parent = self._stage(journal, edge["parent_id"])
        child = validate_stage(payload.get("child_stage", {}))
        if child["stage_id"] != edge["child_id"] or child["status"] != "PLANNED":
            raise WorkflowV2ControllerError("dependency child must be a new PLANNED Stage")
        if child["workspace_id"] != self.workspace_id:
            raise WorkflowV2ControllerError("dependency child workspace identity does not match the journal")
        if child.get("owner_stage_id") is not None or child.get("current_iteration_id") is not None or child.get("current_assessment_id") is not None:
            raise WorkflowV2ControllerError("dependency child must have no pre-existing lifecycle ownership")
        self._require_status(parent, {"PLANNED", "ACTIVE"})
        if edge["child_id"] in journal["stages"]:
            raise WorkflowV2ControllerError("dependency child identity already exists")
        authorization = journal["decisions"].get(edge["authorization_decision_id"])
        if authorization is None:
            raise WorkflowV2ControllerError("dependency requires a Human planning Decision")
        try:
            validate_decision_subject(
                _decision_envelope(authorization),
                subject_id=edge["child_id"],
                subject_digest=dependency_authorization_digest(edge),
                subject_version=1,
            )
        except ContractValidationError as exc:
            raise WorkflowV2ControllerError("dependency authorization Decision is not bound to this edge") from exc
        if (
            authorization.get("actor_kind") != "HUMAN"
            or authorization.get("boundary") != "STAGE_PLANNING"
            or authorization.get("subject_type") != "DEPENDENCY"
            or authorization.get("resolution") != "ACCEPT_DEPENDENCY"
        ):
            raise WorkflowV2ControllerError("dependency requires an accepted Human DEPENDENCY Decision")
        if self._runtime(journal, edge["parent_id"])["in_flight_operation_id"] is not None:
            raise WorkflowV2ControllerError("in-flight parent operation blocks dependency insertion")
        if journal.get("executable_owner_stage_id") not in (None, edge["parent_id"]):
            raise WorkflowV2ControllerError("another Stage owns the executable token")
        if self._open_dependencies(journal, edge["parent_id"]):
            raise WorkflowV2ControllerError("parent already has an open dependency child")
        dependency_fields = ("schema_version", "parent_id", "child_id", "predicate", "input_binding", "output_contract_digest", "authorization_decision_id", "child_status")
        existing = [
            {key: item[key] for key in dependency_fields}
            for item in journal["dependencies"].values()
        ] + [edge]
        parent_budget = parent["budgets"]
        validate_dependency_graph(existing, max_nodes=parent_budget["max_dependency_nodes"], max_depth=parent_budget["max_dependency_depth"])
        if parent["status"] == "ACTIVE" and journal.get("executable_owner_stage_id") != edge["parent_id"]:
            raise WorkflowV2ControllerError("ACTIVE parent must own token before transferring to child")
        journal["stages"][child["stage_id"]] = _copy(child)
        journal["stage_runtime"][child["stage_id"]] = {"current_attempt_id": None, "in_flight_operation_id": None, "execution_authorized": False, "integration_operation_id": None, "closeout": None}
        journal["dependencies"][edge["child_id"]] = {**_copy(edge), "status": "OPEN"}
        journal["executable_owner_stage_id"] = edge["child_id"]
        return {"stage": self.show_stage(edge["parent_id"], journal=journal), "child": self.show_stage(edge["child_id"], journal=journal), "dependency": _copy(journal["dependencies"][edge["child_id"]])}

    def _satisfy_dependency(self, journal: dict[str, Any], command: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        child_id = _text(payload.get("child_id", command["subject_id"]), "child_id")
        edge = journal["dependencies"].get(child_id)
        if edge is None or edge["status"] != "OPEN":
            raise WorkflowV2ControllerError("dependency is not open")
        if command["subject_id"] != edge["parent_id"]:
            raise WorkflowV2ControllerError("dependency satisfaction subject does not match the parent Stage")
        child = self._stage(journal, child_id)
        parent = self._stage(journal, edge["parent_id"])
        self._require_status(parent, {"PLANNED", "ACTIVE"})
        if child["status"] != "CLOSED":
            raise WorkflowV2ControllerError("only a CLOSED child can satisfy a dependency")
        output_digest = _text(payload.get("output_contract_digest"), "output_contract_digest")
        if output_digest != edge["output_contract_digest"]:
            raise WorkflowV2ControllerError("child closeout does not match dependency output contract")
        closeout_identity = _text(payload.get("closeout_identity"), "closeout_identity")
        closeout = self._runtime(journal, child_id).get("closeout")
        if not isinstance(closeout, Mapping) or closeout.get("closeout_id") != closeout_identity:
            raise WorkflowV2ControllerError("dependency satisfaction must bind the child's committed closeout")
        edge["status"] = "SATISFIED"
        edge["satisfied_by"] = closeout_identity
        journal["executable_owner_stage_id"] = edge["parent_id"]
        return {"parent": self.show_stage(parent["stage_id"], journal=journal), "child": self.show_stage(child_id, journal=journal), "dependency": _copy(edge)}

    def _revalidate_blocker(self, journal: dict[str, Any], command: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        """Re-evaluate a historical technical/scientific BLOCKED projection.

        This is deliberately a command, not a new Stage state.  The original
        assessment, observation, and GPT decision remain immutable; a new
        append-only record either releases the current attempt for one bounded
        recovery route or leaves the historical blocker authoritative.
        """

        stage_id = command["subject_id"]
        stage = self._stage(journal, stage_id)
        self._require_status(stage, {"ACTIVE"})
        assessment_id = stage.get("current_assessment_id")
        if not isinstance(assessment_id, str) or not assessment_id:
            raise WorkflowV2ControllerError("blocker revalidation requires the current assessment")
        blocked = self._resolved_gpt_decision(journal, assessment_id)
        if blocked is None or blocked.get("resolution") != "BLOCKED":
            raise WorkflowV2ControllerError("blocker revalidation requires the preserved GPT BLOCKED decision")
        record = payload.get("blocker_record")
        if not isinstance(record, Mapping):
            raise WorkflowV2ControllerError("blocker revalidation requires blocker metadata")
        record = validate_blocker_record(record)
        if record.get("stage_id") != stage_id or record.get("project_id") != stage.get("project_id") or record.get("assessment_id") != assessment_id:
            raise WorkflowV2ControllerError("blocker metadata is not bound to the current Stage assessment")
        if record.get("revision") != journal.get("revision"):
            raise WorkflowV2ControllerError("blocker metadata was created from a stale journal revision")
        still_true = str(payload.get("blocker_still_true", "")).upper()
        if still_true not in BLOCKER_STILL_TRUE:
            raise WorkflowV2ControllerError("blocker_still_true must be YES, NO, or UNKNOWN")
        revalidations = journal.setdefault("blocker_revalidations", [])
        stage_revalidations = [item for item in revalidations if item.get("stage_id") == stage_id]
        if len(stage_revalidations) >= stage["budgets"]["max_revalidation_ops"]:
            raise WorkflowV2ControllerError("blocker revalidation budget exhausted")
        released_attempt_id = payload.get("released_attempt_id")
        current_attempt_id = self._runtime(journal, stage_id).get("current_attempt_id")
        if still_true == "NO":
            if released_attempt_id != current_attempt_id:
                raise WorkflowV2ControllerError("a NO blocker revalidation must release the current attempt")
            if self._runtime(journal, stage_id).get("in_flight_operation_id") is not None:
                raise WorkflowV2ControllerError("blocker revalidation cannot release an in-flight operation")
        revalidation_id = payload.get("revalidation_id") or _id("blocker-revalidation", command["command_id"])
        if any(item.get("revalidation_id") == revalidation_id for item in revalidations):
            existing = next(item for item in revalidations if item.get("revalidation_id") == revalidation_id)
            return {"stage": self.show_stage(stage_id, journal=journal), "blocker_record": _copy(record), "revalidation": _copy(existing)}
        now = _utc_now()
        initial = _copy(dict(record))
        initial["resolved_at"] = None
        validate_blocker_record(initial)
        records = journal.setdefault("blocker_records", [])
        records.append(initial)
        revalidation = {
            "schema_version": "blocker_revalidation.v1",
            "revalidation_id": revalidation_id,
            "blocker_id": initial["blocker_id"],
            "stage_id": stage_id,
            "assessment_id": assessment_id,
            "blocker_still_true": still_true,
            "recoverability": initial["recoverability"],
            "validated_at": now,
            "evidence_refs": _copy(initial["evidence_refs"]),
        }
        if released_attempt_id is not None:
            revalidation["released_attempt_id"] = released_attempt_id
        revalidations.append(revalidation)
        latest = _copy(initial)
        latest["last_validated_at"] = now
        if still_true == "NO":
            latest["resolved_at"] = now
            latest["resolution"] = "STALE_BLOCKER_REASSESSED"
            records.append(latest)
            runtime = self._runtime(journal, stage_id)
            runtime["execution_authorized"] = True
            runtime["last_blocker_revalidation_id"] = revalidation_id
            runtime["last_blocker_assessment_id"] = assessment_id
            stage["current_assessment_id"] = None
        else:
            runtime = self._runtime(journal, stage_id)
            runtime["last_blocker_revalidation_id"] = revalidation_id
        return {
            "stage": self.show_stage(stage_id, journal=journal),
            "blocker_record": _copy(latest),
            "revalidation": _copy(revalidation),
        }

    def _resolve_blocker(self, journal: dict[str, Any], command: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(payload.get("blocker_record"), Mapping):
            return self._revalidate_blocker(journal, command, payload)
        blocker_id = _text(payload.get("blocker_id"), "blocker_id")
        if blocker_id not in journal["blockers"]:
            raise WorkflowV2ControllerError("unknown blocker")
        blocker = journal["blockers"][blocker_id]
        if blocker.get("stage_id") != command["subject_id"]:
            raise WorkflowV2ControllerError("blocker belongs to a different Stage")
        if blocker.get("type") == "UNKNOWN_EXTERNAL_EFFECT":
            raise WorkflowV2ControllerError("external-effect blockers require APPLY_RECEIPT proof")
        if payload.get("predicate_satisfied") is not True:
            raise WorkflowV2ControllerError("typed blocker predicate is not satisfied")
        blocker = journal["blockers"].pop(blocker_id)
        return {"blocker": _copy(blocker), "stage": self.show_stage(command["subject_id"], journal=journal)}

    def _apply_receipt(self, journal: dict[str, Any], command: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        operation_id = _text(payload.get("operation_id"), "operation_id")
        operation = journal["operations"].get(operation_id)
        if operation is None:
            raise WorkflowV2ControllerError("receipt has no committed operation intent")
        operation_stage_id = operation["subject_id"]
        if operation_stage_id in journal["attempts"]:
            operation_stage_id = journal["attempts"][operation_stage_id]["stage_id"]
        if operation_stage_id != command["subject_id"]:
            raise WorkflowV2ControllerError("receipt command subject does not match the operation Stage")
        effect_state = _text(payload.get("effect_state"), "effect_state").upper()
        if effect_state not in {"NOT_SENT_PROVEN", "SETTLED", "UNKNOWN", "CONFLICT", "SENT_UNSETTLED"}:
            raise WorkflowV2ControllerError("receipt contains an unsupported external effect state")
        receipt = payload.get("receipt", {})
        if not isinstance(receipt, Mapping):
            raise WorkflowV2ControllerError("receipt must be an object")
        receipt_digest = sha256_json(receipt)
        if payload.get("receipt_digest") is not None and payload["receipt_digest"] != receipt_digest:
            raise WorkflowV2ControllerError("receipt_digest does not match receipt bytes")
        if effect_state in TERMINAL_EFFECTS and not payload.get("settlement_proof"):
            raise WorkflowV2ControllerError("terminal external effects require explicit settlement proof")
        if effect_state == "NOT_SENT_PROVEN" and operation["capability_manifest"].get("prove_not_sent") is not True:
            raise WorkflowV2ControllerError("NOT_SENT_PROVEN requires a capability-backed proof")
        if operation.get("effect_state") in {"UNKNOWN", "CONFLICT"} and effect_state in {"NOT_SENT_PROVEN", "SETTLED"}:
            if not payload.get("settlement_proof"):
                raise WorkflowV2ControllerError("UNKNOWN/CONFLICT effects require explicit settlement proof")
        if operation.get("effect_state") in {"NOT_SENT_PROVEN", "SETTLED"} and effect_state != operation["effect_state"]:
            raise WorkflowV2ControllerError("terminal external effect cannot regress or change settlement")
        operation["effect_state"] = effect_state
        operation["status"] = "BLOCKED" if effect_state in {"UNKNOWN", "CONFLICT"} else ("SETTLED" if effect_state == "SETTLED" else "RECEIPT_OBSERVED")
        operation["receipt_digest"] = receipt_digest
        operation["receipt"] = _copy(receipt)
        if effect_state in {"UNKNOWN", "CONFLICT"}:
            journal["blockers"][operation_id] = {"blocker_id": operation_id, "stage_id": command["subject_id"], "type": "UNKNOWN_EXTERNAL_EFFECT", "operation_id": operation_id}
        else:
            journal["blockers"].pop(operation_id, None)
        return {"operation": _copy(operation), "stage": self.show_stage(command["subject_id"], journal=journal)}

    def _commit_integration(self, journal: dict[str, Any], command: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        stage_id = command["subject_id"]
        stage = self._stage(journal, stage_id)
        self._require_status(stage, {"READY"})
        if self._pending_decisions(journal, stage_id):
            raise WorkflowV2ControllerError("pending Decision blocks integration")
        runtime = self._runtime(journal, stage_id)
        if runtime["integration_operation_id"] is not None:
            operation = journal["operations"][runtime["integration_operation_id"]]
            return {"stage": self.show_stage(stage_id, journal=journal), "operation": _copy(operation)}
        operation_id = payload.get("operation_id") or _id("operation", command["command_id"])
        operation = {
            "schema_version": "operation_envelope.v2",
            "operation_id": operation_id,
            "workspace_id": self.workspace_id,
            "subject_id": stage_id,
            "intent_digest": payload.get("target_manifest_digest") or sha256_json(payload.get("target_manifest", {})),
            "effect_state": "INTENT_COMMITTED",
            "capability_manifest": _copy(payload.get("capability_manifest", {"query_by_operation_id": True, "idempotent_submit": True, "fence": True, "prove_not_sent": True})),
            "status": "INTENT",
        }
        validate_operation_envelope(operation)
        journal["operations"][operation_id] = operation
        runtime["integration_operation_id"] = operation_id
        return {"stage": self.show_stage(stage_id, journal=journal), "operation": _copy(operation)}

    def _closeout(self, journal: dict[str, Any], command: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        stage_id = command["subject_id"]
        stage = self._stage(journal, stage_id)
        self._require_status(stage, {"READY"})
        runtime = self._runtime(journal, stage_id)
        operation_id = runtime["integration_operation_id"]
        if operation_id is None:
            if payload.get("no_integration_required") is not True:
                raise WorkflowV2ControllerError("CLOSEOUT requires integration receipt or explicit N/A")
        elif journal["operations"][operation_id]["effect_state"] != "SETTLED":
            raise WorkflowV2ControllerError("integration effect is not settled")
        closeout_id = payload.get("closeout_id") or _id("closeout", command["command_id"])
        runtime["closeout"] = {"closeout_id": closeout_id, "verification_digest": payload.get("verification_digest") or sha256_json(payload.get("verification", {})), "integration_operation_id": operation_id}
        stage["status"] = "CLOSED"
        stage["owner_stage_id"] = None
        if journal.get("executable_owner_stage_id") == stage_id:
            journal["executable_owner_stage_id"] = None
        return {"stage": self.show_stage(stage_id, journal=journal), "closeout": _copy(runtime["closeout"])}

    def _stop(self, journal: dict[str, Any], command: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        stage_id = command["subject_id"]
        stage = self._stage(journal, stage_id)
        self._require_status(stage, {"PLANNED", "ACTIVE", "READY"})
        for operation in journal["operations"].values():
            operation_stage = operation.get("subject_id") == stage_id or any(
                attempt.get("attempt_id") == operation.get("subject_id") and attempt.get("stage_id") == stage_id
                for attempt in journal["attempts"].values()
            )
            if operation_stage and operation.get("effect_state") in UNSETTLED_EFFECTS:
                raise WorkflowV2ControllerError("STOP requires all external effects settled or proven not sent")
        stage["status"] = "STOPPED"
        stage["owner_stage_id"] = None
        if journal.get("executable_owner_stage_id") == stage_id:
            journal["executable_owner_stage_id"] = None
        for edge in journal["dependencies"].values():
            if edge.get("parent_id") == stage_id and edge.get("status") == "OPEN":
                edge["status"] = "TERMINATED"
        for decision in journal["decisions"].values():
            if decision.get("subject_id") == stage_id and decision.get("resolution") is None:
                decision["resolution"] = "STOP_SUPERSEDED"
        return {"stage": self.show_stage(stage_id, journal=journal)}

    def show_stage(self, stage_id: str | None = None, *, journal: Mapping[str, Any] | None = None) -> dict[str, Any]:
        source = self._journal if journal is None else journal
        if stage_id is None:
            stage_id = source.get("executable_owner_stage_id")
        if stage_id is None:
            open_stages = [
                stage
                for stage in source.get("stages", {}).values()
                if stage.get("status") not in {"CLOSED", "STOPPED"}
            ]
            if open_stages:
                open_stages.sort(key=lambda item: (0 if item.get("status") in {"ACTIVE", "READY"} else 1, item["stage_id"]))
                stage_id = open_stages[0]["stage_id"]
        if stage_id is None:
            return {"status": None, "stage_id": None, "next_action": "REGISTER_STAGE"}
        stage = _copy(self._stage(source, stage_id))
        runtime = source["stage_runtime"].get(stage_id, {})
        attempts = self._attempts_for(source, stage_id)
        stage["attempt_count"] = len(attempts)
        stage["iteration_count"] = sum(1 for item in source["iterations"].values() if item.get("stage_id") == stage_id)
        stage["pending_decisions"] = [decision["decision_id"] for decision in self._pending_decisions(source, stage_id)]
        stage["open_dependencies"] = [edge["child_id"] for edge in self._open_dependencies(source, stage_id)]
        stage["in_flight_operation_id"] = runtime.get("in_flight_operation_id")
        stage["owner_token"] = source.get("executable_owner_stage_id") == stage_id
        if stage["status"] == "PLANNED":
            stage["next_action"] = "START" if not stage["open_dependencies"] and not stage["pending_decisions"] else "WAIT"
        elif stage["status"] == "ACTIVE":
            if stage["pending_decisions"]:
                stage["next_action"] = "APPLY_DECISION"
            elif runtime.get("in_flight_operation_id"):
                stage["next_action"] = (
                    "RESOLVE_LEGACY_ORPHAN"
                    if self._legacy_orphan_operation(source, stage_id) is not None
                    else "RECORD_OBSERVATION"
                )
            elif stage.get("current_assessment_id"):
                stage["next_action"] = "APPLY_GPT_DECISION"
            elif (
                len(self._budgeted_attempts_for(source, stage_id)) >= stage["budgets"]["max_attempts_total"]
                or len(self._budgeted_attempts_for(source, stage_id, stage.get("current_iteration_id")))
                >= stage["budgets"]["max_attempts_per_iteration"]
            ):
                stage["next_action"] = "ASSESS_RESULT"
            else:
                stage["next_action"] = "REQUEST_EXECUTION"
        elif stage["status"] == "READY":
            stage["next_action"] = "CLOSEOUT" if runtime.get("integration_operation_id") else "COMMIT_INTEGRATION"
        else:
            stage["next_action"] = None
        return stage

    @staticmethod
    def _resolved_gpt_decision(journal: Mapping[str, Any], assessment_id: str | None) -> dict[str, Any] | None:
        if not isinstance(assessment_id, str) or not assessment_id:
            return None
        matches = [
            decision
            for decision in journal.get("decisions", {}).values()
            if (
                decision.get("actor_kind") == "GPT"
                and decision.get("boundary") == "TECHNICAL_REVIEW"
                and decision.get("subject_id") == assessment_id
                and decision.get("resolution") is not None
            )
        ]
        return _copy(matches[-1]) if matches else None

    def resume_projection(self) -> dict[str, Any]:
        stage = self.show_stage()
        next_action = stage.get("next_action")
        actor = "Human" if next_action in {"APPLY_DECISION"} else ("Provider" if next_action == "RECORD_OBSERVATION" else "Controller")
        projection = {"workspace_id": self.workspace_id, "revision": self.revision, "stage": stage, "phase": "WAITING" if next_action == "WAIT" else "RUNNING", "next_action": next_action, "next_actor": actor}
        decision = self._resolved_gpt_decision(self._journal, stage.get("current_assessment_id"))
        if decision is not None:
            # This is a read-only presentation projection.  The lifecycle
            # authority remains the Stage and journal decision records.
            projection["gpt_decision"] = decision["resolution"]
            projection["gpt_decision_id"] = decision["decision_id"]
            if decision["resolution"] == "HUMAN_GATE":
                projection["next_action"] = "HUMAN_GATE"
                projection["next_actor"] = "Human"
                gate = next(
                    (item.get("gate") for item in self._journal.get("human_gates", [])
                     if item.get("decision_id") == decision["decision_id"] and item.get("resolved") is not True),
                    None,
                )
                if gate is not None:
                    projection["human_gate"] = _copy(gate)
        return projection

    def register_stage(self, stage: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
        payload = build_registration_payload(stage)
        return self.dispatch("REGISTER_STAGE", subject_id=payload["stage"]["stage_id"], payload=payload, **kwargs)

    def start(self, stage_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.dispatch("START", subject_id=stage_id, payload=kwargs.pop("payload", {}), **kwargs)

    def request_execution(self, stage_id: str, **kwargs: Any) -> dict[str, Any]:
        payload = _copy(kwargs.pop("payload", {}))
        payload.update({key: kwargs.pop(key) for key in tuple(kwargs) if key in {"request", "request_id", "attempt_id", "request_digest", "provenance", "purpose", "reason", "operation_id", "capability_manifest", "provider_handoff_manifest"}})
        command_id = kwargs.pop("command_id", None) or _new_command_id()
        if "provider_handoff_manifest" not in payload:
            stage = self.resolve_canonical_stage(stage_id)
            iteration_id = stage.get("current_iteration_id")
            if not isinstance(iteration_id, str) or not iteration_id:
                raise WorkflowV2ControllerError("execution requires an opened semantic iteration")
            request = payload.get("request", {})
            request_id = payload.get("request_id") or _id("request", command_id)
            attempt_id = payload.get("attempt_id")
            if attempt_id is None and str(payload.get("reason", "")).upper() == "OBJECTIVE_SUPERSESSION_RECOVERY":
                epoch_id = self.state.get("stage_runtime", {}).get(stage_id, {}).get("current_assessment_epoch_id")
                attempt_id = next(
                    (
                        item.get("new_attempt_id")
                        for item in self.state.get("assessment_supersessions", {}).values()
                        if item.get("stage_id") == stage_id and item.get("new_assessment_epoch_id") == epoch_id
                    ),
                    None,
                )
            attempt_id = attempt_id or _id("attempt", command_id)
            operation_id = payload.get("operation_id") or _id("operation", command_id)
            provenance = payload.get("provenance", {"provider": "fixture-provider", "engine_digest": "engine-fixed-v2"})
            purpose = payload.get("purpose", stage["purpose"])
            payload["provider_handoff_manifest"] = build_provider_handoff_manifest(
                operation_id=operation_id,
                stage_id=stage_id,
                iteration_id=iteration_id,
                attempt_id=attempt_id,
                request_id=request_id,
                request=request,
                provenance=provenance,
                purpose=purpose,
                project_id=stage["project_id"],
                objective_identity=stage_objective_identity(stage),
            )
        return self.dispatch("REQUEST_EXECUTION", subject_id=stage_id, payload=payload, command_id=command_id, **kwargs)

    def resolve_legacy_orphan(self, stage_id: str, **kwargs: Any) -> dict[str, Any]:
        payload = _copy(kwargs.pop("payload", {}))
        payload.update({key: kwargs.pop(key) for key in tuple(kwargs) if key in {"operation_id", "effect_class", "side_effect_audit", "resolution_id"}})
        return self.dispatch("RESOLVE_LEGACY_ORPHAN", subject_id=stage_id, payload=payload, **kwargs)

    def record_observation(self, stage_id: str, observation: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
        result = self.dispatch("RECORD_OBSERVATION", subject_id=stage_id, payload={"observation": observation, **{key: kwargs.pop(key) for key in tuple(kwargs) if key in {"effect_state"}}}, **kwargs)
        # Older callers may construct a pre-binding observation object.  The
        # controller canonicalizes that object from the committed handoff; if
        # it is mutable, reflect the canonical immutable identity back to the
        # caller so later assessment/decision code cannot retain a stale key.
        canonical = result.get("observation")
        if isinstance(observation, dict) and isinstance(canonical, Mapping):
            observation.clear()
            observation.update(_copy(dict(canonical)))
        return result

    def assess_result(self, stage_id: str, assessment: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
        payload = {"assessment": assessment}
        if "correction_receipt" in kwargs:
            payload["correction_receipt"] = kwargs.pop("correction_receipt")
        return self.dispatch("ASSESS_RESULT", subject_id=stage_id, payload=payload, **kwargs)

    def supersede_assessment(self, stage_id: str, **kwargs: Any) -> dict[str, Any]:
        payload = _copy(kwargs.pop("payload", {}))
        payload.update({key: kwargs.pop(key) for key in tuple(kwargs) if key in {
            "assessment_id", "consultation_id", "reason", "misalignment_evidence_digest",
            "misalignment_proof", "maintenance_authority", "supersession_id", "new_assessment_epoch_id", "new_attempt_id",
        }})
        return self.dispatch("SUPERSEDE_ASSESSMENT", subject_id=stage_id, payload=payload, **kwargs)

    def apply_gpt_decision(self, stage_id: str, decision: Mapping[str, Any], *, choice: str, **kwargs: Any) -> dict[str, Any]:
        if isinstance(decision, dict) and decision.get("objective_identity") is None:
            command_id = kwargs.get("command_id")
            prior = self.state.get("commands", {}).get(command_id) if command_id else None
            prior_decision = prior.get("receipt", {}).get("decision") if isinstance(prior, Mapping) else None
            if isinstance(prior_decision, Mapping) and prior_decision.get("objective_identity") is not None:
                decision["objective_identity"] = prior_decision["objective_identity"]
            else:
                stage = self.resolve_canonical_stage(stage_id)
                assessment_id = stage.get("current_assessment_id")
                if assessment_id is None:
                    assessment_id = self._journal.get("stage_runtime", {}).get(stage_id, {}).get("last_blocker_assessment_id")
                assessment = self.state.get("assessments", {}).get(assessment_id)
                if isinstance(assessment, Mapping) and assessment.get("objective_identity") is not None:
                    decision["objective_identity"] = assessment["objective_identity"]
        payload = {"decision": decision, "choice": choice, **{key: kwargs.pop(key) for key in tuple(kwargs) if key in {
            "replan_subtype", "technical_change_digest", "solution_fingerprint", "human_gate",
            "strict_blocked_validation", "blocked_validation",
        }}}
        return self.dispatch("APPLY_GPT_DECISION", subject_id=stage_id, payload=payload, **kwargs)

    def advance_iteration(self, stage_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.dispatch("ADVANCE_ITERATION", subject_id=stage_id, payload=kwargs.pop("payload", kwargs.pop("iteration", {})), **kwargs)

    def attempt_budget_status(self, stage_id: str, *, iteration_id: str | None = None) -> dict[str, Any]:
        """Expose canonical counted-attempt accounting for audit and resume."""

        stage = self._stage(self._journal, stage_id)
        selected_iteration_id = iteration_id or stage.get("current_iteration_id")
        current_iteration = self._journal.get("iterations", {}).get(selected_iteration_id)
        iteration_attempts = self._budgeted_attempts_for(self._journal, stage_id, selected_iteration_id)
        total_attempts = self._budgeted_attempts_for(self._journal, stage_id)
        return {
            "stage_id": stage_id,
            "iteration_id": selected_iteration_id,
            "counted_attempt_ids": [item["attempt_id"] for item in iteration_attempts],
            "counted_attempts": len(iteration_attempts),
            "per_iteration_budget": stage["budgets"]["max_attempts_per_iteration"],
            "per_iteration_exhausted": len(iteration_attempts) >= stage["budgets"]["max_attempts_per_iteration"],
            "total_counted_attempt_ids": [item["attempt_id"] for item in total_attempts],
            "total_counted_attempts": len(total_attempts),
            "total_attempt_budget": stage["budgets"]["max_attempts_total"],
            "total_exhausted": len(total_attempts) >= stage["budgets"]["max_attempts_total"],
            "iteration_index": current_iteration.get("index") if isinstance(current_iteration, Mapping) else None,
            "max_iterations": stage["budgets"]["max_iterations"],
            "max_iteration_exhausted": (
                isinstance(current_iteration, Mapping)
                and current_iteration.get("index", 0) >= stage["budgets"]["max_iterations"]
            ),
        }

    def request_decision(self, decision: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
        return self.dispatch("REQUEST_DECISION", subject_id=decision["subject_id"], payload={"decision": decision}, **kwargs)

    def apply_decision(self, stage_id: str, *, payload: Mapping[str, Any] | None = None, command_id: str | None = None, expected_revision: int | None = None, **kwargs: Any) -> dict[str, Any]:
        return self.dispatch("APPLY_DECISION", subject_id=stage_id, payload=_copy(payload if payload is not None else kwargs), command_id=command_id, expected_revision=expected_revision)

    def add_dependency(self, parent_id: str, dependency: Mapping[str, Any], child_stage: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
        return self.dispatch("ADD_DEPENDENCY", subject_id=parent_id, payload={"dependency": dependency, "child_stage": child_stage}, **kwargs)

    def satisfy_dependency(self, parent_id: str, *, payload: Mapping[str, Any] | None = None, command_id: str | None = None, expected_revision: int | None = None, **kwargs: Any) -> dict[str, Any]:
        return self.dispatch("SATISFY_DEPENDENCY", subject_id=parent_id, payload=_copy(payload if payload is not None else kwargs), command_id=command_id, expected_revision=expected_revision)

    def resolve_blocker(self, stage_id: str, *, payload: Mapping[str, Any] | None = None, command_id: str | None = None, expected_revision: int | None = None, **kwargs: Any) -> dict[str, Any]:
        return self.dispatch("RESOLVE_BLOCKER", subject_id=stage_id, payload=_copy(payload if payload is not None else kwargs), command_id=command_id, expected_revision=expected_revision)

    def revalidate_blocker(self, stage_id: str, *, payload: Mapping[str, Any] | None = None, command_id: str | None = None, expected_revision: int | None = None, **kwargs: Any) -> dict[str, Any]:
        return self.dispatch("RESOLVE_BLOCKER", subject_id=stage_id, payload=_copy(payload if payload is not None else kwargs), command_id=command_id, expected_revision=expected_revision)

    def apply_receipt(self, stage_id: str, *, payload: Mapping[str, Any] | None = None, command_id: str | None = None, expected_revision: int | None = None, **kwargs: Any) -> dict[str, Any]:
        return self.dispatch("APPLY_RECEIPT", subject_id=stage_id, payload=_copy(payload if payload is not None else kwargs), command_id=command_id, expected_revision=expected_revision)

    def commit_integration(self, stage_id: str, *, payload: Mapping[str, Any] | None = None, command_id: str | None = None, expected_revision: int | None = None, **kwargs: Any) -> dict[str, Any]:
        return self.dispatch("COMMIT_INTEGRATION", subject_id=stage_id, payload=_copy(payload if payload is not None else kwargs), command_id=command_id, expected_revision=expected_revision)

    def closeout(self, stage_id: str, *, payload: Mapping[str, Any] | None = None, command_id: str | None = None, expected_revision: int | None = None, **kwargs: Any) -> dict[str, Any]:
        return self.dispatch("CLOSEOUT", subject_id=stage_id, payload=_copy(payload if payload is not None else kwargs), command_id=command_id, expected_revision=expected_revision)

    def stop(self, stage_id: str, *, payload: Mapping[str, Any] | None = None, command_id: str | None = None, expected_revision: int | None = None, **kwargs: Any) -> dict[str, Any]:
        return self.dispatch("STOP", subject_id=stage_id, payload=_copy(payload if payload is not None else kwargs), command_id=command_id, expected_revision=expected_revision)


__all__ = [
    "StageController", "WorkflowV2ControllerError", "build_registration_payload",
    "build_provider_handoff_manifest", "continue_iteration_change_digest",
    "LEGACY_ORPHAN_CLASSIFICATION", "LEGACY_ORPHAN_RESOLUTION",
]
