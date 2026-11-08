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


def test_pdf_link_shows_on_phones() -> None:
    """Mobile browsers leave the PDF frame empty: the “Open the PDF” link is hidden on large
    screens only, and shown at least up to 640 px wide."""
    assert ".doc-open{display:none}" in CSS
    rule = re.search(
        r"@media \(max-width:(\d+)px\),\(pointer:coarse\)\{[^{}]*\.doc-open\{display:block", CSS
    )
    assert rule is not None and int(rule.group(1)) >= 640


def test_the_preview_never_leaves_the_text_being_written() -> None:
    """The editor's preview: a POST with the form token (multipart: the 1 MiB of a field is
    counted in bytes, not in percent-escapes), the sign-in page (session expired) is never shown
    as a preview, links open in a new tab, resolved from the page's reading address."""
    preview = _function("setupPreview")
    assert 'method: "POST"' in preview and "new FormData()" in preview
    assert "URLSearchParams" not in preview and "form.elements.csrf.value" in preview
    assert "response.redirected" in preview and "previewExpired" in preview
    assert "previewMax" in preview and "previewTooLong" in preview
    isolate = _function("isolatePreview")
    assert 'link.target = "_blank"' in isolate and "#preview-" in isolate
    assert "new URL(href, page)" in isolate and "new URL(src, page)" in isolate
    # The textarea is only hidden, never removed or disabled: it is always submitted.
    assert "editor.hidden" in preview and "editor.disabled" not in preview
    # Mermaid in the preview: the reading view's drawing, its frame kept while it is edited.
    patch = _function("patchPreview")
    assert "observeDiagrams(node)" in patch and "redrawDiagram(frame, target)" in patch
    assert "forgetDiagrams(node)" in patch


def test_the_preview_does_not_pile_up_requests() -> None:
    """One request at a time: no abort-and-resend (the server would keep rendering the
    abandoned one), the latest text once the answer is in, a pause at least as long as the last
    rendering, and a 429 (another preview of the account) waited out without an error."""
    preview = _function("setupPreview")
    assert "abort" not in preview and "AbortController" not in preview
    assert "if (busy) { again = true; return; }" in preview
    assert "pause = Math.max(700, performance.now() - started)" in preview
    assert "response.status === 429" in preview
    assert "key === shownKey" in preview  # a text already shown is not asked for again


def test_a_removed_diagram_stops_listening() -> None:
    """A frame taken out of the page stops listening, even before its first message, and its
    code block is no longer watched."""
    assert "diagramStates.get(frame)?.stop()" in _function("removeDiagram")
    assert "nearDiagrams?.unobserve(code)" in _function("forgetDiagrams")
    draw = _function("drawDiagram")
    assert 'removeEventListener("message", listen)' in draw
    diagram = (STATIC / "diagram.js").read_text(encoding="utf-8")
    assert "queue = queue.then(" in diagram and "observed = true" in diagram
