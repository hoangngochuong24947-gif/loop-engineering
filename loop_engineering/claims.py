from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .model import (
    clean_git_environment,
    LoopError,
    LoopPaths,
    load_config,
    load_product,
    repository_path,
    workspace_root,
    write_json,
)
from .tracker import append_event, now_iso


SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _git(repository: Path, *arguments: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        env=clean_git_environment(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        raise LoopError(f"git {' '.join(arguments)} failed in {repository}: {message}")
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _claim_path(paths: LoopPaths, product_id: str, issue: str) -> Path:
    if not SAFE_NAME.fullmatch(product_id) or not SAFE_NAME.fullmatch(issue):
        raise LoopError("Product and Issue must use a safe Git name")
    return (
        workspace_root(paths)
        / "loop"
        / "runs"
        / "claims"
        / product_id
        / f"{issue}.json"
    )


def _worktree_root(
    paths: LoopPaths, product_id: str, repository: Path, config: dict[str, Any]
) -> Path:
    configured = config.get("git", {}).get("worktreeRoot")
    if configured:
        rendered = str(configured).format(repo=repository.name, product=product_id)
        candidate = Path(rendered).expanduser()
        if not candidate.is_absolute():
            candidate = workspace_root(paths) / candidate
        return candidate.resolve()
    return (repository.parent / ".worktrees" / repository.name).resolve()


def _git_common_dir(repository: Path) -> Path | None:
    value = _git(repository, "rev-parse", "--git-common-dir", check=False)
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = repository / path
    return path.resolve()


def load_claim(paths: LoopPaths, product_id: str, issue: str) -> dict[str, Any]:
    path = _claim_path(paths, product_id, issue)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LoopError(f"Issue {issue} is not claimed for product {product_id}") from exc
    except json.JSONDecodeError as exc:
        raise LoopError(f"Invalid claim receipt at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LoopError(f"Claim receipt at {path} is not an object")
    return value


def claim_status(paths: LoopPaths, product_id: str, issue: str) -> dict[str, Any]:
    claim = load_claim(paths, product_id, issue)
    worktree = Path(str(claim.get("worktree", ""))).expanduser()
    status: dict[str, Any] = {
        **claim,
        "state": claim.get("status", "unknown"),
        "available": worktree.is_dir(),
        "branch": None,
        "head": None,
        "dirty": None,
    }
    if claim.get("status") != "active":
        return status
    if not worktree.is_dir() or _git(
        worktree, "rev-parse", "--is-inside-work-tree", check=False
    ) != "true":
        status["state"] = "stale"
        return status
    product = load_product(paths, product_id)
    canonical_repository = repository_path(paths, product)
    if _git_common_dir(worktree) != _git_common_dir(canonical_repository):
        status["state"] = "stale"
        return status
    branch = _git(worktree, "branch", "--show-current", check=False) or None
    head = _git(worktree, "rev-parse", "HEAD", check=False) or None
    dirty = bool(_git(worktree, "status", "--porcelain=v1", check=False))
    status.update({"branch": branch, "head": head, "dirty": dirty})
    if branch != claim.get("branch"):
        status["state"] = "stale"
    return status


def resolve_claim_repository(
    paths: LoopPaths,
    product_id: str,
    issue: str,
    *,
    builder: str | None = None,
) -> Path:
    claim = load_claim(paths, product_id, issue)
    owner = str(claim.get("builder", ""))
    if builder is not None and builder != owner:
        raise LoopError(f"Issue {issue} belongs to {owner}, not {builder}")
    status = claim_status(paths, product_id, issue)
    if status["state"] != "active":
        raise LoopError(f"Issue {issue} claim is {status['state']}")
    return Path(str(claim["worktree"])).resolve()


def close_claim(
    paths: LoopPaths,
    product_id: str,
    *,
    issue: str,
    builder: str,
    result: str,
    merge_sha: str | None = None,
) -> dict[str, Any]:
    if result not in {"merged", "abandoned"}:
        raise LoopError("Claim result must be merged or abandoned")
    if result == "merged" and not merge_sha:
        raise LoopError("Merged claims require a merge SHA")
    claim = load_claim(paths, product_id, issue)
    owner = str(claim.get("builder", ""))
    if builder != owner:
        raise LoopError(f"Issue {issue} belongs to {owner}, not {builder}")
    status = claim_status(paths, product_id, issue)
    if status["state"] != "active":
        raise LoopError(f"Issue {issue} claim is {status['state']}")
    if status["dirty"]:
        raise LoopError(f"Issue {issue} worktree is dirty")

    product = load_product(paths, product_id)
    repository = repository_path(paths, product)
    worktree = Path(str(claim["worktree"])).resolve()
    _git(repository, "worktree", "remove", str(worktree))
    closed = {
        **claim,
        "status": "closed",
        "result": result,
        "mergeSha": merge_sha,
        "closedAt": now_iso(),
    }
    write_json(_claim_path(paths, product_id, issue), closed)
    append_event(
        paths,
        product_id,
        kind="issue_closed",
        summary=f"Issue {issue} claim closed as {result}",
        data={
            "issue": issue,
            "builder": builder,
            "branch": claim.get("branch"),
            "worktree": str(worktree),
            "result": result,
            "mergeSha": merge_sha,
        },
    )
    return closed


def claim_issue(
    paths: LoopPaths,
    product_id: str,
    *,
    issue: str,
    slug: str,
    builder: str,
    base_ref: str | None = None,
) -> dict[str, Any]:
    if not SAFE_NAME.fullmatch(issue) or not SAFE_NAME.fullmatch(slug):
        raise LoopError("Issue and slug must use a safe Git name")
    if not builder.strip():
        raise LoopError("Builder identity must not be empty")
    product = load_product(paths, product_id)
    if not isinstance(product.get("repository"), dict):
        raise LoopError("Issue claims require a product repository manifest")
    repository = repository_path(paths, product)
    if not repository.is_dir():
        raise LoopError(f"Product repository is unavailable: {repository}")
    config = load_config(paths)
    branch_pattern = config.get("git", {}).get("branchPattern", "agent/{issue}-{slug}")
    branch = str(branch_pattern).format(issue=issue, slug=slug, product=product_id)
    if not _git(repository, "check-ref-format", "--branch", branch, check=False):
        raise LoopError(f"Rendered branch is not a safe Git name: {branch}")
    default_branch = str(product["repository"].get("defaultBranch", "main"))
    candidates = [base_ref] if base_ref else [f"origin/{default_branch}", default_branch]
    selected_ref = next(
        (
            candidate
            for candidate in candidates
            if candidate
            and _git(
                repository,
                "rev-parse",
                "--verify",
                f"{candidate}^{{commit}}",
                check=False,
            )
        ),
        None,
    )
    if not selected_ref:
        raise LoopError(f"Could not resolve the product default branch {default_branch}")
    base_sha = _git(repository, "rev-parse", f"{selected_ref}^{{commit}}")
    worktree = _worktree_root(paths, product_id, repository, config) / f"{issue}-{slug}"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if _git(
        repository,
        "show-ref",
        "--verify",
        f"refs/heads/{branch}",
        check=False,
    ):
        raise LoopError(f"Claim branch already exists: {branch}")
    if worktree.exists():
        raise LoopError(f"Claim worktree path already exists: {worktree}")
    claim_path = _claim_path(paths, product_id, issue)
    claim_path.parent.mkdir(parents=True, exist_ok=True)
    claim = {
        "schema": "loop-claim/v1",
        "product": product_id,
        "issue": issue,
        "builder": builder,
        "slug": slug,
        "status": "active",
        "baseRef": selected_ref,
        "baseSha": base_sha,
        "branch": branch,
        "worktree": str(worktree),
        "claimedAt": now_iso(),
    }
    try:
        descriptor = os.open(claim_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise LoopError(f"Issue {issue} is already claimed for product {product_id}") from exc
    worktree_added = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({**claim, "status": "creating"}, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        _git(repository, "worktree", "add", "-b", branch, str(worktree), base_sha)
        worktree_added = True
        write_json(claim_path, claim)
    except Exception:
        if worktree_added:
            _git(repository, "worktree", "remove", str(worktree), check=False)
            _git(repository, "branch", "-D", branch, check=False)
        claim_path.unlink(missing_ok=True)
        raise
    append_event(
        paths,
        product_id,
        kind="issue_claimed",
        summary=f"Issue {issue} claimed by {builder}",
        data={
            "issue": issue,
            "builder": builder,
            "branch": branch,
            "worktree": str(worktree),
            "baseSha": base_sha,
        },
    )
    return claim
