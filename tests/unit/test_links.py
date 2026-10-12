from synaptane.core.markdown import links_fast, links_of

PAGE = """# Titre

Voir [le serveur](serveurs/proxmox.md) et [l'accueil](../README.md "titre").
Lien <absolu> : [site](https://example.com) et [ancre](#section).
Image ![schéma](img/schema.png) et [<espaces>](<dossier/une page.md>).

[ref]: guides/demarrage.md

```markdown
[pas un lien](code/ignore.md)
```

Du `[code](inline/ignore.md)` aussi.
"""


def test_fast_extractor_matches_the_parser_on_common_cases() -> None:
    expected = links_of(PAGE, "infra/index.md")
    assert links_fast(PAGE, "infra/index.md") >= expected
    assert "infra/code/ignore.md" not in links_fast(PAGE, "infra/index.md")
    assert "infra/inline/ignore.md" not in links_fast(PAGE, "infra/index.md")
    assert {"infra/serveurs/proxmox.md", "README.md"} <= expected
    # A reference definition counts too (even unused): useful for backlinks.
    assert "infra/guides/demarrage.md" in links_fast(PAGE, "infra/index.md")


def test_page_without_links_is_skipped() -> None:
    assert links_fast("# Rien\n\nAucun lien ici.\n", "a.md") == set()


def test_wikilinks_bare_urls_and_new_tabs() -> None:
    """Wiki links [[name]] (imported Obsidian vaults), clickable bare URLs, external links
    in a new tab; internal links and anchors in the same one."""
    from synaptane.core.markdown import WikiIndex, links_fast, links_of, resolve_wiki
    from synaptane.web.render import render_markdown

    index = WikiIndex.of(
        [
            "emploi/README.md",
            "emploi/lettres/lettre-a.md",
            "archives/lettre-a.md",  # homonyme plus loin
            "emploi/decks/plan.html",
        ]
    )
    assert resolve_wiki("lettre-a", "emploi/README.md", index) == "emploi/lettres/lettre-a.md"
    assert resolve_wiki("lettre-a", "archives/x.md", index) == "archives/lettre-a.md"
    assert resolve_wiki("plan.html", "emploi/README.md", index) == "emploi/decks/plan.html"
    assert (
        resolve_wiki("lettres/lettre-a", "emploi/README.md", index) == "emploi/lettres/lettre-a.md"
    )
    assert resolve_wiki("absente", "emploi/README.md", index) is None
    text = (
        "- [[lettre-a]] · [[lettre-a|la lettre]] · [[absente]]\n"
        "- CV : https://cv.example.fr/\n- `[[code]]`\n"
    )
    assert links_of(text, "emploi/README.md", index) == {"emploi/lettres/lettre-a.md"}
    assert links_fast(text, "emploi/README.md", index) == {"emploi/lettres/lettre-a.md"}
    html = render_markdown(
        text, "emploi/README.md", lambda p: f"/p/{p}", lambda p: f"/r/{p}", index
    )
    assert '<a href="/p/emploi/lettres/lettre-a.md" rel="noopener noreferrer">la lettre</a>' in html
    assert 'href="/p/emploi/absente.md" class="wikilink-missing"' in html
    assert '<a href="https://cv.example.fr/" target="_blank"' in html
    assert "<code>[[code]]</code>" in html
    internal = render_markdown("[a](lettres/lettre-a.md) [b](#x)", "emploi/README.md", str, str)
    assert "target" not in internal


def test_a_wikilink_with_a_folder_never_falls_back_to_another_folder() -> None:
    """`[[b/contrat]]` must not silently lead to client A's contract."""
    from synaptane.core.markdown import WikiIndex, resolve_wiki

    index = WikiIndex.of(["clients/a/contrat.md", "clients/b/contrat.md", "clients/a/notes.md"])
    here = "clients/a/notes.md"
    assert resolve_wiki("clients/b/contrat", here, index) == "clients/b/contrat.md"
    assert resolve_wiki("b/contrat", here, index) == "clients/b/contrat.md"  # chemin partiel
    assert resolve_wiki("clients/b/Contrat", here, index) == "clients/b/contrat.md"
    assert resolve_wiki("x/contrat", here, index) is None  # dossier inexistant : manquant
    assert resolve_wiki("contrat", here, index) == "clients/a/contrat.md"  # the closest one
    assert resolve_wiki("../../contrat", here, index) is None


def test_broken_links_come_with_a_suggestion() -> None:
    from synaptane.core.markdown import WikiIndex, broken_links

    index = WikiIndex.of(["serveurs/proxmox.md", "stockage/ceph.md", "README.md"])
    text = (
        "[Proxmox](../serveur/proxmox.md) · [ok](../serveurs/proxmox.md) · [[inconnue]] · "
        "[[proxmox]] · [dossier](../serveurs/) · [site](https://example.com)"
    )
    assert broken_links(text, "stockage/ceph.md", index) == [
        ("../serveur/proxmox.md", "../serveurs/proxmox.md"),
        ("[[inconnue]]", None),
    ]
