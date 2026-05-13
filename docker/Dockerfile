# hubzoid container.
#
# Mount a hub at /hub. The container runs `hubzoid run /hub`. Open WebUI is
# included; expose ports 3080 (UI) and 8000 (API).
#
# Build:
#   docker build -f docker/Dockerfile -t hubzoid:dev .
# Run:
#   docker run --rm -p 3080:3080 -p 8000:8000 -v /path/to/my-hub:/hub hubzoid:dev

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates git \
  && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY hubzoid ./hubzoid

RUN pip install --no-cache-dir '.[ui]'

EXPOSE 3080 8000

ENV PORT=3080 BRIDGE_PORT=8000

CMD ["hubzoid", "run", "/hub"]
