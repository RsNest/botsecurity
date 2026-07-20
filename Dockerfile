FROM python:3.12-slim

WORKDIR /app

ARG GIT_SHA=local
ARG BUILD_TIME=n/a
ENV GIT_SHA=$GIT_SHA
ENV BUILD_TIME=$BUILD_TIME

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN addgroup --system bot && adduser --system --ingroup bot bot

COPY --chown=bot:bot bot/ ./bot/

RUN mkdir -p /app/data && chown -R bot:bot /app/data

USER bot

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "from pathlib import Path; assert Path('/app/data/botscan.db').exists()"

CMD ["python", "-m", "bot.main"]
