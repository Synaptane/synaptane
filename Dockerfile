# Synaptane image: web application (serve), worker and administration commands.
#   docker build -t synaptane:local .
#   docker build --build-arg AGENT_CLIS=false -t synaptane:local .   # without Claude Code or Codex
# With Docker Compose: compose.build.yaml. Keep a local name (synaptane:local), never the
# published one (ghcr.io/synaptane/synaptane), which contains no subscription CLI.

FROM python:3.12-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.31@sha256:ecd4de2f060c64bea0ff8ecb182ddf46ba3fcccdc8a60cfdbaf20d1a047d7437 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
# Licenses: copied into the installed package's dist-info (pyproject, license-files).
COPY README.md LICENSE NOTICE THIRD_PARTY_NOTICES.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable


# Claude and ChatGPT subscriptions: official CLIs, run without built-in tools (docs/admin/ai.md).
# Both are native executables: only they are kept, without Node.
FROM node:26-trixie-slim@sha256:193fe51b64e77981119c98c2002c9e32a70e2f006fb4d25068ce0558998917f0 AS agents
ARG AGENT_CLIS=true
# Exact versions (reproducible image), tracked by .github/workflows/cli-versions.yml.
ARG CLAUDE_CODE_VERSION=2.1.314
ARG CODEX_VERSION=0.166.0
RUN mkdir -p /out/usr/local/bin /out/opt \
 && if [ "$AGENT_CLIS" = "true" ]; then \
      npm install -g --omit=dev "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
                                "@openai/codex@${CODEX_VERSION}" \
      && root="$(npm root -g)" \
      && cp "$root/@anthropic-ai/claude-code/bin/claude.exe" /out/usr/local/bin/claude \
      && cp -a "$root"/@openai/codex/node_modules/@openai/codex-linux-*/vendor/*-linux-musl \
               /out/opt/codex \
      && ln -s /opt/codex/bin/codex /out/usr/local/bin/codex; \
    fi


FROM python:3.12-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

# `upgrade`: Debian security fixes, even if the Python image has not been republished
# (rebuild with `--pull --no-cache` to get them: see docs/admin/installation.md).
RUN apt-get update \
 && apt-get upgrade -y \
 && apt-get install -y --no-install-recommends \
      ca-certificates git openssh-client ripgrep tini \
      tesseract-ocr tesseract-ocr-fra tesseract-ocr-eng catdoc \
      postgresql-client-17 age rclone \
 && rm -rf /var/lib/apt/lists/*
COPY --from=agents /out/ /

# /agents: HOME of the subscription CLIs, a volume of the worker only (compose.yaml). Created
# here so that the volume inherits the owner; empty and read-only in the web container.
RUN useradd --uid 1000 --create-home --home-dir /home/synaptane synaptane \
 && install -d -o synaptane -g synaptane /data /backups /agents

COPY --from=build /app/.venv /app/.venv
# Licenses, also at the usual place (texts of the Debian packages: /usr/share/doc/*/copyright,
# /usr/share/common-licenses/).
COPY LICENSE NOTICE THIRD_PARTY_NOTICES.md /usr/share/doc/synaptane/
COPY deploy/docker/entrypoint.sh /usr/local/bin/synaptane-entrypoint
# Data for `synaptane demo`, found next to the Python environment (/app/.venv).
COPY examples /app/examples

# Configuration through SYNAPTANE_* environment variables (see compose.yaml).
# The subscription CLIs keep their login in SYNAPTANE_AGENT_HOME, outside the data volume
# that the web container shares (docs/admin/ai.md).
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    SYNAPTANE_DATA_DIR=/data \
    SYNAPTANE_BACKUP_DIR=/backups \
    SYNAPTANE_AGENT_HOME=/agents \
    HOME=/data/home \
    RCLONE_CONFIG=/data/rclone/rclone.conf \
    DISABLE_AUTOUPDATER=1

# Built commit, to know what is running (`synaptane --version`, `doctor`, administration
# footer, backup manifest, `docker inspect`):
#   docker build --build-arg GIT_SHA="$(git rev-parse HEAD)" .
# Last: changing commits rebuilds no layer.
# VERSION follows pyproject.toml (checked by tests/unit/test_image_metadata.py).
ARG GIT_SHA=unknown
ARG VERSION=1.0.0rc2
LABEL org.opencontainers.image.title="Synaptane" \
      org.opencontainers.image.description="Self-hosted team second brain stored in Git" \
      org.opencontainers.image.source="https://github.com/synaptane/synaptane" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}"
ENV SYNAPTANE_GIT_SHA=${GIT_SHA}

USER synaptane
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8000
ENTRYPOINT ["tini", "--", "synaptane-entrypoint"]
# No trusted proxy by default (only 127.0.0.1): otherwise, an exposed `docker run -p` would
# believe any X-Forwarded-For and the per-IP-address limits could be bypassed.
# Behind a proxy, compose.yaml passes --forwarded-allow-ips (FORWARDED_ALLOW_IPS).
CMD ["synaptane", "serve", "--host", "0.0.0.0", "--port", "8000"]
