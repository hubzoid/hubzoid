"""Docker provider isolation, failed resource creation and safe archive transfer."""
from __future__ import annotations

import io
import tarfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hubzoid.sandbox import docker_provider as dp
from hubzoid.work_items.errors import WorkItemError


class APIError(Exception):
    pass


class NotFound(APIError):
    pass


@pytest.fixture
def provider():
    prov = object.__new__(dp.DockerProvider)
    prov.binding = {"runtime": "runsc", "platform": "linux/amd64"}
    prov.endpoint = None
    prov._docker = SimpleNamespace(errors=SimpleNamespace(APIError=APIError, NotFound=NotFound, ImageNotFound=NotFound))
    prov.client = Mock()
    return prov


@pytest.fixture
def profile():
    return SimpleNamespace(id="work", image={"ref": "example@sha256:1234"}, env={"NOTE": "safe"},
        resources={"cpus": 1.5, "memory_mib": 512, "pids": 128, "require_disk_limit": True, "disk_mib": 2048},
        setup={"cwd": "repo folder", "timeout_seconds": 60, "command": "echo 'ready'"})


def test_create_pins_limits_drops_capabilities_and_uses_isolated_network(provider, profile):
    network = provider.client.networks.create.return_value
    network.name = "isolated-network"
    container = provider.client.containers.create.return_value
    container.id = "container-id"
    container.attrs = {"HostConfig": {"Runtime": "runsc"}}
    state = provider.create("sandbox-id", "deployment-id", profile, with_setup_network=True)
    assert state["network"] == "isolated-network" and state["container_id"] == "container-id"
    options = provider.client.containers.create.call_args.kwargs
    assert options["cap_drop"] == ["ALL"] and options["security_opt"] == ["no-new-privileges:true"]
    assert options["user"] == "1000:1000" and options["nano_cpus"] == 1_500_000_000
    assert options["memswap_limit"] == options["mem_limit"] == "512m"
    assert options["pids_limit"] == 128 and options["storage_opt"] == {"size": "2048m"}
    assert options["platform"] == "linux/amd64" and options["environment"] == {"NOTE": "safe", "HOME": "/home/hubzoid"}
    assert provider.client.networks.create.call_args.kwargs["internal"] is True


@pytest.mark.parametrize(("message", "code"), [("storage unsupported", "resource_limit_unenforceable"), ("other error", None)])
def test_create_failure_cleans_network_and_reports_unenforceable_disk(provider, profile, message, code):
    provider.client.containers.create.side_effect = APIError(message)
    with pytest.raises(WorkItemError if code else APIError) as err:
        provider.create("sandbox-id", "deployment-id", profile, with_setup_network=True)
    if code:
        assert err.value.code == code and err.value.status == 422
    else:
        assert str(err.value) == message
    provider.client.networks.create.return_value.remove.assert_called_once()


def test_wrong_runtime_refuses_downgrade_and_removes_container(provider, profile):
    container = provider.client.containers.create.return_value
    container.attrs = {"HostConfig": {"Runtime": "runc"}}
    with pytest.raises(WorkItemError) as err:
        provider.create("sandbox-id", "deployment-id", profile, with_setup_network=True)
    assert err.value.code == "isolation_insufficient"
    container.remove.assert_called_once_with(force=True)
    provider.client.networks.create.return_value.remove.assert_called_once()
    container.start.assert_not_called()


def test_image_must_be_prepared_and_foreign_container_is_not_used(provider, profile):
    provider.client.images.get.side_effect = NotFound()
    with pytest.raises(WorkItemError) as err:
        provider.create("sandbox-id", "deployment-id", profile, with_setup_network=False)
    assert err.value.code == "image_unavailable"
    provider.client.containers.create.assert_not_called()
    container = provider.client.containers.get.return_value
    container.labels = {"hubzoid.deployment": "other", dp.LABEL: "other-box"}
    assert provider.container({"container_id": "container", "deployment": "expected"}, "sandbox-id") is None
    provider.client.containers.get.side_effect = NotFound()
    assert provider.container({"container_id": "container"}) is None


