"""Fresh-context comprehension E2E for the bounded project-plan contract.

The disposable project is created with only the two user planning sources.
The fresh Codex process is instructed to read only the generated
``WORKFLOW_PLAN.md`` and one canonical current Stage ID.  No prior chat,
source requirements, or execution history is placed in its prompt.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

PRODUCT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PRODUCT_ROOT))

from src.project_plan_ingestion import sync_project_plan  # noqa: E402
from src.workflow_v2_controller import StageController  # noqa: E402


def _sources(root: Path) -> None:
    plan = root / "plan"
    plan.mkdir(parents=True, exist_ok=True)
    (plan / "REQUIREMENTS.md").write_text(
        """# Final Project Goal
Prove a bounded geometry method across a controlled-to-real data maturity ladder.

## Scope
- four independently accepted validation milestones

## Non-goals
- no production deployment

## Final Output
- reproducible evidence for each maturity boundary

## Quality / Acceptance
- every Stage has a machine-verifiable gate
""",
        encoding="utf-8",
    )
    (plan / "STAGE_PLAN.md").write_text(
        """# Stage Plan

## S1 - Synthetic analytical ground truth
- Goal: establish a controlled reference result
- Why This Stage Exists: freeze the analytical baseline before real sampling
- Entry Conditions: project brief is approved
- Dataset: synthetic_facade_s1
- Path: D:/workflow-e2e-data/synthetic_facade_s1
- Data Maturity: Synthetic + analytical GT
- Role: controlled validation
- GT Availability: analytical
- Reference Type: analytical
- Tasks:
  - run accepted baseline
  - evaluate against analytical GT
- Expected Outputs:
  - prediction artifact
  - machine evaluation report
- Primary:
  - boundary distance and topology checks pass
- Secondary:
  - runtime diagnostics are recorded
- Human-visible Evidence:
  - bounded before-and-after evidence panel
- Pass Gate:
  - primary metrics pass and required artifacts exist
- Replan:
  - representation cannot satisfy the frozen geometry requirement
- Stop:
  - private input or irreversible authorization is unavailable
- Human Gate Required: NO
- Next Stage: S2

## S2 - Real data with frozen GT
- Goal: validate the accepted method on real public data with frozen GT
- Why This Stage Exists: add real sampling and noise without changing the reference
- Entry Conditions: S1 is accepted
- Dataset: real_public_frozen_gt
- Data Maturity: Real data with frozen GT
- Tasks: run the frozen-GT validation
- Expected Outputs: real-data evaluation report
- Primary: frozen-GT metrics pass
- Human Gate Required: NO
- Next Stage: S3

## S3 - Real reference comparison
- Goal: compare the method with competitor or human reference evidence
- Why This Stage Exists: test agreement where analytical GT is unavailable
- Entry Conditions: S2 is accepted
- Dataset: real_reference_set
- Data Maturity: Real data with competitor/human reference
- Tasks: produce the reference comparison
- Expected Outputs: comparison evidence
- Primary: reference comparison gate passes
- Human Gate Required: YES
- Next Stage: S4

