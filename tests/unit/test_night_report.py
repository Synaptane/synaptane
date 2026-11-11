"""The night without a database: its schedule in a routine's file, its order, its report in
plain text and in Slack's mrkdwn (agent texts are data)."""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from synaptane.services import night, routine_files
from synaptane.services.routine_files import Config, render
from synaptane.storage.models import Routine

CATALOG = json.loads(
    (Path(__file__).parents[2] / "src/synaptane/web/locales/fr.json").read_text(encoding="utf-8")
)


def read(header: str) -> routine_files.Parsed:
    return routine_files.parse(f"---\n{header}\nreport: r.md\n---\nDo it.\n".encode(), "r")


def test_night_schedules_in_english_and_french() -> None:
    for text, plan in (
        ("night", ("daily", None, None)),
        ("Night Monday", ("weekly", 0, None)),
        ("night mon", ("weekly", 0, None)),
        ("nuit lundi", ("weekly", 0, None)),
        ("chaque nuit", ("daily", None, None)),
        ("nuit 1", ("monthly", None, 1)),
    ):
        parsed = read(f"schedule: {text}")
        assert parsed.config is not None, (text, parsed.error)
        assert parsed.config.plan[1:4] == plan and parsed.config.night == "after", text
    assert routine_files.night_schedule("weekly monday 06:00") is None
    assert read("schedule: night 29").error is not None
    assert read("schedule: night tomorrow").error is not None


def test_place_in_the_night_is_written_back_in_english() -> None:
    before = read("schedule: nuit vendredi\nnight: avant").config
    assert before is not None and before.night == "before" and before.weekday == 4
    text = render(before)
    assert "schedule: night friday\nnight: before\n" in text
    assert routine_files.parse(text.encode(), "r").config == before
    assert read("schedule: night\nnight: après").config.night == "after"  # type: ignore[union-attr]
    assert read("schedule: night\nnight: sometime").error is not None
    # A fixed time with a `night` field: the field is ignored, and said so.
    fixed = read("schedule: daily 02:30\nnight: before")
    assert fixed.config is not None and fixed.config.night == ""
    assert fixed.warnings and "night: ignored" in fixed.warnings[0].template
    monthly = Config(title="t", instructions="i", frequency="monthly", monthday=3, weekday=None)
    monthly.night, monthly.output_page = "after", "r.md"
    assert routine_files.schedule_text(monthly) == "night 3"


def test_the_order_of_the_night() -> None:
    def routine(name: str, place: str) -> Routine:
        return Routine(name=name, title=name, night=place)

    steps = night.plan(
        [
            routine("b", "after"),
            routine("z", "before"),
            routine("a", "after"),
            routine("c", "before"),
        ],
        {"kind": "tidy"},
    )
    assert [s.get("name", "tidy") for s in steps] == ["c", "z", "tidy", "a", "b"]


def report(**values: object) -> night.Report:
    base = {
        "space": "Infra",
        "org": "Acme",
        "day": date(2026, 11, 11),
        "base": "https://brain.example.com/o/acme/infra",
        "tidy": night.TidyLine("nothing"),
        "routines": (),
    }
    return night.Report(**{**base, **values})  # type: ignore[arg-type]


def test_quiet_nights() -> None:
    assert report().quiet
    assert report(tidy=night.TidyLine("skipped", "disabled")).quiet
    assert report(checks=4).quiet  # the same every night: not news
    assert not report(tidy=night.TidyLine("done", filed=1)).quiet
    assert not report(tidy=night.TidyLine("failed")).quiet
    assert not report(routines=(night.RoutineLine("R", "after", "done"),)).quiet
    assert not report(routines=(night.RoutineLine("R", "after", "skipped", "budget"),)).quiet
    assert report(routines=(night.RoutineLine("R", "after", "skipped", "no_ai"),)).quiet


EVIL = "Fini <!channel> <!here|here> @here @Channel <@U123> <https://evil.example|cliquez> & co"


