"""Routine schedule: due or not due, in the space's local time (no database)."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from synaptane.core.errors import SynaptaneError
from synaptane.services import routines
from synaptane.storage.models import Routine, Space


def space(tz: str = "Europe/Paris", repo: str | None = "file:///depot") -> Space:
    return Space(slug="infra", name="Infra", repo_url=repo, tidy_timezone=tz)


def routine(frequency: str = "weekly", **values: object) -> Routine:
    found = Routine(
        name="R",
        instructions="x",
        frequency=frequency,
        weekday=values.pop("weekday", 0),
        monthday=values.pop("monthday", None),
        at=values.pop("at", "06:00"),
        enabled=values.pop("enabled", True),
        last_date=values.pop("last_date", None),
    )
    assert not values
    return found


# Lundi 5 octobre 2026.
MONDAY_0430_UTC = datetime(2026, 10, 5, 4, 30, tzinfo=UTC)  # 06:30 in Paris
MONDAY_0330_UTC = datetime(2026, 10, 5, 3, 30, tzinfo=UTC)  # 05:30 in Paris


def test_weekly_routine_is_due_on_its_day_after_its_hour() -> None:
    assert routines.due(routine(), space(), MONDAY_0430_UTC) == date(2026, 10, 5)
    assert routines.due(routine(), space(), MONDAY_0330_UTC) is None  # too early
    tuesday = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)
    assert routines.due(routine(), space(), tuesday) is None  # not the right day
    done = routine(last_date=date(2026, 10, 5))
    assert routines.due(done, space(), MONDAY_0430_UTC) is None  # already requested that day


def test_the_space_timezone_decides_the_day_and_the_hour() -> None:
    # 04:30 UTC on Monday: 06:30 in Paris (due), but 00:30 in New York (not time yet).
    assert routines.due(routine(), space("America/New_York"), MONDAY_0430_UTC) is None
    # Monday 13:30 in Tokyo: due there.
    assert routines.due(routine(), space("Asia/Tokyo"), MONDAY_0430_UTC) == date(2026, 10, 5)
    # Sunday 22:30 UTC: already Monday 07:30 in Tokyo.
    sunday_late = datetime(2026, 10, 4, 22, 30, tzinfo=UTC)
    assert routines.due(routine(), space("Asia/Tokyo"), sunday_late) == date(2026, 10, 5)
    assert routines.due(routine(), space(), sunday_late) is None  # Sunday in Paris


def test_daily_and_monthly_routines() -> None:
    assert routines.due(routine("daily"), space(), MONDAY_0430_UTC) == date(2026, 10, 5)
    monthly = routine("monthly", weekday=None, monthday=5)
    assert routines.due(monthly, space(), MONDAY_0430_UTC) == date(2026, 10, 5)
    other = routine("monthly", weekday=None, monthday=6)
    assert routines.due(other, space(), MONDAY_0430_UTC) is None


def test_disabled_routine_or_space_without_repository_never_runs() -> None:
    assert routines.due(routine(enabled=False), space(), MONDAY_0430_UTC) is None
    assert routines.due(routine(), space(repo=None), MONDAY_0430_UTC) is None
    assert routines.due(routine(), space("Pas/UnFuseau"), MONDAY_0430_UTC) is None
    assert routines.next_run(routine(enabled=False), space()) is None


def test_next_run_is_the_next_matching_slot_in_local_time() -> None:
    paris = ZoneInfo("Europe/Paris")
    before = routines.next_run(routine(), space(), MONDAY_0330_UTC)
    assert before == datetime(2026, 10, 5, 6, 0, tzinfo=paris)
    after = routines.next_run(routine(), space(), MONDAY_0430_UTC)
    assert after == datetime(2026, 10, 12, 6, 0, tzinfo=paris)
    monthly = routine("monthly", weekday=None, monthday=1, at="08:15")
    assert routines.next_run(monthly, space(), MONDAY_0430_UTC) == datetime(
        2026, 11, 1, 8, 15, tzinfo=paris
    )
    friday = routine(weekday=4, at="16:00")
    assert routines.next_run(friday, space(), MONDAY_0430_UTC) == datetime(
        2026, 10, 9, 16, 0, tzinfo=paris
    )


def test_output_page_and_scope_are_cleaned() -> None:
    assert routines.clean_page(" content/rapports/semaine ") == "rapports/semaine.md"
    assert routines.clean_page("") == ""
    assert routines.clean_scope(["infra/", "/infra", "", "clients/acme"]) == [
        "infra",
        "clients/acme",
    ]
    for bad in ("../secret.md", ".git/config.md"):
        with pytest.raises(SynaptaneError):
            routines.clean_page(bad)
    with pytest.raises(SynaptaneError):
        routines.clean_scope(["../hors"])


def test_templates_are_ready_to_use() -> None:
    assert set(routines.TEMPLATES) == {"coherence", "digest"}
    coherence, digest = routines.TEMPLATES["coherence"], routines.TEMPLATES["digest"]
    french = coherence.text_for("fr")
    assert french.output_page == "rapports/coherence.md" and coherence.output_inbox
    assert digest.text_for("fr").output_page == "rapports/semaine.md"
    assert digest.output_email == "members"
    assert coherence.frequency == digest.frequency == "weekly"
    # One line per paragraph or bullet, as in the form (no wrapping at 100 columns).
    assert "depuis le dernier passage, puis pages liées" in french.instructions
    assert "\n  " not in french.instructions + digest.text_for("fr").instructions
    assert routines.flow("a\nb\n- c\n  d\ne\n\nf") == "a b\n- c d\ne\n\nf"
