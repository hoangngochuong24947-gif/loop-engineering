#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  printf 'Usage: %s <checkpoint-tag-or-commit>\n' "$0" >&2
  exit 2
fi

root=$(git rev-parse --show-toplevel)
reference=$1
git -C "$root" rev-parse --verify "${reference}^{commit}" >/dev/null

if [ -n "$(git -C "$root" status --porcelain)" ]; then
  printf 'Working tree is not clean. Commit or stash changes before restoring.\n' >&2
  exit 1
fi

branch="restore/$(date '+%Y%m%d-%H%M%S')"
git -C "$root" switch -c "$branch" "$reference"
printf 'Created %s at %s. Original history was not rewritten.\n' "$branch" "$reference"
