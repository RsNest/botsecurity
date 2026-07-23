#!/usr/bin/env bash
# Update bot on VDS to the VERSION from the repo (set by CI on each push).
# Flow: git pull → BOT_IMAGE_TAG=<VERSION> → compose pull/up
set -euo pipefail
cd "$(dirname "$0")"

git pull --ff-only

TAG="$(tr -d '[:space:]' < VERSION)"
if ! echo "$TAG" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+([.-].+)?$'; then
  echo "Invalid VERSION file: '$TAG'" >&2
  exit 1
fi

touch .env
if grep -q '^BOT_IMAGE_TAG=' .env; then
  sed -i "s/^BOT_IMAGE_TAG=.*/BOT_IMAGE_TAG=${TAG}/" .env
else
  echo "BOT_IMAGE_TAG=${TAG}" >> .env
fi

echo "Deploying ghcr.io/rsnest/botsecurity:${TAG}"
docker compose pull
docker compose up -d

echo
docker ps --filter name=botsecurity --format 'Image={{.Image}} Status={{.Status}}'
docker exec botsecurity python -c 'from bot.version import build_info; print(build_info())'
