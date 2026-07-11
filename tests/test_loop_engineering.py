from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from loop_engineering.builder import resolve_commands, run_action
from loop_engineering.cli import command_doctor
from loop_engineering.gitops import git_snapshot
from loop_engineering.model import (
    LoopError,
    LoopPaths,
    rank_portfolio,
    repository_state,
    score_opportunity,
)
from loop_engineering.tracker import (
    advance_product,
    append_event,
    gate_status,
    product_status,
    read_events,
    record_blocker,
    record_checker,
    record_release,
    record_runtime_proof,
    resolve_blocker,
)


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

    def make_repository(self, name: str = "repo") -> Path:
        repository = self.root / name
        repository.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "config", "user.name", "Loop Test"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.email", "loop@example.com"], cwd=repository, check=True)
        (repository / "README.md").write_text("test\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=repository, check=True, stdout=subprocess.DEVNULL)
        return repository

    def write_external_product(
        self,
        repository: Path,
        *,
        phase: str,
        commands: dict[str, list[list[str]]],
        required_local: bool = True,
    ) -> None:
        product = {
            "id": "external",
            "name": "External",
            "repository": {
                "path": str(repository),
                "url": "https://example.com/external.git",
                "defaultBranch": "main",
                "requiredLocal": required_local,
            },
            "project": {"scheme": "External"},
            "loop": {"phase": phase, "cycle": 1},
            "commands": commands,
        }
        self.paths.product("external").write_text(json.dumps(product), encoding="utf-8")
        self.paths.events("external").touch()

    def set_gate(self, phase: str, required: list[str]) -> None:
        config = json.loads(self.paths.config.read_text(encoding="utf-8"))
        config["phases"] = [phase]
        config["gates"] = {phase: required}
        self.paths.config.write_text(json.dumps(config), encoding="utf-8")

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
        self.assertEqual(command[2], str((self.root / "targets/Sample").resolve()))

    def test_external_repository_commands_capture_sha_bound_success(self) -> None:
        repository = self.make_repository()
        self.set_gate("build", ["build_result"])
        self.write_external_product(
            repository,
            phase="build",
            commands={"build": [["python3", "-c", "import os; print(os.getcwd())"]]},
        )

        result = run_action(self.paths, "external", "build", execute=True)

        self.assertTrue(result["success"])
        self.assertIn(str(repository), result["steps"][0]["outputTail"])
        self.assertEqual(result["head"], repository_state(self.paths, "external")["head"])
        self.assertTrue(gate_status(self.paths, "external")["ready"])
        snapshot = git_snapshot(self.paths, "external")
        self.assertEqual(snapshot["repositoryPath"], str(repository.resolve()))
        self.assertEqual(snapshot["head"], result["head"])

    def test_failed_or_stale_evidence_never_satisfies_gate(self) -> None:
        repository = self.make_repository()
        self.set_gate("build", ["build_result"])
        self.write_external_product(
            repository,
            phase="build",
            commands={"build": [["python3", "-c", "raise SystemExit(1)"]]},
        )
        self.assertFalse(run_action(self.paths, "external", "build", execute=True)["success"])
        self.assertFalse(gate_status(self.paths, "external")["ready"])

        product = json.loads(self.paths.product("external").read_text(encoding="utf-8"))
        product["commands"] = {"build": [["python3", "-c", "print('ok')"]]}
        self.paths.product("external").write_text(json.dumps(product), encoding="utf-8")
        self.assertTrue(run_action(self.paths, "external", "build", execute=True)["success"])
        self.assertTrue(gate_status(self.paths, "external")["ready"])

        (repository / "change.txt").write_text("new head\n", encoding="utf-8")
        subprocess.run(["git", "add", "change.txt"], cwd=repository, check=True)
        subprocess.run(["git", "commit", "-m", "new head"], cwd=repository, check=True, stdout=subprocess.DEVNULL)
        self.assertFalse(gate_status(self.paths, "external")["ready"])

    def test_manual_reserved_evidence_is_rejected(self) -> None:
        with self.assertRaises(LoopError):
            append_event(
                self.paths,
                "sample",
                kind="test_result",
                summary="pretend pass",
                data={"success": True, "head": "fake"},
            )

    def test_checker_must_be_independent_and_current(self) -> None:
        repository = self.make_repository()
        self.set_gate("verify", ["checker_result"])
        self.write_external_product(repository, phase="verify", commands={})
        head = repository_state(self.paths, "external")["head"]

        with self.assertRaises(LoopError):
            record_checker(
                self.paths,
                "external",
                issue="1",
                builder="same-agent",
                checker="same-agent",
                verdict="pass",
            )

        record_checker(
            self.paths,
            "external",
            issue="1",
            builder="builder-a",
            checker="checker-b",
            verdict="pass",
            head=head,
        )
        self.assertTrue(gate_status(self.paths, "external")["ready"])

        (repository / "after-check.txt").write_text("stale\n", encoding="utf-8")
        subprocess.run(["git", "add", "after-check.txt"], cwd=repository, check=True)
        subprocess.run(["git", "commit", "-m", "after check"], cwd=repository, check=True, stdout=subprocess.DEVNULL)
        status = product_status(self.paths, "external")
        self.assertTrue(status["checkerStale"])
        self.assertFalse(status["gate"]["ready"])

    def test_runtime_proof_is_bound_to_clean_current_head(self) -> None:
        repository = self.make_repository()
        self.set_gate("verify", ["runtime_proof"])
        self.write_external_product(repository, phase="verify", commands={})

        record_runtime_proof(
            self.paths,
            "external",
            actor="runtime-agent",
            summary="Core flow completed",
            artifact="screenshots/core-flow.png",
        )
        self.assertTrue(gate_status(self.paths, "external")["ready"])

        (repository / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
        self.assertFalse(gate_status(self.paths, "external")["ready"])

    def test_release_and_blocker_status_are_structured(self) -> None:
        repository = self.make_repository()
        self.set_gate("release", ["release_result"])
        self.write_external_product(repository, phase="release", commands={})
        subprocess.run(["git", "tag", "-a", "v0.1.0-alpha.1", "-m", "alpha"], cwd=repository, check=True)

        release = record_release(
            self.paths,
            "external",
            tag="v0.1.0-alpha.1",
            url="https://example.com/releases/alpha",
        )
        self.assertTrue(release["data"]["success"])
        self.assertTrue(gate_status(self.paths, "external")["ready"])

        (repository / "post-release.txt").write_text("ops-only\n", encoding="utf-8")
        subprocess.run(["git", "add", "post-release.txt"], cwd=repository, check=True)
        subprocess.run(["git", "commit", "-m", "post release"], cwd=repository, check=True, stdout=subprocess.DEVNULL)
        self.assertTrue(gate_status(self.paths, "external")["ready"])
        self.assertTrue(product_status(self.paths, "external")["releaseBehindMain"])

        record_blocker(
            self.paths,
            "external",
            blocker_id="signing",
            category="account",
            summary="Physical-device signing is unavailable",
            user_action_required=True,
            fallback="Continue in Simulator",
        )
        self.assertTrue(product_status(self.paths, "external")["userActionRequired"])
        resolve_blocker(
            self.paths,
            "external",
            blocker_id="signing",
            summary="Signing configured",
        )
        self.assertFalse(product_status(self.paths, "external")["openBlockers"])

    def test_doctor_fails_for_required_missing_repository(self) -> None:
        missing = self.root / "missing-repository"
        self.write_external_product(
            missing,
            phase="discover",
            commands={},
            required_local=True,
        )
        output = StringIO()
        with redirect_stdout(output):
            exit_code = command_doctor(self.paths, Namespace())
        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertFalse(result["ok"])
        self.assertIn("repository is unavailable", result["errors"][0])


if __name__ == "__main__":
    unittest.main()
