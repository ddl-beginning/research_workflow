"""Offline regression for the Step 9 harness bridge seam."""

from __future__ import annotations

import unittest
from typing import Any

from scripts.stage9_real_smoke import invoke_bridge_runner


class Stage9HarnessBridgeTests(unittest.TestCase):
    def test_explicit_bridge_root_is_forwarded_once(self) -> None:
        calls: list[dict[str, Any]] = []
        explicit_root = "D:/offline-bridge"

        def fake_runner(prompt: str, **kwargs: Any) -> dict[str, Any]:
            calls.append({"prompt": prompt, **kwargs})
            return {"ok": True}

        result = invoke_bridge_runner(
            "offline harness invocation",
            runner=fake_runner,
            bridge_root=explicit_root,
            mode="fresh",
        )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["bridge_root"], explicit_root)
        self.assertEqual(list(calls[0]).count("bridge_root"), 1)

    def test_default_bridge_root_is_added_when_omitted(self) -> None:
        calls: list[dict[str, Any]] = []

        def fake_runner(prompt: str, **kwargs: Any) -> dict[str, Any]:
            calls.append({"prompt": prompt, **kwargs})
            return {"ok": True}

        invoke_bridge_runner("offline default-root invocation", runner=fake_runner)

        self.assertEqual(len(calls), 1)
        self.assertIn("bridge_root", calls[0])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
