# syntax=docker/dockerfile:1
#
# Optional Hubzoid runner image. Use when `pip install hubzoid` fails on the
# host (PyAV build issues, Python-version traps, missing system libraries).
#
# Build:
#   docker build --build-arg HUBZOID_VERSION=0.4.0 -t hubzoid:0.4.0 .
#
# Run (single agent):
#   docker run -d --restart unless-stopped \
#     -p 3080:3080 \
#     -v "$PWD/my-hub:/hub" \
#     --env-file "$PWD/my-hub/.env" \
#     hubzoid:0.4.0
#
# Run with the Slack chat surface too (Socket Mode — no extra port needed):
#   docker run -d --restart unless-stopped \
#     -p 3080:3080 \
#     -v "$PWD/my-hub:/hub" \
#     --env-file "$PWD/my-hub/.env" \
#     hubzoid:0.4.0 run /hub --slack
#   # SLACK_BOT_TOKEN and SLACK_APP_TOKEN must be in your --env-file.
#   # Missing tokens log a warning and the container keeps running (web UI only).
#
# MODEL=claude-local does NOT work inside the image (no `claude` CLI).
# Use a portable API key (OpenRouter, OpenAI, Anthropic).
#
# See docs/DEPLOYING.md for the full production walkthrough.

FROM python:3.12-slim-bookworm

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
WORKDIR /hub

ENV PATH=/home/hubzoid/.local/bin:$PATH \
    HF_HOME=/home/hubzoid/.cache/huggingface \
    SENTENCE_TRANSFORMERS_HOME=/home/hubzoid/.cache/huggingface \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=3080 \
    BRIDGE_PORT=8000

# Override at build time: --build-arg HUBZOID_VERSION=<version>
ARG HUBZOID_VERSION=0.4.0
RUN pip install --user "hubzoid==${HUBZOID_VERSION}"

# Pre-bake Open WebUI's embedding model (~400 MB) so first container start
# does not stall while downloading from Hugging Face. Removes a cold-start
# failure mode and lets the container run in NAT-less private subnets.
RUN python -c "from sentence_transformers import SentenceTransformer; \
    SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"

EXPOSE 3080

ENTRYPOINT ["hubzoid", "run", "/hub"]
