#!/bin/sh
set -eu

root=$(git rev-parse --show-toplevel)
git -C "$root" config core.hooksPath .githooks
printf 'Git hooks enabled from %s/.githooks\n' "$root"
