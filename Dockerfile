# syntax=docker/dockerfile:1
#
# Hubzoid runner image, built from this source tree with the reviewed
# dependency set in requirements.lock, so the image version is the checked-out
# version. Use it when `pip install hubzoid` is not an option on the host.
#
# Build:
#   docker build -t hubzoid .
#
# The Open WebUI chat app (HUBZOID_UI=openwebui) adds
# Open WebUI, PyTorch (CPU build) and ffmpeg, several GB more:
#   docker build --build-arg WITH_OPENWEBUI=true -t hubzoid:openwebui .
#
# Run (single hub). The published port is reachable from other machines, so
# sign-in is on: put HUBZOID_AUTH=true and the first administrator
# (HUBZOID_ADMIN_EMAIL, HUBZOID_ADMIN_PASSWORD) in the hub's .env, then:
#   docker run -d --restart unless-stopped \
#     -p 3080:3080 \
#     -v "$PWD/my-hub:/hub" \
#     hubzoid
#
# Try it without sign-in, reachable from this machine only:
#   docker run --rm -p 127.0.0.1:3080:3080 -v "$PWD/my-hub:/hub" \
#     -e HUBZOID_ALLOW_UNAUTHENTICATED_NETWORK=true hubzoid
# Without sign-in and without that setting, `hubzoid run` refuses to start
# (inside a container it always binds 0.0.0.0) and says how to turn sign-in on.
#
# Run with the Slack chat surface too (Socket Mode, no extra port needed):
#   docker run -d --restart unless-stopped \
#     -p 3080:3080 \
#     -v "$PWD/my-hub:/hub" \
#     hubzoid run /hub --slack
#
# Only port 3080 (the edge: the web app, artifact downloads, MCP) is meant to
# be published. The bridge listens on 127.0.0.1 inside the container and is
# never reachable from outside it.
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

# true adds the openwebui extra for the legacy chat app. Nothing else differs.
ARG WITH_OPENWEBUI=false

# The default image needs no system packages: every dependency ships wheels for
# amd64 and arm64. Open WebUI needs ffmpeg at runtime for audio, and PyAV's
# build dependencies in case a prebuilt wheel is missing for the architecture.
RUN if [ "$WITH_OPENWEBUI" = "true" ]; then \
      apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        pkg-config build-essential \
        libavformat-dev libavcodec-dev libavdevice-dev libavutil-dev \
        libswscale-dev libswresample-dev \
      && rm -rf /var/lib/apt/lists/*; \
    fi

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
# legacy lock pins the CPU build of PyTorch, so no CUDA libraries are installed.
COPY --chown=hubzoid requirements.lock requirements-openwebui.lock /tmp/hubzoid-src/
RUN if [ "$WITH_OPENWEBUI" = "true" ]; then \
      pip install --user --extra-index-url https://download.pytorch.org/whl/cpu \
        -r /tmp/hubzoid-src/requirements-openwebui.lock; \
    else \
      pip install --user -r /tmp/hubzoid-src/requirements.lock; \
    fi
COPY --chown=hubzoid pyproject.toml README.md LICENSE NOTICE /tmp/hubzoid-src/
COPY --chown=hubzoid hubzoid /tmp/hubzoid-src/hubzoid
RUN pip install --user --no-deps /tmp/hubzoid-src && rm -rf /tmp/hubzoid-src

WORKDIR /hub
EXPOSE 3080

ENTRYPOINT ["hubzoid"]
CMD ["run", "/hub"]
