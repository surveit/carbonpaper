# carbonpaper server image — one container per tester (see docs/deploy.md).
# Runtime deps only (requirements.txt, not requirements-dev.txt): the image
# serves the app, it does not lint or test it.
FROM python:3.12-slim

# The llm_transform backend shells out to the `claude` CLI via claude-agent-sdk
# (app/runtime/options.py) and raises without it — so the CLI must be on PATH
# in the image. It is a Node package: install Node LTS from NodeSource, then
# the CLI globally, in one layer so the apt lists never persist.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/carbonpaper

# Requirements first, app second: editing app code reuses the pip layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# All file-backed state (projects, SQLite KV, parquet frames beside the DB)
# lives under /data. Nothing mounts it by default — the deployment is
# ephemeral unless the machine attaches a volume there (see fly.toml).
# CARBONPAPER_SEED_DEMO=1 seeds the bundled example project at boot, so a
# fresh instance opens with something to look at.
ENV CARBONPAPER_PROJECTS_DIR=/data/projects \
    CARBONPAPER_DB_PATH=/data/app.db \
    CARBONPAPER_SEED_DEMO=1
RUN mkdir -p /data/projects

EXPOSE 8080
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
