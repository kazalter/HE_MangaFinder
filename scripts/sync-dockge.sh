#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="${DOCKGE_STACK_DIR:-/opt/stacks/mangafinder}"

if [[ "${1:-}" == "--build-web" ]]; then
  npm ci --prefix "$ROOT_DIR/apps/web"
  npm run build --prefix "$ROOT_DIR/apps/web"
fi

mkdir -p "$TARGET_DIR/data"
rsync -a --delete \
  --exclude node_modules \
  --exclude data \
  --exclude .env \
  --exclude .pytest_cache \
  --exclude .ruff_cache \
  --exclude __pycache__ \
  "$ROOT_DIR/" "$TARGET_DIR/"

if [[ ! -f "$TARGET_DIR/.env" ]]; then
  cp "$TARGET_DIR/.env.example" "$TARGET_DIR/.env"
fi

docker compose --project-directory "$TARGET_DIR" config --quiet
echo "Dockge Stack 已同步到 $TARGET_DIR"
echo "请在 Dockge 中点击 Update，或在 Stack Terminal 执行 docker compose up -d --build"
