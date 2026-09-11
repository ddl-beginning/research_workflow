"""Thin, provider-neutral Bootstrap orchestration seam.

This module composes the already bounded project/context/discovery/blueprint
and Step 15 services.  It deliberately does not own ``workflow-state.json``
and it never writes StageController state directly.  Callers persist the
returned metadata and transition envelope, while :func:`plan_stage` remains
the only Stage-registration seam.

The external consultation boundary is intentionally injected.  A production
caller may provide two independent ``ProjectScopedBridgeConsultant``
instances; offline callers may inject deterministic consultants.  No default
fake, browser profile, API key, or transport is created here.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .asset_layer import CANDIDATE_USER_PROJECT_ASSET_ID, load_available_assets
from .contracts import canonical_json, sha256_json
from .design_review import design_summary_digest, normalize_design_summary
from .project_blueprint import (
    BLUEPRINT_MANIFEST_RELATIVE_PATH,
    PROJECT_BLUEPRINT_MARKER,
    PROJECT_BLUEPRINT_RELATIVE_PATH,
    PROJECT_BLUEPRINT_STATUS,
    READY_FOR_STAGE_PLANNING,
    build_project_blueprint,
    load_project_blueprint,
    verify_project_blueprint,
)
from .project_context import (
    PROJECT_CONTEXT_RELATIVE_PATH,
    build_project_context,
    load_project_context,
    verify_project_context,
)
from .project_discovery import (
    DISCOVERY_REPORT_MARKER,
    DISCOVERY_REPORT_RELATIVE_PATH,
    DISCOVERY_STATUS,
    discover_project,
    load_discovery_report,
    verify_discovery_report,
)
from .project_intake import BriefState, validate_project_brief
from .project_state import ProjectStateError, load_project_brief, resolve_project_root
from .stage_planning import (
    BOOTSTRAP_STATE_MARKER,
    BOOTSTRAP_STATE_NEXT_ACTION,
    BOOTSTRAP_STATE_RELATIVE_PATH,
    BOOTSTRAP_STATE_STATUS,
    EXECUTION_HANDOFF_RELATIVE_PATH,
    HUMAN_ACCEPT_RECEIPT_RELATIVE_PATH,
    STAGE_PLAN_RELATIVE_PATH,
    bootstrap_state_digest,
    build_human_accept_receipt,
    plan_stage_from_accepted_design,
    plan_stage,
    validate_execution_handoff,
    verify_stage_plan,
)


WORKFLOW_TRANSITION_SCHEMA_VERSION = "workflow_transition.v1"
WORKFLOW_ORCHESTRATOR_SCHEMA_VERSION = "workflow_orchestrator.v1"
WORKFLOW_TRANSITIONS = frozenset({"AWAITING_DESIGN_REVIEW", "STAGE_PLANNED"})
ORCHESTRATOR_PHASES = frozenset({"CONTEXT", "DISCOVERY", "BLUEPRINT", "DESIGN_REVIEW", "STAGE_PLANNING"})
ATTEMPT_STATUSES = frozenset({"started", "complete", "reused", "failed", "indeterminate"})
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_ATTEMPTS = 8
_MAX_TEXT = 4000


class WorkflowOrchestratorError(RuntimeError):
    """Bounded, fail-closed Bootstrap orchestration error."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = str(code)
        self.details = copy.deepcopy(dict(details or {}))
        super().__init__(message)


def _bounded_text(value: Any, field: str, *, required: bool = False, maximum: int = _MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise WorkflowOrchestratorError("ORCHESTRATOR_INPUT_INVALID", f"{field} must be a string")
    checked = value.replace("\x00", "").strip()
    if required and not checked:
        raise WorkflowOrchestratorError("ORCHESTRATOR_INPUT_INVALID", f"{field} must be non-empty")
    if len(checked) > maximum:
        raise WorkflowOrchestratorError("ORCHESTRATOR_INPUT_INVALID", f"{field} is too long")
    return checked


def _bounded_copy(value: Any, *, depth: int = 0, path: str = "$") -> Any:
    if depth > 8:
        raise WorkflowOrchestratorError("ORCHESTRATOR_RESULT_INVALID", f"value is too deeply nested at {path}")
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        if len(value) > 64:
            raise WorkflowOrchestratorError("ORCHESTRATOR_RESULT_INVALID", f"too many fields at {path}")
        for key, child in value.items():
            name = str(key)
            if name.casefold().replace("-", "_") in {
                "api_key",
                "access_token",
                "auth_token",
                "cookie",
                "cookies",
                "password",
                "prompt",
                "raw_response",
                "secret",
                "session",
                "token",
                "tokens",
                "transcript",
            }:
                raise WorkflowOrchestratorError("ORCHESTRATOR_SENSITIVE_DATA", f"sensitive field is not allowed at {path}")
            result[name] = _bounded_copy(child, depth=depth + 1, path=f"{path}.{name}")
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > 64:
            raise WorkflowOrchestratorError("ORCHESTRATOR_RESULT_INVALID", f"too many items at {path}")
        return [_bounded_copy(child, depth=depth + 1, path=f"{path}[]") for child in value]
    if isinstance(value, str):
        return _bounded_text(value, path, maximum=12_000)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise WorkflowOrchestratorError("ORCHESTRATOR_RESULT_INVALID", f"unsupported value at {path}")


def _digest(value: Any) -> str:
    return sha256_json(value)


def _file_digest(root: Path, relative: str) -> str:
    path = root / Path(relative)
    if path.is_symlink() or not path.is_file():
        raise WorkflowOrchestratorError("BOOTSTRAP_ARTIFACT_MISSING", f"required artifact is missing: {relative}")
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise WorkflowOrchestratorError("BOOTSTRAP_ARTIFACT_UNREADABLE", f"required artifact is unreadable: {relative}") from exc


def _error_code(exc: BaseException, fallback: str) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code.strip():
        return code.strip()[:128]
    return fallback


def _is_missing_error(exc: BaseException) -> bool:
    code = _error_code(exc, "")
    return code in {
        "CONTEXT_NOT_FOUND",
        "DISCOVERY_REPORT_NOT_FOUND",
        "BLUEPRINT_NOT_FOUND",
        "BLUEPRINT_MANIFEST_NOT_FOUND",
    }


def _relative_artifact(root: Path, relative: str, *, digest: str | None = None) -> dict[str, Any]:
    path = root / Path(relative)
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkflowOrchestratorError("ARTIFACT_PATH_INVALID", f"artifact escaped workspace: {relative}") from exc
    payload: dict[str, Any] = {"path": relative}
    payload["digest"] = digest if digest is not None else hashlib.sha256(path.read_bytes()).hexdigest()
    payload["bytes"] = path.stat().st_size
    return payload


def _safe_relative_plan_path(root: Path, value: Any, field: str) -> str:
    """Validate a Step 15 path before it crosses the orchestration boundary."""

    if not isinstance(value, str) or not value.strip():
        raise WorkflowOrchestratorError("STAGE_PLAN_PATH_INVALID", f"{field} must be a non-empty relative path")
    candidate = Path(value.strip())
    if candidate.is_absolute() or candidate.drive or candidate.anchor or ".." in candidate.parts:
        raise WorkflowOrchestratorError("STAGE_PLAN_PATH_INVALID", f"{field} must remain inside the workspace")
    if value.strip().lower().startswith("file:"):
        raise WorkflowOrchestratorError("STAGE_PLAN_PATH_INVALID", f"{field} must not be a URI")
    try:
        resolved = (root / candidate).resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkflowOrchestratorError("STAGE_PLAN_PATH_INVALID", f"{field} escaped the workspace") from exc
    return candidate.as_posix()


def _project_goal(brief: Mapping[str, Any]) -> str:
    nested = brief.get("brief") if isinstance(brief.get("brief"), Mapping) else {}
    for key in ("goal", "desired_outcome", "problem_statement"):
        value = nested.get(key) if isinstance(nested, Mapping) else None
        if not isinstance(value, str) or not value.strip():
            value = brief.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    rough = brief.get("rough_requirement")
    if isinstance(rough, str) and rough.strip():
        return rough.strip()
    raise WorkflowOrchestratorError("DESIGN_REVIEW_INPUT_INVALID", "approved brief has no usable project goal")


def _brief_list(brief: Mapping[str, Any], *keys: str) -> list[str]:
    nested = brief.get("brief") if isinstance(brief.get("brief"), Mapping) else {}
    for key in keys:
        value = nested.get(key) if isinstance(nested, Mapping) else None
        if value is None:
            value = brief.get(key)
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        if isinstance(value, (list, tuple)):
            values = [str(item).strip() for item in value if isinstance(item, str) and item.strip()]
            if values:
                return values[:16]
    return []


def _route_slug(value: Any, fallback: str) -> str:
    raw = str(value or fallback).strip()
    slug = _SAFE_ID.sub("-", raw).strip("-") or fallback
    return slug[:64]


def _route_text(route: Mapping[str, Any], *keys: str, fallback: str) -> str:
    for key in keys:
        value = route.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:_MAX_TEXT]
    return fallback


