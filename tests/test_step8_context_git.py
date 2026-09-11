from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.artifacts import (  # noqa: E402
    ARTIFACT_ROLES,
    ArtifactRegistry,
    ArtifactRegistryError,
    required_review_artifact_refs,
    validate_artifact_manifest,
)
from src.contracts import ContractValidationError, load_schema  # noqa: E402
from src.stage_controller import validate_stage_contract_v1  # noqa: E402
from src.git_baseline import (  # noqa: E402
    GitBaselineError,
    capture_git_baseline,
    freeze_git_baseline,
    persist_git_baseline,
    verify_git_baseline,
)
from src.stage_context import (  # noqa: E402
    StageContextError,
    build_stage_context,
    load_stage_context,
    render_stage_context_markdown,
    select_evidence,
    verify_stage_context,
    write_stage_context,
)


def run_git(root: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, check=check, capture_output=True, text=True
    )
    return result.stdout.strip()


def make_git_repo() -> Path:
    root = Path(tempfile.mkdtemp(prefix="step8-git-"))
    run_git(root, "init", "-q")
    (root / "README.md").write_text("disposable\n", encoding="utf-8")
    run_git(root, "add", "README.md")
    run_git(root, "-c", "user.name=Step8 Test", "-c", "user.email=step8@example.test", "commit", "-qm", "initial")
    return root


def context(**overrides):
    values = {
        "project_goal": "make the disposable output reviewable",
        "stage": {"stage_id": "stage-8", "stage_name": "artifact layer", "status": "ACTIVE"},
        "user_visible_goal": "show a stable, measurable result",
        "established_facts": ["fixture is deterministic"],
        "current_method": "bounded local experiment",
        "baseline_summary": {"metrics": {"score": 1.0}},
        "latest_result": {"metrics": {"score": 0.7}},
        "current_blocker": "none",
        "rejected_directions": ["unbounded loop"],
        "protected_constraints": {"protected_paths": [".auth"], "allowed_paths": ["src"]},
        "recent_decisions": ["continue primary"],
        "artifact_refs": ["artifact://stage-8/primary/metrics_summary/a"],
        "git_baseline": {"digest": "a" * 64, "dirty": False},
    }
    values.update(overrides)
    return values


class GitBaselineTests(unittest.TestCase):
    def test_clean_and_dirty_baseline_are_read_only_and_content_addressed(self):
        root = make_git_repo()
        head_before = run_git(root, "rev-parse", "HEAD")
        clean = capture_git_baseline(
            root,
            stage_id="stage-8",
            metrics_refs=["metrics/summary.json"],
            review_refs=["review/report.md"],
            config_refs=["config/stage.json"],
        )
        self.assertFalse(clean["dirty"])
        self.assertEqual(clean["commit_sha"], head_before)
        self.assertEqual(clean["repository_path"], clean["repository_root"])
        self.assertEqual(clean["baseline_metrics_refs"], ["metrics/summary.json"])
        (root / "README.md").write_text("user work\n", encoding="utf-8")
        dirty = capture_git_baseline(root, stage_id="stage-8")
        self.assertTrue(dirty["dirty"])
        self.assertNotEqual(clean["digest"], dirty["digest"])
        self.assertEqual(run_git(root, "rev-parse", "HEAD"), head_before)
        self.assertEqual(run_git(root, "rev-list", "--count", "HEAD"), "1")

    def test_freeze_reuses_existing_baseline_and_does_not_overwrite_iteration(self):
        root = make_git_repo()
        destination = root / ".research" / "baseline.json"
        first = freeze_git_baseline(root, destination, stage_id="stage-8")
        (root / "README.md").write_text("later iteration\n", encoding="utf-8")
        second = freeze_git_baseline(root, destination, stage_id="stage-8")
        self.assertEqual(first, second)
        self.assertEqual(json.loads(destination.read_text(encoding="utf-8")), first)
        with self.assertRaises(GitBaselineError):
            persist_git_baseline(capture_git_baseline(root), destination)

    def test_baseline_hash_tamper_fails_closed(self):
        root = make_git_repo()
        baseline = capture_git_baseline(root)
        tampered = copy.deepcopy(baseline)
        tampered["dirty"] = True
        with self.assertRaises(GitBaselineError):
            verify_git_baseline(tampered)

    def test_baseline_sensitive_refs_fail_closed(self):
        root = make_git_repo()
        with self.assertRaises(GitBaselineError):
            capture_git_baseline(root, config_refs=[".env"])

    def test_stage_contract_protected_and_allowed_paths_reject_traversal(self):
        contract = {
            "schema_version": "stage_contract.v1",
            "project_id": "p",
            "repository_root": "D:/disposable",
            "stage_id": "s",
            "stage_name": "stage",
            "project_goal": "goal",
            "stage_goal": "stage goal",
            "user_visible_goal": "visible goal",
            "inputs": [],
            "protected_paths": [".git"],
            "allowed_paths": ["src"],
            "acceptance_description": "checks pass",
            "required_checks": ["unit"],
            "review_artifact_requirements": ["metrics_summary"],
            "baseline": {},
            "status": "PLANNED",
        }
        validate_stage_contract_v1(contract)
        for field in ("protected_paths", "allowed_paths"):
            bad = copy.deepcopy(contract)
            bad[field] = ["../outside"]
            with self.subTest(field=field):
                with self.assertRaises(ContractValidationError):
                    validate_stage_contract_v1(bad)


