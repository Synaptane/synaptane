"""HTML to Markdown for imported pages: what Markdown can say is kept, Confluence macros become
their Markdown equivalent, and nothing of an untrusted page is run, loaded or let through."""

from synaptane.core.html_markdown import (
    MAX_DEPTH,
    callout,
    destination,
    escape,
    parse,
    safe_url,
    to_markdown,
)
from synaptane.core.markdown import parser


def md(html: str, **options: object) -> str:
    return to_markdown(html, **options).strip()  # type: ignore[arg-type]


def test_headings_paragraphs_and_emphasis() -> None:
    html = (
        "<h1>Titre</h1><p>Du <strong>gras </strong>, de l'<em>italique</em>, <s>barré</s>, "
        "<mark>surligné</mark> et <code>a `b`</code>.</p>"
    )
    assert md(html) == (
        "# Titre\n\nDu **gras** , de l'*italique*, ~~barré~~, ==surligné== et `` a `b` ``."
    )
    assert md("<h1>A</h1><h2>B :</h2><h6>C</h6>", shift=1) == "## A\n\n### B\n\n###### C"
    assert md("<p>a<br/>b<br></p>") == "a\\\nb"
    assert md("<p><b><strong>x</strong></b></p>") == "**x**"
    assert md("<p><b> </b></p><p></p>") == ""


def test_text_is_escaped_so_that_it_stays_text() -> None:
    assert escape("*a* [b] `c` snake_case _x_ ==y== %%z%% ~~w~~ &amp;") == (
        "\\*a\\* \\[b\\] \\`c\\` snake_case \\_x\\_ =\\=y=\\= %\\%z%\\% ~\\~w~\\~ \\&amp;"
    )
    assert md("<p># pas un titre</p><p>- pas une liste</p><p>1. non plus</p>") == (
        "\\# pas un titre\n\n\\- pas une liste\n\n1\\. non plus"
    )
    rendered = parser().render(md("<p>&lt;script&gt;alert(1)&lt;/script&gt; *x*</p>"))
    assert "<script>" not in rendered and "*x*" in rendered


def test_lists_nested_ordered_and_tasks() -> None:
    html = (
        "<ul><li>un</li><li>deux<ul><li>imbriqué</li></ul></li></ul>"
        "<ol start='3'><li>trois</li><li><p>quatre</p><p>suite</p></li></ol>"
    )
    assert md(html) == "- un\n- deux\n  - imbriqué\n\n3. trois\n4. quatre\n\n   suite"
    tasks = '<ul class="inline-task-list"><li class="checked">fait</li><li>à faire</li></ul>'
    assert md(tasks) == "- [x] fait\n- [ ] à faire"
    boxes = (
        '<ul><li><input type="checkbox" checked>oui</li><li><input type="checkbox">non</li></ul>'
    )
    assert md(boxes) == "- [x] oui\n- [ ] non"
    assert md("<ul>texte perdu<li>a</li></ul>") == "- texte perdu\n- a"
    assert md("<ol start='x'><li>a</li></ol>") == "1. a"


def test_tables_with_header_spans_and_blocks_in_cells() -> None:
    html = (
        "<table><thead><tr><th>Nom</th><th>Valeur</th></tr></thead><tbody>"
        "<tr><td>a|b</td><td><p>x</p><p>y</p></td></tr><tr><td colspan='2'>large</td></tr>"
        "<tr><td><pre>code</pre></td><td><h2>titre</h2></td></tr></tbody></table>"
    )
    assert md(html) == (
        "| Nom | Valeur |\n| --- | --- |\n| a\\|b | x y |\n| large | |\n| `code` | **titre** |"
    )
    assert md("<table><tr><td>a</td></tr></table>") == "| |\n| --- |\n| a |"
    assert md("<table></table>") == ""


def test_code_blocks_keep_their_language_and_their_backticks() -> None:
    confluence = (
        '<pre class="syntaxhighlighter-pre" data-syntaxhighlighter-params="brush: bash; '
        'gutter: false">echo "&lt;hi&gt;"\nls</pre>'
    )
    assert md(confluence) == '```bash\necho "<hi>"\nls\n```'
    assert md('<pre><code class="language-python">x = 1</code></pre>') == "```python\nx = 1\n```"
    assert md("<pre>a ``` b</pre>") == "````\na ``` b\n````"
    assert md('<pre data-syntaxhighlighter-params="brush: text">x</pre>') == "```\nx\n```"
    assert md("<pre>a<br>b<div>c</div>d</pre>") == "```\na\nb\nc\nd\n```"


def test_links_and_images_go_through_the_caller() -> None:
    html = (
        '<p><a href="Page_12.html">page</a> <a href="https://x.org/a b">web</a> '
        '<img src="a.png" alt="A [1]"></p>'
    )
    moved = md(
        html, link=lambda h: "page.md" if h == "Page_12.html" else h, image=lambda s: "img/a.png"
    )
    assert moved == "[page](page.md) [web](<https://x.org/a b>) ![A \\[1\\]](img/a.png)"
    assert md('<p><a href="x">texte</a></p>', link=lambda h: None) == "texte"
    assert md('<p><a href="x"></a></p>') == "[x](x)"
    assert md('<p><img src="x.png"></p>', image=lambda s: None) == ""
    assert md('<p><a class="confluence-userlink" href="/display/~ada">Ada</a></p>') == "Ada"
    assert destination("a (b).md") == "<a (b).md>" and destination("<x>") == "%3Cx%3E"


