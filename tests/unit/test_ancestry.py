"""Does this decision cover that commit? Answered without one Git call per decision, merges
included."""

from synaptane.services.changes import _Ancestry

# M merges B and C, both children of A; A's parent is the watermark (outside the graph).
GRAPH = _Ancestry([("M", ("B", "C")), ("C", ("A",)), ("B", ("A",)), ("A", ("W",))])


def test_ancestors_through_a_merge() -> None:
    assert GRAPH.reaches("M", "M")
    assert GRAPH.reaches("M", "A") and GRAPH.reaches("M", "C") and GRAPH.reaches("B", "A")


def test_siblings_descendants_and_unknown_commits_are_not_reached() -> None:
    assert not GRAPH.reaches("B", "C") and not GRAPH.reaches("C", "B")
    assert not GRAPH.reaches("A", "B")  # page modified after the decision
    assert not GRAPH.reaches("W", "A")  # decision at the watermark or before: out of range
    assert not GRAPH.reaches("M", "W")
