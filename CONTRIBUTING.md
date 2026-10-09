English | [Français](CONTRIBUTING.fr.md)

# Contributing to Synaptane

Thank you for your interest in Synaptane. Bug reports, documentation fixes, translations and
code are all welcome.

Synaptane is in **alpha / preview**. It is provided as is and maintained on a best-effort basis
by a single person. Reviews may take a while, and some proposals will be declined to keep the
project small and coherent. Please do not take it personally.

Everyone taking part in the project follows the [Code of Conduct](CODE_OF_CONDUCT.md).
Security vulnerabilities are never reported in public: see [SECURITY.md](SECURITY.md).

## Before you start

- **Open an issue before any non-trivial change** (a new feature, a behavior change, a
  refactoring, a new dependency). Agreeing on the approach first saves everyone time.
  Typo fixes and small, obvious bug fixes can go straight to a pull request.
- Look for an existing issue or discussion first.
- **Language**: issues and pull requests may be written in English or French. For new
  contributions, code, comments and commit messages are in English. Some existing code and
  comments are still in French; there is no need to translate them in an unrelated change.

## Development setup

### Requirements

- [uv](https://docs.astral.sh/uv/), which installs Python 3.12 if needed.
- Docker with Docker Compose, for PostgreSQL with pgvector.
- [ripgrep](https://github.com/BurntSushi/ripgrep) (`rg`): **required**, search depends on it.

Optional tools. Without them, the tests that need them are **skipped**, which is expected:

| Tool | Used by |
|---|---|
| `pg_dump` and `pg_restore`, version 17 (`postgresql-client-17`) | backup and restore tests |
| `tesseract` with the French language data (`tesseract-ocr-fra`) | OCR test |
| `age` and `rclone` | off-site backup tests |
| `shellcheck` | lint of `scripts/*.sh` and `deploy/*/*.sh` (run in CI) |

CI installs all of them, so a test skipped on your machine still runs there.

### Install

```sh
uv sync                                    # virtual environment and dependencies
docker compose -f compose.dev.yaml up -d   # PostgreSQL on 127.0.0.1:5433
uv run synaptane --help
```

More details in the [development guide](docs/dev/development.md).

## Checking your changes

```sh
./scripts/check.sh              # full check, same as CI
./scripts/check.sh tests/unit   # arguments are passed to pytest (no coverage threshold)
```

`./scripts/check.sh` runs the same checks as CI, in this order:

1. `uv lock --check`: the lockfile is up to date;
2. `ruff check`: lint;
3. `ruff format --check`: formatting (run `uv run ruff format .` to fix);
4. `mypy` in strict mode, on `src/` and `tests/`;
5. `pytest` in parallel, with line and branch coverage. The full suite fails below **87 %**.

Tests:

- `tests/unit`: no external dependency.
- `tests/integration`: a real PostgreSQL, by default the one from `compose.dev.yaml`. Each
  session creates a throwaway database and drops it afterwards. To use another instance, set
  `SYNAPTANE_TEST_DATABASE_URL`.
- `tests/eval`: see [Evaluation bench](#evaluation-bench) below.

CI also builds the Docker image and runs the full Compose stack end to end
(`scripts/e2e-compose.sh`). You do not need to run it locally, but you can; see the
development guide.

## Code organization

Everything lives in `src/synaptane/`:

| Package | Role |
|---|---|
| `core/` | Pure rules: permissions (`access.can` is the single authority), identifiers, security, errors. |
| `services/` | Use cases: permission checks, database writes, audit log. |
| `storage/` | PostgreSQL: models, connection, Alembic migrations (`storage/migrations`). |
| `git/` | Space repositories: clone, commit, push, fetch, conflicts. |
| `web/`, `api/`, `mcp/` | Web, REST and MCP interfaces. |
| `worker/` | Background jobs: sync, indexing, nightly tidy-up. |
| `ai/` | AI providers behind a single tool-calling conversation interface. |
| `server.py` | FastAPI assembly: routes, static files, errors, security headers. |

Rules:

- **Services are the only entry point.** Interfaces (`web/`, `api/`, `mcp/`) call services;
  they never touch storage or Git directly.
- **Interfaces contain no business logic.** Permission checks and rules belong in `core/` and
  `services/`.
- **REST and MCP stay in parity.** An action available through one should be available
  through the other, with the same rules, unless there is a written reason not to.

## Database migrations

Migrations use Alembic. To generate one (with the development PostgreSQL running):

```sh
ls src/synaptane/storage/migrations/versions | tail -1   # find the last number
uv run python -c "from alembic import command; from synaptane.storage.db import alembic_config; \
  command.revision(alembic_config('postgresql+psycopg://synaptane:synaptane@127.0.0.1:5433/synaptane'), \
  message='…', autogenerate=True, rev_id='NNNN')"
```

- `rev_id` is **sequential**: the number that follows the last migration.
- **Always review** the generated file; autogenerate is a starting point, not a result.
- Name constraints with `op.f(…)`, otherwise the naming convention is applied twice.
- `tests/integration/test_migrations.py` downgrades every migration, upgrades again, and
  checks that the schema matches the models.

## User interface text

Interface text is written **in English**: wrapped in `_("…")` in templates, and passed as is
to the functions that translate it in the code (`page.translate`, `redirect(…, "…")`, business
errors such as `invalid("…")`, `Label("…")`). The French translation goes in the catalog
`src/synaptane/web/locales/fr.json`, as `"English text": "texte français"`. Tests check that
every text of the templates and of these calls is in `fr.json`, with the same placeholders, so
**every new or changed string needs its entry in `fr.json`**. If you are not comfortable
writing French, say so in the pull request and the maintainer will add it.

- Values go through placeholders, never concatenation: `_("Page moved: {path}", path=path)`.
- Same English text, different French (gender, noun or verb): a context,
  `_("Enabled", context="routine")`, translated by the entry `"Enabled|routine": "Activée"`.
- English text that does not change with the count while the French does: a pair,
  `"{n} left by tidying": ["{n} laissé par le rangement", "{n} laissés par le rangement"]`.
- A value that is itself an interface text, or a message computed by the services and shown
  later (stored in the database, sent by e-mail): a `Label`, translated when shown.
- REST errors keep a stable `error` code; their `message` follows `Accept-Language` (English
  by default). Texts for AI models (instructions, tool descriptions, MCP answers), logs and the
  command line are in English and are not translated.

To add a language: its code in `LOCALES` (`web/i18n.py` and `services/accounts.py`) and in
the language menus (`login.html`, `account-profile.html`), a `locales/<code>.json` catalog
with the keys of `fr.json`, its plural rule in `i18n.plural`, and its e-mail texts in
`services/mail.py`. Vocation and routine templates may follow (English otherwise).

## Evaluation bench

`tests/eval` measures the quality of the AI tidy-up and of search ranking. It is not part of
the normal test suite. Run it by hand before changing the agent instructions or tools, the
default model, or the search ranking:

```sh
./scripts/eval.sh          # simulated model, then search: free, about 15 seconds
```

Running it against a real model **costs money** (API calls, billed to your key) and requires
an explicit budget (`--budget`). CI only runs the free self-check. See
[docs/dev/eval.md](docs/dev/eval.md).

## Developer Certificate of Origin (DCO)

Synaptane is licensed under the [Apache License 2.0](LICENSE). There is no contributor license
agreement (CLA). Instead, every commit must be signed off under the
[Developer Certificate of Origin 1.1](https://developercertificate.org/).

By adding a `Signed-off-by` line, you certify that:

- (a) you wrote the contribution, in whole or in part, and have the right to submit it under
  the project's open source license; or
- (b) it is based on previous work under a compatible open source license, and you have the
  right to submit it with your changes under the same license; or
- (c) it was given to you by someone who certified (a), (b) or (c), and you did not change it;
- (d) you understand that the contribution and your sign-off (including your name and email)
  are public and kept indefinitely.

The full text is at <https://developercertificate.org/>. To sign off, commit with `-s`:

```sh
git commit -s -m "Fix search ranking for exact title matches"
```

This adds a line such as `Signed-off-by: Jane Doe <jane@example.com>`, using the name and
email from your Git configuration. Use your real name (or the name you are publicly known
by). To sign off commits you already made: `git commit --amend -s` for the last one, or
`git rebase --signoff main` for a branch.

## AI-assisted contributions

Contributions written with the help of an AI assistant are accepted, on these conditions:

- you have **reviewed** every line and **understand** what it does;
- you **take responsibility** for it, as if you had written it yourself (including the DCO
  sign-off: make sure you have the right to submit it);
- you **say so in the pull request** (the template has a checkbox), with a word on how the
  assistant was used.

Pull requests that look generated and unreviewed will be closed.

## Pull requests

- **One pull request, one topic.** Split unrelated changes.
- `./scripts/check.sh` passes.
- Tests are added or updated for any change in behavior.
- Documentation is updated in **both English and French**: each page exists as `page.md`
  (English) and `page.fr.md` (French), in the same folder. If you cannot write one of the two
  languages, say so and the maintainer will help.
- Add an entry to [CHANGELOG.md](CHANGELOG.md), in the unreleased section, for any change a
  user or an administrator would notice.
- No secrets, tokens or real personal data, in code, tests, fixtures or screenshots.
- All commits are signed off (DCO, see above).
- Pull requests are merged with **squash**: the pull request title becomes the commit
  message, so make it a clear, short sentence in English.

The pull request template contains this checklist. Thank you!
