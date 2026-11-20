# This hub as one image for AWS: the hub service on ECS and, when the stack
# turns it on, the AgentCore runner and MCP runtime. Written by
# `hubzoid deploy init aws` (Hubzoid {{version}}). Built for arm64 (Graviton),
# which AgentCore requires, by the stack's CodeBuild project.
#
# What the container runs is chosen by HUBZOID_ROLE (`hubzoid runner start`):
# unset starts the hub service (`hubzoid run`), `runner` serves agent turns on
# port 8080 and `mcp` the hub's MCP server on port 8000. AgentCore passes a
# container environment variables, never arguments.
#
# The hub's files come from Git and are only read; everything the hub writes
# goes to EFS at /mnt/data. The folder keeps the hub's name: access grants,
# secret references and backups name the hub by it.
FROM {{image}}
USER root
# AWS's OpenTelemetry distribution: AgentCore observability (traces, spans).
RUN pip install "aws-opentelemetry-distro>=0.18,<1"
COPY . /hubs/{{hub}}
USER hubzoid
WORKDIR /hubs/{{hub}}
ENV HUBZOID_DATA_DIR=/mnt/data \
    HUBZOID_HUB_DIR=/hubs/{{hub}}
EXPOSE 3080 8080 8000
CMD ["runner", "start"]
