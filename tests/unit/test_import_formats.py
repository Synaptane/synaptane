"""Notion and Confluence exports laid out as a space: names, tree, links, databases, macros."""

import io
import zipfile
from collections.abc import Mapping

from synaptane.services import imports
from synaptane.services.imports_common import Layout, Names, clean_name, relative, split_name
from synaptane.services.imports_notion import notion_id
from tests.exports import (
    ABSENT,
    PROJECTS,
    TASKS,
    WIKI,
    confluence_export,
    confluence_page,
    notion_export,
    zipped,
)


def converted(
    files: Mapping[str, bytes | str], kind: str = ""
) -> tuple[dict[str, str | bytes], Layout, str]:
    """What each file of the export becomes: path → text (pages) or bytes."""
    with zipfile.ZipFile(io.BytesIO(zipped(files))) as archive:
        entries, _, obsidian = imports._entries(archive)
        detected = imports.detect([path for _, path in entries], obsidian)
        reader = imports._Reader(archive)
        layout = imports._layout(kind or detected, entries, reader.read)
        out: dict[str, str | bytes] = {}
        for item in layout.items:
            data = reader.read(item.info) if item.info is not None else b""
            if item.made is not None:
                data = item.made()
            if item.convert is not None:
                data = item.convert(data)
            out[item.path] = data.decode() if item.path.endswith(".md") else data
    return out, layout, detected


def page(out: dict[str, str | bytes], path: str) -> str:
    text = out[path]
    assert isinstance(text, str)
    return text


def test_formats_are_recognized() -> None:
    assert imports.detect(["a.md", "b/c.png"]) == "markdown"
    assert imports.detect(["a.md"], obsidian=True) == "obsidian"
    assert imports.detect([f"Wiki {WIKI}.md", f"Wiki {WIKI}/x.png"]) == "notion"
    assert imports.detect([f"Base {TASKS}_all.csv"]) == "notion"
    assert imports.detect([f"photo {WIKI}.png"]) == "markdown"  # an id alone is not enough
    assert imports.detect(["index.html", "Home_123.html"]) == "confluence"
    assert imports.detect(["index.html", "styles/site.css"]) == "confluence"
    assert imports.detect(["index.html", "about.html"]) == "markdown"  # a plain site
    assert imports.detect(["sous/index.html", "sous/Home_1.html"]) == "markdown"
    _, _, detected = converted(notion_export())
    assert detected == "notion"
    _, _, detected = converted(confluence_export())  # its top folder (the space key) left out
    assert detected == "confluence"


def test_names_lose_their_id_and_stay_unique() -> None:
    out, _, _ = converted(notion_export())
    assert sorted(out) == [
        "Wiki/Projets (2).md",  # a second "Projets", next to the folder of the first
        "Wiki/Projets/Installation.md",
        "Wiki/Projets/README.md",  # a page with subpages: the home page of its folder
        "Wiki/README.md",
        "Wiki/Tâches/README.md",  # the database's index page
        "Wiki/Tâches/Relire.md",
        "Wiki/Tâches/Écrire la doc.md",
        "Wiki/Untitled.png",
    ]
    names = Names()
    assert [names.take("Notes", ".md"), names.take("notes"), names.take("NOTES", ".md")] == [
        "Notes.md",
        "notes (2)",
        "NOTES (3).md",
    ]
    assert names.take("Notes", ".png") == "Notes.png"
    assert clean_name("  a/b\\c\x00d  ") == "a-b-c-d" and clean_name(" .. ") == "Untitled"
    assert clean_name(".cache") == "cache" and len(clean_name("x" * 300)) == 100
    assert clean_name("é", "x") == "é" and clean_name("é") == "é"  # NFC
    assert split_name("archive.tar.gz") == ("archive.tar", ".gz")
    assert split_name(".env") == (".env", "") and split_name("Note v1. final") == (
        "Note v1. final",
        "",
    )
    assert relative("a/b.md", "a/c d#1?.md") == "c d%231%3F.md"


def test_notion_links_lead_to_the_new_paths() -> None:
    out, layout, _ = converted(notion_export())
    home = page(out, "Wiki/README.md")
    assert "[Projets](Projets/README.md)" in home
    assert "[l'autre](<Projets (2).md>)" in home
    assert "[les tâches](Tâches/README.md)" in home  # the CSV: its index page
    assert "![Untitled](Untitled.png)" in home
    assert "[projets](Projets/README.md)" in home  # notion.so address of a page of the export
    assert "[par uuid](Projets/README.md)" in home
    assert f"[ailleurs](https://www.notion.so/Ailleurs-{ABSENT})" in home  # not in the export
    assert "[ancre](#titre)" in home and "[web](https://example.org)" in home
    assert "[lien](Wiki%20x.md)" in home  # in a code block: as written
    assert page(out, "Wiki/Projets/README.md") == "# Projets\n\nRetour au [Wiki](../README.md).\n"
    assert layout.links.rewritten == 7
    assert layout.links.broken == [
        ("Wiki/README.md", f"Wiki%20{WIKI}/Absente%20{ABSENT}.md"),
        ("Wiki/README.md", "../../hors.md"),
    ]
    assert notion_id(f"https://acme.notion.site/Page-{PROJECTS}") == PROJECTS
    assert notion_id(f"https://www.notion.so/acme/Base-{TASKS}?v=1&p={PROJECTS}") == PROJECTS
    assert notion_id(f"https://evil.example/Page-{PROJECTS}") is None
    assert notion_id("https://www.notion.so/") is None and notion_id("http://[::1") is None