def _route_steps(route: Mapping[str, Any], blueprint: Mapping[str, Any]) -> list[str]:
    values = route.get("steps", route.get("validation_steps"))
    if not isinstance(values, (list, tuple)):
        values = blueprint.get("validation_plan")
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        return []
    return [str(item).strip()[:_MAX_TEXT] for item in values if isinstance(item, str) and item.strip()][:16]


def _candidate_zero_verified(discovery: Mapping[str, Any], root: Path) -> bool:
    if discovery.get("candidate_zero_asset_id") == CANDIDATE_USER_PROJECT_ASSET_ID:
        return True
    try:
        inventory = load_available_assets(root)
    except Exception:
        return False
    if not isinstance(inventory, Mapping):
        return False
    assets = inventory.get("assets")
    return isinstance(assets, list) and any(
        isinstance(item, Mapping)
        and item.get("asset_id") == CANDIDATE_USER_PROJECT_ASSET_ID
        and item.get("verified") is True
        and item.get("candidate_status") in {"candidate_only", "verified"}
        for item in assets
    )


def blueprint_to_design_summary(
    blueprint: Mapping[str, Any],
    *,
    brief: Mapping[str, Any],
    discovery: Mapping[str, Any],
) -> dict[str, Any]:
    """Map one verified Blueprint to the strict project-level review shape.

    The mapper intentionally refuses to invent a review when the Blueprint has
    no comparable alternative or no bounded acceptance step.  It treats the
    primary route as the audited Candidate 0 route only after the local asset
    evidence has been verified by Discovery.
    """

    if not isinstance(blueprint, Mapping) or blueprint.get("status") != PROJECT_BLUEPRINT_STATUS:
        raise WorkflowOrchestratorError("DESIGN_REVIEW_INPUT_INVALID", "Blueprint is not complete")
    if blueprint.get("next_action") != READY_FOR_STAGE_PLANNING or blueprint.get("marker") != PROJECT_BLUEPRINT_MARKER:
        raise WorkflowOrchestratorError("DESIGN_REVIEW_INPUT_INVALID", "Blueprint is not at READY_FOR_STAGE_PLANNING")
    if not isinstance(discovery, Mapping) or discovery.get("status") != DISCOVERY_STATUS:
        raise WorkflowOrchestratorError("DESIGN_REVIEW_INPUT_INVALID", "Discovery is not complete")
    if not _candidate_zero_verified(discovery, Path(str(brief.get("repository_root", "."))).resolve()):
        raise WorkflowOrchestratorError("CANDIDATE_ZERO_NOT_VERIFIED", "Candidate 0 local audit is not verified")
    primary = blueprint.get("primary_route")
    alternatives = blueprint.get("alternatives")
    if not isinstance(primary, Mapping) or not primary:
        raise WorkflowOrchestratorError("DESIGN_REVIEW_INPUT_INVALID", "Blueprint primary_route is missing")
    if not isinstance(alternatives, list) or not alternatives:
        raise WorkflowOrchestratorError(
            "DESIGN_REVIEW_INPUT_INVALID",
            "Blueprint must expose at least one alternative for human comparison",
        )
    routes: list[tuple[Mapping[str, Any], str]] = [(primary, "CANDIDATE_0")]
    for index, raw in enumerate(alternatives):
        if isinstance(raw, str) and raw.strip():
            routes.append(({"title": raw.strip(), "summary": raw.strip(), "route_id": f"alternative-{index + 1}"}, "ALTERNATIVE"))
        elif isinstance(raw, Mapping) and raw:
            routes.append((raw, "ALTERNATIVE"))
        else:
            raise WorkflowOrchestratorError("DESIGN_REVIEW_INPUT_INVALID", f"alternative route {index} is invalid")
    if len(routes) > 16:
        raise WorkflowOrchestratorError("DESIGN_REVIEW_INPUT_INVALID", "Blueprint contains too many routes")

    stages: list[dict[str, Any]] = []
    for index, (route, route_class) in enumerate(routes):
        title = _route_text(route, "title", "name", "route_id", fallback=f"Route {index + 1}")
        summary = _route_text(route, "summary", "description", fallback=title)
        steps = _route_steps(route, blueprint)
        if not steps:
            raise WorkflowOrchestratorError(
                "DESIGN_REVIEW_INPUT_INVALID",
                f"route {title!r} has no bounded acceptance/validation step",
            )
        why_exists = (
            str(blueprint.get("why_primary", "")).strip()
            if route_class == "CANDIDATE_0"
            else "Retain this bounded alternative for explicit human comparison."
        )
        if not why_exists:
            why_exists = "The locally audited Candidate 0 route is the primary bounded baseline."
        stages.append(
            {
                "stage_id": _route_slug(route.get("route_id"), f"route-{index + 1}"),
                "name": title,
                "goal": summary,
                "why_exists": why_exists[:_MAX_TEXT],
                "inputs": [],
                "outputs": [],
                "acceptance": steps,
                "dependencies": [],
                "route_class": route_class,
            }
        )

    summary = {
        "project_final_goal": _project_goal(brief),
        "stage_count": len(stages),
        "stages": stages,
        "major_risks": [
            str(item).strip()[:_MAX_TEXT]
            for item in (blueprint.get("important_risks") or [])
            if isinstance(item, str) and item.strip()
        ][:16],
        "recommended_overall_route": _route_text(primary, "title", "name", "summary", fallback="Candidate 0 route"),
        "review_question": "Accept this bounded Blueprint route for Step 15 Stage planning?",
        "confirmation_needed": True,
    }
    try:
        return normalize_design_summary(summary, require_core=True)
    except Exception as exc:
        if isinstance(exc, WorkflowOrchestratorError):
            raise
        raise WorkflowOrchestratorError("DESIGN_REVIEW_INPUT_INVALID", "Blueprint could not be mapped to Design Review") from exc


