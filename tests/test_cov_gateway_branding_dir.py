"""Which branding folder a gateway uses (webapp_gateway.gateway_branding_dir),
in order: the folder the gateway recorded, HUBZOID_GATEWAY_BRANDING naming a
hub slug or a folder, the gateway's own data folder when it holds a brand
asset, the first hub's, and none without hubs."""
from __future__ import annotations

from pathlib import Path

from hubzoid.webapp_gateway import gateway_branding_dir

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def hubs(tmp_path):
    out = []
    for name in ("Sales", "Ops"):
        (tmp_path / name).mkdir()
        out.append({"path": str(tmp_path / name)})
    return out


def test_the_precedence(tmp_path):
    entries = hubs(tmp_path)
    data = tmp_path / "gateway"
    (data / "branding").mkdir(parents=True)
    assert gateway_branding_dir({"branding_dir": "/srv/brand", "hubs": entries}, env={}) == Path("/srv/brand")
    assert gateway_branding_dir({"hubs": entries}, env={"HUBZOID_GATEWAY_BRANDING": "ops"}) == tmp_path / "Ops" / "branding"
    assert gateway_branding_dir({"hubs": [{**entries[0], "slug": "north"}]},
                                env={"HUBZOID_GATEWAY_BRANDING": "north"}) == tmp_path / "Sales" / "branding"
    assert gateway_branding_dir({"hubs": entries}, env={"HUBZOID_GATEWAY_BRANDING": str(data)}) == data / "branding"
    # An override that names nothing falls through to the hubs.
    assert gateway_branding_dir({"hubs": entries}, env={"HUBZOID_GATEWAY_BRANDING": "nowhere"}) == tmp_path / "Sales" / "branding"
    # The gateway's own folder only when it holds a brand asset (a README is not one).
    (data / "branding" / "README.md").write_text("notes")
    assert gateway_branding_dir({"hubs": entries, "_dir": str(data)}, env={}) == tmp_path / "Sales" / "branding"
    (data / "branding" / "logo.png").write_bytes(PNG)
    assert gateway_branding_dir({"hubs": entries, "_dir": str(data)}, env={}) == data / "branding"
    assert gateway_branding_dir({"hubs": []}, env={}) is None
