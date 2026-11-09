"""Routine files (`.synaptane/routines/<name>.md`): reading, writing, errors (no database)."""

import pytest

from synaptane.core.errors import SynaptaneError
from synaptane.services import routine_files, routines
from synaptane.services.routine_files import Config, parse, render

DIGEST = """---
title: Weekly digest
description: What the team learned
schedule: weekly friday 16:00
scope: projects, decisions
report: reports/weekly.md
inbox: false
email: members
webhook: true
enabled: true
template: digest
---

Write the summary of the week.
"""


def read(text: str, name: str = "weekly-digest") -> routine_files.Parsed:
    return parse(text.encode(), name)


def test_a_file_written_by_hand_is_read() -> None:
    parsed = read(DIGEST)
    assert parsed.error is None and parsed.warnings == [] and parsed.extra == []
    config = parsed.config
    assert config is not None
    assert (config.title, config.description, config.template) == (
        "Weekly digest",
        "What the team learned",
        "digest",
    )
    assert (config.frequency, config.weekday, config.monthday, config.at) == (
        "weekly",
        4,
        None,
        "16:00",
    )
    assert config.scope == ["projects", "decisions"]
    assert config.output_page == "reports/weekly.md" and config.output_email == "members"
    assert config.output_webhook and not config.output_inbox and config.enabled
    assert config.instructions == "Write the summary of the week."


def test_written_then_read_gives_the_same_settings() -> None:
    for key in routines.TEMPLATES:
        for locale in ("en", "fr"):
            config = routines.from_template(key, locale)
            config.scope = ["infra/servers", "a, b"]  # a comma: written as a JSON list
            config.description = "true"  # would read back as a boolean: quoted
            text = render(config)
            assert text.startswith("---\ntitle: ") and '"true"' in text
            assert read(text).config == config


def test_schedules_lists_and_flags_in_several_spellings() -> None:
    def config(header: str) -> Config:
        found = read(f"---\n{header}\nreport: r.md\n---\nDo it.\n").config
        assert found is not None, read(f"---\n{header}\nreport: r.md\n---\nDo it.\n").error
        return found

    assert config("schedule: daily 2:30").at == "02:30"
    assert config("schedule: Weekly MON 06:00").weekday == 0
    assert config("schedule: monthly 28 23:59").monthday == 28
    block = config("schedule: daily 02:00\nscope:\n  - infra\n  - 'clients/acme'\n")
    assert block.scope == ["infra", "clients/acme"]
    assert config('schedule: daily 02:00\nscope: ["a", "b"]').scope == ["a", "b"]
    assert config("schedule: daily 02:00\nenabled: no").enabled is False
    assert config("schedule: daily 02:00\nemail: none").output_email == ""
    assert config("schedule: daily 02:00").title == "Weekly digest"  # from the file name
    crlf = "﻿---\r\nschedule: daily 02:00\r\nreport: r.md\r\n---\r\nDo it.\r\n"
    assert read(crlf).config is not None


def test_an_unknown_field_is_reported_kept_and_harmless() -> None:
    text = DIGEST.replace(
        "template: digest\n", "template: digest\nwrites:\n  - drafts\n  - notes\norder: after\n"
    )
    parsed = read(text)
    assert parsed.config is not None and parsed.error is None
    assert [str(w) for w in parsed.warnings] == [
        "Unknown field, ignored: writes.",
        "Unknown field, ignored: order.",
    ]
    assert parsed.extra == ["writes:", "  - drafts", "  - notes", "order: after"]
    rewritten = render(parsed.config, routine_files.extra_lines(text.encode()))
    assert "writes:\n  - drafts\n  - notes\norder: after\n---" in rewritten
    assert read(rewritten).config == parsed.config
    unknown = read(DIGEST.replace("template: digest", "template: nightly"))
    assert unknown.config is not None and unknown.config.template == ""
    assert [str(w) for w in unknown.warnings] == ["Unknown template, ignored: nightly."]


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("Just text.\n", "The file must start with a header"),
        ("---\nschedule: daily 02:00\n", "Header not closed"),
        ("---\nscope infra\n---\nx\n", "Line 2 of the header is not understood: scope infra"),
        ("---\nreport: r.md\n---\nx\n", "schedule: missing"),
        ("---\nschedule: hourly\nreport: r.md\n---\nx\n", "is not understood"),
        ("---\nschedule: weekly someday 06:00\nreport: r.md\n---\nx\n", "is not understood"),
        ("---\nschedule: monthly 31 06:00\nreport: r.md\n---\nx\n", "Day of the month"),
        ("---\nschedule: daily 25:00\nreport: r.md\n---\nx\n", "Invalid time"),
        ("---\nschedule: daily 02:00\n---\nx\n", "at least one output"),
        ("---\nschedule: daily 02:00\nreport: r.md\n---\n\n", "Instructions"),
        ("---\nschedule: daily 02:00\nreport: r.md\ninbox: maybe\n---\nx\n", "inbox: true or"),
        ("---\nschedule: daily 02:00\nreport: ../x.md\n---\nx\n", "Invalid path"),
        ("---\nschedule: daily 02:00\nemail: everyone\nreport: r\n---\nx\n", "recipients"),
        ('---\nschedule: daily 02:00\ntitle: ["a"]\nreport: r\n---\nx\n', "single value"),
    ],
)
def test_an_invalid_file_says_why(text: str, error: str) -> None:
    parsed = read(text)
    assert parsed.config is None and parsed.error is not None
    assert error in str(parsed.error)


def test_a_file_too_large_is_not_read() -> None:
    parsed = parse(b"---\n" + b"x" * routine_files.MAX_FILE_SIZE, "big")
    assert parsed.config is None and "File too large" in str(parsed.error)


def test_names() -> None:
    assert routine_files.name_for("Résumé hebdomadaire") == "resume-hebdomadaire"
    assert routine_files.name_for("!!!") == "routine"
    assert len(routine_files.name_for("x" * 80)) == 40
    assert routine_files.free_name("digest", {"digest", "digest-2"}) == "digest-3"
    assert routine_files.check_name("Weekly-Digest.md") == "weekly-digest"
    for wrong in ("a/b", "-x", "x" * 41, "é"):
        with pytest.raises(SynaptaneError):
            routine_files.check_name(wrong)
