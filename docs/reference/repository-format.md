English | [Français](repository-format.fr.md)

# Repository format

Each space is one branch of a Git repository. This page describes what Synaptane reads and
writes in that repository. To link a space to its repository, see
[Git repositories](../admin/git.md); for the ideas behind it, see [Concepts](../concepts.md).

## Layout

```
content/                          visible: wiki, search, MCP
  README.md                       home page of the space (created if content/ is missing)
  …free tree…                     Markdown pages, PDF, Office documents, images, any file
.synaptane/                       reserved for Synaptane
  vocation.md                     the space's purpose (optional)
  prompts/tidy.md                 tidy-up rules (optional)
  prompts/<name>.md               the team's prompts for assistants (optional)
  checks.md                       team checks: rules every page must follow (optional)
  routines/<name>.md              the space's agent routines (optional)
  memory/<name>.md                what a routine remembers from one run to the next (optional)
  inbox/
    2026-10-03T14-12-05Z-note-wifi-password-policy-3f2a.md     one note
    2026-10-03T14-12-05Z-note-wifi-password-policy-3f2a/       its attachments, if any
README.md, .github/, …            anything else: ignored by Synaptane
```

- Everything outside `content/` and `.synaptane/` is ignored: the repository can hold its own
  README, CI files or other folders.
