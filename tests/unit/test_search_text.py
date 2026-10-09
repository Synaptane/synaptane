"""Search, text side: folding of accents and ligatures, stems, highlighting, excerpts."""

import shutil
import subprocess

import pytest

from synaptane.core.markdown import outline
from synaptane.services.search import (
    Query,
    Snippet,
    _Candidate,
    _passage,
    fold,
    headings,
    readable,
    stem,
    term_pattern,
)
from synaptane.web.wiki import highlight_terms


def test_fold_removes_accents_and_ligatures() -> None:
    assert fold("Nœuds, CŒUR, ex æquo, Straße, État, l’été") == (
        "noeuds, coeur, ex aequo, strasse, etat, l'ete"
    )
    assert Query.parse("Nœuds").terms == Query.parse("noeuds").terms == ("noeuds",)


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep absent")
@pytest.mark.parametrize(
    ("typed", "written"),
    [
        ("noeuds", "Les nœuds du cluster"),
        ("nœuds", "les noeuds du cluster"),
        ("coeur", "Au CŒUR du sujet"),
        ("Etat", "État des lieux"),
        ("aequo", "ex æquo"),
        ("strasse", "Hauptstraße"),
        ("sauvegarder", "La sauvegarde nocturne"),
        ("sauvegarde", "Sauvegardées chaque nuit"),
        ("installation", "Installer le paquet"),
        ("reseaux", "le réseau du bureau"),
        ("deploying", "deployed on Monday"),
    ],
)
def test_the_pattern_finds_every_spelling_in_the_raw_text(typed: str, written: str) -> None:
    """ripgrep reads the raw text: the pattern is what carries every spelling."""
    [term] = Query.parse(typed).terms
    found = subprocess.run(
        ["rg", "--ignore-case", "--count", "-e", term_pattern(term)],
        input=written.encode(),
        capture_output=True,
    )
    assert found.stdout.strip() == b"1", (typed, written, term_pattern(term))


def test_stems_are_cautious_on_short_words() -> None:
    assert stem("sauvegarder") == stem("sauvegarde") == stem("sauvegardees") == "sauvegard"
    assert stem("configuration") == stem("configurer") == "configur"
    assert stem("serveurs") == "serveur" and stem("notes") == "note"
    # Short words: nothing moves ("porter" must not find "portail").
    assert stem("porter") == "porter" and stem("art") == "art" and stem("etat") == "etat"
    assert stem("d'acces") == "d'acces"  # not a word: left as is
    assert term_pattern("art").startswith(r"\b")  # 3 lettres : mot entier


def test_highlight_never_breaks_entities_nor_nests_marks() -> None:
    """Positions computed on the raw text, merged, then escaped separately."""
    text = 'Budget R&D amp "quot" <b>gras</b>'
    assert str(highlight_terms(text, ["amp"])) == (
        "Budget R&amp;D <mark>amp</mark> &#34;quot&#34; &lt;b&gt;gras&lt;/b&gt;"
    )
    assert "<mark>" not in str(highlight_terms(text, ["34"]))  # not inside "&#34;"
    assert str(highlight_terms("zzhl mark", ["zzhl", "mark"])) == (
        "<mark>zzhl</mark> <mark>mark</mark>"
    )
    # Nested or adjacent terms: a single highlight, never a <mark> inside a <mark>.
    assert str(highlight_terms("Proxmox", ["proxmox", "prox"])) == "<mark>Proxmox</mark>"
    assert str(highlight_terms("zzhlmark", ["zzhl", "mark"])) == "<mark>zzhlmark</mark>"


def test_highlight_keeps_the_original_spelling() -> None:
    assert str(highlight_terms("Les nœuds, NŒUDS", ["noeuds"])) == (
        "Les <mark>nœuds</mark>, <mark>NŒUDS</mark>"
    )
    decomposed = "État des lieux"  # "É" written as two characters (NFD)
    assert str(highlight_terms(decomposed, ["etat"])) == "<mark>État</mark> des lieux"
    # Stem: the whole word is highlighted, whatever its ending.
    assert str(highlight_terms("Sauvegardées hier", ["sauvegarder"])) == (
        "<mark>Sauvegardées</mark> hier"
    )


def test_readable_lines() -> None:
    assert readable("| pve1 | 10.0.0.11 | calcul |") == "pve1 · 10.0.0.11 · calcul"
    assert readable("|---|:---:|---|") == "" and readable("```bash") == ""
    assert readable("~~~") == "" and readable("---") == "" and readable("   ") == ""
    assert readable("## La **sauvegarde** [ici](x.md) `vzdump` ##") == ("La sauvegarde ici vzdump")
    assert readable("- [x] voir [[guides/vpn|le guide]]") == "voir le guide"
    assert readable("> citée") == "citée"
    assert readable("# pas un titre", is_markdown=False) == "# pas un titre"


def test_passage_is_one_readable_block() -> None:
    lines = {
        5: ("## Nœuds", False),
        6: ("", False),
        7: ("| Nœud | IP | Rôle |", False),
        8: ("|---|---|---|", False),
        9: ("| pve1 | 10.0.0.11 | calcul |", True),
        10: ("| pve2 | 10.0.0.12 | calcul |", False),
        30: ("```bash", False),
        31: ("vzdump --all  # pve1", True),
        32: ("```", False),
    }
    entry = _Candidate("cluster.md", lines=lines)
    snippets = [Snippet(9, lines[9][0]), Snippet(31, lines[31][0])]
    assert _passage(entry, snippets) == [
        "Nœud · IP · Rôle",  # the table rule is skipped without marking a gap
        "pve1 · 10.0.0.11 · calcul",
        "pve2 · 10.0.0.12 · calcul",
        "…",
        "vzdump --all # pve1",  # neither "bash" nor the closing fence of the block
    ]


def test_heading_anchors_are_those_of_the_rendering() -> None:
    text = (
        "---\ntitle: x\n---\n# Guide\n\n```bash\n# pas un titre\n```\n\n## Sauvegarde\n\nx\n\n"
        "## Sauvegarde\n\nDeux lignes\npour un titre\n---\n\n### La `vzdump` **nocturne** "
        "[lien](x.md) ###\n\n#### Prérequis & co\n\n##### Trop fin\n\n## [[guides/vpn|VPN]]\n"
    )
    found = headings(text.splitlines())
    assert [(h.level, h.anchor, h.text) for h in found] == outline(text)
    assert [h.anchor for h in found][1:3] == ["sauvegarde", "sauvegarde-1"]
    assert headings(text.splitlines(), until=12)[-1].anchor == "sauvegarde"
