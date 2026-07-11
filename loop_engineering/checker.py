from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .builder import select_verification_profile
from .claims import claim_status, load_claim
from .model import (
    clean_git_environment,
    LoopError,
    LoopPaths,
    load_product,
    repository_path,
    repository_state_at,
    workspace_root,
    write_json,
)
from .tracker import (
    append_event,
    now_iso,
    read_events,
    record_checker as record_product_checker,
)


CHECKER_VERDICTS = {"pass", "pass-with-follow-ups", "changes-required", "blocked"}


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        env=clean_git_environment(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        raise LoopError(f"git {' '.join(arguments)} failed in {repository}: {message}")
    return completed.stdout.strip()


def _contract_path(
    paths: LoopPaths, product_id: str, issue: str, head: str
) -> Path:
    return (
        workspace_root(paths)
        / "loop"
        / "runs"
        / "checkers"
        / product_id
        / issue
        / f"{head}.json"
    )


def _load_contract(
    paths: LoopPaths, product_id: str, issue: str, head: str
) -> dict[str, Any]:
    path = _contract_path(paths, product_id, issue, head)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LoopError(f"Checker has not started for issue {issue} at {head}") from exc
    if not isinstance(value, dict):
        raise LoopError(f"Checker contract at {path} is not an object")
    return value


def checker_start(
    paths: LoopPaths,
    product_id: str,
    *,
    issue: str,
    builder: str,
    checker: str,
    head: str | None = None,
) -> dict[str, Any]:
    if builder == checker:
        raise LoopError("Builder and Checker must be different identities")
    claim = load_claim(paths, product_id, issue)
    if claim.get("builder") != builder:
        raise LoopError(f"Issue {issue} belongs to {claim.get('builder')}, not {builder}")
    status = claim_status(paths, product_id, issue)
    if status["state"] != "active":
        raise LoopError(f"Issue {issue} claim is {status['state']}")
    if status["dirty"]:
        raise LoopError("Checker start requires a clean claimed worktree")
    checked_head = head or status["head"]
    if checked_head != status["head"]:
        raise LoopError(
            f"Checker head {checked_head} does not match claimed head {status['head']}"
        )

    product = load_product(paths, product_id)
    repository = repository_path(paths, product)
    checker_worktree = (
        Path(str(claim["worktree"])).parent
        / "checkers"
        / f"{issue}-{checked_head[:12]}"
    )
    path = _contract_path(paths, product_id, issue, checked_head)
    if checker_worktree.exists():
        contract = _load_contract(paths, product_id, issue, checked_head)
        if contract.get("builder") != builder or contract.get("checker") != checker:
            raise LoopError("Checker identities do not match the existing contract")
        state = repository_state_at(checker_worktree, product.get("repository", {}))
        if (
            state.get("head") != checked_head
            or state.get("branch") is not None
            or state.get("dirty") is not False
        ):
            raise LoopError("Existing Checker worktree is not clean at the exact SHA")
        return contract
    checker_worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(repository, "worktree", "add", "--detach", str(checker_worktree), checked_head)

    contract = {
        "schema": "loop-check/v1",
        "product": product_id,
        "issue": issue,
        "builder": builder,
        "checker": checker,
        "baseSha": claim["baseSha"],
        "headSha": checked_head,
        "branch": claim["branch"],
        "worktree": str(checker_worktree),
        "readOnlyIntent": True,
        "verdict": None,
        "startedAt": now_iso(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    contract["reportPath"] = str(path.relative_to(workspace_root(paths)))
    write_json(path, contract)
    return contract


def checker_record(
    paths: LoopPaths,
    product_id: str,
    *,
    issue: str,
    builder: str,
    checker: str,
    verdict: str,
    head: str | None = None,
    pull_request: str | None = None,
    report: str | None = None,
) -> dict[str, Any]:
    if verdict not in CHECKER_VERDICTS:
        raise LoopError(f"Unknown checker verdict: {verdict}")
    if builder == checker:
        raise LoopError("Builder and Checker must be different identities")
    try:
        claim = load_claim(paths, product_id, issue)
    except LoopError as exc:
        if "is not claimed" not in str(exc):
            raise
        return record_product_checker(
            paths,
            product_id,
            issue=issue,
            builder=builder,
            checker=checker,
            verdict=verdict,
            head=head,
            pull_request=pull_request,
            report=report,
        )
    if claim.get("builder") != builder:
        raise LoopError(f"Issue {issue} belongs to {claim.get('builder')}, not {builder}")
    status = claim_status(paths, product_id, issue)
    checked_head = head or status["head"]
    if status["state"] != "active" or checked_head != status["head"]:
        raise LoopError("Checker evidence must match the current claimed SHA")
    contract = _load_contract(paths, product_id, issue, checked_head)
    if contract.get("checker") != checker or contract.get("builder") != builder:
        raise LoopError("Checker identities do not match the started contract")

    product = load_product(paths, product_id)
    checker_worktree = Path(str(contract["worktree"]))
    state = repository_state_at(checker_worktree, product.get("repository", {}))
    if state.get("head") != checked_head:
        raise LoopError("Checker checkout is not at the exact claimed SHA")
    if state.get("branch") is not None:
        raise LoopError("Checker checkout must remain detached")
    if state.get("dirty") is not False:
        raise LoopError("Checker checkout must be clean and read-only")

    event = append_event(
        paths,
        product_id,
        kind="checker_result",
        summary=f"Checker {verdict} for issue {issue}",
        data={
            "issue": issue,
            "builder": builder,
            "checker": checker,
            "pullRequest": pull_request,
            "verdict": verdict,
            "success": verdict == "pass",
            "head": checked_head,
            "branch": claim["branch"],
            "dirty": False,
            "checkerWorktree": str(checker_worktree),
            "contractPath": contract.get("reportPath"),
            "report": report or contract.get("reportPath"),
        },
        trusted=True,
    )
    contract.update(
        {
            "verdict": verdict,
            "recordedAt": now_iso(),
            "pullRequest": pull_request,
            "externalReport": report,
        }
    )
    write_json(_contract_path(paths, product_id, issue, checked_head), contract)
    return event


def checker_cleanup(
    paths: LoopPaths,
    product_id: str,
    *,
    issue: str,
    builder: str,
    checker: str,
    head: str | None = None,
) -> dict[str, Any]:
    claim = load_claim(paths, product_id, issue)
    status = claim_status(paths, product_id, issue)
    checked_head = head or status["head"]
    contract = _load_contract(paths, product_id, issue, checked_head)
    if contract.get("builder") != builder or contract.get("checker") != checker:
        raise LoopError("Checker identities do not match the started contract")
    checker_worktree = Path(str(contract["worktree"]))
    product = load_product(paths, product_id)
    state = repository_state_at(checker_worktree, product.get("repository", {}))
    if state.get("head") != checked_head or state.get("branch") is not None:
        raise LoopError("Checker cleanup requires the exact detached SHA")
    if state.get("dirty") is not False:
        raise LoopError("Checker cleanup requires a clean worktree")
    _git(repository_path(paths, product), "worktree", "remove", str(checker_worktree))
    contract.update(
        {"worktreeRemoved": True, "cleanedAt": now_iso()}
    )
    write_json(_contract_path(paths, product_id, issue, checked_head), contract)
    return contract


def _artifact_exists(paths: LoopPaths, value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = workspace_root(paths) / path
    return path.is_file()


def merge_readiness(
    paths: LoopPaths, product_id: str, issue: str, *, risk: str | None = None
) -> dict[str, Any]:
    claim = load_claim(paths, product_id, issue)
    status = claim_status(paths, product_id, issue)
    events = read_events(paths, product_id)
    claim_risk = str(claim.get("risk", "medium"))
    expected_profile = select_verification_profile("auto", claim_risk)
    missing: list[str] = []
    reasons: dict[str, str] = {}

    if status["state"] != "active" or status["dirty"] is not False:
        missing.append("claim")
        reasons["claim"] = f"claim is {status['state']} or dirty"

    verification = next(
        (
            event
            for event in reversed(events)
            if event.get("kind") == "verification_result"
            and event.get("data", {}).get("issue") == issue
        ),
        None,
    )
    verification_data = verification.get("data", {}) if verification else {}
    verification_valid = (
        verification_data.get("success") is True
        and verification_data.get("head") == status.get("head")
        and verification_data.get("branch") == claim.get("branch")
        and verification_data.get("dirty") is False
        and verification_data.get("builder") == claim.get("builder")
        and verification_data.get("claimRisk", "medium") == claim_risk
        and verification_data.get("profile") in {expected_profile, "full"}
        and _artifact_exists(paths, verification_data.get("receiptPath"))
    )
    if not verification_valid:
        missing.append("verification")
        reasons["verification"] = "missing, failed, stale, or risk/profile mismatched"

    checker = next(
        (
            event
            for event in reversed(events)
            if event.get("kind") == "checker_result"
            and event.get("data", {}).get("issue") == issue
        ),
        None,
    )
    checker_data = checker.get("data", {}) if checker else {}
    checker_valid = (
        checker_data.get("verdict") == "pass"
        and checker_data.get("success") is True
        and checker_data.get("head") == status.get("head")
        and checker_data.get("branch") == claim.get("branch")
        and checker_data.get("dirty") is False
        and checker_data.get("builder") == claim.get("builder")
        and checker_data.get("checker") != claim.get("builder")
        and _artifact_exists(paths, checker_data.get("contractPath"))
    )
    if not checker_valid:
        missing.append("checker")
        reasons["checker"] = "missing, non-pass, dirty, or stale"

    return {
        "schema": "loop-ready/v1",
        "product": product_id,
        "issue": issue,
        "risk": claim_risk,
        "expectedProfile": expected_profile,
        "head": status.get("head"),
        "ready": not missing,
        "missing": missing,
        "reasons": reasons,
        "verification": verification,
        "checker": checker,
    }
