#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  printf 'Usage: %s <checkpoint-or-release-tag>\n' "$0" >&2
  exit 2
fi

root=$(git rev-parse --show-toplevel)
reference=$1
case "$reference" in
  checkpoint/*|stage/*|v*) ;;
  *)
    printf 'Restore accepts only checkpoint/*, stage/*, or v* tags.\n' >&2
    exit 2
    ;;
esac

commit=$(git -C "$root" rev-parse --verify "refs/tags/${reference}^{commit}")
short_head=$(git -C "$root" rev-parse --short "$commit")
stamp=$(date -u '+%Y%m%dT%H%M%SZ')
branch="restore/${stamp}-${short_head}"
repository_name=$(basename "$root")
worktree_root="$(dirname "$root")/.worktrees/${repository_name}"
worktree="${worktree_root}/${branch#restore/}"

mkdir -p "$worktree_root"
git -C "$root" worktree add --quiet -b "$branch" "$worktree" "$reference"
printf 'Created isolated restore worktree %s on %s at %s.\n' "$worktree" "$branch" "$reference"
