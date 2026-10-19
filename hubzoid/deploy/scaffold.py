"""The provider files `hubzoid deploy init` writes into a hub."""
from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from . import PROVIDERS

DEFAULT_REGISTRY = "ghcr.io/hubzoid/hubzoid"


class ScaffoldError(ValueError):
    pass


@dataclass
class Written:
    path: str            # relative to the hub
    action: str          # "created" | "unchanged" | "replaced"


def default_image() -> str:
    from .. import __version__

    return f"{DEFAULT_REGISTRY}:{__version__}"


def _template(*parts: str) -> str:
    return resources.files("hubzoid.deploy").joinpath("providers", *parts).read_text(encoding="utf-8")


def render(provider: str, image: str | None = None) -> dict[str, str]:
    """{hub-relative path: content} for ``provider``."""
    from .. import __version__

    if provider not in PROVIDERS:
        raise ScaffoldError(f"Unknown provider {provider!r}: use {' or '.join(PROVIDERS)}.")
    values = {"{{provider}}": provider, "{{image}}": image or default_image(), "{{version}}": __version__}

    def fill(text: str) -> str:
        for key, value in values.items():
            text = text.replace(key, value)
        return text

    return {
        "Dockerfile": fill(_template("common", "Dockerfile")),
        ".dockerignore": fill(_template("common", "dockerignore")),
        f"deploy/{provider}/README.md": fill(_template(provider, "README.md")),
        f"deploy/{provider}/env.example": fill(_template(provider, "env.example")),
    }


def write(hub_dir: Path, provider: str, *, image: str | None = None, force: bool = False) -> list[Written]:
    """Write the provider files into ``hub_dir``. Refuses, before writing
    anything, to replace a file that differs unless ``force``."""
    hub_dir = Path(hub_dir)
    if not (hub_dir / "AGENTS.md").is_file():
        raise ScaffoldError(f"{hub_dir} is not a hub (no AGENTS.md).")
    files = render(provider, image)
    plan: list[Written] = []
    clashes: list[str] = []
    for rel, text in files.items():
        target = hub_dir / rel
        if target.is_symlink():
            raise ScaffoldError(f"{rel} is a symbolic link. Remove it first.")
        if not target.exists():
            plan.append(Written(rel, "created"))
        elif target.read_text(encoding="utf-8", errors="replace") == text:
            plan.append(Written(rel, "unchanged"))
        else:
            plan.append(Written(rel, "replaced"))
            clashes.append(rel)
    if clashes and not force:
        raise ScaffoldError("These files already exist and differ: " + ", ".join(clashes)
                            + ". Pass --force to replace them.")
    for item in plan:
        if item.action != "unchanged":
            target = hub_dir / item.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(files[item.path], encoding="utf-8")
    return plan
