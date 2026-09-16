"""Offline checks for the supervisor-to-bridge project adapter."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
import shutil
from typing import Any

from src.bridge_adapter import (
    BridgeEnvelopeError,
    ProjectScopedBridgeConsultant,
    build_project_context_pack,
    normalize_bridge_envelope,
)
from src.stage_integration import subprocess_bridge_runner


PROJECT_URL = "https://chatgpt.com/g/g-p-adapter-test/project"


def completed_envelope(*, response: str = "{}", project_url: str = PROJECT_URL) -> dict[str, Any]:
    return {
        "responseText": response,
        "consultationId": "CONSULT-20260904-000001-aabbccdd",
        "requestCount": 1,
        "receipt": {
            "consultation_id": "CONSULT-20260904-000001-aabbccdd",
            "request_count": 1,
            "status": "complete",
            "mode": "fresh",
            "project_url": project_url,
            "project_scope_requested": True,
            "project_scope_verified": True,
            "project_scope_evidence": {
                "initial_navigation": {
                    "requested_url": project_url,
                    "landed_url": project_url,
                    "matched": True,
                    "verified": True,
                }
            },
            "conversation_id": "00000000-0000-0000-0000-000000000001",
        },
    }


class BridgeAdapterTests(unittest.TestCase):
    def test_native_camel_case_envelope_is_normalized_and_bound(self) -> None:
        result = normalize_bridge_envelope(
            completed_envelope(response='{"ok":true}'),
            expected_project_url=PROJECT_URL,
            expected_mode="fresh",
            require_receipt=True,
        )
        self.assertEqual(result["response_text"], '{"ok":true}')
        self.assertEqual(result["consultation_id"], "CONSULT-20260904-000001-aabbccdd")
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["project_url"], PROJECT_URL)

    def test_missing_or_cross_project_envelope_fails_closed(self) -> None:
        missing = completed_envelope()
        missing.pop("responseText")
        with self.assertRaises(BridgeEnvelopeError) as raised:
            normalize_bridge_envelope(missing, expected_project_url=PROJECT_URL, require_receipt=True)
        self.assertEqual(raised.exception.code, "BRIDGE_RESPONSE_EMPTY")

        with self.assertRaises(BridgeEnvelopeError) as raised:
            normalize_bridge_envelope(
                completed_envelope(project_url="https://chatgpt.com/g/g-p-other/project"),
                expected_project_url=PROJECT_URL,
                require_receipt=True,
            )
        self.assertEqual(raised.exception.code, "PROJECT_SCOPE_MISMATCH")

    def test_scoped_receipt_flags_missing_or_false_fail_closed(self) -> None:
        for field in ("project_scope_requested", "project_scope_verified"):
            false_envelope = completed_envelope()
            false_envelope["receipt"][field] = False
            with self.assertRaises(BridgeEnvelopeError) as raised:
                normalize_bridge_envelope(false_envelope, expected_project_url=PROJECT_URL, require_receipt=True)
            self.assertEqual(raised.exception.code, "PROJECT_SCOPE_INVALID")

            missing_envelope = completed_envelope()
            missing_envelope["receipt"].pop(field)
            with self.assertRaises(BridgeEnvelopeError) as raised:
                normalize_bridge_envelope(missing_envelope, expected_project_url=PROJECT_URL, require_receipt=True)
            self.assertEqual(raised.exception.code, "PROJECT_SCOPE_INVALID")

        missing_evidence = completed_envelope()
        missing_evidence["receipt"].pop("project_scope_evidence")
        with self.assertRaises(BridgeEnvelopeError) as raised:
            normalize_bridge_envelope(missing_evidence, expected_project_url=PROJECT_URL, require_receipt=True)
        self.assertEqual(raised.exception.code, "PROJECT_SCOPE_INVALID")

    def test_legacy_receipt_without_project_scope_remains_accepted(self) -> None:
        legacy = completed_envelope()
        for field in (
            "project_url",
            "project_scope_requested",
            "project_scope_verified",
            "project_scope_evidence",
        ):
            legacy["receipt"].pop(field, None)
        result = normalize_bridge_envelope(legacy)
        self.assertNotIn("project_url", result)
        self.assertNotIn("project_scope_verified", result)

    def test_wrapped_envelope_keeps_outer_receipt_and_requires_receipt_scope(self) -> None:
        envelope = completed_envelope(response='{"wrapped":true}')
        receipt = envelope.pop("receipt")
        wrapped = {"data": envelope, "receipt": receipt}
        result = normalize_bridge_envelope(
            wrapped,
            expected_project_url=PROJECT_URL,
            expected_mode="fresh",
            require_receipt=True,
        )
        self.assertEqual(result["response_text"], '{"wrapped":true}')
        self.assertEqual(result["project_url"], PROJECT_URL)

        receipt_without_scope = dict(receipt)
        receipt_without_scope.pop("project_url")
        with self.assertRaises(BridgeEnvelopeError) as raised:
            normalize_bridge_envelope(
                {"data": envelope, "receipt": receipt_without_scope},
                expected_project_url=PROJECT_URL,
                require_receipt=True,
            )
        self.assertEqual(raised.exception.code, "PROJECT_SCOPE_MISSING")

    def test_default_https_port_normalizes_to_canonical_project_url(self) -> None:
        envelope = completed_envelope(project_url="https://chatgpt.com:443/g/g-p-adapter-test/project")
        envelope["receipt"]["project_url"] = "https://chatgpt.com:443/g/g-p-adapter-test/project"
        result = normalize_bridge_envelope(
            envelope,
            expected_project_url=PROJECT_URL,
            require_receipt=True,
        )
        self.assertEqual(result["project_url"], PROJECT_URL)

    def test_adapter_forwards_project_url_once_and_builds_bounded_pack(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-adapter-") as directory:
            root = Path(directory)
            research = root / ".research"
            research.mkdir()
            (research / "PROJECT_BRIEF.json").write_text("{\"state\":\"APPROVED\"}\n", encoding="utf-8")
            calls: list[dict[str, Any]] = []

            def runner(prompt: str, **kwargs: Any) -> dict[str, Any]:
                calls.append({"prompt": prompt, **kwargs})
                return completed_envelope(response='{"answer":"bounded"}')

            consultant = ProjectScopedBridgeConsultant(root, bridge_runner=runner)
            result = consultant.consult(
                project_url=PROJECT_URL,
                mode="fresh",
                prompt="bounded project discovery",
                evidence={"project_id": "p", "real_goal": "goal"},
            )
            self.assertEqual(consultant.calls, 1)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["project_url"], PROJECT_URL)
            self.assertEqual(calls[0]["mode"], "fresh")
            self.assertEqual(result["response_text"], '{"answer":"bounded"}')
            names = [item["logicalName"] for item in calls[0]["context_pack"]["evidence"]]
            self.assertEqual(names, ["PROJECT_BRIEF.json"])
            self.assertNotIn("prompt", calls[0]["context_pack"])

    def test_context_pack_does_not_walk_repository(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-pack-") as directory:
            root = Path(directory)
            (root / "unrelated-secret.txt").write_text("must not be selected", encoding="utf-8")
            pack = build_project_context_pack(root, evidence={"real_goal": "goal"})
            self.assertEqual(pack["evidence"], [])
            self.assertEqual(pack["mode"], "fresh")

    def test_context_pack_includes_sanitized_brief_without_local_root_or_secrets(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-sanitized-pack-") as directory:
            root = Path(directory)
            research = root / ".research"
            research.mkdir()
            (research / "PROJECT_BRIEF.json").write_text(
                json.dumps(
                    {
                        "schema_version": "project_brief.v1",
                        "project_id": "project-test",
                        "state": "APPROVED",
                        "status": "APPROVED",
                        "repository_root": str(root),
                        "project_identity": {
                            "project_id": "project-test",
                            "repository_root": str(root),
                        },
                        "brief": {
                            "goal": "preserve the measurable baseline",
                            "success_criteria": ["the result remains bounded"],
                        },
                        "password": "must-not-be-attached",
                    }
                ),
                encoding="utf-8",
            )
            (research / "PROJECT_CONTEXT.md").write_text(
                f"# Context\n\nrepository_root: {root}\n",
                encoding="utf-8",
            )
            first = build_project_context_pack(root, evidence={"real_goal": "preserve the baseline"})
            second = build_project_context_pack(root, evidence={"real_goal": "preserve the baseline"})
            self.assertEqual(first, second)
            descriptors = {item["logicalName"]: item for item in first["evidence"]}
            self.assertIn("PROJECT_BRIEF.json", descriptors)
            brief_text = descriptors["PROJECT_BRIEF.json"]["content"]
            brief_view = json.loads(brief_text)
            self.assertEqual(brief_view["state"], "APPROVED")
            self.assertEqual(brief_view["brief"]["goal"], "preserve the measurable baseline")
            self.assertEqual(brief_view["repository_root"], "<LOCAL_REPOSITORY_ROOT>")
            self.assertNotIn("must-not-be-attached", brief_text)
            self.assertNotIn(str(root), json.dumps(first, ensure_ascii=False))
            self.assertNotIn(root.as_posix(), json.dumps(first, ensure_ascii=False))
            for descriptor in first["evidence"]:
                self.assertNotIn("sourcePath", descriptor)
                self.assertIn("content", descriptor)
                self.assertTrue(descriptor["sourceRelativePath"].startswith(".research/"))

    def test_subprocess_runner_adapts_native_json_envelope_and_receipt(self) -> None:
        if shutil.which("node") is None:
            self.skipTest("node is required for the deterministic subprocess boundary fixture")
        with tempfile.TemporaryDirectory(prefix="bridge-subprocess-") as directory:
            base = Path(directory)
            root = base / "project"
            bridge_root = base / "bridge"
            (root / ".research").mkdir(parents=True)
            (bridge_root / "scripts").mkdir(parents=True)
            (bridge_root / "scripts" / "consult-pack.mjs").write_text(
                """
