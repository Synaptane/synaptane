"""Three-way merge of a page modified during tidying."""

from synaptane.services.tidy_commit import merge_text

BASE = b"# Titre\n\nun\ndeux\ntrois\n\nfin\n"


def test_changes_far_apart_are_merged() -> None:
    theirs = BASE.replace(b"# Titre", b"# Nouveau titre")
    ours = BASE + b"\nVoir aussi [x](x.md).\n"
    assert merge_text(BASE, theirs, ours) == theirs + b"\nVoir aussi [x](x.md).\n"


def test_the_same_line_changed_twice_is_a_conflict() -> None:
    assert merge_text(BASE, BASE.replace(b"deux", b"2"), BASE.replace(b"deux", b"II")) is None


def test_deletion_against_modification_and_binaries_are_conflicts() -> None:
    assert merge_text(BASE, None, BASE + b"x\n") is None  # deleted by a human
    assert merge_text(BASE, BASE + b"x\n", None) is None  # deleted by the agent
    assert merge_text(b"\0PDF", b"\0PDF2", b"\0PDF3") is None


def test_two_creations_merge_only_when_compatible() -> None:
    assert merge_text(None, b"# A\n", b"# A\n") == b"# A\n"
    assert merge_text(None, b"# A\n", b"# B\n") is None
