"""The tests' HTML helper: if it were wrong, the permission checks that rely on it ("the
reader does not see the form") would pass without checking anything."""

import pytest

from tests.htmlutil import data_test, parse, select, select_one

PAGE = """<!doctype html>
<html><body>
  <nav class="sidebar">
    <a class="button" href="/admin" data-test="admin-link">Administration</a>
  </nav>
  <form class="card" method="post" action="/o/acme/infra/-/members" data-test="member-add">
    <h2>Ajouter un membre</h2>
    <input type="hidden" name="csrf" value="abc">
    <label for="u">Nom<br>d'utilisateur</label><input id="u" name="username" required>
    <select name="role"><option value="reader" selected>Lecteur</option></select>
    <button type="submit">Ajouter</button>
  </form>
  <ul><li>un<li>deux &amp; trois</ul>
  <svg><use href="#icon-plus"/></svg>
  <script>const fake = '<a data-test="admin-link">';</script>
  <p>Fin</p></div>
</body></html>
"""


def test_data_test_selector_finds_the_element_with_its_attributes_and_text() -> None:
    form = select_one(PAGE, data_test("member-add"))
    assert form.tag == "form" and form.action == "/o/acme/infra/-/members"
    assert form.get("method") == "post" and "card" in form.classes
    # The text of the descendants, whitespace normalized, across empty elements.
    assert form.text == "Ajouter un membre Nom d'utilisateur Lecteur Ajouter"
    link = select_one(PAGE, data_test("admin-link"))  # the <script> one is not an element
    assert link.href == "/admin" and link.text == "Administration"


def test_void_elements_do_not_swallow_their_siblings() -> None:
    form = select_one(PAGE, "form")
    inputs = select(form, "input")
    assert [i.get("name") for i in inputs] == ["csrf", "username"]
    assert inputs[1].get("required") == ""  # boolean attribute
    assert select_one(form, "button").parent is form  # not a child of <input> nor of <br>
    assert select_one(PAGE, "svg use").get("href") == "#icon-plus"  # balise auto-fermante


def test_compound_descendant_and_child_selectors() -> None:
    assert len(select(PAGE, 'form[data-test="member-add"] input[name="csrf"]')) == 1
    assert len(select(PAGE, "form > input")) == 2
    assert select(PAGE, "body > input") == []  # descendant, but not a direct child
    assert [a.href for a in select(PAGE, "nav.sidebar a.button")] == ["/admin"]
    assert len(select(PAGE, "#u")) == 1 and len(select(PAGE, "[required]")) == 1
    assert len(select(PAGE, '[action^="/o/acme"]')) == 1
    assert len(select(PAGE, '[action$="/-/members"]')) == 1
    assert len(select(PAGE, "[action*='infra']")) == 1
    assert len(select(PAGE, "[class~=card]")) == 1
    assert select(PAGE, data_test("absent")) == []


def test_unclosed_and_stray_tags_are_tolerated() -> None:
    items = select(PAGE, "ul > li")
    assert [li.text for li in items] == ["un", "deux & trois"]  # entities decoded
    assert select_one(PAGE, "p").text == "Fin"  # the orphan </div> is ignored
    assert parse("texte <b>gras</b>").text == "texte gras"
    # A block closes the open paragraph; a cell closes the previous one, not the table.
    assert select("<p>un<div>bloc</div>", "p div") == []
    table = "<table><tr><td>a<td>b<tr><td>c</table><span>après</span>"
    assert [row.text for row in select(table, "table > tr")] == ["ab", "c"]
    assert select(table, "td span") == []


def test_select_one_refuses_zero_or_several_matches() -> None:
    with pytest.raises(LookupError, match="0 élément"):
        select_one(PAGE, data_test("absent"))
    with pytest.raises(LookupError, match="2 élément"):
        select_one(PAGE, "form > input")
    with pytest.raises(ValueError):
        select(PAGE, "a:hover")
