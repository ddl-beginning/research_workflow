"""Offline acceptance tests for Step 13 bounded Project Discovery."""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from src.contracts import load_schema, sha256_json, validate_instance
from src.asset_layer import AssetLayerError
from src.bridge_adapter import ProjectScopedBridgeConsultant
from src.project_context import build_project_context
from src.project_discovery import (
    ANTI_TUNNEL_RESEARCH_RULE,
    DISCOVERY_MARKER,
    DISCOVERY_REPORT_RELATIVE_PATH,
    LOCAL_PATH_PLACEHOLDER,
    MAX_REPORT_BYTES,
    MAX_RESPONSE_TEXT_LENGTH,
    ProjectDiscoveryError,
    _parse_response_text,
    _sanitize_consultant_response,
    build_approved_discovery_packet,
    build_discovery_evidence,
    build_discovery_prompt,
    discover_project,
    load_discovery_report,
    normalize_discovery_response,
)
from src.project_intake import ProjectRequirementsIntake
from src.stage_integration import subprocess_bridge_runner


class _FakeConsult:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.response


class _FakeVerifier:
    def __init__(self, *, exists: bool = True) -> None:
        self.exists = exists
        self.calls: list[dict[str, Any]] = []

    def verify(self, candidate: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"candidate": candidate, **kwargs})
        return {"verified": self.exists, "checked_read_only": True, "files": []}


