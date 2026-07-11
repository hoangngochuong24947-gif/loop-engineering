from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class LoopError(RuntimeError):
    pass


GIT_REPOSITORY_ENVIRONMENT = {
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CONFIG",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    "GIT_DIR",
    "GIT_GRAFT_FILE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_INTERNAL_SUPER_PREFIX",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PREFIX",
    "GIT_REPLACE_REF_BASE",
    "GIT_SHALLOW_FILE",
    "GIT_WORK_TREE",
}


def clean_git_environment() -> dict[str, str]:
    """Return an environment that cannot redirect Git into another repository."""
    environment = os.environ.copy()
    for key in tuple(environment):
        if (
            key in GIT_REPOSITORY_ENVIRONMENT
            or key.startswith("GIT_CONFIG_KEY_")
            or key.startswith("GIT_CONFIG_VALUE_")
        ):
            environment.pop(key, None)
    return environment


@dataclass(frozen=True)
class LoopPaths:
    root: Path

    @property
    def loop_dir(self) -> Path:
        return self.root / "loop"

    @property
    def config(self) -> Path:
        return self.loop_dir / "config.json"

    @property
    def portfolio(self) -> Path:
        return self.loop_dir / "portfolio.json"

    @property
    def products(self) -> Path:
        return self.loop_dir / "products"

    @property
    def tracker(self) -> Path:
        return self.loop_dir / "tracker"

    @property
    def runs(self) -> Path:
        return self.loop_dir / "runs"

    def product(self, product_id: str) -> Path:
        return self.products / f"{product_id}.json"

    def events(self, product_id: str) -> Path:
        return self.tracker / f"{product_id}.jsonl"


def discover_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "loop" / "config.json").is_file():
            return candidate
    raise LoopError("Could not find loop/config.json from the current directory")


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError as exc:
        raise LoopError(f"Missing required file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise LoopError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LoopError(f"Expected a JSON object in {path}")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=False)
            handle.write("\n")
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def load_config(paths: LoopPaths) -> dict[str, Any]:
    config = load_json(paths.config)
    if not config.get("phases") or not config.get("gates"):
        raise LoopError("Loop config must define phases and gates")
    return config


def load_product(paths: LoopPaths, product_id: str) -> dict[str, Any]:
    product = load_json(paths.product(product_id))
    required = ("id", "name", "project", "loop", "commands")
    missing = [field for field in required if field not in product]
    if missing:
        raise LoopError(f"Product {product_id} is missing: {', '.join(missing)}")
    if product["id"] != product_id:
        raise LoopError(
            f"Product manifest id {product['id']!r} does not match {product_id!r}"
        )
    repository = product.get("repository")
    if not isinstance(repository, dict) and not product.get("targetPath"):
        raise LoopError(
            f"Product {product_id} must define repository.path or targetPath"
        )
    if isinstance(repository, dict) and not repository.get("path"):
        raise LoopError(f"Product {product_id} repository must define path")
    return product


def repository_path(paths: LoopPaths, product: dict[str, Any]) -> Path:
    repository = product.get("repository")
    raw_path = repository.get("path") if isinstance(repository, dict) else None
    raw_path = raw_path or product.get("targetPath")
    if not raw_path:
        raise LoopError(f"Product {product.get('id', '<unknown>')} has no repository path")
    candidate = Path(str(raw_path)).expanduser()
    if not candidate.is_absolute():
        candidate = workspace_root(paths) / candidate
    return candidate.resolve()


def product_repository_path(paths: LoopPaths, product_id: str) -> Path:
    return repository_path(paths, load_product(paths, product_id))


def workspace_root(paths: LoopPaths) -> Path:
    common_dir = _git(paths.root, "rev-parse", "--git-common-dir", check=False)
    if not common_dir:
        return paths.root
    common_path = Path(common_dir)
    if not common_path.is_absolute():
        common_path = paths.root / common_path
    resolved = common_path.resolve()
    return resolved.parent if resolved.name == ".git" else paths.root


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
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def repository_state_at(
    repository: Path, repository_config: dict[str, Any] | None = None
) -> dict[str, Any]:
    repository_config = repository_config or {}
    default_branch = str(repository_config.get("defaultBranch", "main"))
    required_local = bool(repository_config.get("requiredLocal", True))
    state: dict[str, Any] = {
        "path": str(repository),
        "url": repository_config.get("url"),
        "defaultBranch": default_branch,
        "requiredLocal": required_local,
        "available": repository.is_dir(),
        "isGitRepository": False,
        "branch": None,
        "head": None,
        "mainHead": None,
        "dirty": None,
    }
    if not repository.is_dir():
        return state
    if _git(repository, "rev-parse", "--is-inside-work-tree", check=False) != "true":
        return state
    state["isGitRepository"] = True
    state["branch"] = _git(repository, "branch", "--show-current", check=False) or None
    state["head"] = _git(repository, "rev-parse", "HEAD", check=False) or None
    state["dirty"] = bool(_git(repository, "status", "--porcelain=v1", check=False))
    for reference in (f"origin/{default_branch}", default_branch):
        value = _git(repository, "rev-parse", reference, check=False)
        if value:
            state["mainHead"] = value
            break
    return state


def repository_state(paths: LoopPaths, product_id: str) -> dict[str, Any]:
    product = load_product(paths, product_id)
    repository = repository_path(paths, product)
    return repository_state_at(repository, product.get("repository", {}))


def score_opportunity(
    opportunity: dict[str, Any], config: dict[str, Any]
) -> float:
    metrics = opportunity.get("metrics", {})
    scoring = config.get("scoring", {})
    if not scoring:
        raise LoopError("Loop config does not define scoring metrics")

    weighted_total = 0.0
    total_weight = 0.0
    for metric_name, rule in scoring.items():
        if metric_name not in metrics:
            raise LoopError(
                f"Opportunity {opportunity.get('id', '<unknown>')} lacks metric {metric_name}"
            )
        raw_value = metrics[metric_name]
        if not isinstance(raw_value, (int, float)) or not 1 <= raw_value <= 5:
            raise LoopError(f"Metric {metric_name} must be between 1 and 5")
        weight = float(rule["weight"])
        normalized = (float(raw_value) - 1.0) / 4.0
        if rule.get("direction") == "lower":
            normalized = 1.0 - normalized
        weighted_total += normalized * weight
        total_weight += weight

    return round(100.0 * weighted_total / total_weight, 1)


def rank_portfolio(paths: LoopPaths) -> list[dict[str, Any]]:
    config = load_config(paths)
    portfolio = load_json(paths.portfolio)
    opportunities = portfolio.get("opportunities")
    if not isinstance(opportunities, list):
        raise LoopError("Portfolio must contain an opportunities array")

    ranked: list[dict[str, Any]] = []
    for opportunity in opportunities:
        if not isinstance(opportunity, dict):
            raise LoopError("Every portfolio opportunity must be an object")
        item = dict(opportunity)
        item["score"] = score_opportunity(item, config)
        ranked.append(item)
    return sorted(ranked, key=lambda item: (-item["score"], item["id"]))


def next_phase(config: dict[str, Any], current_phase: str) -> str | None:
    phases = config["phases"]
    try:
        index = phases.index(current_phase)
    except ValueError as exc:
        raise LoopError(f"Unknown loop phase: {current_phase}") from exc
    if index == len(phases) - 1:
        return None
    return phases[index + 1]
