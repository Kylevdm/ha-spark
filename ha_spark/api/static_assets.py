"""Package-owned ingress assets and their integrity manifest.

Regenerate after an asset change with ``python -m ha_spark.api.static_assets``.
The static directory is a resource directory, not a Python package.
"""

from hashlib import sha256
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

MANIFEST = "MANIFEST.sha256"


def asset_names(root: Traversable) -> list[str]:
    """List every resource file, relative to the asset root, in path order."""
    names: list[str] = []
    for child in root.iterdir():
        if child.is_dir():
            names.extend(f"{child.name}/{name}" for name in asset_names(child))
        else:
            names.append(child.name)
    return sorted(names)


def manifest_text(root: Traversable) -> str:
    """Hash every asset except the manifest itself, including nested resources."""
    return "".join(
        f"{sha256(root.joinpath(name).read_bytes()).hexdigest()}  {name}\n"
        for name in asset_names(root) if name != MANIFEST
    )


def packaged_assets() -> dict[str, bytes]:
    """Read only the explicitly manifested files (and the manifest) for serving."""
    root = files("ha_spark.api").joinpath("static")
    names = [line.split("  ", 1)[1] for line in root.joinpath(MANIFEST).read_text().splitlines()]
    return {name: root.joinpath(name).read_bytes() for name in [*names, MANIFEST]}


if __name__ == "__main__":
    root = Path(__file__).with_name("static")
    root.joinpath(MANIFEST).write_text(manifest_text(root))
