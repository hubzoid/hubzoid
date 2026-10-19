# Hubzoid's default sandbox image: an unprivileged workspace for agent commands.
# Git, Python with uv, Node with npm, ripgrep and tini. No credential helpers,
# no SSH keys, no sudo. Commands run as `hubzoid` (uid 1000).
FROM debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash ca-certificates coreutils curl findutils git gnupg grep less procps \
      python3 python3-pip python3-venv ripgrep sed tini xz-utils nodejs npm \
 && rm -rf /var/lib/apt/lists/* \
 && pip3 install --no-cache-dir --break-system-packages uv \
 && useradd --uid 1000 --create-home --home-dir /home/hubzoid --shell /bin/bash hubzoid \
 && mkdir -p /workspace \
 && chown hubzoid:hubzoid /workspace \
 && git config --system --unset-all credential.helper || true

ENV HOME=/home/hubzoid PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
USER hubzoid
WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["tail", "-f", "/dev/null"]
