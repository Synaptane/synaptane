English | [Français](ai.fr.md)

# AI and tidy-up

Every night, an AI agent files the inbox of each space into its wiki. It uses the
organization's AI provider.

## Choose a provider (organization page → Artificial intelligence)

| Provider | Credentials | Notes |
|---|---|---|
| Anthropic (Claude) | API key (Anthropic Console) | Default model: `claude-sonnet-5-5`, medium thinking (good value for tidy-up; `claude-opus-5-5` for demanding spaces). Automatic fallback when a safety filter refuses. |
| OpenAI, Mistral | API key | Model to enter (`mistral-large-latest` by default for Mistral). |
| Ollama | None (local address) | The model must support tool calling. |
| OpenAI-compatible | Address and, if needed, key | vLLM, LM Studio, gateways… |
| **Claude subscription (Claude Code)** | CLI signed in on the server | Super admin only. See below. |
| **ChatGPT subscription (Codex)** | CLI signed in on the server | Super admin only. See below. |

- The API key is encrypted and never shown again. It stays tied to its provider and its
  address: changing either one means entering the key again (or removing it).
- Custom **API address** (Ollama, OpenAI-compatible, gateway): only the super admin can change
  it, because it makes the server send requests to the chosen address.
- **Test the connection** makes a real tool call. With an API key, the test runs right away;
  with a subscription, it runs in the worker (see below).