import fs from 'node:fs/promises';
import path from 'node:path';
const args = process.argv.slice(2);
const spec = JSON.parse(await fs.readFile(args[args.indexOf('--spec') + 1], 'utf8'));
if (spec.project_id !== 'adapter-project') throw new Error('project_id was not forwarded');
const id = 'CONSULT-20260904-000002-aabbccdd';
const dir = path.join(spec.root_dir, '.consultations', id);
await fs.mkdir(dir, { recursive: true });
const receiptPath = path.join(dir, 'receipt.json');
await fs.writeFile(receiptPath, JSON.stringify({
  consultation_id: id,
  request_count: 1,
  status: 'complete',
  mode: 'fresh',
  project_url: spec.project_url,
  project_scope_requested: true,
  project_scope_verified: true,
  project_scope_evidence: {
    initial_navigation: {
      requested_url: spec.project_url,
      landed_url: spec.project_url,
      matched: true,
      verified: true
    }
  },
  conversation_id: '00000000-0000-0000-0000-000000000002'
}));
console.log(JSON.stringify({
  responseText: '{"bridge":"native"}',
  consultationId: id,
  requestCount: 1,
  projectUrl: spec.project_url,
  receiptPath
}));
""".strip()
                + "\n",
                encoding="utf-8",
            )
            result = subprocess_bridge_runner(
                "native envelope boundary",
                mode="fresh",
                continue_from=None,
                context_pack={"mode": "fresh", "evidence": []},
                root_dir=str(root),
                profile_dir=None,
                bridge_root=bridge_root,
                project_url=PROJECT_URL,
                project_id="adapter-project",
            )
            self.assertEqual(result["response_text"], '{"bridge":"native"}')
            self.assertEqual(result["request_count"], 1)
            self.assertEqual(result["project_url"], PROJECT_URL)
            self.assertEqual(result["receipt"]["project_url"], PROJECT_URL)
            self.assertTrue(Path(result["receipt_path"]).is_file())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
