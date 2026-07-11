from __future__ import annotations

import json
import shlex
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .claims import claim_status, load_claim, resolve_claim_repository
from .model import (
    clean_git_environment,
    LoopError,
    LoopPaths,
    load_product,
    repository_path,
    repository_state_at,
    workspace_root,
)
from .tracker import append_event


VERIFICATION_PROFILES = {"fast", "focused", "full", "auto"}
FULL_VERIFICATION_RISKS = {
    "high",
    "data-migration",
    "external-write",
    "external-writes",
    "governance",
    "shared-architecture",
    "release",
}


def select_verification_profile(profile: str, risk: str) -> str:
    if profile not in VERIFICATION_PROFILES:
        raise LoopError(f"Unknown verification profile: {profile}")
    if not risk.strip():
        raise LoopError("Verification risk must not be empty")
    if profile != "auto":
        return profile
    return "full" if risk in FULL_VERIFICATION_RISKS else "focused"


def _verification_actions(product: dict[str, Any], profile: str) -> list[str]:
    available = [
        action for action in ("build", "test") if product["commands"].get(action)
    ]
    if not available:
        return []
    if profile == "full":
        return available
    preferred = "build" if profile == "fast" else "test"
    return [preferred] if preferred in available else [available[0]]


def _command_context(
    paths: LoopPaths, product: dict[str, Any], repository: Path | None = None
) -> dict[str, str]:
    project = product.get("project", {})
    repo_path = repository or repository_path(paths, product)
    repository_config = product.get("repository")
    target_path = product.get("targetPath")
    if target_path:
        target = Path(str(target_path)).expanduser()
        if not target.is_absolute():
            target = workspace_root(paths) / target
        resolved_target = target.resolve()
    else:
        resolved_target = repo_path
    project_value = Path(str(project.get("projectPath", "")))
    project_path = (
        repo_path / project_value
        if isinstance(repository_config, dict)
        else workspace_root(paths) / project_value
    )
    return {
        "root": str(paths.root),
        "repoPath": str(repo_path),
        "targetPath": str(resolved_target),
        "projectPath": str(project_path.resolve()),
        "scheme": str(project.get("scheme", "")),
        "simulatorName": str(project.get("simulatorName", "")),
        "bundleId": str(project.get("bundleId", "")),
    }


def resolve_commands(
    paths: LoopPaths,
    product_id: str,
    action: str,
    *,
    repository: Path | None = None,
) -> list[list[str]]:
    product = load_product(paths, product_id)
    commands = product.get("commands", {}).get(action)
    if not commands:
        raise LoopError(f"Product {product_id} has no {action!r} commands")
    if not isinstance(commands, list):
        raise LoopError(f"Product command {action!r} must be an array")
    context = _command_context(paths, product, repository)
    resolved: list[list[str]] = []
    for command in commands:
        if not isinstance(command, list) or not command:
            raise LoopError(f"Every {action!r} command must be a non-empty array")
        resolved.append([str(token).format(**context) for token in command])
    return resolved


def printable_commands(commands: list[list[str]]) -> list[str]:
    return [shlex.join(command) for command in commands]


def _event_kind(action: str) -> str:
    return {
        "build": "build_result",
        "test": "test_result",
        "verify": "test_result",
    }.get(action, f"{action}_result")


