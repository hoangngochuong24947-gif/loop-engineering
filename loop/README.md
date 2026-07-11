# Loop Engineering

Loop Engineering is the operating system for researching, building, verifying,
and releasing a portfolio of small native apps without losing the evidence that
led to each decision.

## Loop

1. `discover`: collect problem, market, pricing, and user evidence.
2. `decide`: select a direction, success metric, and explicit kill criteria.
3. `design`: define architecture, the smallest vertical slice, and verification.
4. `build`: produce a runnable slice through the product builder.
5. `verify`: run tests and capture runtime proof, not just compiler success.
6. `learn`: record what changed and the next hypothesis.
7. `release`: decide to ship, hold, pivot, or kill.

Every phase is a gate. The tracker is append-only JSONL, while product manifests
store the current phase and reproducible build commands.

## Components

- `loop/portfolio.json`: scored market opportunity inventory.
- `loop/products/*.json`: product manifests and builder commands.
- `loop/tracker/*.jsonl`: append-only evidence and decision log.
- `loop/runs/`: ignored command output and Git snapshots.
- `loop_engineering/`: tracker, builder, scoring, and Git implementation.
- `scripts/loopctl.py`: command-line entry point.

## Common commands

```bash
python3 scripts/loopctl.py doctor
python3 scripts/loopctl.py portfolio
python3 scripts/loopctl.py status clearday
python3 scripts/loopctl.py track clearday --kind architecture --summary "Local task graph and planner service"
python3 scripts/loopctl.py advance clearday
python3 scripts/loopctl.py build clearday
python3 scripts/loopctl.py build clearday --execute
python3 scripts/loopctl.py verify clearday --execute
python3 scripts/loopctl.py runtime-record clearday \
  --actor simulator-agent --summary "Created and saved a plan" \
  --artifact https://example.com/runtime-evidence
python3 scripts/loopctl.py checker-record clearday \
  --issue 12 --builder builder-a --checker checker-b --verdict pass
python3 scripts/loopctl.py release-record clearday \
  --tag v0.1.0-alpha.1 --url https://github.com/OWNER/REPO/releases/tag/v0.1.0-alpha.1
python3 scripts/loopctl.py block clearday \
  --id signing --category account --summary "Device signing unavailable" \
  --user-action-required --fallback "Continue in Simulator"
python3 scripts/loopctl.py git-snapshot clearday --record
```

Builder commands default to a dry run. `--execute` is required to run external
commands. The legacy `loopctl checkpoint` command is retained only for old
single-repository manifests. External product repositories use their own
`scripts/create-checkpoint.sh` after merge readiness, so ordinary commits remain
quiet and checkpoints are annotated, verified milestones.

## Truthful evidence

Product manifests use an external repository registration:

```json
{
  "repository": {
    "path": "../clearday-ios",
    "url": "https://github.com/OWNER/clearday-ios",
    "defaultBranch": "main",
    "requiredLocal": false
  }
}
```

`doctor` reports whether each clone actually exists and is a Git repository.
`status` shows current branch/head, default-branch head, stale Checker evidence,
release drift, open blockers, and whether user action is required.

Build/test/runtime/Checker evidence is reserved for structured commands. It
must pass, come from a clean product worktree, and match the current SHA.
Manual `track --kind test_result` cannot forge a green gate. Release evidence
may remain valid when `main` moves forward; `status` then reports
`releaseBehindMain: true` so the next prerelease decision is explicit.

## Batch product flow

1. Add and score opportunities in the portfolio.
2. Create a product manifest with `new-product`.
3. Record evidence until the phase gate is complete.
4. Advance the product and build one vertical slice.
5. Capture build, test, runtime, and learning events.
6. Run the product repository's explicit verified checkpoint script when the
   slice is a meaningful recovery point.
7. Repeat or move the product to release, hold, pivot, or killed status.