class ContextLayerTests(unittest.TestCase):
    def test_context_contains_required_fields_is_bounded_and_renders_markdown(self):
        built = build_stage_context(**context(), plan_id="plan-8", stage_id="stage-8")
        self.assertEqual(built["resource_count"], 9)
        for field in (
            "project_goal", "stage", "user_visible_goal", "established_facts", "current_method",
            "baseline_summary", "latest_result", "current_blocker", "rejected_directions",
            "protected_constraints", "recent_decisions", "artifact_refs", "git_baseline",
        ):
            self.assertIn(field, built)
        markdown = render_stage_context_markdown(built)
        self.assertIn("# STAGE_CONTEXT", markdown)
        self.assertIn("## Git baseline", markdown)
        self.assertLess(len(markdown), 120000)
        self.assertEqual(verify_stage_context(built)["digest"], built["digest"])

    def test_overflow_is_explicitly_compressed_or_rejected(self):
        resources = {f"class_{index}": {"value": index} for index in range(10)}
        selected = select_evidence(resources, overflow="compress")
        self.assertLessEqual(len(selected["resources"]), 9)
        self.assertEqual(len(selected["selection"]["compressed_classes"]), 2)
        self.assertIn("compressed_evidence", selected["resources"])
        self.assertEqual(
            set(selected["resources"]["compressed_evidence"]["source_classes"]),
            set(selected["selection"]["compressed_classes"]),
        )
        with self.assertRaises(StageContextError) as error:
            select_evidence(resources, overflow="reject")
        self.assertIn("class_", str(error.exception))

    def test_normal_delta_reports_reused_files_and_changed_classes(self):
        first = build_stage_context(
            **context(),
            plan_id="plan-8",
            stage_id="stage-8",
            resources={
                "stage_context": {"files": [{"path": "STAGE_CONTEXT.md", "digest": "a" * 64}]},
                "latest_result": {"files": [{"path": "result.json", "digest": "b" * 64}]},
            },
        )
        second = build_stage_context(
            **context(latest_result={"metrics": {"score": 0.5}}),
            plan_id="plan-8",
            stage_id="stage-8",
            resources={
                "stage_context": {"files": [{"path": "STAGE_CONTEXT.md", "digest": "a" * 64}]},
                "latest_result": {"files": [{"path": "result.json", "digest": "c" * 64}]},
            },
            previous_context=first,
            mode="NORMAL",
        )
        self.assertEqual(second["delta"]["reused_files"], ["STAGE_CONTEXT.md"])
        self.assertIn("latest_result", second["delta"]["changed_resources"])

    def test_fresh_context_excludes_previous_recommendation(self):
        built = build_stage_context(
            **context(),
            plan_id="plan-8",
            stage_id="stage-8",
            mode="FRESH",
            resources={
                "stage_context": {"facts": ["fresh facts"]},
                "latest_result": {"status": "SUCCEEDED"},
                "previous_recommendation": "do not copy this",
                "sunk_cost_narrative": "do not copy this either",
            },
        )
        self.assertTrue(built["fresh_isolation_verified"])
        self.assertNotIn("previous_recommendation", built["resources"])
        self.assertNotIn("sunk_cost_narrative", built["resources"])
        self.assertEqual(
            set(built["selection"]["fresh_excluded_classes"]),
            {"previous_recommendation", "sunk_cost_narrative"},
        )

    def test_fresh_nested_previous_recommendation_is_not_copied(self):
        values = context()
        values["latest_result"] = {"facts": ["new"], "previous_recommendation": "must not appear"}
        built = build_stage_context(
            **values,
            plan_id="plan-8",
            stage_id="stage-8",
            mode="FRESH",
            resources={"latest_result": {"facts": ["new"], "previous_recommendation": "must not appear"}},
        )
        serialized = json.dumps(built, ensure_ascii=False)
        self.assertNotIn("must not appear", serialized)
        self.assertTrue(built["fresh_isolation_verified"])

    def test_context_disk_manifest_and_markdown_tamper_fail_closed(self):
        built = build_stage_context(**context(), plan_id="plan-8", stage_id="stage-8")
        directory = Path(tempfile.mkdtemp(prefix="step8-context-"))
        write_stage_context(built, directory)
        self.assertEqual(load_stage_context(directory)["digest"], built["digest"])
        json_path = directory / "stage_context.json"
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        payload["current_blocker"] = "tampered"
        json_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(StageContextError):
            load_stage_context(directory)

    def test_context_rejects_sensitive_keys_instead_of_dropping_them(self):
        with self.assertRaises(StageContextError):
            build_stage_context(**context(established_facts={"token": "secret"}), plan_id="p", stage_id="s")


