"""The durable project execution profile used by the portable launcher.

The profile is deliberately stored in the canonical ``PROJECT_BRIEF.json``.
It is a small policy projection, not a second workflow controller or a
replacement for the V2 journal.  Machine credentials and browser state never
belong in this module.
"""

from __future__ import annotations

import copy
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Mapping

from .project_state import ProjectStateError, load_project_brief, resolve_project_root, save_project_brief


EXECUTION_PROFILE_NAME = "autonomous_research"
EXECUTION_PROFILE_VERSION = "1"
EXECUTION_PROFILE_AUTHORITY = "PROJECT_BRIEF"

# This is the one bounded maintenance budget owned by the current execution
# profile.  It deliberately keeps the historical one-attempt-per-iteration
# boundary while allowing one canonical next iteration and one additional
# total attempt.  The values are policy, not scientific evidence.
AUTONOMOUS_RESEARCH_BUDGET_POLICY = {
    "max_iterations": 2,
    "max_attempts_per_iteration": 1,
    "max_attempts_total": 2,
    "max_revalidation_ops": 2,
    "max_validator_revisions": 2,
    "max_dependency_nodes": 4,
    "max_dependency_depth": 2,
    "max_descendant_attempts": 4,
}

AUTO_POLICY = (
    "ordinary_algorithm_correction",
    "test_failure",
    "threshold_debug",
    "connectivity",
    "boundary_relation_bug",
    "technical_gpt_continue",
    "technical_gpt_replan",
    "bounded_retry",
    "transport_recovery",
    "next_technical_stage",
    "next_benchmark_scene",
    "assessment",
    "iteration_transition",
)

HUMAN_POLICY = (
    "irreducible_scientific_choice",
    "final_goal_change",
    "new_external_data_requiring_user_choice",
    "destructive_or_irreversible_action",
    "explicit_human_visual_or_semantic_review",
    "gpt_human_gate_with_unresolved_choice",
)


class ExecutionProfileError(RuntimeError):
    """A bounded error while reading or persisting the project profile."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        super().__init__(message)


def default_execution_profile() -> dict[str, Any]:
    """Return a detached, JSON-safe default profile."""

    return {
        "name": EXECUTION_PROFILE_NAME,
        "version": EXECUTION_PROFILE_VERSION,
        "authority": EXECUTION_PROFILE_AUTHORITY,
        "auto": list(AUTO_POLICY),
        "human": list(HUMAN_POLICY),
        "budget_policy": copy.deepcopy(AUTONOMOUS_RESEARCH_BUDGET_POLICY),
    }


def _profile_view(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": value.get("name"),
        "version": value.get("version"),
        "authority": value.get("authority"),
        "auto": list(value.get("auto", [])) if isinstance(value.get("auto"), list) else [],
        "human": list(value.get("human", [])) if isinstance(value.get("human"), list) else [],
        "budget_policy": copy.deepcopy(value.get("budget_policy")) if isinstance(value.get("budget_policy"), Mapping) else None,
    }


def validate_execution_profile(value: Any) -> dict[str, Any]:
    """Validate the one supported profile without accepting a second policy."""

    if not isinstance(value, Mapping):
        raise ExecutionProfileError("EXECUTION_PROFILE_INVALID", "execution profile must be an object")
    expected = default_execution_profile()
    checked = _profile_view(value)
    if checked != expected:
        raise ExecutionProfileError(
            "EXECUTION_PROFILE_CONFLICT",
            "project execution profile is not the supported autonomous_research profile",
        )
    return copy.deepcopy(expected)


def _load_valid_brief(root: Path) -> dict[str, Any]:
    try:
        raw = load_project_brief(root)
    except ProjectStateError as exc:
        raise ExecutionProfileError("PROJECT_BRIEF_UNREADABLE", "canonical project brief could not be loaded") from exc
    if raw is None:
        raise ExecutionProfileError("WORKFLOW_NOT_FOUND", "no canonical project brief exists; run workflow init")
    try:
        # Lazy import keeps the profile constants usable by project_intake
        # while validation still reuses the canonical brief contract.
        from .project_intake import validate_project_brief

        return validate_project_brief(raw, root)
    except Exception as exc:  # the intake boundary exposes a stable error code, but keep this seam bounded
        code = getattr(exc, "code", "PROJECT_BRIEF_INVALID")
        raise ExecutionProfileError(str(code), "canonical project brief failed validation") from exc


def ensure_execution_profile(
    project_root: str | Path,
    *,
    create: bool = True,
) -> dict[str, Any]:
    """Read the profile and optionally persist the default in the brief.

    ``create`` only updates the existing canonical brief.  It never creates a
    brief, journal, Stage, consultation, browser profile, or credential.
    """

    root = resolve_project_root(project_root)
    brief = _load_valid_brief(root)
    current = brief.get("execution_profile")
    if current is not None:
        # V2.1.3 profiles predate the explicit budget authority.  Treat that
        # absence as an attributable legacy/default profile and migrate it in
        # the canonical brief, never by editing the journal by hand.
        if isinstance(current, Mapping) and "budget_policy" not in current:
            if not create:
                raise ExecutionProfileError("EXECUTION_PROFILE_LEGACY", "project execution profile has no budget authority")
            updated = copy.deepcopy(brief)
            updated["execution_profile"] = default_execution_profile()
            updated["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            updated["revision"] = int(updated.get("revision", 0)) + 1
            try:
                from .project_intake import validate_project_brief

                checked = validate_project_brief(updated, root)
                save_project_brief(root, checked)
            except ProjectStateError as exc:
                raise ExecutionProfileError("EXECUTION_PROFILE_WRITE_FAILED", "legacy execution profile could not be migrated") from exc
            except Exception as exc:
                code = getattr(exc, "code", "EXECUTION_PROFILE_INVALID")
                raise ExecutionProfileError(str(code), "migrated execution profile failed project brief validation") from exc
            return default_execution_profile()
        return validate_execution_profile(current)
    if not create:
        raise ExecutionProfileError("EXECUTION_PROFILE_MISSING", "project has no execution profile; run workflow init")
    updated = copy.deepcopy(brief)
    updated["execution_profile"] = default_execution_profile()
    try:
        from .project_intake import validate_project_brief

        checked = validate_project_brief(updated, root)
        save_project_brief(root, checked)
    except ProjectStateError as exc:
        raise ExecutionProfileError("EXECUTION_PROFILE_WRITE_FAILED", "execution profile could not be persisted") from exc
    except Exception as exc:
        code = getattr(exc, "code", "EXECUTION_PROFILE_INVALID")
        raise ExecutionProfileError(str(code), "execution profile failed project brief validation") from exc
    return default_execution_profile()


__all__ = [
    "AUTO_POLICY",
    "AUTONOMOUS_RESEARCH_BUDGET_POLICY",
    "EXECUTION_PROFILE_AUTHORITY",
    "EXECUTION_PROFILE_NAME",
    "EXECUTION_PROFILE_VERSION",
    "ExecutionProfileError",
    "HUMAN_POLICY",
    "default_execution_profile",
    "ensure_execution_profile",
    "validate_execution_profile",
]