def test_setup_proxy_is_removed_even_when_setup_exec_fails(provider, profile, monkeypatch):
    container = Mock()
    container.exec_run.side_effect = RuntimeError("exec failed")
    monkeypatch.setattr(provider, "container", lambda state: container)
    proxy = Mock()
    monkeypatch.setattr(provider, "_start_proxy", lambda *a: (proxy, "10.0.0.2"))
    with pytest.raises(RuntimeError, match="exec failed"):
        provider.run_setup("sandbox-id", "deployment-id", {"network": "isolated"}, profile, hosts=["pypi.org"])
    proxy.remove.assert_called_once_with(force=True)
    options = container.exec_run.call_args.kwargs
    assert options["environment"]["HTTPS_PROXY"] == "http://10.0.0.2:3128"
    assert options["user"] == dp.USER and options["workdir"] == "/workspace"


@pytest.mark.parametrize("running", [True, False])
def test_setup_proxy_readiness_success_or_cleanup(provider, running):
    proxy = provider.client.containers.create.return_value
    proxy.status = "running" if running else "exited"
    proxy.logs.return_value = b"Accepting HTTP Socket connections" if running else b"proxy failed"
    proxy.attrs = {"NetworkSettings": {"Networks": {"isolated": {"IPAddress": "10.0.0.2"}}}}
    if running:
        assert provider._start_proxy("sandbox-id", "deployment-id", "isolated", ["pypi.org"]) == (proxy, "10.0.0.2")
    else:
        with pytest.raises(WorkItemError, match="proxy failed") as err:
            provider._start_proxy("sandbox-id", "deployment-id", "isolated", [])
        assert err.value.code == "setup_failed"
        proxy.remove.assert_called_once_with(force=True)
    provider.client.networks.get.return_value.connect.assert_called_once_with(proxy)


def test_delete_uses_deployment_labels_and_ignores_already_removed_resources(provider):
    container, network = Mock(), Mock()
    container.remove.side_effect = NotFound()
    network.remove.side_effect = APIError("already removed")
    provider.client.containers.list.return_value = [container]
    provider.client.networks.list.return_value = [network]
    assert provider.delete("sandbox-id", "deployment-id") == 1
    assert provider.client.containers.list.call_args.kwargs["filters"] == {"label": [
        "hubzoid.sandbox=sandbox-id", "hubzoid.deployment=deployment-id"]}


def test_write_tree_keeps_symlinks_without_reading_their_targets(provider, tmp_path, monkeypatch):
    source = tmp_path / "repo"
    source.mkdir()
    (source / "nested").mkdir()
    (source / "nested" / "file.txt").write_text("repo bytes")
    outside = tmp_path / "secret.txt"
    outside.write_text("private bytes")
    (source / "outside-link").symlink_to(outside)
    container = Mock()
    monkeypatch.setattr(provider, "container", lambda state: container)
    result = provider.write_tree({}, source, "/workspace/repo")
    assert result["files"] == 1 and result["bytes"] > 0
    destination, data = container.put_archive.call_args.args
    assert destination == "/workspace"
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        assert archive.getmember("repo/outside-link").issym()
        assert archive.extractfile("repo/nested/file.txt").read() == b"repo bytes"
        assert all(member.uid == member.gid == 1000 for member in archive.getmembers())
    assert b"private bytes" not in data


def test_command_output_demux_and_resource_metadata(provider, monkeypatch):
    container = Mock()
    container.exec_run.return_value = SimpleNamespace(exit_code=17, output=(b"output", b"error"))
    monkeypatch.setattr(provider, "container", lambda state: container)
    assert provider.run({}, ["echo", "a; b"], timeout=9) == {"exit_code": 17, "stdout": b"output", "stderr": b"error"}
    assert container.exec_run.call_args.args[0] == ["bash", "-lc", "timeout -k 5 9 echo 'a; b'"]
    container.id, container.labels = "container-id", {dp.LABEL: "sandbox-id", "hubzoid.role": "box"}
    container.attrs = {"Created": "2026-11-22T00:00:00Z"}
    network = SimpleNamespace(id="network-id", attrs={"Labels": {dp.LABEL: "sandbox-id"}, "Created": "invalid"})
    provider.client.containers.list.return_value = [container]
    provider.client.networks.list.return_value = [network]
    resources = provider.resources("deployment-id")
    assert resources[0]["sandbox_id"] == "sandbox-id" and resources[0]["created"] == 1795305600
    assert resources[1] == {"sandbox_id": "sandbox-id", "kind": "network", "id": "network-id", "role": "network", "created": None}
    assert dp._epoch("") is None
