"""Step 9 local orchestration guards and bounded receipt tests."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from src.stage_controller import StageController, StageControllerError, StageState
from src.stage_integration import (
    INTEGRATION_MARKER,
    INTEGRATION_BLOCKED_MARKER,
    StageIntegrationAdapter,
    StageIntegrationError,
    parse_dialogue_decision,
    subprocess_bridge_runner,
)


def contract(stage_id: str = "stage-9") -> dict:
    return {
        "schema_version": "stage_contract.v1",
        "plan_id": "plan-step9",
        "project_id": "disposable-step9",
        "repository_root": "D:/disposable-step9",
        "stage_id": stage_id,
        "stage_name": "real bridge integration fixture",
        "project_goal": "produce a bounded user-visible fixture artifact",
        "stage_goal": "improve the fixture output and verify a closeout review",
        "user_visible_goal": "show a stable fixture output with passing checks",
        "inputs": ["input.txt"],
        "protected_paths": [".git", ".auth"],
        "allowed_paths": [".research", "tests"],
        "acceptance_description": "the result has passing checks and a review artifact",
        "required_checks": ["unit", "measurement"],
        "review_artifact_requirements": ["metrics_summary"],
        "baseline": {"metric": 1.0},
        "status": "PLANNED",
    }


class FakeBridge:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, prompt: str, **kwargs):
        index = len(self.calls) + 1
        self.calls.append({"prompt": prompt, **kwargs})
        consultation_id = f"CONSULT-20260904-00000{index}-aabbccdd"
        mode = kwargs["mode"]
        pack = kwargs.get("context_pack") or {}
        packet_id = pack.get("packet_id", pack.get("packetId")) if isinstance(pack, dict) else None
        response_decision = "STAGE_READY" if packet_id in {"PACK-close", "PACK-c"} else "CONTINUE"
        return {
            "consultation_id": consultation_id,
            "request_count": 1,
            "response_text": f"bounded response {index}\nWORKFLOW_DECISION: {response_decision}\n",
            "receipt_path": f"D:/disposable-step9/.consultations/{consultation_id}/receipt.json",
            "receipt": {
                "consultation_id": consultation_id,
                "mode": mode,
                "status": "complete",
                "request_count": 1,
                "conversation_id": f"conversation-{index}",
                "conversation_validated": True,
                "response_char_count": 40,
            },
        }


def build_adapter(*, bridge=None, ceiling=12, receipt_root=None):
    controller = StageController(contract())
    adapter = StageIntegrationAdapter(
        controller,
        bridge_runner=bridge or FakeBridge(),
        emergency_iteration_ceiling=ceiling,
        receipt_root=receipt_root,
    )
    return controller, adapter


class Step9GuardTests(unittest.TestCase):
    def test_consult_and_local_action_require_explicit_start(self):
        bridge = FakeBridge()
        controller, adapter = build_adapter(bridge=bridge)
        with self.assertRaises(StageIntegrationError) as error:
            adapter.publish_evidence({"score": 1}, abstraction_layer="fixture")
        self.assertEqual(error.exception.code, "STAGE_NOT_STARTED")
        with self.assertRaises(StageIntegrationError) as error:
            adapter.consult_gpt("review", context_pack={"packet_id": "PACK-test"})
        self.assertEqual(error.exception.code, "STAGE_NOT_STARTED")
        self.assertEqual(controller.show_stage()["status"], StageState.PLANNED.value)
        self.assertEqual(bridge.calls, [])

    def test_response_is_read_before_local_action_and_does_not_trigger_another_consult(self):
        bridge = FakeBridge()
        controller, adapter = build_adapter(bridge=bridge)
        adapter.start_stage()
        adapter.publish_evidence({"score": 1.0}, abstraction_layer="fixture", user_visible_improvement=True)
        handle = adapter.consult_gpt("review", context_pack={"packet_id": "PACK-test"})
        with self.assertRaises(StageIntegrationError) as error:
            adapter.execute_local_action(lambda _response, _request: {"status": "SUCCEEDED"})
        self.assertEqual(error.exception.code, "GPT_RESPONSE_NOT_READ")
        self.assertEqual(len(bridge.calls), 1)
        self.assertEqual(controller.show_stage()["status"], StageState.ACTIVE.value)

        view = adapter.read_response(handle)
        self.assertEqual(view["decision"], "CONTINUE")
        calls: list[dict] = []

        def local_action(response, request):
            calls.append({"response": response, "request": request})
            self.assertEqual(response["decision"], "CONTINUE")
            return {
                "status": "SUCCEEDED",
                "evidence": {"score": 0.8, "changed_files": [".research/result.txt"]},
            }

        result = adapter.execute_local_action(
            local_action,
            objective="apply one bounded fixture improvement",
            abstraction_layer="fixture",
            user_visible_improvement=True,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["evidence"]["revision"], 2)
        self.assertEqual(len(bridge.calls), 1)
        self.assertIsNone(adapter._pending)

    def test_duplicate_digest_mode_is_rejected_and_fresh_can_be_distinct(self):
        bridge = FakeBridge()
        _controller, adapter = build_adapter(bridge=bridge)
        adapter.start_stage()
        adapter.publish_evidence({"score": 1}, abstraction_layer="fixture", user_visible_improvement=True)
        first = adapter.consult_gpt("review", context_pack={"packet_id": "PACK-a"})
        adapter.read_response(first)
        with self.assertRaises(StageIntegrationError) as error:
            adapter.consult_gpt("duplicate", context_pack={"packet_id": "PACK-a"})
        self.assertIn(error.exception.code, {"CONSULTATION_PENDING", "EVIDENCE_REVISION_NOT_NEW"})
        adapter.execute_local_action(
            lambda _response, _request: {"status": "SUCCEEDED", "evidence": {"score": 0.7}},
            abstraction_layer="fixture",
            user_visible_improvement=True,
        )
        second = adapter.consult_gpt("closeout", mode="FRESH", context_pack={"packet_id": "PACK-b"})
        self.assertEqual(second.mode, "FRESH")
        self.assertEqual(len(bridge.calls), 2)

    def test_response_decision_is_not_an_implicit_stage_transition(self):
        bridge = FakeBridge()
        controller, adapter = build_adapter(bridge=bridge)
        adapter.start_stage()
        adapter.publish_evidence({"score": 1}, abstraction_layer="fixture", user_visible_improvement=True)
        adapter.execute_local_action(
            lambda _response, _request: {"status": "SUCCEEDED", "evidence": {"score": 0.5}},
            abstraction_layer="fixture",
            user_visible_improvement=True,
        )
        handle = adapter.consult_gpt("fresh closeout", mode="FRESH", context_pack={"packet_id": "PACK-close"})
        view = adapter.read_response(handle)
        self.assertEqual(view["decision"], "STAGE_READY")
        self.assertEqual(controller.show_stage()["status"], StageState.ACTIVE.value)
        ready = adapter.mark_stage_ready(
            required_checks={"unit": "PASS", "measurement": "PASS"},
            review_artifacts={"metrics_summary": "artifact://stage-9/primary/metrics_summary/a"},
        )
        self.assertEqual(ready["stage"]["status"], StageState.STAGE_READY.value)
        with self.assertRaises(StageIntegrationError) as error:
            adapter.consult_gpt("must fail", context_pack={"packet_id": "PACK-no"})
        self.assertEqual(error.exception.code, "STAGE_READY_LOCKED")
        with self.assertRaises(StageIntegrationError) as error:
            adapter.execute_local_action(lambda _response, _request: {"status": "SUCCEEDED"})
        self.assertEqual(error.exception.code, "STAGE_READY_LOCKED")

    def test_approved_stage_does_not_start_registered_next_stage(self):
        bridge = FakeBridge()
        controller, adapter = build_adapter(bridge=bridge)
        adapter.start_stage()
        adapter.publish_evidence({"score": 1}, abstraction_layer="fixture", user_visible_improvement=True)
        adapter.execute_local_action(
            lambda _response, _request: {"status": "SUCCEEDED", "evidence": {"score": 0.4}},
            abstraction_layer="fixture",
            user_visible_improvement=True,
        )
        adapter.consult_gpt("closeout", mode="FRESH", context_pack={"packet_id": "PACK-c"})
        adapter.read_response()
        adapter.mark_stage_ready(
            required_checks={"unit": "PASS", "measurement": "PASS"},
            review_artifacts={"metrics_summary": "artifact://stage-9/primary/metrics_summary/a"},
        )
        adapter.approve_stage(rationale="disposable harness approval")
        controller.register_stage(contract("stage-next"))
        self.assertEqual(controller.show_stage("stage-next")["status"], StageState.PLANNED.value)
        self.assertIsNone(controller.state["active_stage_id"])

    def test_same_abstraction_threshold_requires_fresh_or_replan(self):
        bridge = FakeBridge()
        _controller, adapter = build_adapter(bridge=bridge)
        adapter.start_stage()
        first = adapter.publish_evidence({"score": 1}, abstraction_layer="same", user_visible_improvement=False)
        self.assertFalse(first["architecture"]["triggered"])
        second = adapter.publish_evidence({"score": 1.1}, abstraction_layer="same", user_visible_improvement=False)
        self.assertTrue(second["architecture"]["triggered"])
        with self.assertRaises(StageIntegrationError) as error:
            adapter.consult_gpt("ordinary follow-up", context_pack={"packet_id": "PACK-a"})
        self.assertEqual(error.exception.code, "ARCHITECTURE_REVIEW_REQUIRED")
        fresh = adapter.consult_gpt("independent review", mode="FRESH", context_pack={"packet_id": "PACK-b"})
        self.assertEqual(fresh.mode, "FRESH")
        self.assertEqual(len(bridge.calls), 1)

    def test_emergency_ceiling_is_only_a_high_safety_limit(self):
        bridge = FakeBridge()
        controller, adapter = build_adapter(bridge=bridge, ceiling=2)
        adapter.start_stage()
        adapter.execute_local_action(
            lambda _response, _request: {"status": "SUCCEEDED", "evidence": {"n": 1}},
            abstraction_layer="fixture",
            user_visible_improvement=True,
        )
        adapter.execute_local_action(
            lambda _response, _request: {"status": "SUCCEEDED", "evidence": {"n": 2}},
            abstraction_layer="fixture",
            user_visible_improvement=True,
        )
        with self.assertRaises(StageIntegrationError) as error:
            adapter.execute_local_action(lambda _response, _request: {"status": "SUCCEEDED"})
        self.assertEqual(error.exception.code, "EMERGENCY_ITERATION_CEILING")
        self.assertEqual(controller.show_stage()["status"], StageState.ACTIVE.value)

    def test_receipt_is_bounded_and_has_no_prompt_or_raw_response(self):
        bridge = FakeBridge()
        with tempfile.TemporaryDirectory() as directory:
            _controller, adapter = build_adapter(bridge=bridge, receipt_root=directory)
            adapter.start_stage()
            adapter.publish_evidence({"score": 1}, abstraction_layer="fixture", user_visible_improvement=True)
            handle = adapter.consult_gpt("secret prompt must not persist", context_pack={"packet_id": "PACK-a"})
            adapter.read_response(handle)
            payload = json.loads(adapter.receipt_path.read_text(encoding="utf-8"))
            serialized = json.dumps(payload, ensure_ascii=False)
            self.assertIsNone(payload["marker"])
            self.assertIsNone(payload["failure_code"])
            self.assertIsNone(payload["stop_reason"])
            self.assertNotIn("secret prompt", serialized)
            self.assertNotIn("bounded response", serialized)
            self.assertNotIn("raw_response", serialized)
            self.assertLess(len(serialized), 100_000)

    def test_failed_receipt_is_blocked_and_never_claims_pass(self):
        def invalid_budget_bridge(_prompt, **_kwargs):
            return {
                "consultation_id": "CONSULT-20260904-000099-aabbccdd",
                "request_count": 0,
                "response_text": "bounded response",
                "receipt": {"status": "complete", "request_count": 0},
            }

        with tempfile.TemporaryDirectory() as directory:
            _controller, adapter = build_adapter(
                bridge=invalid_budget_bridge,
                receipt_root=directory,
            )
            adapter.start_stage()
            adapter.publish_evidence(
                {"score": 1},
                abstraction_layer="fixture",
                user_visible_improvement=True,
            )
            with self.assertRaises(StageIntegrationError) as error:
                adapter.consult_gpt(
                    "bounded review",
                    context_pack={"packet_id": "PACK-failed"},
                )
            self.assertEqual(error.exception.code, "BRIDGE_REQUEST_BUDGET_INVALID")
            payload = json.loads(adapter.receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["marker"], INTEGRATION_BLOCKED_MARKER)
            self.assertNotEqual(payload["marker"], INTEGRATION_MARKER)
            self.assertEqual(payload["failure_code"], "BRIDGE_REQUEST_BUDGET_INVALID")
            self.assertEqual(payload["stop_reason"], "consultation_failed")
            self.assertEqual(payload["status"], StageState.ACTIVE.value)
            self.assertIn("integration_receipt", error.exception.details)

    def test_invalid_workflow_decision_is_failed_before_complete_event_and_not_retried(self):
        calls: list[dict] = []

        def invalid_decision_bridge(prompt, **kwargs):
            calls.append({"prompt": prompt, **kwargs})
            return {
                "consultation_id": "CONSULT-20260905-000099-aabbccdd",
                "request_count": 1,
                "response_text": "private bounded review\nWORKFLOW_DECISION: MAYBE\n",
                "receipt": {
                    "status": "complete",
                    "request_count": 1,
                    "mode": kwargs["mode"],
                    "conversation_id": "conversation-invalid",
                },
            }

        with tempfile.TemporaryDirectory() as directory:
            controller, adapter = build_adapter(
                bridge=invalid_decision_bridge,
                receipt_root=directory,
            )
            adapter.start_stage()
            adapter.publish_evidence(
                {"score": 1},
                abstraction_layer="fixture",
                user_visible_improvement=True,
            )
            with self.assertRaises(StageIntegrationError) as raised:
                adapter.consult_gpt(
                    "bounded review",
                    context_pack={"packet_id": "PACK-invalid-decision"},
                )
            self.assertEqual(raised.exception.code, "GPT_DECISION_INVALID")
            payload = json.loads(adapter.receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["marker"], INTEGRATION_BLOCKED_MARKER)
            self.assertEqual(payload["failure_code"], "GPT_DECISION_INVALID")
            self.assertEqual(payload["stop_reason"], "consultation_failed")
            self.assertEqual(payload["consultation_count"], 0)
            self.assertFalse(any(event["event"] == "consultation_complete" for event in payload["events"]))
            failure = [event for event in payload["events"] if event["event"] == "consultation_failed"][-1]
            self.assertEqual(failure["error_code"], "GPT_DECISION_INVALID")
            serialized = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn("private bounded review", serialized)
            self.assertNotIn("raw_response", serialized)

            # The StageController request gate consumed the digest even though
            # the response was malformed, so a second call cannot retry it.
            with self.assertRaises(StageIntegrationError) as retry:
                adapter.consult_gpt(
                    "retry must be rejected",
                    context_pack={"packet_id": "PACK-invalid-decision-retry"},
                )
            self.assertEqual(retry.exception.code, "CONSULTATION_GATE_REJECTED")
            self.assertEqual(len(calls), 1)

    def test_subprocess_runner_removes_transient_prompt_and_response_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "fixture"
            root.mkdir()
            fake_bridge = Path(directory) / "bridge"
            (fake_bridge / "scripts").mkdir(parents=True)
            script = fake_bridge / "scripts" / "consult-pack.mjs"
            script.write_text(
                """
