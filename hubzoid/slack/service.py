"""systemd unit template for `hubzoid-slack@<hub>.service`.

Mirrors the existing `hubzoid@.service` shape: same user, same env-file
convention, but waits for the bridge to be up before starting.
"""
from __future__ import annotations

from pathlib import Path


_TEMPLATE = """\
[Unit]
Description=HubZoid Slack adapter for {hub_name}
After=hubzoid@{hub_name}.service network-online.target
Requires=hubzoid@{hub_name}.service

[Service]
Type=simple
User={user}
WorkingDirectory={hub_dir}
EnvironmentFile={hub_dir}/.env
ExecStart={python} -m hubzoid slack run {hub_dir}
Restart=always
RestartSec=3

# Security hardening (matches hubzoid@.service)
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths={hub_dir}

[Install]
WantedBy=multi-user.target
"""


def systemd_unit_for_hub(
    *,
    hub_dir: Path,
    python_path: Path,
    user: str = "hubzoid",
) -> str:
    """Render a systemd unit for the Slack adapter against this hub."""
    return _TEMPLATE.format(
        hub_name=hub_dir.name,
        hub_dir=str(hub_dir),
        python=str(python_path),
        user=user,
    )
