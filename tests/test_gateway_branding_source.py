"""GatewayPlan.branding_source — where the shared OWUI chrome comes from.

Priority: HUBZOID_GATEWAY_BRANDING override (slug or path) -> populated
<gw_data>/branding -> first hub -> gw_data fallback.
"""
from __future__ import annotations

from pathlib import Path

from hubzoid import gateway


def _plan(*hubs: tuple[Path, str]) -> gateway.GatewayPlan:
    backends = tuple(
        gateway.GatewayBackend(
            hub_dir=hd, slug=slug, bridge_port=8000 + i, api_key="k",
            model_label=f"m{i}",
        )
        for i, (hd, slug) in enumerate(hubs)
    )
    return gateway.GatewayPlan(backends=backends)


def test_defaults_to_first_hub(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    plan = _plan((a, "a"), (b, "b"))
    # gw_data has no branding/ -> the first hub is the source.
    assert plan.branding_source(tmp_path / "gw") == a


def test_populated_gw_data_wins_over_first_hub(tmp_path):
    a = tmp_path / "a"; a.mkdir()
    plan = _plan((a, "a"))
    gw = tmp_path / "gw"
    (gw / "branding").mkdir(parents=True)
    (gw / "branding" / "favicon.png").write_bytes(b"x")
    assert plan.branding_source(gw) == gw


def test_gw_data_with_only_readme_is_not_a_source(tmp_path):
    a = tmp_path / "a"; a.mkdir()
    plan = _plan((a, "a"))
    gw = tmp_path / "gw"
    (gw / "branding").mkdir(parents=True)
    (gw / "branding" / "README.md").write_text("# just docs")
    # No real asset -> fall through to the first hub.
    assert plan.branding_source(gw) == a


def test_override_by_hub_slug(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    plan = _plan((a, "a"), (b, "b"))
    assert plan.branding_source(tmp_path / "gw", override="b") == b


def test_override_by_path(tmp_path):
    a = tmp_path / "a"; a.mkdir()
    custom = tmp_path / "custom"; custom.mkdir()
    plan = _plan((a, "a"))
    assert plan.branding_source(tmp_path / "gw", override=str(custom)) == custom


def test_bad_override_falls_back_to_first_hub(tmp_path):
    a = tmp_path / "a"; a.mkdir()
    plan = _plan((a, "a"))
    # Unknown slug and nonexistent path both fall back.
    assert plan.branding_source(tmp_path / "gw", override="nope") == a


def test_blank_override_ignored(tmp_path):
    a = tmp_path / "a"; a.mkdir()
    plan = _plan((a, "a"))
    assert plan.branding_source(tmp_path / "gw", override="   ") == a


def test_no_backends_falls_back_to_gw_data(tmp_path):
    plan = _plan()  # empty gateway (degenerate)
    gw = tmp_path / "gw"
    assert plan.branding_source(gw) == gw
