English | [Français](wiki.fr.md)

# Read, write and search the wiki

Each space shows the `content/` folder of its Git repository as a wiki.
- The tree is on the left and the page on the right.
- The **Repository and members** tab gives the sync state and the list of members.

## Read

- **Markdown**: rendered with headings, tables, checkboxes and highlighted code.
  - Relative links (`[guide](../infra/proxmox.md)`) lead to the target page. A link that
    starts with `/` starts from the root of `content/`.
  - Relative images are displayed.
- **Folder**: its `README.md` or its `index.md` serves as an introduction, followed by the list
  of files.
- **Other files**:
  - PDF: the browser's viewer;
  - images: displayed;
  - text and code files: syntax highlighting;
  - other types: download.
- **Pages linking here**: the Markdown pages that link to the page shown.
- **See also**: related pages, found without searching (see below).
- **History**: each change, with its author and the detail of the changed lines.

### See also: related pages

Below **Pages linking here** (and, on mobile, after the content), the **See also** panel shows
up to 8 pages close to the page shown: their title, path and the reason.
- **same topic**: meaning, when the space has semantic search (embeddings); two pages that talk
  about the same thing in different words find each other.
- **shared words**: rare words in common (accents and word forms folded, as in search; common
  words hardly count).
- **cites this page**: it links to the page shown; **linked from this page**: the page shown
  links to it; **shared links**: they cite the same pages, or are cited by the same ones.
- **same folder**: a slight bonus, never enough on its own.

The panel loads after the page, without slowing it down, and stays hidden when nothing is close
enough: better nothing than noise. PDFs, Office documents and images appear through their
extracted text; a file without text, never.

**not linked yet** flags a related page that the page shown does not link to. A contributor
can click **Suggest a link**: a note asking to link the two pages is dropped in the inbox, and
the nightly tidy-up will add the link (nothing is written to the wiki right away). The
suggestion is made only once; while it waits, the panel shows **Suggestion pending**.

Assistants get the same list (MCP tool `related`, REST `GET …/files/{path}/related`), and the
tidy-up agent receives it when it reads each note: enough to complete an existing page rather
than duplicate it.

HTML and SVG files from a repository never run in Synaptane: they are offered for download.

## Write (contributors)

- **Edit** a text page, **New page**, **Add** a file (25 MB maximum), **Move or delete**.
- Each save produces a commit in your name, pushed to the repository at once.
- **Move** keeps links right, in the same commit: the relative links of the moved page are
  recomputed for its new location, and the pages that linked to it are rewritten (relative
  links, and `[[folder/name]]` links that would no longer lead to it). A `[[name]]` link that
  still finds it is not touched, and only the link changes: the rest of the page stays as it
  is. The message shown after the move lists the updated pages. MCP, REST and the tidy-up agent
  move files the same way.
- Each changed page also shows up in the inbox (a **Change** item, computed from the Git
  history). The nightly tidy-up will check its location, its links and its consistency with the
  neighboring pages. A change to spaces or blank lines only is ignored.
- A text that seems to contain a secret (password, API key, private key) is refused before it
  is committed: it would end up on the Git host. Remove it (keep it in a password manager), or
  check **This is not a secret: save anyway** if it is a false alarm.
- If the page has changed in the meantime (another person, an external push), the save is
  refused and your text is kept in the form. Reload, then carry over your changes.

## Verify a page

To know, when reading a page, whether you can trust it: who answers for it, when it was
verified, whether it is due for review. Everything is in the page's front matter, so in Git:

```markdown
---
owner: ada            # an account username, or a free-form name
verified: 2026-10-03  # date of the last verification
review_every: 6m      # 90d, 12w, 6m, 1y; absent: the space's interval; never: never
---
# Failover procedure
```

- **State** of a Markdown page: **verified** (a date, and the due date not yet passed), **to
  review** (due date passed) or **never verified**. Without an interval, neither its own nor
  the space's, a verified page stays verified. Other files have no state.
- **On the page**: a discreet badge in the top bar, "Verified on Oct 3 · Ada" in green, or
  "To review since …" in amber; nothing for a page never verified. The details (owner, due
  date, interval, "modified since it was verified") are at the top of the ⋮ menu, and on hover
  over the badge. On mobile, the badge shrinks to its icon.
