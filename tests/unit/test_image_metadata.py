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
    build = (ROOT / "compose.build.yaml").read_text(encoding="utf-8")
    assert "GIT_SHA: ${GIT_SHA:-unknown}" in build


def semver(version: str) -> str:
    """pyproject.toml version (PEP 440, 1.0.0rc1) as the image tag (semver, 1.0.0-rc.1)."""
    match = re.fullmatch(r"(\d+\.\d+\.\d+)(?:(a|b|rc)(\d+))?", version)
    assert match, f"{version}: expected X.Y.Z, X.Y.ZaN, X.Y.ZbN or X.Y.ZrcN"
    release, pre, number = match.groups()
    if not pre:
        return release
    label = {"a": "alpha", "b": "beta", "rc": "rc"}[pre]
    return f"{release}-{label}.{number}"


def test_compose_pulls_the_version_of_the_repository() -> None:
    """No `latest` (absent for a pre-release) and no build in compose.yaml: by default, the
    published image of the version that the repository describes."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    expected = semver(project["version"])
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    defaults = re.findall(r"\$\{SYNAPTANE_VERSION:-([^}]*)\}", compose)
    assert defaults and set(defaults) == {expected}, "compose.yaml : SYNAPTANE_VERSION à monter"
    assert not re.search(r"^\s*build:", compose, re.MULTILINE), "build: va dans compose.build.yaml"
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert re.findall(r"^SYNAPTANE_VERSION=(\S+)$", env, re.MULTILINE) == [expected]
    for doc in (
        "README.md",
        "README.fr.md",
        "docs/admin/installation.md",
        "docs/admin/installation.fr.md",
    ):
        text = (ROOT / doc).read_text(encoding="utf-8")
        tags = set(re.findall(r"git clone --branch v(\S+)", text))
        assert tags == {expected}, f"{doc} : version à cloner à mettre à jour ({tags})"


def test_the_image_ships_the_licenses() -> None:
    """Apache-2.0 and third-party notices: copied into the image with the package (dist-info)."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    allowed = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    for name in project["license-files"]:
        assert (ROOT / name).is_file()
        assert f"!{name}" in allowed, f"{name} manque à .dockerignore"
        assert name in dockerfile, f"{name} n'est pas copié dans l'image"


def test_postgres_image_is_the_same_everywhere() -> None:
    """Production, development and CI test the same PostgreSQL, pinned by digest."""
    images = {
        name: re.findall(r"pgvector/pgvector:\S+", (ROOT / name).read_text(encoding="utf-8"))
        for name in ("compose.yaml", "compose.dev.yaml", ".github/workflows/ci.yml")
    }
    found = {image for names in images.values() for image in names}
    assert all(images.values()) and len(found) == 1, images
    assert "@sha256:" in found.pop()
