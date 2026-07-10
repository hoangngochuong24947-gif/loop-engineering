from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .model import (
    LoopError,
    LoopPaths,
    load_config,
    load_product,
    next_phase,
    write_json,
)


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
) -> dict[str, Any]:
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
    present = {
        event.get("kind")
        for event in events
        if event.get("phase") == phase and isinstance(event.get("kind"), str)
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
    return {
        "id": product_id,
        "name": product["name"],
        "phase": product["loop"]["phase"],
        "cycle": product["loop"]["cycle"],
        "hypothesis": product["loop"].get("hypothesis", ""),
        "gate": gate_status(paths, product_id),
        "eventCount": len(events),
        "lastEvent": events[-1] if events else None,
    }


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
