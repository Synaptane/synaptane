from datetime import UTC, datetime

from synaptane.git.repo import Identity
from synaptane.services.inbox import note_item, parse_front_matter

AT = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
ME = Identity("René", "rene@example.com")


def test_note_roundtrip_and_single_heading() -> None:
    path, data = note_item(ME, "mcp", "Titre", "Corps", "infra", ["a.pdf"], AT)
    assert path.startswith(".synaptane/inbox/2026-10-03T12-00-00Z-note-titre-")
    fields, body = parse_front_matter(data.decode())
    assert fields["title"] == "Titre" and fields["attachments"] == ["a.pdf"]
    assert body == "Corps\n"  # the title is in the header, not repeated in the body
    _, own = note_item(ME, "mcp", "Titre", "# Son propre titre\n\nCorps", "", [], AT)
    assert parse_front_matter(own.decode())[1].count("# ") == 1


def test_front_matter_tolerates_plain_yaml() -> None:
    fields, body = parse_front_matter("---\ntype: note\ntitle: 'Écrit à la main'\n---\nTexte")
    assert fields == {"type": "note", "title": "Écrit à la main"} and body == "Texte"
