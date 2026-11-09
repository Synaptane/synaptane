English | [Français](first-steps.fr.md)

# First steps

On first start, the super admin's home page lists these steps.

1. **Administration**: the **Organizations** tab to create them (box checked by default: you
   become their admin, otherwise your assistants would see nothing), the **Accounts** tab to
   create accounts (each account has its own page: rights, password).
2. On the organization page, one page per topic:
   - **Spaces**, then **New space**: name, identifier, Git repository address;
   - **Members**: add people, choose admins;
   - **Groups**: give a team access to several spaces at once;
   - **Artificial intelligence**: the provider of the nightly tidy-up;
   - **Semantic search** and **Webhooks**, optional;
   - **Settings**: name, description, default role in the spaces, deletion.
3. On a space, the **Settings** tab: **Git repository** (address, access, webhook, status
   and conflicts), **Members** (reader or contributor, groups, restricted space), **Tidy-up** (time,
   time zone, model),
   **Semantic search** and **General**.

Deleting a space or an organization requires typing its identifier again.

To link a space to its repository, see [Git](git.md). For sign-in with GitLab, GitHub, Google
or OIDC, see [SSO](sso.md); to notify other tools, see [Webhooks](webhooks.md). All of this is
also possible through the [REST API](../reference/api.md).

## Demo data

To try Synaptane before linking your own repositories, `synaptane demo` loads the wiki of a
fictional IT team ([examples/acme-wiki](../../examples/acme-wiki/README.md)): 50 pages, a space
purpose, six inbox notes covering typical tidy-up cases, verified pages, a page to review and a
broken link. Eight files that are not Markdown are linked from the pages, to see what
Synaptane does with them: a PDF contract, a scanned delivery note (OCR), a Word checklist, a
slide deck, two spreadsheets (one of them in the old `.xls` format), a rack diagram (OCR) and an
HTML status report. Search for "Kestrel", a word that only appears in the scan (Tesseract
required, included in the Docker image). On an initialized and migrated instance, it creates
what is missing, then syncs and indexes the space:
- a `demo` super admin, whose password is printed once (or given by
  `SYNAPTANE_DEMO_PASSWORD`, or `--password`);
- the `acme` organization, with `demo` as admin;
- the `acme/it` space, backed by a local Git repository, `<data_dir>/demo/acme-it.git`.

Running it again changes nothing: an existing account keeps its password, and what you changed
in the space stays.

The repository is local (`file://`): Synaptane refuses it unless
`SYNAPTANE_ALLOW_LOCAL_REPOS=true` is set for the command, the server and the worker. **Demo
and development only**: this setting lets an organization admin link a space to any Git folder
of the server. Run the demo on a throwaway instance, not on your production one.

With Docker Compose, `examples/compose.demo.yaml` sets it for `app` and `worker`:

```sh
echo 'COMPOSE_FILE=compose.yaml:examples/compose.demo.yaml' >> .env
docker compose up -d
docker compose exec app synaptane demo
```

With a self-built image: `COMPOSE_FILE=compose.yaml:compose.build.yaml:examples/compose.demo.yaml`.

From source:

```sh
export SYNAPTANE_ALLOW_LOCAL_REPOS=true   # in the shells of serve and worker too
uv run synaptane demo
```

The command prints what it created (`super admin demo`, `organization acme`…), the address to
open, the `demo` password and how to connect an AI assistant. It also says what keeps the demo
repository in sync: in a container, the Compose `worker` service, to leave running; from source,
a `synaptane worker` to keep running, with `SYNAPTANE_ALLOW_LOCAL_REPOS=true`. Tidying the inbox
needs an AI provider (organization page, **Artificial intelligence**). `docker compose down -v`
deletes the whole demo instance.

**Next step:** [connect your AI assistant (MCP)](../guide/mcp.md), then link the spaces
to their repositories ([Git repositories](git.md)).
