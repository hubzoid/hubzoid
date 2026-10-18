"""The local Docker provider: the small gap around the Agents SDK's Docker
sandbox session.

The OpenAI Agents SDK (`agents.sandbox`, used here as a library for every
runtime) runs commands and moves files in a container. Hubzoid creates that
container itself, because the SDK's own client sets no CPU, memory or process
limits, copies its manifest environment into the container, and recreates a
missing container on resume. Here:

* every container runs as uid 1000 with all capabilities dropped,
  no-new-privileges, CPU, memory (no swap) and process limits, and labels
  naming the deployment and the sandbox;
* a sandbox whose profile allows setup network sits on its own internal Docker
  network. A per-sandbox Squid proxy, the only other member, forwards the
  setup command's requests to the allowed registry hosts and nothing else. The
  sandbox has no route or DNS of its own;
* after setup the container is stopped (every setup process ends), taken off
  that network, and started again with no network at all, then checked;
* a container that is gone is reported missing, never recreated.

No credential is passed to any container.
"""
from __future__ import annotations

import hashlib
import io
import logging
import shlex
import tarfile
import time
import uuid
from pathlib import Path

from ..work_items.errors import WorkItemError

log = logging.getLogger(__name__)

#: Squid for the setup phase, pinned by digest (ubuntu/squid 6.6, Apache-2.0
#: image of the GPL Squid binary, run unmodified as a separate container).
SQUID_IMAGE = ("ubuntu/squid:6.6-24.04_edge@sha256:"
               "8a3baed477e2c282ab8aa5edad442f69873246964f225c5c2ae8364b6610963c")
BASE_DIR = Path(__file__).parent / "images" / "base"
WORKSPACE = "/workspace"
USER = "1000:1000"
LABEL = "hubzoid.sandbox"
OUTPUT_TAIL = 8192


def base_image() -> str:
    """The tag of Hubzoid's default image, named by its Dockerfile's digest."""
    digest = hashlib.sha256((BASE_DIR / "Dockerfile").read_bytes()).hexdigest()[:12]
    return f"hubzoid-sandbox:{digest}"


def image_name(profile) -> str:
    return base_image() if profile.image.get("base") else profile.image["ref"]


def _unavailable(exc: Exception, endpoint: str | None) -> WorkItemError:
    return WorkItemError("docker_unavailable",
                         f"Docker is not reachable at {endpoint or 'the default endpoint'} ({type(exc).__name__}). "
                         "Start Docker Desktop or the Docker engine, then run `hubzoid sandbox doctor`.",
                         status=503)


def _squid_conf(hosts: list[str]) -> bytes:
    allowed = " ".join(hosts) if hosts else "invalid.invalid"
    return (f"http_port 3128\n"
            f"acl allowed dstdomain {allowed}\n"
            "acl SSL_ports port 443\n"
            "acl Safe_ports port 80 443\n"
            "http_access deny !Safe_ports\n"
            "http_access deny CONNECT !SSL_ports\n"
            "http_access allow allowed\n"
            "http_access deny all\n"
            "cache deny all\n"
            "via off\n"
            "forwarded_for delete\n").encode()


