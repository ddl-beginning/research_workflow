"""Strict, bounded Design Package contract for Research-to-Design Closure V1."""
from __future__ import annotations
from typing import Any, Mapping

REQUIRED_FIELDS = ("recommended_solution", "recommendation_reason", "alternatives", "evidence", "risks", "unknowns", "validation_method", "stage_blueprint", "success_criteria", "non_goals")

class DesignPackageError(ValueError):
    pass

def validate_design_package(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DesignPackageError("design package must be an object")
    out = dict(value)
    missing = [k for k in REQUIRED_FIELDS if k not in out]
    if missing:
        raise DesignPackageError("missing required fields: " + ", ".join(missing))
    for key in REQUIRED_FIELDS:
        if not isinstance(out[key], (str, list, dict)):
            raise DesignPackageError(f"{key} has invalid type")
    if not str(out["recommended_solution"]).strip():
        raise DesignPackageError("recommended_solution must be non-empty")
    return out
