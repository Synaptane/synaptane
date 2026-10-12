from pathlib import Path

import pytest

from synaptane.config import ConfigError, load_settings
from synaptane.core import names
from synaptane.core.errors import SynaptaneError


@pytest.mark.parametrize("value", ["acme", "infra-2026", "a", "x" * 64])
def test_valid_slugs(value: str) -> None:
    assert names.slug(value) == value


@pytest.mark.parametrize("value", ["", "Acme", "-acme", "acme-", "a--b", "a_b", "x" * 65, "admin"])
def test_invalid_slugs(value: str) -> None:
    with pytest.raises(SynaptaneError):
        names.slug(value)


def test_username_is_lowercased() -> None:
    assert names.username(" Rene.Cotton ") == "rene.cotton"


def test_config_file_with_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "synaptane.toml"
    config.write_text('database_url = "postgresql+psycopg://a@b/c"\ndata_dir = "data"\n')
    monkeypatch.setenv("SYNAPTANE_PUBLIC_URL", "https://brain.example.com/")
    settings = load_settings(config)
    assert settings.data_dir == tmp_path / "data"
    assert settings.public_url == "https://brain.example.com"
    assert settings.secure_cookies
    assert settings.default_locale == "en"  # English, unless configured otherwise


def test_default_locale_is_configurable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "synaptane.toml"
    config.write_text('database_url = "postgresql+psycopg://a@b/c"\ndata_dir = "data"\n')
    monkeypatch.setenv("SYNAPTANE_DEFAULT_LOCALE", "fr")
    assert load_settings(config).default_locale == "fr"
    monkeypatch.setenv("SYNAPTANE_DEFAULT_LOCALE", "de")
    with pytest.raises(ConfigError, match="default_locale"):
        load_settings(config)


def test_config_errors_do_not_leak_values(tmp_path: Path) -> None:
    config = tmp_path / "synaptane.toml"
    config.write_text('database_url = "postgresql://user:hunter2@db/x"\npublic_url = "ftp://x"\n')
    with pytest.raises(ConfigError) as error:
        load_settings(config)
    assert "hunter2" not in str(error.value)
    assert "data_dir" in str(error.value)


def test_inbox_file_names_keep_letters_without_accents() -> None:
    """Regression: "Documenter le retour arrière" gave `…-retour-arri-re-…`."""
    from datetime import UTC, datetime

    from synaptane.core.names import ascii_text
    from synaptane.services.inbox import item_name

    assert ascii_text("Œuvre déjà Façade Straße") == "oeuvre deja facade strasse"
    name = item_name(datetime(2026, 10, 5, tzinfo=UTC), "note", "Documenter le retour arrière")
    assert "-note-documenter-le-retour-arriere-" in name


def test_file_types_for_icons() -> None:
    """Tree icon: the type is read from the extension (case-insensitive)."""
    from synaptane.web.pages import _file_type

    cases = {
        "notes/cr.md": "md",
        "deck.HTML": "html",
        "contrat.pdf": "pdf",
        "photo.jpeg": "image",
        "lettre.docx": "doc",
        "budget.csv": "sheet",
        "pitch.pptx": "slides",
        "conf.yaml": "code",
        "Dockerfile": "code",
        "lisez-moi.txt": "text",
        "sources.zip": "archive",
        "rdv.mp3": "media",
        "sans-extension": "file",
        "v2.0/fichier": "file",
    }
    assert {name: _file_type(name) for name in cases} == cases
