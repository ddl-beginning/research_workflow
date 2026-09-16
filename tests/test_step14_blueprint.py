"""Offline Step 14 project-blueprint acceptance tests."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from src.contracts import load_schema, validate_instance
from src.project_blueprint import (
    BLUEPRINT_MANIFEST_RELATIVE_PATH,
    CODEX_FEASIBILITY,
    MAX_BLUEPRINT_BYTES,
    PROJECT_BLUEPRINT_MARKER,
    PROJECT_BLUEPRINT_RELATIVE_PATH,
    ProjectBlueprintError,
    READY_FOR_STAGE_PLANNING,
    build_project_blueprint,
    compact_codex_feasibility,
    load_project_blueprint,
    normalize_blueprint_response,
)
from src.project_context import build_project_context
from src.project_discovery import discover_project
from src.project_intake import ProjectRequirementsIntake


PROJECT_URL = "https://chatgpt.com/g/g-p-example-project/project"


class _FakeConsult:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.response


class _TrueVerifier:
    def verify(self, **_: Any) -> dict[str, Any]:
        return {"verified": True, "checked_read_only": True}


class Step14BlueprintTests(unittest.TestCase):
    def _ready_inputs(self, root: Path) -> None:
        intake = ProjectRequirementsIntake(root)
        intake.initialize(
            mode="USER_CONFIRMED_BRIEF",
            brief={
                "goal": "choose a route for a bounded result",
                "success_criteria": ["route can be checked before Stage planning"],
                "chatgpt_project_binding": {"url": PROJECT_URL},
            },
        )
        intake.approve(rationale="Step 14 test")
        build_project_context(root, target="choose a route for a bounded result", next_action="discover")
        discovery_response = {
            "real_goal": "choose a route for a bounded result",
            "search_summary": "fake discovery input",
            "candidate_repositories": [],
            "primary_recommendation": None,
            "why_primary": "",
            "simpler_alternative": "keep the current representation",
            "relevant_non_repo_methods": ["small experiment"],
            "important_risks": ["fit requires local measurement"],
            "facts_needing_local_verification": [],
            "no_direct_match_found": True,
            "alternatives": [],
        }
        discover_project(root, consultant=_FakeConsult(discovery_response), verifier=_TrueVerifier())

    def _blueprint_response(self) -> dict[str, Any]:
        return {
            "primary_route": {"route_id": "route-primary", "title": "direct measurement", "steps": ["measure"]},
            "alternatives": [
                {"route_id": "route-a", "title": "small prototype"},
                {"route_id": "route-b", "title": "existing code"},
                {"route_id": "route-c", "title": "manual check"},
                {"route_id": "route-d", "title": "short experiment"},
            ],
            "composition_decision": "BUILD_NEW",
            "base_decision": "BUILD_NEW",
            "available_assets_used": ["requirement-project-brief"],
            "why_primary": "it is directly measurable",
            "feasibility_summary": "the route is feasible as a bounded experiment",
            "validation_plan": ["run the local check"],
            "important_risks": ["measurement noise"],
            "open_questions": ["which sample is first"],
        }

    def test_01_preconditions_require_approved_context_and_discovery(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step14-precondition-") as directory:
            root = Path(directory)
            intake = ProjectRequirementsIntake(root)
            intake.initialize(
                mode="USER_CONFIRMED_BRIEF",
                brief={"goal": "target", "success_criteria": ["done"], "chatgpt_project_url": PROJECT_URL},
            )
            with self.assertRaises(ProjectBlueprintError) as raised:
                build_project_blueprint(root, consultant=_FakeConsult(self._blueprint_response()))
            self.assertEqual(raised.exception.code, "BRIEF_NOT_APPROVED")
            intake.approve()
            with self.assertRaises(ProjectBlueprintError) as context_error:
                build_project_blueprint(root, consultant=_FakeConsult(self._blueprint_response()))
            self.assertEqual(context_error.exception.code, "CONTEXT_REQUIRED")
            build_project_context(root, target="target", next_action="discover")
            with self.assertRaises(ProjectBlueprintError) as discovery_error:
                build_project_blueprint(root, consultant=_FakeConsult(self._blueprint_response()))
            self.assertEqual(discovery_error.exception.code, "DISCOVERY_REQUIRED")

    def test_02_compact_feasibility_excludes_discovery_recommendation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step14-feasibility-") as directory:
            root = Path(directory)
            self._ready_inputs(root)
            packet = compact_codex_feasibility(root)
            self.assertEqual(packet["stage"], CODEX_FEASIBILITY)
            self.assertNotIn("primary_recommendation", packet["discovery"])
            self.assertNotIn("alternatives", packet["discovery"])
            self.assertEqual(packet["project_url"], PROJECT_URL)

    def test_02b_compact_feasibility_does_not_expose_local_repository_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step14-path-boundary-") as directory:
            root = Path(directory)
            self._ready_inputs(root)
            packet = compact_codex_feasibility(root)
            encoded = json.dumps(packet, ensure_ascii=False, sort_keys=True)
            self.assertNotIn(str(root), encoded)
            self.assertNotIn(root.as_posix(), encoded)

    def test_03_fresh_consult_forwards_url_and_omits_previous_route_language(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step14-consult-") as directory:
            root = Path(directory)
            self._ready_inputs(root)
            consult = _FakeConsult(self._blueprint_response())
            result = build_project_blueprint(root, consultant=consult)
            self.assertEqual(len(consult.calls), 1)
            self.assertEqual(consult.calls[0]["project_url"], PROJECT_URL)
            self.assertEqual(consult.calls[0]["mode"], "fresh")
            prompt = consult.calls[0]["prompt"].casefold()
            self.assertNotIn("previous recommendation", prompt)
            self.assertNotIn("sunk-cost", prompt)
            self.assertNotIn("primary_recommendation", prompt)
            self.assertNotIn("route-primary", prompt)
            self.assertEqual(result["status"], "PROJECT_BLUEPRINT_READY")
            self.assertEqual(result["next_action"], READY_FOR_STAGE_PLANNING)
            self.assertFalse(result["stage_created"])
            self.assertFalse(result["stage_started"])

    def test_04_exactly_one_primary_and_at_most_four_alternatives(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step14-routes-") as directory:
            root = Path(directory)
            self._ready_inputs(root)
            result = build_project_blueprint(root, consultant=_FakeConsult(self._blueprint_response()))
            self.assertIsInstance(result["primary_route"], dict)
            self.assertEqual(result["primary_route"]["route_id"], "route-primary")
            self.assertEqual(len(result["alternatives"]), 4)
            validate_instance(result, load_schema("project_blueprint"))
            manifest = load_project_blueprint(root)
            validate_instance({key: value for key, value in manifest.items() if key != "blueprint_markdown" and key != "path"}, load_schema("project_blueprint"))

    def test_05_atomic_canonical_outputs_and_idempotent_reuse(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step14-idempotent-") as directory:
            root = Path(directory)
            self._ready_inputs(root)
            consult = _FakeConsult(self._blueprint_response())
            first = build_project_blueprint(root, consultant=consult)
            blueprint_path = root / PROJECT_BLUEPRINT_RELATIVE_PATH
            manifest_path = root / BLUEPRINT_MANIFEST_RELATIVE_PATH
            before = blueprint_path.read_bytes()
            mtime = blueprint_path.stat().st_mtime_ns
            time.sleep(0.01)
            second = build_project_blueprint(root, consultant=consult)
            self.assertEqual(len(consult.calls), 1)
            self.assertEqual(first["feasibility_digest"], second["feasibility_digest"])
            self.assertTrue(second["idempotent_reuse"])
            self.assertEqual(blueprint_path.read_bytes(), before)
            self.assertEqual(blueprint_path.stat().st_mtime_ns, mtime)
            self.assertTrue(manifest_path.is_file())
            self.assertIn(PROJECT_BLUEPRINT_MARKER, blueprint_path.read_text(encoding="utf-8"))
            self.assertLessEqual(blueprint_path.stat().st_size, MAX_BLUEPRINT_BYTES)
            self.assertEqual(sorted(path.name for path in (root / ".research" / "blueprint").iterdir()), ["BLUEPRINT_MANIFEST.json", "PROJECT_BLUEPRINT.md"])

    def test_06_malformed_response_fails_closed_without_stage_creation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step14-malformed-") as directory:
            root = Path(directory)
            self._ready_inputs(root)
            consult = _FakeConsult("not-json")
            with self.assertRaises(ProjectBlueprintError) as raised:
                build_project_blueprint(root, consultant=consult)
            self.assertEqual(raised.exception.code, "BLUEPRINT_RESPONSE_INVALID")
            self.assertEqual(len(consult.calls), 1)
            self.assertFalse((root / PROJECT_BLUEPRINT_RELATIVE_PATH).exists())

    def test_07_bounded_grammar_accepts_safe_suffix_controls_and_known_wrapper(self) -> None:
        payload = self._blueprint_response()
        payload["primary_route"]["title"] = "direct\nmeasurement"
        encoded = json.dumps({"blueprint": payload}, ensure_ascii=False, separators=(",", ":"))
        # Simulate the known renderer defect: an LF is literal inside a JSON
        # string, while the object is followed by a short safe explanation.
        encoded = encoded.replace("\\n", "\n", 1) + "\nCompleted in one bounded pass."
        normalized = normalize_blueprint_response(encoded)
        self.assertEqual(normalized["primary_route"]["title"], "direct\nmeasurement")
        self.assertEqual(normalized["composition_decision"], "BUILD_NEW")

    def test_08_wrapper_selection_is_explicit_unique_and_non_recursive(self) -> None:
        payload = self._blueprint_response()
        cases = (
            {"unknown_wrapper": payload},
            {"blueprint": payload, "result": payload},
            {
                "primary_route": payload["primary_route"],
                "alternatives": [],
                "composition_decision": "BUILD_NEW",
                "base_decision": "BUILD_NEW",
                "blueprint": payload,
            },
            {"blueprint": {"nested": payload}},
        )
        for value in cases:
            with self.subTest(value=list(value)):
                with self.assertRaises(ProjectBlueprintError):
                    normalize_blueprint_response(value)

    def test_09_response_semantics_fail_closed_and_do_not_truncate(self) -> None:
        payload = self._blueprint_response()
        invalid = []
        missing_primary = dict(payload)
        missing_primary.pop("primary_route")
        invalid.append(missing_primary)
        wrong_alternatives = dict(payload)
        wrong_alternatives["alternatives"] = "not-an-array"
        invalid.append(wrong_alternatives)
        overflow = dict(payload)
        overflow["alternatives"] = [
            {"route_id": f"route-{index}", "title": "bounded"}
            for index in range(5)
        ]
        invalid.append(overflow)
        duplicate_decision = dict(payload)
        duplicate_decision["base_decision"] = "BUILD_NEW"
        duplicate_decision["composition_decision"] = "KEEP_EXISTING"
        invalid.append(duplicate_decision)
        wrong_list = dict(payload)
        wrong_list["modules_to_keep"] = "one string is not an array"
        invalid.append(wrong_list)
        overlong_list = dict(payload)
        overlong_list["modules_to_keep"] = ["x"] * 17
        invalid.append(overlong_list)
        for value in invalid:
            with self.subTest(value=list(value)):
                with self.assertRaises(ProjectBlueprintError):
                    normalize_blueprint_response(value)

    def test_10_duplicate_path_and_secret_text_fail_closed(self) -> None:
        payload = self._blueprint_response()
        duplicate = json.dumps(payload, ensure_ascii=False).replace(
            '"base_decision": "BUILD_NEW"',
            '"base_decision": "BUILD_NEW", "base_decision": "BUILD_NEW"',
        )
        with self.assertRaises(ProjectBlueprintError):
            normalize_blueprint_response(duplicate)
        path_payload = dict(payload)
        path_payload["why_primary"] = r"C:\Users\Alice\private.txt"
        with self.assertRaises(ProjectBlueprintError):
            normalize_blueprint_response(path_payload)
        secret_payload = dict(payload)
        secret_payload["why_primary"] = "Bearer " + ("a" * 24)
        with self.assertRaises(ProjectBlueprintError):
            normalize_blueprint_response(secret_payload)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
