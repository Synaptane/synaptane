"""The image says what it contains: version and commit."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_image_version_label_follows_pyproject() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    match = re.search(r"^ARG VERSION=(\S+)$", dockerfile, re.MULTILINE)
    assert match and match[1] == project["version"], "ARG VERSION du Dockerfile à mettre à jour"
    assert 'org.opencontainers.image.revision="${GIT_SHA}"' in dockerfile
    assert "ENV SYNAPTANE_GIT_SHA=${GIT_SHA}" in dockerfile


def test_compose_passes_the_commit_to_the_build() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "GIT_SHA: ${GIT_SHA:-unknown}" in compose


def test_the_image_ships_the_licenses() -> None:
    """Apache-2.0 and third-party notices: copied into the image with the package (dist-info)."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    allowed = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    for name in project["license-files"]:
        assert (ROOT / name).is_file()
        assert f"!{name}" in allowed, f"{name} manque à .dockerignore"
        assert name in dockerfile, f"{name} n'est pas copié dans l'image"
