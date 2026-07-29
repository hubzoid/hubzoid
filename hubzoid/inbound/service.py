"""systemd unit template for `hubzoid-inbound@<hub>.service`.

Mirrors `hubzoid-slack@.service`: same user and env-file convention, waits for
the bridge to be up before starting. Serves the WhatsApp/Telegram webhook app;
put `/webhooks/*` behind your reverse proxy to this hub's inbound port.
"""
from __future__ import annotations

from pathlib import Path

_TEMPLATE = """\
[Unit]
Description=HubZoid inbound surfaces (WhatsApp/Telegram) for {hub_name}
After=hubzoid@{hub_name}.service network-online.target
Requires=hubzoid@{hub_name}.service

[Service]
Type=simple
User={user}
WorkingDirectory={hub_dir}
EnvironmentFile={hub_dir}/.env
ExecStart={python} -m hubzoid inbound run {hub_dir}
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


def systemd_unit_for_hub(*, hub_dir: Path, python_path: Path, user: str = "hubzoid") -> str:
    """Render a systemd unit for the inbound surfaces against this hub."""
    return _TEMPLATE.format(
        hub_name=hub_dir.name, hub_dir=str(hub_dir), python=str(python_path), user=user,
    )