def test_notion_callouts_toggles_and_line_breaks() -> None:
    out, _, _ = converted(notion_export())
    home = page(out, "Wiki/README.md")
    assert "> [!note]\n> 💡 **Astuce :** une ligne\\\n> puis une autre\n>\n" in home
    assert "> [!note]- Plus de détails\n>\n> Texte caché\n" in home
    assert "<aside>dans du code</aside>" in home and "<aside>\n" not in home
    assert "| 1 | a b |" in page(out, "Wiki/Projets/Installation.md")
    files = {
        f"A {WIKI}.md": (
            "# A\n\n<aside>\nDehors\n\n<details>\n<summary><strong>Dedans</strong></summary>\n\n"
            "```\ncode\n```\n\n</details>\n\n</aside>\n\nFin <mark>vive</mark> <span>x</span> "
            "`<br>`\n</aside>\n"
        )
    }
    nested = page(converted(files)[0], "A.md")
    assert nested == (
        "# A\n\n> [!note]\n> Dehors\n>\n> > [!note]- Dedans\n> >\n> > ```\n> > code\n> > ```\n"
        "> >\n>\n>\n\nFin ==vive== x `<br>`\n</aside>\n"
    )


def test_a_notion_database_becomes_an_index_page_and_front_matter() -> None:
    out, layout, _ = converted(notion_export())
    assert page(out, "Wiki/Tâches/README.md") == (
        "# Tâches\n\n"
        "| Name | Status | owner | Échéance | Projet |\n| --- | --- | --- | --- | --- |\n"
        "| [Écrire la doc](<Écrire la doc.md>) | Done | Ada | 3 octobre 2026 "
        "| [Projets](../Projets/README.md) |\n"
        "| [Relire](Relire.md) | In progress |  |  |  |\n"
        "| Ligne sans page |  |  |  |  |\n"
    )
    assert page(out, "Wiki/Tâches/Écrire la doc.md") == (
        '---\nStatus: "Done"\n"owner (Notion)": "Ada"\nÉchéance: "3 octobre 2026"\n'
        'Projet: "Projets"\n---\n# Écrire la doc\n\nLe contenu.\n'
    )
    assert page(out, "Wiki/Tâches/Relire.md").startswith(
        '---\nStatus: "In progress"\n---\n# Relire\n\nÀ relire.'
    )
    # The `_all.csv` is the complete one: the other is not imported.
    assert layout.skipped == [f"Wiki {WIKI}/Tâches {TASKS}.csv"]


def test_notion_databases_without_folder_odd_names_and_shortened_titles() -> None:
    long_title = "Un titre très long que Notion raccourcit dans le nom du fichier exporté"
    files: dict[str, bytes | str] = {
        f"Base {TASKS}.csv": 'Name,"Prix | HT",Lien\nA,"1 | 2",Seul (B%20b.md)\n,,\n',
        f"Liste {WIKI}.csv": "Titre,Note\n" + f'"{long_title}",x\n"Sans page",y\n',
        f"Liste {WIKI}/{long_title[:50]} {PROJECTS}.md": "# x\n\nNote: x\n\nCorps\n",
        f"Vide {ABSENT}.csv": "",
        f"Notes {PROJECTS}.md": "# Notes\n\n[base](Base%20" + TASKS + ".csv)\n",
        "README.md": "# Accueil sans id\n",
        "README.MD": "# Doublon par la casse\n",
        "plain.csv": "a,b\n",
    }
    out, _, detected = converted(files)
    assert detected == "notion"
    assert page(out, "Base.md") == (
        "# Base\n\n| Name | Prix \\| HT | Lien |\n| --- | --- | --- |\n"
        "| A | 1 \\| 2 | Seul (B%20b.md) |\n"
    )
    assert f"[{long_title}](<{long_title[:50]}.md>)" in page(out, "Liste/README.md")
    assert "| Sans page | y |" in page(out, "Liste/README.md")
    assert page(out, f"Liste/{long_title[:50]}.md") == '---\nNote: "x"\n---\n# x\n\nCorps\n'
    assert page(out, "Vide.md") == "# Vide\n"
    assert page(out, "Notes.md") == "# Notes\n\n[base](Base.md)\n"
    assert {"README.MD", "README (2).md", "plain.csv"} <= set(out)  # case ignored