- **Mark as verified** (contributors, ⋮ menu): reread the page, then the button writes today's
  date in `verified`; a page without an owner becomes yours. The same form chooses the owner
  (an account of the space, or a free-form name) and the interval; **Save** changes them
  without dating. It is a commit in your name (its subject starts with `Page vérifiée`) that
  only touches the lines of these three fields: the rest of the text is kept byte for byte,
  and the block is created at the top if it does not exist.
- **Tidy-up**: a commit that only changes these three fields is not a change to review (the
  content is compared, not the author or the message: the same thing pushed from Git is
  recognized too). A page changed and then verified still has to be reviewed.
- **AI agents**: the tidy-up agent and assistants (MCP) can neither erase nor change these
  fields by rewriting a page: they are restored. A page changed by the agent stays verified;
  the page says "modified since it was verified" (compared by day), and it is up to its owner
  to reread it.
- **Lists and search**: a green (verified) or amber (to review) mark follows the page name in
  folders and in results. Search ranks verified, up-to-date pages a little higher (+10%), pages
  due for review a little lower (−7%): the factor is multiplicative, relevance remains the
  first criterion.
- **Space interval**: *Settings → General*, **Verified pages: review** (1, 3 or 6 months, 1 or
  2 years, or never). A page that carries `review_every` keeps its own.
- **Reminders**: the home page lists, under **Waiting**, the pages due for review that you own
  (recognized by your username or your e-mail address). A grouped e-mail goes out at most once
  a week, on a working day, to each owner of overdue pages (verified address; uncheck
  **Receive e-mails from the routines of my spaces** in *My account* to stop receiving it).
  The **Consistency check** routine receives the list of pages to review.

The purpose of the space can say which pages must be verified, and by whom (the templates
suggest it in their "Ce qui compte le plus", "what matters most", section).

## Wiki health

The **Health** tab of a space (gear icon, next to Activity) runs a check without AI, at the
latest commit:
- **broken links**, with a suggestion when a file with the same name exists elsewhere;
- **orphan pages**: no other page leads to them (folder `README.md` and `index.md` files do not
  count);
- **empty** or **untitled** pages (neither a `# Title` nor a `title:` in the front matter);
- **pages to review**: verified, but the verification due date has passed (owner and date),
  and among verified pages, those **without an owner**;
- **old pages**: not changed for 3, 6, 12 or 24 months, according to Git.

Each line leads to the page, and to **Edit** for contributors. The same check is served by the
REST API (`GET /api/v1/organizations/{org}/spaces/{space}/health`, parameters `months` and
`limit`). The tidy-up also runs it at the end of a run, on what it wrote.

## Search

- **Menu field**: searches all your spaces.
- **Field of a space**: searches that space only.

It behaves like `grep`:
- case-insensitive and accent-insensitive search ("procedure" finds "procédure"), singular as
  well as plural ("servers" finds "server"); a word of three letters or fewer only matches a
  whole word;
- each word counts, except stop words ("de", "la", "the"…); pages that contain all the words,
  or the exact phrase, come first;
- **Regular expression** for patterns, for example `back\w+ night`.

In spaces where **semantic search** is enabled, it also finds pages that are close in meaning
(a question in English about a page in French, a paraphrase…). These results are labeled "by
meaning", or "words and meaning". Enabling semantic search on a space never pushes down its
pages found by words.

**Notes dropped but not yet tidied** that match are shown above the results, under **Waiting
to be tidied**: what was just dropped can be found right away.

Search covers:
- Markdown and all text files;
- the text of PDFs, including scanned ones thanks to OCR;
- the text of Word, Excel, PowerPoint and OpenDocument documents;
- the text of images (OCR);
- file names.

The text of a new PDF becomes searchable a few seconds to a few minutes after it is added, the
time to extract it. You never see a result from a space you do not have access to.

## Inbox and activity

- **Inbox** (left bar): filter by text or by type (notes, changes, items left by the tidy-up).
  On a note, a contributor can **edit** it (if the tidy-up had left it, it goes back to "to
  process"), **move** it to the inbox of another space where they contribute, or **delete** it
  (it stays in the Git history). A changed page that asks nothing of the tidy-up can be
  **marked as seen**.
- **Activity**: everything that changes in the space, according to the Git history (pages,
  inbox, tidy-ups), with commits pushed outside Synaptane flagged. Each page touched leads to
  the detail of the change.
