"""Codex-native, project-level Requirements Intake (Step 11).

This module is intentionally independent from ``stage_controller`` and from
the browser bridge.  It records one canonical, user-owned project brief in
``.research/PROJECT_BRIEF.json``.  It does not discover a project, create a
Stage, invoke GPT, or retain an interview transcript.

The public service is small enough to use from a CLI or a natural-language
entry point.  Every interview turn is deterministic and exposes at most one
current core question.  Repository facts that can be checked locally are
collected read-only and are never turned into user questions.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import argparse
import sys
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import ContractValidationError, canonical_json, load_schema, validate_instance
from .execution_profile import default_execution_profile
from .project_state import (
    PROJECT_BRIEF_RELATIVE_PATH,
    ProjectStateError,
    load_project_brief,
    resolve_project_root,
    save_project_brief,
)


PROJECT_BRIEF_SCHEMA_VERSION = "project_brief.v1"
REQUIREMENTS_RESULT_SCHEMA_VERSION = "requirements_intake_result.v1"
REQUIREMENT_BASELINE_SCHEMA_VERSION = "requirement_baseline.v1"
PROJECT_INTAKE_MARKER = "CODEX_NATIVE_REQUIREMENTS_INTAKE_PASS"
PROJECT_BRIEF_PATH = PROJECT_BRIEF_RELATIVE_PATH


class BriefState(str, Enum):
    """Canonical project brief lifecycle states."""

    DRAFT = "DRAFT"
    WAITING_USER_APPROVAL = "WAITING_USER_APPROVAL"
    APPROVED = "APPROVED"
    CANCELLED = "CANCELLED"


class IntakeMode(str, Enum):
    """Supported ways to enter a project-level brief."""

    USER_CONFIRMED_BRIEF = "USER_CONFIRMED_BRIEF"
    CODEX_REQUIREMENTS_INTERVIEW = "CODEX_REQUIREMENTS_INTERVIEW"


class ProjectIntakeError(RuntimeError):
    """A bounded, stable-code failure from requirements intake."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        super().__init__(message)


# Compatibility aliases make the state/mode vocabulary easy to consume from
# small integrations without coupling those integrations to enum names.
ProjectBriefState = BriefState
RequirementsIntakeMode = IntakeMode
RequirementsIntakeError = ProjectIntakeError
ProjectRequirementsError = ProjectIntakeError

BRIEF_STATES = tuple(item.value for item in BriefState)
INTAKE_MODES = tuple(item.value for item in IntakeMode)
NEEDS_MORE_USER_INPUT = "NEEDS_MORE_USER_INPUT"
WAITING_USER_APPROVAL = BriefState.WAITING_USER_APPROVAL.value
USER_CONFIRMED_BRIEF = IntakeMode.USER_CONFIRMED_BRIEF.value
CODEX_REQUIREMENTS_INTERVIEW = IntakeMode.CODEX_REQUIREMENTS_INTERVIEW.value
GPT_CALLS = 0
MAX_QUESTION_COUNT = 1


# High-confidence patterns only.  Requirements text containing a credential
# is rejected before it can be persisted; ordinary user prose is not treated
# as a secret merely because it contains words such as "key" or "token".
_SECRET_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.I),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|sk-proj-[A-Za-z0-9_-]{20,})\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b", re.I),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
)
_FORBIDDEN_KEY_NAMES = {
    "api_key",
    "apikey",
    "access_token",
    "auth_token",
    "client_secret",
    "cookie",
    "cookies",
    "dom",
    "local_storage",
    "password",
    "passwd",
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
    # A confidence score would be pseudo-precision in this workflow.  The
    # contract intentionally has no confidence field at all.
    "confidence",
    "confidence_score",
}

_GENERIC_ROUGH_REQUIREMENTS = {
    "想做成这个工作流",
    "想做成這個工作流",
    "want to make this workflow",
    "make it this workflow",
    "make this workflow",
    "i want this workflow",
}

_FIELD_ALIASES = {
    "project_goal": "goal",
    "project_objective": "goal",
    "objective": "goal",
    "user_visible_goal": "desired_outcome",
    "outcome": "desired_outcome",
    "desired_result": "desired_outcome",
    "problem": "problem_statement",
    "problem_statement": "problem_statement",
    "input": "input",
    "inputs": "input",
    "input_refs": "input",
    "expected_output": "expected_output",
    "expected_outputs": "expected_output",
    "output": "expected_output",
    "outputs": "expected_output",
    "acceptance": "acceptance_criteria",
    "acceptance_criteria": "acceptance_criteria",
    "success": "success_criteria",
    "success_conditions": "success_criteria",
    "out_of_scope": "non_goals",
    "not_in_scope": "non_goals",
    "stakeholder": "stakeholders",
}
_TEXT_FIELDS = {
    "title",
    "problem_statement",
    "goal",
    "desired_outcome",
    "priority",
}
_FREEFORM_FIELDS = {
    # Inputs and outputs can be a short description, a list of paths/assets,
    # or a small structured value supplied by the user.  Keep the original
    # shape while applying the same bounded safety checks as text/list fields.
    "input",
    "expected_output",
}
_LIST_FIELDS = {
    "success_criteria",
    "acceptance_criteria",
    "constraints",
    "non_goals",
    "scope",
    "stakeholders",
    "preferences",
}

_REQUIREMENT_BASELINE_FIELDS = (
    "problem",
    "desired_outcome",
    "input",
    "expected_output",
    "success_criteria",
    "constraints",
    "non_goals",
    "scope",
    "preferences",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_text(value: Any, field: str, *, required: bool = False, max_length: int = 20_000) -> str:
    if not isinstance(value, str):
        raise ProjectIntakeError("INPUT_INVALID", f"{field} must be a string")
    result = value.strip()
    if required and not result:
        raise ProjectIntakeError("INPUT_INVALID", f"{field} must be non-empty")
    if len(result) > max_length:
        raise ProjectIntakeError("INPUT_TOO_LONG", f"{field} is too long")
    if "\x00" in result:
        raise ProjectIntakeError("INPUT_INVALID", f"{field} contains a NUL character")
    return result


def _assert_safe(value: Any, *, path: str = "$", depth: int = 0) -> None:
    """Reject secrets and pseudo-confidence fields before persistence."""

    if depth > 12:
        raise ProjectIntakeError("INPUT_TOO_DEEP", "input nesting is too deep to validate safely")
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key).strip().lower().replace("-", "_")
            if key_text in _FORBIDDEN_KEY_NAMES:
                raise ProjectIntakeError("SECRET_OR_CONFIDENCE_REJECTED", f"forbidden field at {path}")
            _assert_safe(child, path=f"{path}.{key}", depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _assert_safe(child, path=f"{path}[{index}]", depth=depth + 1)
        return
    if isinstance(value, str):
        for pattern in _SECRET_VALUE_PATTERNS:
            if pattern.search(value):
                raise ProjectIntakeError("SECRET_REJECTED", f"high-confidence secret at {path}")
        return
    if value is None or isinstance(value, (bool, int, float)):
        return
    raise ProjectIntakeError("INPUT_INVALID", f"unsupported value at {path}")


def _normalise_mode(value: IntakeMode | str | None) -> str:
    if value is None:
        return IntakeMode.CODEX_REQUIREMENTS_INTERVIEW.value
    if isinstance(value, IntakeMode):
        return value.value
    if not isinstance(value, str):
        raise ProjectIntakeError("MODE_INVALID", "intake mode must be a supported string")
    candidate = value.strip().upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "USER_CONFIRMED": IntakeMode.USER_CONFIRMED_BRIEF.value,
        "CONFIRMED": IntakeMode.USER_CONFIRMED_BRIEF.value,
        "INTERVIEW": IntakeMode.CODEX_REQUIREMENTS_INTERVIEW.value,
        "CODEX_INTERVIEW": IntakeMode.CODEX_REQUIREMENTS_INTERVIEW.value,
    }
    candidate = aliases.get(candidate, candidate)
    if candidate not in INTAKE_MODES:
        raise ProjectIntakeError("MODE_INVALID", "intake mode must be USER_CONFIRMED_BRIEF or CODEX_REQUIREMENTS_INTERVIEW")
    return candidate


