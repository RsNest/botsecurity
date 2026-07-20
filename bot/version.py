"""Runtime build identity — set by Docker build args / env in GHCR images."""

from __future__ import annotations

import os
from datetime import datetime, timezone

# Bump when you want a human-visible deploy marker in /version
APP_VERSION = "2026.07.20-ghcr"


def build_info() -> dict[str, str]:
    sha = os.getenv("GIT_SHA", "local").strip() or "local"
    built = os.getenv("BUILD_TIME", "").strip()
    return {
        "version": APP_VERSION,
        "git_sha": sha[:12] if sha != "local" else "local",
        "build_time": built or "n/a",
        "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


def format_version() -> str:
    info = build_info()
    return (
        f"🤖 <b>botsecurity</b> <code>{info['version']}</code>\n"
        f"Git: <code>{info['git_sha']}</code>\n"
        f"Image build: <code>{info['build_time']}</code>\n"
        f"Process now: {info['started_at']}\n\n"
        "Если после <code>docker compose pull</code> здесь новый sha — "
        "деплой из GHCR сработал."
    )
