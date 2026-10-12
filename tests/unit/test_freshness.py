"""Verification and freshness: lenient front matter reading, state, minimal rewrite."""

from datetime import UTC, date, datetime

import pytest

from synaptane.core.errors import SynaptaneError
from synaptane.services import freshness
from synaptane.services.freshness import (
    NEVER,
    Freshness,
    Meta,
    Period,
    clean_period,
    keep_verification,
    of_text,
    parse_period,
    read_meta,
    same_but_verification,
    scalar,
    state_of,
    update_front_matter,
    without_verification,
)
from synaptane.storage.models import Space

TODAY = date(2026, 10, 7)


def test_period_parsing_is_tolerant() -> None:
    assert parse_period("90d") == Period(90, "d")
    assert parse_period("6m") == Period(6, "m")
    assert parse_period("1y") == Period(1, "y")
    assert parse_period(" 12 semaines ") == Period(12, "w")
    assert parse_period("6 mois") == Period(6, "m")
    assert parse_period("1 an") == Period(1, "y")
    assert parse_period("'3m'") == Period(3, "m")
    assert parse_period("45") == Period(45, "d")  # no unit: days
    assert parse_period("never") == NEVER and parse_period("jamais") == NEVER
    assert parse_period("0") == NEVER and parse_period("0m") == NEVER
    for unreadable in ("", None, "souvent", "6x", "-3m", "m6"):
        assert parse_period(unreadable) is None
    assert parse_period("99999d") is None  # more than 4 digits: unreadable
    assert parse_period("5000d") == Period(3660, "d")  # capped
    assert clean_period(" 6 mois ") == "6m" and clean_period("") == ""
    assert clean_period("jamais") == "never"
    with pytest.raises(SynaptaneError):
        clean_period("souvent")


def test_period_arithmetic_handles_month_ends() -> None:
    assert Period(1, "m").after(date(2026, 1, 31)) == date(2026, 2, 28)
    assert Period(1, "y").after(date(2028, 2, 29)) == date(2029, 2, 28)
    assert Period(6, "m").after(date(2026, 10, 3)) == date(2027, 4, 3)
    assert Period(2, "w").after(date(2026, 10, 3)) == date(2026, 10, 17)
    assert Period(90, "d").after(date(2026, 10, 3)) == date(2027, 1, 1)
    assert str(Period(90, "d")) == "90d"


def test_reading_is_tolerant() -> None:
    page = "---\ntitle: Proxmox\nowner: ada\nverified: 2026-10-03\nreview_every: 6m\n---\n# P\n"
    meta = read_meta(page)
    assert (meta.owner, meta.verified, meta.review_every) == ("ada", date(2026, 10, 3), "6m")
    # Quotes, comments, date and time, Windows line endings, BOM.
    quoted = (
        '﻿---\r\nowner: "Ada Lovelace"  # équipe infra\r\n'
        "verified: '2026-10-03T09:30:00Z'\r\nreview_every: 90d # trimestre\r\n---\r\nTexte\r\n"
    )
    meta = read_meta(quoted)
    assert (meta.owner, meta.verified, meta.review_every) == (
        "Ada Lovelace",
        date(2026, 10, 3),
        "90d",
    )
    assert read_meta('---\nowner: "a \\"b\\""\n---\n').owner == 'a "b"'
    assert read_meta("---\nowner: 'l''équipe'\n---\n").owner == "l'équipe"
    # Nothing readable: empty, never an error.
    for text in (
        "",
        "# Sans front matter\n",
        "---\nowner: ada\n",  # block never closed
        "Texte\n---\nowner: ada\n---\n",  # not at the top
        "---\n---\n",
        "---\n: rien\n  - liste\n---\n",
        "```\n---\nowner: ada\n---\n```\n",
    ):
        assert read_meta(text).empty, text
    assert read_meta("---\nverified: hier\n---\n").verified is None  # unreadable date
    assert read_meta("---\nverified: hier\n---\n").raw == {"verified": "hier"}
    # A value spanning several lines is not an owner.
    assert read_meta("---\nowner:\n  - ada\n  - bob\nverified: 2026-10-03\n---\n").owner == ""
    assert read_meta("---\nowner: ada\nowner: bob\n---\n").owner == "ada"  # the first one
    assert read_meta("---\n  owner: ada\n---\n").empty  # indented: not at the top level
    assert read_meta("...\n").empty
    assert read_meta("---\nowner: ada\n...\nSuite\n").owner == "ada"  # YAML end "..."


