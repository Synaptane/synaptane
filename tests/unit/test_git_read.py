"""Batch reads (`read_many`) and streamed reads (`object_stream`): exotic paths, missing
objects, same hardened options as the other commands."""

from pathlib import Path

import pytest

from synaptane.git.repo import GitError, Identity, Repository

ME = Identity("Test", "test@example.com")


@pytest.fixture
def repo(tmp_path: Path) -> Repository:
    path = tmp_path / "copie"
    path.mkdir()
    repository = Repository(path, "", "main", ME)
    repository._git("init", "--quiet", "-b", "main")
    repository.commit_changes(
        {
            "x blob 12": b"chemin trompeur\n",
            "dossier/page.md": b"# Page\n",
            "a b.md": b"avec espace\n",
            "vide.md": b"",
            "binaire.bin": b"\0\n\nfin\n" * 3,
        },
        ME,
        "départ",
    )
    return repository


def test_read_many_survives_tricky_and_missing_paths(repo: Repository) -> None:
    wanted = [
        "x blob",  # missing: "HEAD:x blob missing", which looks like a header
        "x blob 12",
        "dossier",  # a folder: its content (a tree) must be skipped, not read
        "dossier/page.md",
        "absent.md",
        "a b.md",
        "vide.md",
        "binaire.bin",
    ]
    assert repo.read_many(wanted) == {
        "x blob 12": b"chemin trompeur\n",
        "dossier/page.md": b"# Page\n",
        "a b.md": b"avec espace\n",
        "vide.md": b"",
        "binaire.bin": b"\0\n\nfin\n" * 3,
    }


def test_read_many_of_an_unknown_revision_is_empty(repo: Repository) -> None:
    assert repo.read_many(["dossier/page.md"], "0" * 40) == {}


def test_hardened_command_line_for_batch_and_stream(
    repo: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, ...]] = []
    original = Repository._command

    def spy(*args: str, network: bool = False) -> list[str]:
        seen.append(args)
        return original(*args, network=network)

    monkeypatch.setattr(Repository, "_command", staticmethod(spy))
    repo.read_many(["vide.md"])
    entry = repo.blob_entry("dossier/page.md")
    assert entry is not None
    assert b"".join(repo.object_stream(entry[0])) == b"# Page\n"
    assert any(a[0] == "cat-file" and a[1].startswith("--batch") for a in seen)
    assert ("cat-file", "blob", entry[0]) in seen


def test_a_failed_stream_is_an_error_not_an_empty_file(repo: Repository) -> None:
    with pytest.raises(GitError):
        b"".join(repo.object_stream("0" * 40))


def test_a_stream_closed_early_is_not_an_error(repo: Repository) -> None:
    entry = repo.blob_entry("binaire.bin")
    assert entry is not None
    chunks = repo.object_stream(entry[0], chunk=4)
    assert next(chunks) == b"\0\n\nf"
    chunks.close()  # client gone: git is stopped without error