def _tar_one(name: str, data: bytes, *, mode=0o644, uid=0) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name)
        info.size, info.mode, info.uid, info.gid, info.mtime = len(data), mode, uid, uid, int(time.time())
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class DockerProvider:
    kind = "docker"

    def __init__(self, binding: dict):
        self.binding = binding
        self.endpoint = binding.get("endpoint")
        try:
            import docker
        except ImportError as exc:   # pragma: no cover - the extra is not installed
            raise WorkItemError("docker_unavailable", "Docker sandboxes need the docker package: "
                                "pip install 'hubzoid[sandbox]'.", status=503) from exc
        self._docker = docker
        try:
            self.client = (docker.DockerClient(base_url=self.endpoint, timeout=60) if self.endpoint
                           else docker.from_env(timeout=60))
            self.client.ping()
        except Exception as exc:  # noqa: BLE001 - any connection problem means unreachable
            raise _unavailable(exc, self.endpoint) from None

    # -- checks and images ------------------------------------------------------

    def check(self) -> dict:
        info, version = self.client.info(), self.client.version()
        security = " ".join(info.get("SecurityOptions") or [])
        return {"reachable": True, "server_version": version.get("Version"),
                "runtimes": sorted((info.get("Runtimes") or {}).keys()),
                "runtime": self.binding.get("runtime", "runc"),
                "isolation": "gvisor" if self.binding.get("runtime") == "runsc" else "container",
                "storage_driver": info.get("Driver"), "rootless": "rootless" in security,
                "disk_limit": "unenforced",
                "operating_system": info.get("OperatingSystem")}

    def has_image(self, name: str) -> bool:
        try:
            self.client.images.get(name)
            return True
        except self._docker.errors.ImageNotFound:
            return False

    def prepare(self, profile, *, needs_proxy: bool) -> dict:
        """Build or pull the profile's image (and the setup proxy's)."""
        name = image_name(profile)
        if not self.has_image(name):
            if profile.image.get("base"):
                log.info("building %s", name)
                self.client.images.build(path=str(BASE_DIR), tag=name, rm=True, pull=False,
                                         labels={"hubzoid.image": "base"})
            else:
                log.info("pulling %s", name)
                self.client.images.pull(name)
        if needs_proxy and not self.has_image(SQUID_IMAGE):
            self.client.images.pull(SQUID_IMAGE)
        return {"image": name, "image_id": self.client.images.get(name).id}

    # -- lifecycle ----------------------------------------------------------------

    def _labels(self, deployment: str, sandbox_id: str, role: str) -> dict:
        return {LABEL: sandbox_id, "hubzoid.deployment": deployment, "hubzoid.role": role}

    def create(self, sandbox_id: str, deployment: str, profile, *, with_setup_network: bool) -> dict:
        res = profile.resources
        name = image_name(profile)
        if not self.has_image(name):
            raise WorkItemError("image_unavailable", f"The image for profile {profile.id} is not prepared. "
                                f"Run `hubzoid sandbox profiles prepare . {profile.id}`.", status=503)
        network = None
        if with_setup_network:
            network = self.client.networks.create(f"hz-sbx-{sandbox_id[:12]}", driver="bridge", internal=True,
                                                  labels=self._labels(deployment, sandbox_id, "network"))
        kwargs = dict(
            image=name, name=f"hz-sbx-{sandbox_id[:12]}", hostname="sandbox", detach=True, user=USER,
            working_dir=WORKSPACE, environment=dict(profile.env, HOME="/home/hubzoid"),
            labels=self._labels(deployment, sandbox_id, "box"), cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"], nano_cpus=int(res["cpus"] * 1e9),
            mem_limit=f"{res['memory_mib']}m", memswap_limit=f"{res['memory_mib']}m", pids_limit=res["pids"],
            ipc_mode="private", runtime=self.binding.get("runtime", "runc"))
        if self.binding.get("platform"):
            kwargs["platform"] = self.binding["platform"]
        if res["require_disk_limit"]:
            kwargs["storage_opt"] = {"size": f"{res['disk_mib']}m"}
        if network is not None:
            kwargs["network"] = network.name
        else:
            kwargs["network_mode"] = "none"
        try:
            container = self.client.containers.create(**kwargs)
        except self._docker.errors.APIError as exc:
            if network is not None:
                network.remove()
            if res["require_disk_limit"] and "storage" in str(exc).lower():
                raise WorkItemError("resource_limit_unenforceable", "This Docker engine cannot limit disk size. "
                                    "Set require_disk_limit: false or use a storage driver that can.",
                                    status=422) from None
            raise
        container.start()
        return {"container_id": container.id, "network": network.name if network else None, "image": name}

    def container(self, state: dict):
        """The sandbox's container, or None when it no longer exists."""
        try:
            return self.client.containers.get(state["container_id"])
        except self._docker.errors.NotFound:
            return None

    def run_setup(self, sandbox_id: str, deployment: str, state: dict, profile, *, hosts: list[str]) -> dict:
        """Run the profile's reviewed setup command once, through the setup
        proxy when the profile allows hosts. Returns {exit_code, output}."""
        setup = profile.setup
        container = self.container(state)
        env = {}
        proxy = None
        try:
            if state.get("network") and hosts:
                proxy = self._start_proxy(sandbox_id, deployment, state["network"], hosts)
                url = f"http://{proxy[1]}:3128"
                env = {"HTTP_PROXY": url, "HTTPS_PROXY": url, "http_proxy": url, "https_proxy": url,
                       "NO_PROXY": "localhost,127.0.0.1", "no_proxy": "localhost,127.0.0.1"}
            cwd = str(Path(WORKSPACE) / setup["cwd"])
            command = (f"mkdir -p {shlex.quote(cwd)} && cd {shlex.quote(cwd)} && "
                       f"timeout -k 5 {int(setup['timeout_seconds'])} bash -lc {shlex.quote(setup['command'])}")
            result = container.exec_run(["bash", "-lc", command], user=USER, environment=env, workdir=WORKSPACE)
            output = (result.output or b"").decode("utf-8", errors="replace")
            return {"exit_code": result.exit_code, "output": output[-OUTPUT_TAIL:],
                    "timed_out": result.exit_code in (124, 137)}
        finally:
            if proxy is not None:
                self._remove(proxy[0])

    def _start_proxy(self, sandbox_id, deployment, network_name, hosts) -> tuple:
        proxy = self.client.containers.create(
            SQUID_IMAGE, name=f"hz-sbx-proxy-{sandbox_id[:12]}", detach=True, network="bridge",
            labels=self._labels(deployment, sandbox_id, "proxy"), mem_limit="256m", pids_limit=256)
        proxy.put_archive("/etc/squid", _tar_one("squid.conf", _squid_conf(hosts)))
        self.client.networks.get(network_name).connect(proxy)
        proxy.start()
        deadline = time.time() + 30
        while time.time() < deadline:
            proxy.reload()
            if proxy.status != "running":
                break
            if b"Accepting HTTP Socket connections" in proxy.logs(tail=200):
                ip = proxy.attrs["NetworkSettings"]["Networks"][network_name]["IPAddress"]
                return proxy, ip
            time.sleep(0.5)
        logs = proxy.logs(tail=20).decode(errors="replace")
        self._remove(proxy)
        raise WorkItemError("setup_failed", "The setup network proxy did not start: " + logs[-500:], status=503)

    def go_offline(self, state: dict) -> dict:
        """End every setup process and remove all network, then check it."""
        container = self.container(state)
        container.stop(timeout=5)
        if state.get("network"):
            try:
                net = self.client.networks.get(state["network"])
                net.disconnect(container, force=True)
                net.remove()
            except self._docker.errors.NotFound:
                pass
        container.start()
        return self.verify_offline(state)

    def verify_offline(self, state: dict) -> dict:
        container = self.container(state)
        container.reload()
        networks = set((container.attrs.get("NetworkSettings") or {}).get("Networks") or {}) - {"none"}
        probe = container.exec_run(
            ["python3", "-c", "import socket\n"
             "for h in (('1.1.1.1', 443), ('2606:4700:4700::1111', 443)):\n"
             "    try:\n        socket.create_connection(h, 3).close(); print('open', h[0])\n"
             "    except OSError:\n        pass\n"
             "try:\n    socket.getaddrinfo('pypi.org', 443); print('dns')\nexcept OSError:\n    pass\n"],
            user=USER)
        reached = (probe.output or b"").decode(errors="replace").split()
        processes = container.exec_run(["ps", "-eo", "pid=,comm="], user=USER).output.decode().split("\n")
        ok = not networks and not reached
        return {"mode": "deny_all", "verified": ok, "networks": sorted(networks), "reached": reached,
                "processes": [p.strip() for p in processes if p.strip()]}

    def start(self, state: dict) -> None:
        container = self.container(state)
        container.reload()
        if container.status != "running":
            container.start()

    def stop(self, state: dict) -> None:
        container = self.container(state)
        if container is not None:
            container.stop(timeout=5)

    def delete(self, sandbox_id: str) -> int:
        """Remove every resource labelled with this sandbox. Idempotent."""
        removed = 0
        for c in self.client.containers.list(all=True, filters={"label": f"{LABEL}={sandbox_id}"}):
            self._remove(c)
            removed += 1
        for n in self.client.networks.list(filters={"label": f"{LABEL}={sandbox_id}"}):
            try:
                n.remove()
                removed += 1
            except self._docker.errors.APIError:
                pass
        return removed

    def _remove(self, container) -> None:
        try:
            container.remove(force=True)
        except self._docker.errors.NotFound:
            pass

    def resources(self, deployment: str) -> list[dict]:
        """Every container and network this deployment labelled."""
        out = []
        for c in self.client.containers.list(all=True, filters={"label": f"hubzoid.deployment={deployment}"}):
            created = c.attrs.get("Created", "")
            out.append({"sandbox_id": c.labels.get(LABEL), "kind": "container", "id": c.id,
                        "role": c.labels.get("hubzoid.role"), "created": _epoch(created)})
        for n in self.client.networks.list(filters={"label": f"hubzoid.deployment={deployment}"}):
            out.append({"sandbox_id": n.attrs.get("Labels", {}).get(LABEL), "kind": "network", "id": n.id,
                        "role": "network", "created": _epoch(n.attrs.get("Created", ""))})
        return out

    # -- files and commands ---------------------------------------------------

    def session(self, state: dict, sandbox_id: str):
        """The Agents SDK session over this container (commands, files, PTY)."""
        from agents.sandbox.manifest import Manifest
        from agents.sandbox.sandboxes.docker import DockerSandboxSession, DockerSandboxSessionState
        from agents.sandbox.snapshot import resolve_snapshot

        container = self.container(state)
        sid = uuid.UUID(sandbox_id)
        sdk_state = DockerSandboxSessionState(
            session_id=sid, manifest=Manifest(root=WORKSPACE), image=container.image.tags[0]
            if container.image.tags else container.image.id, snapshot=resolve_snapshot(None, str(sid)),
            container_id=container.id)
        return DockerSandboxSession(docker_client=self.client, container=container, state=sdk_state)

    def write_tree(self, state: dict, source: Path, dest: str) -> dict:
        """Copy a host folder into the sandbox as uid 1000. Symlinks are kept as
        links inside the sandbox; nothing outside `source` is read."""
        buf = io.BytesIO()
        files = 0
        top = Path(dest).name
        with tarfile.open(fileobj=buf, mode="w") as tar:
            root = tar.gettarinfo(str(source), arcname=top)
            root.uid = root.gid = 1000
            tar.addfile(root)
            for path in sorted(source.rglob("*")):
                rel = path.relative_to(source)
                info = tar.gettarinfo(str(path), arcname=str(Path(top) / rel))
                info.uid = info.gid = 1000
                info.uname = info.gname = "hubzoid"
                if path.is_symlink() or not path.is_file():
                    tar.addfile(info)
                else:
                    with path.open("rb") as fh:
                        tar.addfile(info, fh)
                    files += 1
        container = self.container(state)
        container.put_archive(str(Path(dest).parent), buf.getvalue())
        return {"files": files, "bytes": buf.tell()}

    def run(self, state: dict, argv: list[str], *, timeout: int, workdir: str = WORKSPACE) -> dict:
        container = self.container(state)
        command = f"timeout -k 5 {int(timeout)} " + " ".join(shlex.quote(a) for a in argv)
        result = container.exec_run(["bash", "-lc", command], user=USER, workdir=workdir, demux=True)
        out, err = result.output if isinstance(result.output, tuple) else (result.output, b"")
        return {"exit_code": result.exit_code, "stdout": (out or b""), "stderr": (err or b"")}


def _epoch(text: str) -> float | None:
    from datetime import datetime

    if not text:
        return None
    try:
        return datetime.fromisoformat(text[:26].rstrip("Z") + "+00:00").timestamp()
    except ValueError:
        return None
