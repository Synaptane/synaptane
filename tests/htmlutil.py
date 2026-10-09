"""Reading the rendered HTML in tests, without dependencies: the standard library's
`html.parser` and a subset of CSS selectors.

Why: a test that looks for a label (`"Add a member" not in page`) still passes the day the
label changes, without checking anything anymore. Instead, target an element marked
`data-test="…"` in the template, and also check that it is there when it should be.

Supported selectors: `tag`, `#id`, `.class`, `[attr]`, `[attr="v"]` (and `^=`, `$=`, `*=`,
`~=`), their combinations (`form[data-test="member-add"]`), the descendant (space) and the
direct child (`>`). Several comma-separated selectors are not supported: make two calls.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser

# Elements without a closing tag: they never become parents of what follows.
VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param"}
    | {"source", "track", "wbr"}
)
# Their text is not displayed text.
NOT_TEXT = frozenset({"script", "style", "template"})
# Optional closing tags: an opening tag closes the element of the same family left open,
# without going beyond its container (`<li>one<li>two` gives two sibling `<li>`).
IMPLIED_END: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "li": (frozenset({"li"}), frozenset({"ul", "ol", "menu"})),
    "dt": (frozenset({"dt", "dd"}), frozenset({"dl"})),
    "dd": (frozenset({"dt", "dd"}), frozenset({"dl"})),
    "option": (frozenset({"option"}), frozenset({"select", "datalist", "optgroup"})),
    "tr": (frozenset({"tr"}), frozenset({"table", "thead", "tbody", "tfoot"})),
    "td": (frozenset({"td", "th"}), frozenset({"tr", "table"})),
    "th": (frozenset({"td", "th"}), frozenset({"tr", "table"})),
}
# A block closes the paragraph left open just above it.
CLOSES_P = frozenset(
    {"address", "article", "aside", "blockquote", "details", "div", "dl", "fieldset", "figure"}
    | {"footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "main", "nav"}
    | {"ol", "p", "pre", "section", "table", "ul"}
)


@dataclass(eq=False)
class Element:
    """An element of the document: tag, attributes, children (elements or text)."""

    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Element | str] = field(default_factory=list, repr=False)
    parent: Element | None = field(default=None, repr=False)

    def get(self, name: str) -> str | None:
        """Value of an attribute; an empty string for a boolean attribute (`required`)."""
        return self.attrs.get(name)

    @property
    def href(self) -> str | None:
        return self.get("href")

    @property
    def action(self) -> str | None:
        return self.get("action")

    @property
    def classes(self) -> set[str]:
        return set((self.get("class") or "").split())

    @property
    def text(self) -> str:
        """Visible text of the element and its descendants, whitespace normalized."""
        return " ".join("".join(self._texts()).split())

    def _texts(self) -> Iterator[str]:
        for child in self.children:
            if isinstance(child, str):
                yield child
            elif child.tag == "br":
                yield " "
            elif child.tag not in NOT_TEXT:
                yield from child._texts()

    def elements(self) -> Iterator[Element]:
        """The descendants, in document order (without the element itself)."""
        for child in self.children:
            if isinstance(child, Element):
                yield child
                yield from child.elements()

    def select(self, selector: str) -> list[Element]:
        return select(self, selector)

    def select_one(self, selector: str) -> Element:
        return select_one(self, selector)


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element("#document")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._close_implied(tag)
        element = Element(tag, {k: v or "" for k, v in attrs}, parent=self.stack[-1])
        self.stack[-1].children.append(element)
        if tag not in VOID:
            self.stack.append(element)

    def _close_implied(self, tag: str) -> None:
        if tag in CLOSES_P and self.stack[-1].tag == "p":
            self.stack.pop()
        if tag not in IMPLIED_END:
            return
        family, containers = IMPLIED_END[tag]
        for depth in range(len(self.stack) - 1, 0, -1):
            open_tag = self.stack[depth].tag
            if open_tag in containers:
                return
            if open_tag in family:
                del self.stack[depth:]
                return

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # `<br/>`, `<svg …/>`: an element without children, whatever it is.
        element = Element(tag, {k: v or "" for k, v in attrs}, parent=self.stack[-1])
        self.stack[-1].children.append(element)

    def handle_endtag(self, tag: str) -> None:
        # Close up to the matching opening tag (a `<p>` or an `<li>` left open closes
        # with its parent); an orphan closing tag is ignored.
        for depth in range(len(self.stack) - 1, 0, -1):
            if self.stack[depth].tag == tag:
                del self.stack[depth:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def parse(html: str) -> Element:
    """Parses a document (or a fragment) and returns its `#document` root."""
    builder = _Builder()
    builder.feed(html)
    builder.close()
    return builder.root


# --- Selectors --------------------------------------------------------------------------------

_ATTRIBUTE = re.compile(
    r"""\[\s*([\w:-]+)\s*(?:([~^$*]?=)\s*(?:"([^"]*)"|'([^']*)'|([^\s\]]+)))?\s*\]"""
)
_SIMPLE = re.compile(r"([\w-]+|\*)|#([\w-]+)|\.([\w-]+)")
_OPERATORS: dict[str, Callable[[str, str], bool]] = {
    "=": lambda actual, wanted: actual == wanted,
    "^=": lambda actual, wanted: bool(wanted) and actual.startswith(wanted),
    "$=": lambda actual, wanted: bool(wanted) and actual.endswith(wanted),
    "*=": lambda actual, wanted: bool(wanted) and wanted in actual,
    "~=": lambda actual, wanted: wanted in actual.split(),
}

