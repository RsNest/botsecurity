#!/usr/bin/env bash
# One-shot update on VDS: pull repo + image and recreate the container.
set -euo pipefail
cd "$(dirname "$0")"

git pull --ff-only
docker compose pull
docker compose up -d

echo
docker ps --filter name=botsecurity --format 'Image={{.Image}} Status={{.Status}}'
docker exec botsecurity python -c 'from bot.version import build_info; print(build_info())'
