from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from .builder import run_action, run_verification
from .gitops import checkpoint, git_snapshot, record_snapshot
from .model import (
    LoopError,
    LoopPaths,
    discover_root,
    load_config,
    load_json,
    load_product,
    rank_portfolio,
    write_json,
)
from .tracker import advance_product, append_event, product_status, read_events


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _parse_data(items: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise LoopError(f"Data values must use key=value: {item}")
        key, value = item.split("=", 1)
        parsed[key] = value
    return parsed


def _product_ids(paths: LoopPaths) -> list[str]:
    return sorted(path.stem for path in paths.products.glob("*.json"))


def command_doctor(paths: LoopPaths, _: argparse.Namespace) -> int:
    checks: list[dict[str, Any]] = []
    for label, path in (
        ("config", paths.config),
        ("portfolio", paths.portfolio),
        ("products", paths.products),
        ("tracker", paths.tracker),
    ):
        checks.append({"name": label, "ok": path.exists(), "path": str(path)})
    for tool in ("git", "python3", "xcodebuild"):
        checks.append({"name": tool, "ok": shutil.which(tool) is not None})

    errors: list[str] = []
    try:
        load_config(paths)
        ranked = rank_portfolio(paths)
        checks.append({"name": "portfolioScoring", "ok": True, "count": len(ranked)})
    except LoopError as exc:
        errors.append(str(exc))
    for product_id in _product_ids(paths):
        try:
            load_product(paths, product_id)
            read_events(paths, product_id)
        except LoopError as exc:
            errors.append(str(exc))

    result = {"ok": not errors and all(check["ok"] for check in checks), "checks": checks, "errors": errors}
    _print_json(result)
    return 0 if result["ok"] else 1


def command_portfolio(paths: LoopPaths, args: argparse.Namespace) -> int:
    ranked = rank_portfolio(paths)
    if args.json:
        _print_json(ranked)
        return 0
    print(f"{'Score':>5}  {'ID':<28}  Name")
    for opportunity in ranked:
        print(f"{opportunity['score']:>5.1f}  {opportunity['id']:<28}  {opportunity['name']}")
    return 0


def command_status(paths: LoopPaths, args: argparse.Namespace) -> int:
    product_ids = [args.product] if args.product else _product_ids(paths)
    statuses = [product_status(paths, product_id) for product_id in product_ids]
    if args.json:
        _print_json(statuses)
        return 0
    for status in statuses:
        marker = "ready" if status["gate"]["ready"] else "waiting"
        print(
            f"{status['id']}: cycle {status['cycle']} / {status['phase']} / {marker}"
        )
        if status["gate"]["missing"]:
            print("  missing: " + ", ".join(status["gate"]["missing"]))
        print(f"  hypothesis: {status['hypothesis']}")
    return 0


def command_track(paths: LoopPaths, args: argparse.Namespace) -> int:
    event = append_event(
        paths,
        args.product,
        kind=args.kind,
        summary=args.summary,
        phase=args.phase,
        data=_parse_data(args.data),
    )
    _print_json(event)
    return 0


def command_advance(paths: LoopPaths, args: argparse.Namespace) -> int:
    _print_json(advance_product(paths, args.product))
    return 0


def command_run(paths: LoopPaths, args: argparse.Namespace) -> int:
    result = run_action(paths, args.product, args.action, execute=args.execute)
    _print_json(result)
    return 0 if result.get("success") is not False else 1


def command_build(paths: LoopPaths, args: argparse.Namespace) -> int:
    result = run_action(paths, args.product, "build", execute=args.execute)
    _print_json(result)
    return 0 if result.get("success") is not False else 1


def command_verify(paths: LoopPaths, args: argparse.Namespace) -> int:
    result = run_verification(paths, args.product, execute=args.execute)
    _print_json(result)
    return 0 if result.get("success") is not False else 1


def command_git_snapshot(paths: LoopPaths, args: argparse.Namespace) -> int:
    snapshot = record_snapshot(paths, args.product) if args.record else git_snapshot(paths, args.product)
    _print_json(snapshot)
    return 0


def command_checkpoint(paths: LoopPaths, args: argparse.Namespace) -> int:
    result = checkpoint(
        paths,
        args.product,
        args.message,
        commit=args.commit,
        skip_verify=args.skip_verify,
    )
    _print_json(result)
    return 0


def command_new_product(paths: LoopPaths, args: argparse.Namespace) -> int:
    product_path = paths.product(args.id)
    if product_path.exists():
        raise LoopError(f"Product already exists: {args.id}")
    template = load_json(paths.loop_dir / "templates" / "product.json")
    rendered = json.loads(
        json.dumps(template)
        .replace("PRODUCT_ID", args.id)
        .replace("PRODUCT_NAME", args.name)
        .replace("IDEA_ID", args.idea)
    )
    write_json(product_path, rendered)
    paths.events(args.id).parent.mkdir(parents=True, exist_ok=True)
    paths.events(args.id).touch()
    _print_json({"product": args.id, "manifest": str(product_path), "tracker": str(paths.events(args.id))})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Loop Engineering product operator")
    subcommands = parser.add_subparsers(dest="command", required=True)

    doctor = subcommands.add_parser("doctor")
    doctor.set_defaults(handler=command_doctor)

    portfolio = subcommands.add_parser("portfolio")
    portfolio.add_argument("--json", action="store_true")
    portfolio.set_defaults(handler=command_portfolio)

    status = subcommands.add_parser("status")
    status.add_argument("product", nargs="?")
    status.add_argument("--json", action="store_true")
    status.set_defaults(handler=command_status)

    track = subcommands.add_parser("track")
    track.add_argument("product")
    track.add_argument("--kind", required=True)
    track.add_argument("--summary", required=True)
    track.add_argument("--phase")
    track.add_argument("--data", action="append", default=[])
    track.set_defaults(handler=command_track)

    advance = subcommands.add_parser("advance")
    advance.add_argument("product")
    advance.set_defaults(handler=command_advance)

    run = subcommands.add_parser("run")
    run.add_argument("product")
    run.add_argument("action")
    run.add_argument("--execute", action="store_true")
    run.set_defaults(handler=command_run)

    build = subcommands.add_parser("build")
    build.add_argument("product")
    build.add_argument("--execute", action="store_true")
    build.set_defaults(handler=command_build)

    verify = subcommands.add_parser("verify")
    verify.add_argument("product")
    verify.add_argument("--execute", action="store_true")
    verify.set_defaults(handler=command_verify)

    snapshot = subcommands.add_parser("git-snapshot")
    snapshot.add_argument("product")
    snapshot.add_argument("--record", action="store_true")
    snapshot.set_defaults(handler=command_git_snapshot)

    checkpoint_parser = subcommands.add_parser("checkpoint")
    checkpoint_parser.add_argument("product")
    checkpoint_parser.add_argument("--message", required=True)
    checkpoint_parser.add_argument("--commit", action="store_true")
    checkpoint_parser.add_argument("--skip-verify", action="store_true")
    checkpoint_parser.set_defaults(handler=command_checkpoint)

    new_product = subcommands.add_parser("new-product")
    new_product.add_argument("--id", required=True)
    new_product.add_argument("--name", required=True)
    new_product.add_argument("--idea", required=True)
    new_product.set_defaults(handler=command_new_product)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        paths = LoopPaths(discover_root())
        return int(args.handler(paths, args))
    except LoopError as exc:
        print(f"loopctl: {exc}", file=sys.stderr)
        return 2
