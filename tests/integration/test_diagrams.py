"""Mermaid diagrams: what a page holds is not trusted, so the drawing stays in a sandbox."""

import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

import synaptane
from synaptane.server import CSP, DIAGRAM_CSP
from synaptane.web.render import render_markdown

STATIC = Path(synaptane.__file__).parent / "web" / "static"
MERMAID = "vendor/mermaid-11.17.2.min.js"
# dist/mermaid.min.js of the npm package, checked against the registry's integrity hash.
MERMAID_SHA256 = "581ed7d74bd9048d0e3a91363927d72ef22942d7722546b27f7cc29e35390eb8"


def test_the_frame_has_no_access_to_anything(client: TestClient) -> None:
    frame = client.get("/diagram")
    assert frame.status_code == 200
    policy = frame.headers["content-security-policy"]
    assert policy == DIAGRAM_CSP
    assert "sandbox allow-scripts;" in policy  # no allow-same-origin: no cookies, no Synaptane
    assert "default-src 'none'" in policy and "script-src 'self'" in policy
    assert "unsafe-eval" not in policy and "connect-src" not in policy
    assert "frame-ancestors 'self'" in policy
    assert f"/static/{MERMAID}" in frame.text and "/static/diagram.js" in frame.text


def test_the_policy_of_the_pages_does_not_change(client: TestClient) -> None:
    assert "unsafe-inline" not in CSP
    login_page = client.get("/login")
    assert login_page.headers["content-security-policy"] == CSP
    assert 'data-diagram-label="' in login_page.text


def test_the_scripts_of_the_frame_are_served(client: TestClient) -> None:
    assert client.get(f"/static/{MERMAID}").status_code == 200
    assert client.get("/static/diagram.js").status_code == 200


def test_the_shipped_mermaid_is_the_one_from_npm() -> None:
    assert hashlib.sha256((STATIC / MERMAID).read_bytes()).hexdigest() == MERMAID_SHA256


def test_a_diagram_stays_code_in_the_page() -> None:
    html = render_markdown(
        '```mermaid\ngraph TD\n  A["<script>x</script>"] --> B\n```\n',
        "a.md",
        lambda p: p,
        lambda p: p,
    )
    assert '<code class="language-mermaid">' in html
    assert "<script" not in html and "&lt;script&gt;" in html


def test_the_frame_is_sandboxed_on_the_page_side_too() -> None:
    assert 'setAttribute("sandbox", "allow-scripts")' in (STATIC / "preferences.js").read_text()
