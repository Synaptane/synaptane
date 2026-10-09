"""Secret detection: real ones are found, ordinary sentences pass."""

import pytest

from synaptane.core.errors import ErrorCode, Label, SynaptaneError
from synaptane.core.secret_scan import added_findings, refuse_secrets, scan_file, scan_text
from synaptane.web.i18n import translator

SECRETS = [
    "password: hunter22",
    "mdp = P@ssw0rd!",
    'api_key: "a1b2c3d4e5f6"',
    "DATABASE_URL=postgresql://synaptane:s3cr3t-Pw@db:5432/x",
    "AKIAIOSFODNN7EXAMPLE",
    "ghp_" + "a" * 36,
    "glpat-" + "c" * 20,
    "sk-ant-api03-" + "b" * 30,
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
]
ORDINARY = [
    "Mot de passe : voir le coffre Bitwarden",
    "Mot de passe: stocké dans Vault",
    "postgresql://user:password@host/db",
    "postgresql://user:${PGPASSWORD}@host/db",
    "token: <votre jeton>",
    "api_key = ${API_KEY}",
    "secret: ********",
    "Le token d'accès expire après 1 heure.",
    "Le secret = partager tôt.",
    "https://example.com/a:b@c",
]


@pytest.mark.parametrize("text", SECRETS)
def test_secrets_are_found(text: str) -> None:
    assert scan_text(f"# Notes\n\n{text}\n")[0].line == 3


@pytest.mark.parametrize("text", ORDINARY)
def test_ordinary_text_passes(text: str) -> None:
    assert scan_text(text) == []


def test_files_and_added_lines() -> None:
    assert scan_file(".env", b"X=1")[0].kind == "secrets file"
    assert scan_file("photo.png", b"\x89PNG\0\0password: hunter22") == []
    old = b"# Serveur\npassword: hunter22\n"
    assert added_findings("s.md", old + b"Ajout sans secret\n", old) == []  # already there
    assert added_findings("s.md", old + b"token = Zx9-abcdefgh\n", old)[0].line == 3
    with pytest.raises(SynaptaneError) as refused:
        refuse_secrets(scan_text("password: hunter22", Label("text")))
    assert refused.value.code is ErrorCode.SECRET
    assert "password or key (text, line 1)" in refused.value.message
    # Shown in the reader's language, the details included.
    french = translator("fr")(refused.value.template, **refused.value.params)
    assert "mot de passe ou clé (texte, ligne 1)" in french


ASSISTANT_STYLE = [
    "**Mot de passe** : `Tr0ub4dor&3xKq9`",
    "| Mot de passe | Tr0ub4dor&3xKq9 |",
    "Le mot de passe root est Tr0ub4dor&3xKq9!",
    "Mot de passe Wi-Fi : Xy12zz!!wifi",
    "**MDP** : Tr0ub4dor&3xKq9",
    "export DB_PASSWORD=Tr0ub4dor3xKq9",
    "password is Tr0ub4dor3xKq9",
    "Clé API Mistral : a8F3kLmN9pQ2rS5tU7vW1xY4zB6cD0eF",
    "hf_" + "A" * 34,
    "- mot de passe: `Tr0ub4dor&3xKq9`",
    "Mot de passe : Correct#Horse&Battery",  # no digit, but long and mixed
]
ASSISTANT_ORDINARY = [
    # Notes about AI, where "token" is everywhere (false alarms seen when importing a vault).
    "- Mission : cadrage AI FinOps, suivi des tokens multi-cloud et des coûts d'inférence",
    "- **Actu IA** : Token FM (80€/mois), l'abonnement le plus complet",
    "Le secret de fabrication : l'expérience client",
    "Les tokens de GPT-4o-mini coûtent moins cher que ceux de Claude",
    "Jeton expiré le 2026-09-28T10:00Z, renouvelé depuis",
    "Clés API : Kubernetes/Docker et Terraform, gérées par l'équipe plateforme",
    "Mot de passe : changer-le-tous-les-90-jours",
    "Le mot de passe est dans https://vault.example.com/secret/42",
    "Le jeton est renouvelé automatiquement toutes les 24h.",
    "Voir la page ../securite/mots-de-passe.md",
]


@pytest.mark.parametrize("text", ASSISTANT_STYLE)
def test_assistant_formats_are_caught(text: str) -> None:
    assert scan_text(text), text


@pytest.mark.parametrize("text", ASSISTANT_ORDINARY)
def test_assistant_prose_passes(text: str) -> None:
    assert scan_text(text) == [], text


def test_a_file_cut_inside_a_character_is_still_scanned() -> None:
    from synaptane.core.secret_scan import MAX_SCAN, scan_file

    key = b"AWS_SECRET=AKIAIOSFODNN7EXAMPLE\n"
    data = key + b"a" * (MAX_SCAN - len(key) - 1) + "é".encode() + b"suite"
    assert scan_file("notes.md", data)
