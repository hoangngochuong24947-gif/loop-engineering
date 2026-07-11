from __future__ import annotations

import json
import subprocess
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .model import (
    LoopError,
    LoopPaths,
    load_config,
    load_product,
    next_phase,
    repository_path,
    repository_state,
    write_json,
)


RESERVED_EVIDENCE_KINDS = {
    "build_result",
    "test_result",
    "runtime_proof",
    "checker_result",
    "release_result",
}


def now_iso() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def read_events(paths: LoopPaths, product_id: str) -> list[dict[str, Any]]:
    event_path = paths.events(product_id)
    if not event_path.exists():
        return []
    events: list[dict[str, Any]] = []
    with event_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LoopError(
                    f"Invalid tracker event at {event_path}:{line_number}: {exc}"
                ) from exc
            if not isinstance(event, dict):
                raise LoopError(
                    f"Tracker event at {event_path}:{line_number} is not an object"
                )
            events.append(event)
    return events


def append_event(
    paths: LoopPaths,
    product_id: str,
    *,
    kind: str,
    summary: str,
    phase: str | None = None,
    data: dict[str, Any] | None = None,
    trusted: bool = False,
) -> dict[str, Any]:
    if kind in RESERVED_EVIDENCE_KINDS and not trusted:
        raise LoopError(
            f"{kind} is reserved for structured evidence commands and cannot be added manually"
        )
    product = load_product(paths, product_id)
    active_phase = phase or product["loop"]["phase"]
    event = {
        "timestamp": now_iso(),
        "product": product_id,
        "phase": active_phase,
        "kind": kind,
        "summary": summary,
        "data": data or {},
    }
    event_path = paths.events(product_id)
    event_path.parent.mkdir(parents=True, exist_ok=True)
    with event_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
        handle.write("\n")
    return event


def gate_status(paths: LoopPaths, product_id: str) -> dict[str, Any]:
    config = load_config(paths)
    product = load_product(paths, product_id)
    phase = product["loop"]["phase"]
    required = list(config["gates"].get(phase, []))
    events = read_events(paths, product_id)
    state = repository_state(paths, product_id)

    def valid(event: dict[str, Any]) -> bool:
        kind = event.get("kind")
        if event.get("phase") != phase or not isinstance(kind, str):
            return False
        if kind not in RESERVED_EVIDENCE_KINDS:
            return True
        data = event.get("data")
        base_valid = (
            isinstance(data, dict)
            and data.get("success") is True
            and data.get("dirty") is False
        )
        if not base_valid:
            return False
        if kind == "release_result":
            return bool(data.get("head"))
        return (
            state.get("dirty") is False
            and bool(state.get("head"))
            and data.get("head") == state.get("head")
        )

    present = {
        event.get("kind")
        for event in events
        if event.get("kind") in required and valid(event)
    }
    missing = [kind for kind in required if kind not in present]
    return {
        "phase": phase,
        "required": required,
        "present": sorted(present),
        "missing": missing,
        "ready": not missing,
    }


def product_status(paths: LoopPaths, product_id: str) -> dict[str, Any]:
    product = load_product(paths, product_id)
    events = read_events(paths, product_id)
    repository = repository_state(paths, product_id)
    latest_checker = next(
        (event for event in reversed(events) if event.get("kind") == "checker_result"),
        None,
    )
    latest_release = next(
        (event for event in reversed(events) if event.get("kind") == "release_result"),
        None,
    )
    resolved_blockers = {
        event.get("data", {}).get("blockerId")
        for event in events
        if event.get("kind") == "blocker_resolved"
    }
    blockers = [
        event
        for event in events
        if event.get("kind") == "blocker"
        and event.get("data", {}).get("blockerId") not in resolved_blockers
    ]
    checker_data = latest_checker.get("data", {}) if latest_checker else {}
    release_data = latest_release.get("data", {}) if latest_release else {}
    return {
        "id": product_id,
        "name": product["name"],
        "phase": product["loop"]["phase"],
        "cycle": product["loop"]["cycle"],
        "hypothesis": product["loop"].get("hypothesis", ""),
        "gate": gate_status(paths, product_id),
        "eventCount": len(events),
        "lastEvent": events[-1] if events else None,
        "repository": repository,
        "latestChecker": latest_checker,
        "checkerStale": bool(latest_checker)
        and (
            checker_data.get("head") != repository.get("head")
            or repository.get("dirty") is not False
        ),
        "latestRelease": latest_release,
        "releaseBehindMain": bool(latest_release)
        and bool(repository.get("mainHead"))
        and release_data.get("head") != repository.get("mainHead"),
        "openBlockers": blockers,
        "userActionRequired": any(
            event.get("data", {}).get("userActionRequired") is True
            for event in blockers
        ),
    }


