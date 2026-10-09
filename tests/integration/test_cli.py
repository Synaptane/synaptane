from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from synaptane import version_label
from synaptane.cli import main
from tests.integration.conftest import ADMIN_URL


def test_init_upgrade_admin_and_doctor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    name = f"synaptane_cli_{tmp_path.name[-8:].replace('-', '_')}"
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(ADMIN_URL).set(database=name).render_as_string(hide_password=False)
    config = tmp_path / "synaptane.toml"
    # The CLI contract has its own tests (simulated outputs): here, neither claude nor codex.
    monkeypatch.setattr("synaptane.ai.cli_contract.check_all", lambda: [])
    try:
        assert main(["--config", str(config), "init", "--database-url", url]) == 0
        assert (config.stat().st_mode & 0o777) == 0o600
        assert (tmp_path / "data" / "master.key").stat().st_size == 32
        assert main(["--config", str(config), "doctor"]) == 1  # migrations not applied
        # Neither server nor worker on an outdated database: their pages would return 500s.
        assert main(["--config", str(config), "serve"]) == 1
        assert main(["--config", str(config), "worker"]) == 1
        assert "synaptane db upgrade" in capsys.readouterr().err
        assert main(["--config", str(config), "db", "upgrade"]) == 0
        monkeypatch.setenv("SYNAPTANE_ADMIN_PASSWORD", "mot-de-passe-admin")
        assert main(["--config", str(config), "admin", "create", "--username", "root"]) == 0
        assert main(["--config", str(config), "admin", "create", "--username", "root"]) == 1
        capsys.readouterr()
        assert main(["--config", str(config), "doctor"]) == 0
        out = capsys.readouterr().out
        assert "FAILED" not in out and f"Synaptane {version_label()}" in out
        # Broken contract, old login in the data volume: reported, without failing.
        from synaptane.ai.cli_contract import Report

        broken = Report("claude", "Claude Code (claude)", version="2.1.300")
        broken.problems.append("outils intégrés proposés au modèle : Bash")
        monkeypatch.setattr(
            "synaptane.ai.cli_contract.check_all",
            lambda: [broken, Report("codex", "Codex (codex)", installed=False)],
        )
        monkeypatch.setenv("SYNAPTANE_AGENT_HOME", str(tmp_path / "agents"))
        (tmp_path / "data" / "home" / ".codex").mkdir(parents=True)
        assert main(["--config", str(config), "doctor"]) == 0
        out = capsys.readouterr().out
        assert "[warning] Claude subscription (claude): agent isolation (2.1.300)" in out
        assert "Bash" in out and "[warning] ChatGPT subscription (codex)" in out
        assert "[warning] Subscription sign-ins outside the data volume" in out
        assert "rm -rf" in out and "home/.codex" in out
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()