def run_action(
    paths: LoopPaths,
    product_id: str,
    action: str,
    *,
    execute: bool,
    issue: str | None = None,
    builder: str | None = None,
) -> dict[str, Any]:
    if (issue is None) != (builder is None):
        raise LoopError("Issue and Builder must be provided together")
    product = load_product(paths, product_id)
    repository = (
        resolve_claim_repository(paths, product_id, issue, builder=builder)
        if issue is not None
        else repository_path(paths, product)
    )
    commands = resolve_commands(paths, product_id, action, repository=repository)
    result: dict[str, Any] = {
        "product": product_id,
        "action": action,
        "execute": execute,
        "commands": printable_commands(commands),
        "success": None,
        "steps": [],
        "issue": issue,
        "builder": builder,
        "worktree": str(repository) if issue is not None else None,
    }
    if not execute:
        return result

    if not repository.is_dir():
        raise LoopError(f"Product repository is unavailable: {repository}")
    before = repository_state_at(repository, product.get("repository", {}))
    result["repository"] = before
    success = True
    for command in commands:
        completed = subprocess.run(
            command,
            cwd=repository,
            env=clean_git_environment(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        output = completed.stdout or ""
        step = {
            "command": shlex.join(command),
            "returnCode": completed.returncode,
            "outputTail": output[-12000:],
        }
        result["steps"].append(step)
        if completed.returncode != 0:
            success = False
            break

    result["success"] = success
    after = repository_state_at(repository, product.get("repository", {}))
    result["head"] = after["head"]
    result["branch"] = after["branch"]
    result["dirty"] = after["dirty"]
    runs = workspace_root(paths) / "loop" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S-%f")
    scope = f"issue-{issue}" if issue else "product"
    run_path = runs / f"{product_id}-{action}-{scope}-{stamp}-{uuid.uuid4().hex}.json"
    with run_path.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    result["runPath"] = str(run_path.relative_to(workspace_root(paths)))

    append_event(
        paths,
        product_id,
        kind=_event_kind(action),
        summary=f"{action} {'passed' if success else 'failed'}",
        data={
            "runPath": result["runPath"],
            "success": success,
            "action": action,
            "head": result["head"],
            "branch": result["branch"],
            "dirty": result["dirty"],
            "issue": issue,
            "builder": builder,
            "worktree": result["worktree"],
        },
        trusted=True,
    )
    return result


def run_verification(
    paths: LoopPaths,
    product_id: str,
    *,
    execute: bool,
    issue: str | None = None,
    builder: str | None = None,
    risk: str = "low",
    profile: str = "auto",
) -> dict[str, Any]:
    product = load_product(paths, product_id)
    selected_profile = (
        select_verification_profile(profile, risk) if issue is not None else "full"
    )
    actions = _verification_actions(product, selected_profile)
    if not actions:
        raise LoopError(f"Product {product_id} has no build or test commands")
    results = [
        run_action(
            paths,
            product_id,
            action,
            execute=execute,
            issue=issue,
            builder=builder,
        )
        for action in actions
    ]
    success_values = [result["success"] for result in results]
    success = None if not execute else all(value is True for value in success_values)
    receipt: dict[str, Any] = {
        "schema": "loop-verification/v1",
        "product": product_id,
        "execute": execute,
        "issue": issue,
        "builder": builder,
        "risk": risk if issue is not None else None,
        "requestedProfile": profile if issue is not None else None,
        "profile": selected_profile,
        "success": success,
        "results": results,
    }
    if issue is None:
        return receipt

    claim = load_claim(paths, product_id, issue)
    status = claim_status(paths, product_id, issue)
    receipt.update(
        {
            "baseSha": claim["baseSha"],
            "head": status["head"],
            "branch": status["branch"],
            "dirty": status["dirty"],
            "worktree": claim["worktree"],
            "commands": [
                command
                for result in results
                for command in result.get("commands", [])
            ],
            "artifactPaths": [
                result["runPath"] for result in results if result.get("runPath")
            ],
        }
    )
    if not execute:
        return receipt

    runs = workspace_root(paths) / "loop" / "runs" / "verification"
    runs.mkdir(parents=True, exist_ok=True)
    receipt_path = runs / (
        f"{product_id}-issue-{issue}-{status['head'][:12]}-"
        f"{uuid.uuid4().hex}.json"
    )
    with receipt_path.open("x", encoding="utf-8") as handle:
        json.dump(receipt, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    receipt["receiptPath"] = str(receipt_path.relative_to(workspace_root(paths)))
    append_event(
        paths,
        product_id,
        kind="verification_result",
        summary=f"{selected_profile} verification {'passed' if success else 'failed'} for issue {issue}",
        data={key: value for key, value in receipt.items() if key != "results"},
        trusted=True,
    )
    return receipt