class Step13DiscoveryTests(unittest.TestCase):
    def _approved(
        self,
        root: Path,
        *,
        context: bool = True,
        constraints: list[str] | None = None,
    ) -> dict[str, Any]:
        intake = ProjectRequirementsIntake(root)
        intake.initialize(
            mode="USER_CONFIRMED_BRIEF",
            brief={
                "goal": "find a bounded reusable route",
                "success_criteria": ["recommendation is locally checkable"],
                "constraints": list(constraints or []),
                "chatgpt_project_url": "https://chatgpt.com/g/g-p-example-project/project",
            },
        )
        approved = intake.approve(rationale="Step 13 test")
        if context:
            build_project_context(root, target="find a bounded reusable route", next_action="inspect candidates")
        return approved

    def _response(self, *urls: str, no_direct: bool = False) -> dict[str, Any]:
        candidates = [{"repo_url": value, "name": "candidate"} for value in urls]
        primary: Any = None if no_direct else (urls[0] if urls else "a non-repository route")
        return {
            "real_goal": "find a bounded reusable route",
            "search_summary": "bounded offline fixture search",
            "candidate_repositories": candidates,
            "primary_recommendation": primary,
            "why_primary": "the route is inspectable" if not no_direct else "",
            "simpler_alternative": "keep the existing implementation",
            "relevant_non_repo_methods": ["small local experiment"],
            "important_risks": ["license and fit need review"],
            "facts_needing_local_verification": ["default branch"],
            "no_direct_match_found": no_direct,
            "alternatives": list(urls[1:]),
        }

    def test_01_unapproved_brief_is_rejected_before_consult(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step13-unapproved-") as directory:
            root = Path(directory)
            intake = ProjectRequirementsIntake(root)
            intake.initialize(
                mode="USER_CONFIRMED_BRIEF",
                brief={"goal": "target", "success_criteria": ["done"], "chatgpt_project_url": "https://chatgpt.com/g/g-p-other-project/project"},
            )
            consult = _FakeConsult(self._response("https://github.com/example/repo"))
            with self.assertRaises(ProjectDiscoveryError) as raised:
                discover_project(root, consultant=consult, verifier=_FakeVerifier())
            self.assertEqual(raised.exception.code, "BRIEF_NOT_APPROVED")
            self.assertEqual(consult.calls, [])

    def test_02_approved_packet_and_context_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step13-packet-") as directory:
            root = Path(directory)
            approved = self._approved(root)
            packet = build_approved_discovery_packet(root, brief=approved)
            self.assertEqual(packet["mode"], "fresh")
            self.assertEqual(packet["evidence"]["project_id"], approved["project_id"])
            self.assertTrue(packet["evidence"]["project_context"]["present"])
            self.assertIn("PROJECT_CONTEXT_COMPACTION_PASS", packet["evidence"]["project_context"]["markdown"])
            self.assertEqual(len(packet["evidence_digest"]), 64)

    def test_03_antitunnel_and_project_url_are_forwarded_once(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step13-forward-") as directory:
            root = Path(directory)
            self._approved(root)
            consult = _FakeConsult(self._response("https://github.com/Example/Repo.git"))
            verifier = _FakeVerifier()
            report = discover_project(root, consultant=consult, verifier=verifier)
            self.assertEqual(len(consult.calls), 1)
            self.assertEqual(consult.calls[0]["project_url"], "https://chatgpt.com/g/g-p-example-project/project")
            self.assertEqual(consult.calls[0]["mode"], "fresh")
            self.assertIn(ANTI_TUNNEL_RESEARCH_RULE, consult.calls[0]["prompt"])
            self.assertEqual(report["consultation"]["request_count"], 1)
            self.assertEqual(report["marker"], DISCOVERY_MARKER)
            self.assertTrue(verifier.calls)
            self.assertEqual(verifier.calls[0]["candidate"]["repo_url"], "https://github.com/Example/Repo")

    def test_asset_layer_failure_telemetry_keeps_only_bounded_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step13-asset-telemetry-") as directory:
            root = Path(directory)
            approved = self._approved(root)
            failure = AssetLayerError(
                "AVAILABLE_ASSETS_INVALID",
                "raw local detail must not cross the receipt boundary",
                schema_path="$.assets[0].role",
                failure_field="assets[0].role",
            )
            with patch("src.project_discovery.collect_available_assets", side_effect=failure):
                with self.assertRaises(ProjectDiscoveryError) as raised:
                    build_discovery_evidence(root, brief=approved)
            error = raised.exception
            self.assertEqual(error.code, "ASSET_LAYER_INVALID")
            self.assertEqual(
                error.details,
                {
                    "asset_error_code": "AVAILABLE_ASSETS_INVALID",
                    "schema_path": "$.assets[0].role",
                    "failure_field": "assets[0].role",
                },
            )
            self.assertNotIn("raw local detail", str(error))

    def test_asset_layer_constraint_overflow_is_reported_without_raw_text(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step13-asset-constraint-overflow-") as directory:
            root = Path(directory)
            (root / "README.md").write_text("# local project\n", encoding="utf-8")
            approved = self._approved(root, constraints=[f"constraint-{index}" for index in range(17)])
            with self.assertRaises(ProjectDiscoveryError) as raised:
                build_discovery_evidence(root, brief=approved)
            error = raised.exception
            self.assertEqual(error.code, "ASSET_LAYER_INVALID")
            self.assertEqual(error.details, {"asset_error_code": "ASSET_INPUT_TOO_LARGE", "failure_field": "brief.constraints"})
            self.assertNotIn("constraint-16", str(error))

    def test_03b_production_prompt_requires_strict_rfc8259_json(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step13-prompt-contract-") as directory:
            root = Path(directory)
            approved = self._approved(root)
            prompt = build_discovery_prompt(root, brief=approved).casefold()
            self.assertIn("rfc 8259", prompt)
            self.assertIn("valid json object", prompt)
            self.assertIn("no markdown fence", prompt)
            self.assertIn("no leading or trailing prose", prompt)
            self.assertIn("no comments", prompt)
            self.assertIn("multiple objects", prompt)
            self.assertIn("an array", prompt)
            self.assertIn("a scalar", prompt)
            self.assertIn("json escapes", prompt)
            self.assertIn("newline", prompt)
            self.assertIn("tab", prompt)
            self.assertIn("control character", prompt)
            self.assertIn("double quotes", prompt)
            self.assertIn("backslashes", prompt)

    def test_03b_compact_prompt_still_requires_complete_schema(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step13-prompt-compact-") as directory:
            root = Path(directory)
            approved = self._approved(root)
            prompt = build_discovery_prompt(root, brief=approved).casefold()
            self.assertIn("keep the output compact", prompt)
            self.assertIn("<=10000 chars", prompt)
            self.assertIn("compress summaries", prompt)
            self.assertIn("complete semantic schema", prompt)
            self.assertIn("required fields", prompt)
            self.assertIn("never omit required fields", prompt)

    def test_03e_prompt_lists_semantic_schema_and_minimal_json_skeleton(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step13-prompt-schema-") as directory:
            root = Path(directory)
            approved = self._approved(root)
            prompt = build_discovery_prompt(root, brief=approved).casefold()
            for field in (
                "real_goal",
                "search_summary",
                "no_direct_match_found",
                "primary_recommendation",
                "why_primary",
                "candidate_repositories",
                "alternatives",
                "relevant_non_repo_methods",
                "simpler_alternative",
                "important_risks",
                "facts_needing_local_verification",
            ):
                with self.subTest(field=field):
                    self.assertIn(field, prompt)
            for constraint in (
                "non-empty string",
                "boolean",
                "at most five",
                "at most four",
                "at most eight",
                "at most sixteen",
                "no unknown fields",
                "unverified claims, not established local facts",
                "absolute paths",
                "local absolute path or relative file path",
                "windows drive paths",
                "unc paths",
                "posix paths",
                "/home/",
                "/tmp/",
                "./",
                "../",
                "attachment paths",
                "repository root",
                "profile path",
                "workspace paths",
                "log paths",
                "prompt filenames",
                "attachment filenames",
                "<local_path>",
                "<local_repository_root>",
                "canonical https",
                "repo_url",
                "clone path",
                "candidate-user-project",
                "asset_id",
                "bounded summary",
                "secrets",
                '"primary_recommendation":null',
                '"candidate_repositories":[]',
                '"alternatives":[]',
            ):
                with self.subTest(constraint=constraint):
                    self.assertIn(constraint, prompt)

    def test_03h_path_safe_placeholders_and_https_repo_shape_are_accepted(self) -> None:
        payload = self._response("https://github.com/example/repo")
        payload["search_summary"] = (
            "Local evidence is summarized with <LOCAL_PATH> under "
            "<LOCAL_REPOSITORY_ROOT>."
        )
        payload["primary_recommendation"] = {
            "asset_id": "candidate-user-project",
            "summary": "bounded local candidate summary",
        }
        payload["alternatives"] = [
            {
                "asset_id": "candidate-user-project",
                "summary": "same local candidate at <LOCAL_PATH>",
            },
            {"repo_url": "https://github.com/example/other", "name": "external"},
        ]

        normalized = normalize_discovery_response(payload)
        self.assertEqual(normalized["search_summary"], payload["search_summary"])
        self.assertEqual(normalized["primary_recommendation"], payload["primary_recommendation"])
        self.assertEqual(
            normalized["candidate_repositories"][0]["repo_url"],
            "https://github.com/example/repo",
        )

    def test_03i_local_path_variants_fail_closed_as_absolute_path(self) -> None:
        path_values = (
            r"C:\Users\Alice\repo\README.md",
            r"\\server\share\repo\README.md",
            "/home/alice/repo/README.md",
            "/tmp/repo/README.md",
            "./src/main.py",
            "../secrets.txt",
        )
        for local_path in path_values:
            payload = self._response("https://github.com/example/repo")
            payload["search_summary"] = f"candidate reference: {local_path}"
            with self.subTest(local_path=local_path):
                with self.assertRaises(ProjectDiscoveryError) as raised:
                    normalize_discovery_response(payload)
                self.assertEqual(raised.exception.code, "DISCOVERY_RESPONSE_INVALID")
                self.assertEqual(getattr(raised.exception, "grammar_branch", None), "ABSOLUTE_PATH")

    def test_03j_https_repository_url_is_not_mistaken_for_a_local_path(self) -> None:
        payload = self._response("https://github.com/example/repo")
        payload["search_summary"] = "External reference: https://github.com/example/repo"
        normalized = normalize_discovery_response(payload)
        self.assertEqual(normalized["search_summary"], payload["search_summary"])
        self.assertEqual(normalized["candidate_repositories"][0]["repo_url"], "https://github.com/example/repo")

    def test_03k_real_response_adapter_sanitizes_local_paths_but_strict_parser_rejects_them(self) -> None:
        payload = self._response("https://github.com/example/repo")
        payload["search_summary"] = (
            r"Windows C:\Users\Alice\repo\README.md; "
            r"UNC \\server\share\repo\README.md; "
            "POSIX /tmp/repo/README.md; "
            "remote https://github.com/example/repo"
        )
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)

        # The strict parser remains fail-closed when called directly.
        with self.assertRaises(ProjectDiscoveryError) as direct:
            _parse_response_text(raw)
        self.assertEqual(direct.exception.code, "DISCOVERY_RESPONSE_INVALID")
        self.assertEqual(getattr(direct.exception, "grammar_branch", None), "ABSOLUTE_PATH")
        with self.assertRaises(ProjectDiscoveryError) as public_direct:
            normalize_discovery_response(raw)
        self.assertEqual(getattr(public_direct.exception, "grammar_branch", None), "ABSOLUTE_PATH")

        sanitized, telemetry = _sanitize_consultant_response(raw)
        self.assertIsInstance(sanitized, dict)
        sanitized_text = json.dumps(sanitized, ensure_ascii=False, sort_keys=True)
        # The strict matcher can expose an encoded UNC path as more than one
        # lexical hit; all three path families must nevertheless be covered.
        self.assertGreaterEqual(telemetry["path_sanitization_count"], 3)
        self.assertRegex(telemetry["path_sanitization_digest"], r"^[0-9a-f]{64}$")
        self.assertNotIn(r"C:\Users\Alice\repo\README.md", sanitized_text)
        self.assertNotIn(r"\\server\share\repo\README.md", sanitized_text)
        self.assertNotIn("/tmp/repo/README.md", sanitized_text)
        self.assertIn(LOCAL_PATH_PLACEHOLDER, sanitized_text)
        self.assertIn("https://github.com/example/repo", sanitized_text)

        normalized = normalize_discovery_response(sanitized)
        self.assertIn(LOCAL_PATH_PLACEHOLDER, normalized["search_summary"])
        self.assertIn("https://github.com/example/repo", normalized["search_summary"])

    def test_03m_http_url_path_like_segments_are_preserved_by_adapter(self) -> None:
        cases = (
            ("https://example.com/foo-/bar", "https://example.com/foo-/bar"),
            ("https://example.com/foo./bar", "https://example.com/foo./bar"),
            ("https://example.com/repo?next=/tmp/repo", "https://example.com/repo"),
        )
        for visible_url, candidate_url in cases:
            with self.subTest(visible_url=visible_url):
                payload = self._response(candidate_url)
                payload["search_summary"] = f"bounded remote reference: {visible_url}"
                raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                sanitized, telemetry = _sanitize_consultant_response(raw)
                self.assertIn(visible_url, json.dumps(sanitized, ensure_ascii=False, sort_keys=True))
                self.assertEqual(telemetry["path_sanitization_count"], 0)

                with tempfile.TemporaryDirectory(prefix="step13-http-url-adapter-") as directory:
                    root = Path(directory)
                    approved = self._approved(root)
                    report = discover_project(
                        root,
                        brief=approved,
                        consultant=_FakeConsult(raw),
                        verifier=_FakeVerifier(),
                    )
                    self.assertEqual(report["candidate_repositories"][0]["repo_url"], candidate_url)
                    self.assertIn(visible_url, report["search_summary"])

        file_url_payload = self._response("https://example.com/repo")
        file_url_payload["search_summary"] = "local file:///tmp/repo must remain protected"
        file_sanitized, file_telemetry = _sanitize_consultant_response(
            json.dumps(file_url_payload, ensure_ascii=False, sort_keys=True)
        )
        self.assertNotIn("file:///tmp/repo", json.dumps(file_sanitized, ensure_ascii=False, sort_keys=True))
        self.assertGreaterEqual(file_telemetry["path_sanitization_count"], 1)

        for secret_url in (
            "https://example.com/repo?token=secret-value",
            "https://example.com/repo?api_key=secret-value",
        ):
            with self.subTest(secret_url=secret_url):
                secret_payload = self._response("https://example.com/repo")
                secret_payload["search_summary"] = f"forbidden remote reference: {secret_url}"
                with self.assertRaises(ProjectDiscoveryError) as raised:
                    _sanitize_consultant_response(
                        json.dumps(secret_payload, ensure_ascii=False, sort_keys=True)
                    )
                self.assertEqual(raised.exception.code, "DISCOVERY_SECRET_REJECTED")

    def test_03n_adapter_extracts_object_before_sanitizing_and_keeps_strict_parser(self) -> None:
        payload = self._response("https://github.com/example/repo")
        payload["search_summary"] = r"bounded local evidence C:\repo\README.md"
        raw = "Explanation /tmp/wrapper-before\n```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```\nclosing /tmp/wrapper-after"

        sanitized, telemetry = _sanitize_consultant_response(raw)
        self.assertIsInstance(sanitized, dict)
        serialized = json.dumps(sanitized, ensure_ascii=False, sort_keys=True)
        self.assertIn(LOCAL_PATH_PLACEHOLDER, serialized)
        self.assertNotIn("/tmp/wrapper-before", serialized)
        self.assertNotIn("/tmp/wrapper-after", serialized)
        self.assertGreaterEqual(telemetry["path_sanitization_count"], 1)
        normalized = normalize_discovery_response(sanitized)
        self.assertIn(LOCAL_PATH_PLACEHOLDER, normalized["search_summary"])

        with self.assertRaises(ProjectDiscoveryError) as direct:
            normalize_discovery_response(raw)
        self.assertEqual(getattr(direct.exception, "grammar_branch", None), "ABSOLUTE_PATH")

    def test_03o_adapter_rejects_absolute_path_keys_and_attaches_bounded_telemetry(self) -> None:
        payload = self._response("https://github.com/example/repo")
        payload[r"C:\private\field"] = "must not be accepted"
        with self.assertRaises(ProjectDiscoveryError) as raised:
            _sanitize_consultant_response(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        self.assertEqual(raised.exception.code, "DISCOVERY_RESPONSE_INVALID")
        self.assertEqual(getattr(raised.exception, "grammar_branch", None), "ABSOLUTE_PATH")
        telemetry = getattr(raised.exception, "response_telemetry", None)
        self.assertIsInstance(telemetry, dict)
        self.assertRegex(telemetry["response_sha256_before"], r"^[0-9a-f]{64}$")

    def test_03p_adapter_rejects_secret_in_discarded_wrapper_and_preserves_https_values(self) -> None:
        payload = self._response("https://example.com/repo?next=/tmp/repo")
        payload["search_summary"] = "remote https://example.com/foo-/bar"
        raw = "prefix api_key=not-real /tmp/hidden\n" + json.dumps(payload, ensure_ascii=False)
        with self.assertRaises(ProjectDiscoveryError) as raised:
            _sanitize_consultant_response(raw)
        self.assertEqual(raised.exception.code, "DISCOVERY_SECRET_REJECTED")

        safe_raw = "prefix /tmp/ignored\n" + json.dumps(payload) + "\nsuffix /tmp/ignored-too"
        sanitized, telemetry = _sanitize_consultant_response(safe_raw)
        self.assertEqual(telemetry["path_sanitization_count"], 0)
        self.assertIn("https://example.com/foo-/bar", json.dumps(sanitized, ensure_ascii=False))
        self.assertIn("https://example.com/repo?next=/tmp/repo", json.dumps(sanitized, ensure_ascii=False))

    def test_03l_response_failure_receipt_keeps_branch_and_digest_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step13-path-sanitization-failure-") as directory:
            root = Path(directory)
            approved = self._approved(root)
            raw = r'{"broken": "unterminated'
            with self.assertRaises(ProjectDiscoveryError) as raised:
                discover_project(root, brief=approved, consultant=_FakeConsult(raw), verifier=_FakeVerifier())
            self.assertEqual(raised.exception.code, "DISCOVERY_RESPONSE_INVALID")

            state_path = root / ".research" / "discovery" / "CONSULTATION_STATE.json"
            state_text = state_path.read_text(encoding="utf-8")
            state = json.loads(state_text)
            self.assertEqual(state["grammar_branch"], "OBJECT_UNBALANCED")
            self.assertEqual(state["semantic_branch"], "SCHEMA")
            self.assertEqual(state["path_sanitization_count"], 0)
            self.assertIsNone(state["path_sanitization_digest"])
            self.assertNotIn("unterminated", state_text)

    def test_03f_minimal_no_direct_object_passes_and_wrong_types_fail_closed(self) -> None:
        minimal = {
            "real_goal": "find the real user-visible outcome",
            "search_summary": "No direct match was found in the bounded search",
            "no_direct_match_found": True,
            "primary_recommendation": None,
            "why_primary": "",
            "candidate_repositories": [],
            "alternatives": [],
            "relevant_non_repo_methods": [],
            "simpler_alternative": "",
            "important_risks": [],
            "facts_needing_local_verification": [],
        }
        normalized = normalize_discovery_response(minimal)
        self.assertEqual(normalized["real_goal"], minimal["real_goal"])
        self.assertIsNone(normalized["primary_recommendation"])
        self.assertEqual(normalized["candidate_repositories"], [])
        self.assertEqual(normalized["alternatives"], [])

        invalid = (
            ("real_goal", 42),
            ("search_summary", []),
            ("no_direct_match_found", "true"),
            ("primary_recommendation", []),
            ("candidate_repositories", {}),
            ("alternatives", None),
            ("relevant_non_repo_methods", [{"method": "route"}]),
            ("important_risks", [3]),
            ("facts_needing_local_verification", [{}]),
        )
        for field, value in invalid:
            payload = dict(minimal)
            payload[field] = value
            with self.subTest(field=field):
                with self.assertRaises(ProjectDiscoveryError):
                    normalize_discovery_response(payload)

    def test_03g_semantic_failure_branches_are_stable_and_deidentified(self) -> None:
        minimal = {
            "real_goal": "find the real user-visible outcome",
            "search_summary": "bounded search summary",
            "no_direct_match_found": True,
            "primary_recommendation": None,
            "why_primary": "",
            "candidate_repositories": [],
            "alternatives": [],
            "relevant_non_repo_methods": [],
            "simpler_alternative": "",
            "important_risks": [],
            "facts_needing_local_verification": [],
        }
        cases: tuple[tuple[str, Any, str, str], ...] = (
            ("real_goal", "", "DISCOVERY_INPUT_INVALID", "REAL_GOAL"),
            ("search_summary", "", "DISCOVERY_INPUT_INVALID", "SEARCH_SUMMARY"),
            ("no_direct_match_found", "false", "DISCOVERY_RESPONSE_INVALID", "NO_DIRECT_MATCH"),
            ("primary_recommendation", 17, "DISCOVERY_RESPONSE_INVALID", "PRIMARY"),
            ("candidate_repositories", {}, "DISCOVERY_RESPONSE_INVALID", "CANDIDATE"),
            ("alternatives", {}, "DISCOVERY_RESPONSE_INVALID", "ALTERNATIVE"),
            ("relevant_non_repo_methods", [17], "DISCOVERY_INPUT_INVALID", "METHODS"),
            ("simpler_alternative", None, "DISCOVERY_INPUT_INVALID", "SIMPLER_ALTERNATIVE"),
            ("important_risks", [17], "DISCOVERY_INPUT_INVALID", "RISKS"),
            ("facts_needing_local_verification", [17], "DISCOVERY_INPUT_INVALID", "FACTS"),
        )
        for field, value, expected_code, expected_branch in cases:
            payload = dict(minimal)
            payload[field] = value
            with self.subTest(field=field):
                with self.assertRaises(ProjectDiscoveryError) as raised:
                    normalize_discovery_response(payload)
                self.assertEqual(raised.exception.code, expected_code)
                self.assertEqual(getattr(raised.exception, "semantic_branch", None), expected_branch)
                self.assertNotIn(repr(value), str(raised.exception))

        direct = dict(minimal)
        direct.update(
            {
                "no_direct_match_found": False,
                "primary_recommendation": None,
                "why_primary": "",
            }
        )
        with self.assertRaises(ProjectDiscoveryError) as raised:
            normalize_discovery_response(direct)
        self.assertEqual(raised.exception.code, "PRIMARY_RECOMMENDATION_REQUIRED")
        self.assertEqual(getattr(raised.exception, "semantic_branch", None), "PRIMARY")

        candidate_overflow = dict(minimal)
        candidate_overflow["candidate_repositories"] = [
            {"repo_url": f"https://github.com/example/repo-{index}"} for index in range(6)
        ]
        with self.assertRaises(ProjectDiscoveryError) as raised:
            normalize_discovery_response(candidate_overflow)
        self.assertEqual(raised.exception.code, "CANDIDATE_LIMIT")
        self.assertEqual(getattr(raised.exception, "semantic_branch", None), "CANDIDATE")

        alternative_overflow = dict(minimal)
        alternative_overflow["alternatives"] = [f"route-{index}" for index in range(5)]
        with self.assertRaises(ProjectDiscoveryError) as raised:
            normalize_discovery_response(alternative_overflow)
        self.assertEqual(raised.exception.code, "ALTERNATIVE_LIMIT")
        self.assertEqual(getattr(raised.exception, "semantic_branch", None), "ALTERNATIVE")

    def test_03c_literal_controls_are_recovered_deterministically(self) -> None:
        payload = self._response("https://github.com/example/repo")
        payload["search_summary"] = "line one\nline two\tcolumn\rnext"
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)

        # The transport contract remains escaped RFC 8259 JSON, but the finite
        # parser compatibility boundary repairs only literal controls inside
        # JSON strings.  The resulting semantic object must match the escaped
        # form and therefore produce the same claims digest.
        literal = (
            " \t\r\n"
            + encoded.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")
            + "\r\n\t "
        )
        normalized_literal = normalize_discovery_response(literal)
        normalized_escaped = normalize_discovery_response(encoded)
        self.assertEqual(normalized_literal["search_summary"], payload["search_summary"])
        self.assertEqual(normalized_literal, normalized_escaped)
        self.assertEqual(sha256_json(normalized_literal), sha256_json(normalized_escaped))

    def test_03c_response_bound_allows_13183_complete_object_and_rejects_overflow(self) -> None:
        payload = self._response("https://github.com/example/repo")
        payload["search_summary"] = "summary line one\nsummary line two\tcolumn\rnext " + ("x" * 8_000)
        payload["why_primary"] = "because " + ("y" * 3_000)
        payload["simpler_alternative"] = "keep " + ("z" * 1_000)

        # Exercise the same finite LF/CR/TAB compatibility path as the real
        # response shape while keeping every semantic field below 12,000.
        target_length = 13_183
        def literal_json() -> str:
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            return encoded.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")

        padding = target_length - len(literal_json())
        self.assertGreater(padding, 0)
        payload["simpler_alternative"] += "z" * padding
        response = literal_json()
        self.assertEqual(len(response), target_length)
        self.assertLessEqual(len(response), MAX_RESPONSE_TEXT_LENGTH)
        normalized = normalize_discovery_response(response)
        self.assertEqual(normalized["real_goal"], payload["real_goal"])
        self.assertEqual(normalized["search_summary"], payload["search_summary"])

        with self.assertRaises(ProjectDiscoveryError) as raised:
            normalize_discovery_response("x" * (MAX_RESPONSE_TEXT_LENGTH + 1))
        # Grammar failures keep the compatibility-facing outer code stable;
        # the de-identified branch identifies only the bounded failure class.
        self.assertEqual(raised.exception.code, "DISCOVERY_RESPONSE_INVALID")
        self.assertEqual(getattr(raised.exception, "grammar_branch", None), "TOO_LARGE")

    def test_03d_control_recovery_stays_fail_closed_outside_safe_string_controls(self) -> None:
        payload = self._response("https://github.com/example/repo")
        payload["search_summary"] = "line one\nline two\tcolumn\rnext"
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        literal = encoded.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")

        invalid_responses = (
            # Unsupported C0 controls are not JSON whitespace and may not be
            # silently stripped from prose boundaries or object structure.
            literal + "\x01",
            "\x0b" + literal,
            literal + "\x00",
            encoded.replace("line one", "bad" + "\x02"),
            encoded.replace("line one", "bad" + "\\u0001"),
            # A malformed escape is not repaired, even when a safe literal
            # control appears later in the same string.
            encoded.replace("line one", "bad" + "\\q"),
            # Candidate selection remains unique and duplicate-key rejection
            # remains owned by the strict decoder.
            encoded + encoded,
            json.dumps({**payload, "search_summary": "Bearer " + ("a" * 24)}),
        )
        expected_codes = (
            "DISCOVERY_RESPONSE_INVALID",
            "DISCOVERY_RESPONSE_INVALID",
            "DISCOVERY_RESPONSE_INVALID",
            "DISCOVERY_RESPONSE_INVALID",
            "DISCOVERY_RESPONSE_INVALID",
            "DISCOVERY_RESPONSE_INVALID",
            "DISCOVERY_RESPONSE_INVALID",
            "DISCOVERY_SECRET_REJECTED",
        )
        for response, code in zip(invalid_responses, expected_codes):
            with self.subTest(response=repr(response[:48])):
                with self.assertRaises(ProjectDiscoveryError) as raised:
                    normalize_discovery_response(response)
                self.assertEqual(raised.exception.code, code)

    def test_04_malformed_response_is_one_shot_and_recorded(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step13-malformed-") as directory:
            root = Path(directory)
            self._approved(root)
            consult = _FakeConsult("not-json")
            with self.assertRaises(ProjectDiscoveryError) as raised:
                discover_project(root, consultant=consult, verifier=_FakeVerifier())
            self.assertEqual(raised.exception.code, "DISCOVERY_RESPONSE_INVALID")
            self.assertEqual(len(consult.calls), 1)
            self.assertTrue((root / ".research" / "discovery" / "CONSULTATION_STATE.json").is_file())
            with self.assertRaises(ProjectDiscoveryError) as duplicate:
                discover_project(root, consultant=consult, verifier=_FakeVerifier())
            self.assertEqual(duplicate.exception.code, "CONSULTATION_NOT_RETRIED")
            self.assertEqual(len(consult.calls), 1)

    def test_04b_input_invalid_receipt_keeps_only_fixed_semantic_branch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step13-semantic-receipt-") as directory:
            root = Path(directory)
            self._approved(root)
            response = self._response(no_direct=True)
            response["search_summary"] = {"private_marker": "do-not-persist"}
            consult = _FakeConsult(response)
            with self.assertRaises(ProjectDiscoveryError) as raised:
                discover_project(root, consultant=consult, verifier=_FakeVerifier())
            self.assertEqual(raised.exception.code, "DISCOVERY_INPUT_INVALID")
            self.assertEqual(getattr(raised.exception, "semantic_branch", None), "SEARCH_SUMMARY")
            state_path = root / ".research" / "discovery" / "CONSULTATION_STATE.json"
            state_text = state_path.read_text(encoding="utf-8")
            state = json.loads(state_text)
            self.assertEqual(state["error_code"], "DISCOVERY_INPUT_INVALID")
            self.assertEqual(state["semantic_branch"], "SEARCH_SUMMARY")
            self.assertNotIn("private_marker", state_text)
            self.assertNotIn("do-not-persist", state_text)
            self.assertNotIn("search_summary", state_text)

    def test_04c_real_adapter_bridge_failure_persists_bounded_forensic_and_blocks_retry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step13-bridge-forensic-") as directory:
            root = Path(directory)
            approved = self._approved(root)
            project_url = approved["brief"]["chatgpt_project_url"]

            def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess:
                return subprocess.CompletedProcess(
                    command,
                    1,
                    stdout="",
                    stderr="CONTEXT_PACK_CONSULTATION_FAILED CONTEXT_PACK_INVALID\n",
                )

            consultant = ProjectScopedBridgeConsultant(
                root,
                bridge_runner=lambda prompt, **kwargs: subprocess_bridge_runner(prompt, **kwargs),
                bridge_root=Path(r"D:\work\research_tools\chatgpt_browser_bridge"),
                timeout_ms=1_000,
            )
            with patch("src.stage_integration.subprocess.run", fake_run):
                with self.assertRaises(ProjectDiscoveryError) as raised:
                    discover_project(root, brief=approved, consultant=consultant, verifier=_FakeVerifier())
            self.assertEqual(raised.exception.code, "CONTEXT_PACK_INVALID")
            self.assertEqual(consultant.calls, 1)

            state_path = root / ".research" / "discovery" / "CONSULTATION_STATE.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["failure_phase"], "BRIDGE_SUBPROCESS")
            self.assertEqual(state["exception_class"], "StageIntegrationError")
            self.assertEqual(state["error_code"], "CONTEXT_PACK_INVALID")
            self.assertIsNone(state["request_count"])
            self.assertEqual(state["attempt_count"], 1)
            state_text = state_path.read_text(encoding="utf-8")
            self.assertNotIn("stderr", state_text)
            self.assertNotIn("bounded discovery", state_text)
            self.assertEqual(state["project_url"], project_url)

            with self.assertRaises(ProjectDiscoveryError) as duplicate:
                discover_project(root, brief=approved, consultant=consultant, verifier=_FakeVerifier())
            self.assertEqual(duplicate.exception.code, "CONSULTATION_NOT_RETRIED")
            self.assertEqual(consultant.calls, 1)

    def test_04d_duplicate_runner_kwargs_are_reported_without_retry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step13-duplicate-kwargs-") as directory:
            root = Path(directory)
            approved = self._approved(root)
            bridge_root = Path(r"D:\work\research_tools\chatgpt_browser_bridge")

            def duplicate_runner(prompt: str, **kwargs: Any) -> Any:
                # This reproduces the historical wrapper shape that supplied
                # bridge_root twice.  It must fail before any subprocess call.
                return subprocess_bridge_runner(prompt, bridge_root=bridge_root, **kwargs)

            consultant = ProjectScopedBridgeConsultant(
                root,
                bridge_runner=duplicate_runner,
                bridge_root=bridge_root,
                timeout_ms=1_000,
            )
            with self.assertRaises(ProjectDiscoveryError) as raised:
                discover_project(root, brief=approved, consultant=consultant, verifier=_FakeVerifier())
            self.assertEqual(raised.exception.code, "BRIDGE_RUNNER_FAILED")
            self.assertEqual(consultant.calls, 1)
            state = json.loads((root / ".research" / "discovery" / "CONSULTATION_STATE.json").read_text(encoding="utf-8"))
            self.assertEqual(state["failure_phase"], "BRIDGE_RUNNER")
            self.assertEqual(state["exception_class"], "TypeError")
            self.assertEqual(state["error_code"], "BRIDGE_RUNNER_FAILED")
            self.assertIsNone(state["request_count"])
            self.assertEqual(state["attempt_count"], 1)

    def test_05_injected_local_verifier_controls_promotion(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step13-verifier-") as directory:
            root = Path(directory)
            self._approved(root)
            consult = _FakeConsult(self._response("https://github.com/example/nonexistent"))
            verifier = _FakeVerifier(exists=False)
            with self.assertRaises(ProjectDiscoveryError) as raised:
                discover_project(root, consultant=consult, verifier=verifier)
            self.assertEqual(raised.exception.code, "REPOSITORY_VERIFICATION_FAILED")
            self.assertFalse((root / DISCOVERY_REPORT_RELATIVE_PATH).exists())

    def test_06_duplicate_repository_urls_are_canonicalized_before_verifier(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step13-dedupe-") as directory:
            root = Path(directory)
            self._approved(root)
            response = self._response(
                "https://GitHub.com/example/repo.git",
                "https://github.com/example/repo/",
                "https://github.com/example/other.git",
            )
            consult = _FakeConsult(response)
            verifier = _FakeVerifier()
            report = discover_project(root, consultant=consult, verifier=verifier)
            self.assertEqual(len(verifier.calls), 2)
            self.assertEqual(
                [item["repo_url"] for item in report["candidate_repositories"]],
                ["https://github.com/example/repo", "https://github.com/example/other"],
            )
            self.assertLessEqual(len(report["alternatives"]), 4)

    def test_07_no_direct_match_is_a_valid_report(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step13-no-match-") as directory:
            root = Path(directory)
            self._approved(root)
            report = discover_project(
                root,
                consultant=_FakeConsult(self._response(no_direct=True)),
                verifier=_FakeVerifier(),
            )
            self.assertTrue(report["no_direct_match_found"])
            self.assertIsNone(report["primary_recommendation"])
            self.assertEqual(report["candidate_repositories"], [])
            self.assertEqual(report["alternatives"], [])
            validate_instance(report, load_schema("discovery_report"))

    def test_08_same_evidence_digest_is_blocked_and_report_is_persistent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step13-duplicate-") as directory:
            root = Path(directory)
            self._approved(root)
            response = self._response("https://github.com/example/repo")
            discover_project(root, consultant=_FakeConsult(response), verifier=_FakeVerifier())
            report_path = root / DISCOVERY_REPORT_RELATIVE_PATH
            before = report_path.read_bytes()
            time.sleep(0.01)
            consult = _FakeConsult(response)
            with self.assertRaises(ProjectDiscoveryError) as raised:
                discover_project(root, consultant=consult, verifier=_FakeVerifier())
            self.assertEqual(raised.exception.code, "DUPLICATE_EVIDENCE")
            self.assertEqual(consult.calls, [])
            self.assertEqual(report_path.read_bytes(), before)
            self.assertEqual(load_discovery_report(root)["marker"], DISCOVERY_MARKER)
            self.assertLessEqual(report_path.stat().st_size, MAX_REPORT_BYTES)

    def test_09_post_consultation_candidate_links_do_not_change_duplicate_digest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step13-local-duplicate-") as directory:
            root = Path(directory)
            # Candidate 0 causes record_verified_candidates() to persist a
            # comparison_required relationship after the first consultation.
            # That provenance must not make the same pre-consultation evidence
            # look new and permit a second GPT request.
            (root / "README.md").write_text("# Existing local project\n", encoding="utf-8")
            (root / "main.py").write_text("print('bounded')\n", encoding="utf-8")
            self._approved(root)
            response = self._response("https://github.com/example/repo")
            discover_project(root, consultant=_FakeConsult(response), verifier=_FakeVerifier())
            with self.assertRaises(ProjectDiscoveryError) as raised:
                discover_project(root, consultant=_FakeConsult(response), verifier=_FakeVerifier())
            self.assertEqual(raised.exception.code, "DUPLICATE_EVIDENCE")

    def test_10_response_grammar_accepts_bare_fenced_and_bounded_prose(self) -> None:
        payload = self._response("https://github.com/example/repo")
        bare = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        cases = (
            bare,
            "```json\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n```",
            "Here is the bounded discovery result:\n" + bare,
            bare + "\nThis is the complete bounded result.",
            "Here is the bounded discovery result.\n" + bare + "\nThis is the complete bounded result.",
        )
        for response in cases:
            with self.subTest(response=response[:32]):
                normalized = normalize_discovery_response(response)
                self.assertEqual(normalized["real_goal"], payload["real_goal"])
                self.assertEqual(normalized["primary_recommendation"], payload["primary_recommendation"])

    def test_11_response_grammar_rejects_ambiguous_or_unsafe_text(self) -> None:
        payload = self._response("https://github.com/example/repo")
        bare = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        invalid_responses = (
            (bare + "\n" + bare, "DISCOVERY_RESPONSE_INVALID"),
            ("not-json", "DISCOVERY_RESPONSE_INVALID"),
            ("```python\n" + bare + "\n```", "DISCOVERY_RESPONSE_INVALID"),
            ("print(\"not prose\")\n" + bare, "DISCOVERY_RESPONSE_INVALID"),
            (json.dumps([payload]), "DISCOVERY_RESPONSE_INVALID"),
            ("false", "DISCOVERY_RESPONSE_INVALID"),
            ("```json\n" + bare + "\n```\n```json\n" + bare + "\n```", "DISCOVERY_RESPONSE_INVALID"),
            ("The local file is C:\\Users\\Alice\\private.json\n" + bare, "DISCOVERY_RESPONSE_INVALID"),
            ("x" * (MAX_RESPONSE_TEXT_LENGTH + 1), "DISCOVERY_RESPONSE_INVALID"),
            (json.dumps({**payload, "search_summary": "Bearer " + ("a" * 24)}), "DISCOVERY_SECRET_REJECTED"),
        )
        for response, code in invalid_responses:
            with self.subTest(response=response[:40]):
                with self.assertRaises(ProjectDiscoveryError) as raised:
                    normalize_discovery_response(response)
                self.assertEqual(raised.exception.code, code)

    def test_12_common_markdown_heading_and_fenced_json_are_accepted(self) -> None:
        payload = self._response("https://github.com/example/repo")
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        responses = (
            "## Project Discovery result\n\nThe bounded recommendation follows:\n\n```json\n"
            + body
            + "\n```\n",
            "### Discovery {result}\n\n```JSON\n"
            + body
            + "\n```\nThe prose boundary is explicit: {candidate} is only a label.\n",
            "### CRLF heading\r\n  ```json   \r\n"
            + body
            + "\r\n  ```  \r\n",
        )
        for response in responses:
            with self.subTest(response=response[:48]):
                normalized = normalize_discovery_response(response)
                self.assertEqual(normalized["real_goal"], payload["real_goal"])
                self.assertEqual(normalized["primary_recommendation"], payload["primary_recommendation"])
        fenced_string_payload = dict(payload)
        fenced_string_payload["search_summary"] = "JSON string may contain ``` without opening another fence"
        fenced_string_body = json.dumps(fenced_string_payload, ensure_ascii=False, indent=2)
        normalized = normalize_discovery_response("```json\n" + fenced_string_body + "\n```")
        self.assertEqual(normalized["search_summary"], fenced_string_payload["search_summary"])

    def test_13_fenced_body_isolated_and_fence_failures_are_rejected(self) -> None:
        payload = self._response("https://github.com/example/repo")
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        invalid_responses = (
            "## Result\n```python\n" + body + "\n```",
            "## Result\n```json\n" + body + "\n```\n```json\n" + body + "\n```",
            "## Result\n```json\n" + body,
            "## Result\n```json\n{\"x\": }\n```",
            "## Result\n```json\n{\"x\": 1, \"x\": 2}\n```",
            "## Result\n```json\n[]\n```",
            "## Result\n```json\nfalse\n```",
            "## Result `inline code`\n```json\n" + body + "\n```",
            "## Result\n```json\n" + body + "\n```\n```",
        )
        for response in invalid_responses:
            with self.subTest(response=response[:48]):
                with self.assertRaises(ProjectDiscoveryError) as raised:
                    normalize_discovery_response(response)
                self.assertEqual(raised.exception.code, "DISCOVERY_RESPONSE_INVALID")

    def test_14_balanced_scanner_handles_nested_objects_and_braces_in_strings(self) -> None:
        payload = self._response("https://github.com/example/repo")
        payload["search_summary"] = "bounded {search} summary with [ordinary] punctuation"
        payload["nested_metadata"] = {
            "object": {"text": "literal braces: { and } and brackets: [ ]", "quoted": "\\\""},
            "items": [{"label": "inner"}],
        }
        bare = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        accepted = (
            bare,
            "Heading\n" + bare + "\nCompleted.",
        )
        for response in accepted:
            with self.subTest(response=response[:48]):
                self.assertEqual(normalize_discovery_response(response)["real_goal"], payload["real_goal"])

        invalid_responses = (
            "Prefix {ordinary} text\n" + bare,
            bare + "\nSuffix [ordinary] text",
            bare + "\n" + bare,
            '{"broken": }',
            '{"unbalanced": 1',
            json.dumps([payload], ensure_ascii=False),
            "false",
        )
        for response in invalid_responses:
            with self.subTest(response=response[:48]):
                with self.assertRaises(ProjectDiscoveryError) as raised:
                    normalize_discovery_response(response)
                self.assertEqual(raised.exception.code, "DISCOVERY_RESPONSE_INVALID")

    def test_15_grammar_failure_branch_is_stable_and_deidentified(self) -> None:
        responses = (
            '{"broken": 1 with private marker alpha-123',
            '{"broken": 1 with private marker beta-456',
        )
        failures = []
        for response in responses:
            with self.assertRaises(ProjectDiscoveryError) as raised:
                normalize_discovery_response(response)
            failures.append(raised.exception)
        self.assertEqual([error.code for error in failures], ["DISCOVERY_RESPONSE_INVALID"] * 2)
        self.assertEqual([getattr(error, "grammar_branch", None) for error in failures], ["OBJECT_UNBALANCED"] * 2)
        self.assertEqual(str(failures[0]), str(failures[1]))
        self.assertNotIn("alpha-123", str(failures[0]))
        self.assertNotIn("beta-456", str(failures[1]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
