from __future__ import annotations

from pathlib import Path

from scripts.workflow import build_parser, resolve_default_command, run_command
from src.product_metadata import PRODUCT_VERSION, checkout_provenance
from src.project_plan_ingestion import detect_plan_sources


def test_two_file_plan_is_the_no_argument_startup_route(tmp_path: Path) -> None:
    plan = tmp_path / "plan"
    plan.mkdir()
    (plan / "REQUIREMENTS.md").write_text("# Goal\nA bounded project\n", encoding="utf-8")
    (plan / "STAGE_PLAN.md").write_text("# Stage S1\nGoal: verify the result\n", encoding="utf-8")

    assert resolve_default_command(tmp_path) == "resume"


def test_requirement_typo_is_actionable_and_not_renamed(tmp_path: Path) -> None:
    plan = tmp_path / "plan"
    plan.mkdir()
    typo = plan / "REQUIREMENT.md"
    typo.write_text("# Goal\nA bounded project\n", encoding="utf-8")
    (plan / "STAGE_PLAN.md").write_text("# Stage S1\nGoal: verify the result\n", encoding="utf-8")

    result = detect_plan_sources(tmp_path)

    assert result["status"] == "INCOMPLETE"
    assert result["error_code"] == "MISSING_CANONICAL_PLAN_FILE"
    assert result["diagnostic"]["expected"] == "plan/REQUIREMENTS.md"
    assert result["diagnostic"]["candidates"] == ["plan/REQUIREMENT.md"]
    assert typo.is_file()


def test_numbered_requirements_are_ambiguous(tmp_path: Path) -> None:
    plan = tmp_path / "plan"
    plan.mkdir()
    (plan / "REQUIREMENTS (1).md").write_text("# Goal\nA\n", encoding="utf-8")
    (plan / "REQUIREMENTS (2).md").write_text("# Goal\nB\n", encoding="utf-8")
    (plan / "STAGE_PLAN.md").write_text("# Stage S1\nGoal: verify the result\n", encoding="utf-8")

    result = detect_plan_sources(tmp_path)

    assert result["status"] == "AMBIGUOUS"
    assert result["error_code"] == "AMBIGUOUS_PLAN_SOURCE"
    assert result["diagnostic"]["candidates"] == [
        "plan/REQUIREMENTS (1).md",
        "plan/REQUIREMENTS (2).md",
    ]


def test_engine_checkout_cannot_be_used_as_business_project() -> None:
    engine_root = Path(__file__).resolve().parents[1]
    args = build_parser().parse_args(["resume", "--project", str(engine_root), "--json"])

    code, result = run_command(args)

    assert code == 1
    assert result["code"] == "THIS_IS_WORKFLOW_ENGINE_REPOSITORY"
    assert result["writes_performed"] is False


def test_checkout_provenance_uses_single_product_version() -> None:
    checkout = checkout_provenance(Path(__file__).resolve().parents[1])

    assert checkout is not None
    assert checkout["version"] == PRODUCT_VERSION