import fs from 'node:fs/promises';
import path from 'node:path';
const args = process.argv.slice(2);
const spec = JSON.parse(await fs.readFile(args[args.indexOf('--spec') + 1], 'utf8'));
const id = 'CONSULT-20260904-000000-aabbccdd';
const dir = path.join(spec.root_dir, '.consultations', id);
await fs.mkdir(dir, { recursive: true });
await fs.writeFile(path.join(dir, 'request.txt'), spec.question);
await fs.writeFile(path.join(dir, 'response.txt'), 'safe\\nWORKFLOW_DECISION: CONTINUE\\n');
await fs.writeFile(path.join(dir, 'receipt.json'), JSON.stringify({
  consultation_id: id, status: 'complete', request_count: 1,
  mode: 'fresh', conversation_id: 'conversation-a'
}));
console.log(`consultation_id=${id}`);
console.log('request_count=1');
console.log(`receipt=${path.join(dir, 'receipt.json')}`);
console.log('CHATGPT_RESPONSE_BEGIN');
console.log('safe');
console.log('WORKFLOW_DECISION: CONTINUE');
console.log('CHATGPT_RESPONSE_END');
""".strip()
                + "\n",
                encoding="utf-8",
            )
            result = subprocess_bridge_runner(
                "transient prompt",
                mode="fresh",
                continue_from=None,
                context_pack={"packet_id": "PACK-test"},
                root_dir=str(root),
                profile_dir=None,
                node_executable="node",
                bridge_root=fake_bridge,
            )
            self.assertEqual(result["request_count"], 1)
            consultation_dir = root / ".consultations" / result["consultation_id"]
            self.assertFalse((consultation_dir / "request.txt").exists())
            self.assertFalse((consultation_dir / "response.txt").exists())
            self.assertTrue((consultation_dir / "receipt.json").exists())

    def test_subprocess_runner_exposes_only_context_staging_root_to_bridge(self):
        if shutil.which("node") is None:
            self.skipTest("node is required for the deterministic subprocess boundary fixture")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "supervisor-origin"
            root.mkdir()
            fake_bridge = Path(directory) / "bridge"
            (fake_bridge / "scripts").mkdir(parents=True)
            (fake_bridge / "scripts" / "consult-pack.mjs").write_text(
                "import fs from 'node:fs/promises';\n"
                "import path from 'node:path';\n"
                "const args = process.argv.slice(2);\n"
                "const spec = JSON.parse(await fs.readFile(args[args.indexOf('--spec') + 1], 'utf8'));\n"
                "await fs.writeFile(path.join(spec.root_dir, 'observed-attachment-root.txt'), process.env.CHATGPT_ALLOWED_ATTACHMENT_ROOTS || '');\n"
                "const id = 'CONSULT-20260904-000000-aabbccdd';\n"
                "const dir = path.join(spec.root_dir, '.consultations', id);\n"
                "await fs.mkdir(dir, { recursive: true });\n"
                "await fs.writeFile(path.join(dir, 'receipt.json'), JSON.stringify({ consultation_id: id, status: 'complete', request_count: 1, mode: 'fresh', conversation_id: 'conversation-a' }));\n"
                "console.log(`consultation_id=${id}`);\n"
                "console.log('request_count=1');\n"
                "console.log(`receipt=${path.join(dir, 'receipt.json')}`);\n"
                "console.log('CHATGPT_RESPONSE_BEGIN');\n"
                "console.log('safe\\nWORKFLOW_DECISION: CONTINUE');\n"
                "console.log('CHATGPT_RESPONSE_END');\n",
                encoding="utf-8",
            )
            result = subprocess_bridge_runner(
                "supervisor-origin staging boundary",
                mode="fresh",
                continue_from=None,
                context_pack={"packet_id": "PACK-supervisor-origin"},
                root_dir=str(root),
                profile_dir=None,
                bridge_root=fake_bridge,
            )
            self.assertEqual(result["request_count"], 1)
            observed = (root / "observed-attachment-root.txt").read_text(encoding="utf-8")
            self.assertEqual(
                Path(observed).resolve(),
                (root / ".consultations" / "staging").resolve(),
            )
            self.assertNotEqual(Path(observed).resolve(), root.resolve())

    def test_subprocess_failure_marker_accepts_space_and_equals_forms(self):
        if shutil.which("node") is None:
            self.skipTest("node is required for the deterministic subprocess boundary fixture")
        markers = (
            ("CONTEXT_PACK_SOURCE_NOT_FOUND", "CONTEXT_PACK_CONSULTATION_FAILED CONTEXT_PACK_SOURCE_NOT_FOUND"),
            ("LOGIN_REQUIRED", "CONTEXT_PACK_CONSULTATION_FAILED=LOGIN_REQUIRED"),
        )
        for expected_code, marker in markers:
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "fixture"
                root.mkdir()
                fake_bridge = Path(directory) / "bridge"
                (fake_bridge / "scripts").mkdir(parents=True)
                (fake_bridge / "scripts" / "consult-pack.mjs").write_text(
                    f"console.error({json.dumps(marker)});\nprocess.exitCode = 1;\n",
                    encoding="utf-8",
                )
                with self.assertRaises(StageIntegrationError) as raised:
                    subprocess_bridge_runner(
                        "bounded failure marker test",
                        mode="fresh",
                        continue_from=None,
                        context_pack={"packet_id": "PACK-failure-marker"},
                        root_dir=str(root),
                        profile_dir=None,
                        bridge_root=fake_bridge,
                    )
                error = raised.exception
                self.assertEqual(error.code, expected_code)
                self.assertEqual(error.details["bridge_failure_code"], expected_code)
                self.assertIsNone(error.details["request_count"])
                self.assertEqual(error.details["failure_phase"], "BRIDGE_SUBPROCESS")
                self.assertEqual(error.details["exception_class"], "BridgeProcessFailure")
                self.assertEqual(error.details["attempt_count"], 1)
                self.assertIsNone(error.details["receipt_path"])

    def test_subprocess_early_failure_is_bounded_and_scrubs_stderr(self):
        if shutil.which("node") is None:
            self.skipTest("node is required for the deterministic subprocess boundary fixture")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "fixture"
            root.mkdir()
            fake_bridge = Path(directory) / "bridge"
            (fake_bridge / "scripts").mkdir(parents=True)
            stderr_lines = [
                "CONTEXT_PACK_CONSULTATION_FAILED CONTEXT_PACK_SECRET_REJECTED",
                "status=failed_before_prompt",
                "prompt=do-not-persist",
                "cookie=do-not-persist",
                "token=do-not-persist",
                "raw DOM do-not-persist",
                "source path C:/private/do-not-persist",
            ]
            stderr_text = "\n".join(stderr_lines) + "\n"
            (fake_bridge / "scripts" / "consult-pack.mjs").write_text(
                "const lines = "
                + json.dumps(stderr_lines)
                + ";\nfor (const line of lines) console.error(line);\nprocess.exitCode = 1;\n",
                encoding="utf-8",
            )
            with self.assertRaises(StageIntegrationError) as raised:
                subprocess_bridge_runner(
                    "secret prompt must not cross the boundary",
                    mode="fresh",
                    continue_from=None,
                    context_pack={"packet_id": "PACK-early-failure"},
                    root_dir=str(root),
                    profile_dir=None,
                    bridge_root=fake_bridge,
                )
            error = raised.exception
            details_text = json.dumps(error.details, ensure_ascii=False)
            self.assertEqual(error.code, "CONTEXT_PACK_SECRET_REJECTED")
            self.assertEqual(error.details["bridge_failure_code"], "CONTEXT_PACK_SECRET_REJECTED")
            self.assertIsNone(error.details["request_count"])
            self.assertEqual(error.details["failure_phase"], "BRIDGE_SUBPROCESS")
            self.assertEqual(error.details["exception_class"], "BridgeProcessFailure")
            self.assertEqual(error.details["attempt_count"], 1)
            self.assertEqual(error.details["status"], "failed_before_prompt")
            self.assertEqual(list(root.rglob("receipt.json")), [])
            self.assertEqual(
                error.details["stderr_sha256"],
                hashlib.sha256(stderr_text.encode("utf-8")).hexdigest(),
            )
            self.assertLessEqual(len(error.details["stderr_summary"]), 256)
            self.assertRegex(error.details["stderr_summary"], r"^[A-Za-z0-9 _.,:;()=_-]+$")
            for secret in ("do-not-persist", "secret prompt", "cookie", "token", "raw DOM", str(root)):
                self.assertNotIn(secret, details_text)
            self.assertNotIn("stderr", error.details)

    def test_subprocess_malformed_failure_marker_fails_closed(self):
        if shutil.which("node") is None:
            self.skipTest("node is required for the deterministic subprocess boundary fixture")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "fixture"
            root.mkdir()
            fake_bridge = Path(directory) / "bridge"
            (fake_bridge / "scripts").mkdir(parents=True)
            (fake_bridge / "scripts" / "consult-pack.mjs").write_text(
                "console.error('CONTEXT_PACK_CONSULTATION_FAILED');\nprocess.exitCode = 1;\n",
                encoding="utf-8",
            )
            with self.assertRaises(StageIntegrationError) as raised:
                subprocess_bridge_runner(
                    "malformed marker test",
                    mode="fresh",
                    continue_from=None,
                    context_pack={"packet_id": "PACK-malformed-marker"},
                    root_dir=str(root),
                    profile_dir=None,
                    bridge_root=fake_bridge,
                )
            self.assertEqual(raised.exception.code, "BRIDGE_EXTERNAL_FAILURE")
            self.assertEqual(raised.exception.details["bridge_failure_code"], "BRIDGE_EXTERNAL_FAILURE")
            self.assertIsNone(raised.exception.details["request_count"])
            self.assertEqual(raised.exception.details["failure_phase"], "BRIDGE_SUBPROCESS")
            self.assertEqual(raised.exception.details["exception_class"], "BridgeProcessFailure")
            self.assertEqual(raised.exception.details["attempt_count"], 1)

    def test_subprocess_failure_code_is_truncated_and_invalid_code_is_not_retained(self):
        if shutil.which("node") is None:
            self.skipTest("node is required for the deterministic subprocess boundary fixture")
        cases = (
            ("A" + ("X" * 200), "A" + ("X" * 127)),
            ("TOKEN=do-not-persist", "BRIDGE_EXTERNAL_FAILURE"),
        )
        for raw_code, expected_code in cases:
            with self.subTest(raw_code=raw_code[:16]), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "fixture"
                root.mkdir()
                fake_bridge = Path(directory) / "bridge"
                (fake_bridge / "scripts").mkdir(parents=True)
                marker = f"CONTEXT_PACK_CONSULTATION_FAILED {raw_code}"
                (fake_bridge / "scripts" / "consult-pack.mjs").write_text(
                    f"console.error({json.dumps(marker)});\nprocess.exitCode = 1;\n",
                    encoding="utf-8",
                )
                with self.assertRaises(StageIntegrationError) as raised:
                    subprocess_bridge_runner(
                        "bounded failure code test",
                        mode="fresh",
                        continue_from=None,
                        context_pack={"packet_id": "PACK-failure-code"},
                        root_dir=str(root),
                        profile_dir=None,
                        bridge_root=fake_bridge,
                    )
                error = raised.exception
                self.assertEqual(error.code, expected_code)
                self.assertEqual(error.details["bridge_failure_code"], expected_code)
                self.assertLessEqual(len(error.details["bridge_failure_code"]), 128)
                self.assertNotIn("do-not-persist", json.dumps(error.details))

    def test_subprocess_failure_request_count_prefers_receipt_then_markers(self):
        if shutil.which("node") is None:
            self.skipTest("node is required for the deterministic subprocess boundary fixture")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "fixture"
            root.mkdir()
            fake_bridge = Path(directory) / "bridge"
            (fake_bridge / "scripts").mkdir(parents=True)
            script = fake_bridge / "scripts" / "consult-pack.mjs"
            script.write_text(
                "import fs from 'node:fs/promises';\n"
                "const spec = JSON.parse(await fs.readFile(process.argv[process.argv.indexOf('--spec') + 1], 'utf8'));\n"
                "const receiptPath = `${spec.root_dir}/receipt.json`;\n"
                "await fs.writeFile(receiptPath, JSON.stringify({failure_code:'PROJECT_NAVIGATION_FAILED', request_count:0, status:'failed_before_prompt'}));\n"
                "console.error('CONTEXT_PACK_CONSULTATION_FAILED ATTACHMENT_UPLOAD_FAILED');\n"
                "console.error('request_count=1');\n"
                "console.error(`receipt=${receiptPath}`);\n"
                "process.exitCode = 1;\n",
                encoding="utf-8",
            )
            with self.assertRaises(StageIntegrationError) as raised:
                subprocess_bridge_runner(
                    "request count accounting test",
                    mode="fresh",
                    continue_from=None,
                    context_pack={"packet_id": "PACK-request-count"},
                    root_dir=str(root),
                    profile_dir=None,
                    bridge_root=fake_bridge,
                )
            error = raised.exception
            self.assertEqual(error.code, "PROJECT_NAVIGATION_FAILED")
            self.assertEqual(error.details["bridge_failure_code"], "PROJECT_NAVIGATION_FAILED")
            self.assertEqual(error.details["request_count"], 0)
            self.assertEqual(error.details["status"], "failed_before_prompt")

    def test_subprocess_failure_request_count_uses_marker_without_receipt(self):
        if shutil.which("node") is None:
            self.skipTest("node is required for the deterministic subprocess boundary fixture")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "fixture"
            root.mkdir()
            fake_bridge = Path(directory) / "bridge"
            (fake_bridge / "scripts").mkdir(parents=True)
            (fake_bridge / "scripts" / "consult-pack.mjs").write_text(
                "console.error('CONTEXT_PACK_CONSULTATION_FAILED RESPONSE_TIMEOUT');\n"
                "console.error('request_count=1');\n"
                "process.exitCode = 1;\n",
                encoding="utf-8",
            )
            with self.assertRaises(StageIntegrationError) as raised:
                subprocess_bridge_runner(
                    "request marker accounting test",
                    mode="fresh",
                    continue_from=None,
                    context_pack={"packet_id": "PACK-request-marker"},
                    root_dir=str(root),
                    profile_dir=None,
                    bridge_root=fake_bridge,
                )
            self.assertEqual(raised.exception.code, "RESPONSE_TIMEOUT")
            self.assertEqual(raised.exception.details["request_count"], 1)


class DecisionParserTests(unittest.TestCase):
    def test_closed_decision_parser(self):
        self.assertEqual(parse_dialogue_decision("analysis\nWORKFLOW_DECISION: CONTINUE\n"), "CONTINUE")
        with self.assertRaises(StageIntegrationError):
            parse_dialogue_decision("WORKFLOW_DECISION: CONTINUE\nWORKFLOW_DECISION: REPLAN")
        with self.assertRaises(StageIntegrationError):
            parse_dialogue_decision("WORKFLOW_DECISION: MAYBE")

    def test_all_closed_workflow_decisions_remain_accepted(self):
        for decision in ("CONTINUE", "REPLAN", "STAGE_READY", "HUMAN_GATE", "BLOCKED"):
            with self.subTest(decision=decision):
                self.assertEqual(
                    parse_dialogue_decision(f"bounded analysis\nWORKFLOW_DECISION: {decision}\n"),
                    decision,
                )


if __name__ == "__main__":
    unittest.main()
