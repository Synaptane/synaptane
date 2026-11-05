"""Obsidian syntax when reading a page: the file is left as written, only the display changes."""

import pytest

from synaptane.core.markdown import WikiIndex, broken_links, link_targets, rewrite_links
from synaptane.web.render import render_markdown

INDEX = WikiIndex.of(["notes/page.md", "attachments/photo.png", "Autre page.md"])


def html(text: str) -> str:
    return render_markdown(
        text, "notes/page.md", lambda p: f"/p/{p}", lambda p: f"/raw/{p}", INDEX
    ).strip()


def test_an_embedded_image_is_shown_wherever_it_is() -> None:
    assert html("![[photo.png]]") == '<p><img src="/raw/attachments/photo.png" alt="photo.png"></p>'


@pytest.mark.parametrize(
    ("label", "attributes"),
    [
        ("300", 'alt="photo.png" width="300"'),
        ("300x200", 'alt="photo.png" width="300" height="200"'),
        ("Une légende", 'alt="Une légende"'),
    ],
)
def test_size_or_text_of_an_embedded_image(label: str, attributes: str) -> None:
    assert attributes in html(f"![[photo.png|{label}]]")


def test_an_embedded_image_that_does_not_exist_is_a_broken_link() -> None:
    assert 'src="/raw/notes/absente.png"' in html("![[absente.png]]")
    assert broken_links("![[absente.png]]", "notes/page.md", INDEX) == [("[[absente.png]]", None)]
    assert link_targets("![[photo.png|300]]", "notes/page.md", INDEX) == [
        ("[[photo.png]]", "attachments/photo.png")
    ]


def test_embedding_a_page_stays_a_link() -> None:
    assert html("![[Autre page]]") == (
        '<p><a href="/p/Autre page.md" rel="noopener noreferrer">Autre page</a></p>'
    )


def test_an_embed_follows_a_moved_image() -> None:
    moves = {"attachments/photo.png": "images/photo.png"}
    after = WikiIndex.of(["notes/page.md", "images/photo.png", "Autre page.md"])
    text = rewrite_links(
        "![[attachments/photo.png|300]]", "notes/page.md", "notes/page.md", moves, INDEX, after
    )
    assert text == "![[images/photo.png|300]]"


@pytest.mark.parametrize(
    ("text", "kind", "title"),
    [
        ("> [!warning] Attention\n> Texte", "callout callout-warn", "Attention"),
        ("> [!WARNING]\n> Texte", "callout callout-warn", "Warning"),  # GitHub
        ("> [!tip]- Replié\n> Texte", "callout callout-ok", "Replié"),
        ("> [!bug] Panne\n> Texte", "callout callout-danger", "Panne"),
        ("> [!note] Note\n> Texte", "callout", "Note"),
        ("> [!sur-mesure]\n> Texte", "callout", "Sur mesure"),
    ],
)
def test_callouts(text: str, kind: str, title: str) -> None:
    result = html(text)
    assert f'<blockquote class="{kind}">' in result
    assert f'<p class="callout-title">{title}</p>' in result
    assert "<p>Texte</p>" in result
    assert "[!" not in result


def test_a_callout_title_alone_and_its_formatting() -> None:
    assert html("> [!tip] **Fort**") == (
        '<blockquote class="callout callout-ok">\n'
        '<p class="callout-title"><strong>Fort</strong></p>\n</blockquote>'
    )


def test_a_plain_quote_is_not_a_callout() -> None:
    assert html("> Texte [!note]") == "<blockquote>\n<p>Texte [!note]</p>\n</blockquote>"


def test_a_callout_cannot_choose_its_class() -> None:
    assert "<blockquote>" in html('> [!x"onload="y]\n> Texte')


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("==mot==", "<p><mark>mot</mark></p>"),
        ("un ==mot **fort**== ici", "<p>un <mark>mot <strong>fort</strong></mark> ici</p>"),
        ("x==1 and y==2", "<p>x==1 and y==2</p>"),
        ("a == b == c", "<p>a == b == c</p>"),
        ("==1 et 2==3", "<p>==1 et 2==3</p>"),
        ("`==code==`", "<p><code>==code==</code></p>"),
    ],
)
def test_highlight(text: str, expected: str) -> None:
    assert html(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("avant %%caché%% après", "<p>avant  après</p>"),
        ("seul\n\n%%caché%%", "<p>seul</p>"),
        ("%%\nun\n\ndeux\n%%\n\nsuite", "<p>suite</p>"),
        ("%% caché %%\n\nsuite", "<p>suite</p>"),
        ("100%% sûr, 20%% peut-être", "<p>100%% sûr, 20%% peut-être</p>"),
        ("non fermé %% ici", "<p>non fermé %% ici</p>"),
        ("%%\nnon fermé", "<p>%%\nnon fermé</p>"),
    ],
)
def test_comments_are_hidden(text: str, expected: str) -> None:
    assert html(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Une phrase. ^mon-bloc", "<p>Une phrase. </p>"),
        ("Une phrase.\n\n^mon-bloc\n\nSuite", "<p>Une phrase.</p>\n<p>Suite</p>"),
        ("x ^2", "<p>x ^2</p>"),
        ("x ^abc et la suite", "<p>x ^abc et la suite</p>"),
        ("x^abc", "<p>x^abc</p>"),
    ],
)
def test_block_ids_are_hidden(text: str, expected: str) -> None:
    assert html(text) == expected


def test_code_is_left_alone() -> None:
    result = html("```\n==a== %%b%% ![[photo.png]] ^bloc\n> [!note]\n```")
    for written in ("==a==", "%%b%%", "![[photo.png]]", "^bloc", "[!note]"):
        assert written in result
