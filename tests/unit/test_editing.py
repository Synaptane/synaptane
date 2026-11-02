"""What the writing tools tell the assistant: the closest passage to a misquoted `old`, the
lines of each occurrence, the change saved."""

from synaptane.mcp import editing

PAGE = (
    "# Projet\n\n## Tarifs\n\nLe forfait mensuel coûte 49 euros.\n"
    "Le forfait annuel coûte 490 euros.\n\n## Suite\n\nRien.\n"
)


def test_the_closest_passage_is_found_line_for_line() -> None:
    assert editing.closest(PAGE, "Le forfait mensuel coute 50 euros.") == (
        5,
        5,
        "Le forfait mensuel coûte 49 euros.",
    )
    two = editing.closest(PAGE, "Le forfait mensuel coûte 49 €.\nLe forfait annuel coûte 490 €.")
    assert two is not None and two[:2] == (5, 6)
    assert editing.closest(PAGE, "Une phrase sans aucun rapport avec la page") is None
    assert editing.closest("", "x") is None


def test_occurrences_give_their_lines() -> None:
    assert editing.lines_of(PAGE, "Le forfait") == [5, 6]
    assert editing.lines_of(PAGE, "absent") == []


def test_the_change_is_a_short_diff() -> None:
    after = PAGE.replace("49 euros", "45 euros")
    text = editing.change(PAGE, after)
    assert text.startswith("@@") and "---" not in text
    assert "-Le forfait mensuel coûte 49 euros." in text
    assert "+Le forfait mensuel coûte 45 euros." in text
    assert "Rien." not in text  # two lines of context only
    assert editing.change(PAGE, PAGE) == ""
    long = editing.change("", "\n".join(f"ligne {n}" for n in range(2_000)))
    assert len(long) < editing.CHANGE + 100 and long.endswith("more lines: diff gives all]")
