"""Page rendering: safe Markdown, resolved internal links, code highlighting.

The produced HTML is always sanitized (nh3): a repository cannot inject a script.
"""

import posixpath
from collections.abc import Callable
from urllib.parse import quote, urlsplit

import nh3
from markdown_it.token import Token
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name, guess_lexer_for_filename
from pygments.util import ClassNotFound

from synaptane.core.markdown import WIKI_SCHEME, WikiIndex, parser, resolve, resolve_wiki, walk

_FORMATTER = HtmlFormatter(nowrap=True, classnames=True)
MAX_HIGHLIGHT = 512 * 1024

ALLOWED_TAGS = set(nh3.ALLOWED_TAGS) | {"input", "span", "details", "summary"}
ALLOWED_ATTRIBUTES = {
    **{tag: set(attrs) for tag, attrs in nh3.ALLOWED_ATTRIBUTES.items()},
    "a": {"href", "title", "target", "class"},
    "img": {"src", "alt", "title", "width", "height"},
    "input": {"type", "checked", "disabled"},
    "span": {"class"},
    "code": {"class"},
    "pre": {"class"},
    "li": {"class"},
    "ul": {"class"},
    "td": {"align", "style"},
    "th": {"align", "style"},
    **{f"h{level}": {"id"} for level in range(1, 7)},
}


def _highlight_block(code: str, language: str, _attrs: object) -> str:
    try:
        lexer = get_lexer_by_name(language) if language else None
    except ClassNotFound:
        lexer = None
    if lexer is None or len(code) > MAX_HIGHLIGHT:
        return ""  # markdown-it then escapes the code itself
    return highlight(code, lexer, _FORMATTER)


def highlight_file(name: str, text: str) -> str:
    """Highlighted code of a text file (lines in `<span>`), escaped in every case."""
    try:
        lexer = guess_lexer_for_filename(name, text[:4096]) if len(text) <= MAX_HIGHLIGHT else None
    except ClassNotFound:
        lexer = None
    if lexer is None:
        from html import escape

        return escape(text)
    return highlight(text, lexer, _FORMATTER)


_MD = parser(_highlight_block)


def _rewrite(
    tokens: list[Token],
    current: str,
    page_url: Callable[[str], str],
    raw_url: Callable[[str], str],
    index: WikiIndex | None,
) -> None:
    for token in walk(tokens):
        if token.type == "link_open":
            href = str(token.attrGet("href") or "")
            if href.startswith(WIKI_SCHEME):
                # Wiki link: the nearest file with that name; otherwise the page to create.
                name, _, fragment = href[len(WIKI_SCHEME) :].partition("#")
                target = resolve_wiki(name, current, index) if index else None
                if target is None:
                    missing = name.strip().strip("/")
                    if not posixpath.splitext(missing)[1]:
                        missing += ".md"
                    target = posixpath.normpath(posixpath.join(posixpath.dirname(current), missing))
                    if target.startswith(".."):  # climbs out of the wiki: page at the root
                        target = "/".join(p for p in target.split("/") if p != "..")
                    token.attrSet("class", "wikilink-missing")
                token.attrSet("href", page_url(target) + (f"#{fragment}" if fragment else ""))
                continue
            resolved = resolve(current, href)
            if resolved is not None:
                fragment = urlsplit(href).fragment
                token.attrSet("href", page_url(resolved) + (f"#{fragment}" if fragment else ""))
            elif urlsplit(href).scheme in ("http", "https"):
                token.attrSet("target", "_blank")  # outside Synaptane: new tab
        elif token.type == "image":
            resolved = resolve(current, str(token.attrGet("src") or ""))
            if resolved is not None:
                token.attrSet("src", raw_url(resolved))


def render_markdown(
    text: str,
    current: str,
    page_url: Callable[[str], str],
    raw_url: Callable[[str], str],
    index: WikiIndex | None = None,
) -> str:
    tokens = _MD.parse(text)
    _rewrite(tokens, current, page_url, raw_url, index)
    html = _MD.renderer.render(tokens, _MD.options, {})
    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer",
    )


def url_path(path: str) -> str:
    return quote(path, safe="/")


def pygments_css() -> str:
    """Code colors (Pygments classes under `.hl`), light and dark.

    The background stays `pre`'s from app.css: rules set on `.hl` alone are dropped.
    """

    def rules(style: str, prefix: str) -> str:
        css = HtmlFormatter(style=style).get_style_defs(prefix)  # type: ignore[no-untyped-call]
        return "\n".join(line for line in css.splitlines() if line.startswith(f"{prefix} ."))

    light = rules("xcode", ".hl")  # only built-in light style above 4.5:1 (WCAG AA)
    dark = rules("github-dark", ":root[data-theme=dark] .hl")
    auto = rules("github-dark", ":root:not([data-theme=light]) .hl")
    return f"{light}\n{dark}\n@media (prefers-color-scheme: dark) {{\n{auto}\n}}\n"
