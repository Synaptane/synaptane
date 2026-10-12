"""Authors of Synaptane's own commits: English names, earlier French names still recognized in
histories written before the code was translated."""

import pytest

from synaptane.services import activity, git_sync, inbox


@pytest.mark.parametrize(
    ("author", "expected"),
    [
        ("Synaptane tidy-up", True),
        ("Synaptane – rangement", True),  # before the translation
        ("Synaptane", False),
        ("Alice", False),
    ],
)
def test_tidy_commits_are_recognized_under_both_names(author: str, expected: bool) -> None:
    assert inbox.is_tidy_agent(author) is expected


def test_tidy_commits_are_classified_as_tidy_runs() -> None:
    pages = ("content/guide.md",)
    assert activity._kind("Synaptane tidy-up", pages, "Synaptane") == "tidy"
    assert activity._kind("Synaptane – rangement", pages, "Synaptane") == "tidy"
    assert activity._kind("Alice", pages, "Synaptane") == "page"
    assert activity._kind("Synaptane", (".synaptane/inbox/x.md",), "Synaptane") == "system"


def test_author_names_are_in_english() -> None:
    assert inbox.AGENT_NAME == "Synaptane tidy-up"
    assert git_sync.RECOVERY_AUTHOR == "Synaptane recovery"