def test_states() -> None:
    def state(text: str, default: str = "") -> Freshness:
        return of_text(text, default, TODAY)

    never = state("# Page\n")
    assert (never.state, never.is_verified, never.stale) == ("never", False, False)
    assert state("---\nowner: ada\n---\n").owner == "ada"
    assert state("---\nowner: ada\n---\n").state == "never"
    ok = state("---\nverified: 2026-10-03\n---\n")
    assert ok.state == "verified" and ok.due is None and ok.period == ""  # no review period
    with_default = state("---\nverified: 2026-04-07\n---\n", "6m")
    assert with_default.state == "stale" and with_default.due == date(2026, 10, 7)
    assert with_default.inherited and with_default.period == "6m"
    assert state("---\nverified: 2026-04-08\n---\n", "6m").state == "verified"
    own = state("---\nverified: 2026-10-01\nreview_every: 3d\n---\n", "1y")
    assert own.state == "stale" and own.due == date(2026, 10, 4) and not own.inherited
    exempt = state("---\nverified: 2020-01-01\nreview_every: never\n---\n", "6m")
    assert exempt.state == "verified" and exempt.due is None and exempt.period == "never"
    unreadable = state("---\nverified: 2026-01-01\nreview_every: souvent\n---\n", "1m")
    assert unreadable.inherited and unreadable.state == "stale"  # falls back to the space
    assert ok.search_factor() > 1 > with_default.search_factor()
    assert never.search_factor() == 1
    assert with_default.as_dict() == {
        "state": "stale",
        "verified": False,
        "stale": True,
        "verified_at": "2026-04-07",
        "owner": None,
        "review_due": "2026-10-07",
        "review_every": "6m",
    }
    assert state_of(Meta(), "6m", TODAY) == Freshness("never", period="6m", inherited=True)


def test_update_keeps_everything_else_byte_for_byte() -> None:
    page = (
        "---\ntitle: Proxmox  # garde\ntags: [infra, sauvegarde]\nverified: 2025-01-01\n"
        "notes: |\n  texte libre\n---\n# Proxmox\n\nTexte **gras**  \n"
    )
    updated = update_front_matter(page, {"verified": "2026-10-07", "owner": "ada"})
    assert updated == (
        "---\ntitle: Proxmox  # garde\ntags: [infra, sauvegarde]\nverified: 2026-10-07\n"
        "notes: |\n  texte libre\nowner: ada\n---\n# Proxmox\n\nTexte **gras**  \n"
    )
    # No block: created at the top, the text follows as is (Windows line ending kept).
    windows = "# Titre\r\n\r\nTexte\r\n"
    created = update_front_matter(windows, {"verified": "2026-10-07"})
    assert created == "---\r\nverified: 2026-10-07\r\n---\r\n" + windows
    bom = "﻿# Titre\n"
    assert update_front_matter(bom, {"owner": "ada"}) == "﻿---\nowner: ada\n---\n# Titre\n"
    assert update_front_matter(windows, {"owner": None}) == windows  # nothing to remove
    # A page starting with a horizontal rule (block never closed): block added before it.
    ruled = "---\nTexte sous un trait\n"
    assert update_front_matter(ruled, {"owner": "ada"}) == "---\nowner: ada\n---\n" + ruled
    # Replace a multi-line value, remove a field, remove the last one.
    listed = "---\nowner:\n  - ada\n  - bob\ntitle: T\n---\nX\n"
    assert (
        update_front_matter(listed, {"owner": "carol"}) == "---\nowner: carol\ntitle: T\n---\nX\n"
    )
    assert update_front_matter("---\nowner: ada\ntitle: T\n---\nX\n", {"owner": None}) == (
        "---\ntitle: T\n---\nX\n"
    )
    assert update_front_matter("---\nowner: ada\n---\nX\n", {"owner": None}) == "X\n"
    assert update_front_matter("---\nowner:   ada\n---\n", {"owner": "bob"}) == (
        "---\nowner:   bob\n---\n"  # alignment kept
    )
    # Key stuck to the colon, empty value, empty block, block without final newline.
    assert update_front_matter("---\nowner:\n---\n", {"owner": "bob"}) == "---\nowner: bob\n---\n"
    assert update_front_matter("---\n---\nX", {"owner": "bob"}) == "---\nowner: bob\n---\nX"
    assert update_front_matter("---\ntitle: T\n---", {"owner": "b"}) == (
        "---\ntitle: T\nowner: b\n---"
    )
    assert update_front_matter("---\r\ntitle: T\r\n---\r\n", {"owner": "b"}) == (
        "---\r\ntitle: T\r\nowner: b\r\n---\r\n"
    )


