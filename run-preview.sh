#!/usr/bin/env bash
# Local design-preview server for the dashboard. Sample credentials only —
# this is never the deployment path (docker-compose owns that).
#
# TELEMETRY_DIR is deliberately a scratch directory and NOT the repo's ./data:
# that one is the live bind mount, holding the deployed config.json (encrypted
# with a key only the container has) alongside real telemetry and playback
# sessions. A design preview must not read it, and must certainly not be able
# to save over it.
set -euo pipefail
cd "$(dirname "$0")"
PREVIEW_DIR="${PREVIEW_DIR:-${TMPDIR:-/tmp}/stream-picker-preview}"
mkdir -p "$PREVIEW_DIR"
echo "preview state: $PREVIEW_DIR (throwaway — delete it to start clean)" >&2
exec env ADDON_SECRET=preview-secret \
  ADMIN_USERNAME=admin ADMIN_PASSWORD=preview-2026 \
  TELEMETRY_DIR="$PREVIEW_DIR" \
  CONFIG_FILE="$PREVIEW_DIR/config.json" \
  FAST_BASE_URL="https://comet.example/abc123" \
  TMDB_API_KEY="preview-tmdb-key" \
  NZB_INDEXERS="demo|https://idx.example|demokey" \
  ./.venv/bin/python -m uvicorn app.main:app \
  --host 0.0.0.0 --port 8791 --no-access-log