def _project_id(root: Path) -> str:
    # The resolved, case-folded repository path is a local identity key.  It
    # remains stable when a user revises the brief and avoids random duplicate
    # project identities on repeated ``requirements-init`` calls.
    identity_key = root.as_posix().casefold().encode("utf-8")
    return "project-" + hashlib.sha256(identity_key).hexdigest()[:24]


def project_identity(project_root: str | os.PathLike[str] = ".") -> dict[str, str]:
    """Return the stable identity used by all brief revisions."""

    root = resolve_project_root(project_root)
    return {
        "project_id": _project_id(root),
        "repository_root": root.as_posix(),
        "identity_method": "resolved_repository_path_sha256",
    }


def _looks_generic_requirement(value: str | None) -> bool:
    if not isinstance(value, str):
        return True
    normalised = " ".join(value.strip().casefold().split())
    return not normalised or normalised in _GENERIC_ROUGH_REQUIREMENTS


def _repo_facts(root: Path) -> dict[str, Any]:
    """Collect a tiny, read-only fact pack; never ask the user for these."""

    try:
        names = sorted(item.name for item in root.iterdir() if item.name not in {".research"})[:80]
    except OSError:
        names = []
    common_markers = {
        "pyproject.toml": "python",
        "requirements.txt": "python",
        "package.json": "node",
        "Cargo.toml": "rust",
        "go.mod": "go",
        "pom.xml": "java",
        "README.md": "readme",
    }
    detected = sorted({label for filename, label in common_markers.items() if (root / filename).exists()})
    return {
        "checked_read_only": True,
        "repository_name": root.name,
        "has_git_metadata": (root / ".git").exists(),
        "top_level_entries": names,
        "detected_project_markers": detected,
    }


def _string_list(value: Any, field: str) -> list[str]:
    if isinstance(value, str):
        text = _safe_text(value, field)
        return [text] if text else []
    if not isinstance(value, list):
        raise ProjectIntakeError("INPUT_INVALID", f"{field} must be a string or array of strings")
    result: list[str] = []
    for index, item in enumerate(value):
        text = _safe_text(item, f"{field}[{index}]", required=True, max_length=8_000)
        if text not in result:
            result.append(text)
    return result


def _freeform_value(value: Any, field: str) -> Any:
    """Normalize a bounded input/output value without losing its shape."""

    if isinstance(value, str):
        return _safe_text(value, field)
    if isinstance(value, list):
        return _string_list(value, field)
    if isinstance(value, Mapping):
        _assert_safe(value, path=field)
        return copy.deepcopy(dict(value))
    raise ProjectIntakeError("INPUT_INVALID", f"{field} must be text, an array, or an object")


def _value_present(value: Any) -> bool:
    """Return whether a baseline value carries user-meaningful content."""

    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict)):
        return any(_value_present(item) for item in value) if not isinstance(value, dict) else bool(value)
    return value is not None


def _empty_brief() -> dict[str, Any]:
    return {
        "title": "",
        "problem_statement": "",
        "goal": "",
        "desired_outcome": "",
        "input": "",
        "expected_output": "",
        "success_criteria": [],
        "acceptance_criteria": [],
        "constraints": [],
        "non_goals": [],
        "scope": [],
        "stakeholders": [],
        "preferences": [],
    }


