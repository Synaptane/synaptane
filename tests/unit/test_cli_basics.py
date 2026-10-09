from collections.abc import Iterator
from pathlib import Path

import pytest

import synaptane
from synaptane import __version__, git_commit, short_commit, version_label
from synaptane.cli import main


@pytest.fixture
def fresh_commit() -> Iterator[None]:
    """`git_commit` is cached for the lifetime of the process: start from scratch."""
    git_commit.cache_clear()
    yield
    git_commit.cache_clear()


def test_without_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "usage: synaptane" in capsys.readouterr().out


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"synaptane {__version__} ({short_commit()})"


def test_commit_comes_from_the_image(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fresh_commit: None
) -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    monkeypatch.setenv("SYNAPTANE_GIT_SHA", sha)
    assert git_commit() == sha
    assert version_label() == f"{__version__} (0123456)"
    with pytest.raises(SystemExit):
        main(["--version"])
    assert capsys.readouterr().out.strip() == f"synaptane {__version__} (0123456)"


def test_commit_falls_back_to_the_repository_then_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fresh_commit: None
) -> None:
    # Image built without --build-arg: "unknown", so the source repository is asked instead.
    monkeypatch.setenv("SYNAPTANE_GIT_SHA", "unknown")
    from_sources = git_commit()
    assert from_sources == synaptane.UNKNOWN_COMMIT or len(from_sources) == 40

    # Package installed outside any repository: unknown, without error.
    git_commit.cache_clear()
    monkeypatch.delenv("SYNAPTANE_GIT_SHA")
    (tmp_path / "synaptane").mkdir()
    monkeypatch.setattr(synaptane, "__file__", str(tmp_path / "synaptane" / "__init__.py"))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    assert git_commit() == synaptane.UNKNOWN_COMMIT

    # Not even git.
    git_commit.cache_clear()
    monkeypatch.setenv("PATH", str(tmp_path))
    assert git_commit() == "unknown" and short_commit() == "unknown"


def test_access_log_hides_tokens() -> None:
    import logging

    from synaptane.core.logs import AccessFilter

    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        "%s - %s %s",
        ("1.2.3.4", "GET", "/reset/abc?x=1"),
        None,
    )
    assert AccessFilter().filter(record) and record.args == ("1.2.3.4", "GET", "/reset/…?…")
    probe = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0, "%s", ("h", "GET", "/readyz"), None
    )
    assert not AccessFilter().filter(probe)


def test_rate_limit_counts_per_key_over_a_sliding_window() -> None:
    from synaptane.core.throttle import RateLimit

    now = [0.0]
    limit = RateLimit(2, 60, clock=lambda: now[0])
    assert limit.allow("a") and limit.allow("a") and not limit.allow("a")
    assert limit.allow("b")
    now[0] = 61
    assert limit.allow("a")