def test_scalar_quotes_only_when_needed() -> None:
    assert scalar("ada") == "ada"
    assert scalar("Ada Lovelace") == "Ada Lovelace"
    assert scalar("ada@example.com") == "ada@example.com"
    assert scalar("2026-10-07") == "2026-10-07" and scalar("6m") == "6m"
    assert scalar("yes") == '"yes"' and scalar("null") == '"null"'
    assert scalar("équipe: infra") == '"équipe: infra"'
    assert scalar("#ops") == '"#ops"' and scalar("a #b") == '"a #b"'
    assert read_meta(f"---\nowner: {scalar('équipe: infra')}\n---\n").owner == "équipe: infra"


def test_comparison_ignores_verification_fields_only() -> None:
    before = "# Proxmox\n\nSauvegarde à 2h.\n"
    verified = update_front_matter(before, {"verified": "2026-10-07", "owner": "ada"})
    assert without_verification(verified) == before
    assert same_but_verification(before, verified)
    reverified = update_front_matter(verified, {"verified": "2027-01-01", "review_every": "6m"})
    assert same_but_verification(verified, reverified)
    edited = verified.replace("2h", "3h")
    assert not same_but_verification(before, edited)
    other = update_front_matter(verified, {"title": "Autre"})
    assert not same_but_verification(verified, other)  # other front matter field
    kept = "---\ntitle: T\n---\nX\n"
    assert without_verification(kept) == kept
    assert same_but_verification(kept, update_front_matter(kept, {"owner": "ada"}))


def test_agents_cannot_erase_or_forge_verification() -> None:
    old = "---\ntitle: T\nowner: ada\nverified: 2026-10-03\n---\n# T\n\nAncien.\n"
    # Rewritten without the front matter: put back.
    text, fixed = keep_verification(old, "# T\n\nNouveau.\n")
    assert fixed and text == "---\nowner: ada\nverified: 2026-10-03\n---\n# T\n\nNouveau.\n"
    # Block kept but date changed: restored; title changed: allowed.
    text, fixed = keep_verification(
        old, old.replace("2026-10-03", "2030-01-01").replace("T\n", "U\n", 1)
    )
    assert fixed and read_meta(text).verified == date(2026, 10, 3) and "title: U" in text
    # Nothing changed: as is.
    assert keep_verification(old, old.replace("Ancien", "Nouveau")) == (
        old.replace("Ancien", "Nouveau"),
        False,
    )
    # Page never verified: an agent cannot claim it for itself.
    text, fixed = keep_verification("# T\n", "---\nverified: 2026-10-07\n---\n# T\n")
    assert fixed and text == "# T\n"
    assert keep_verification(None, "# Nouvelle\n") == ("# Nouvelle\n", False)


def test_owner_matching_and_local_day() -> None:
    assert freshness.owned_by("Ada", ("ada", "ada@example.com"))
    assert freshness.owned_by("@ada", ("ada",))
    assert freshness.owned_by("ADA@example.com", ("ada", "ada@example.com"))
    assert not freshness.owned_by("Ada Lovelace", ("ada",))
    assert not freshness.owned_by("", ("ada",))
    space = Space(tidy_timezone="Pacific/Auckland")
    late = datetime(2026, 10, 7, 20, 0, tzinfo=UTC)
    assert freshness.today_for(space, late) == date(2026, 10, 8)
    assert freshness.today_for(Space(tidy_timezone="Nulle/Part"), late) == date(2026, 10, 7)
    assert freshness.is_page("a/B.MD") and not freshness.is_page("a.pdf")
