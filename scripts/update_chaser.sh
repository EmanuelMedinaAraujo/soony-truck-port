#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
chaser=integrations/supplier-chaser
if [[ ! -f "$chaser/.git" ]]; then
    git submodule update --init -- "$chaser"
fi
old=$(git -C "$chaser" rev-parse HEAD)
git submodule update --remote integrations/supplier-chaser
new=$(git -C "$chaser" rev-parse HEAD)
if [[ "$old" == "$new" ]]; then
    printf 'Supplier Chaser is already at %s.\n' "$new"
else
    git -C "$chaser" log --oneline "$old..$new"
fi
(cd "$chaser" && pixi install --locked)
scripts/run_chaser.sh restart
