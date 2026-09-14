#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/renderer"
npm ci
echo "Renderer ready at $ROOT/renderer (wheel build runs npm ci via hatch_build.py)"
