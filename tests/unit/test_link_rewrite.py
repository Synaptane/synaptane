"""A move rewrites the links, and only the links' targets."""

from synaptane.core.markdown import WikiIndex, links_fast, rewrite_links
from synaptane.services.links import move_changes

MOVED = """# Proxmox

Retour à l'[accueil](../README.md) et au [stockage](../stockage/ceph.md#pools "Ceph").
Schéma : ![réseau](img/reseau.png) · [dossier](../stockage/) · [racine](/README.md)
Voisine : [notes](./notes.md) · [ici](#sauvegarde) · [site](https://example.com/a.md)
Espaces : [<plan>](<../plans/plan de reprise.md>) · [ref][r] · [[ceph]]

[r]: ../stockage/ceph.md

```markdown
[pas un lien](../README.md)
```

Du `[code](../README.md)` aussi.
"""

FILES = {
    "content/README.md": b"# Infra\n\nVoir [Proxmox](serveurs/proxmox.md#sauvegarde).\n",
    "content/serveurs/proxmox.md": MOVED.encode(),
    "content/serveurs/notes.md": b"# Notes\n",
    "content/serveurs/img/reseau.png": b"PNG",
    "content/stockage/ceph.md": (
        b"# Ceph\n\n- [Proxmox](../serveurs/proxmox.md)\n- [[proxmox]] et [[serveurs/proxmox]]\n"
        b"- [[serveurs/proxmox#sauvegarde|la sauvegarde]]\n- ![img](../serveurs/img/reseau.png)\n"
    ),
    "content/plans/plan de reprise.md": b"# Plan\n",
    "content/autre.md": b"# Autre\n\nAucun lien vers Proxmox ici.\n",
}


def links_of_files(files: dict[str, bytes]) -> dict[str, set[str]]:
    index = WikiIndex.of(p.removeprefix("content/") for p in files)
    return {
        path: links_fast(data.decode(), path.removeprefix("content/"), index)
        for path, data in files.items()
        if path.endswith(".md")
    }


def move(files: dict[str, bytes], old: str, new: str) -> dict[str, bytes]:
    def read(paths: list[str]) -> dict[str, bytes]:
        return {p: files[p] for p in paths if p in files}

    return move_changes(read, set(files), links_of_files(files), old, new)


NEW = "content/infra/hyperviseurs/proxmox.md"


def test_outgoing_links_follow_the_moved_page() -> None:
    text = move(FILES, "content/serveurs/proxmox.md", NEW)[NEW].decode()
    expected = (
        MOVED.replace("](../README.md)", "](../../README.md)", 1)
        .replace("../stockage/ceph.md#pools", "../../stockage/ceph.md#pools")
        .replace("](img/reseau.png)", "](../../serveurs/img/reseau.png)")
        .replace("](../stockage/)", "](../../stockage/)")
        .replace("](./notes.md)", "](../../serveurs/notes.md)")
        .replace("<../plans/plan de reprise.md>", "<../../plans/plan de reprise.md>")
        .replace("[r]: ../stockage/ceph.md", "[r]: ../../stockage/ceph.md")
    )
    # Everything else is kept as is: link title, code, external links, anchors, link from
    # the root, wiki link that still resolves.
    assert text == expected
    assert "[pas un lien](../README.md)" in text and "`[code](../README.md)`" in text


def test_incoming_links_are_rewritten_and_plain_wikilinks_kept() -> None:
    writes = move(FILES, "content/serveurs/proxmox.md", NEW)
    assert set(writes) == {NEW, "content/README.md", "content/stockage/ceph.md"}
    assert writes["content/README.md"] == (
        b"# Infra\n\nVoir [Proxmox](infra/hyperviseurs/proxmox.md#sauvegarde).\n"
    )
    assert writes["content/stockage/ceph.md"].decode() == (
        "# Ceph\n\n- [Proxmox](../infra/hyperviseurs/proxmox.md)\n"
        "- [[proxmox]] et [[infra/hyperviseurs/proxmox]]\n"
        "- [[infra/hyperviseurs/proxmox#sauvegarde|la sauvegarde]]\n"
        "- ![img](../serveurs/img/reseau.png)\n"
    )


def test_a_partial_folder_wikilink_that_still_resolves_is_kept() -> None:
    writes = move(FILES, "content/serveurs/proxmox.md", "content/infra/serveurs/proxmox.md")
    ceph = writes["content/stockage/ceph.md"].decode()
    assert "[[proxmox]] et [[serveurs/proxmox]]" in ceph  # `…/serveurs/proxmox` still leads there
    assert "[Proxmox](../infra/serveurs/proxmox.md)" in ceph


def test_a_renamed_page_and_a_moved_image() -> None:
    renamed = move(FILES, "content/serveurs/proxmox.md", "content/serveurs/pve.md")
    assert b"- [[pve]] et [[serveurs/pve]]\n" in renamed["content/stockage/ceph.md"]
    assert b"[[serveurs/pve#sauvegarde|la sauvegarde]]" in renamed["content/stockage/ceph.md"]
    assert b"(serveurs/pve.md#sauvegarde)" in renamed["content/README.md"]
    assert renamed["content/serveurs/pve.md"] == MOVED.encode()  # same folder: nothing to redo
    image = move(FILES, "content/serveurs/img/reseau.png", "content/medias/reseau.png")
    assert image["content/medias/reseau.png"] == b"PNG"
    assert "![réseau](../medias/reseau.png)".encode() in image["content/serveurs/proxmox.md"]
    assert b"![img](../medias/reseau.png)" in image["content/stockage/ceph.md"]


def test_a_homonym_coming_closer_does_not_steal_a_wikilink() -> None:
    """A `[[name]]` keeps its target when a homonym comes closer: it is then qualified."""
    files = {
        "content/clients/a/contrat.md": b"# A\n",
        "content/clients/b/notes.md": b"# Notes\n\nVoir [[contrat]].\n",
        "content/archives/contrat.md": b"# Ancien\n",
    }
    writes = move(files, "content/archives/contrat.md", "content/clients/b/contrat.md")
    assert writes["content/clients/b/notes.md"] == b"# Notes\n\nVoir [[a/contrat]].\n"
    writes = move(files, "content/archives/contrat.md", "content/vieux/contrat.md")
    assert set(writes) == {"content/vieux/contrat.md"}  # nothing else changes


def test_crlf_unicode_and_non_markdown_are_left_alone() -> None:
    page = "# Été\r\n\r\nVoir [l'été](../été.md) — fin.\r\n"
    index = WikiIndex.of(["été.md", "x/page.md"])
    moved = WikiIndex.of(["archives/été.md", "x/page.md"])
    out = rewrite_links(page, "x/page.md", "x/page.md", {"été.md": "archives/été.md"}, index, moved)
    assert out == page.replace("](../été.md)", "](../archives/été.md)")
    files = {"content/a.md": b"[b](b.txt)\n", "content/b.txt": b"\xff\xfe binaire"}
    writes = move(files, "content/b.txt", "content/c/b.txt")
    assert writes == {"content/c/b.txt": b"\xff\xfe binaire", "content/a.md": b"[b](c/b.txt)\n"}
