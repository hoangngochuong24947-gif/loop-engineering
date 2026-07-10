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
python3 scripts/loopctl.py git-snapshot clearday --record
python3 scripts/loopctl.py checkpoint clearday --message "first planning slice"
```

Builder commands default to a dry run. `--execute` is required to run external
commands. Checkpoints default to a plan and require `--commit` to create a Git
commit. A checkpoint refuses to commit when unrelated files are already staged.

## Batch product flow

1. Add and score opportunities in the portfolio.
2. Create a product manifest with `new-product`.
3. Record evidence until the phase gate is complete.
4. Advance the product and build one vertical slice.
5. Capture build, test, runtime, and learning events.
6. Create a product-scoped Git checkpoint.
7. Repeat or move the product to release, hold, pivot, or killed status.

