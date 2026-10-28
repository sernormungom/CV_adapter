# syntax=docker/dockerfile:1
#
# CV Adapter dev/workshop image: one defined environment for a maintainer (VS
# Code "Reopen in Container"), any coding agent they run, a release check and,
# later, CI.
#
# Why it exists: an undefined environment used to cost a lot of work — a test
# runner without pytest, a Chromium without libnspr4/libasound, Node missing so
# client behaviour tests silently skipped. Everything those runs needed is
# installed here, at pinned versions, so `make test-complete` runs every gate
# for real.
#
# This is NOT the shipped artefact. The repository-root Dockerfile is (app
# only, no test tooling) and must stay that way. Nothing here is copied from
# the repository except the two requirements files: the source arrives as a
# bind mount at /workspace, so the image never goes stale against a branch.
#
# No secrets: no API key, token or CLI login is ever baked in. Credentials are
# passed as environment variables at run time (see compose.yml).

# Same interpreter line as production (Dockerfile: python:3.12-slim) and CI
# (PYTHON_VERSION 3.12). The full Debian variant rather than -slim: it already
# carries the compilers, headers and shared libraries a wheel-less dependency
# or a debugging session reaches for. Bookworm is pinned rather than the
# floating default so Playwright's --with-deps package list matches a
# distribution it supports.
ARG PYTHON_IMAGE=python:3.12.14-bookworm
# Node runs the tests/client/*.js behaviour probes (absent Node = silent skip)
# and hosts the agent CLIs. Active LTS line, same Debian release as above so
# the copied binary's libc/libstdc++ match.
ARG NODE_IMAGE=node:24.21.0-bookworm

FROM ${NODE_IMAGE} AS node

FROM ${PYTHON_IMAGE}

# Host user mapping: files the container writes into the bind-mounted repo
# must be owned by the host user, not root. Override with
# DEV_UID/DEV_GID in compose (`id -u` / `id -g` on the host).
ARG USERNAME=dev
ARG USER_UID=1000
ARG USER_GID=1000

# Pinned tool versions. Bump deliberately, in one commit, and re-run
# scripts/devcontainer-smoke.sh from a fresh container.
ARG PLAYWRIGHT_VERSION=1.63.0
ARG CLAUDE_CODE_VERSION=2.1.286
ARG CODEX_VERSION=0.159.3

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Browsers in a fixed shared path, not root's home, so the non-root user
    # can launch what root installed at build time.
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    # The CLIs are pinned above; a self-update inside a throwaway container
    # would make two runs of the same image differ (and fails as non-root).
    DISABLE_AUTOUPDATER=1

# Diagnostics and the tools the Makefile assumes. postgresql-client gives
# psql/pg_isready (Debian 12 ships v15; it talks to the Postgres 16 server
# fine for diagnostics — the tests use psycopg, not psql).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl git less make postgresql-client procps \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node --version && npm --version

# The image's own interpreter is the only Python: no venv to source, so
# `python`, `pytest` and `alembic` cannot resolve to different interpreters
# (the S26 failure). requirements-dev.txt pulls in requirements.txt, and
# `pip check` fails the build on an inconsistent set, as CI does.
# Playwright is dev-image-only (browser walkthroughs, not pytest), so it is
# pinned here rather than added to requirements-dev.txt and CI.
COPY requirements.txt requirements-dev.txt /tmp/requirements/
RUN python -m pip install --upgrade pip \
    && python -m pip install \
        -r /tmp/requirements/requirements-dev.txt \
        "playwright==${PLAYWRIGHT_VERSION}" \
    && python -m pip check \
    && rm -rf /tmp/requirements

# Chromium plus every system library it links (libnspr4, libasound2, ...),
# which is exactly what V3's walkthroughs were missing.
RUN python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# Agent CLIs, pinned. Installed globally as root so the non-root user can run
# but not modify them.
RUN npm install -g --no-fund --no-audit \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
        "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force

# Non-root user. --non-unique: a host GID can collide with a group the base
# image already has, and the numeric match is what matters on the bind mount.
# No sudo: the container is the agents' sandbox and needs no root at run time.
RUN groupadd --gid "${USER_GID}" --non-unique "${USERNAME}" \
    && useradd --uid "${USER_UID}" --gid "${USER_GID}" --non-unique \
        --create-home --shell /bin/bash "${USERNAME}" \
    && mkdir -p /workspace \
    && chown "${USER_UID}:${USER_GID}" /workspace

USER ${USERNAME}
# Bytecode goes to the container's own filesystem rather than __pycache__
# directories in the bind mount: faster on the /mnt/c 9p mount, and it leaves
# the checkout untouched.
ENV PYTHONPYCACHEPREFIX=/home/${USERNAME}/.cache/pycache
WORKDIR /workspace

CMD ["sleep", "infinity"]
