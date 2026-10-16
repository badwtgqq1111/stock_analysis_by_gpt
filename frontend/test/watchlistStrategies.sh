#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
DIR=$(mktemp -d)
trap 'rm -rf "$DIR"' EXIT
./node_modules/.bin/tsc -p test/tsconfig.watchlist.json --outDir "$DIR"
node test/watchlistStrategies.test.cjs "$DIR/components/watchlistStrategies.js"