class ArtifactLayerTests(unittest.TestCase):
    def test_all_review_roles_support_non_png_and_are_referenceable(self):
        root = Path(tempfile.mkdtemp(prefix="step8-artifacts-"))
        registry = ArtifactRegistry("stage-8", root=root, required_roles=ARTIFACT_ROLES)
        extensions = {"representative_good": ".txt", "representative_hard": ".cad", "worst_case": ".html", "before_after": ".pdf", "metrics_summary": ".json"}
        for index, role in enumerate(ARTIFACT_ROLES):
            path = root / f"evidence-{index}{extensions[role]}"
            path.write_text(f"evidence {role}\n", encoding="utf-8")
            registry.register(path, role, media_type="application/octet-stream")
        manifest = registry.save(root / "artifacts.manifest.json")
        validate_artifact_manifest(manifest, check_files=True)
        refs = required_review_artifact_refs(manifest)
        self.assertEqual(len(refs), len(ARTIFACT_ROLES))
        self.assertEqual(len(set(refs)), len(refs))

    def test_primary_and_challenger_do_not_overwrite_each_other(self):
        root = Path(tempfile.mkdtemp(prefix="step8-lanes-"))
        primary = root / "primary.txt"
        challenger = root / "challenger.txt"
        primary.write_text("primary\n", encoding="utf-8")
        challenger.write_text("challenger\n", encoding="utf-8")
        registry = ArtifactRegistry("stage-8", root=root)
        one = registry.register(primary, "before_after", lane="primary")
        two = registry.register(challenger, "before_after", lane="challenger")
        self.assertNotEqual(one["artifact_id"], two["artifact_id"])
        self.assertEqual({item["lane"] for item in registry.artifacts}, {"primary", "challenger"})
        self.assertEqual(len(registry.validate()["artifacts"]), 2)

    def test_artifact_duplicate_role_and_content_tamper_fail_closed(self):
        root = Path(tempfile.mkdtemp(prefix="step8-tamper-"))
        path = root / "metrics.txt"
        path.write_text("one\n", encoding="utf-8")
        registry = ArtifactRegistry("stage-8", root=root)
        registry.register(path, "metrics_summary")
        with self.assertRaises(ArtifactRegistryError):
            registry.register(path, "metrics_summary")
        manifest = registry.save(root / "manifest.json")
        path.write_text("tampered\n", encoding="utf-8")
        with self.assertRaises(ArtifactRegistryError):
            validate_artifact_manifest(manifest, check_files=True)

    def test_artifact_manifest_tamper_and_sensitive_metadata_fail_closed(self):
        root = Path(tempfile.mkdtemp(prefix="step8-manifest-"))
        path = root / "metrics.json"
        path.write_text("{}\n", encoding="utf-8")
        registry = ArtifactRegistry("stage-8", root=root)
        with self.assertRaises(ArtifactRegistryError):
            registry.register(path, "metrics_summary", metadata={"token": "secret"})
        registry.register(path, "metrics_summary")
        manifest = registry.manifest()
        tampered = copy.deepcopy(manifest)
        tampered["artifacts"][0]["role"] = "worst_case"
        with self.assertRaises(ArtifactRegistryError):
            validate_artifact_manifest(tampered)

    def test_persisted_manifest_tamper_fails_when_reloaded(self):
        root = Path(tempfile.mkdtemp(prefix="step8-manifest-load-"))
        path = root / "metrics.txt"
        path.write_text("stable\n", encoding="utf-8")
        manifest_path = root / "manifest.json"
        registry = ArtifactRegistry("stage-8", root=root, manifest_path=manifest_path)
        registry.register(path, "metrics_summary")
        registry.save()
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["artifacts"][0]["basename"] = "other.txt"
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ArtifactRegistryError):
            ArtifactRegistry("stage-8", root=root, manifest_path=manifest_path)