def test_confluence_tree_names_and_attachments() -> None:
    out, layout, _ = converted(confluence_export())
    assert sorted(out) == [
        "Ops Home/FAQ.md",
        "Ops Home/Procédures/Backup/README.md",
        "Ops Home/Procédures/Backup/runbook.pdf",
        "Ops Home/Procédures/Backup/schéma réseau.png",
        "Ops Home/Procédures/README.md",
        "Ops Home/README.md",
        "attachments/77777/1.txt",  # its page is not in the export: left where it was
    ]
    assert out["Ops Home/Procédures/Backup/schéma réseau.png"] == b"\x89PNG\r\n\x1a\nfake"
    assert layout.skipped == [
        "OPS/images/icons/bullet_blue.gif",
        "OPS/images/icons/emoticons/check.svg",
        "OPS/index.html",
        "OPS/styles/site.css",
    ]


def test_confluence_pages_become_markdown() -> None:
    out, layout, _ = converted(confluence_export())
    backup = page(out, "Ops Home/Procédures/Backup/README.md")
    assert backup.startswith(
        "# Backup\n\n*Created by Ada Lovelace, last modified on Oct 03, 2026*\n\n"
        "![](<schéma réseau.png>) ✅\n\n> [!danger]\n>\n> Jamais le vendredi.\n\n"
        "**backup.sh**\n\n```bash\npg_dump -Fc base > base.dump\n```\n\n"
        "> [!note]- Restauration\n>\n> 1. Arrêter\n> 2. Restaurer\n\n"
        "| Base | Fréquence |\n| --- | --- |\n| prod | chaque nuit |\n\n"
        "[vignette](<schéma réseau.png>)\n\n## Attachments\n\n"
        "[runbook.pdf](runbook.pdf) (application/pdf)\\\n"
    )
    assert "alert(1)" not in backup and "Document generated" not in backup
    procedures = page(out, "Ops Home/Procédures/README.md")
    assert "## Liste\n\n[Backup](Backup/README.md)" in procedures  # h1 lowered under the title
    assert "> [!warning] Avant tout\n>\n> Prévenir l'équipe." in procedures
    home = page(out, "Ops Home/README.md")
    assert home.endswith(
        "Bienvenue. [Procédures](Procédures/README.md), [FAQ](FAQ.md), "
        "[sauvegarde](Procédures/Backup/README.md), [disparue](Disparue_99999.html), espace, "
        "ancre, sortie, [web](https://example.org).\n"
    )
    assert page(out, "Ops Home/FAQ.md").endswith("Questions : [accueil](README.md)\n")
    assert layout.links.broken == [
        ("Ops Home/README.md", "Disparue_99999.html"),
        ("Ops Home/README.md", "../../../etc/passwd"),
    ]


def test_confluence_tree_without_index_from_breadcrumbs() -> None:
    crumbs = [("index.html", "Ops")]
    files: dict[str, bytes | str] = {
        "index.html": "<html><body>Rien</body></html>",
        "Parent_1.html": confluence_page(
            "Parent",
            crumbs,
            '<p><a href="attachments/1/8.png">voir</a> <a href="attachments/1/9.pdf">Plan.PDF</a>'
            "</p>",
        ),
        "attachments/1/8.png": b"png",
        "attachments/1/9.pdf": b"pdf",
        "Child_2.html": confluence_page(
            "Child", [*crumbs, ("Parent_1.html", "Parent")], "<p>C</p>"
        ),
        # A loop between two pages, and a parent outside the export: both at the top.
        "A_3.html": confluence_page("Même titre", [*crumbs, ("B_4.html", "B")], "<p>A</p>"),
        "B_4.html": confluence_page("Même titre", [*crumbs, ("A_3.html", "A")], "<p>B</p>"),
        "Lost_5.html": confluence_page("Perdue", [*crumbs, ("Gone_9.html", "x")], ""),
        "6.html": "<html><body><div id='main-content'><h2>Sans titre</h2></div></body></html>",
        "attachments/thumbnails/1/7.png": b"vignette",
    }
    out, layout, detected = converted(files)
    assert detected == "confluence"
    assert sorted(out) == [
        "6.md",
        "Même titre/Même titre.md",  # the loop is cut at the second page
        "Même titre/README.md",
        "Parent/8.png",  # a link text that is not its name: the name in the export
        "Parent/Child.md",
        "Parent/Plan.PDF",
        "Parent/README.md",
        "Perdue.md",
    ]
    assert page(out, "6.md") == "# 6\n\n## Sans titre\n"
    assert page(out, "Perdue.md") == (
        "# Perdue\n\n*Created by Ada Lovelace, last modified on Oct 03, 2026*\n"
    )
    assert "attachments/thumbnails/1/7.png" in layout.skipped


def test_a_format_can_be_forced() -> None:
    out, layout, _ = converted(notion_export(), "markdown")
    assert f"Wiki {WIKI}/Tâches {TASKS}.csv" in out and not layout.skipped
    out, _, _ = converted({"Page_1.html": "<p>x</p>", "index.html": ""}, "markdown")
    assert set(out) == {"Page_1.html", "index.html"}
