# Loop Engineering

Loop Engineering is a small, dependency-free operating system for running a
portfolio of software products through evidence-driven loops.

It combines:

- a scored market opportunity portfolio;
- phase gates from discovery through release;
- append-only JSONL tracking;
- external product-repository registration and live Git state;
- reproducible product builder commands;
- build, test, runtime, Checker, release, and blocker evidence bound to Git SHAs;
- Git snapshots and guarded product-scoped checkpoints;
- checkpoint tags and non-destructive rollback branches.

## Install

```bash
python3 -m pip install -e .
loopctl doctor
```

The repository also includes a no-install entry point:

```bash
python3 scripts/loopctl.py portfolio
```

## Product loop

1. Discover a painful, frequent problem and collect market/pricing evidence.
2. Decide with a success metric and explicit kill criteria.
3. Design one vertical slice and its verification plan.
4. Build through reproducible commands in a product manifest.
5. Verify with tests and runtime proof.
6. Learn and record the next hypothesis.
7. Release, hold, pivot, or kill.

See [loop/README.md](loop/README.md) for commands and file layout.

The tracker repository does not contain product source. Each product manifest
registers its own clone path, public repository URL, and default branch. Command
execution occurs inside that product repository, and failed, dirty, or stale
evidence cannot satisfy a phase gate.

## Tests

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 scripts/loopctl.py doctor
```

## Rollback safety

Install the repository hooks:

```bash
./scripts/install-hooks.sh
```

Ordinary commits do not create tags. Create a checkpoint only for a clean,
pushed milestone; verification runs before an annotated tag is pushed:

```bash
./scripts/create-checkpoint.sh truthful-tracker
```

To inspect or continue from a checkpoint without rewriting history or changing
the active worktree:

```bash
./scripts/restore-checkpoint.sh checkpoint/20260710-223000-abc1234
```

The restore command creates a new branch in an isolated worktree at the selected
checkpoint or release tag. It never runs `reset --hard`, switches the active
worktree, or discards local changes.

## License

MIT
