"""Guardrails on the interface's stylesheet and script. These defects only show in a
browser; here we check that their cause does not come back."""

import re
from pathlib import Path

STATIC = Path(__file__).parents[2] / "src" / "synaptane" / "web" / "static"
TEMPLATES = STATIC.parent / "templates"
CSS = (STATIC / "app.css").read_text(encoding="utf-8")
JS = (STATIC / "preferences.js").read_text(encoding="utf-8")


def _function(name: str) -> str:
    """Code of a preferences.js function, without its comments."""
    start = JS.index(f"const {name} = ")
    body = JS[start : JS.index("\n  };\n", start)]
    return "\n".join(line for line in body.splitlines() if not line.strip().startswith("//"))


def test_tree_does_not_move_the_tab_starting_point() -> None:
    """`scrollIntoView` on the tree's current page made the skip link jump on the first
    Tab; only the container scrolls."""
    tree = _function("setupTree")
    assert "scrollIntoView" not in tree and "scrollTop" in tree


def test_outline_reopens_when_the_window_grows() -> None:
    """Outline collapsed under 1,100 px, reopened when switching to a large screen."""
    toc = _function("setupToc")
    assert 'small.addEventListener("change"' in toc
    assert "!small.matches && toc.open" in toc  # click blocked only if it is open


def test_breadcrumb_menu_follows_its_button() -> None:
    """Sticky `:hover` on touch and `:focus-within` prevented closing the menu."""
    assert ":focus-within .crumb-menu" not in CSS
    hover_rules = [m.start() for m in re.finditer(r"\.crumb-more:hover \.crumb-menu", CSS)]
    assert hover_rules
    for position in hover_rules:
        assert CSS[:position].rstrip().endswith("@media(hover:hover){")
    crumbs = _function("setupCrumbs")
    assert "button.focus()" in crumbs and "focusout" in crumbs


def test_settings_columns_can_shrink_on_mobile() -> None:
    """A `1fr` column takes the minimum width of the members table (overflow at 390 px);
    `minmax(0,1fr)` lets it shrink."""
    assert ".settings-layout{grid-template-columns:1fr}" not in CSS
    assert CSS.count(".settings-layout{grid-template-columns:minmax(0,1fr)}") >= 2


def test_tree_toggles_are_touch_targets() -> None:
    """Expand buttons of at least 24 px, 32 px on touch screens."""
    width = re.search(r"\.tree-toggle,\.tree-spacer\{flex:none;width:(\d+)px", CSS)
    assert width is not None and int(width.group(1)) >= 24
    assert re.search(r"@media \(pointer:coarse\)\{\s*\.tree-toggle,\.tree-spacer\{width:32px", CSS)


def test_no_inline_script_in_templates() -> None:
    """CSP `script-src 'self'`: all the behavior is in preferences.js."""
    for template in TEMPLATES.glob("*.html"):
        for tag in re.findall(r"<script\b[^>]*>", template.read_text(encoding="utf-8")):
            assert "src=" in tag, template.name
        assert not re.search(r"\son[a-z]+=", template.read_text(encoding="utf-8")), template.name