def test_unsafe_content_is_dropped() -> None:
    html = (
        "<script>alert(1)</script><style>p{}</style><iframe src='https://evil'></iframe>"
        "<p><a href='javascript:alert(1)'>clic</a> <a href=' JaVaScRiPt:x'>b</a> "
        "<img src='data:image/png;base64,AAAA'> <img src='vbscript:x'> <img src=''></p>"
        "<form><input name='q'><button>Envoyer</button></form><object data='x'></object>"
    )
    assert md(html) == "clic b"
    assert not safe_url("javascript:alert(1)") and safe_url("mailto:a@b.c") and safe_url("p.html")
    assert not safe_url("http://[::1")  # malformed


def test_confluence_panels_become_callouts() -> None:
    kinds = {"information": "info", "tip": "tip", "note": "warning", "warning": "danger"}
    for css, kind in kinds.items():
        html = (
            f'<div class="confluence-information-macro confluence-information-macro-{css}">'
            '<p class="title">Attention <b>!</b></p><span class="aui-icon"></span>'
            '<div class="confluence-information-macro-body"><p>Corps</p></div></div>'
        )
        assert md(html) == f"> [!{kind}] Attention !\n>\n> Corps"
    assert md('<div class="confluence-information-macro"><p>x</p></div>') == "> [!info]"
    panel = (
        '<div class="panel" data-panel-type="success"><div class="panelHeader"><b>Titre</b></div>'
        '<div class="panelContent"><p>Contenu</p></div></div>'
    )
    assert md(panel) == "> [!tip] Titre\n>\n> Contenu"
    assert md('<div class="panel"><p>Nu</p></div>') == "> [!note]\n>\n> Nu"
    expand = (
        '<div class="expand-container"><div class="expand-control"><span class="expand-icon">'
        '</span><span class="expand-control-text">Plus</span></div><div class="expand-content">'
        "<p>Caché</p></div></div>"
    )
    assert md(expand) == "> [!note]- Plus\n>\n> Caché"
    code = (
        '<div class="code panel pdl"><div class="codeHeader panelHeader"><b>run.sh</b></div>'
        '<div class="codeContent"><pre data-syntaxhighlighter-params="brush: sh">ls</pre>'
        "</div></div>"
    )
    assert md(code) == "**run.sh**\n\n```sh\nls\n```"
    assert md('<div class="preformatted panel"><pre>brut</pre></div>') == "```\nbrut\n```"
    assert md('<div class="toc-macro"><ul><li>Sommaire</li></ul></div><p>x</p>') == "x"
    lozenge = '<p>État : <span class="status-macro aui-lozenge">FAIT</span></p>'
    assert md(lozenge) == "État : **FAIT**"
    emoticons = (
        '<p><img class="emoticon" data-emoji-fallback="✅" alt="(tick)"> '
        '<img class="emoticon" data-emoji-fallback=":check:" alt="(ok)"></p>'
    )
    assert md(emoticons) == "✅ (ok)"


def test_callouts_are_read_by_the_renderer() -> None:
    html = parser().render(callout("warning", "Titre", "Corps\n\n- a"))
    assert 'class="callout callout-warn"' in html and "Titre" in html and "<li>a</li>" in html
    assert callout("note", "", "") == "> [!note]"


def test_other_blocks() -> None:
    assert md("<blockquote><p>a</p><p>b</p></blockquote>") == "> a\n>\n> b"
    assert md("<p>a</p><hr><p>b</p>") == "a\n\n---\n\nb"
    assert md("<dl><dt>Terme</dt><dd>Définition</dd></dl>") == "**Terme**\n\nDéfinition"
    assert md("<details><summary>Titre</summary><p>Dedans</p></details>") == (
        "> [!note]- Titre\n>\n> Dedans"
    )
    assert md("<aside><p>Note</p></aside>") == "> [!note]\n>\n> Note"
    assert md("<figure><img src='a.png'><figcaption>Légende</figcaption></figure>") == (
        "![](a.png)\n\n*Légende*"
    )
    assert md("<a href='x'><div>bloc</div></a>") == "[bloc](x)"


def test_broken_and_hostile_html_is_tolerated() -> None:
    assert md("<p>a<p>b<div>c</p></div><li>d") == "a\n\nb\n\nc\n\nd"
    assert md("</div></p>texte<b>gras") == "texte**gras**"
    assert (
        md("<table><tr><td>a<td>b<tr><td>c</table>") == "| | |\n| --- | --- |\n| a | b |\n| c | |"
    )
    deep = "<div>" * 5000 + "fond" + "</div>" * 5000
    assert md(deep) == "fond"
    nested = "<ul><li>" * (MAX_DEPTH + 20) + "x"
    assert "x" in md(nested)
    root = parse("<p id='a' class='b c'>t</p>")
    assert root.find(lambda n: n.get("id") == "a") is not None
    assert root.find_all(lambda n: "c" in n.classes)[0].text() == "t"
