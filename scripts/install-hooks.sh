#!/bin/sh
set -eu

root=$(git rev-parse --show-toplevel)
chmod +x "$root"/.githooks/* "$root"/scripts/*.sh
git -C "$root" config core.hooksPath .githooks
printf 'Git hooks enabled from %s/.githooks\n' "$root"
