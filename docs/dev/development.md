English | [Français](development.fr.md)

# Working on Synaptane

## Requirements

- [uv](https://docs.astral.sh/uv/), which installs Python 3.12 if needed.
- Docker, for PostgreSQL and pgvector in development.
- Git 2.28 or later.

## Get started

```sh
uv sync                                    # environment and dependencies
docker compose -f compose.dev.yaml up -d   # PostgreSQL on 127.0.0.1:5433
uv run synaptane --help
```

## Run the app locally

Against the PostgreSQL of `compose.dev.yaml` (user, password and database: `synaptane`), from
the root of the repository. `synaptane.toml`, `data/` and `local/` are ignored by Git.

```sh
uv run synaptane init --database-url postgresql+psycopg://synaptane:synaptane@127.0.0.1:5433/synaptane
echo 'allow_local_repos = true' >> synaptane.toml   # file:// repositories: development only
uv run synaptane db upgrade
uv run synaptane admin create --username admin --display-name "Admin"
uv run synaptane doctor
```

- `init` writes `./synaptane.toml` (mode 600) and creates the data folder `./data/`, with the
  master key `data/master.key` and the repository copies `data/repos/`. Defaults:
  `--data-dir ./data` (relative to the folder of the configuration file) and
  `--public-url http://127.0.0.1:8000`.
- For another file: `--config` before the command (`uv run synaptane --config
  local/dev.toml init …`), or `SYNAPTANE_CONFIG`, to give again with every command.
- Each key of the file can come from a `SYNAPTANE_<KEY>` variable, which takes precedence: for
  example `SYNAPTANE_DATABASE_URL`, or `SYNAPTANE_ALLOW_LOCAL_REPOS=true` instead of the
  `allow_local_repos` line.
- `admin create` asks for the password (12 characters at least), or reads it from
  `SYNAPTANE_ADMIN_PASSWORD`.
- Locally, `doctor` shows warnings for the public URL in HTTP and for missing optional tools
  (Tesseract, catdoc, PostgreSQL client, age, rclone): harmless for development.

Then, in two terminals:

```sh
uv run synaptane serve     # web, REST and MCP on http://127.0.0.1:8000
uv run synaptane worker    # background jobs: Git sync, indexing, tidy-up
```

`serve` and `worker` refuse to start on a database that is behind: run `db upgrade` again after
an update that brings a migration. For another port, `serve --port 8001`, and
`public_url = "http://127.0.0.1:8001"` in `synaptane.toml`.

### Link a space to a local repository

No Git host needed: a bare repository on disk is enough.

```sh
git init --bare -b main local/notes.git
echo "file://$PWD/local/notes.git"   # the address to give the space
```

1. Sign in at http://127.0.0.1:8000 with the `admin` account.
2. **Administration** → **New organization**, for example `acme` → **Create organization**.
3. In the organization: **New space**, **Git repository address**: the `file://…` address
   above, branch `main` → **Create space**.

A `file://` repository needs no access to set up. The worker prepares the empty repository
(first commit with `content/README.md` and `.synaptane/inbox/`): the space is ready. A
`git clone local/notes.git` then lets you push pages into it as a person would.

`allow_local_repos` is for development and tests only: on a server, it would let an
organization admin make the worker read any Git repository on the machine. Without it, syncing
a `file://` repository fails.

## Check

```sh
./scripts/check.sh            # lockfile, ruff, format, strict mypy, pytest
./scripts/check.sh tests/unit # arguments are passed to pytest
```

- `tests/unit`: no dependencies.
- `tests/integration`: a real PostgreSQL, the one from `compose.dev.yaml` by default. Each
  session creates a throwaway database, then drops it. To target another instance:
  `SYNAPTANE_TEST_DATABASE_URL`.
- `tests/eval`: tidy-up and search evaluation bench, outside the normal suite, run by hand
  before changing an instruction, a model or the ranking (`./scripts/eval.sh`, see
  [eval.md](eval.md)). Its self-check with the simulated model runs in CI.

CI (`.github/workflows/ci.yml`) runs the same script, then builds the image and runs
`scripts/e2e-compose.sh`: the full Compose stack, from installation to restore after a
`down -v`. Locally, without the CLIs to go faster:

```sh
docker build --build-arg AGENT_CLIS=false -t synaptane:e2e .
SYNAPTANE_IMAGE=synaptane:e2e ./scripts/e2e-compose.sh
```

## New migration

```sh
uv run python -c "from alembic import command; from synaptane.storage.db import alembic_config; \
  command.revision(alembic_config('postgresql+psycopg://synaptane:synaptane@127.0.0.1:5433/synaptane'), \
  message='…', autogenerate=True, rev_id='NNNN')"
```

The `synaptane` database must be up to date (`uv run synaptane db upgrade`, see
[Run the app locally](#run-the-app-locally)): Alembic compares the models with its schema.

`rev_id`: the number after the last migration in `storage/migrations/versions/`
(`ls src/synaptane/storage/migrations/versions | tail -1`).
Always review the generated file, and name constraints with `op.f(…)`: otherwise the naming
convention would be applied twice. `tests/integration/test_migrations.py` downgrades all
migrations, upgrades again, then checks that the schema matches the models.

## Translations

Interface texts are written in English, in the templates (`_("…")`) and in the code
(business errors, `redirect` messages, `Label`). `web/locales/fr.json` translates them into
French (`"English text": "texte français"`); English needs no catalog, the text is its own
translation. `web/i18n.py` also handles contexts (`_("Enabled", context="routine")`, entry
`"Enabled|routine"`), French plurals of invariant English texts (a pair of forms) and French
typography (no-break spaces before `:` `;` `?` `!`). Tests check that every text of the
templates and of the translating calls is in `fr.json`, with the same placeholders.

Texts computed by the services and shown later are `Label`s (template and values): stored
(`Label.dumps`, connection test result) or sent by email (`mail.compose` translates them for
each recipient), they are translated when shown. Texts stored before this switch, in French,
are still recognized (Git errors, tidy-up log, commit subjects). REST errors keep their `error`
code and translate their `message` according to `Accept-Language` (English by default); MCP
answers stay in English. See [CONTRIBUTING.md](../../CONTRIBUTING.md#user-interface-text) to
add a text or a language.

## Code layout

`src/synaptane/`:

| Package | Role |
|---|---|
| `core/` | Pure rules: permissions (`access.can`, the only authority), identifiers, security, errors. |
| `services/` | Use cases: permission checks, database writes, audit. The only entry point for the interfaces. |
| `storage/` | PostgreSQL: models, connection, Alembic migrations (`storage/migrations`). |
| `git/` | Space repositories: clone, commits, push, fetch, conflicts. |
| `ai/` | AI providers, behind a single tool-calling conversation: Anthropic API, OpenAI-compatible APIs (OpenAI, Mistral, Ollama…), subscription CLIs (Claude Code, Codex) and the contract that checks their isolation; embeddings, public prices and estimated cost, subscription quota. |
| `web/`, `api/`, `mcp/` | Web, REST and MCP interfaces. They contain no business rules. |
| `server.py` | FastAPI assembly: routes, static files, errors, security headers. |
| `worker/` | Background jobs: sync, indexing, nightly tidy-up. |
| `cli.py` | The `synaptane` command line: `init`, `db upgrade`, `admin create`, `serve`, `worker`, `doctor`, `backup`, `restore`… |
| `config.py` | Settings: TOML file (`synaptane.toml`), overridden by `SYNAPTANE_*` variables. |
| `extract.py` | Text of PDFs, Office documents and images (OCR with Tesseract, old `.doc` and `.xls` with catdoc, old `.ppt` with `extract_ppt.py`), read in a separate process with bounded resources. |

In `web/`, `pages.py` holds the foundations (request context, templates, redirects, errors);
each group of pages has its own module and router (`login_pages`, `org_pages`, `space_pages`,
`admin_pages`, `account_pages`, `wiki`, `inbox_pages`…). The tidy-up is split between
`services/tidy.py` (run, scheduling) and `services/tidy_tools.py` (working view, the agent's
tools and instructions).

## System tools for tests

- `rg` (ripgrep) is required.
- `pg_dump` and `pg_restore` version 17 (package `postgresql-client-17`): without them, the
  tests in `tests/integration/test_backup.py` that make backups are **skipped**.
- `age` (with `age-keygen`) and `rclone`: without them, the off-site copy tests that encrypt or
  copy (`tests/unit/test_offsite.py`, `tests/integration/test_offsite.py`) are **skipped**. CI
  does not install them: `scripts/e2e-compose.sh` exercises the off-site copy in the image,
  which contains them.
- `tesseract`, with `fra`, is needed for the OCR test (`tests/fixtures/scan-fr.pdf`), which is
  skipped if it is missing. CI installs it. The PDF, a page of French text with no text layer,
  is made by `tests/fixtures/scan_fr.py` (`uv run python tests/fixtures/scan_fr.py` regenerates
  it).
- `catdoc` (package `catdoc`: `catdoc`, `xls2csv`) reads the old `.doc` and `.xls` files of
  `tests/fixtures/legacy/` (made by `scripts/legacy_files.py`) and of the demo; the tests that
  need it are skipped if it is missing. CI installs it. The `.ppt` files are read in Python
  (`extract_ppt.py`, with olefile): their tests always run.
- Without a system package, you can borrow Tesseract (or catdoc) from a Docker image that
  contains it, with a small script named after the tool (`tesseract`; for catdoc, the same
  script under the two names `catdoc` and `xls2csv`) placed first in the `PATH`:

  ```sh
  exec docker run --rm -i --network none -u "$(id -u):$(id -g)" -v /tmp:/tmp \
    --entrypoint "$(basename "$0")" <image> "$@"
  ```
