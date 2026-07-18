#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

npx --yes markdownlint-cli2@0.23.1 \
  README.md \
  CONTRIBUTING.md \
  SECURITY.md \
  docs/HOW_IT_WORKS.md \
  docs/MAINTAINER_GUIDE.md \
  docs/design/*.md \
  local-cortex/console/CHANGELOG.md \
  .github/PULL_REQUEST_TEMPLATE.md