def _attempt(
    *,
    phase: str,
    status: str,
    root: Path,
    project_id: str,
    input_digest: str,
    request_budget: int,
    request_count: int | None,
    error_code: str | None = None,
    indeterminate: bool = False,
) -> dict[str, Any]:
    if phase not in ORCHESTRATOR_PHASES:
        raise WorkflowOrchestratorError("ORCHESTRATOR_RESULT_INVALID", "attempt phase is not allowed")
    if status not in ATTEMPT_STATUSES:
        raise WorkflowOrchestratorError("ORCHESTRATOR_RESULT_INVALID", "attempt status is not allowed")
    payload: dict[str, Any] = {
        "schema_version": "workflow_attempt.v1",
        "phase": phase,
        "status": status,
        "attempt": 1,
        "request_budget": request_budget,
        "request_count": request_count,
        "request_count_known": request_count is not None,
        "input_digest": input_digest,
        "project_id": project_id,
        "repository_root": root.as_posix(),
        "retry_allowed": False,
        "indeterminate": bool(indeterminate),
    }
    if error_code is not None:
        payload["error_code"] = str(error_code)[:128]
    return payload


def _validate_attempt(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkflowOrchestratorError("ORCHESTRATOR_RESULT_INVALID", "attempt must be an object")
    result = _bounded_copy(dict(value), path="attempt")
    if result.get("schema_version") != "workflow_attempt.v1":
        raise WorkflowOrchestratorError("ORCHESTRATOR_RESULT_INVALID", "attempt schema is invalid")
    if result.get("phase") not in ORCHESTRATOR_PHASES or result.get("status") not in ATTEMPT_STATUSES:
        raise WorkflowOrchestratorError("ORCHESTRATOR_RESULT_INVALID", "attempt phase/status is invalid")
    if result.get("attempt") != 1 or result.get("retry_allowed") is not False:
        raise WorkflowOrchestratorError("ORCHESTRATOR_RESULT_INVALID", "attempt retry policy is invalid")
    if not isinstance(result.get("request_budget"), int) or result["request_budget"] not in {0, 1}:
        raise WorkflowOrchestratorError("ORCHESTRATOR_RESULT_INVALID", "attempt request budget is invalid")
    request_count = result.get("request_count")
    if request_count is not None and (not isinstance(request_count, int) or request_count not in {0, 1}):
        raise WorkflowOrchestratorError("ORCHESTRATOR_RESULT_INVALID", "attempt request count is invalid")
    if result.get("request_count_known") is not (request_count is not None):
        raise WorkflowOrchestratorError("ORCHESTRATOR_RESULT_INVALID", "attempt request count binding is invalid")
    if not isinstance(result.get("input_digest"), str) or _HEX64.fullmatch(result["input_digest"]) is None:
        raise WorkflowOrchestratorError("ORCHESTRATOR_RESULT_INVALID", "attempt input digest is invalid")
    return result


def _transition(
    *,
    transition: str,
    root: Path,
    project_id: str,
    brief_digest: str,
    attempts: Sequence[Mapping[str, Any]],
    artifacts: Mapping[str, Any],
    design_review: Mapping[str, Any] | None = None,
    stage_plan: Mapping[str, Any] | None = None,
    bootstrap_state: Mapping[str, Any] | None = None,
    execution_handoff: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if transition not in WORKFLOW_TRANSITIONS:
        raise WorkflowOrchestratorError("ORCHESTRATOR_RESULT_INVALID", "workflow transition is not allowed")
    checked_attempts = [_validate_attempt(item) for item in attempts]
    if not checked_attempts or len(checked_attempts) > _MAX_ATTEMPTS:
        raise WorkflowOrchestratorError("ORCHESTRATOR_RESULT_INVALID", "workflow transition has an invalid attempt list")
    payload: dict[str, Any] = {
        "schema_version": WORKFLOW_TRANSITION_SCHEMA_VERSION,
        "orchestrator_schema_version": WORKFLOW_ORCHESTRATOR_SCHEMA_VERSION,
        "transition": transition,
        "project_id": project_id,
        "repository_root": root.as_posix(),
        "brief_digest": brief_digest,
        "validated": True,
        "retry_allowed": False,
        "stage_created": transition == "STAGE_PLANNED",
        "stage_started": False,
        "next_action": "REQUEST_DESIGN_REVIEW" if transition == "AWAITING_DESIGN_REVIEW" else "RUN_WORKFLOW",
        "attempts": checked_attempts,
        "artifacts": _bounded_copy(dict(artifacts), path="artifacts"),
    }
    if design_review is not None:
        payload["design_review"] = _bounded_copy(dict(design_review), path="design_review")
    if stage_plan is not None:
        payload["stage_plan"] = _bounded_copy(dict(stage_plan), path="stage_plan")
    if bootstrap_state is not None:
        payload["bootstrap_state"] = _bounded_copy(dict(bootstrap_state), path="bootstrap_state")
    if execution_handoff is not None:
        payload["execution_handoff"] = _bounded_copy(dict(execution_handoff), path="execution_handoff")
    return validate_workflow_transition(payload)


def validate_workflow_transition(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and detach the small transition envelope returned to callers."""

    if not isinstance(value, Mapping):
        raise WorkflowOrchestratorError("ORCHESTRATOR_RESULT_INVALID", "workflow transition must be an object")
    result = _bounded_copy(dict(value), path="transition")
    if result.get("schema_version") != WORKFLOW_TRANSITION_SCHEMA_VERSION:
        raise WorkflowOrchestratorError("ORCHESTRATOR_RESULT_INVALID", "workflow transition schema is invalid")
    if result.get("transition") not in WORKFLOW_TRANSITIONS or result.get("validated") is not True:
        raise WorkflowOrchestratorError("ORCHESTRATOR_RESULT_INVALID", "workflow transition is not validated")
    if result.get("retry_allowed") is not False or result.get("stage_started") is not False:
        raise WorkflowOrchestratorError("ORCHESTRATOR_RESULT_INVALID", "workflow transition safety flags are invalid")
    if result.get("stage_created") is not (result.get("transition") == "STAGE_PLANNED"):
        raise WorkflowOrchestratorError("ORCHESTRATOR_RESULT_INVALID", "workflow transition Stage binding is invalid")
    if result.get("next_action") not in {"REQUEST_DESIGN_REVIEW", "RUN_WORKFLOW"}:
        raise WorkflowOrchestratorError("ORCHESTRATOR_RESULT_INVALID", "workflow transition next_action is invalid")
    if not isinstance(result.get("project_id"), str) or not result["project_id"].strip():
        raise WorkflowOrchestratorError("ORCHESTRATOR_RESULT_INVALID", "workflow transition project identity is missing")
    if not isinstance(result.get("brief_digest"), str) or _HEX64.fullmatch(result["brief_digest"]) is None:
        raise WorkflowOrchestratorError("ORCHESTRATOR_RESULT_INVALID", "workflow transition brief digest is invalid")
    attempts = result.get("attempts")
    if not isinstance(attempts, list) or not attempts or len(attempts) > _MAX_ATTEMPTS:
        raise WorkflowOrchestratorError("ORCHESTRATOR_RESULT_INVALID", "workflow transition attempts are invalid")
    result["attempts"] = [_validate_attempt(item) for item in attempts]
    if result["transition"] == "AWAITING_DESIGN_REVIEW":
        review = result.get("design_review")
        if not isinstance(review, Mapping) or review.get("state") != "AWAITING_DESIGN_REVIEW":
            raise WorkflowOrchestratorError("ORCHESTRATOR_RESULT_INVALID", "pending Design Review is missing")
        summary = review.get("summary")
        digest = review.get("design_digest")
        if not isinstance(summary, Mapping) or not isinstance(digest, str) or digest != design_summary_digest(summary):
            raise WorkflowOrchestratorError("ORCHESTRATOR_RESULT_INVALID", "Design Review digest is inconsistent")
        if result.get("bootstrap_state") is None:
            artifacts = result.get("artifacts")
            provenance = review.get("research_provenance")
            consultation_ref = review.get("gpt_reconsultation_ref")
            if not isinstance(artifacts, Mapping) or not isinstance(provenance, (Mapping, list)) and not isinstance(consultation_ref, str):
                raise WorkflowOrchestratorError("ORCHESTRATOR_RESULT_INVALID", "Design Review research provenance is missing")
    if result["transition"] == "STAGE_PLANNED":
        plan = result.get("stage_plan")
        if not isinstance(plan, Mapping) or plan.get("stage_status") != "PLANNED" or plan.get("stage_started") is not False:
            raise WorkflowOrchestratorError("ORCHESTRATOR_RESULT_INVALID", "planned Stage metadata is invalid")
        if result.get("bootstrap_state") is None and result.get("execution_handoff") is None:
            artifacts = result.get("artifacts")
            if not isinstance(artifacts, Mapping):
                raise WorkflowOrchestratorError("ORCHESTRATOR_RESULT_INVALID", "execution handoff artifacts are missing")
            # A legacy injected Step 15 service may return only its bounded
            # verification envelope.  The production accepted-design route
            # carries an execution-handoff descriptor and the runtime checks
            # the StageController claim before checkpointing it.
            if not isinstance(artifacts.get("execution_handoff"), Mapping):
                verification = plan.get("verification")
                if not isinstance(verification, Mapping) or verification.get("passed") is not True:
                    raise WorkflowOrchestratorError("ORCHESTRATOR_RESULT_INVALID", "accepted-design execution handoff is missing")
    return result


@dataclass(frozen=True)
class WorkflowOrchestratorDependencies:
    """Injectable public-service seams; no transport is selected here."""

    build_context: Callable[..., Mapping[str, Any]] = build_project_context
    verify_context: Callable[..., Mapping[str, Any]] = verify_project_context
    load_context: Callable[..., Mapping[str, Any]] = load_project_context
    discover: Callable[..., Mapping[str, Any]] = discover_project
    verify_discovery: Callable[..., Mapping[str, Any]] = verify_discovery_report
    load_discovery: Callable[..., Mapping[str, Any]] = load_discovery_report
    build_blueprint: Callable[..., Mapping[str, Any]] = build_project_blueprint
    verify_blueprint: Callable[..., Mapping[str, Any]] = verify_project_blueprint
    load_blueprint: Callable[..., Mapping[str, Any]] = load_project_blueprint
    plan_stage: Callable[..., Mapping[str, Any]] = plan_stage
    plan_accepted_design: Callable[..., Mapping[str, Any]] = plan_stage_from_accepted_design
    verify_stage_plan: Callable[..., Mapping[str, Any]] = verify_stage_plan


def _default_consultation_ref(blueprint: Mapping[str, Any]) -> str:
    bridge = blueprint.get("bridge_receipt")
    if isinstance(bridge, Mapping):
        consultation_id = bridge.get("consultation_id")
        if isinstance(consultation_id, str) and consultation_id.strip():
            return consultation_id.strip()
    consultation = blueprint.get("consultation")
    claims = consultation.get("claims_digest") if isinstance(consultation, Mapping) else None
    if isinstance(claims, str) and _HEX64.fullmatch(claims):
        return f"blueprint-{claims[:24]}"
    digest = blueprint.get("blueprint_digest")
    if isinstance(digest, str) and _HEX64.fullmatch(digest):
        return f"blueprint-{digest[:24]}"
    raise WorkflowOrchestratorError("DESIGN_REVIEW_INPUT_INVALID", "Blueprint has no bounded consultation reference")


def _load_approved(root: Path, supplied: Mapping[str, Any] | None) -> dict[str, Any]:
    if supplied is None:
        try:
            supplied = load_project_brief(root)
        except ProjectStateError as exc:
            raise WorkflowOrchestratorError("BRIEF_NOT_FOUND", "approved project brief is required") from exc
    try:
        checked = validate_project_brief(supplied, root)
    except Exception as exc:
        raise WorkflowOrchestratorError("BRIEF_INVALID", "canonical project brief failed validation") from exc
    if checked.get("state") != BriefState.APPROVED.value or checked.get("status") != BriefState.APPROVED.value:
        raise WorkflowOrchestratorError("BRIEF_NOT_APPROVED", "Bootstrap orchestration requires an approved brief")
    return checked


def _is_complete_discovery(report: Mapping[str, Any], brief: Mapping[str, Any]) -> bool:
    return (
        report.get("status") == DISCOVERY_STATUS
        and report.get("marker") == DISCOVERY_REPORT_MARKER
        and report.get("project_id") == brief.get("project_id")
        and report.get("brief_digest") == _digest(brief)
    )


def _is_complete_blueprint(report: Mapping[str, Any], brief: Mapping[str, Any], discovery: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
    return (
        report.get("status") == PROJECT_BLUEPRINT_STATUS
        and report.get("next_action") == READY_FOR_STAGE_PLANNING
        and report.get("marker") == PROJECT_BLUEPRINT_MARKER
        and report.get("project_id") == brief.get("project_id")
        and report.get("brief_digest") == _digest(brief)
        and report.get("context_digest") == context.get("context_digest")
        and report.get("discovery_digest") == discovery.get("evidence_digest")
        and report.get("stage_created") is False
        and report.get("stage_started") is False
    )


def _receipt_count(envelope: Mapping[str, Any], *, phase: str) -> int:
    consultation = envelope.get("consultation")
    if isinstance(consultation, Mapping) and consultation.get("request_count") == 1:
        return 1
    bridge = envelope.get("bridge_receipt")
    if isinstance(bridge, Mapping) and bridge.get("request_count") == 1:
        return 1
    # Offline injected consultants do not expose a transport receipt.  Their
    # canonical report still records exactly one bounded consultation.
    if envelope.get("consultation_count") == 1 or envelope.get("gpt_calls") == 1:
        return 1
    raise WorkflowOrchestratorError("CONSULTATION_BUDGET_INVALID", f"{phase} did not prove request_count=1")


class WorkflowOrchestrator:
    """Compose Bootstrap and Step 15 without owning runtime or Stage state."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        consultant_factory: Callable[[str, Path], Any],
        dependencies: WorkflowOrchestratorDependencies | None = None,
        discovery_verifier: Any = None,
        auto_build_context: bool = True,
    ) -> None:
        try:
            self.workspace_root = resolve_project_root(workspace_root)
        except ProjectStateError as exc:
            raise WorkflowOrchestratorError("WORKSPACE_INVALID", "workspace_root must be an existing directory") from exc
        if not callable(consultant_factory):
            raise WorkflowOrchestratorError("CONSULTANT_FACTORY_INVALID", "consultant_factory must be callable")
        self.consultant_factory = consultant_factory
        self.dependencies = dependencies or WorkflowOrchestratorDependencies()
        self.discovery_verifier = discovery_verifier
        self.auto_build_context = bool(auto_build_context)
        self._consultant_ids: set[int] = set()

    def _new_consultant(self, phase: str) -> Any:
        try:
            consultant = self.consultant_factory(phase, self.workspace_root)
        except Exception as exc:
            raise WorkflowOrchestratorError("CONSULTANT_FACTORY_FAILED", f"{phase} consultant factory failed") from exc
        if consultant is None or not (callable(consultant) or callable(getattr(consultant, "consult", None))):
            raise WorkflowOrchestratorError("CONSULTANT_INVALID", f"{phase} consultant is not callable")
        identity = id(consultant)
        if identity in self._consultant_ids:
            raise WorkflowOrchestratorError("CONSULTANT_REUSED", f"{phase} reused a previous consultant instance")
        self._consultant_ids.add(identity)
        return consultant

    def _context(self, brief: Mapping[str, Any]) -> dict[str, Any]:
        context_path = self.workspace_root / PROJECT_CONTEXT_RELATIVE_PATH
        try:
            verified = self.dependencies.verify_context(self.workspace_root)
            loaded = self.dependencies.load_context(self.workspace_root)
            if not isinstance(loaded, Mapping):
                raise WorkflowOrchestratorError("CONTEXT_INVALID", "verified project context is not an object")
            return {**dict(loaded), **dict(verified)}
        except Exception as exc:
            if context_path.exists() or not self.auto_build_context:
                raise WorkflowOrchestratorError(_error_code(exc, "CONTEXT_INVALID"), "project context failed verification") from exc
        try:
            self.dependencies.build_context(self.workspace_root, brief=brief)
            verified = self.dependencies.verify_context(self.workspace_root)
            loaded = self.dependencies.load_context(self.workspace_root)
        except Exception as exc:
            raise WorkflowOrchestratorError(_error_code(exc, "CONTEXT_BUILD_FAILED"), "project context could not be built") from exc
        if not isinstance(loaded, Mapping) or not isinstance(verified, Mapping):
            raise WorkflowOrchestratorError("CONTEXT_INVALID", "project context verification returned an invalid object")
        return {**dict(loaded), **dict(verified)}

    def _existing_discovery(self, brief: Mapping[str, Any]) -> dict[str, Any] | None:
        path = self.workspace_root / DISCOVERY_REPORT_RELATIVE_PATH
        if not path.exists():
            return None
        try:
            self.dependencies.verify_discovery(self.workspace_root)
            report = self.dependencies.load_discovery(self.workspace_root)
        except Exception as exc:
            raise WorkflowOrchestratorError(_error_code(exc, "DISCOVERY_INVALID"), "existing Discovery report failed verification") from exc
        if not isinstance(report, Mapping) or not _is_complete_discovery(report, brief):
            raise WorkflowOrchestratorError("DISCOVERY_INPUT_MISMATCH", "existing Discovery report is stale")
        if not _candidate_zero_verified(report, self.workspace_root):
            raise WorkflowOrchestratorError("CANDIDATE_ZERO_NOT_VERIFIED", "existing Discovery report lacks Candidate 0 evidence")
        return dict(report)

    def _existing_blueprint(
        self,
        brief: Mapping[str, Any],
        discovery: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        path = self.workspace_root / PROJECT_BLUEPRINT_RELATIVE_PATH
        manifest = self.workspace_root / BLUEPRINT_MANIFEST_RELATIVE_PATH
        if not path.exists() and not manifest.exists():
            return None
        try:
            self.dependencies.verify_blueprint(self.workspace_root)
            report = self.dependencies.load_blueprint(self.workspace_root)
        except Exception as exc:
            raise WorkflowOrchestratorError(_error_code(exc, "BLUEPRINT_INVALID"), "existing Blueprint failed verification") from exc
        if not isinstance(report, Mapping) or not _is_complete_blueprint(report, brief, discovery, context):
            raise WorkflowOrchestratorError("BLUEPRINT_INPUT_MISMATCH", "existing Blueprint is stale")
        return dict(report)

    def _bootstrap_state(
        self,
        *,
        brief: Mapping[str, Any],
        context: Mapping[str, Any],
        discovery: Mapping[str, Any],
        blueprint: Mapping[str, Any],
    ) -> dict[str, Any]:
        discovery_report_digest = _file_digest(self.workspace_root, DISCOVERY_REPORT_RELATIVE_PATH.as_posix())
        blueprint_file_digest = _file_digest(self.workspace_root, PROJECT_BLUEPRINT_RELATIVE_PATH.as_posix())
        manifest_digest = _file_digest(self.workspace_root, BLUEPRINT_MANIFEST_RELATIVE_PATH.as_posix())
        state: dict[str, Any] = {
            "schema_version": "bootstrap_state.v1",
            "status": BOOTSTRAP_STATE_STATUS,
            "marker": BOOTSTRAP_STATE_MARKER,
            "markers": [BOOTSTRAP_STATE_MARKER, "WORKFLOW_BOOTSTRAP_ORCHESTRATOR_PASS"],
            "next_action": BOOTSTRAP_STATE_NEXT_ACTION,
            "project_scope_verified": True,
            "project_id": str(brief["project_id"]),
            "brief_digest": _digest(brief),
            "context_digest": str(context.get("context_digest", "")),
            "discovery_digest": str(discovery.get("evidence_digest", "")),
            "blueprint_digest": str(blueprint.get("blueprint_digest", "")),
            "discovery": {
                "marker": DISCOVERY_REPORT_MARKER,
                "status": DISCOVERY_STATUS,
                "report_path": DISCOVERY_REPORT_RELATIVE_PATH.as_posix(),
                "report_digest": discovery_report_digest,
            },
            "blueprint": {
                "marker": PROJECT_BLUEPRINT_MARKER,
                "status": PROJECT_BLUEPRINT_STATUS,
                "next_action": READY_FOR_STAGE_PLANNING,
                "blueprint_path": PROJECT_BLUEPRINT_RELATIVE_PATH.as_posix(),
                "blueprint_digest": blueprint_file_digest,
                "manifest_path": BLUEPRINT_MANIFEST_RELATIVE_PATH.as_posix(),
                "manifest_digest": manifest_digest,
            },
            "stage_created": False,
            "stage_started": False,
        }
        if not _HEX64.fullmatch(state["context_digest"]) or not _HEX64.fullmatch(state["discovery_digest"]):
            raise WorkflowOrchestratorError("BOOTSTRAP_STATE_INVALID", "Bootstrap input digests are incomplete")
        if not _HEX64.fullmatch(state["blueprint_digest"]):
            raise WorkflowOrchestratorError("BOOTSTRAP_STATE_INVALID", "Blueprint digest is incomplete")
        state["state_digest"] = bootstrap_state_digest(state)
        return state

    def prepare(self, *, brief: Mapping[str, Any] | None = None, trigger: str = "INITIAL_ARCHITECTURE") -> dict[str, Any]:
        """Run/reuse Discovery and Blueprint, then return pending review metadata."""

        root = self.workspace_root
        checked_brief = _load_approved(root, brief)
        project_id = str(checked_brief["project_id"])
        brief_digest = _digest(checked_brief)
        context = self._context(checked_brief)
        context_digest = str(context.get("context_digest", ""))
        if _HEX64.fullmatch(context_digest) is None:
            raise WorkflowOrchestratorError("CONTEXT_INVALID", "verified context has no valid digest")
        attempts: list[dict[str, Any]] = [
            _attempt(
                phase="CONTEXT",
                status="complete",
                root=root,
                project_id=project_id,
                input_digest=context_digest,
                request_budget=0,
                request_count=0,
            )
        ]

        discovery = self._existing_discovery(checked_brief)
        if discovery is not None:
            discovery_input_digest = str(discovery.get("evidence_digest", ""))
            attempts.append(
                _attempt(
                    phase="DISCOVERY",
                    status="reused",
                    root=root,
                    project_id=project_id,
                    input_digest=discovery_input_digest,
                    request_budget=0,
                    request_count=0,
                )
            )
        else:
            # The evidence digest is generated by the existing discovery
            # service.  The attempt records the brief/context binding before
            # crossing the injected external boundary.
            discovery_input_digest = _digest({"brief": brief_digest, "context": context_digest, "phase": "DISCOVERY"})
            attempts.append(
                _attempt(
                    phase="DISCOVERY",
                    status="started",
                    root=root,
                    project_id=project_id,
                    input_digest=discovery_input_digest,
                    request_budget=1,
                    request_count=0,
                )
            )
            consultant = self._new_consultant("DISCOVERY")
            try:
                result = self.dependencies.discover(
                    root,
                    consultant=consultant,
                    verifier=self.discovery_verifier,
                    brief=checked_brief,
                )
                self.dependencies.verify_discovery(root)
                loaded = self.dependencies.load_discovery(root)
            except Exception as exc:
                attempts[-1] = _attempt(
                    phase="DISCOVERY",
                    status="indeterminate",
                    root=root,
                    project_id=project_id,
                    input_digest=discovery_input_digest,
                    request_budget=1,
                    request_count=None,
                    error_code=_error_code(exc, "DISCOVERY_FAILED"),
                    indeterminate=True,
                )
                raise WorkflowOrchestratorError(
                    _error_code(exc, "DISCOVERY_FAILED"),
                    "Discovery failed; no automatic retry is permitted",
                    details={"attempts": attempts, "retry_allowed": False, "indeterminate": True},
                ) from exc
            if not isinstance(loaded, Mapping):
                loaded = result if isinstance(result, Mapping) else None
            if not isinstance(loaded, Mapping) or not _is_complete_discovery(loaded, checked_brief):
                attempts[-1] = _attempt(
                    phase="DISCOVERY",
                    status="indeterminate",
                    root=root,
                    project_id=project_id,
                    input_digest=discovery_input_digest,
                    request_budget=1,
                    request_count=None,
                    error_code="DISCOVERY_RESULT_INVALID",
                    indeterminate=True,
                )
                raise WorkflowOrchestratorError(
                    "DISCOVERY_RESULT_INVALID",
                    "Discovery output is not bound to the approved project",
                    details={"attempts": attempts, "retry_allowed": False, "indeterminate": True},
                )
            discovery = dict(loaded)
            if not _candidate_zero_verified(discovery, root):
                attempts[-1] = _attempt(
                    phase="DISCOVERY",
                    status="indeterminate",
                    root=root,
                    project_id=project_id,
                    input_digest=discovery_input_digest,
                    request_budget=1,
                    request_count=None,
                    error_code="CANDIDATE_ZERO_NOT_VERIFIED",
                    indeterminate=True,
                )
                raise WorkflowOrchestratorError(
                    "CANDIDATE_ZERO_NOT_VERIFIED",
                    "Discovery did not prove the local Candidate 0 audit",
                    details={"attempts": attempts, "retry_allowed": False, "indeterminate": True},
                )
            request_count = _receipt_count(discovery, phase="Discovery")
            attempts[-1] = _attempt(
                phase="DISCOVERY",
                status="complete",
                root=root,
                project_id=project_id,
                input_digest=str(discovery.get("evidence_digest")),
                request_budget=1,
                request_count=request_count,
            )

        assert discovery is not None
        if not _candidate_zero_verified(discovery, root):
            raise WorkflowOrchestratorError("CANDIDATE_ZERO_NOT_VERIFIED", "Candidate 0 local audit is not verified")

        blueprint = self._existing_blueprint(checked_brief, discovery, context)
        if blueprint is not None:
            blueprint_input_digest = str(blueprint.get("feasibility_digest", ""))
            attempts.append(
                _attempt(
                    phase="BLUEPRINT",
                    status="reused",
                    root=root,
                    project_id=project_id,
                    input_digest=blueprint_input_digest,
                    request_budget=0,
                    request_count=0,
                )
            )
        else:
            blueprint_input_digest = _digest(
                {
                    "brief": brief_digest,
                    "context": context_digest,
                    "discovery": discovery.get("evidence_digest"),
                    "phase": "BLUEPRINT",
                }
            )
            attempts.append(
                _attempt(
                    phase="BLUEPRINT",
                    status="started",
                    root=root,
                    project_id=project_id,
                    input_digest=blueprint_input_digest,
                    request_budget=1,
                    request_count=0,
                )
            )
            consultant = self._new_consultant("BLUEPRINT")
            try:
                result = self.dependencies.build_blueprint(root, consultant=consultant, brief=checked_brief)
                self.dependencies.verify_blueprint(root)
                loaded = self.dependencies.load_blueprint(root)
            except Exception as exc:
                attempts[-1] = _attempt(
                    phase="BLUEPRINT",
                    status="indeterminate",
                    root=root,
                    project_id=project_id,
                    input_digest=blueprint_input_digest,
                    request_budget=1,
                    request_count=None,
                    error_code=_error_code(exc, "BLUEPRINT_FAILED"),
                    indeterminate=True,
                )
                raise WorkflowOrchestratorError(
                    _error_code(exc, "BLUEPRINT_FAILED"),
                    "Blueprint failed; no automatic retry is permitted",
                    details={"attempts": attempts, "retry_allowed": False, "indeterminate": True},
                ) from exc
            if not isinstance(loaded, Mapping):
                loaded = result if isinstance(result, Mapping) else None
            if not isinstance(loaded, Mapping) or not _is_complete_blueprint(loaded, checked_brief, discovery, context):
                attempts[-1] = _attempt(
                    phase="BLUEPRINT",
                    status="indeterminate",
                    root=root,
                    project_id=project_id,
                    input_digest=blueprint_input_digest,
                    request_budget=1,
                    request_count=None,
                    error_code="BLUEPRINT_RESULT_INVALID",
                    indeterminate=True,
                )
                raise WorkflowOrchestratorError(
                    "BLUEPRINT_RESULT_INVALID",
                    "Blueprint output is not bound to current Bootstrap evidence",
                    details={"attempts": attempts, "retry_allowed": False, "indeterminate": True},
                )
            blueprint = dict(loaded)
            request_count = _receipt_count(blueprint, phase="Blueprint")
            attempts[-1] = _attempt(
                phase="BLUEPRINT",
                status="complete",
                root=root,
                project_id=project_id,
                input_digest=str(blueprint.get("feasibility_digest")),
                request_budget=1,
                request_count=request_count,
            )

        assert blueprint is not None
        if not _is_complete_blueprint(blueprint, checked_brief, discovery, context):
            raise WorkflowOrchestratorError("BLUEPRINT_INPUT_MISMATCH", "Blueprint is stale for current Bootstrap evidence")
        bootstrap = self._bootstrap_state(
            brief=checked_brief,
            context=context,
            discovery=discovery,
            blueprint=blueprint,
        )
        try:
            summary = blueprint_to_design_summary(blueprint, brief=checked_brief, discovery=discovery)
        except WorkflowOrchestratorError:
            raise
        except Exception as exc:
            raise WorkflowOrchestratorError("DESIGN_REVIEW_INPUT_INVALID", "Blueprint Design Review mapping failed") from exc
        trigger_value = _bounded_text(trigger, "trigger", required=True, maximum=64).upper()
        if trigger_value not in {"INITIAL_ARCHITECTURE", "MAJOR_REPLAN"}:
            raise WorkflowOrchestratorError("DESIGN_REVIEW_INPUT_INVALID", "unsupported Design Review trigger")
        digest = design_summary_digest(summary)
        consultation_ref = _default_consultation_ref(blueprint)
        review = {
            "schema_version": "workflow_design_review.v1",
            "state": "AWAITING_DESIGN_REVIEW",
            "trigger": trigger_value,
            "revision": 1,
            "design_digest": digest,
            "summary": summary,
            "feedback": None,
            "feedback_digest": None,
            "feedback_required": False,
            "gpt_reconsultation_ref": consultation_ref,
            "stage_created": False,
            "stage_started": False,
            "step15_status": "AWAITING_DESIGN_REVIEW",
            "human_decision": None,
        }
        attempts.append(
            _attempt(
                phase="DESIGN_REVIEW",
                status="complete",
                root=root,
                project_id=project_id,
                input_digest=digest,
                request_budget=0,
                request_count=0,
            )
        )
        artifacts = {
            "context": _relative_artifact(root, PROJECT_CONTEXT_RELATIVE_PATH.as_posix(), digest=context_digest),
            "discovery": _relative_artifact(root, DISCOVERY_REPORT_RELATIVE_PATH.as_posix()),
            "blueprint": _relative_artifact(root, PROJECT_BLUEPRINT_RELATIVE_PATH.as_posix()),
            "blueprint_manifest": _relative_artifact(root, BLUEPRINT_MANIFEST_RELATIVE_PATH.as_posix()),
            "bootstrap_state_path": BOOTSTRAP_STATE_RELATIVE_PATH.as_posix(),
        }
        return _transition(
            transition="AWAITING_DESIGN_REVIEW",
            root=root,
            project_id=project_id,
            brief_digest=brief_digest,
            attempts=attempts,
            artifacts=artifacts,
            design_review=review,
            bootstrap_state=bootstrap,
        )

    def _plan_accepted_design(
        self,
        *,
        accepted_review: Mapping[str, Any],
        brief: Mapping[str, Any],
        design_digest: str,
        stage_options: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Plan an accepted design through the V1 execution handoff.

        This route is selected when the project has no legacy Bootstrap
        envelope.  Its inputs are the approved Requirement Baseline, the
        accepted Design Review, the canonical Design Package, verified
        research receipts, and a self-binding Human ACCEPT receipt.
        """

        root = self.workspace_root
        project_id = str(brief["project_id"])
        brief_digest = _digest(brief)
        receipt = accepted_review.get("human_accept_receipt")
        if not isinstance(receipt, Mapping):
            human_decision = accepted_review.get("human_decision")
            if not isinstance(human_decision, Mapping) or human_decision.get("decision") != "ACCEPT":
                raise WorkflowOrchestratorError(
                    "HUMAN_ACCEPT_RECEIPT_MISSING",
                    "accepted Design Review must include a Human ACCEPT receipt or decision",
                )
            actor = human_decision.get("actor")
            if not isinstance(actor, str) or not actor.strip():
                raise WorkflowOrchestratorError("HUMAN_ACCEPT_RECEIPT_INVALID", "Human ACCEPT actor is missing")
            receipt = build_human_accept_receipt(
                project_id=project_id,
                brief_digest=brief_digest,
                design_digest=design_digest,
                actor=actor,
                accepted_at=accepted_review.get("accepted_at") if isinstance(accepted_review.get("accepted_at"), str) else None,
                source="existing_accepted_checkpoint",
            )
        provenance = accepted_review.get("research_provenance")
        attempts = [
            _attempt(
                phase="STAGE_PLANNING",
                status="started",
                root=root,
                project_id=project_id,
                input_digest=_digest({"brief": brief_digest, "design": design_digest, "handoff": "accepted-design-v1"}),
                request_budget=0,
                request_count=0,
            )
        ]
        options = dict(stage_options or {})
        allowed = {
            "stage_id",
            "stage_name",
            "allowed_paths",
            "protected_paths",
            "required_checks",
            "review_artifact_requirements",
            "max_iterations",
        }
        unknown = sorted(set(options) - allowed)
        if unknown:
            raise WorkflowOrchestratorError("STAGE_PLANNING_INPUT_INVALID", f"unsupported stage option(s): {unknown!r}")
        try:
            plan = self.dependencies.plan_accepted_design(
                root,
                accepted_review=accepted_review,
                human_accept_receipt=receipt,
                research_provenance=provenance,
                stage_options=options,
            )
            verification = self.dependencies.verify_stage_plan(root)
        except Exception as exc:
            attempts[-1] = _attempt(
                phase="STAGE_PLANNING",
                status="failed",
                root=root,
                project_id=project_id,
                input_digest=attempts[-1]["input_digest"],
                request_budget=0,
                request_count=0,
                error_code=_error_code(exc, "STAGE_PLANNING_FAILED"),
            )
            raise WorkflowOrchestratorError(
                _error_code(exc, "STAGE_PLANNING_FAILED"),
                "accepted-design Stage planning failed; no automatic retry is permitted",
                details={"attempts": attempts, "retry_allowed": False, "indeterminate": False},
            ) from exc
        if not isinstance(plan, Mapping) or plan.get("stage_status") != "PLANNED" or plan.get("stage_started") is not False:
            raise WorkflowOrchestratorError("STAGE_PLAN_INVALID", "accepted-design planning did not leave the Stage PLANNED")
        if not isinstance(verification, Mapping) or verification.get("stage_status") != "PLANNED":
            raise WorkflowOrchestratorError("STAGE_PLAN_INVALID", "accepted-design verification did not prove PLANNED")
        stage_contract_path = _safe_relative_plan_path(root, plan.get("stage_contract_path"), "stage_contract_path")
        stage_state_path = _safe_relative_plan_path(root, plan.get("stage_state_path"), "stage_state_path")
        contract_digest = plan.get("contract_digest")
        if not isinstance(contract_digest, str) or _HEX64.fullmatch(contract_digest) is None:
            raise WorkflowOrchestratorError("STAGE_PLAN_INVALID", "accepted-design Stage contract digest is invalid")
        handoff_path = root / EXECUTION_HANDOFF_RELATIVE_PATH
        try:
            handoff_payload = json.loads(handoff_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise WorkflowOrchestratorError("EXECUTION_HANDOFF_INVALID", "accepted-design execution handoff is unreadable") from exc
        try:
            handoff = validate_execution_handoff(
                handoff_payload,
                project_id=project_id,
                brief_digest=brief_digest,
                design_package_digest=str(plan.get("design_package_digest")),
                design_digest=design_digest,
            )
        except Exception as exc:
            raise WorkflowOrchestratorError(_error_code(exc, "EXECUTION_HANDOFF_INVALID"), "accepted-design execution handoff is invalid") from exc
        attempts[-1] = _attempt(
            phase="STAGE_PLANNING",
            status="complete",
            root=root,
            project_id=project_id,
            input_digest=attempts[-1]["input_digest"],
            request_budget=0,
            request_count=0,
        )
        stage_plan = {
            "schema_version": "stage_planning.v1",
            "status": plan.get("status"),
            "stage_status": plan.get("stage_status"),
            "stage_id": plan.get("stage_id"),
            "plan_id": plan.get("plan_id"),
            "stage_plan_path": plan.get("stage_plan_path"),
            "stage_contract_path": stage_contract_path,
            "stage_state_path": stage_state_path,
            "contract_digest": contract_digest,
            "stage_started": False,
            "planning_mode": plan.get("planning_mode"),
            "execution_handoff_digest": handoff.get("handoff_digest"),
            "verification": dict(verification),
        }
        artifacts = {
            "execution_handoff": _relative_artifact(root, EXECUTION_HANDOFF_RELATIVE_PATH.as_posix()),
            "design_package": _relative_artifact(root, ".research/DESIGN_PACKAGE.json"),
            "human_accept_receipt": _relative_artifact(root, HUMAN_ACCEPT_RECEIPT_RELATIVE_PATH.as_posix()),
            "stage_plan": _relative_artifact(root, str(plan.get("stage_plan_path"))),
            "stage_contract": {"path": stage_contract_path, "digest": contract_digest},
            "stage_state": {"path": stage_state_path},
        }
        return _transition(
            transition="STAGE_PLANNED",
            root=root,
            project_id=project_id,
            brief_digest=brief_digest,
            attempts=attempts,
            artifacts=artifacts,
            stage_plan=stage_plan,
            execution_handoff=handoff,
        )

    def plan_accepted(
        self,
        accepted_review: Mapping[str, Any],
        *,
        brief: Mapping[str, Any] | None = None,
        stage_options: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run existing Step 15 after an explicitly accepted review.

        The caller must persist ``prepare()``'s ``bootstrap_state`` at the
        canonical path before calling this method.  This method never writes
        that file, the workflow checkpoint, or Stage state itself.
        """

        root = self.workspace_root
        checked_brief = _load_approved(root, brief)
        project_id = str(checked_brief["project_id"])
        brief_digest = _digest(checked_brief)
        if not isinstance(accepted_review, Mapping) or accepted_review.get("state") != "ACCEPTED":
            raise WorkflowOrchestratorError("DESIGN_REVIEW_NOT_ACCEPTED", "an accepted Design Review is required")
        if accepted_review.get("feedback_required") is True:
            ref = accepted_review.get("gpt_reconsultation_ref")
            if not isinstance(ref, str) or not ref.strip():
                raise WorkflowOrchestratorError(
                    "DESIGN_REVIEW_RECONSULT_REQUIRED",
                    "feedback requires a new bounded GPT consultation reference",
                )
        summary = accepted_review.get("summary")
        digest = accepted_review.get("design_digest")
        if not isinstance(summary, Mapping) or not isinstance(digest, str) or digest != design_summary_digest(summary):
            raise WorkflowOrchestratorError("DESIGN_REVIEW_INVALID", "accepted Design Review digest is invalid")
        try:
            normalized_summary = normalize_design_summary(summary, require_core=True)
        except Exception as exc:
            raise WorkflowOrchestratorError("DESIGN_REVIEW_INVALID", "accepted Design Review summary is invalid") from exc
        if design_summary_digest(normalized_summary) != digest:
            raise WorkflowOrchestratorError("DESIGN_REVIEW_INVALID", "accepted Design Review summary changed")

        bootstrap_path = root / BOOTSTRAP_STATE_RELATIVE_PATH
        if not bootstrap_path.is_file() or bootstrap_path.is_symlink():
            return self._plan_accepted_design(
                accepted_review=accepted_review,
                brief=checked_brief,
                design_digest=digest,
                stage_options=stage_options,
            )
        if not bootstrap_path.is_file() or bootstrap_path.is_symlink():
            raise WorkflowOrchestratorError("BOOTSTRAP_STATE_NOT_FOUND", "caller must persist bootstrap_state before planning")
        try:
            bootstrap = json.loads(bootstrap_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise WorkflowOrchestratorError("BOOTSTRAP_STATE_INVALID", "persisted bootstrap_state is unreadable") from exc
        if not isinstance(bootstrap, Mapping) or bootstrap.get("project_id") != project_id:
            raise WorkflowOrchestratorError("PROJECT_IDENTITY_MISMATCH", "bootstrap_state belongs to another project")
        attempts = [
            _attempt(
                phase="STAGE_PLANNING",
                status="started",
                root=root,
                project_id=project_id,
                input_digest=_digest({"brief": brief_digest, "design": digest, "bootstrap": bootstrap.get("state_digest")}),
                request_budget=0,
                request_count=0,
            )
        ]
        options = dict(stage_options or {})
        allowed = {
            "stage_id",
            "stage_name",
            "allowed_paths",
            "protected_paths",
            "required_checks",
            "review_artifact_requirements",
            "max_iterations",
        }
        unknown = sorted(set(options) - allowed)
        if unknown:
            raise WorkflowOrchestratorError("STAGE_PLANNING_INPUT_INVALID", f"unsupported stage option(s): {unknown!r}")
        try:
            plan = self.dependencies.plan_stage(
                root,
                bootstrap_state_path=bootstrap_path,
                **options,
            )
            verification = self.dependencies.verify_stage_plan(root)
        except Exception as exc:
            attempts[-1] = _attempt(
                phase="STAGE_PLANNING",
                status="failed",
                root=root,
                project_id=project_id,
                input_digest=attempts[-1]["input_digest"],
                request_budget=0,
                request_count=0,
                error_code=_error_code(exc, "STAGE_PLANNING_FAILED"),
            )
            raise WorkflowOrchestratorError(
                _error_code(exc, "STAGE_PLANNING_FAILED"),
                "Step 15 planning failed; no automatic retry is permitted",
                details={"attempts": attempts, "retry_allowed": False, "indeterminate": False},
            ) from exc
        if not isinstance(plan, Mapping) or plan.get("stage_status") != "PLANNED" or plan.get("stage_started") is not False:
            raise WorkflowOrchestratorError("STAGE_PLAN_INVALID", "Step 15 did not leave the Stage PLANNED")
        if not isinstance(verification, Mapping) or verification.get("stage_status") != "PLANNED":
            raise WorkflowOrchestratorError("STAGE_PLAN_INVALID", "Step 15 verification did not prove PLANNED")
        stage_contract_path = _safe_relative_plan_path(root, plan.get("stage_contract_path"), "stage_contract_path")
        stage_state_path = _safe_relative_plan_path(root, plan.get("stage_state_path"), "stage_state_path")
        contract_digest = plan.get("contract_digest")
        if not isinstance(contract_digest, str) or _HEX64.fullmatch(contract_digest) is None:
            raise WorkflowOrchestratorError("STAGE_PLAN_INVALID", "Step 15 contract digest is invalid")
        attempts[-1] = _attempt(
            phase="STAGE_PLANNING",
            status="complete",
            root=root,
            project_id=project_id,
            input_digest=attempts[-1]["input_digest"],
            request_budget=0,
            request_count=0,
        )
        return _transition(
            transition="STAGE_PLANNED",
            root=root,
            project_id=project_id,
            brief_digest=brief_digest,
            attempts=attempts,
            artifacts={
                "bootstrap_state": _relative_artifact(root, BOOTSTRAP_STATE_RELATIVE_PATH.as_posix()),
                "stage_plan": _relative_artifact(root, STAGE_PLAN_RELATIVE_PATH.as_posix()),
                "stage_contract": {"path": stage_contract_path, "digest": contract_digest},
                "stage_state": {"path": stage_state_path},
            },
            stage_plan={
                "schema_version": "stage_planning.v1",
                "status": plan.get("status"),
                "stage_status": plan.get("stage_status"),
                "stage_id": plan.get("stage_id"),
                "plan_id": plan.get("plan_id"),
                "stage_plan_path": plan.get("stage_plan_path"),
                "stage_contract_path": stage_contract_path,
                "stage_state_path": stage_state_path,
                "contract_digest": contract_digest,
                "stage_started": False,
                "verification": dict(verification),
            },
            bootstrap_state=bootstrap,
        )


__all__ = [
    "ATTEMPT_STATUSES",
    "ORCHESTRATOR_PHASES",
    "WORKFLOW_ORCHESTRATOR_SCHEMA_VERSION",
    "WORKFLOW_TRANSITION_SCHEMA_VERSION",
    "WORKFLOW_TRANSITIONS",
    "WorkflowOrchestrator",
    "WorkflowOrchestratorDependencies",
    "WorkflowOrchestratorError",
    "blueprint_to_design_summary",
    "validate_workflow_transition",
]
