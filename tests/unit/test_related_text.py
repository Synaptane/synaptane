"""Related pages: profile of a text (folding, stems, stop words, title) and fast title."""

from synaptane.services.related import TERMS, TITLE_WEIGHT, fast_title, profile_of


def test_profile_folds_stems_and_drops_stopwords() -> None:
    profile = profile_of(
        "Les sauvegardes et la sauvegarde : « Nœuds » du cluster, nœud ; 2026 et pour avec.",
        "Sauvegarder",
    )
    counts = dict(zip(profile.terms, profile.counts, strict=True))
    assert counts["sauvegard"] == 2 + TITLE_WEIGHT  # sauvegardes, sauvegarde, titre
    assert counts["noeud"] == 2  # ligature and accent folded, plural removed
    assert counts["clust"] == 1  # same stem as the search ("cluster" → "clust")
    assert not {"les", "pour", "avec", "2026", "et"} & set(profile.terms)
    assert profile.title == "Sauvegarder" and profile.length == sum(profile.counts)


def test_profile_keeps_the_most_frequent_terms() -> None:
    words = " ".join(f"terme{n:02d}x " * (n + 1) for n in range(40))
    profile = profile_of(words, "")
    assert len(profile.terms) == TERMS
    assert profile.terms[0] == "terme39x" and "terme00x" not in profile.terms


def test_fast_title() -> None:
    assert fast_title("# Proxmox\n\nTexte", "repli") == "Proxmox"
    assert fast_title("---\ntitle: « Réseau »\n---\nSans titre", "repli") == "« Réseau »"
    assert fast_title("```bash\n# pas un titre\n```\n\n# Le titre\n", "repli") == "Le titre"
    assert fast_title("Rien ici.\n## Section\n", "repli") == "repli"
    assert (
        fast_title("---\ntags: [a]\n---\n# Après le front matter #\n", "x")
        == "Après le front matter"
    )
