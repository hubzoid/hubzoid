# syntax=docker/dockerfile:1
#
# Hubzoid runner image, built from this source tree with the reviewed
# dependency set in requirements.lock, so the image version is the checked-out
# version. Use it when `pip install hubzoid` fails on the host (PyAV build
# issues, Python-version traps, missing system libraries).
#
# Build:
#   docker build -t hubzoid .
#
# Run (single agent):
#   docker run -d --restart unless-stopped \
#     -p 3080:3080 \
#     -v "$PWD/my-hub:/hub" \
#     --env-file "$PWD/my-hub/.env" \
#     hubzoid
#
# Run with the Slack chat surface too (Socket Mode, no extra port needed):
#   docker run -d --restart unless-stopped \
#     -p 3080:3080 \
#     -v "$PWD/my-hub:/hub" \
#     --env-file "$PWD/my-hub/.env" \
#     hubzoid run /hub --slack
#
# Only port 3080 (the edge: chat UI, artifact downloads, portal, MCP) is
# meant to be published. The bridge listens on 127.0.0.1 inside the
# container and is never reachable from outside it.
#
# Local CLI backends require operator-provisioned CLI binaries and logins.
# The base image does not install Codex; use a hosted provider by default.
# Use a portable API key (OpenRouter, OpenAI, Anthropic).
#
# See docs/DEPLOYING.md for the full production walkthrough.

# Debian 13 (trixie): its SQLite 3.46 supports what the workflow engine needs
# on Python 3.12. Debian 12's SQLite 3.40 does not, and scheduled work would
# not start.
FROM python:3.12-slim-trixie

# Runtime + build deps. ffmpeg is needed at runtime by Open WebUI's audio path.
# The libav-dev packages and build-essential cover the PyAV build deps in case
# a prebuilt wheel is not available for the target architecture.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg \
      pkg-config build-essential \
      libavformat-dev libavcodec-dev libavdevice-dev libavutil-dev \
      libswscale-dev libswresample-dev \
    && rm -rf /var/lib/apt/lists/*

# Non-root user.
RUN useradd -r -m -d /home/hubzoid -s /bin/bash hubzoid
USER hubzoid

ENV PATH=/home/hubzoid/.local/bin:$PATH \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=3080 \
    BRIDGE_PORT=8000 \
    HUBZOID_HOST=0.0.0.0

# Reviewed dependencies first (cached layer), then the package itself. The
# lock pins the CPU build of PyTorch, so no CUDA libraries are installed.
COPY --chown=hubzoid requirements.lock /tmp/hubzoid-src/requirements.lock
RUN pip install --user --extra-index-url https://download.pytorch.org/whl/cpu \
      -r /tmp/hubzoid-src/requirements.lock
COPY --chown=hubzoid pyproject.toml README.md LICENSE NOTICE /tmp/hubzoid-src/
COPY --chown=hubzoid hubzoid /tmp/hubzoid-src/hubzoid
RUN pip install --user --no-deps /tmp/hubzoid-src && rm -rf /tmp/hubzoid-src

WORKDIR /hub
EXPOSE 3080

ENTRYPOINT ["hubzoid"]
CMD ["run", "/hub"]
