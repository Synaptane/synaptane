"""Commit subjects written by Synaptane are shown as labels, in English and in the French of
histories written before the code was translated."""

from html import unescape

import pytest

from synaptane.web.i18n import translator
from synaptane.web.pages import templates


def label(subject: str) -> str:
    template = templates.env.from_string("{{ subject|commit_label }}")
    rendered = template.render(subject=subject, _=lambda text, **values: text.format(**values))
    return unescape(rendered)  # autoescape: the apostrophe comes out as &#39;


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Inbox: Wi-Fi password policy", "Note dropped: Wi-Fi password policy"),
        ("Inbox: edit “NAS”", "Note edited: NAS"),
        ("Inbox: “NAS” moved to acme/it", "Note moved to acme/it: NAS"),
        ("Synaptane: initialize space", "Space initialization"),
        ("Synaptane: merge remote changes", "Remote repository merged"),
        ("Inbox : modification de « NAS »", "Note edited: NAS"),
        ("Synaptane : initialisation de l'espace", "Space initialization"),
        ("Tidy-up: 3 item(s) filed", "Tidying run"),
        ("Rangement : 3 éléments rangés", "Tidying run"),
        ("Fix the backup page", "Fix the backup page"),
    ],
)
def test_commit_subjects_get_a_label(subject: str, expected: str) -> None:
    assert label(subject) == expected  # English source text (`_` returns it as is)


def test_commit_labels_are_translated() -> None:
    template = templates.env.from_string("{{ subject|commit_label }}")
    rendered = template.render(subject="Inbox: edit “NAS”", _=translator("fr"))
    assert rendered == "Note modifiée\u00a0: NAS"
