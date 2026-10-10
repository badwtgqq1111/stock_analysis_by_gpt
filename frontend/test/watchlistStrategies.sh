#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
DIR=$(mktemp -d)
trap 'rm -rf "$DIR"' EXIT
./node_modules/.bin/tsc src/components/watchlistStrategies.ts --strict --target ES2020 --module commonjs --outDir "$DIR"
node test/watchlistStrategies.test.cjs "$DIR/watchlistStrategies.js"
