"""Offline acceptance tests for the canonical Step 13/14 asset layer."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from src.asset_layer import (
    ASSET_LAYER_MARKER,
    AssetLayerError,
    CANDIDATE_USER_PROJECT_ASSET_ID,
    LOCAL_PROJECT_CANDIDATE_MARKER,
    LOCAL_PROJECT_PROFILE_RELATIVE_PATH,
    build_asset_pack,
    build_local_project_profile,
    collect_available_assets,
    load_available_assets,
    record_verified_candidates,
    validate_asset_pack,
)
from src.contracts import load_schema, validate_instance
from src.project_blueprint import build_project_blueprint
from src.project_context import build_project_context
from src.project_discovery import discover_project
from src.project_intake import ProjectRequirementsIntake


PROJECT_URL = "https://chatgpt.com/g/g-p-6a9a2d82aec881918b65e066b18b95d8-ce-shi/project"


class _DiscoveryConsult:
    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.response = response or {
            "real_goal": "choose a bounded route",
            "search_summary": "offline asset test",
            "candidate_repositories": [],
            "primary_recommendation": None,
            "why_primary": "",
            "simpler_alternative": "keep the shortest route",
            "relevant_non_repo_methods": [],
            "important_risks": [],
            "facts_needing_local_verification": [],
            "no_direct_match_found": True,
            "alternatives": [],
        }

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.response


class _BlueprintConsult:
    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "primary_route": {"route_id": "borrow", "title": "keep the base and borrow one module"},
            "alternatives": [],
            "composition_decision": "KEEP_AND_BORROW",
            "base_decision": "KEEP_AND_BORROW",
            "existing_user_project_role": "retain the existing entry point",
            "external_repository_role": "borrow one verified algorithm module",
            "available_assets_used": ["requirement-project-brief"],
            "modules_to_keep": ["existing entry point"],
            "modules_to_borrow": ["verified algorithm module"],
            "modules_to_replace": [],
            "modules_to_delete_retire": [],
            "new_components_required": ["small adapter"],
            "why_composition_preferred": "it preserves the measured base while limiting borrowed scope",
            "baseline_must_not_regress": ["existing output"],
            "anti_tunnel_architecture_risks": ["do not copy the external repository"],
            "architecture_reset_triggers": ["adapter cannot meet the baseline"],
            "why_primary": "bounded composition",
            "feasibility_summary": "feasible",
            "validation_plan": ["run the local check"],
            "important_risks": [],
            "open_questions": [],
        }


class AssetLayerTests(unittest.TestCase):
    def _approved(self, root: Path, *, constraints: list[str] | None = None) -> dict[str, Any]:
        intake = ProjectRequirementsIntake(root)
        intake.initialize(
            mode="USER_CONFIRMED_BRIEF",
            brief={
                "goal": "choose a bounded route",
                "success_criteria": ["route is checkable"],
                "constraints": list(constraints or []),
                "chatgpt_project_url": PROJECT_URL,
            },
        )
        approved = intake.approve(rationale="asset test")
        build_project_context(root, target="choose a bounded route", next_action="discover")
        return approved

    def test_no_local_project_still_has_valid_discovery_assets(self) -> None:
        with tempfile.TemporaryDirectory(prefix="asset-no-local-") as directory:
            root = Path(directory)
            approved = self._approved(root)
            report = discover_project(root, brief=approved, consultant=_DiscoveryConsult(), verifier=lambda **_: {"verified": True})
            assets = load_available_assets(root)
            self.assertIsNotNone(assets)
            assert assets is not None
            self.assertFalse(assets["local_project_present"])
            self.assertFalse((root / LOCAL_PROJECT_PROFILE_RELATIVE_PATH).exists())
            self.assertIn("requirement-project-brief", report["available_assets_used"])
            validate_instance(assets, load_schema("available_assets"))

    def test_local_profile_and_candidate_zero_are_read_only_and_not_preferred(self) -> None:
        with tempfile.TemporaryDirectory(prefix="asset-local-") as directory:
            root = Path(directory)
            (root / "README.md").write_text("# Existing local capability\n", encoding="utf-8")
            (root / "main.py").write_text("print('local')\n", encoding="utf-8")
            approved = self._approved(root)
            profile = build_local_project_profile(root, brief=approved)
            self.assertIsNotNone(profile)
            assert profile is not None
            self.assertIn(LOCAL_PROJECT_CANDIDATE_MARKER, profile["markdown"])
            assets = collect_available_assets(root, brief=approved)
            candidate = next(item for item in assets["assets"] if item["asset_id"] == CANDIDATE_USER_PROJECT_ASSET_ID)
            self.assertTrue(candidate["verified"])
            self.assertEqual(candidate["candidate_status"], "candidate_only")
            self.assertEqual(candidate["upload_policy"], "never")
            self.assertIn("no preference", candidate["summary"])
            self.assertNotIn("main.py", build_asset_pack(root, brief=approved)["canonical_files"])

    def test_canonical_update_is_idempotent_and_pack_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory(prefix="asset-idempotent-") as directory:
            root = Path(directory)
            approved = self._approved(root)
            first = collect_available_assets(root, brief=approved)
            path = root / ".research" / "AVAILABLE_ASSETS.json"
            before = path.read_bytes()
            second = collect_available_assets(root, brief=approved)
            self.assertTrue(second["idempotent_reuse"])
            self.assertEqual(first["assets_digest"], second["assets_digest"])
            self.assertEqual(path.read_bytes(), before)
            pack = build_asset_pack(root, brief=approved)
            self.assertFalse(pack["whole_repo_attached"])
            self.assertTrue(pack["bounded"])
            self.assertIn("requirement-project-brief", pack["asset_ids"])
            validate_asset_pack(pack)
            self.assertEqual(pack["schema_version"], "asset_pack.v1")

    def test_unverified_external_claims_are_not_established_facts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="asset-claims-") as directory:
            root = Path(directory)
            approved = self._approved(root)
            payload = record_verified_candidates(
                root,
                [
                    {"repo_url": "https://github.com/example/unverified", "likely_gap": "unverified claim"},
                    {
                        "repo_url": "https://github.com/example/verified",
                        "likely_gap": "also only a claim",
                        "local_verification": {
                            "verified_locally": True,
                            "claims_source": "LOCAL_VERIFIER",
                            "default_head": "a" * 40,
                        },
                    },
                ],
                brief=approved,
            )
            external = [item for item in payload["assets"] if item["kind"] == "external_repository"]
            self.assertEqual(len(external), 1)
            verification = external[0]["verification"]
            self.assertNotIn("also only a claim", verification["established_facts"])
            self.assertIn("also only a claim", external[0]["unverified_claims"])

    def test_keep_and_borrow_composition_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory(prefix="asset-composition-") as directory:
            root = Path(directory)
            approved = self._approved(root)
            discover_project(root, brief=approved, consultant=_DiscoveryConsult(), verifier=lambda **_: {"verified": True})
            blueprint = build_project_blueprint(root, brief=approved, consultant=_BlueprintConsult())
            self.assertEqual(blueprint["base_decision"], "KEEP_AND_BORROW")
            self.assertEqual(blueprint["modules_to_borrow"], ["verified algorithm module"])
            self.assertEqual(blueprint["asset_layer_marker"], ASSET_LAYER_MARKER)
            self.assertFalse(blueprint["stage_created"])
            self.assertFalse(blueprint["stage_started"])
            stored = json.loads((root / ".research" / "blueprint" / "BLUEPRINT_MANIFEST.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["base_decision"], "KEEP_AND_BORROW")

    def test_brief_constraints_use_one_sixteen_item_bound_in_profile_and_asset(self) -> None:
        with tempfile.TemporaryDirectory(prefix="asset-constraints-9-") as directory:
            root = Path(directory)
            (root / "README.md").write_text("# bounded local project\n", encoding="utf-8")
            constraints = [f"constraint-{index}" for index in range(9)]
            approved = self._approved(root, constraints=constraints)

            profile = build_local_project_profile(root, brief=approved)
            self.assertIsNotNone(profile)
            assert profile is not None
            self.assertEqual(profile["protected_frozen_boundaries"][-len(constraints) :], constraints)

            inventory = collect_available_assets(root, brief=approved)
            requirement = next(item for item in inventory["assets"] if item["asset_id"] == "requirement-project-brief")
            self.assertEqual(requirement["important_constraints"], constraints)

    def test_brief_constraints_seventeen_still_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="asset-constraints-17-") as directory:
            root = Path(directory)
            (root / "README.md").write_text("# bounded local project\n", encoding="utf-8")
            approved = self._approved(root, constraints=[f"constraint-{index}" for index in range(17)])

            with self.assertRaises(AssetLayerError) as profile_error:
                build_local_project_profile(root, brief=approved)
            self.assertEqual(profile_error.exception.code, "ASSET_INPUT_TOO_LARGE")
            self.assertEqual(profile_error.exception.failure_field, "brief.constraints")

            with self.assertRaises(AssetLayerError) as inventory_error:
                collect_available_assets(root, brief=approved)
            self.assertEqual(inventory_error.exception.code, "ASSET_INPUT_TOO_LARGE")
            self.assertEqual(inventory_error.exception.failure_field, "brief.constraints")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
