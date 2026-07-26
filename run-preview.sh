#!/usr/bin/env bash
# Local design-preview server for the dashboard. Sample credentials only —
# this is never the deployment path (docker-compose owns that).
cd "$(dirname "$0")"
exec env ADDON_SECRET=fork-preview-secret \
  ADMIN_USERNAME=admin ADMIN_PASSWORD=fork-preview-2026 \
  TELEMETRY_DIR="$PWD/data" \
  FAST_BASE_URL="https://comet.example/abc123" \
  TMDB_API_KEY="preview-tmdb-key" \
  NZB_INDEXERS="demo|https://idx.example|demokey" \
  /srv/docker/stream-picker/.venv/bin/python -m uvicorn app.main:app \
  --host 0.0.0.0 --port 8791 --no-access-log