def _canonical_brief_fields(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return _empty_brief()
    if not isinstance(value, Mapping):
        raise ProjectIntakeError("BRIEF_INVALID", "brief must be an object")
    _assert_safe(value, path="brief")
    result = _empty_brief()
    for raw_key, raw_value in value.items():
        key = str(raw_key).strip()
        canonical_key = _FIELD_ALIASES.get(key, key)
        if canonical_key in _TEXT_FIELDS:
            result[canonical_key] = _safe_text(raw_value, f"brief.{canonical_key}")
        elif canonical_key in _FREEFORM_FIELDS:
            result[canonical_key] = _freeform_value(raw_value, f"brief.{canonical_key}")
        elif canonical_key in _LIST_FIELDS:
            result[canonical_key] = _string_list(raw_value, f"brief.{canonical_key}")
        elif canonical_key in result:
            # Keep future brief fields JSON-safe, while ensuring they cannot
            # smuggle a transcript or secret through arbitrary nested data.
            result[canonical_key] = copy.deepcopy(raw_value)
        else:
            # Preserve user-authored extension fields in this same canonical
            # object.  There is no separate metadata/answers file.
            result[canonical_key] = copy.deepcopy(raw_value)
    return result


def _apply_brief_update(current: Mapping[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    """Merge one user update without retaining a turn-by-turn transcript."""

    _assert_safe(update, path="update")
    merged = copy.deepcopy(dict(current))
    nested = update.get("brief")
    source: Mapping[str, Any]
    if nested is not None:
        if not isinstance(nested, Mapping):
            raise ProjectIntakeError("BRIEF_INVALID", "brief update must be an object")
        source = nested
        # Also accept explicit top-level fields alongside ``brief``.
        top_level = {key: value for key, value in update.items() if key != "brief"}
        if top_level:
            source = {**dict(nested), **top_level}
    else:
        source = update
    for raw_key, raw_value in source.items():
        key = _FIELD_ALIASES.get(str(raw_key).strip(), str(raw_key).strip())
        if key in {
            "state",
            "status",
            "project_id",
            "project_identity",
            "repository_root",
            "revision",
            "schema_version",
            "original_requirement",
            "requirement_baseline",
            "requirement_sources",
        }:
            raise ProjectIntakeError("IMMUTABLE_FIELD", f"{key} is controlled by intake")
        if key in _TEXT_FIELDS:
            merged[key] = _safe_text(raw_value, f"brief.{key}")
        elif key in _FREEFORM_FIELDS:
            merged[key] = _freeform_value(raw_value, f"brief.{key}")
        elif key in _LIST_FIELDS:
            merged[key] = _string_list(raw_value, f"brief.{key}")
        else:
            merged[key] = copy.deepcopy(raw_value)
    _assert_safe(merged, path="brief")
    return merged


def _baseline_source_fields(document: Mapping[str, Any] | None) -> set[str]:
    """Return the fields explicitly supplied by the user for this revision.

    The source marker is deliberately small metadata.  It lets us keep a
    parsed interpretation of ``rough_requirement`` in ``inferred`` while
    preserving answers and direct brief fields as ``confirmed`` after a
    process restart.
    """

    if not isinstance(document, Mapping):
        return set()
    sources = document.get("requirement_sources")
    if not isinstance(sources, Mapping):
        return set()
    fields = sources.get("confirmed_fields")
    if not isinstance(fields, list):
        return set()
    return {str(item) for item in fields if isinstance(item, str) and item.strip()}


def _source_field_names(value: Mapping[str, Any] | None) -> set[str]:
    """Map direct brief input keys to their canonical brief names."""

    if not isinstance(value, Mapping):
        return set()
    nested = value.get("brief")
    if isinstance(nested, Mapping):
        value = {**dict(nested), **{key: child for key, child in value.items() if key != "brief"}}
    names: set[str] = set()
    for raw_key, raw_value in value.items():
        key = _FIELD_ALIASES.get(str(raw_key).strip(), str(raw_key).strip())
        if key in _TEXT_FIELDS or key in _FREEFORM_FIELDS or key in _LIST_FIELDS:
            if _value_present(raw_value):
                names.add(key)
    return names


def _brief_baseline_value(brief: Mapping[str, Any], field: str) -> Any:
    """Read one Requirement Baseline field from the compatible brief shape."""

    if field == "problem":
        return brief.get("problem_statement", "")
    if field == "desired_outcome":
        return brief.get("desired_outcome", "")
    if field in {"input", "expected_output"}:
        return brief.get(field, "")
    return brief.get(field, [] if field in {"success_criteria", "constraints", "non_goals", "scope", "preferences"} else "")


def _build_requirement_baseline(
    brief: Mapping[str, Any],
    rough_requirement: str | None = None,
    *,
    confirmed_fields: set[str] | None = None,
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical, three-way Requirement Baseline projection.

    ``confirmed`` contains direct user facts, ``inferred`` contains only
    bounded local interpretations, and ``unknown`` names fields that still
    need attention.  ``blocking_unknown`` is a deliberately explicit subset
    used by intake lifecycle decisions; optional context can remain visible
    without causing an endless interview.
    """

    direct = set(confirmed_fields or set())
    previous_confirmed = previous.get("confirmed", {}) if isinstance(previous, Mapping) else {}
    if isinstance(previous_confirmed, Mapping):
        # Existing classifications survive a refresh unless the caller marks
        # the field as a new direct user correction.
        direct.update(str(key) for key in previous_confirmed)

    confirmed: dict[str, Any] = {}
    inferred: dict[str, Any] = {}
    unknown: list[str] = []
    blocking_unknown: list[str] = []

    def add_confirmed(name: str, value: Any) -> bool:
        if _value_present(value):
            confirmed[name] = copy.deepcopy(value)
            return True
        return False

    def add_inferred(name: str, value: Any) -> bool:
        if _value_present(value):
            inferred[name] = copy.deepcopy(value)
            return True
        return False

    def add_explicit(
        name: str,
        value: Any,
        *,
        source_name: str | None = None,
        allow_empty: bool = False,
    ) -> bool:
        if (source_name or name) in direct and (allow_empty or _value_present(value)):
            confirmed[name] = copy.deepcopy(value)
            return True
        return False

    problem = _brief_baseline_value(brief, "problem")
    goal = brief.get("goal", "")
    desired = _brief_baseline_value(brief, "desired_outcome")
    input_value = _brief_baseline_value(brief, "input")
    expected = _brief_baseline_value(brief, "expected_output")
    success = brief.get("success_criteria") or brief.get("acceptance_criteria") or []

    # A problem statement is a separate field.  The legacy ``goal`` slot is
    # retained as a direct project target, then used only as an interpretation
    # when a user did not supply a problem statement.
    if "problem_statement" in direct:
        add_explicit("problem", problem, source_name="problem_statement")
    elif _value_present(problem):
        add_confirmed("problem", problem)
    elif _value_present(goal):
        add_inferred("problem", goal)
    elif _value_present(desired):
        add_inferred("problem", desired)

    if "desired_outcome" in direct:
        add_explicit("desired_outcome", desired)
    elif _value_present(desired):
        add_confirmed("desired_outcome", desired)
    elif _value_present(goal):
        add_inferred("desired_outcome", goal)

    if "input" in direct:
        add_explicit("input", input_value)
    elif _value_present(input_value):
        add_confirmed("input", input_value)
    elif not _value_present(problem):
        # Existing Core V1 style briefs describe a local target but often do
        # not spell out an input.  The local workspace is a bounded inference
        # for that compatibility shape; an explicit problem statement still
        # requires the user to name the real input.
        add_inferred("input", ["current local workspace"])

    if "expected_output" in direct:
        add_explicit("expected_output", expected)
    elif _value_present(expected):
        add_confirmed("expected_output", expected)
    elif _value_present(desired):
        add_inferred("expected_output", desired)
    elif _value_present(goal):
        add_inferred("expected_output", goal)

    if "success_criteria" in direct or "acceptance_criteria" in direct:
        if _value_present(success):
            add_confirmed("success_criteria", success)
    elif _value_present(success):
        add_confirmed("success_criteria", success)

    for name in ("constraints", "non_goals", "scope", "preferences"):
        value = brief.get(name, [])
        if name in direct:
            add_explicit(name, value, allow_empty=True)
        elif _value_present(value):
            add_confirmed(name, value)

    # Any unfilled baseline slot is visible.  Only missing fields that can
    # change the next design decision block the transition to approval.
    for name in _REQUIREMENT_BASELINE_FIELDS:
        if name in confirmed or name in inferred:
            continue
        unknown.append(name)
        if name in {"problem", "desired_outcome", "input", "expected_output", "success_criteria"}:
            blocking_unknown.append(name)

    # The raw requirement remains a user-owned value outside this projection;
    # expose its existence as a confirmed source without treating the whole
    # sentence as a parsed fact.
    if isinstance(rough_requirement, str) and rough_requirement.strip():
        confirmed["original_requirement"] = rough_requirement.strip()

    return {
        "schema_version": REQUIREMENT_BASELINE_SCHEMA_VERSION,
        "confirmed": confirmed,
        "inferred": inferred,
        "unknown": unknown,
        "blocking_unknown": blocking_unknown,
    }


def _requirement_baseline_display(baseline: Mapping[str, Any]) -> dict[str, Any]:
    """Create the compact human-facing confirmation projection."""

    confirmed = baseline.get("confirmed", {}) if isinstance(baseline, Mapping) else {}
    inferred = baseline.get("inferred", {}) if isinstance(baseline, Mapping) else {}
    unknown = baseline.get("unknown", []) if isinstance(baseline, Mapping) else []
    labels = {
        "problem": "要解决的问题",
        "desired_outcome": "期望结果",
        "input": "输入/已有资产",
        "expected_output": "最终输出",
        "success_criteria": "成功标准",
        "constraints": "重要约束",
        "non_goals": "明确不做",
        "scope": "范围",
        "preferences": "用户偏好",
    }
    lines: list[str] = []
    for name in _REQUIREMENT_BASELINE_FIELDS:
        value = confirmed.get(name) if isinstance(confirmed, Mapping) else None
        source = "已确认"
        if not _value_present(value):
            value = inferred.get(name) if isinstance(inferred, Mapping) else None
            source = "系统推断"
        if _value_present(value):
            rendered = ", ".join(str(item) for item in value) if isinstance(value, list) else str(value)
            lines.append(f"{labels.get(name, name)}（{source}）：{rendered}")
    if isinstance(unknown, list) and unknown:
        lines.append("尚未确认：" + "、".join(str(item) for item in unknown))
    return {"fields": copy.deepcopy(dict(baseline)), "text": "；".join(lines)}


def _has_goal(brief: Mapping[str, Any], rough_requirement: str | None) -> bool:
    for key in ("goal", "desired_outcome", "problem_statement"):
        if isinstance(brief.get(key), str) and brief[key].strip():
            return True
    return isinstance(rough_requirement, str) and not _looks_generic_requirement(rough_requirement)


def _has_success_criteria(brief: Mapping[str, Any]) -> bool:
    return any(
        isinstance(brief.get(key), list) and any(str(item).strip() for item in brief[key])
        for key in ("success_criteria", "acceptance_criteria")
    )


def _question_for(
    brief: Mapping[str, Any],
    rough_requirement: str | None,
    *,
    baseline: Mapping[str, Any] | None = None,
) -> dict[str, str] | None:
    """Return exactly one core question, or ``None`` when the brief is ready."""

    if not _has_goal(brief, rough_requirement):
        return {
            "id": "desired_outcome",
            "kind": "core",
            "text": "你希望这个项目最终为谁解决什么问题，并带来什么可观察的结果？",
        }
    checked_baseline = baseline if isinstance(baseline, Mapping) else _build_requirement_baseline(brief, rough_requirement)
    blocking = checked_baseline.get("blocking_unknown", [])
    if isinstance(blocking, list) and "input" in blocking:
        return {
            "id": "input",
            "kind": "core",
            "text": "这项工作要处理的输入是什么？请说明已有资产、数据或当前项目状态。",
        }
    if isinstance(blocking, list) and "expected_output" in blocking:
        return {
            "id": "expected_output",
            "kind": "core",
            "text": "最终需要产出什么可观察的结果或 artifact？",
        }
    if not _has_success_criteria(brief):
        return {
            "id": "success_criteria",
            "kind": "core",
            "text": "用哪些可观察、可验证的结果判断这项工作完成？请给出一到三个验收条件。",
        }
    # Constraints, scope, and preferences are optional user-provided detail.
    # Do not manufacture another question for them: repository facts are
    # collected read-only and an explicit user can still add these fields via
    # ``requirements-update``.
    return None


def _is_complete(brief: Mapping[str, Any], rough_requirement: str | None) -> bool:
    baseline = _build_requirement_baseline(brief, rough_requirement)
    blocking = baseline.get("blocking_unknown", [])
    return _has_goal(brief, rough_requirement) and _has_success_criteria(brief) and not blocking


def _refresh_lifecycle(document: dict[str, Any], *, confirmed_fields: set[str] | None = None) -> None:
    """Recompute derived lifecycle/question fields in place."""

    brief = document.get("brief") if isinstance(document.get("brief"), Mapping) else {}
    previous_baseline = document.get("requirement_baseline")
    sources = _baseline_source_fields(document)
    if confirmed_fields:
        sources.update(confirmed_fields)
    document["requirement_sources"] = {"confirmed_fields": sorted(sources)}
    baseline = _build_requirement_baseline(
        brief,
        document.get("rough_requirement"),
        confirmed_fields=sources,
        previous=previous_baseline if isinstance(previous_baseline, Mapping) else None,
    )
    document["requirement_baseline"] = baseline

    state = document.get("state")
    if state in {BriefState.APPROVED.value, BriefState.CANCELLED.value}:
        document["next_question"] = None
        document["questions"] = []
        document["next_action"] = state
        document["status"] = state
        document["brief_status"] = state
        return
    question = _question_for(brief, document.get("rough_requirement"), baseline=baseline)
    if question is None:
        document["state"] = BriefState.WAITING_USER_APPROVAL.value
        document["status"] = BriefState.WAITING_USER_APPROVAL.value
        document["brief_status"] = BriefState.WAITING_USER_APPROVAL.value
        document["next_action"] = BriefState.WAITING_USER_APPROVAL.value
        document["next_question"] = None
        document["questions"] = []
    else:
        document["state"] = BriefState.DRAFT.value
        document["status"] = NEEDS_MORE_USER_INPUT
        document["brief_status"] = BriefState.DRAFT.value
        document["next_action"] = NEEDS_MORE_USER_INPUT
        document["next_question"] = question
        document["questions"] = [copy.deepcopy(question)]


def _migrate_document(document: Mapping[str, Any], root: Path) -> tuple[dict[str, Any], bool]:
    """Fill Requirement Baseline defaults for pre-baseline briefs.

    The canonical file remains ``project_brief.v1``.  Migration is therefore
    an additive defaulting step, preserving the existing revision and project
    identity while making the new baseline available to runtime consumers.
    """

    migrated = copy.deepcopy(dict(document))
    changed = False
    identity = project_identity(root)

    if migrated.get("schema_version") != PROJECT_BRIEF_SCHEMA_VERSION:
        migrated["schema_version"] = PROJECT_BRIEF_SCHEMA_VERSION
        changed = True
    if not isinstance(migrated.get("project_identity"), Mapping):
        migrated["project_identity"] = identity
        changed = True
    if not migrated.get("project_id"):
        migrated["project_id"] = identity["project_id"]
        changed = True
    if not migrated.get("repository_root"):
        migrated["repository_root"] = identity["repository_root"]
        changed = True
    if "intake_mode" not in migrated:
        migrated["intake_mode"] = migrated.get("mode", IntakeMode.CODEX_REQUIREMENTS_INTERVIEW.value)
        changed = True
    if "mode" not in migrated:
        migrated["mode"] = migrated["intake_mode"]
        changed = True
    if "rough_requirement" not in migrated:
        migrated["rough_requirement"] = ""
        changed = True
    if "original_requirement" not in migrated:
        # ``rough_requirement`` was the only raw user text in the original
        # schema, so it is the safest lossless migration source.
        migrated["original_requirement"] = str(migrated.get("rough_requirement") or "")
        changed = True
    if migrated.get("state") not in BRIEF_STATES:
        migrated["state"] = BriefState.DRAFT.value
        changed = True
    if "status" not in migrated:
        migrated["status"] = (
            migrated["state"]
            if migrated["state"] in {BriefState.WAITING_USER_APPROVAL.value, BriefState.APPROVED.value, BriefState.CANCELLED.value}
            else NEEDS_MORE_USER_INPUT
        )
        changed = True
    if "brief_status" not in migrated:
        migrated["brief_status"] = migrated["state"]
        changed = True
    if "next_action" not in migrated:
        migrated["next_action"] = migrated["status"]
        changed = True
    for key, default in (
        ("repository_facts", _repo_facts(root)),
        ("revision", 1),
        ("created_at", _now()),
        ("updated_at", _now()),
        ("approved_at", None),
        ("approval", None),
        ("cancelled_at", None),
        ("cancel_reason", None),
        ("cancellation", None),
        ("next_question", None),
        ("questions", []),
        ("gpt_calls", 0),
        ("external_calls", 0),
    ):
        if key not in migrated:
            migrated[key] = copy.deepcopy(default)
            changed = True
    if not isinstance(migrated.get("brief"), Mapping):
        migrated["brief"] = _empty_brief()
        changed = True
    else:
        canonical = _canonical_brief_fields(migrated["brief"])
        if canonical != migrated["brief"]:
            migrated["brief"] = canonical
            changed = True

    if not isinstance(migrated.get("requirement_sources"), Mapping):
        brief_value = migrated.get("brief", {})
        fields = _source_field_names(brief_value if isinstance(brief_value, Mapping) else None)
        rough = str(migrated.get("rough_requirement") or "").strip()
        # A legacy auto-filled goal came from the raw sentence and should be
        # classified as an inference rather than as a direct brief fact.
        if rough and isinstance(brief_value, Mapping) and brief_value.get("goal") == rough:
            fields.discard("goal")
        migrated["requirement_sources"] = {"confirmed_fields": sorted(fields)}
        changed = True

    baseline = migrated.get("requirement_baseline")
    refreshed = _build_requirement_baseline(
        migrated.get("brief", {}),
        migrated.get("rough_requirement"),
        confirmed_fields=_baseline_source_fields(migrated),
        previous=baseline if isinstance(baseline, Mapping) else None,
    )
    if baseline != refreshed:
        migrated["requirement_baseline"] = refreshed
        changed = True
    if migrated.get("state") == BriefState.DRAFT.value:
        # Older drafts may have no persisted question.  Recompute exactly one
        # question from the migrated baseline so they remain answerable.
        before = copy.deepcopy(migrated)
        _refresh_lifecycle(migrated)
        after = migrated
        if before != after:
            changed = True
    elif migrated.get("state") in {
        BriefState.WAITING_USER_APPROVAL.value,
        BriefState.APPROVED.value,
        BriefState.CANCELLED.value,
    }:
        if migrated.get("next_question") is not None or migrated.get("questions") != []:
            migrated["next_question"] = None
            migrated["questions"] = []
            changed = True
    return migrated, changed


def _validate_document(document: Mapping[str, Any], root: Path) -> dict[str, Any]:
    if not isinstance(document, Mapping):
        raise ProjectIntakeError("BRIEF_INVALID", "project brief must be an object")
    detached = copy.deepcopy(dict(document))
    _assert_safe(detached, path="project_brief")
    try:
        validate_instance(detached, load_schema("project_brief"))
    except ContractValidationError as exc:
        raise ProjectIntakeError("BRIEF_INVALID", "project brief failed schema validation") from exc
    identity = project_identity(root)
    if detached.get("project_id") != identity["project_id"]:
        raise ProjectIntakeError("PROJECT_IDENTITY_MISMATCH", "project brief belongs to another project")
    if detached.get("repository_root") != identity["repository_root"]:
        raise ProjectIntakeError("PROJECT_IDENTITY_MISMATCH", "project brief repository root does not match project")
    nested_identity = detached.get("project_identity")
    if not isinstance(nested_identity, Mapping) or nested_identity.get("project_id") != identity["project_id"]:
        raise ProjectIntakeError("PROJECT_IDENTITY_MISMATCH", "project brief identity is inconsistent")
    if detached.get("state") not in BRIEF_STATES:
        raise ProjectIntakeError("BRIEF_INVALID", "project brief has an unknown state")
    if detached.get("gpt_calls") != 0:
        raise ProjectIntakeError("GPT_CALL_GUARD", "requirements intake permits zero GPT calls")
    if detached.get("external_calls", 0) != 0:
        raise ProjectIntakeError("EXTERNAL_CALL_GUARD", "requirements intake permits zero external calls")
    baseline = detached.get("requirement_baseline")
    if not isinstance(baseline, Mapping):
        raise ProjectIntakeError("BRIEF_INVALID", "project brief requirement baseline is missing")
    for key in ("confirmed", "inferred", "unknown", "blocking_unknown"):
        if key not in baseline:
            raise ProjectIntakeError("BRIEF_INVALID", "project brief requirement baseline is incomplete")
    if not isinstance(baseline.get("confirmed"), Mapping) or not isinstance(baseline.get("inferred"), Mapping):
        raise ProjectIntakeError("BRIEF_INVALID", "project brief requirement baseline classifications are invalid")
    if not isinstance(baseline.get("unknown"), list) or not isinstance(baseline.get("blocking_unknown"), list):
        raise ProjectIntakeError("BRIEF_INVALID", "project brief requirement baseline unknowns are invalid")
    if detached.get("state") == BriefState.DRAFT.value:
        if detached.get("status") != NEEDS_MORE_USER_INPUT or len(detached.get("questions", [])) != 1:
            raise ProjectIntakeError("BRIEF_INVALID", "DRAFT must expose exactly one next question")
    else:
        if detached.get("questions") != [] or detached.get("next_question") is not None:
            raise ProjectIntakeError("BRIEF_INVALID", "terminal/approval states cannot retain questions")
    return detached


def validate_project_brief(document: Mapping[str, Any], project_root: str | os.PathLike[str] = ".") -> dict[str, Any]:
    """Validate and detach a canonical project brief."""

    root = resolve_project_root(project_root)
    migrated, _ = _migrate_document(document, root)
    return _validate_document(migrated, root)


def assert_gpt_calls_zero(document: Mapping[str, Any] | None = None) -> int:
    """Code-level guard used by tests and integrations.

    The intake module never imports or calls a GPT transport.  If a caller
    hands this guard a persisted snapshot, a non-zero counter is rejected.
    """

    if document is not None and document.get("gpt_calls", 0) != 0:
        raise ProjectIntakeError("GPT_CALL_GUARD", "requirements intake permits zero GPT calls")
    return 0


def gpt_call_count() -> int:
    """Return the immutable number of GPT calls made by this module (zero)."""

    return 0


def _result(operation: str, document: Mapping[str, Any], root: Path, *, entrypoint: str | None = None) -> dict[str, Any]:
    checked = _validate_document(document, root)
    question = copy.deepcopy(checked.get("next_question"))
    result: dict[str, Any] = {
        "schema_version": REQUIREMENTS_RESULT_SCHEMA_VERSION,
        "operation": operation,
        "project_id": checked["project_id"],
        "state": checked["state"],
        "status": checked["status"],
        "brief_status": checked["brief_status"],
        "next_action": checked["next_action"],
        "question": question,
        "questions": [question] if question is not None else [],
        "question_count": 1 if question is not None else 0,
        "project_brief_path": PROJECT_BRIEF_RELATIVE_PATH.as_posix(),
        "project_brief": copy.deepcopy(checked),
        "brief": copy.deepcopy(checked["brief"]),
        "original_requirement": checked.get("original_requirement", checked.get("rough_requirement", "")),
        "requirement_baseline": copy.deepcopy(checked.get("requirement_baseline", {})),
        "requirement_baseline_display": _requirement_baseline_display(checked.get("requirement_baseline", {})),
        "gpt_calls": 0,
        "external_calls": 0,
        "side_effects": {
            "project_discovery_started": False,
            "stage_started": False,
            "gpt_calls": 0,
        },
    }
    if entrypoint is not None:
        result["entrypoint"] = entrypoint
    _assert_safe(result, path="result")
    return result


class ProjectRequirementsIntake:
    """Persistent, independent requirements-intake service for one project."""

    def __init__(self, project_root: str | os.PathLike[str] = ".") -> None:
        self.root = resolve_project_root(project_root)
        self.path = self.root / PROJECT_BRIEF_RELATIVE_PATH

    @property
    def project_id(self) -> str:
        """Stable identity for this service's project."""

        return project_identity(self.root)["project_id"]

    @property
    def state(self) -> dict[str, Any] | None:
        """Return the validated canonical document, if one exists."""

        return self._load()

    @property
    def current_question(self) -> dict[str, str] | None:
        document = self._load()
        if document is None:
            return None
        value = document.get("next_question")
        return copy.deepcopy(value) if isinstance(value, Mapping) else None

    def _load(self) -> dict[str, Any] | None:
        try:
            document = load_project_brief(self.root)
        except ProjectStateError as exc:
            raise ProjectIntakeError("BRIEF_UNREADABLE", "canonical project brief could not be loaded") from exc
        if document is None:
            return None
        migrated, changed = _migrate_document(document, self.root)
        checked = _validate_document(migrated, self.root)
        if changed:
            try:
                save_project_brief(self.root, checked)
            except ProjectStateError as exc:
                raise ProjectIntakeError("BRIEF_WRITE_FAILED", "migrated project brief could not be written") from exc
        return checked

    def _save(self, document: Mapping[str, Any]) -> dict[str, Any]:
        checked = _validate_document(document, self.root)
        try:
            save_project_brief(self.root, checked)
        except ProjectStateError as exc:
            raise ProjectIntakeError("BRIEF_WRITE_FAILED", "canonical project brief could not be written") from exc
        return checked

    def show(self) -> dict[str, Any]:
        document = self._load()
        if document is None:
            raise ProjectIntakeError("BRIEF_NOT_FOUND", "no canonical project brief exists; run requirements-init first")
        return _result("requirements-show", document, self.root)

    def initialize(
        self,
        *,
        mode: IntakeMode | str | None = None,
        rough_requirement: str | None = None,
        requirement: str | None = None,
        raw_requirement: str | None = None,
        original_requirement: str | None = None,
        user_requirement: str | None = None,
        brief: Mapping[str, Any] | None = None,
        text: str | None = None,
        entrypoint: str | None = None,
    ) -> dict[str, Any]:
        """Create or revise the one brief for this project.

        Repeated initialization never creates a second identity.  Supplying
        new user content is treated as a revision of the existing identity;
        a cancelled brief remains fail-closed and must not be silently
        resurrected.
        """

        selected_mode = _normalise_mode(mode)
        raw_text = rough_requirement
        if raw_text is None:
            raw_text = requirement
        if raw_text is None:
            raw_text = raw_requirement
        if raw_text is None:
            raw_text = original_requirement
        if raw_text is None:
            raw_text = user_requirement
        if raw_text is None:
            raw_text = text
        if raw_text is not None:
            raw_text = _safe_text(raw_text, "rough_requirement")
        if brief is not None:
            if not isinstance(brief, Mapping):
                raise ProjectIntakeError("BRIEF_INVALID", "brief must be an object")
            _assert_safe(brief, path="brief")

        existing = self._load()
        if existing is not None:
            if existing["state"] == BriefState.CANCELLED.value:
                raise ProjectIntakeError("BRIEF_CANCELLED", "cancelled project brief cannot be resumed")
            has_new_input = raw_text is not None or brief is not None or mode is not None
            if not has_new_input:
                return _result("requirements-init", existing, self.root, entrypoint=entrypoint)
            revised = copy.deepcopy(existing)
            if mode is not None:
                revised["intake_mode"] = selected_mode
                revised["mode"] = selected_mode
            if raw_text is not None:
                revised["rough_requirement"] = raw_text
                if not str(revised.get("original_requirement") or "").strip():
                    revised["original_requirement"] = raw_text
                if not _looks_generic_requirement(raw_text) and not _has_goal(revised["brief"], None):
                    revised["brief"]["goal"] = raw_text
            if brief is not None:
                revised["brief"] = _apply_brief_update(revised["brief"], brief)
            confirmed_fields = _source_field_names(brief)
            if revised["state"] == BriefState.APPROVED.value:
                revised["state"] = BriefState.DRAFT.value
                revised["approved_at"] = None
                revised["approval"] = None
            revised["revision"] = int(revised.get("revision", 0)) + 1
            revised["updated_at"] = _now()
            _refresh_lifecycle(revised, confirmed_fields=confirmed_fields)
            saved = self._save(revised)
            return _result("requirements-update", saved, self.root, entrypoint=entrypoint)

        identity = project_identity(self.root)
        initial_brief = _canonical_brief_fields(brief)
        if raw_text is not None and not _looks_generic_requirement(raw_text) and not _has_goal(initial_brief, None):
            initial_brief["goal"] = raw_text
        now = _now()
        document: dict[str, Any] = {
            "schema_version": PROJECT_BRIEF_SCHEMA_VERSION,
            "project_id": identity["project_id"],
            "project_identity": identity,
            "repository_root": identity["repository_root"],
            "intake_mode": selected_mode,
            "mode": selected_mode,
            "state": BriefState.DRAFT.value,
            "status": NEEDS_MORE_USER_INPUT,
            "brief_status": BriefState.DRAFT.value,
            "next_action": NEEDS_MORE_USER_INPUT,
            "rough_requirement": raw_text or "",
            "original_requirement": raw_text or "",
            "brief": initial_brief,
            # The execution profile is a small, durable policy projection
            # owned by this canonical brief.  It does not introduce another
            # controller, journal, or authority store.
            "execution_profile": default_execution_profile(),
            "requirement_sources": {"confirmed_fields": sorted(_source_field_names(brief))},
            "repository_facts": _repo_facts(self.root),
            "revision": 1,
            "created_at": now,
            "updated_at": now,
            "approved_at": None,
            "approval": None,
            "cancelled_at": None,
            "cancel_reason": None,
            "cancellation": None,
            "next_question": None,
            "questions": [],
            # These counters are explicit guard fields, never a claim about
            # quality or likelihood.
            "gpt_calls": 0,
            "external_calls": 0,
        }
        # A USER_CONFIRMED_BRIEF still passes through the same approval gate;
        # the mode means the content came from the user rather than an
        # interview turn.  A genuinely incomplete user brief remains DRAFT.
        _refresh_lifecycle(document, confirmed_fields=_source_field_names(brief))
        saved = self._save(document)
        return _result("requirements-init", saved, self.root, entrypoint=entrypoint)

    # Friendly aliases for small callers that model intake as a stateful
    # object rather than a CLI service.
    def init(
        self,
        mode: IntakeMode | str | None = None,
        rough_requirement: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return self.initialize(mode=mode, rough_requirement=rough_requirement, **kwargs)

    def start(
        self,
        mode: IntakeMode | str | None = None,
        rough_requirement: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return self.initialize(mode=mode, rough_requirement=rough_requirement, **kwargs)

    def answer(self, answer: str | Mapping[str, Any], *, question_id: str | None = None) -> dict[str, Any]:
        """Answer the single currently exposed core question."""

        document = self._load()
        if document is None:
            raise ProjectIntakeError("BRIEF_NOT_FOUND", "run requirements-init before answering")
        if document["state"] != BriefState.DRAFT.value:
            raise ProjectIntakeError("ANSWER_NOT_ALLOWED", "answers are accepted only while the brief is DRAFT")
        current = document.get("next_question")
        if not isinstance(current, Mapping):
            raise ProjectIntakeError("QUESTION_NOT_FOUND", "the brief has no pending question")
        expected_id = str(current.get("id"))
        if question_id is not None and str(question_id).strip() != expected_id:
            raise ProjectIntakeError("QUESTION_MISMATCH", "answer does not match the current question")
        if isinstance(answer, Mapping):
            update = dict(answer)
            if len(update) == 1 and expected_id not in update and "brief" not in update:
                # Accept a conventional ``{"answer": "..."}`` wrapper.
                update = {expected_id: update.get("answer")}
        else:
            update = {expected_id: _safe_text(answer, "answer", required=True)}
        revised = copy.deepcopy(document)
        revised["brief"] = _apply_brief_update(revised["brief"], update)
        revised["revision"] = int(revised.get("revision", 0)) + 1
        revised["updated_at"] = _now()
        _refresh_lifecycle(revised, confirmed_fields=_source_field_names(update))
        saved = self._save(revised)
        return _result("requirements-answer", saved, self.root)

    def apply_answer(self, answer: str | Mapping[str, Any], *, question_id: str | None = None) -> dict[str, Any]:
        return self.answer(answer, question_id=question_id)

    def update(self, updates: Mapping[str, Any] | str, *, rough_requirement: str | None = None) -> dict[str, Any]:
        """Apply an explicit user revision while preserving project identity."""

        document = self._load()
        if document is None:
            raise ProjectIntakeError("BRIEF_NOT_FOUND", "run requirements-init before updating")
        if document["state"] == BriefState.CANCELLED.value:
            raise ProjectIntakeError("BRIEF_CANCELLED", "cancelled project brief cannot be updated")
        if isinstance(updates, str):
            try:
                parsed = json.loads(updates)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ProjectIntakeError("UPDATE_INVALID", "update must be a JSON object or mapping") from exc
            updates = parsed
        if not isinstance(updates, Mapping):
            raise ProjectIntakeError("UPDATE_INVALID", "update must be an object")
        revised = copy.deepcopy(document)
        revised["brief"] = _apply_brief_update(revised["brief"], updates)
        if rough_requirement is not None:
            revised["rough_requirement"] = _safe_text(rough_requirement, "rough_requirement")
            if not str(revised.get("original_requirement") or "").strip():
                revised["original_requirement"] = revised["rough_requirement"]
        if revised["state"] == BriefState.APPROVED.value:
            revised["state"] = BriefState.DRAFT.value
            revised["approved_at"] = None
            revised["approval"] = None
        revised["revision"] = int(revised.get("revision", 0)) + 1
        revised["updated_at"] = _now()
        _refresh_lifecycle(revised, confirmed_fields=_source_field_names(updates))
        saved = self._save(revised)
        return _result("requirements-update", saved, self.root)

    def update_brief(self, updates: Mapping[str, Any] | str, *, rough_requirement: str | None = None) -> dict[str, Any]:
        return self.update(updates, rough_requirement=rough_requirement)

    def approve(self, *, actor: str = "local-human", rationale: str = "") -> dict[str, Any]:
        """Approve a ready brief, with no discovery, Stage, or GPT side effect."""

        document = self._load()
        if document is None:
            raise ProjectIntakeError("BRIEF_NOT_FOUND", "run requirements-init before approval")
        if document["state"] == BriefState.CANCELLED.value:
            raise ProjectIntakeError("BRIEF_CANCELLED", "cancelled project brief cannot be approved")
        if document["state"] == BriefState.APPROVED.value:
            result = _result("requirements-approve", document, self.root)
            result["actions_triggered"] = []
            result["approval_requires_explicit_next_step"] = True
            return result
        if document["state"] != BriefState.WAITING_USER_APPROVAL.value:
            raise ProjectIntakeError("BRIEF_NOT_READY", "brief needs more user input before approval")
        actor_text = _safe_text(actor, "actor", required=True, max_length=500)
        rationale_text = _safe_text(rationale, "rationale", max_length=4_000)
        _assert_safe({"actor": actor_text, "rationale": rationale_text}, path="approval")
        revised = copy.deepcopy(document)
        revised["state"] = BriefState.APPROVED.value
        revised["status"] = BriefState.APPROVED.value
        revised["brief_status"] = BriefState.APPROVED.value
        revised["next_action"] = BriefState.APPROVED.value
        revised["next_question"] = None
        revised["questions"] = []
        revised["approved_at"] = _now()
        revised["approval"] = {"actor": actor_text, "rationale": rationale_text}
        revised["revision"] = int(revised.get("revision", 0)) + 1
        revised["updated_at"] = _now()
        saved = self._save(revised)
        result = _result("requirements-approve", saved, self.root)
        result["actions_triggered"] = []
        result["approval_requires_explicit_next_step"] = True
        return result

    def cancel(self, *, actor: str = "local-human", reason: str = "") -> dict[str, Any]:
        """Cancel fail-closed; no later operation may resume this identity."""

        document = self._load()
        if document is None:
            raise ProjectIntakeError("BRIEF_NOT_FOUND", "run requirements-init before cancellation")
        if document["state"] == BriefState.CANCELLED.value:
            result = _result("requirements-cancel", document, self.root)
            result["fail_closed"] = True
            result["actions_triggered"] = []
            return result
        if document["state"] == BriefState.APPROVED.value:
            raise ProjectIntakeError("CANCEL_NOT_ALLOWED", "an approved brief cannot be cancelled")
        actor_text = _safe_text(actor, "actor", required=True, max_length=500)
        reason_text = _safe_text(reason or "cancelled by user", "reason", required=True, max_length=4_000)
        _assert_safe({"actor": actor_text, "reason": reason_text}, path="cancellation")
        revised = copy.deepcopy(document)
        revised["state"] = BriefState.CANCELLED.value
        revised["status"] = BriefState.CANCELLED.value
        revised["brief_status"] = BriefState.CANCELLED.value
        revised["next_action"] = BriefState.CANCELLED.value
        revised["next_question"] = None
        revised["questions"] = []
        revised["cancelled_at"] = _now()
        revised["cancel_reason"] = reason_text
        revised["cancellation"] = {"actor": actor_text, "reason": reason_text}
        revised["revision"] = int(revised.get("revision", 0)) + 1
        revised["updated_at"] = _now()
        saved = self._save(revised)
        result = _result("requirements-cancel", saved, self.root)
        result["fail_closed"] = True
        result["actions_triggered"] = []
        return result


# A short class alias for integrations that use the requirement-domain name.
RequirementsIntake = ProjectRequirementsIntake
ProjectIntake = ProjectRequirementsIntake


def requirements_init(
    project_root: str | os.PathLike[str] = ".",
    *,
    mode: IntakeMode | str | None = None,
    rough_requirement: str | None = None,
    requirement: str | None = None,
    raw_requirement: str | None = None,
    original_requirement: str | None = None,
    user_requirement: str | None = None,
    brief: Mapping[str, Any] | None = None,
    text: str | None = None,
) -> dict[str, Any]:
    return ProjectRequirementsIntake(project_root).initialize(
        mode=mode,
        rough_requirement=rough_requirement,
        requirement=requirement,
        raw_requirement=raw_requirement,
        original_requirement=original_requirement,
        user_requirement=user_requirement,
        brief=brief,
        text=text,
    )


def requirements_show(project_root: str | os.PathLike[str] = ".") -> dict[str, Any]:
    return ProjectRequirementsIntake(project_root).show()


def requirements_answer(
    project_root: str | os.PathLike[str],
    answer: str | Mapping[str, Any],
    *,
    question_id: str | None = None,
) -> dict[str, Any]:
    return ProjectRequirementsIntake(project_root).answer(answer, question_id=question_id)


def requirements_update(
    project_root: str | os.PathLike[str],
    updates: Mapping[str, Any] | str,
    *,
    rough_requirement: str | None = None,
) -> dict[str, Any]:
    return ProjectRequirementsIntake(project_root).update(updates, rough_requirement=rough_requirement)


def requirements_approve(
    project_root: str | os.PathLike[str] = ".",
    *,
    actor: str = "local-human",
    rationale: str = "",
) -> dict[str, Any]:
    return ProjectRequirementsIntake(project_root).approve(actor=actor, rationale=rationale)


def requirements_cancel(
    project_root: str | os.PathLike[str] = ".",
    *,
    actor: str = "local-human",
    reason: str = "",
) -> dict[str, Any]:
    return ProjectRequirementsIntake(project_root).cancel(actor=actor, reason=reason)


# Function aliases used by integrations that prefer verb-first names.
initialize_requirements = requirements_init
init_requirements = requirements_init
show_project_brief = requirements_show
answer_requirements = requirements_answer
update_project_brief = requirements_update
approve_project_brief = requirements_approve
cancel_project_brief = requirements_cancel


def recognize_intake_request(text: str) -> bool:
    """Recognize a natural-language request to enter requirements intake."""

    value = _safe_text(text, "text")
    normalised = " ".join(value.casefold().split())
    return any(phrase in normalised for phrase in _GENERIC_ROUGH_REQUIREMENTS)


def is_requirements_intake_request(text: str) -> bool:
    return recognize_intake_request(text)


def natural_language_entry(
    project_root: str | os.PathLike[str],
    text: str,
    *,
    mode: IntakeMode | str = IntakeMode.CODEX_REQUIREMENTS_INTERVIEW,
) -> dict[str, Any]:
    """Enter intake from a short natural-language request without Web/GPT."""

    if not recognize_intake_request(text):
        raise ProjectIntakeError("INTAKE_REQUEST_NOT_RECOGNIZED", "text did not request the requirements workflow")
    return ProjectRequirementsIntake(project_root).initialize(
        mode=mode,
        rough_requirement=text,
        entrypoint="natural-language",
    )


def handle_natural_language(
    project_root: str | os.PathLike[str],
    text: str,
    *,
    mode: IntakeMode | str = IntakeMode.CODEX_REQUIREMENTS_INTERVIEW,
) -> dict[str, Any]:
    return natural_language_entry(project_root, text, mode=mode)


def check_intake_file_hygiene(project_root: str | os.PathLike[str] = ".") -> dict[str, Any]:
    """Report intake-owned files without touching unrelated Stage artifacts."""

    root = resolve_project_root(project_root)
    research = root / ".research"
    allowed = {"PROJECT_BRIEF.json", "PROJECT_BRIEF.md"}
    forbidden: list[str] = []
    if research.is_dir():
        for path in research.iterdir():
            if not path.is_file():
                continue
            name = path.name
            lowered = name.casefold()
            if ("transcript" in lowered or "question" in lowered or "answers" in lowered or "brief" in lowered) and name not in allowed:
                forbidden.append(name)
    return {
        "canonical_path": PROJECT_BRIEF_RELATIVE_PATH.as_posix(),
        "allowed_optional_files": sorted(allowed),
        "forbidden_intake_files": sorted(forbidden),
        "passed": not forbidden,
    }


def _cli_json(value: str, root: Path, field: str) -> Any:
    """Read a JSON object from an inline value or a user-supplied file."""

    candidate = Path(value).expanduser()
    if candidate.is_file():
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProjectIntakeError("INPUT_INVALID", f"could not read {field}") from exc
    else:
        try:
            raw = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProjectIntakeError("INPUT_INVALID", f"{field} must be JSON or a JSON file") from exc
    if not isinstance(raw, Mapping):
        raise ProjectIntakeError("INPUT_INVALID", f"{field} must contain an object")
    _assert_safe(raw, path=field)
    return dict(raw)


def _cli_brief_args(args: argparse.Namespace) -> dict[str, Any] | None:
    values: dict[str, Any] = {}
    raw_brief = getattr(args, "brief", None)
    if raw_brief:
        parsed = _cli_json(str(raw_brief), Path(getattr(args, "repo", ".")), "brief")
        values.update(parsed)
    direct_fields = {
        "title": getattr(args, "title", None),
        "problem_statement": getattr(args, "problem_statement", None),
        "goal": getattr(args, "goal", None),
        "desired_outcome": getattr(args, "desired_outcome", None),
        "input": getattr(args, "input_value", None),
        "expected_output": getattr(args, "expected_output", None),
        "priority": getattr(args, "priority", None),
        "success_criteria": getattr(args, "success_criteria", None),
        "acceptance_criteria": getattr(args, "acceptance_criteria", None),
        "constraints": getattr(args, "constraints", None),
        "non_goals": getattr(args, "non_goals", None),
        "scope": getattr(args, "scope", None),
        "stakeholders": getattr(args, "stakeholders", None),
    }
    for key, value in direct_fields.items():
        if value not in (None, "", []):
            values[key] = value
    return values or None


def build_requirements_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex-native project requirements intake (Step 11)")
    parser.add_argument("--repo", "-C", "--project-root", default=".", help="project repository (default: current directory)")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("requirements-init", aliases=["requirements_init", "init-requirements"], help="create or revise the canonical project brief")
    init.add_argument("--mode", default=IntakeMode.CODEX_REQUIREMENTS_INTERVIEW.value)
    init.add_argument("rough_requirement_arg", nargs="?")
    init.add_argument("--rough-requirement", "--requirement", "--text", default=None)
    init.add_argument("--brief", "--brief-file", help="inline JSON object or JSON file containing the user brief")
    init.add_argument("--title")
    init.add_argument("--problem-statement")
    init.add_argument("--goal", "--project-goal")
    init.add_argument("--desired-outcome", "--outcome")
    init.add_argument("--input", "--inputs", dest="input_value")
    init.add_argument("--expected-output", "--output", dest="expected_output")
    init.add_argument("--priority")
    init.add_argument("--success-criteria", action="append", default=[])
    init.add_argument("--acceptance-criteria", action="append", default=[])
    init.add_argument("--constraints", action="append", default=[])
    init.add_argument("--non-goals", "--non-goal", action="append", default=[])
    init.add_argument("--scope", action="append", default=[])
    init.add_argument("--stakeholders", action="append", default=[])

    show = sub.add_parser("requirements-show", aliases=["requirements_show", "show-requirements"], help="show the canonical project brief")
    show.set_defaults()

    answer = sub.add_parser("requirements-answer", aliases=["requirements_answer", "answer-requirements"], help="answer the one current core question")
    answer.add_argument("answer", nargs="?")
    answer.add_argument("--answer", dest="answer_option")
    answer.add_argument("--question-id", "--question")

    update = sub.add_parser("requirements-update", aliases=["requirements_update", "update-requirements"], help="apply an explicit brief revision")
    update.add_argument("updates", nargs="?")
    update.add_argument("--json", "--update", dest="json_update")
    update.add_argument("--brief", "--brief-update", dest="brief_update")
    update.add_argument("--field", action="append", default=[], help="field=value (repeatable)")
    update.add_argument("--rough-requirement", "--requirement")

    approve = sub.add_parser("requirements-approve", aliases=["requirements_approve", "approve-requirements"], help="approve a ready project brief")
    approve.add_argument("--actor", default="local-human")
    approve.add_argument("--rationale", default="")

    cancel = sub.add_parser("requirements-cancel", aliases=["requirements_cancel", "cancel-requirements"], help="cancel intake fail-closed")
    cancel.add_argument("--actor", default="local-human")
    cancel.add_argument("reason_arg", nargs="?")
    cancel.add_argument("--reason", default=None)

    intake = sub.add_parser("intake", aliases=["natural-language", "requirements"], help="recognize a natural-language intake request")
    intake.add_argument("text", nargs="?")
    intake.add_argument("--text", dest="text_option")
    intake.add_argument("--mode", default=IntakeMode.CODEX_REQUIREMENTS_INTERVIEW.value)
    return parser


def _cli_update_values(args: argparse.Namespace) -> Mapping[str, Any]:
    raw = getattr(args, "json_update", None) or getattr(args, "brief_update", None) or getattr(args, "updates", None)
    if raw:
        return _cli_json(str(raw), Path(getattr(args, "repo", ".")), "update")
    values: dict[str, Any] = {}
    for item in getattr(args, "field", []) or []:
        if "=" not in item:
            raise ProjectIntakeError("UPDATE_INVALID", "--field values must use name=value")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ProjectIntakeError("UPDATE_INVALID", "--field name must be non-empty")
        values[key] = value.strip()
    if not values:
        raise ProjectIntakeError("UPDATE_INVALID", "an update object or --field is required")
    return values


def requirements_cli_main(argv: Sequence[str] | None = None) -> int:
    parser = build_requirements_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        root = resolve_project_root(args.repo)
        command = args.command
        service = ProjectRequirementsIntake(root)
        if command in {"requirements-init", "requirements_init", "init-requirements"}:
            result = service.initialize(
                mode=args.mode,
                rough_requirement=args.rough_requirement if args.rough_requirement is not None else args.rough_requirement_arg,
                brief=_cli_brief_args(args),
            )
        elif command in {"requirements-show", "requirements_show", "show-requirements"}:
            result = service.show()
        elif command in {"requirements-answer", "requirements_answer", "answer-requirements"}:
            answer = args.answer_option if args.answer_option is not None else args.answer
            if answer is None:
                raise ProjectIntakeError("INPUT_INVALID", "an answer is required")
            result = service.answer(answer, question_id=args.question_id)
        elif command in {"requirements-update", "requirements_update", "update-requirements"}:
            result = service.update(_cli_update_values(args), rough_requirement=args.rough_requirement)
        elif command in {"requirements-approve", "requirements_approve", "approve-requirements"}:
            result = service.approve(actor=args.actor, rationale=args.rationale)
        elif command in {"requirements-cancel", "requirements_cancel", "cancel-requirements"}:
            reason = args.reason if args.reason is not None else args.reason_arg
            result = service.cancel(actor=args.actor, reason=reason or "")
        elif command in {"intake", "natural-language", "requirements"}:
            text_value = args.text_option if args.text_option is not None else args.text
            if text_value is None:
                raise ProjectIntakeError("INPUT_INVALID", "natural-language intake text is required")
            result = natural_language_entry(root, text_value, mode=args.mode)
        else:  # pragma: no cover - argparse constrains the branch
            raise ProjectIntakeError("COMMAND_INVALID", "unknown requirements command")
        # ASCII-escaped JSON keeps the thin CLI byte stream deterministic on
        # Windows consoles (which may default to a legacy code page) while
        # preserving the exact Unicode values for JSON consumers.
        print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))
        return 0
    except ProjectIntakeError as exc:
        print(f"ERROR {exc.code}: {exc}", file=sys.stderr)
        return 2
    except (ProjectStateError, OSError, TypeError, ValueError) as exc:
        print(f"ERROR INTAKE_INTERNAL: {type(exc).__name__}", file=sys.stderr)
        return 2


__all__ = [
    "BRIEF_STATES",
    "BriefState",
    "CODEX_REQUIREMENTS_INTERVIEW",
    "GPT_CALLS",
    "INTAKE_MODES",
    "MAX_QUESTION_COUNT",
    "NEEDS_MORE_USER_INPUT",
    "PROJECT_BRIEF_SCHEMA_VERSION",
    "PROJECT_BRIEF_PATH",
    "REQUIREMENT_BASELINE_SCHEMA_VERSION",
    "PROJECT_INTAKE_MARKER",
    "ProjectBriefState",
    "ProjectIntake",
    "ProjectIntakeError",
    "ProjectRequirementsIntake",
    "ProjectRequirementsError",
    "RequirementsIntake",
    "RequirementsIntakeError",
    "RequirementsIntakeMode",
    "USER_CONFIRMED_BRIEF",
    "WAITING_USER_APPROVAL",
    "assert_gpt_calls_zero",
    "check_intake_file_hygiene",
    "gpt_call_count",
    "handle_natural_language",
    "is_requirements_intake_request",
    "natural_language_entry",
    "project_identity",
    "recognize_intake_request",
    "requirements_answer",
    "requirements_approve",
    "requirements_cancel",
    "requirements_init",
    "requirements_show",
    "requirements_update",
    "initialize_requirements",
    "init_requirements",
    "show_project_brief",
    "answer_requirements",
    "update_project_brief",
    "approve_project_brief",
    "cancel_project_brief",
    "validate_project_brief",
]
