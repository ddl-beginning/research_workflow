"""Low-load and FRESH-isolation assertions for the disposable Step 9 harness."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from scripts.stage9_real_smoke import (
    assert_bounded_pack_receipt,
    assert_low_load_pack_spec,
)
from src.stage_integration import StageIntegrationError


class Step9HarnessGuardTests(unittest.TestCase):
    def test_pack_spec_has_one_evidence_and_no_generated_overflow_files(self):
        pack = {
            "mode": "normal",
            "evidence": [{"sourcePath": ".research/result.txt"}],
        }
        assert_low_load_pack_spec(pack, mode="normal")
        with self.assertRaises(StageIntegrationError) as error:
            assert_low_load_pack_spec(
                {**pack, "evidence": [pack["evidence"][0], {"sourcePath": ".research/other.txt"}]},
                mode="normal",
            )
        self.assertEqual(error.exception.code, "E2E_PACK_NOT_MINIMAL")

    def test_fresh_pack_rejects_previous_recommendation_fields(self):
        with self.assertRaises(StageIntegrationError) as error:
            assert_low_load_pack_spec(
                {
                    "mode": "fresh",
                    "previousRecommendation": "discarded",
                    "evidence": [{"sourcePath": ".research/result.txt"}],
                },
                mode="fresh",
            )
        self.assertEqual(error.exception.code, "E2E_FRESH_ISOLATION_FAILED")

    def test_materialized_pack_is_bounded_to_three_attachments(self):
        handle = SimpleNamespace(receipt={"context_pack": {"attachment_count": 3}})
        assert_bounded_pack_receipt(handle, mode="FRESH")
        too_large = SimpleNamespace(receipt={"context_pack": {"attachment_count": 4}})
        with self.assertRaises(StageIntegrationError) as error:
            assert_bounded_pack_receipt(too_large, mode="NORMAL")
        self.assertEqual(error.exception.code, "E2E_PACK_ATTACHMENT_BOUND_EXCEEDED")


if __name__ == "__main__":
    unittest.main()
