from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from loop_engineering.claims import (
    claim_issue,
    claim_status,
    close_claim,
    load_claim,
    resolve_claim_repository,
)
from loop_engineering.checker import checker_record, checker_start, merge_readiness
from loop_engineering.builder import (
    resolve_commands,
    run_action,
    run_verification,
    select_verification_profile,
)
from loop_engineering.cli import build_parser, command_doctor, main as cli_main
from loop_engineering.gitops import git_snapshot
from loop_engineering.model import (
    clean_git_environment,
    LoopError,
    LoopPaths,
    product_repository_path,
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
        self.original_environment = os.environ.copy()
        isolated_environment = clean_git_environment()
        os.environ.clear()
        os.environ.update(isolated_environment)
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
        os.environ.clear()
        os.environ.update(self.original_environment)

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

    def run_git(self, arguments: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            env=clean_git_environment(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

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

    def test_repository_portfolio_matches_market_expansion_contract(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        portfolio = json.loads(
            (repository_root / "loop/portfolio.json").read_text(encoding="utf-8")
        )
        opportunities = portfolio["opportunities"]
        expected_ids = {
            "visual-day-planner",
            "focus-intervention",
            "habit-routine",
            "focus-sound",
            "study-deck",
            "student-timetable",
            "private-journal",
            "mood-symptom-log",
            "accessible-reader",
            "pdf-workbench",
            "document-receipt-scanner",
            "voice-memory",
            "medication-log",
            "sleep-log",
            "workout-planner",
            "plant-care",
            "envelope-budget",
            "pantry-meal",
            "recipe-library",
            "natural-language-calendar",
            "private-clipboard",
            "mail-triage",
            "markdown-knowledge-base",
            "mac-storage-cleaner",
            "home-inventory",
            "photo-cleanup",
        }

        self.assertEqual(len(opportunities), 26)
        self.assertEqual({item["id"] for item in opportunities}, expected_ids)
        self.assertEqual(len({item["name"] for item in opportunities}), 26)

        required_fields = {
            "id",
            "name",
            "category",
            "batch",
            "incumbents",
            "pricingPain",
            "wedge",
            "firstSlice",
            "optionalAI",
            "primaryRisk",
            "firstIntents",
            "metrics",
            "references",
        }
        config = json.loads(
            (repository_root / "loop/config.json").read_text(encoding="utf-8")
        )
        for opportunity in opportunities:
            with self.subTest(opportunity=opportunity["id"]):
                self.assertTrue(required_fields.issubset(opportunity))
                self.assertIn(opportunity["batch"], {"A", "B", "C"})
                self.assertIsInstance(opportunity["references"], list)
                self.assertTrue(
                    all(
                        reference.startswith("https://")
                        for reference in opportunity["references"]
                    )
                )
                self.assertTrue(opportunity["pricingPain"]["signal"])
                self.assertTrue(opportunity["pricingPain"]["ourModel"])
                self.assertTrue(opportunity["firstSlice"]["workflow"])
                self.assertTrue(opportunity["optionalAI"]["proposal"])
                self.assertTrue(opportunity["optionalAI"]["fallback"])
                self.assertTrue(opportunity["optionalAI"]["requiresReview"])
                self.assertTrue(opportunity["primaryRisk"]["area"])
                self.assertTrue(opportunity["primaryRisk"]["mitigation"])
                self.assertIsInstance(score_opportunity(opportunity, config), float)

    def test_batch_a_portfolio_slices_are_local_without_account_or_ai(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        portfolio = json.loads(
            (repository_root / "loop/portfolio.json").read_text(encoding="utf-8")
        )
        opportunities = portfolio["opportunities"]
        batch_a = [item for item in opportunities if item["batch"] == "A"]

        self.assertGreaterEqual(len(batch_a), 7)
        for opportunity in batch_a:
            with self.subTest(opportunity=opportunity["id"]):
                first_slice = opportunity["firstSlice"]
                self.assertTrue(first_slice["localFirst"])
                self.assertFalse(first_slice["accountRequired"])
                self.assertFalse(first_slice["aiRequired"])

    def test_legacy_portfolio_directions_are_archived_losslessly(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        portfolio = json.loads(
            (repository_root / "loop/portfolio.json").read_text(encoding="utf-8")
        )
        legacy_ids = {
            "freelance-time",
            "family-operations",
            "personal-crm",
            "knowledge-inbox",
            "subscription-tracker",
            "language-speaking",
            "pet-care",
            "food-log",
            "travel-organizer",
            "wardrobe-memory",
        }
        baseline = json.loads(
            subprocess.run(
                ["git", "show", "2f06178:loop/portfolio.json"],
                cwd=repository_root,
                env=clean_git_environment(),
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout
        )
        expected = {
            item["id"]: item
            for item in baseline["opportunities"]
            if item["id"] in legacy_ids
        }
        archive = portfolio["legacyBacklog"]["archive"]
        config = json.loads(
            (repository_root / "loop/config.json").read_text(encoding="utf-8")
        )

        self.assertEqual({item["id"] for item in archive}, legacy_ids)
        self.assertEqual(set(expected), legacy_ids)
        self.assertTrue(
            legacy_ids.isdisjoint(
                {item["id"] for item in portfolio["opportunities"]}
            )
        )
        for archived in archive:
            with self.subTest(opportunity=archived["id"]):
                self.assertEqual(archived["sourceCommit"], "2f06178")
                self.assertEqual(
                    archived["previousScore"],
                    score_opportunity(expected[archived["id"]], config),
                )
                self.assertEqual(archived["evidenceStatus"], "not-in-current-research")
                self.assertTrue(archived["archiveReason"])
                self.assertTrue(archived["futureReview"])
                original = {
                    key: value
                    for key, value in archived.items()
                    if key
                    not in {
                        "sourceCommit",
                        "previousScore",
                        "evidenceStatus",
                        "archiveReason",
                        "futureReview",
                    }
                }
                self.assertEqual(original, expected[archived["id"]])

    def test_active_references_are_registered_and_unknown_evidence_is_honest(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        portfolio = json.loads(
            (repository_root / "loop/portfolio.json").read_text(encoding="utf-8")
        )
        registry = portfolio["researchEvidence"]["registeredReferences"]
        registered_urls = set(registry.values())
        self.assertEqual(set(registry), {f"S{index}" for index in range(1, 31)})

        for opportunity in portfolio["opportunities"]:
            with self.subTest(opportunity=opportunity["id"]):
                self.assertTrue(set(opportunity["references"]).issubset(registered_urls))
                if not opportunity["references"]:
                    self.assertEqual(
                        opportunity["evidenceStatus"], "needs-primary-source"
                    )
                    self.assertTrue(opportunity["researchSection"])
                    self.assertNotEqual(opportunity["batch"], "A")
                    pricing_signal = opportunity["pricingPain"]["signal"].lower()
                    for unsupported_claim in (
                        "$",
                        "/year",
                        "/month",
                        "subscription",
                        "paywall",
                        "premium",
                    ):
                        self.assertNotIn(unsupported_claim, pricing_signal)

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

        product["commands"] = {"build": [["python3", "-c", "raise SystemExit(1)"]]}
        self.paths.product("external").write_text(json.dumps(product), encoding="utf-8")
        self.assertFalse(run_action(self.paths, "external", "build", execute=True)["success"])
        self.assertFalse(gate_status(self.paths, "external")["ready"])

        product["commands"] = {"build": [["python3", "-c", "print('ok again')"]]}
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

        record_checker(
            self.paths,
            "external",
            issue="1",
            builder="builder-a",
            checker="checker-b",
            verdict="changes-required",
            head=head,
        )
        self.assertFalse(gate_status(self.paths, "external")["ready"])

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
        self.assertTrue(product_status(self.paths, "external")["checkerStale"])
        self.assertFalse(gate_status(self.paths, "external")["ready"])
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
        record_blocker(
            self.paths,
            "external",
            blocker_id="signing",
            category="account",
            summary="Signing expired again",
            user_action_required=True,
        )
        self.assertEqual(len(product_status(self.paths, "external")["openBlockers"]), 1)

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

    def test_issue_claim_creates_clean_worktree_at_exact_base_sha(self) -> None:
        repository = self.make_repository()
        self.write_external_product(repository, phase="build", commands={})
        base_sha = repository_state(self.paths, "external")["head"]

        claim = claim_issue(
            self.paths,
            "external",
            issue="3",
            slug="issue-claim-lifecycle",
            builder="builder-a",
        )

        self.assertEqual(claim["baseSha"], base_sha)
        self.assertEqual(claim["branch"], "agent/3-issue-claim-lifecycle")
        self.assertEqual(claim["status"], "active")
        worktree = Path(claim["worktree"])
        self.assertTrue(worktree.is_dir())
        self.assertEqual(
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree,
                text=True,
                check=True,
                stdout=subprocess.PIPE,
            ).stdout.strip(),
            base_sha,
        )
        self.assertEqual(
            subprocess.run(
                ["git", "status", "--porcelain=v1"],
                cwd=worktree,
                text=True,
                check=True,
                stdout=subprocess.PIPE,
            ).stdout,
            "",
        )
        self.assertEqual(load_claim(self.paths, "external", "3"), claim)

    def test_duplicate_issue_claim_is_rejected_without_replacing_receipt(self) -> None:
        repository = self.make_repository()
        self.write_external_product(repository, phase="build", commands={})
        original = claim_issue(
            self.paths,
            "external",
            issue="3",
            slug="first-slice",
            builder="builder-a",
        )

        with self.assertRaisesRegex(LoopError, "already claimed"):
            claim_issue(
                self.paths,
                "external",
                issue="3",
                slug="second-slice",
                builder="builder-b",
            )

        self.assertEqual(load_claim(self.paths, "external", "3"), original)

    def test_claim_branch_collision_preserves_existing_branch_and_leaves_no_receipt(self) -> None:
        repository = self.make_repository()
        self.write_external_product(repository, phase="build", commands={})
        branch = "agent/3-existing"
        subprocess.run(["git", "branch", branch], cwd=repository, check=True)

        with self.assertRaises(LoopError):
            claim_issue(
                self.paths,
                "external",
                issue="3",
                slug="existing",
                builder="builder-a",
            )

        self.assertEqual(
            subprocess.run(
                ["git", "branch", "--list", branch],
                cwd=repository,
                text=True,
                check=True,
                stdout=subprocess.PIPE,
            ).stdout.strip(),
            branch,
        )
        with self.assertRaisesRegex(LoopError, "not claimed"):
            load_claim(self.paths, "external", "3")

    def test_claim_path_collision_leaves_no_branch_or_receipt(self) -> None:
        repository = self.make_repository()
        self.write_external_product(repository, phase="build", commands={})
        collision = repository.parent / ".worktrees" / repository.name / "3-existing"
        collision.mkdir(parents=True)

        with self.assertRaisesRegex(LoopError, "path already exists"):
            claim_issue(
                self.paths,
                "external",
                issue="3",
                slug="existing",
                builder="builder-a",
            )

        self.assertEqual(
            subprocess.run(
                ["git", "branch", "--list", "agent/3-existing"],
                cwd=repository,
                text=True,
                check=True,
                stdout=subprocess.PIPE,
            ).stdout,
            "",
        )
        with self.assertRaisesRegex(LoopError, "not claimed"):
            load_claim(self.paths, "external", "3")

    def test_dirty_stable_checkout_does_not_block_clean_issue_worktree(self) -> None:
        repository = self.make_repository()
        self.write_external_product(repository, phase="build", commands={})
        (repository / "stable-dirty.txt").write_text("keep me\n", encoding="utf-8")

        claim = claim_issue(
            self.paths,
            "external",
            issue="3",
            slug="isolated",
            builder="builder-a",
        )

        self.assertTrue((repository / "stable-dirty.txt").exists())
        self.assertFalse(claim_status(self.paths, "external", "3")["dirty"])

    def test_claim_uses_configured_worktree_root(self) -> None:
        repository = self.make_repository()
        self.write_external_product(repository, phase="build", commands={})
        config = json.loads(self.paths.config.read_text(encoding="utf-8"))
        config["git"] = {
            "branchPattern": "agent/{issue}-{slug}",
            "worktreeRoot": "custom-worktrees/{repo}",
        }
        self.paths.config.write_text(json.dumps(config), encoding="utf-8")

        claim = claim_issue(
            self.paths,
            "external",
            issue="3",
            slug="configured-root",
            builder="builder-a",
        )

        self.assertEqual(
            Path(claim["worktree"]),
            (
                self.root
                / "custom-worktrees"
                / repository.name
                / "3-configured-root"
            ).resolve(),
        )

    def test_product_relative_path_is_stable_from_tracker_linked_worktree(self) -> None:
        tracker = self.root / "tracker"
        tracker.mkdir()
        product_repository = self.make_repository("product-repository")
        loop_dir = tracker / "loop"
        (loop_dir / "products").mkdir(parents=True)
        (loop_dir / "tracker").mkdir()
        (loop_dir / "config.json").write_text(
            json.dumps({"phases": ["build"], "gates": {"build": []}}),
            encoding="utf-8",
        )
        (loop_dir / "products" / "external.json").write_text(
            json.dumps(
                {
                    "id": "external",
                    "name": "External",
                    "repository": {
                        "path": "../product-repository",
                        "defaultBranch": "main",
                    },
                    "project": {},
                    "loop": {"phase": "build", "cycle": 1},
                    "commands": {},
                }
            ),
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-b", "main"], cwd=tracker, check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "config", "user.name", "Loop Test"], cwd=tracker, check=True)
        subprocess.run(["git", "config", "user.email", "loop@example.com"], cwd=tracker, check=True)
        subprocess.run(["git", "add", "."], cwd=tracker, check=True)
        subprocess.run(["git", "commit", "-m", "tracker"], cwd=tracker, check=True, stdout=subprocess.DEVNULL)
        linked = self.root / ".worktrees" / "tracker" / "test"
        linked.parent.mkdir(parents=True)
        subprocess.run(
            ["git", "worktree", "add", "-b", "agent/test", str(linked), "main"],
            cwd=tracker,
            check=True,
            stdout=subprocess.DEVNULL,
        )

        self.assertEqual(
            product_repository_path(LoopPaths(linked), "external"),
            product_repository.resolve(),
        )

    def test_claim_lock_is_shared_across_tracker_worktrees(self) -> None:
        tracker = self.root / "tracker"
        tracker.mkdir()
        product_repository = self.make_repository("product-repository")
        loop_dir = tracker / "loop"
        (loop_dir / "products").mkdir(parents=True)
        (loop_dir / "tracker").mkdir()
        (loop_dir / "runs").mkdir()
        (loop_dir / "config.json").write_text(
            json.dumps(
                {
                    "phases": ["build"],
                    "gates": {"build": []},
                    "git": {"branchPattern": "agent/{issue}-{slug}"},
                }
            ),
            encoding="utf-8",
        )
        (loop_dir / "portfolio.json").write_text(
            json.dumps({"opportunities": []}), encoding="utf-8"
        )
        (loop_dir / "products" / "external.json").write_text(
            json.dumps(
                {
                    "id": "external",
                    "name": "External",
                    "repository": {
                        "path": str(product_repository),
                        "defaultBranch": "main",
                    },
                    "project": {},
                    "loop": {"phase": "build", "cycle": 1},
                    "commands": {},
                }
            ),
            encoding="utf-8",
        )
        (loop_dir / "tracker" / "external.jsonl").touch()
        environment = clean_git_environment()
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=tracker,
            env=environment,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "config", "user.name", "Loop Test"],
            cwd=tracker,
            env=environment,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "loop@example.com"],
            cwd=tracker,
            env=environment,
            check=True,
        )
        subprocess.run(
            ["git", "add", "."], cwd=tracker, env=environment, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "tracker"],
            cwd=tracker,
            env=environment,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        linked = self.root / ".worktrees" / "tracker" / "builder"
        linked.parent.mkdir(parents=True)
        subprocess.run(
            ["git", "worktree", "add", "-b", "agent/tracker", str(linked), "main"],
            cwd=tracker,
            env=environment,
            check=True,
            stdout=subprocess.DEVNULL,
        )

        claim_issue(
            LoopPaths(tracker),
            "external",
            issue="3",
            slug="one",
            builder="builder-a",
        )
        with self.assertRaisesRegex(LoopError, "already claimed"):
            claim_issue(
                LoopPaths(linked),
                "external",
                issue="3",
                slug="two",
                builder="builder-b",
            )

    def test_legacy_product_rejects_issue_claim_but_keeps_existing_command_resolution(self) -> None:
        self.assertEqual(resolve_commands(self.paths, "sample", "build")[0][1], "Sample")
        with self.assertRaisesRegex(LoopError, "repository manifest"):
            claim_issue(
                self.paths,
                "sample",
                issue="3",
                slug="legacy",
                builder="builder-a",
            )

    def test_issue_claim_rejects_unsafe_issue_and_slug_before_git_changes(self) -> None:
        repository = self.make_repository()
        self.write_external_product(repository, phase="build", commands={})

        for issue, slug in (("../3", "safe"), ("3", "../escape"), ("3", "bad name")):
            with self.subTest(issue=issue, slug=slug):
                with self.assertRaisesRegex(LoopError, "safe Git name"):
                    claim_issue(
                        self.paths,
                        "external",
                        issue=issue,
                        slug=slug,
                        builder="builder-a",
                    )

        with self.assertRaisesRegex(LoopError, "safe Git name"):
            load_claim(self.paths, "external", "../3")

        self.assertEqual(
            subprocess.run(
                ["git", "branch", "--list", "agent/*"],
                cwd=repository,
                text=True,
                check=True,
                stdout=subprocess.PIPE,
            ).stdout,
            "",
        )

    def test_issue_claim_status_and_resolution_detect_builder_and_stale_worktree(self) -> None:
        repository = self.make_repository()
        self.write_external_product(repository, phase="build", commands={})
        claim = claim_issue(
            self.paths,
            "external",
            issue="3",
            slug="claim-status",
            builder="builder-a",
        )

        status = claim_status(self.paths, "external", "3")
        self.assertEqual(status["state"], "active")
        self.assertFalse(status["dirty"])
        self.assertEqual(
            resolve_claim_repository(
                self.paths, "external", "3", builder="builder-a"
            ),
            Path(claim["worktree"]),
        )
        with self.assertRaisesRegex(LoopError, "belongs to builder-a"):
            resolve_claim_repository(
                self.paths, "external", "3", builder="builder-b"
            )

        subprocess.run(
            ["git", "worktree", "remove", claim["worktree"]],
            cwd=repository,
            check=True,
        )
        self.assertEqual(claim_status(self.paths, "external", "3")["state"], "stale")
        replacement = Path(claim["worktree"])
        replacement.mkdir(parents=True)
        subprocess.run(
            ["git", "init", "-b", claim["branch"]],
            cwd=replacement,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        self.assertEqual(claim_status(self.paths, "external", "3")["state"], "stale")
        with self.assertRaisesRegex(LoopError, "stale"):
            resolve_claim_repository(
                self.paths, "external", "3", builder="builder-a"
            )

    def test_issue_close_rejects_dirty_worktree_and_preserves_branch(self) -> None:
        repository = self.make_repository()
        self.write_external_product(repository, phase="build", commands={})
        claim = claim_issue(
            self.paths,
            "external",
            issue="3",
            slug="safe-close",
            builder="builder-a",
        )
        worktree = Path(claim["worktree"])
        dirty_file = worktree / "dirty.txt"
        dirty_file.write_text("not committed\n", encoding="utf-8")

        with self.assertRaisesRegex(LoopError, "dirty"):
            close_claim(
                self.paths,
                "external",
                issue="3",
                builder="builder-a",
                result="merged",
                merge_sha="abc123",
            )
        self.assertTrue(worktree.is_dir())

        dirty_file.unlink()
        closed = close_claim(
            self.paths,
            "external",
            issue="3",
            builder="builder-a",
            result="merged",
            merge_sha="abc123",
        )
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["result"], "merged")
        self.assertFalse(worktree.exists())
        self.assertEqual(claim_status(self.paths, "external", "3")["state"], "closed")
        self.assertEqual(
            subprocess.run(
                ["git", "branch", "--list", claim["branch"]],
                cwd=repository,
                text=True,
                check=True,
                stdout=subprocess.PIPE,
            ).stdout.strip(),
            claim["branch"],
        )

    def test_builder_command_runs_in_claimed_worktree_and_does_not_advance_product_gate(self) -> None:
        repository = self.make_repository()
        self.set_gate("build", ["build_result"])
        self.write_external_product(
            repository,
            phase="build",
            commands={"build": [["python3", "-c", "import os; print(os.getcwd())"]]},
        )
        claim = claim_issue(
            self.paths,
            "external",
            issue="3",
            slug="builder-cwd",
            builder="builder-a",
        )

        result = run_action(
            self.paths,
            "external",
            "build",
            execute=True,
            issue="3",
            builder="builder-a",
        )

        self.assertTrue(result["success"])
        self.assertIn(claim["worktree"], result["steps"][0]["outputTail"])
        self.assertEqual(result["issue"], "3")
        self.assertEqual(result["builder"], "builder-a")
        self.assertEqual(result["worktree"], claim["worktree"])
        self.assertEqual(result["branch"], claim["branch"])
        self.assertFalse(gate_status(self.paths, "external")["ready"])

        with self.assertRaisesRegex(LoopError, "belongs to builder-a"):
            run_action(
                self.paths,
                "external",
                "build",
                execute=True,
                issue="3",
                builder="builder-b",
            )

    def test_hook_git_environment_cannot_redirect_product_operations(self) -> None:
        sentinel = self.make_repository("sentinel")
        repository = self.make_repository("product")
        self.write_external_product(
            repository,
            phase="build",
            commands={"build": [["git", "rev-parse", "--show-toplevel"]]},
        )
        sentinel_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=sentinel,
            env=clean_git_environment(),
            text=True,
            check=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
        os.environ.update(
            {
                "GIT_DIR": str(sentinel / ".git"),
                "GIT_WORK_TREE": str(sentinel),
                "GIT_INDEX_FILE": str(sentinel / ".git" / "index"),
                "GIT_COMMON_DIR": str(sentinel / ".git"),
            }
        )

        claim = claim_issue(
            self.paths,
            "external",
            issue="3",
            slug="hook-isolation",
            builder="builder-a",
        )
        result = run_action(
            self.paths,
            "external",
            "build",
            execute=True,
            issue="3",
            builder="builder-a",
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["steps"][0]["outputTail"].strip(), claim["worktree"])
        clean_environment = clean_git_environment()
        self.assertEqual(
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=sentinel,
                env=clean_environment,
                text=True,
                check=True,
                stdout=subprocess.PIPE,
            ).stdout.strip(),
            sentinel_head,
        )
        self.assertEqual(
            subprocess.run(
                ["git", "branch", "--list", claim["branch"]],
                cwd=sentinel,
                env=clean_environment,
                text=True,
                check=True,
                stdout=subprocess.PIPE,
            ).stdout.strip(),
            "",
        )
        self.assertEqual(
            subprocess.run(
                ["git", "config", "--get", "core.bare"],
                cwd=sentinel,
                env=clean_environment,
                text=True,
                check=True,
                stdout=subprocess.PIPE,
            ).stdout.strip(),
            "false",
        )

    def test_concurrent_issue_runs_use_distinct_durable_receipts(self) -> None:
        repository = self.make_repository()
        self.write_external_product(
            repository,
            phase="build",
            commands={"build": [["python3", "-c", "print('ok')"]]},
        )
        claim_issue(
            self.paths,
            "external",
            issue="1",
            slug="first",
            builder="builder-a",
        )
        claim_issue(
            self.paths,
            "external",
            issue="2",
            slug="second",
            builder="builder-b",
        )

        first = run_action(
            self.paths,
            "external",
            "build",
            execute=True,
            issue="1",
            builder="builder-a",
        )
        second = run_action(
            self.paths,
            "external",
            "build",
            execute=True,
            issue="2",
            builder="builder-b",
        )

        self.assertNotEqual(first["runPath"], second["runPath"])
        first_receipt = json.loads((self.root / first["runPath"]).read_text(encoding="utf-8"))
        second_receipt = json.loads((self.root / second["runPath"]).read_text(encoding="utf-8"))
        self.assertEqual(first_receipt["issue"], "1")
        self.assertEqual(first_receipt["builder"], "builder-a")
        self.assertEqual(second_receipt["issue"], "2")
        self.assertEqual(second_receipt["builder"], "builder-b")

    def test_cli_exposes_issue_lifecycle_and_builder_routing_arguments(self) -> None:
        parser = build_parser()

        claim = parser.parse_args(
            [
                "issue-claim",
                "clearday",
                "--issue",
                "3",
                "--slug",
                "claim-lifecycle",
                "--builder",
                "builder-a",
            ]
        )
        self.assertEqual(claim.issue, "3")
        self.assertEqual(claim.slug, "claim-lifecycle")

        status = parser.parse_args(["issue-status", "clearday", "--issue", "3"])
        self.assertEqual(status.issue, "3")

        close = parser.parse_args(
            [
                "issue-close",
                "clearday",
                "--issue",
                "3",
                "--builder",
                "builder-a",
                "--result",
                "merged",
                "--merge-sha",
                "abc123",
            ]
        )
        self.assertEqual(close.result, "merged")

        build = parser.parse_args(
            ["build", "clearday", "--issue", "3", "--builder", "builder-a"]
        )
        self.assertEqual(build.issue, "3")
        self.assertEqual(build.builder, "builder-a")
        run = parser.parse_args(
            [
                "run",
                "clearday",
                "build",
                "--issue",
                "3",
                "--builder",
                "builder-a",
            ]
        )
        self.assertEqual(run.issue, "3")
        verify = parser.parse_args(
            [
                "verify",
                "clearday",
                "--issue",
                "3",
                "--builder",
                "builder-a",
                "--risk",
                "high",
                "--profile",
                "auto",
            ]
        )
        self.assertEqual(verify.builder, "builder-a")
        self.assertEqual(verify.risk, "high")
        checker_start_args = parser.parse_args(
            [
                "checker-start",
                "clearday",
                "--issue",
                "3",
                "--builder",
                "builder-a",
                "--checker",
                "checker-b",
            ]
        )
        self.assertEqual(checker_start_args.checker, "checker-b")
        ready = parser.parse_args(
            ["ready", "clearday", "--issue", "3", "--risk", "high"]
        )
        self.assertEqual(ready.risk, "high")

    def test_cli_claim_build_status_and_close_run_end_to_end(self) -> None:
        repository = self.make_repository()
        self.write_external_product(
            repository,
            phase="build",
            commands={"build": [["python3", "-c", "import os; print(os.getcwd())"]]},
        )
        previous_directory = Path.cwd()
        try:
            os.chdir(self.root)
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    cli_main(
                        [
                            "issue-claim",
                            "external",
                            "--issue",
                            "3",
                            "--slug",
                            "cli-flow",
                            "--builder",
                            "builder-a",
                        ]
                    ),
                    0,
                )
            claim = json.loads(output.getvalue())

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    cli_main(
                        [
                            "build",
                            "external",
                            "--issue",
                            "3",
                            "--builder",
                            "builder-a",
                            "--execute",
                        ]
                    ),
                    0,
                )
            build = json.loads(output.getvalue())
            self.assertIn(claim["worktree"], build["steps"][0]["outputTail"])

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    cli_main(
                        [
                            "verify",
                            "external",
                            "--issue",
                            "3",
                            "--builder",
                            "builder-a",
                            "--execute",
                        ]
                    ),
                    0,
                )
            verify = json.loads(output.getvalue())
            self.assertEqual(verify["issue"], "3")
            self.assertEqual(verify["builder"], "builder-a")
            self.assertIn(claim["worktree"], verify["results"][0]["worktree"])

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    cli_main(["issue-status", "external", "--issue", "3"]),
                    0,
                )
            self.assertEqual(json.loads(output.getvalue())["state"], "active")

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    cli_main(
                        [
                            "issue-close",
                            "external",
                            "--issue",
                            "3",
                            "--builder",
                            "builder-a",
                            "--result",
                            "abandoned",
                        ]
                    ),
                    0,
                )
            self.assertEqual(json.loads(output.getvalue())["status"], "closed")
        finally:
            os.chdir(previous_directory)

    def test_risk_selected_verification_receipt_binds_current_issue_sha(self) -> None:
        repository = self.make_repository()
        self.write_external_product(
            repository,
            phase="build",
            commands={
                "build": [["python3", "-c", "print('build')"]],
                "test": [["python3", "-c", "print('test')"]],
            },
        )
        claim = claim_issue(
            self.paths,
            "external",
            issue="4",
            slug="risk-verification",
            builder="builder-a",
        )

        self.assertEqual(select_verification_profile("auto", "low"), "focused")
        self.assertEqual(select_verification_profile("auto", "governance"), "full")
        for explicit in ("fast", "focused", "full"):
            self.assertEqual(select_verification_profile(explicit, "high"), explicit)
        focused = run_verification(
            self.paths,
            "external",
            execute=True,
            issue="4",
            builder="builder-a",
            risk="low",
            profile="auto",
        )

        self.assertTrue(focused["success"])
        self.assertEqual(focused["profile"], "focused")
        self.assertEqual([item["action"] for item in focused["results"]], ["test"])
        self.assertEqual(focused["issue"], "4")
        self.assertEqual(focused["builder"], "builder-a")
        self.assertEqual(focused["risk"], "low")
        self.assertEqual(focused["baseSha"], claim["baseSha"])
        self.assertEqual(focused["head"], claim_status(self.paths, "external", "4")["head"])
        self.assertEqual(focused["branch"], claim["branch"])
        self.assertFalse(focused["dirty"])
        self.assertTrue(focused["commands"])
        self.assertTrue(focused["artifactPaths"])

        fast = run_verification(
            self.paths,
            "external",
            execute=True,
            issue="4",
            builder="builder-a",
            risk="low",
            profile="fast",
        )
        self.assertEqual(fast["profile"], "fast")
        self.assertEqual([item["action"] for item in fast["results"]], ["build"])

        full = run_verification(
            self.paths,
            "external",
            execute=True,
            issue="4",
            builder="builder-a",
            risk="release",
            profile="auto",
        )
        self.assertEqual(full["profile"], "full")
        self.assertEqual(
            [item["action"] for item in full["results"]],
            ["build", "test"],
        )

    def test_checker_and_readiness_are_bound_to_current_claim_sha(self) -> None:
        repository = self.make_repository()
        self.write_external_product(
            repository,
            phase="build",
            commands={
                "build": [["python3", "-c", "print('build')"]],
                "test": [["python3", "-c", "print('test')"]],
            },
        )
        claim = claim_issue(
            self.paths,
            "external",
            issue="4",
            slug="checker-ready",
            builder="builder-a",
        )
        worktree = Path(claim["worktree"])
        (worktree / "feature.txt").write_text("feature\n", encoding="utf-8")
        self.run_git(["add", "feature.txt"], cwd=worktree)
        self.run_git(["commit", "-m", "feature"], cwd=worktree)
        head = claim_status(self.paths, "external", "4")["head"]
        run_verification(
            self.paths,
            "external",
            execute=True,
            issue="4",
            builder="builder-a",
            risk="high",
            profile="auto",
        )

        with self.assertRaisesRegex(LoopError, "different identities"):
            checker_start(
                self.paths,
                "external",
                issue="4",
                builder="builder-a",
                checker="builder-a",
                head=head,
            )
        with self.assertRaisesRegex(LoopError, "does not match claimed head"):
            checker_start(
                self.paths,
                "external",
                issue="4",
                builder="builder-a",
                checker="checker-b",
                head=claim["baseSha"],
            )
        contract = checker_start(
            self.paths,
            "external",
            issue="4",
            builder="builder-a",
            checker="checker-b",
            head=head,
        )
        checker_worktree = Path(contract["worktree"])
        self.assertEqual(contract["headSha"], head)
        self.assertTrue(contract["readOnlyIntent"])
        self.assertIsNone(
            self.run_git(
                ["branch", "--show-current"], cwd=checker_worktree
            ).stdout.strip()
            or None
        )
        checker_dirty = checker_worktree / "checker-dirty.txt"
        checker_dirty.write_text("must not change product code\n", encoding="utf-8")
        with self.assertRaisesRegex(LoopError, "clean and read-only"):
            checker_record(
                self.paths,
                "external",
                issue="4",
                builder="builder-a",
                checker="checker-b",
                verdict="pass",
                head=head,
            )
        checker_dirty.unlink()
        checker_record(
            self.paths,
            "external",
            issue="4",
            builder="builder-a",
            checker="checker-b",
            verdict="pass",
            head=head,
        )
        self.assertTrue(merge_readiness(self.paths, "external", "4", risk="high")["ready"])

        product = json.loads(self.paths.product("external").read_text(encoding="utf-8"))
        successful_commands = product["commands"]
        product["commands"] = {
            "build": [["python3", "-c", "raise SystemExit(1)"]],
            "test": [["python3", "-c", "print('test')"]],
        }
        self.paths.product("external").write_text(json.dumps(product), encoding="utf-8")
        failed = run_verification(
            self.paths,
            "external",
            execute=True,
            issue="4",
            builder="builder-a",
            risk="high",
            profile="auto",
        )
        self.assertFalse(failed["success"])
        self.assertIn(
            "verification",
            merge_readiness(self.paths, "external", "4", risk="high")["missing"],
        )
        product["commands"] = successful_commands
        self.paths.product("external").write_text(json.dumps(product), encoding="utf-8")
        run_verification(
            self.paths,
            "external",
            execute=True,
            issue="4",
            builder="builder-a",
            risk="high",
            profile="auto",
        )
        self.assertTrue(merge_readiness(self.paths, "external", "4", risk="high")["ready"])

        (worktree / "later.txt").write_text("later\n", encoding="utf-8")
        self.run_git(["add", "later.txt"], cwd=worktree)
        self.run_git(["commit", "-m", "later"], cwd=worktree)
        stale = merge_readiness(self.paths, "external", "4", risk="high")
        self.assertFalse(stale["ready"])
        self.assertIn("verification", stale["missing"])
        self.assertIn("checker", stale["missing"])

        new_head = claim_status(self.paths, "external", "4")["head"]
        run_verification(
            self.paths,
            "external",
            execute=True,
            issue="4",
            builder="builder-a",
            risk="high",
            profile="auto",
        )
        checker_start(
            self.paths,
            "external",
            issue="4",
            builder="builder-a",
            checker="checker-b",
            head=new_head,
        )
        checker_record(
            self.paths,
            "external",
            issue="4",
            builder="builder-a",
            checker="checker-b",
            verdict="changes-required",
            head=new_head,
        )
        changed = merge_readiness(self.paths, "external", "4", risk="high")
        self.assertFalse(changed["ready"])
        self.assertIn("checker", changed["missing"])


if __name__ == "__main__":
    unittest.main()