Test = Callable[[Element], bool]


def _has(name: str) -> Test:
    return lambda element: name in element.attrs


def _attribute(name: str, operator: str, wanted: str) -> Test:
    check = _OPERATORS[operator]
    return lambda element: name in element.attrs and check(element.attrs[name], wanted)


def _tag(tag: str) -> Test:
    return lambda element: element.tag == tag


def _id(ident: str) -> Test:
    return lambda element: element.get("id") == ident


def _class(name: str) -> Test:
    return lambda element: name in element.classes


def _compound(text: str) -> Test:
    """A compound selector (`a.button[href="/admin"]`) becomes a predicate."""
    tests: list[Test] = []
    position = 0
    while position < len(text):
        if text[position] == "[":
            match = _ATTRIBUTE.match(text, position)
            if match is None:
                raise ValueError(f"Sélecteur d'attribut invalide : {text!r}")
            name, operator = match.group(1), match.group(2)
            wanted = next((g for g in match.group(3, 4, 5) if g is not None), "")
            tests.append(_has(name) if operator is None else _attribute(name, operator, wanted))
        else:
            match = _SIMPLE.match(text, position)
            if match is None or (match.group(1) and position):
                raise ValueError(f"Sélecteur non pris en charge : {text!r}")
            tag, ident, cls = match.groups()
            if tag and tag != "*":
                tests.append(_tag(tag.lower()))
            elif ident:
                tests.append(_id(ident))
            elif cls:
                tests.append(_class(cls))
        position = match.end()
    return lambda element: all(test(element) for test in tests)


def _steps(selector: str) -> list[tuple[str, Test]]:
    """Splits `a > b c` into steps (combinator, predicate), without cutting inside a `[…]`."""
    tokens = re.findall(r"""\[(?:"[^"]*"|'[^']*'|[^\]])*\]|>|[^\s>\[]+|\s+""", selector.strip())
    steps: list[tuple[str, Test]] = []
    combinator, current = " ", ""
    for token in [*tokens, " "]:
        if token.isspace() or token == ">":
            if current:
                steps.append((combinator, _compound(current)))
                combinator, current = " ", ""
            if token == ">":
                combinator = ">"
        else:
            current += token
    if not steps:
        raise ValueError(f"Sélecteur vide : {selector!r}")
    return steps


def _matches(element: Element, steps: list[tuple[str, Test]], scope: Element) -> bool:
    """Checks the steps from right to left, walking up the ancestors under `scope`."""
    combinator, test = steps[-1]
    if not test(element):
        return False
    if len(steps) == 1:
        return True
    ancestor = element.parent
    while ancestor is not None and ancestor is not scope:
        if _matches(ancestor, steps[:-1], scope):
            return True
        if combinator == ">":
            return False
        ancestor = ancestor.parent
    return False


def select(html: str | Element, selector: str) -> list[Element]:
    """The elements matching the selector, in document order."""
    scope = parse(html) if isinstance(html, str) else html
    steps = _steps(selector)
    return [element for element in scope.elements() if _matches(element, steps, scope)]


def select_one(html: str | Element, selector: str) -> Element:
    """The single matching element; an explicit error if there is none or several (a
    `data-test` must point to a single element of the page)."""
    found = select(html, selector)
    if len(found) != 1:
        raise LookupError(f"{selector!r} : {len(found)} élément(s) au lieu d'un seul")
    return found[0]


def data_test(name: str) -> str:
    """The selector of a test marker: `data_test("member-add")`."""
    return f'[data-test="{name}"]'