- On the first sync, Synaptane creates what is missing, and only that: `content/README.md`
  (with the space's name and description) if `content/` does not exist, and
  `.synaptane/inbox/`.
- Space settings (description, tidy-up time and time zone, model, embeddings) are kept in the
  database and changed in the web UI or the REST API, not in the repository. Routines are
  files of the repository; only the state of their runs is kept in the database.
- Git is the source of truth: every file here can also be changed by a push, and Synaptane
  picks it up on the next sync.

## `content/`

All file types are accepted. What Synaptane does with a file depends on its type:

| Type | Display | Search |
|---|---|---|
| Markdown (`.md`, `.markdown`) | Rendered as a wiki page | Full text |
| Text (`.txt`, `.csv`, `.json`, `.yaml`, code…) | Shown as text | Full text |
| PDF | Viewer | Native text, OCR for scanned pages |
| Office (`.docx`, `.xlsx`, `.pptx`, `.odt`, `.ods`, `.odp`, `.doc`, `.xls`, `.ppt`) | Download, with a text preview | Extracted text (old `.doc` and `.xls`: with catdoc) |
| HTML (`.html`, `.htm`) | Rendered in an isolated frame (sandbox), source one click away | Full text (source) |
| Images (`.png`, `.jpg`, `.gif`, `.webp`, `.bmp`, `.avif`, `.svg`) | Displayed | Text read by OCR, except SVG and AVIF (can be turned off with `ocr_images`) |
| Other binary files | Download | File name and path only |

Text extracted from PDF, Office documents and images is kept outside the repository, in the
server's cache, and rebuilt by `synaptane reindex`. Details:
[Files other than Markdown](../guide/wiki.md#files-other-than-markdown).

### Pages

- **Title**: the first level-1 heading (`# Title`); without one, the file name.
- **Folder home page**: `README.md` (or `index.md`) is shown when a folder is opened.
- **Links**: relative Markdown links (`[guide](../infra/proxmox.md)`) and wiki links
  (`[[name]]`, `[[name|label]]`, `[[name#section]]`, `[[folder/name]]`) are resolved in the
  wiki. Moving a page through Synaptane updates the links that pointed to it.
- **Obsidian syntax**: pages written in Obsidian display as they do there, and their files
  are not changed. `![[image.png]]` shows the image (with `|300` or `|300x200` for its size),
  `> [!warning] Title` is a callout (GitHub's `> [!WARNING]` too), `==text==` is highlighted,
  and `%%comments%%` and block ids (`^id`) are hidden when reading. A `%%comment%%` is not
  private: search, assistants and Git still see it. `![[page]]` is a link: the page is not
  included, and a callout cannot be folded.
- **Diagrams**: a ` ```mermaid ` code block is drawn as a diagram when reading. If it cannot be
  drawn, the code stays on the page. The drawing happens in your browser, in an isolated frame.
- **Front matter**: optional. Synaptane reads three verification fields; other fields are
  kept as they are (the properties of a Notion database row, for instance, written there by
  the [import](../guide/wiki.md#from-notion)).

```markdown
---
owner: ada            # account username, or a free-form name
verified: 2026-10-03  # date of the last verification
review_every: 6m      # 90d, 12w, 6m, 1y; absent: the space's period; never: never
reconfirmed: 1        # verified again with no change: period doubled (written by Synaptane)
---
# Failover procedure
```

A page is **verified** (a date, and the review date not yet reached), **due for review** (the
review date has passed) or **never verified**. Each verification with no change since the
previous one doubles the period (`reconfirmed`, up to two years). A commit that only changes these fields is not
reported to the tidy-up, and AI agents cannot change or remove them by rewriting a page: only
**Mark as verified** (web), `verify_page` (MCP) or the REST API do. See
[Verify a page](../guide/wiki.md#verify-a-page).

## `.synaptane/vocation.md`

The space's **purpose**, in Markdown: what the space is for and for whom, what goes in it and
what does not, how folders are organized, conventions (language, tone, file names, page
formats), what matters most, vocabulary. The tidy-up agent, routines and AI assistants read
it (MCP: `list_spaces` gives its first 600 characters, `read` with the path
`.synaptane/vocation.md` gives all of it). 20,000 characters at most.

It is edited in **Settings → Purpose** (organization administrators), through the REST API, or
by a push. Templates are provided: `infra`, `marketing`, `product`, `client`, `personal`,
`family`, `job-search`, `blank`. An empty text removes the file.

## `.synaptane/prompts/tidy.md`

The space's **tidy-up rules**: precise rules for the tidy-up agent only, such as "one page per
supplier in `suppliers/`" or "never rename procedures". They come after the default
instructions and the purpose, and take precedence over the purpose on how to file. 20,000
characters at most. Neither the purpose nor the rules lift the safeguards (deletion cap,
content treated as data).

The former name of this file, `.synaptane/prompts/rangement.md`, is still read when it is the
only one present; the first save from Synaptane writes `tidy.md` instead.

See [AI and tidy-up](../admin/ai.md).

## `.synaptane/checks.md`

The space's **team checks**: rules that every Markdown page of `content/` must follow, checked
without AI in **Health**, at each write and by the tidy-up (see
[Team checks](../guide/wiki.md#team-checks)). Written in **Settings → Purpose** (organization
administrators), through the REST API or by a push; 20,000 characters at most from Synaptane.

Each level-2 heading (`## Title`) is a rule; the `- key: value` list items right under it
describe it. Any other text (paragraphs, level-1 or level-3 headings, HTML comments, code
blocks) is a comment. A value between backticks is taken as is (```` `` a`b `` ```` for a
value that holds a backtick).

| Key | Meaning |
|---|---|
| `pattern` | Regular expression ([RE2 syntax](https://github.com/google/re2/wiki/Syntax), linear time), on the text of the page, front matter included; `^` and `$` match at each line. With `field`: on the value of the field |
| `field` | Front matter field (top-level key) |
| `expect` | What `field` must be, separated by commas: `present` (there and not empty), `list` (a YAML list: `[a, b]` or one `- item` per line), `date` (`YYYY-MM-DD`, without a time). `list` and `date` only apply when the field is there |
| `preset` | Personal data: `email`, `phone` (international, French and North American formats), `card` (13 to 19 digits that pass the Luhn check), `iban` (length of its country and check digits, upper or lower case). No-break spaces, dashes and zero-width characters between digits are seen through. Stands alone: no `pattern` or `field` |
| `allow` | Values never reported, separated by commas. For `email`: domains (`acme.com`, its subdomains included) or addresses. The `example.com`, `.org` and `.net` domains and the `.example`, `.invalid`, `.test` and `.localhost` ones are always allowed |
| `paths` | Folders or globs relative to `content/`, separated by commas: `clients/` (everything under it), `notes/*.md` (`*` within a folder), `**/meetings/*.md` (`**` across folders). Without it: every page |
| `except` | Folders or globs left out, same form |
| `level` | `warn` (default): reported. `block`: a write that breaks the rule is refused |
| `message` | What to do, shown with each violation and with a refusal |
| `mask` | `yes`: the excerpt of a `pattern` is masked, as for presets |
| `fix` | `yes` or `no`: whether the [nightly maintenance](../admin/ai.md#wiki-maintenance) fixes what people break of this rule. By default, `yes` for a `block` rule and for personal data (a preset, or `mask: yes`), `no` for the other `warn` rules, only reported in **Health** |

Limits: 50 rules, 500 characters per pattern, 3,000 RE2 steps per compiled pattern and 15,000
for the whole file (a counted repetition such as `.{0,1000}` costs that many: RE2 stays
linear, but its cost per byte grows with them), 64,000 characters of the file read. **Health**
examines the first 1 MB of each page and shows 20 violations per rule and per page; a write
is checked in full. A faulty rule (unknown key, invalid pattern, pattern too complex, pattern
that matches an empty text, two rules with the same title) is
ignored and its line is shown in **Health**; the other rules apply. A save from Synaptane
refuses a file with a faulty rule.

A `block` rule refuses the writes of the web UI, the REST API, MCP, the import, the tidy-up,
the report page and the pages a routine keeps, and the undo of a tidy-up or of an assistant's run, when they add
a violation: a page that already broke the rule stays editable, and a
page moved into a folder that a rule covers counts as new. A push cannot be refused.

## `.synaptane/prompts/<name>.md`

The team's **prompts for assistants**: every other Markdown file of the folder, one per prompt
(30 at most), named with lowercase letters, digits and dashes. Front matter `title`,
`description`, `arguments` (names separated by commas, `?` for an optional one); the body uses
`{{argument}}` and `{{include page.md}}`. Written in **Settings → Assistant prompts**
(organization administrators) or by a push. See [Your team's
prompts](../guide/mcp.md#your-teams-prompts).

## `.synaptane/routines/<name>.md`

The space's **agent routines**, one Markdown file each (20 at most), named with lowercase
letters, digits and dashes (40 characters at most): the name of the routine. A header between
two `---` lines, then the instructions:

```markdown
---
title: Weekly digest
schedule: weekly friday 16:00
scope: projects, decisions
report: reports/weekly-digest.md
email: members
---
Write the summary of the week…
```

Fields: `title`, `description`, `schedule` (`daily 02:30`, `weekly monday 06:00`,
`monthly 1 06:00`; French is read too, such as `hebdo lundi 06:00`, and Synaptane writes English
back), `scope` (folders separated by commas, a JSON list or one `- folder` line
each), `writes` (folders the routine keeps, same syntax, 5 at most: it writes its own pages
there; never the whole wiki nor a hidden folder), `report`, `inbox`, `email` (`admins`,
`members`, `none`), `webhook`, `enabled`, `template`, `memory` (`true`: the routine keeps a
memory, see below). Values are plain text or JSON. An unknown field is reported and ignored;
an invalid file does not run, and its error shows in Settings → Routines and the REST API.

Written in **Settings → Routines** (organization administrators: a commit in their name) or by
a push: anyone who can push can change them. Read at each synchronization; the state of the
runs (last run, run requested) stays in the database, so a changed file never makes a routine
run again. See [Agent routines](../admin/ai.md#agent-routines-per-space).

## `.synaptane/memory/<name>.md`

The **memory** of the routine `<name>`, when its file says `memory: true`: free Markdown,
4,000 characters at most, that the routine rewrites in full at the end of a run and reads at
the start of the next one (topics already reported, pages followed, what remains to watch).
It goes in the run's commit; a run that is interrupted leaves it as it was, and a memory
changed during a run (a push already synchronized, an administrator clearing it) keeps that
change. Anyone who can push can correct or delete it; the routine reads it as data, never as instructions. Removed
with its routine from Synaptane, and by the next synchronization when a push removes or
renames the routine without moving its memory. See
[Memory](../admin/ai.md#memory).

## `.synaptane/inbox/`

One **note** is one Markdown file with a front matter, named after its date (UTC), its type,
a short form of its title and a random suffix, so that names are unique and sort by date.
Attachments go in a folder with the same name, without `.md`.

```markdown
---
type: "note"
title: "Wi-Fi password policy"
source: "mcp"
deposited_at: "2026-10-03T14:12:05+00:00"
deposited_by: ["Ada Lovelace <ada@example.com>"]
via: "Claude"
origin: "Conversation about the guest network"
hint: "network/wifi.md"
attachments: ["policy.pdf"]
---

Guest Wi-Fi passwords change every quarter…
```

| Field | Meaning |
|---|---|
| `type` | `note` |
| `title` | Title of the note |
| `source` | Channel used: `web`, `api`, `mcp` or `email` (the [capture by email](../admin/email-capture.md)) |
| `deposited_at` | Date and time of the drop (UTC, ISO 8601) |
| `deposited_by` | Who dropped it: `Name <email>` (a list) |
| `via` | Client: name of the OAuth application or of the personal token (optional) |
| `origin` | Where the information comes from: URL, meeting, conversation (optional, 2,000 characters at most) |
| `hint` | Suggested location (optional, 500 characters at most) |
| `attachments` | File names of the attachments (optional) |

Values are written as JSON, which is valid YAML. Limits on a note: 200,000 characters of
content, 10 attachments, 25 MB per attachment.

When the tidy-up agent files a note, it copies the attachments it needs into `content/`, then
removes the note and its attachments folder from the inbox. When it cannot file a note, the
note stays, with three more fields: `status: "left"`, `reason` and `left_at`. A run that is
interrupted before handling a note records it in `tidy_attempts`; after 3 interrupted runs,
the note is left with a reason.

**Changes** (pages changed directly in `content/`) have no file: they are computed from the
Git history since the last tidy-up.

## Commits

- Writes from the web UI, the REST API or MCP are committed in the name of the person who
  acted, then pushed right away.
- Tidy-up runs are committed by `Synaptane tidy-up` (`Synaptane – rangement` in older
  histories), subject `Tidy-up: …`: one commit per run. Its commits, like Synaptane's merges
  and the initial files, are never reported to the inbox as changes.
- A routine commits its report page, inbox items, the pages written in its folders and its
  memory as `Synaptane routine (<title>)`, subject `Routine "<title>": …` (`Synaptane –
  routine` in older histories). When it wrote pages in its folders, the message ends with the
  trailers `Source: routine-pages` and `Via: {"folders": ["<folder>", …], "report": "<page>"}`
  (`report` when it has a report page): the nightly maintenance reads the pages of those
  folders like a person's change, its report page aside.
- A routine saved from Synaptane: `Routine updated: <name>` or `Routine removed: <name>` (with
  its memory), in the administrator's name; a memory cleared: `Routine memory cleared: <name>`.
  The memories of routines removed by a push: `Synaptane: memories of removed routines
  deleted`. The routines kept in the database by older versions are written by
  `Synaptane: routines written to the repository`, at the first synchronization after the
  upgrade.
- Synaptane's own commits have English subjects: `Inbox: <title>` (a note dropped; also
  `Inbox: edit “…”`, `Inbox: delete “…”`, `Inbox: “…” moved to <space>`),
  `Synaptane: initialize space`, `Synaptane: merge remote changes`,
  `Synaptane: resolve conflicts`, `Synaptane: recover local changes (…)`. The latter is
  authored by `Synaptane recovery` (`Synaptane – reprise` in older histories), so that the
  pages it reapplies are still reported to the tidy-up. Histories written by older versions
  keep their French subjects (`Inbox : …`, `Rangement : …`, `Synaptane : …`); the web
  history labels both.
- Synaptane never force-pushes.
