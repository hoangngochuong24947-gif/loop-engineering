from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from loop_engineering.builder import resolve_commands
from loop_engineering.model import LoopError, LoopPaths, rank_portfolio, score_opportunity
from loop_engineering.tracker import advance_product, append_event, gate_status, read_events


class LoopEngineeringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.paths = LoopPaths(self.root)
        self.paths.products.mkdir(parents=True)
        self.paths.tracker.mkdir(parents=True)
        config = {
            "phases": ["discover", "decide"],
            "gates": {"discover": ["problem_statement"], "decide": ["selected_direction"]},
            "scoring": {
                "pain": {"weight": 1, "direction": "higher"},
                "risk": {"weight": 1, "direction": "lower"},
            },
        }
        self.paths.config.parent.mkdir(parents=True, exist_ok=True)
        self.paths.config.write_text(json.dumps(config), encoding="utf-8")
        self.paths.portfolio.write_text(
            json.dumps(
                {
                    "opportunities": [
                        {"id": "strong", "name": "Strong", "metrics": {"pain": 5, "risk": 1}},
                        {"id": "weak", "name": "Weak", "metrics": {"pain": 1, "risk": 5}},
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.product = {
            "id": "sample",
            "name": "Sample",
            "targetPath": "targets/Sample",
            "project": {
                "projectPath": "targets/Sample/Sample.xcodeproj",
                "scheme": "Sample",
                "simulatorName": "iPhone 17",
                "bundleId": "com.example.Sample",
            },
            "loop": {"phase": "discover", "cycle": 1},
            "commands": {"build": [["echo", "{scheme}", "{targetPath}"]]},
        }
        self.paths.product("sample").write_text(json.dumps(self.product), encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_score_inverts_risk(self) -> None:
        config = json.loads(self.paths.config.read_text(encoding="utf-8"))
        self.assertEqual(
            score_opportunity({"id": "x", "metrics": {"pain": 5, "risk": 1}}, config),
            100.0,
        )
        self.assertEqual(
            score_opportunity({"id": "x", "metrics": {"pain": 1, "risk": 5}}, config),
            0.0,
        )

    def test_portfolio_is_ranked(self) -> None:
        ranked = rank_portfolio(self.paths)
        self.assertEqual([item["id"] for item in ranked], ["strong", "weak"])

    def test_tracker_gate_and_advance(self) -> None:
        self.assertFalse(gate_status(self.paths, "sample")["ready"])
        append_event(
            self.paths,
            "sample",
            kind="problem_statement",
            summary="A real problem",
        )
        self.assertTrue(gate_status(self.paths, "sample")["ready"])
        status = advance_product(self.paths, "sample")
        self.assertEqual(status["phase"], "decide")
        self.assertEqual(read_events(self.paths, "sample")[-1]["kind"], "phase_advanced")

    def test_advance_rejects_missing_gate(self) -> None:
        with self.assertRaises(LoopError):
            advance_product(self.paths, "sample")

    def test_builder_resolves_product_tokens(self) -> None:
        command = resolve_commands(self.paths, "sample", "build")[0]
        self.assertEqual(command[0:2], ["echo", "Sample"])
        self.assertEqual(command[2], str(self.root / "targets/Sample"))


if __name__ == "__main__":
    unittest.main()
