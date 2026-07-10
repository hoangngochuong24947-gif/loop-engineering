from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from .builder import run_verification
from .model import LoopError, LoopPaths, load_config, load_product
from .tracker import append_event


def _git(paths: LoopPaths, *arguments: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=paths.root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        raise LoopError(f"git {' '.join(arguments)} failed: {message}")
    return completed.stdout.strip()


def git_snapshot(paths: LoopPaths, product_id: str) -> dict[str, Any]:
    product = load_product(paths, product_id)
    status_lines = _git(paths, "status", "--porcelain=v1").splitlines()
    staged_lines = _git(paths, "diff", "--cached", "--name-only").splitlines()
    return {
        "product": product_id,
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "branch": _git(paths, "branch", "--show-current"),
        "head": _git(paths, "rev-parse", "HEAD", check=False) or None,
        "dirty": bool(status_lines),
        "status": status_lines,
        "staged": staged_lines,
        "ownedPaths": product.get("ownedPaths", []),
    }


def record_snapshot(paths: LoopPaths, product_id: str) -> dict[str, Any]:
    snapshot = git_snapshot(paths, product_id)
    paths.runs.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    snapshot_path = paths.runs / f"{product_id}-git-{stamp}.json"
    with snapshot_path.open("w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    relative_path = str(snapshot_path.relative_to(paths.root))
    snapshot["runPath"] = relative_path
    append_event(
        paths,
        product_id,
        kind="git_snapshot",
        summary=f"Captured Git state on {snapshot['branch'] or 'detached HEAD'}",
        data={"runPath": relative_path, "dirty": snapshot["dirty"]},
    )
    return snapshot


def _path_owned(path: str, owned_paths: list[str]) -> bool:
    normalized = Path(path).as_posix().rstrip("/")
    for owned in owned_paths:
        owned_normalized = Path(owned).as_posix().rstrip("/")
        if normalized == owned_normalized or normalized.startswith(owned_normalized + "/"):
            return True
    return False


def checkpoint(
    paths: LoopPaths,
    product_id: str,
    message: str,
    *,
    commit: bool,
    skip_verify: bool,
) -> dict[str, Any]:
    config = load_config(paths)
    product = load_product(paths, product_id)
    owned_paths = list(product.get("ownedPaths", []))
    if not owned_paths:
        raise LoopError(f"Product {product_id} has no ownedPaths")

    snapshot = git_snapshot(paths, product_id)
    unrelated_staged = [
        path for path in snapshot["staged"] if not _path_owned(path, owned_paths)
    ]
    commit_pattern = config.get("git", {}).get(
        "commitPattern", "loop({product}): {message}"
    )
    commit_message = commit_pattern.format(product=product_id, message=message)
    plan = {
        "product": product_id,
        "commit": commit,
        "message": commit_message,
        "ownedPaths": owned_paths,
        "unrelatedStaged": unrelated_staged,
        "verification": None,
    }
    if not commit:
        return plan
    if unrelated_staged:
        raise LoopError(
            "Refusing checkpoint because unrelated files are staged: "
            + ", ".join(unrelated_staged)
        )

    if not skip_verify:
        verification = run_verification(paths, product_id, execute=True)
        plan["verification"] = verification
        if verification["success"] is not True:
            raise LoopError("Refusing checkpoint because verification failed")

    _git(paths, "add", "--", *owned_paths)
    _git(paths, "commit", "-m", commit_message)
    plan["head"] = _git(paths, "rev-parse", "HEAD")
    append_event(
        paths,
        product_id,
        kind="git_checkpoint",
        summary=commit_message,
        data={"head": plan["head"]},
    )
    return plan
