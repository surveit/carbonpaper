FROM python:3.12-slim

# curl + ca-certificates fetch the Claude Code CLI installer below; git is what the
# CLI itself shells out to. No compiler: every package uv.lock pins resolves to a
# manylinux x86_64 or pure-python wheel on this base, starlark-pyo3's compiled
# extension (manylinux_2_17) included, so there is nothing to build from source.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# --frozen: install exactly what uv.lock pins, and fail the build if the lock has
# drifted from pyproject.toml rather than quietly re-resolving to newer versions.
# --no-dev leaves the test/lint group out of the image. `[tool.uv] package = false`
# means nothing is built from this directory, so only these two files are needed.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-cache
ENV PATH="/app/.venv/bin:${PATH}"

# The standalone native installer: no Node.js in the image. It needs only bash,
# curl and coreutils, and installs under $HOME rather than system-wide — as root
# with SUDO_USER unset, that is /root/.local/bin/claude, which the PATH below puts
# where app.core.llm_sdk's shutil.which("claude") looks.
#
# `claude --version` is a BUILD GATE, not a smoke test: the installer places the
# binary itself, so a release that moves it would otherwise produce an image that
# builds clean and then fails every llm_transform stage at run time.
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH="/root/.local/bin:${PATH}"
RUN claude --version

# Codex's supported Linux installer ships a standalone executable, so this
# image does not need Node.js. The visible command lives outside CODEX_HOME,
# which is mounted only after the container starts. The entrypoint copies this
# installed package into a new volume before Codex reads its runtime home.
RUN mkdir -p /opt/codex \
    && curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_HOME=/opt/codex CODEX_NON_INTERACTIVE=1 CODEX_INSTALL_DIR=/usr/local/bin sh
RUN codex --version

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
# Served at /intro by app.main. Its own directory because it is not part of the
# app's own surface — see app/web/config.py's INTRO_DIR.
COPY intro ./intro
# Four migrations import scripts.stage_signatures / scripts.stage_description at
# module level, so alembic cannot even build its revision map without this —
# the entrypoint's `alembic upgrade head` dies and the machine never boots.
# alembic.ini's `prepend_sys_path = .` is what resolves it from WORKDIR.
COPY scripts ./scripts
COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

# The volume Fly mounts at /data holds both: app.core.store_config computes the
# frames root from the database path's own directory, so pinning the database
# carries the frames with it and CARBON_PAPER_FRAMES_ROOT stays unset.
#
# CLAUDE_CONFIG_DIR joins them because the CLI keeps each chat's transcript under
# it, and a chat resumed by id against a config dir the last deploy discarded
# fails permanently. fly.toml sets the same value; it is repeated here so the
# entrypoint's mkdir has a path under plain `docker run` too.
ENV CARBON_PAPER_DB_PATH=/data/app.db \
    CARBON_PAPER_PROJECTS_DIR=/data/projects \
    CLAUDE_CONFIG_DIR=/data/claude \
    CODEX_HOME=/data/codex \
    PYTHONUNBUFFERED=1

EXPOSE 8080

ENTRYPOINT ["./docker-entrypoint.sh"]