class SchemaChecks(unittest.TestCase):
    def test_step8_schemas_are_loadable(self):
        for name in ("git_baseline.schema.json", "stage_context.schema.json", "artifact_manifest.schema.json"):
            with self.subTest(name=name):
                self.assertEqual(load_schema(name)["type"], "object")


class Step8AcceptanceIntegration(unittest.TestCase):
    def test_baseline_context_and_artifact_layers_form_one_local_acceptance(self):
        root = make_git_repo()
        baseline_path = root / ".research" / "baseline.json"
        baseline = freeze_git_baseline(
            root,
            baseline_path,
            stage_id="stage-8",
            metrics_refs=["metrics/summary.json"],
            review_refs=["review/closeout.md"],
            config_refs=["config/stage.json"],
        )
        artifacts_root = root / ".research" / "artifacts"
        artifacts_root.mkdir(parents=True)
        good = artifacts_root / "good.txt"
        metrics = artifacts_root / "metrics.json"
        good.write_text("representative good\n", encoding="utf-8")
        metrics.write_text('{"score": 0.7}\n', encoding="utf-8")
        registry = ArtifactRegistry("stage-8", root=artifacts_root, required_roles=["representative_good", "metrics_summary"])
        good_record = registry.register(good, "representative_good")
        metrics_record = registry.register(metrics, "metrics_summary")
        manifest = registry.save(artifacts_root / "manifest.json")
        pack = build_stage_context(
            plan_id="plan-8",
            stage_id="stage-8",
            project_goal="make the disposable output reviewable",
            stage={"stage_id": "stage-8", "stage_name": "artifact layer", "status": "ACTIVE"},
            user_visible_goal="show a stable, measurable result",
            established_facts=["baseline is frozen", "artifacts are hashed"],
            current_method="bounded local experiment",
            baseline_summary=baseline,
            latest_result={"metrics": {"score": 0.7}},
            current_blocker="none",
            rejected_directions=["unbounded loop"],
            protected_constraints={"protected_paths": [".git"], "allowed_paths": [".research"]},
            recent_decisions=["continue primary"],
            artifact_refs=[good_record["uri"], metrics_record["uri"]],
            git_baseline=baseline,
        )
        context_dir = root / ".research" / "context"
        write_stage_context(pack, context_dir)
        restored = load_stage_context(context_dir)
        self.assertEqual(restored["git_baseline"]["digest"], baseline["digest"])
        self.assertEqual(required_review_artifact_refs(manifest), [good_record["uri"], metrics_record["uri"]])
        self.assertEqual(registry.validate(require_roles=True)["manifest_digest"], manifest["manifest_digest"])


if __name__ == "__main__":
    unittest.main()
