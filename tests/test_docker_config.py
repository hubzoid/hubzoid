"""The container publishes only the edge port. The bridge trusts identity
headers and must never be reachable from outside the container; it binds
127.0.0.1 there, and only 3080 is published. The image installs the
checked-out source with the reviewed lock, so its version is the repo's.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import yaml

from hubzoid import cli

ROOT = Path(__file__).resolve().parents[1]


def test_compose_publishes_only_the_edge_port():
    compose = yaml.safe_load((ROOT / "docker" / "docker-compose.yml").read_text())
    service = compose["services"]["hubzoid"]
    assert service["ports"] == ["${HUBZOID_PUBLISH:-3080}:3080"]  # only the edge
    assert service["build"]["dockerfile"] == "Dockerfile"


def test_postgres_profile_keeps_the_database_private():
    compose = yaml.safe_load((ROOT / "docker" / "docker-compose.postgres.yml").read_text())
    db, hub = compose["services"]["db"], compose["services"]["hubzoid"]
    assert "ports" not in db and "ports" not in hub      # nothing new is published
    assert hub["environment"]["DATABASE_URL"].startswith("postgresql+psycopg://")
    assert hub["depends_on"]["db"]["condition"] == "service_healthy"
    assert "${POSTGRES_PASSWORD:?" in db["environment"]["POSTGRES_PASSWORD"]  # no default password


def test_image_builds_from_source_with_the_lock_and_binds_the_edge_publicly():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "requirements.lock" in dockerfile
    assert "HUBZOID_VERSION" not in dockerfile  # no stale pinned release
    assert "HUBZOID_HOST=0.0.0.0" in dockerfile
    assert "EXPOSE 3080\n" in dockerfile and "EXPOSE 3080 8000" not in dockerfile
    assert not (ROOT / "docker" / "Dockerfile").exists()


def test_host_option_reads_env_for_run_and_gateway():
    for command in (cli.run, cli.gateway):
        host = inspect.signature(command).parameters["host"].default
        assert host.envvar == "HUBZOID_HOST"


def test_bridge_always_binds_loopback():
    source = inspect.getsource(cli)
    assert '"--host", "127.0.0.1", "--port", str(br_port)' in source


def test_image_uses_cpu_pytorch_without_cuda():
    """The default image has no PyTorch at all; the Open WebUI image
    (and its lock) uses the CPU build."""
    core = (ROOT / "requirements.lock").read_text().lower()
    assert "\ntorch==" not in core
    lock = (ROOT / "requirements-openwebui.lock").read_text().lower()
    assert "download.pytorch.org/whl/cpu" in (ROOT / "Dockerfile").read_text()
    assert "+cpu" in lock
    for text in (core, lock):
        for pkg in ("nvidia-", "cuda-toolkit", "triton=="):
            assert f"\n{pkg}" not in text, pkg


def test_image_base_has_a_new_enough_sqlite():
    """DBOS on Python 3.12 needs SQLite 3.42 (Debian 12 has 3.40)."""
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "FROM python:3.12-slim-trixie" in dockerfile and "bookworm" not in dockerfile.split("FROM", 1)[1]


def _run_steps(dockerfile: str) -> list[str]:
    """RUN instructions with their continuation lines joined."""
    steps, current = [], None
    for line in dockerfile.splitlines():
        if line.startswith("RUN "):
            current = line
        elif current is not None:
            current += "\n" + line
        if current is not None and not line.rstrip().endswith("\\"):
            steps.append(current)
            current = None
    return steps


def test_default_image_is_slim_and_open_webui_is_a_build_argument():
    """No ffmpeg, PyAV build tools or PyTorch unless WITH_OPENWEBUI=true."""
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "ARG WITH_OPENWEBUI=false" in dockerfile
    steps = _run_steps(dockerfile)
    apt = [s for s in steps if "apt-get install" in s]
    assert len(apt) == 1 and 'if [ "$WITH_OPENWEBUI" = "true" ]' in apt[0]
    assert "ffmpeg" in apt[0] and "libavcodec-dev" in apt[0]
    (deps,) = [s for s in steps if "-r /tmp/hubzoid-src/requirements" in s]
    legacy, default = deps.split("else", 1)
    assert "requirements-openwebui.lock" in legacy and "download.pytorch.org/whl/cpu" in legacy
    assert "requirements.lock" in default and "pytorch" not in default
    assert "requirements-openwebui.lock" in dockerfile.split("COPY", 1)[1]  # in the build context
    assert '--no-deps /tmp/hubzoid-src' in dockerfile
    assert 'CMD ["run", "/hub"]' in dockerfile


def test_compose_runs_the_web_app_with_sign_in():
    compose = yaml.safe_load((ROOT / "docker" / "docker-compose.yml").read_text())
    service = compose["services"]["hubzoid"]
    env = service["environment"]
    assert env["HUBZOID_AUTH"] == "${HUBZOID_AUTH:-true}"  # the port is reachable from other machines
    assert env["HUBZOID_ALLOW_UNAUTHENTICATED_NETWORK"] == "${HUBZOID_ALLOW_UNAUTHENTICATED_NETWORK:-false}"
    assert not [k for k in env if k.startswith(("WEBUI_", "OPENAI_API_", "OWUI_"))]
    assert service["build"]["args"]["WITH_OPENWEBUI"] == "${HUBZOID_WITH_OPENWEBUI:-false}"
    text = (ROOT / "docker" / "docker-compose.yml").read_text()
    for needle in ("HUBZOID_ADMIN_EMAIL", "HUBZOID_PUBLIC_URL", "HUBZOID_UI=openwebui",
                   "127.0.0.1:3080"):
        assert needle in text, needle
