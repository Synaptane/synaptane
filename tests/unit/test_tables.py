"""Markdown tables and the drifts between aligned tables of two pages."""

from synaptane.core import tables

INVENTORY = """# Serveurs

| Machine | RAM | Disque |
|---|---|---|
| NAS | 8 Go | 12 To |
| PVE | 64 Go | 2 To |
| `routeur` | 1 Go \\| 2 Go | — |

```
| Pas | un tableau |
|-----|------------|
```
"""


def test_tables_are_read_outside_code() -> None:
    [table] = tables.tables_of(INVENTORY)
    assert table.line == 3 and table.headings == ("Machine", "RAM", "Disque")
    assert [line for line, _ in table.rows] == [5, 6, 7]
    assert table.rows[2][1] == ("`routeur`", "1 Go | 2 Go", "—")  # escaped pipe kept
    assert tables.plain("**[NAS](nas.md)**  Synology") == "nas synology"
    assert tables.plain("[[serveurs/pve|PVE]]") == "pve"


def test_a_drift_needs_aligned_tables_that_mostly_agree() -> None:
    other = "| machine | Disque |\n|--|--|\n| nas | 8 To |\n| pve | 2 To |\n| routeur | ? |\n"
    [drift] = tables.drifts({"a.md": tables.tables_of(INVENTORY), "b.md": tables.tables_of(other)})
    assert (drift.key, drift.column) == ("NAS", "Disque")
    assert (drift.first.line, drift.first.value) == (5, "12 To")
    assert (drift.second.page, drift.second.line, drift.second.value) == ("b.md", 3, "8 To")

    # More disagreements than agreements: two sheets of different things.
    sheet = "| Donnée | Valeur |\n|--|--|\n| Siège | {0} |\n| Création | {1} |\n"
    two = {"a.md": sheet.format("Paris", "2018"), "b.md": sheet.format("Lyon", "2020")}
    assert tables.drifts({p: tables.tables_of(t) for p, t in two.items()}) == []
    # Dated notes: each holds on its day.
    dated = {"veille-2026-11-01.md": INVENTORY, "veille-2026-11-02.md": other}
    assert tables.drifts({p: tables.tables_of(t) for p, t in dated.items()}) == []
    # One shared row is not enough; row numbers are not keys.
    numbered = "| # | Disque |\n|--|--|\n| 1 | 8 To |\n| 2 | 2 To |\n"
    alone = "| Machine | Disque |\n|--|--|\n| NAS | 8 To |\n| Autre | 1 To |\n"
    found = {"a.md": INVENTORY, "b.md": alone, "c.md": numbered, "d.md": numbered + "\n"}
    assert tables.drifts({p: tables.tables_of(t) for p, t in found.items()}) == []
