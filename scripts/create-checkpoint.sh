#!/bin/sh
set -eu

root=$(git rev-parse --show-toplevel)
purpose=${1:-}
case "$purpose" in
  ''|*[!A-Za-z0-9._-]*)
    printf 'Usage: %s <short-purpose>\n' "$0" >&2
    exit 2
    ;;
esac

if [ -n "$(git -C "$root" status --porcelain)" ]; then
  printf 'Working tree is not clean. Commit changes before creating a checkpoint.\n' >&2
  exit 1
fi

upstream=$(git -C "$root" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null || true)
if [ -z "$upstream" ] || [ "$(git -C "$root" rev-parse HEAD)" != "$(git -C "$root" rev-parse "$upstream")" ]; then
  printf 'Push the current commit to its upstream branch before creating a checkpoint.\n' >&2
  exit 1
fi

python3 -m unittest discover -s "$root/tests" -p 'test_*.py'
python3 "$root/scripts/loopctl.py" doctor >/dev/null

stamp=$(date -u '+%Y%m%dT%H%M%SZ')
short_head=$(git -C "$root" rev-parse --short HEAD)
tag="checkpoint/${purpose}/${stamp}-${short_head}"
git -C "$root" tag -a "$tag" -m "Verified checkpoint: $purpose at $short_head"
git -C "$root" push origin "$tag"
printf 'Created and pushed %s\n' "$tag"
