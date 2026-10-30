English | [Français](CONTRIBUTING.fr.md)

# Contributing to Synaptane

Thank you for your interest in Synaptane. Bug reports, documentation fixes, translations and
code are all welcome.

Synaptane is provided as is and maintained on a best-effort basis by a single person. Reviews may take a while, and some proposals will be declined to keep the
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
| `catdoc` (`catdoc`, `xls2csv`) | tests of the old Office files `.doc` and `.xls` |
| `age` and `rclone` | off-site backup tests |
| `shellcheck` | lint of `scripts/*.sh` and `deploy/*/*.sh` (run in CI) |

The CI `check` job installs `ripgrep`, `tesseract` with French, `catdoc` and
`postgresql-client-17`, and runs `shellcheck` (already on the runner): the tests that need them
run there even if they are skipped on your machine. It does not install `age` and `rclone`, so
the off-site backup tests are skipped there too. Both tools are in the Docker image, and the
`image` job exercises an encrypted off-site copy during its end-to-end Compose run.

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
  `"{n} left by the tidy-up": ["{n} laissé par le rangement", "{n} laissés par le rangement"]`.
- A value that is itself an interface text, or a message computed by the services and shown
  later (stored in the database, sent by email): a `Label`, translated when shown.
- REST errors keep a stable `error` code; their `message` follows `Accept-Language` (English
  by default). Texts for AI models (instructions, tool descriptions, MCP answers), logs and the
  command line are in English and are not translated.

To add a language: its code in `LOCALES` (`web/i18n.py` and `services/accounts.py`) and in
the language menus (`login.html`, `account-profile.html`), a `locales/<code>.json` catalog
with the keys of `fr.json`, its plural rule in `i18n.plural`, and its email texts in
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
agreement (CLA). Instead, every commit must be signed off. By adding a `Signed-off-by` line to
a commit, you certify the Developer Certificate of Origin 1.1 below, reproduced verbatim from
<https://developercertificate.org/>:

```text
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.


Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```

To sign off, commit with `-s`:

```sh
git commit -s -m "Fix search ranking for exact title matches"
```

This adds a line such as `Signed-off-by: Jane Doe <jane@example.com>`, using the name and
email from your Git configuration. Use your real name (or the name you are publicly known
by). To keep your address private, you may use your GitHub noreply address,
`ID+login@users.noreply.github.com` (shown in your GitHub email settings). To sign off commits
you already made: `git commit --amend -s` for the last one, or `git rebase --signoff main` for
a branch, then force-push.

The [DCO workflow](.github/workflows/dco.yml) checks every commit of a pull request (merge
commits excepted) and names the ones without a `Signed-off-by` line.

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
- All commits are signed off ([DCO](#developer-certificate-of-origin-dco), see above).
- Pull requests are merged with **squash**: the pull request title becomes the title of the
  merged commit, so make it a clear, short sentence in English. The messages of your commits,
  with their `Signed-off-by` lines, become its body.

The pull request template contains this checklist. Thank you!

## For maintainers

### Merging pull requests

In the repository settings (Settings → General → Pull Requests), keep **Allow squash
merging** enabled, with the default commit message set to **Pull request title and commit
details**. The merged commit then keeps the `Signed-off-by` lines of the pull request's
commits.

The [DCO workflow](.github/workflows/dco.yml) checks every commit of each pull request, from
its base to its head. To block merging without it, make its check (`Signed-off-by on every
commit`) required in the rules of `main`.

### Publishing a release

1. In a pull request, set the new version everywhere:
   - `pyproject.toml`: `version`, in the PEP 440 form (`1.0.0rc1`, `1.0.0`). Then run
     `uv lock` so that `uv.lock` follows (`uv lock --check` fails otherwise).
   - `Dockerfile`: `ARG VERSION=`, same value.
   - `compose.yaml`: the default value of `${SYNAPTANE_VERSION:-X.Y.Z}`, for both images, in
     the semver form of the tag (`1.0.0-rc.1`, `1.0.0`).
   - `.env.example`: `SYNAPTANE_VERSION=`, same semver form.
   - The version examples (`--branch vX.Y.Z`, `SYNAPTANE_VERSION=`) in `README.md`,
     `README.fr.md`, `docs/admin/installation.md` and `docs/admin/installation.fr.md`.
   - `CHANGELOG.md`: the `## [X.Y.Z] - YYYY-MM-DD` section, dated, and its link at the end of
     the file. This section becomes the release notes.

   `tests/unit/test_image_metadata.py` checks that the `Dockerfile`, `compose.yaml`,
   `.env.example` and the `git clone --branch` examples follow `pyproject.toml`.
2. Once it is merged and CI is green, tag the merged commit of `main` and push the tag:

   ```sh
   git switch main && git pull
   git tag -a v1.0.0 -m "v1.0.0"   # or v1.0.0-rc.2 for a pre-release
   git push origin v1.0.0
   ```

3. The tag starts [`release.yml`](.github/workflows/release.yml), which:
   - checks that the tag matches `pyproject.toml` (in the PEP 440 sense: `v1.0.0-rc.1` is
     `1.0.0rc1`) and that the commit is on `main`, and publishes nothing otherwise;
   - runs the full CI again on this commit;
   - publishes the images `ghcr.io/synaptane/synaptane` and
     `ghcr.io/synaptane/synaptane-nginx`, tagged `X.Y.Z` and `X.Y`, and `latest` only for a
     final release (a pre-release only gets its own tag), with SBOM, provenance and signed
     attestations;
   - creates the GitHub Release (marked as a pre-release for `-rc.N`), with the notes taken
     from `CHANGELOG.md`;
   - if the `SITE_DISPATCH_TOKEN` secret exists, starts the sync of the website's
     documentation (`repository_dispatch` event of type `sync-docs` sent to
     `synaptane/website`). Without the secret, this step is skipped;
   - publishes the Python package (sdist and wheel) on [PyPI](https://pypi.org/project/synaptane/)
     by Trusted Publishing, from the `pypi` environment: no token is stored. The README links
     are made absolute (GitHub, at the tag) for the PyPI page. A tag released before this
     step existed is published by hand: Actions → Release → Run workflow, with the tag.
4. Check the run in the Actions tab, then the release page. If the version or branch check,
   or the CI, fails, nothing is published: fix the cause, delete the tag
   (`git tag -d vX.Y.Z` and `git push --delete origin vX.Y.Z`), then tag again. Release
   tags are protected by a ruleset (no move, no deletion): an administrator disables it in
   Settings → Rules → Rulesets for the deletion, then enables it again.