## S4 - Company data without GT
- Goal: characterize behavior on company data without GT
- Why This Stage Exists: establish the final no-GT maturity boundary
- Entry Conditions: S3 is accepted and private data is authorized
- Dataset: company_data_without_gt
- Data Maturity: Real company data without GT
- Tasks: run the no-GT characterization
- Expected Outputs: company-data characterization
- Primary: declared no-GT evaluation gate passes
- Human Gate Required: YES
- Next Stage: DONE
""",
        encoding="utf-8",
    )


def _json_from_output(text: str) -> dict[str, Any]:
    candidates = [text.strip()]
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    candidates.extend(fenced)
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("fresh Codex did not return a JSON stage comprehension")


def _matches(expected: dict[str, Any], actual: dict[str, Any]) -> tuple[bool, list[str]]:
    def normalized_list(value: Any) -> list[Any] | None:
        if isinstance(value, list):
            return [str(item).strip().lstrip("- ").strip() if not isinstance(item, dict) else item for item in value]
        if isinstance(value, str):
            return [line.strip().lstrip("- ").strip() for line in value.splitlines() if line.strip()]
        return None

    missing = [key for key in expected if key not in actual]
    if missing:
        return False, ["missing keys: " + ", ".join(missing)]
    errors: list[str] = []
    for key, value in expected.items():
        observed = actual.get(key)
        if key == "human_gate_required":
            observed_bool = observed if isinstance(observed, bool) else str(observed).strip().upper() in {"YES", "TRUE", "REQUIRED"}
            if observed_bool != value:
                errors.append(f"{key}: expected {value!r}, got {observed!r}")
        elif key in {"current_stage_id", "next_stage"}:
            if observed != value:
                errors.append(f"{key}: expected {value!r}, got {observed!r}")
        elif isinstance(value, list):
            expected_list = normalized_list(value)
            observed_list = normalized_list(observed)
            if observed_list != expected_list:
                errors.append(f"{key}: expected exact ordered list")
        elif observed != value:
            errors.append(f"{key}: expected exact value")
    return not errors, errors


def run_e2e(codex: str, output: Path, workspace_root: Path | None = None) -> int:
    temporary_parent = workspace_root or Path.cwd()
    if not temporary_parent.is_dir():
        raise ValueError(f"comprehension workspace root is not a directory: {temporary_parent}")
    with tempfile.TemporaryDirectory(prefix="workflow-plan-comprehension-", dir=str(temporary_parent)) as temporary:
        root = Path(temporary)
        _sources(root)
        # Codex's normal local filesystem tools are most reliable inside a
        # disposable repository.  Git metadata is harness-only and is not a
        # third planning input or part of the prompt.
        subprocess.run(["git", "init", "-q"], cwd=str(root), check=True, capture_output=True)
        controller = StageController(workspace_id="workspace-plan-comprehension-12345678", state_path=root / ".workflow-v2" / "journal.json")
        result = sync_project_plan(root, controller=controller)
        current = result["canonical"]["stage"]
        current_stage_id = str(current["stage_id"])
        workflow_plan_path = root / "plan" / "WORKFLOW_PLAN.md"
        workflow_plan_text = workflow_plan_path.read_text(encoding="utf-8")
        stage = next(item for item in result["stages"] if (item.get("bound_stage_id") or item["stage_id"]) == current_stage_id)
        expected = {
            "current_stage_id": current_stage_id,
            "goal": stage["stage_goal"],
            "why_this_stage_exists": stage["why_this_stage_exists"],
            "entry_conditions": stage["entry_conditions"],
            "data": [
                {
                    "dataset_name": spec["dataset_name"],
                    "path": spec["path"],
                    "maturity": spec["maturity"],
                    "gt_availability": spec["gt_availability"],
                    "reference_type": spec["reference_type"],
                }
                for spec in stage["dataset_specs"]
            ],
            "tasks": [f"T{index:02d} — {task}" for index, task in enumerate(stage["tasks"], start=1)],
            "expected_outputs": stage["expected_outputs"],
            "primary_machine_gate": stage["machine_evaluation_primary"],
            "human_visible_evidence": stage["human_visible_evidence"],
            "human_gate_required": stage["human_gate_required"],
            "replan_conditions": stage["replan_conditions"],
            "next_stage": stage["next_stage"],
        }
        prompt = (
            "This is a fresh comprehension check. The only file provided below is plan/WORKFLOW_PLAN.md. "
            f"The current Stage ID is {current_stage_id}. Do not use prior chat context, read any other file, "
            "or modify the workspace. First locate the Stage Card whose heading is '# Stage "
            f"{current_stage_id} —'. Copy the requested values from that card; do not infer them from memory. "
            "Return only one JSON object with exactly these keys: "
            "current_stage_id, goal, why_this_stage_exists, entry_conditions, data, tasks, expected_outputs, "
            "primary_machine_gate, human_visible_evidence, human_gate_required, replan_conditions, next_stage. "
            "Copy values exactly from the current Stage Card (goal is the value after the 'Stage Goal:' label). "
            "The data value must be a list of objects with keys "
            "dataset_name, path, maturity, gt_availability, reference_type. Never use null for a requested value. Do not include commentary.\n\n"
            "<WORKFLOW_PLAN.md>\n" + workflow_plan_text + "\n</WORKFLOW_PLAN.md>"
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            process = subprocess.run(
                [codex, "exec", "--ephemeral", "--skip-git-repo-check", "-s", "read-only", "-m", "gpt-5.6-luna", "-C", str(root), "-o", str(root / "codex-last-message.json"), "-"],
                cwd=str(root),
                input=prompt,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=240,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            process_error = None
        except subprocess.TimeoutExpired as exc:
            process = subprocess.CompletedProcess(args=[], returncode=124, stdout=exc.stdout or "", stderr=exc.stderr or "")
            process_error = "fresh Codex timed out at the bounded 240 second limit"
        last_message = root / "codex-last-message.json"
        raw = last_message.read_text(encoding="utf-8") if last_message.is_file() else process.stdout
        actual: dict[str, Any] | None = None
        errors: list[str] = []
        try:
            actual = _json_from_output(raw)
            passed, errors = _matches(expected, actual)
        except (OSError, UnicodeError, ValueError) as exc:
            passed = False
            errors = [str(exc)]
        if process_error:
            passed = False
            errors.insert(0, process_error)
        report = {
            "schema_version": "workflow_plan_comprehension_e2e.v1",
            "status": "PASS" if process.returncode == 0 and passed else "FAIL",
            "marker": "CODEX_STAGE_PLAN_COMPREHENSION_E2E: PASS" if process.returncode == 0 and passed else "CODEX_STAGE_PLAN_COMPREHENSION_E2E: FAIL",
            "current_stage_id": current_stage_id,
            "fresh_context": True,
            "prompt_sources": ["plan/WORKFLOW_PLAN.md", current_stage_id],
            "file_content_provided": True,
            "prior_chat_provided": False,
            "source_requirements_provided": False,
            "codex_model": "gpt-5.6-luna",
            "codex_returncode": process.returncode,
            "errors": errors,
            "observed": actual if errors else None,
            "human_intervention_count": 0,
            "temporary_workspace": str(root),
        }
        output.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if report["status"] == "PASS" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fresh Codex project-plan comprehension E2E")
    parser.add_argument("--codex", default=shutil.which("codex") or "codex")
    parser.add_argument("--output", required=True)
    parser.add_argument("--workspace-root", default=None, help="existing local directory for the disposable Codex workspace")
    args = parser.parse_args(argv)
    workspace_root = Path(args.workspace_root).expanduser().resolve() if args.workspace_root else None
    return run_e2e(str(args.codex), Path(args.output).expanduser().resolve(), workspace_root)


if __name__ == "__main__":
    raise SystemExit(main())
