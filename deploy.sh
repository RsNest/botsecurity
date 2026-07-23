#!/usr/bin/env bash
# Optional helper. Same as:
#   git pull && docker compose pull && docker compose up -d
set -euo pipefail
cd "$(dirname "$0")"
git pull --ff-only
docker compose pull
docker compose up -d
docker ps --filter name=botsecurity --format 'Image={{.Image}} Status={{.Status}}'
docker exec botsecurity python -c 'from bot.version import build_info; print(build_info())'
