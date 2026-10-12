English | [Français](cli.fr.md)

# Command line

`synaptane [--config FILE] <command>` (default configuration: `$SYNAPTANE_CONFIG`, otherwise
`./synaptane.toml`). With Docker: `docker compose exec app synaptane …`.
`synaptane --version` prints the version.

| Command | What it does |
|---|---|
| `init [--database-url …] [--data-dir …] [--public-url …]` | Writes `synaptane.toml` if it does not exist, and creates the folders and the master key. Without `--database-url`, uses the `SYNAPTANE_*` variables. Never replaces an existing key. |
| `db upgrade` | Applies the database migrations. |
| `admin create --username … [--display-name …] [--email …]` | Creates a super admin. The password is prompted for, or read from `SYNAPTANE_ADMIN_PASSWORD`. |
| `demo [--password …] [--source FOLDER]` | Loads the demo data: the wiki of a fictional IT team (`examples/acme-wiki/` by default, or `--source`), in a local Git repository under `data_dir/demo/`. Creates what is missing (the `demo` super admin, the `acme` organization, the `acme/it` space), then syncs and indexes the space; run again, it recreates and overwrites nothing. The `demo` password is set only when the account is created: `--password`, otherwise `SYNAPTANE_DEMO_PASSWORD`, otherwise generated and shown once. Requires `allow_local_repos = true` (`SYNAPTANE_ALLOW_LOCAL_REPOS=true`) for this command, `serve` and `worker`: keep it to a disposable demo instance, since it lets an organization admin point a space at any Git folder on the server. See [Demo data](../admin/installation.md#demo-data). |
| `serve [--host 127.0.0.1] [--port 8000] [--forwarded-allow-ips 127.0.0.1]` | Web, REST API and MCP. |
| `worker` | Background jobs: Git sync, indexing, tidy-up, webhooks. |
| `reindex [--space org/space]` | Clears the extracted text and the semantic index, then asks the worker to rebuild everything. |
| `doctor` | Checks the installation: key, tools, database, migrations, HTTPS, off-site copy (as a warning only). Exit code 1 on failure. |
| `check-clis [--installed-only]` | Checks, without signing in or calling the model, that the subscription CLIs (`claude`, `codex`) give the agent only Synaptane's tools. Both are required, as in an image built with them (`AGENT_CLIS=true`, the Dockerfile's default; the published image has neither); with `--installed-only`, a missing CLI is not an error. Exit code 1 if the contract is broken or could not be checked. See [Use a Claude or ChatGPT subscription](../admin/ai.md#use-a-claude-or-chatgpt-subscription). |
| `backup [--output FILE\|FOLDER\|-] [--without-repos] [--without-key] [--offsite]` | Backup, read back before it is reported. Without `--output`: in `$SYNAPTANE_BACKUP_DIR` (`/backups` with Docker), otherwise the current folder; with `--offsite` alone, no local copy. `--offsite` encrypts the archive (age) and copies it to each rclone destination. Exit codes: 0 all good, 1 no usable archive or copy refused, 3 archive good but a destination failed. See [Backup and restore](../admin/backup.md). |
| `offsite test` | Writes, reads back and deletes a test file on each off-site destination; reports a missing age key. |
| `offsite list` | Encrypted archives present on each destination. |
| `restore ARCHIVE\|- [--force]` | Restores a backup (a `.tar.gz` file, or `-` for standard input), with the app and the worker stopped. `--force` replaces an existing instance or its key. See [Backup and restore](../admin/backup.md). |
