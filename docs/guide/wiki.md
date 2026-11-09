English | [Français](wiki.fr.md)

# Read, write and search the wiki

Each space shows the `content/` folder of its Git repository as a wiki.
- The tree is on the left and the page on the right.
- The space's **Settings** (the gear next to its name) give the sync state (**Git repository**)
  and the list of members (**Members**).

## Read

- **Markdown**: rendered with headings, tables, checkboxes and highlighted code.
  - Relative links (`[guide](../infra/proxmox.md)`) lead to the target page. A link that
    starts with `/` starts from the root of `content/`.
  - Relative images are displayed.
- **Folder**: its `README.md` or its `index.md` serves as an introduction, followed by the list
  of files.
- **Other files** (see [Files other than Markdown](#files-other-than-markdown)):
  - PDF: the browser's viewer (or an **Open the PDF** link on a phone or tablet);
  - images: displayed;
  - HTML pages: rendered in an isolated frame;
  - Office documents: download, with their extracted text;
  - text and code files: syntax highlighting;
  - other types: download.
- **Pages linking here**: the Markdown pages that link to the page shown.
- **See also**: related pages, found without searching (see below).
- **History**: each change, with its author and the detail of the changed lines. A
  contributor can **restore** an earlier version: **Compare with the current version** shows
  what it would change, then **Restore this version** puts it back in a new commit, under
  their name. Nothing is lost: the replaced text stays in the history. The page keeps its
  current verification (owner, date): going back to an old text does not verify it.
- **Deleted or moved page**: its old address says so. A moved page gives its new address; a
  deleted one links to its history, where restoring a version brings it back.

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

## Files other than Markdown

Markdown first, and all your files. Pages are Markdown, and only they have links, backlinks,
an outline, verification and related pages. But a space holds any file, versioned in Git like
a page (history, move, delete), and Synaptane gets the most out of each one:

| Format | In the wiki | Search | AI assistants (MCP) |
|---|---|---|---|
| PDF | Built-in viewer | Text layer, or OCR for scanned pages | Read the extracted text |
| Word, PowerPoint, Excel (`.docx`, `.pptx`, `.xlsx`, and the old `.doc`, `.ppt`, `.xls`), OpenDocument (`.odt`, `.odp`, `.ods`) | Download, extracted text below | Extracted text | Read the extracted text |
| Images (PNG, JPEG, GIF, WebP, BMP, AVIF, SVG) | Displayed | Text in the image, by OCR (not SVG or AVIF) | Read the OCR text |
| HTML (`.html`, `.htm`) | Rendered in an isolated frame, source one click away | Words | Read and write |
| Other text files (CSV, JSON, YAML, code, logs…) | Syntax highlighting | Words | Read and write |
| Anything else (archives, videos…) | Download | File name | Listed, no text |

On spaces where semantic search is enabled, every text in this table is also searchable by
meaning.

- **PDF**: shown in the browser's PDF viewer, inside the page. Below 700 px wide or on a touch
  screen, where that viewer often stays blank, an **Open the PDF** link above it opens the file
  in the browser's own reader. Its text is extracted page by page; a page without text (a scan) is read by OCR with Tesseract, in the languages set by the
  administrator (French and English by default). Very long documents are read up to 500 pages,
  and up to 100 scanned pages.
- **Word, PowerPoint, Excel and OpenDocument**: no preview in the browser, a **Download** link.
  Their text is extracted and shown under the link, in **Extracted text (used by search)**:
  the body of a Word document, the text of the slides (not the speaker notes), the text and
  values of spreadsheet cells: for a formula, its result as saved in the file, not the formula
  itself.
- **Images**: displayed in the page, and in Markdown pages that embed them
  (`![rack](rack.png)`). The text they contain (a diagram, a screenshot, a photo of a label)
  is read by OCR, unless the administrator turned it off (`ocr_images`). An SVG is displayed as
  an image, its scripts never run; opened on its own, it is downloaded.
- **HTML pages**: rendered in a frame isolated from Synaptane (sandbox): no access to your
  session or to Synaptane, no network requests from scripts, no form submission. **View
  source** shows the code, highlighted. It is never rendered on its own at Synaptane's
  address: opened outside its wiki page, it is downloaded. Search finds words of the source.
- **Text files** (CSV, JSON, YAML, logs, scripts, configuration…): highlighted according to
  their extension, editable like a page, searchable.
- **Other files**: stored and versioned, offered for download, found by their name.

The text of a new document becomes searchable a few seconds to a few minutes after it is added,
the time for the worker to extract it.

**Adding files**:
- web UI: **Add** in a folder (contributors), 25 MB per file;
- from an AI assistant: as attachments of a note (`inbox_add`, base64, up to 10 files of 25 MB
  each), with the folder in `hint`; the nightly tidy-up files them near the page they relate
  to and links them;
- REST API: `PUT …/files/{path}` with `content_base64`, or attachments of an inbox note;
- Git: commit them under `content/` and push. Files over 64 MB are kept but their text is not
  extracted.

**Old Office formats**: the text of `.doc`, `.xls` and `.ppt` files (binary formats from
before 2007) is extracted too: `.doc` and `.xls` by
[catdoc](https://www.wagner.pp.ru/~vitus/software/catdoc/), included in the Docker image, `.ppt`
by Synaptane itself. They are shown like the others: a download link, the extracted text below.
What is read, and the limits:
- **Word** (`.doc`): Word 97 to 2003, and also Word 6 and 95; an RTF file named `.doc` is read
  too. One line per paragraph, table cells separated by tabs.
- **Excel** (`.xls`): Excel 97 to 2003, and Excel 5 and 95. One line per row, cells separated
  by tabs, a blank line between sheets. A date is written as a date (`2019-04-20`) only in one
  of Excel's built-in formats; in a custom format, and in files saved by LibreOffice, it shows
  as a number (days since 1900).
- **PowerPoint** (`.ppt`): PowerPoint 97 to 2003, and files saved in that format by LibreOffice
  (other exporters are untested). Titles, bodies, free text boxes, tables (one line per row, cells separated
  by tabs) and speaker notes, slide after slide (a blank line between slides). Not read:
  PowerPoint 95 files, the text of masters (footers set there), of WordArt, of charts and of
  embedded objects, comments, and text that is only in a picture.
- A file protected by a password, or damaged, has no text: it stays downloadable and is found
  by its name. For the best search, save these files as `.docx`, `.xlsx` or `.pptx`.

Without catdoc (installation from source without the `catdoc` package), `.doc` and `.xls` files
are kept and downloadable, without text, and `synaptane doctor` warns about it; after installing
it, `synaptane reindex` reads the files already there. `.ppt` files do not need it.

## First steps of a space

Until its first tidy-up, the home of a space shows contributors what is left to do: link a Git
repository, write the vocation, choose the AI provider, add content, let the tidy-up run. Each
step ticks itself when the thing is there. The list goes away after the first tidy-up that did
something; **Hide** removes it sooner, in this browser. It does not appear when the space's
tidy-up is turned off.

## Write (contributors)

- **Edit** a text page, **New page**, **Add** a file (25 MB maximum, any type: see
  [Files other than Markdown](#files-other-than-markdown)), **Move** (or rename) or **Delete
  this file** (⋮ menu).
- **Preview**, above the text of a Markdown page (when editing it or creating it), shows the
  page as it will read, without saving anything: the same rendering as reading it, Obsidian
  syntax (`![[image]]`, callouts, `==highlight==`, `%%` comments), links and `[[wiki links]]`
  resolved in the space, images of the space, highlighted code and Mermaid diagrams drawn.
  **Write** goes back to the text, which stays as it is. On a large screen, **Side by side**
  shows both: the preview follows the typing after a short pause, and this choice is
  remembered in your browser. In the preview, links open in a new tab, so the text being
  written is never left. The preview needs JavaScript and only exists for Markdown pages; it is refused
  above 262,144 characters (the save is not), it is prepared one at a time for each account,
  and nothing is committed, indexed or dropped in the inbox until you save.
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

## Import Obsidian, Markdown, Notion or Confluence

Contributors can bring in a whole folder: in a folder's **⋮** menu, **Import Obsidian, Notion,
Confluence…**. Send a zip (100 MB at most, 25 MB per file, 10,000 files, 400 MB once unpacked),
choosing a folder of the space if you wish. Synaptane recognizes what the archive is:

- an **Obsidian vault**, or any folder of **Markdown files** zipped with its attachments: the
  files are kept as they are;
- a **Notion export** or a **Confluence export (HTML)**: the pages are converted to Markdown,
  with clean names, and their links lead to the new paths (see below).

The form can force the format; so can the check screen (**Read it this way**), if the archive is
not read as it should be. Nothing is written at this stage: Synaptane shows what would happen.

- **Recognized format**, the number of pages and attachments and, for an export, the number of
  links rewritten.
- **Files to import**: where each file goes. If everything is in one top folder (a vault or a
  space zipped from outside), that folder is left out.
- **Likely secrets**: these files are left out, because a secret pushed to Git is hard to erase.
  Converted pages are checked too. Tick one only if it holds no real secret.
- **Already in the space**: left as they are, never overwritten.
- **Links that lead nowhere**: links of an export to a page that is not in it (left as
  written, so that **Health** lists them afterwards).
- **Files of the export not imported as they are**: Confluence styles and icons, the second CSV
  of a Notion database (see below).
- **Left out**: hidden files and folders (`.obsidian`, `.trash`, `.git`), system files, and
  what cannot be written safely (links, encrypted files, paths that leave the folder).

**Import** writes everything in one commit, under your name. If it touches more than 20 pages,
the tidy-up reviews it as one item, not page by page. Wiki links and
[Obsidian syntax](../reference/repository-format.md) work as they are. Afterwards, **Health**
lists the links that lead nowhere. An archive that is not imported is deleted after 24 hours.

Whatever the format, a name Windows keeps for its devices (`CON`, `PRN`, `AUX`, `NUL`, `COM1` to
`COM9`, `LPT1` to `LPT9`, with or without an extension) gets a `_` (`con.md` becomes `con_.md`),
so that the repository can be cloned on Windows; links to it in a vault are to be fixed by hand
(**Health** lists them).

### From Notion

1. In Notion, open the **⋯** menu of the page to export (or, for the whole workspace,
   **Settings → General → Export all workspace content**; workspace owners only).
2. Choose **Export**, format **Markdown & CSV**, **Include content: Everything**, and turn
   on **Include subpages** (and **Create folders for subpages** if Notion offers it).
3. Download the zip Notion sends (by e-mail for a large export) and send it to Synaptane as
   it is. A large workspace comes as a zip of zips (`Export-…-Part-1.zip`, `-Part-2.zip`…):
   send the outer zip, Synaptane unpacks the parts, within the same limits. If the export is
   larger than 100 MB, export a few top-level pages at a time.

What is converted:

- **Names**: the 32-character id Notion adds to every page, folder and table
  (`Notes 0123…abcd.md`) is removed; a name Notion shortened takes the full title of its page
  (100 characters at most). Two pages with the same title in a folder become `Notes.md`
  and `Notes (2).md` (case ignored, so the repository can be cloned on macOS and Windows). A page
  with subpages or files becomes the home page of its folder: `Notes/README.md`, with
  `Notes/Subpage.md` and `Notes/image.png`.
- **Links**: links to a page, a file, an image or a database of the export are rewritten to
  the new paths, and so are `https://www.notion.so/…` addresses of a page of the export.
  Links to pages that are not in the export stay as written.
- **Databases**: the CSV becomes an index page (`Tasks/README.md`): a table of the rows, each
  linked to its page, relations as links. When Notion gives both `Tasks.csv` and
  `Tasks_all.csv`, the complete `_all` one is used. The properties of each row go to the
  front matter of its page (`Status: "Done"`), moved from the lines Notion writes under the
  title; a property named like a verification field of Synaptane (`owner`, `verified`,
  `review_every`, `reconfirmed`) becomes `owner (Notion)`, so that it is not taken for one. A
  cell longer than 128 KiB is shortened in the index page and the front matter, and the check
  says so.
- **Blocks**: callouts (`<aside>`) become callouts, toggles (`<details>`) folded callouts
  (shown open), `<br>` a line break. The rest of Notion's Markdown is kept as it is.

What is lost: database views, filters, sorts and formulas (only the values are exported),
comments, colours, page icons and covers, synced blocks (exported as copies), embeds of other
sites (left as links), and anything Notion itself does not put in the export.

### From Confluence

1. In Confluence, open the space, then **Space settings → Export space** (on Confluence Data
   Center or Server: **Space tools → Content tools → Export**).
2. Choose **HTML**, then **Normal export** (the whole space) or **Custom export** (some pages),
   and download the zip.
3. Send it to Synaptane as it is.

The **XML** export (for another Confluence site) is not read: Synaptane says so, and asks for the
HTML one.

What is converted:

- **Tree**: the pages under a page go in its folder, as in Confluence's page tree (taken from
  the export's `index.html`, otherwise from each page's breadcrumb). A page with subpages or
  attachments becomes the home page of its folder: `Procedures/README.md`. Names lose the
  page id (`Procedures_65540.html` becomes `Procedures`), with `(2)` for two equal titles.
- **Pages**: headings, paragraphs, bold, italic, strikethrough, lists (task lists too), tables,
  code blocks with their language, quotes, links and images become Markdown. Info, tip, note and
  warning panels become callouts with the same colour (`> [!info]`, `> [!tip]`, `> [!warning]`
  for a yellow note, `> [!danger]` for a red warning), an expand a folded callout (shown open),
  a panel a callout with its title, a status a bold word, an emoticon its emoji. The page's
  "Created by … last modified on …" line is kept in italics, and its list of attachments at the
  bottom. A page whose HTML exceeds 5 MB is not converted: the check lists it with the reason.
- **Attachments**: next to their page, under their real name (`attachments/65541/65601.png`
  becomes `Procedures/Backup/network diagram.png`). An attachment whose page is not in the
  export stays where it was, in `attachments/<page id>/`.
- **Links**: links to a page or an attachment of the export are rewritten to the new paths, as
  are addresses of the Confluence site with the id of a page of the export
  (`…/pages/65540/…`, `viewpage.action?pageId=65540`). Links to pages that are not in the
  export stay as written; Confluence anchors and links that leave the export keep only their text.

What is lost: page history, comments in the margin, likes, labels, page restrictions,
layouts and colours, macros that compute their content when the page is shown (Jira issues,
page lists, tables of contents: Synaptane shows its own outline) beyond what the export holds,
and diagrams from apps (draw.io, Gliffy) beyond their exported image. The export's styles and
icons are not imported.

The HTML of an export is not trusted: it is only read, nothing in it is run or loaded, scripts,
frames and forms are dropped, links keep only safe schemes (`http`, `https`, `mailto`…), and every
path written comes from the layout made by Synaptane, never from the archive.

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
  without dating. It is a commit in your name (`Page verified: <path>`, or
  `Page tracking updated: <path>` when only the owner or the interval changes) that only
  touches the lines of these three fields: the rest of the text is kept byte for byte,
  and the block is created at the top if it does not exist. Such a commit does not count as a
  change for "modified since it was verified"; neither do those written in French by earlier
  versions (subjects starting with `Page vérifiée` or `Suivi de la page mis à jour`), still
  recognized.
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
- **Report as outdated** (anyone who reads the page, ⋮ menu): say what no longer holds. The
  page then shows a banner to everyone with the reports (who, when, why), search marks it
  "reported as outdated", the space's **Health** tab lists it, and assistants see it. A report
  stays open until someone **verifies** the page (reread and found right), a contributor
  **dismisses** it (the page still holds), or its author withdraws it. A moved page keeps its
  reports.
- **A page that still holds waits longer**: verified again with no change since its previous
  verification, its interval doubles (6 months, then 1 year, then 2 years at most), and
  `reconfirmed: N` in its front matter counts these verifications in a row. A change to the
  page, by anyone, or an interval chosen by hand starts again from its interval. "No change"
  compares the text with the one the page had when it was last verified, not commit messages.
- **Reminders**: the home page lists, under **Waiting**, the pages due for review that you own
  (recognized by your username or your email address). A grouped email goes out at most once
  a week, on a working day, to each owner of overdue pages (verified address; uncheck
  **Receive emails from the routines of my spaces** in *My account* to stop receiving it).
  The **Consistency check** routine receives the list of pages to review.

The purpose of the space can say which pages must be verified, and by whom (most templates
suggest it in their "What matters most" section, "Ce qui compte le plus" in French).

## Wiki health

The **Health** tab of a space (gear icon, next to Activity) runs a check without AI, at the
latest commit:
- **broken links**, with a suggestion when a file with the same name exists elsewhere;
- **orphan pages**: no other page leads to them (folder `README.md` and `index.md` files do not
  count);
- **empty** or **untitled** pages (neither a `# Title` nor a `title:` in the front matter);
- **pages to review**: verified, but the verification due date has passed (owner and date),
  and among verified pages, those **without an owner**;
- **old pages**: not changed for 3, 6, 12 or 24 months, according to Git;
- **tables that disagree**: two pages each have a table about the same things (same first
  column, at least two rows in common, a column of the same name) and give a different value
  for the same row, the NAS with 8 TB here and 12 TB there. One of them is probably outdated.
  Tables that disagree more than they agree describe different things (two companies' sheets),
  and two dated notes (`veille-2026-11-02.md`) each hold on their day: neither is reported.
  The agent routines receive these values too.

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

Each result gives its last change: when, by whom, with an **AI** badge when an AI wrote it
(the nightly tidy-up, a routine, an assistant). With "verified" or "to review", it says how
far to trust the page.

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

- **By email**: when the super admin has set up the [capture by
  email](../admin/email-capture.md), the **Inbox** page of a space shows its address to those
  whose emails would be accepted there (a verified address, the right to drop notes; a super
  admin only in their spaces), for example `brain+acme.infra@example.com`, with a **Copy**
  button (and so do the first steps of the space). Send or forward an email to it **from the
  verified address of your account**: it becomes a note in your name, its subject as the title,
  with its attachments (10 at most), and the tidy-up files it. The receiving server must have
  authenticated your email (DKIM or DMARC of your domain, see the admin guide): a forged sender
  is refused in silence. An email can name several spaces, in To or Cc (never in Bcc), and must
  be dated within 48 hours of its arrival (forward an older one: the forward is dated today).
  Depending on the settings, you get an answer when the email is refused (why) or each time
  (with the link to the note).
- **Inbox** (left bar): filter by text or by type (notes, changes, items left by the tidy-up).
  On a note, a contributor can **edit** it (if the tidy-up had left it, it goes back to "to
  process"), **move** it to the inbox of another space where they contribute, or **delete** it
  (it stays in the Git history). A changed page that asks nothing of the tidy-up can be
  **marked as seen**.
- **Preview** of a note: above its text, when dropping it (in the inbox or from **Drop
  information**, in the space chosen) or correcting it, as for a page (see
  [Write](#write-contributors)): the note as it will read in the inbox, without saving anything
  (links from the root of the space, as when reading the note). Side by side is offered when
  correcting a note, not in the narrow drop form. Previewing asks for the same right as dropping or correcting the note
  (contributor).
- **Activity**: everything that changes in the space, according to the Git history (pages,
  inbox, tidy-ups), with commits pushed outside Synaptane flagged. Each page touched leads to
  the detail of the change.
  - **Person or AI**: a change written by an AI carries an **AI** badge: the nightly
    tidy-up, a routine, or an assistant connected over MCP, named ("Alice via Claude"). A
    change through the REST API gives the token's name. The **Written by AI** filter keeps
    only those.
  - At the top, the **share of the AI** in the page changes of the last 30 days, with the
    count for each origin (tidy-up, routines, assistants, web interface, REST API, pushed with
    Git).
  - A commit pushed from elsewhere always counts as **pushed with Git**, whatever its message
    says: only Synaptane's own commits are read for their origin.