def full() -> night.Report:
    return report(
        tidy=night.TidyLine("done", filed=3, left=1, fixed=2, problems_left=1, summary=EVIL),
        routines=(
            night.RoutineLine(
                "Cohérence",
                "before",
                "done",
                summary=EVIL,
                page="rapports/cohérence semaine.md",
                inbox=("<!everyone> lien cassé",),
                routine_id="r1",
            ),
            night.RoutineLine(
                "Résumé",
                "after",
                "failed",
                problem=night.Problem("{detail}", {"detail": "<@U999> boom"}),
                routine_id="r2",
            ),
        ),
        checks=2,
        cost=Decimal("0.1234"),
        month=(Decimal("3.4"), Decimal("20")),
    )


def test_slack_text_lets_no_mention_or_link_through() -> None:
    text = night.write(full(), "en", night.SLACK_STYLE)
    for forbidden in ("<!channel>", "<!here", "<!everyone>", "<@U123>", "<@U999>", "<https://evil"):
        assert forbidden not in text
    assert "&lt;!channel&gt;" in text and "&amp; co" in text
    assert "@​here" in text and "@​Channel" in text
    # Our own links only, the path quoted: nothing can end the link early.
    links = [part.split("|")[0] for part in text.split("<")[1:]]
    assert links == [
        "https://brain.example.com/o/acme/infra/p/rapports/coh%C3%A9rence%20semaine.md",
        "https://brain.example.com/o/acme/infra/-/routines/r2",
        "https://brain.example.com/o/acme/infra/health",
        "https://brain.example.com/o/acme/infra/tidy",
    ]
    assert text.startswith("*Night of November 11, 2026 in “Infra” (Acme)*")
    assert "Tidy-up: 3 items filed, 1 left in the inbox, 2 problems fixed, 1 problem left." in text
    assert "- Cohérence (before the tidy-up): done" in text
    assert "Dropped into the inbox (1): “&lt;!everyone&gt; lien cassé”" in text
    assert "- Résumé (after the tidy-up): failed: &lt;@U999&gt; boom" in text
    assert "Cost of the night: ≈ €0.12 (this month: €3.40 of €20.00)" in text


def test_plain_text_in_french_keeps_the_text_and_drops_control_characters() -> None:
    sample = full()
    text = night.write(sample, "fr").replace(" ", " ").replace(" ", " ")
    assert "Nuit du 11 novembre 2026 dans « Infra » (Acme)" in text
    assert "Rangement : 3 éléments rangés, 1 laissé dans l'inbox, 2 problèmes corrigés" in text
    assert "1 problème laissé" in text
    assert "- Cohérence (avant le rangement) : fait" in text
    assert "Voir le rapport : https://brain.example.com/o/acme/infra/p/rapports/" in text
    assert "Coût de la nuit : ≈ 0,12 € (ce mois-ci : 3,40 € sur 20,00 €)" in text
    assert "Contrôles de l'équipe : 2 écarts au dernier commit." in text
    assert night.scrub_agent_text("a\x00b\x1b[31m\n c") == "a b [31m c"
    # A right-to-left override would show what follows backwards: an address disguised.
    assert night.scrub_agent_text("voir \u202ehttps://evil.com\u202c ici\u2067") == (
        "voir https://evil.com ici"
    )


def test_every_text_of_the_report_is_in_the_french_catalog() -> None:
    texts = [
        *night.REASONS.values(),
        *night.STATUSES.values(),
        *night.PLACES.values(),
        *(form for pair in night.COUNTS.values() for form in pair),
    ]
    assert sorted(t for t in texts if t not in CATALOG) == []


@pytest.mark.parametrize("url", ["ftp://x/y", "https://user:pw@hooks.example/x", "nope"])
def test_webhook_addresses_refused(url: str) -> None:
    with pytest.raises(Exception, match="webhook"):
        night.check_webhook_url(url)


def test_the_providers_address_is_for_the_admins_only() -> None:
    error = "Ollama: timed out (http://10.1.2.3:11434/api/chat)"
    problem = night._problem(error, night.scrub_agent_text)
    unknown = night.Problem("{detail}", {"detail": "boom at https://10.1.2.3/x"})
    lines = (
        night.RoutineLine("R", "after", "failed", problem=problem, routine_id="r"),
        night.RoutineLine("S", "after", "failed", problem=unknown, routine_id="s"),
    )
    members = night.write(report(routines=lines), "en")
    admins = night.write(report(routines=lines), "en", admin=True)
    assert "10.1.2.3" not in members and "does not answer in time" in members
    assert admins.count("10.1.2.3") == 2