- Each space can pick another model (the space's **Settings → Tidy-up**).

## Use a Claude or ChatGPT subscription

Instead of a pay-as-you-go API key, the agent can run on a subscription. The provider's
**official CLI** (`claude` or `codex`) then runs the agent, and Synaptane never sees the
credentials.

1. Install the CLI on the worker's machine. The published image
   (`ghcr.io/synaptane/synaptane`) contains neither, since Claude Code may not be
   redistributed. An image you build yourself contains both (`AGENT_CLIS=true`, the
   Dockerfile's default): in `.env`, `COMPOSE_FILE=compose.yaml:compose.build.yaml` and
   `SYNAPTANE_IMAGE=synaptane:local`, then
   `GIT_SHA=$(git rev-parse HEAD) docker compose up -d --build`. See
   [Build the image yourself](installation.md#build-the-image-yourself).
2. Sign it in once, **in the worker**, with the provider's flow:
   - with Docker: `docker compose exec worker claude auth login`, or
     `docker compose exec worker codex login --device-auth`;
   - without Docker: `claude auth login`, or `codex login`, as the system account that runs
     `synaptane worker` (prefixed with `HOME=…` if `SYNAPTANE_AGENT_HOME` is set: the
     Artificial intelligence page shows the exact command).
3. As super admin, choose "Claude subscription" or "ChatGPT subscription" for the
   organization, then **Test the connection**.

`synaptane doctor` reports whether the CLIs are installed and whether they meet the isolation
contract (see "Security").

**Where the login lives.** The CLI keeps it in its own `HOME`, given by
`SYNAPTANE_AGENT_HOME`:
- with Docker, `/agents`, on the `synaptane-agents` volume, mounted **only** in `worker`
  (where it is also the `HOME`, hence the simple command above). The `app` container, which
  faces the Internet, cannot see it. It is not in the data volume or in backups either: after
  restoring onto another server, sign the CLI in again. `docker compose down -v` erases it;
- without Docker and without this variable, the `HOME` of the account that runs the worker.

Synaptane never reads, copies, moves or stores these credentials: only the official CLI
handles them.

**Test the connection** runs **in the worker**, the only place where the CLI is signed in. The
page shows "Test in progress…" and updates itself (or `POST …/organizations/{org}/ai/test`
returns 202, and `GET …/ai` gives `test_pending`, then `test_ok` and `test_message`). The
test goes through the fast queue and takes a few minutes at most. If "Test in progress…"
does not change, check that the worker is running (`docker compose ps`). For Claude, it is a
real tiny call to the smallest model: an expired session is caught this way, even when
`claude auth status` says it is signed in.

**Upgrading from a version where the login was in `/data/home`.** It used to sit in the data
volume, shared with the web container. Synaptane does not move it (only the CLI handles
credentials); `synaptane doctor` and the Artificial intelligence page report the old folder
as long as it exists (without reading anything in it). Once and for all:
1. sign the CLI in again in the worker: `docker compose exec worker claude auth login`
   (or `codex login --device-auth`);
2. **Test the connection** on the Artificial intelligence page;
3. delete the old login yourself:
   `docker compose exec app rm -rf /data/home/.claude /data/home/.claude.json /data/home/.codex`.

A subscription has no usage cost: the page asks for neither price nor budget. Instead,
Synaptane reads the subscription's **quota**, that is the used share of the 5-hour and 7-day
windows:
- with Claude, on every tidy-up run, from the readings Claude Code emits
  (`rate_limit_event`, not documented by Anthropic: the reading may disappear with a CLI
  update, and tidy-up then carries on without it);
- the tidy-up log shows it (for example 3% → 5% for the 5-hour window, 29% for the 7-day
  window), and so does the connection test;
- above 90% of a window, the organization's administrators are alerted.

With ChatGPT (Codex), the quota is not read yet.

**Security.** During a run, the CLI only has Synaptane's tidy-up tools, served by a
short-lived local MCP server protected by a single-use token. It has no shell, no file access
and no web: malicious content dropped into the inbox cannot read the server.

This guarantee rests on command-line options (`--tools ""` and `--strict-mcp-config` for
Claude Code, with deferred tool loading switched off by `ENABLE_TOOL_SEARCH=false`;
`--disable …`, a read-only sandbox and refused approvals for Codex). A new
version may rename them or change their meaning: `synaptane check-clis` **checks this
automatically**, with no login, no model call and no network:
- Claude Code's help still lists every option used, with the same meaning;
- every Codex feature that is switched off still exists; any other feature on by default
  must be in an allowlist (`cli_contract.CODEX_ALLOWED`): a new feature makes the contract
  fail until it is classified (when in doubt, switched off);
- above all, the exact command of a run is launched against a **fake local provider**
  (127.0.0.1, dummy key), which records the tools the CLI offers the model and then answers
  with an error. Only Synaptane's tools are allowed. For Codex, the following are also
  allowed: tool search (limited to Synaptane's server, checked), reading MCP resources
  (Synaptane publishes none), `request_user_input` (never answered in non-interactive mode)
  and `apply_patch` (refused by the read-only sandbox; no option removes it).

This contract runs in CI every time it builds the image (with the CLIs), and every Monday
before the CLI upgrade workflow opens its PR: if it fails, the PR says so in its title
("isolation contract broken"). `synaptane doctor` reports it too (as a warning).

Known limitation: the CLI runs under the same system account as the worker, which can read
`/data` (master key, repositories). A flaw in the CLI itself could therefore be enough to
read the server. Running it under a separate account without that read access is planned.

Provider terms (October 2026):
- **OpenAI** allows using a ChatGPT subscription in third-party tools.
- **Anthropic** allows signing a subscription in to the **official Claude Code binary**. It
  forbids third-party applications from handling these credentials, and states that Pro and
  Max subscription limits assume ordinary individual use.

For a company organization shared by several people, prefer an API key, or a Team or
Enterprise plan.

## Nightly tidy-up (per space)

Settings are in the space's **Settings → Tidy-up**; the log and **Tidy now** are in its
**Tidy-up** tab (the button is also in the Inbox tab).

- **Daily tidy-up** (time and time zone): 02:00 Paris time by default. A run only happens if the inbox has
  items. **Tidy now** starts one right away (5 times per person and per space over 24 hours,
  20 for an organization admin: see [Cost and budget](#cost-and-budget)).
- **Tidy-up as you go** (the "Also tidy as you go, shortly after each drop" box, off by
  default): a run starts N minutes after a note is dropped (**Delay after a drop**, 15 minutes
  by default, from 1 to 1,440), on top of the scheduled one. Notes dropped during that delay join the same run. Nothing is scheduled
  once the month's budget is used up.
- **Notices to contributors**: after each committed run, the person who dropped a note gets
  an email (one per run) saying where it was filed, with links to the pages, or why it was
  left. On by default; anyone can turn it off in **My account**. Sent only to a verified
  address, and only if the person can still read the space. A note left again on the next
  run does not trigger another notice.
- **Purpose and space-specific instructions**: see
  [below](#purpose-and-tidy-up-instructions).
- **What the agent does**:
  - it reads the tree and the pages, as well as the text of attachments (extracted from PDFs,
    Office documents and images during the run, and kept for later runs);
  - it searches by words and, if meaning-based search is enabled for the space, by meaning: a
    page that says the same thing in other words is completed rather than duplicated;
  - when reading a note, it receives its **related pages** (same topic, shared words: see
    [See also](../guide/wiki.md#see-also-related-pages)), and for a page someone changed,
    that page's related pages, with possible duplicates flagged; the `related` tool gives
    them for any page. Related pages "not linked yet" help it add links. A note asking to
    link X and Y, dropped from the wiki's "See also" panel (**Suggest a link**), only asks it
    to add the link;
  - it judges how fresh a page is from its last three changes (`history`), and writes a long
    page in several steps (`append_page`);
  - it creates, completes, moves, merges or deletes pages, and files attachments;
  - it links pages: links to the pages mentioned, the folder index (`README.md`), at most
    three backlinks; it keeps wiki links `[[name]]`, which follow a moved page;
  - when it moves a page, the page's links and the links pointing to it are rewritten right
    away (the same computation as for a move from the web);
  - at the end of the run, a check without AI sends it, once, the broken links it introduced
    and the pages it created with no incoming link, for one round of fixes; whatever remains
    is marked "To verify" in the run's log, without blocking anything;
  - it closes each item, or leaves it in the inbox with a reason (it will be retried).
- **Safeguards**, with no human approval at all:
  - the agent only rewrites a page after reading all of it, and only closes an item after
    reading it; an attachment never overwrites a file;
  - **at most 10 deletions per run**: beyond that, they are refused, the agent leaves the
    items concerned in the inbox, and administrators are alerted (email, `space.alert`
    webhook);
  - inbox content (text, title, suggestion, source), changes made by others and the pages it
    reads are presented to the agent as data, never as instructions, between tags that
    dropped text cannot close. Space-specific instructions cannot lift this rule;
  - the agent must not make anything up: when in doubt, it leaves the item with its question;
  - text extracted from an attachment goes through the secret detector: if it probably
    contains a secret, it is not sent to the model, the attachment is not filed and the item
    is left with the reason;
  - with Claude through an API key (no gateway), tools are strict: the model cannot pass them
    unknown or wrongly typed arguments;
  - write tools report broken links to it, and `move` reports the pages that still point to
    the old path;
  - each closed item states the pages it was merged into (log: "→ page", or no page given);
  - **big changes**: never blocked (Git keeps the history, and the run can be undone), but
    reported to administrators (`big_changes` alert) when an existing page loses at least
    30% of its content and at least 300 bytes, or when a run moves at least 10 pages or
    touches at least 20 existing pages. The log lists every heavily shortened page;
  - a response cut off by the length limit is never executed.
- **Bulk imports**: a commit that touches more than 20 pages (import, reorganization) becomes
  a single "Import: N pages" item; the agent checks the overall organization instead of
  rereading each page.
- **"Left" or "not processed"**: an item is *left* by the agent's decision, with its reason;
  it is only picked up again by a manual tidy-up or after it changes. An item given to a run
  that stops before closing it (provider error, limit, budget reached) is *not processed*:
  the work already done is committed, the item stays new and is picked up by the next run,
  with a counter (`tidy_attempts` in a note's header, in the database for a changed page).
  After 3 interrupted runs, it is left with the reason. In both cases (run stopped, item
  given up), administrators get the `tidy_interrupted` alert. An interrupted run does not
  chain straight into another run.
- **Language**: the agent's instructions are in English, whatever the language of the space.
  It writes pages, summaries and reasons in the language of the space's existing pages
  (detected from its home page and longest pages), or the one its purpose sets; in a space
  with no pages yet, in the language of the note it files. A page it completes keeps its
  language; in a space that mixes languages, it keeps the language of the page or folder it
  writes in.
- **One commit per run**, under the name `Synaptane tidy-up` (subject `Tidy-up: …`), pushed
  right away; commits from older versions, named `Synaptane – rangement`, are still
  recognized as tidy-up runs. If
  someone changed a page during the run, the two versions are merged, file by file; on a
  conflict, the human version is kept for that file (logged as a conflict), the rest is
  committed, and the items filed into that page become not processed again. Nothing is
  overwritten, and **the model is never rerun** for a write conflict. If the local copy is
  busy (sync in progress), the commit waits up to 35 minutes.
- **One run at a time per space**, even with several workers: a request that arrives during
  a run is queued again two minutes later. **Tidy now** replaces a pending scheduled run: it
  does run in manual mode.
- **Limits per run**: 25 items, 120 steps, 2 million tokens, or 400 tool calls with a
  subscription. If new items remain, a **chained run** follows right away (8 at most), until
  the inbox is empty or the month's budget is reached.
- **Log** (the space's **Tidy-up** tab): operations, summary, tokens and estimated cost of
  each run, failures and retries included.
- **"Quality" block in the log**: measured without AI, just before the commit, for each
  completed run (nothing for a failed run or one older than the measurement). The title says
  "nothing to report" or the number of points to check; the expanded block shows:

  | Line | What it measures | What to do if it lights up |
  |---|---|---|
  | Files created, modified, moved, deleted | size of the run | compare with the number of items filed |
  | Broken links before / after, **introduced** | links that no longer lead anywhere, in the pages touched and in those that pointed to a moved or deleted page | fix the links listed, or undo the run |
  | Created with no incoming link | page or attachment that nothing links to the rest of the wiki | add a link from the topic's page |
  | Missing from the folder index | page missing from its folder's `README.md` (for a README, from the parent folder's) | complete the index |
  | Pages created without a level-1 heading, empty | form | complete the page |
  | Items filed without a page given | item closed without saying where it was merged: a legitimate duplicate, or lost information | check the item in the inbox history |
  | Pages heavily shortened, text removed | content removed from kept pages (a move is compared with the old path) and from deleted pages | reread the commit diff |
  | Items left | share of items left in the inbox | a high, lasting share points to unclear instructions |
  | Per filed item | tokens, turns and cost per filed item | compare two models or two sets of instructions |

  Nothing is blocked: big changes are also reported to administrators (`big_changes` alert,
  above). The same values are in the `metrics` field of `GET …/tidy/runs` (REST), to track
  them over time. To change the instructions or the model without a leap in the dark, see
  the [evaluation bench](../dev/eval.md).
- Without a configured AI provider, scheduled tidy-up does not run (no failure is recorded
  every night).

## Purpose and tidy-up instructions

The second brain of an infrastructure team, a marketing team or a family is not tidied the
same way. So each space can describe, in **Settings → Purpose** (organization
administrators), two Markdown texts versioned in its repository:

| | Purpose | Tidy-up instructions |
|---|---|---|
| File | `.synaptane/vocation.md` | `.synaptane/prompts/tidy.md` (the old name `rangement.md` is still read if it is the only one) |
| Says | **what** the space **is** | **how to file** |
| Content | what it is for and for whom; what goes in and what does not; how folders are organized; conventions (language, tone, file names, page format: fact sheets, procedures, meeting notes…); what matters most (accuracy, freshness, confidentiality…); vocabulary | precise rules for the agent: "one page per supplier in `suppliers/`", "never rename procedures"… |
| Read by | tidy-up, routines, AI assistants (MCP, REST) | the tidy-up agent only |

- **Templates** ready to adapt: infrastructure / ops team, marketing team, product team,
  client project, personal second brain, family, job search, blank. Each one exists in
  English and in French, and is offered in the language of the person using it (account
  language; `GET /vocation-templates` in REST likewise). A template pre-fills the
  editor (with a confirmation if it already holds other text); nothing is saved before
  **Save**. **Preview** shows the rendering without saving.
- **Save** makes a commit in the person's name (`Space purpose updated` or `Tidy-up rules
  updated`; `… removed` when the text is emptied), under the space lock, then a sync; the page
  shows the last five changes (date, author). Git remains the source: both files can also be
  changed with a push. If the file changed since the page was opened (push, another
  administrator), saving is refused and you are asked whether to replace it. 20,000
  characters at most; a probable secret is refused (as for pages); empty text removes the
  file.
- **When creating a space**, a purpose template can be chosen. The repository is not cloned
  yet: the text is put on hold (a `<id>.attente.json` file next to the local copy, in
  `repos/`) and the **first sync** writes it to the repository, in the person's name, right
  after the space is set up. A repository that already has its own purpose keeps it. Until
  the space is synced, the Purpose page (and `PUT …/vocation`) edits this pending text.
- **In tidy-up**, the purpose is given to the agent in its system instructions, in a block
  introduced as the space's purpose defined by its administrators, after the default
  instructions and **before** the space-specific instructions, which take precedence over it
  on how to file. It is read at the run's starting commit, without the templates' HTML
  comments. The agent files according to the folder organization it describes, follows its
  conventions and leaves in the inbox, with the reason, what it excludes. Neither the
  purpose nor the instructions lift the safeguards: dropped content remains data, never
  instructions, and the deletion cap always applies.
- **For assistants**: `list_spaces` (MCP) gives the beginning of each space's purpose, and
  `read` with the path `.synaptane/vocation.md` gives all of it. The MCP server's
  instructions ask assistants to read and follow it before writing to a space.

## Agent routines (per space)

A **routine** is a scheduled AI agent that rereads a space's wiki and produces an output: a
report, suggested fixes, a digest sent to the team. The space's **Settings → Routines**
(`GET …/spaces/{space}/routines` in REST). Organization administrators create, edit, run
and delete them; contributors see the routines and their log; readers do not see them (they
read report pages like any other page).

A routine is made of:

- **instructions**: what the agent must do and what its output must contain;
- a **schedule**: every day, every week (one day) or every month (day 1 to 28), at a local
  time of the space (the tidy-up time zone). A routine created, re-enabled or rescheduled
  after its time of day waits for the next slot; **Run now** runs it right away;
- a **scope**: folders of the wiki, or the whole space. The agent only reads there (tree,
  pages, search, backlinks, related pages, history);
- **outputs**, at least one:
  - a **report page**, rewritten on every run (`reports/consistency.md` for example). Any
    manual change there will be replaced: the page says so in its footer;
  - **inbox items** (10 at most per run), one per fix to make, with the pages concerned: the
    nightly tidy-up handles them with its safeguards (deletion cap, merge, undo);
  - an **email** to the organization's administrators, or to all the space's members: only
    to a verified address, to active accounts that can read the space and have not unchecked
    "Receive emails from the routines of my spaces" in **My account** (a preference separate
    from tidy-up notices, on by default, recalled in every email);
  - the `routine.done` **webhook**, with the output text (organization webhooks).

### Built-in templates

Added in one click (**Enable**), or after adjusting them (**Customize first**):

- **Consistency check**, every Monday at 06:00: rereads the pages changed since the last run,
  then the pages linked to them (40 at most; in a quiet week, the oldest ones in scope), and
  spots contradictions between pages, information that is probably outdated (past dates, "in
  progress", versions) and duplicate topics. It also receives the wiki health check (broken
  links, orphans, old pages: see the **Health** tab) and, computed without AI, the pairs of
  pages with very similar content among the first 20 pages to reread (possible duplicates,
  which it checks by reading them). Output: the `reports/consistency.md` page and one inbox
  item per problem to fix, with the pages and excerpts concerned. It does not drop again a
  problem already dropped and not yet filed.
- **Weekly digest**, every Friday at 16:00: what the team learned and decided during the
  week, based on the wiki's Git history (pages created, heavily changed) and tidy-up runs
  (items filed, left). Output: the `reports/weekly-digest.md` page and an email to the
  space's members.

Both templates exist in English and in French: the form is pre-filled with the instructions
and the report page in the language of the person creating the routine (`rapports/coherence.md`
and `rapports/semaine.md` in French), and a routine enabled in one click takes that language
too. A **blank routine** takes your own instructions (review of a folder, Monday briefing,
regenerated welcome FAQ…).

### Security

- **A routine writes nowhere except its outputs.** It has no wiki write tool: the report
  page has a path fixed by the routine (the agent does not choose it), items go to the
  inbox, and real fixes go through tidy-up. This is also what limits injection: malicious
  text read in a page can, at worst, only suggest an inbox item, which tidy-up examines.
- Everything the agent reads (pages, search results, history, activity, health check, space
  purpose) is presented to it as data, never as instructions, between tags that text cannot
  close. The routine's instructions come from an administrator and remain subject to these
  rules.
- A probable secret (password, key, token) in the output text or an item is refused to the
  agent, which must remove it.
- **Space purpose**: if the repository contains `.synaptane/vocation.md`, the agent gets it
  in its context, to judge what is on topic.
- **Language**: as for tidy-up, the agent's instructions are in English; it writes the output
  text, the inbox items and its summary in the language of the space's pages, or the one its
  purpose sets; in a space with no pages yet, in the language of the routine's instructions.

### Execution, cost and log

- The worker checks every minute what is due. Routines go through the same queue as tidy-up
  (slow jobs) and **never at the same time as a tidy-up of the same space** (same lock): if
  busy, the request is picked up again two minutes later.
- Same providers as tidy-up (API key, or Claude/ChatGPT subscription through the official
  CLI; Synaptane never reads a subscription's credentials).
- **Limits per run**: 60 steps, one million tokens, 200 tool calls with a subscription.
- **Budget**: the cost of routines counts toward the organization's monthly budget (together
  with tidy-up). Budget reached: the routine is not started; reached during the run, it
  stops. In both cases the failure is logged and nothing is published.
- **An interrupted run publishes nothing** (provider error, limit, budget): no half-written
  report. Its cost is counted, and administrators get the `routine_failed` alert (email,
  `space.alert` webhook). A transient provider error before any work is retried by the
  queue, without an alert; a subscription's quota is read as for tidy-up (`quota_high`
  alert above 90%).
- **Log** (the routine's page): summary, outputs (page rewritten, items dropped, emails
  sent, webhook), output text, model, tokens, estimated cost or quota, and for a failure,
  the translated cause (technical details for administrators).
- A routine's commit (report page and inbox items together) is made under the name
  `Synaptane routine (<name>)`, subject `Routine "<name>": …`: it does not show up as a change
  for tidy-up to examine (nor do those of older versions, named `Synaptane – routine`).
  Runs of a deleted routine still count toward the month's budget.
- Without a configured AI provider, a scheduled routine does not run (no failure recorded);
  started by hand, it fails and says why.

Nightly tidy-up stays separate: it is not a routine and keeps its own settings.

## Cost and budget

Organization page → **Artificial intelligence**, "Cost and budget" section (or
`PUT …/organizations/{org}/ai` with `price_input`, `price_output`, `monthly_budget`).

- **Price**: in € per million tokens, input and output. Empty: Synaptane applies the model's
  public price when it knows it (Claude models), in dollars counted as euros; the cost shown
  is therefore an estimate. With no price and no known model, no cost is computed. Prices
  entered apply to every model of the organization, including those chosen by its spaces.
- Input read from the provider's cache counts for a tenth of the input price (a twentieth for
  `claude-opus-5-5`, a fortieth for `claude-fable-5-1`), input written to the cache for 1.25
  times the input price (Anthropic's price for the 5-minute cache, the only one Synaptane
  requests). With Anthropic, Synaptane turns caching on: each turn rereads the previous one
  at a lower cost.
- **Subscriptions** (Claude Code, Codex): no usage cost, and no budget to set.
- **Monthly budget** (optional): sum of the month's estimated costs (UTC) across all the
  organization's spaces: tidy-up runs, routine runs and semantic indexing (see below). The
  organization's administrators get an email at 80% then at 100% (if email sending is
  configured), and the `tidy.budget` webhook is sent. At 100%, runs are refused until the
  next month; raising the budget starts them again. A run in progress stops as soon as its
  cost would exhaust the budget (work done is kept), and chained runs stop as soon as the
  budget is reached.
- **Providers billed per use** (Anthropic, OpenAI, Mistral, or an OpenAI-compatible API with
  a key): the form suggests a budget of €20 a month when the provider is first set up, and
  as long as no budget is set, the AI page shows a warning: spending has no limit. This does
  not apply to subscriptions, Ollama or an OpenAI-compatible API without a key.
- **A budget requires a known price** for the organization's model **and** for the model
  chosen by each space (public price for Claude models, or prices entered by the admin).
  Otherwise the budget is refused, and once a budget is set, a space cannot pick a model
  without a known price. If a model loses its price later (for example after an update), each
  run with it is counted at the highest known public price, never zero, and the run's log
  says so.
- **Semantic indexing** counts toward the month's spending: tokens reported by the provider,
  or overestimated (one token per three characters) when it does not report them, at the
  public prices of the OpenAI, Gemini, Mistral and Voyage AI models (the highest of them for
  a model it does not know). Ollama and OpenAI-compatible APIs are not counted, nor is the
  vectorization of search queries. Indexing is not stopped by the budget.
- **Usage limits**: **Tidy now** allows 5 manual runs per person and per space over 24 hours
  (20 for an organization admin); scheduled and as-you-go runs are not affected. An account
  drops at most 120 notes per hour into inboxes, across all spaces and interfaces (web, REST,
  MCP), notes moved to another space included. Beyond that, the request is refused (HTTP 429,
  `too_many_requests`).
- **Undo this run** (in the tidy-up log, administrators) creates a reverse commit; the items
  return to the inbox. It fails once a page of the run was changed since.
- **Undo some pages…** (contributors) lists the pages the run changed, each with its diff, and
  puts the ones you tick back as they were before the run, in one commit under your name. A
  page changed since the run is refused: restore it from its history. A note the run filed
  returns to the inbox once all the pages it went into are undone; before that, the next run
  would write it again into the pages kept.

## Semantic search (embeddings, optional)

By default, search works by words (grep-style). In spaces where it is enabled, semantic
search also finds pages **close in meaning**: a question in English about a page in French,
or a paraphrase with no word in common.

1. **Organization page → Semantic search**: choose a provider.

   | Provider | Default model | Notes |
   |---|---|---|
   | Ollama (local, no key) | `bge-m3` | Multilingual; content does not leave the server. `ollama pull bge-m3`. |
   | OpenAI | `text-embedding-3-small` | API key. |
   | Google Gemini | `gemini-embedding-001` | Google AI Studio API key (multilingual, 3,072 dimensions). Without billing enabled, the free tier applies and Google may use the content sent. |
   | Mistral | `mistral-embed` | API key. |
   | Voyage AI | `voyage-3.5` | API key. |
   | OpenAI-compatible | to enter | Any `/v1/embeddings` API. |

   The page explains, for each provider, where and how to create the key.

   For French (or any non-English content), prefer a **multilingual** model.
   `nomic-embed-text` (Ollama), for example, is poor at telling relevant pages from the
   others.
2. The space's **Settings → Semantic search**: enable it. The index is built in the
   background; the page shows the number of files and chunks indexed.

How it works:
- **Chunking**: pages are split by section, with the title and headings. Text extracted from
  PDFs and Office documents is indexed too.
- **Updates**: the index is updated on every change. Changing the model rebuilds it entirely;
  `synaptane reindex` rebuilds it from Git.
- **Fusion**: word and meaning results are merged (RRF). A space without embeddings is not
  at a disadvantage in a search across several spaces.
- **Meaning filter**: a page found *only* by meaning is suggested only if it clearly stands
  out from the other neighbors. A question unrelated to the space therefore returns nothing.
  In a space of similar pages (call transcripts, tickets), even a relevant question rarely
  stands out: set a **minimal similarity** on the organization's Semantic search page. The
  best page must then reach it instead (between 0 and 1, e.g. 0.55 with `voyage-4-large`).
  Its scale depends on the model: measure it on questions about the space and on unrelated
  ones. It is cleared when the model changes.
- **Provider outage**: word search remains available.
- **Caller control**: `mode=text` (REST, MCP) forces word-only search.
- **Related pages** ("See also" panel, MCP tool `related`): with embeddings, a page's vector
  is the average of its chunks, and each other page is judged by its closest chunk, with
  similarity normalized to the space's median (so the threshold does not depend on the
  model). Without an ANN index, this computation scans all the space's chunks (0.4 s for
  80,000 chunks of 1,024 dimensions), with a 4 s timeout, then it is kept for an hour as long
  as the page does not change; past the timeout, related pages are computed from words and
  links only. Without embeddings, they are computed with no call to the provider.
  Computation and caches: `src/synaptane/services/related.py`.