def _git(repository: Any, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        raise LoopError(f"git {' '.join(arguments)} failed: {message}")
    return completed.stdout.strip()


def record_checker(
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
    allowed = {"pass", "pass-with-follow-ups", "changes-required", "blocked"}
    if verdict not in allowed:
        raise LoopError(f"Unknown checker verdict: {verdict}")
    if builder == checker:
        raise LoopError("Builder and Checker must be different identities")
    state = repository_state(paths, product_id)
    if not state.get("head"):
        raise LoopError(f"Product repository is unavailable: {state['path']}")
    if state.get("dirty"):
        raise LoopError("Checker evidence requires a clean product worktree")
    checked_head = head or state["head"]
    if checked_head != state["head"]:
        raise LoopError(
            f"Checker head {checked_head} does not match current head {state['head']}"
        )
    success = verdict in {"pass", "pass-with-follow-ups"}
    return append_event(
        paths,
        product_id,
        kind="checker_result",
        summary=f"Checker {verdict} for issue {issue}",
        data={
            "issue": issue,
            "pullRequest": pull_request,
            "builder": builder,
            "checker": checker,
            "verdict": verdict,
            "success": success,
            "head": checked_head,
            "branch": state.get("branch"),
            "dirty": False,
            "report": report,
        },
        trusted=True,
    )


def record_runtime_proof(
    paths: LoopPaths,
    product_id: str,
    *,
    actor: str,
    summary: str,
    artifact: str,
) -> dict[str, Any]:
    state = repository_state(paths, product_id)
    if not state.get("head"):
        raise LoopError(f"Product repository is unavailable: {state['path']}")
    if state.get("dirty"):
        raise LoopError("Runtime evidence requires a clean product worktree")
    return append_event(
        paths,
        product_id,
        kind="runtime_proof",
        summary=summary,
        data={
            "actor": actor,
            "artifact": artifact,
            "success": True,
            "head": state["head"],
            "branch": state.get("branch"),
            "dirty": False,
        },
        trusted=True,
    )


def record_release(
    paths: LoopPaths,
    product_id: str,
    *,
    tag: str,
    url: str,
    stage_report: str | None = None,
) -> dict[str, Any]:
    product = load_product(paths, product_id)
    repository = repository_path(paths, product)
    state = repository_state(paths, product_id)
    if not state.get("isGitRepository"):
        raise LoopError(f"Product repository is unavailable: {repository}")
    if state.get("dirty"):
        raise LoopError("Release evidence requires a clean product worktree")
    release_head = _git(repository, "rev-parse", f"{tag}^{{commit}}")
    main_head = state.get("mainHead")
    if not main_head:
        raise LoopError("Could not resolve the product default branch")
    completed = subprocess.run(
        ["git", "merge-base", "--is-ancestor", release_head, main_head],
        cwd=repository,
        check=False,
    )
    if completed.returncode != 0:
        raise LoopError(f"Release tag {tag} is not reachable from the default branch")
    return append_event(
        paths,
        product_id,
        kind="release_result",
        summary=f"Recorded user-testable release {tag}",
        data={
            "tag": tag,
            "url": url,
            "stageReport": stage_report,
            "head": release_head,
            "mainHead": main_head,
            "success": True,
            "dirty": False,
        },
        trusted=True,
    )


def record_blocker(
    paths: LoopPaths,
    product_id: str,
    *,
    blocker_id: str,
    category: str,
    summary: str,
    user_action_required: bool,
    cost: str | None = None,
    fallback: str | None = None,
) -> dict[str, Any]:
    return append_event(
        paths,
        product_id,
        kind="blocker",
        summary=summary,
        data={
            "blockerId": blocker_id,
            "category": category,
            "userActionRequired": user_action_required,
            "cost": cost,
            "fallback": fallback,
        },
    )


def resolve_blocker(
    paths: LoopPaths, product_id: str, *, blocker_id: str, summary: str
) -> dict[str, Any]:
    return append_event(
        paths,
        product_id,
        kind="blocker_resolved",
        summary=summary,
        data={"blockerId": blocker_id},
    )


def advance_product(paths: LoopPaths, product_id: str) -> dict[str, Any]:
    config = load_config(paths)
    product = load_product(paths, product_id)
    gate = gate_status(paths, product_id)
    if not gate["ready"]:
        raise LoopError(
            f"Cannot advance {product_id}; missing gate evidence: "
            + ", ".join(gate["missing"])
        )

    current_phase = product["loop"]["phase"]
    following_phase = next_phase(config, current_phase)
    if following_phase is None:
        raise LoopError(
            f"Product {product_id} is at release; record a release decision first"
        )

    history = product["loop"].setdefault("history", [])
    history.append(
        {
            "phase": current_phase,
            "completedAt": now_iso(),
            "cycle": product["loop"]["cycle"],
        }
    )
    product["loop"]["phase"] = following_phase
    product["loop"]["updatedAt"] = now_iso()
    write_json(paths.product(product_id), product)
    append_event(
        paths,
        product_id,
        kind="phase_advanced",
        summary=f"Advanced from {current_phase} to {following_phase}",
        phase=following_phase,
        data={"from": current_phase, "to": following_phase},
    )
    return product_status(paths, product_id)
