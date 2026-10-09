English | [Français](eval.fr.md)

# Tidy-up and search evaluation bench

Without measurement, every change to the instructions, the model or the ranking is a leap in
the dark. The bench in `tests/eval/` measures the **outcome** of a run (the state of the wiki
afterwards), not the path taken, with graders written in code: free and deterministic.

Run it **by hand** before changing:
- the agent's instructions (`INSTRUCTIONS` in `services/tidy_tools.py`) or its tools;
- the default model or effort;
- the search ranking (`services/search.py`, `services/semantic.py`).

It needs the development PostgreSQL (`docker compose -f compose.dev.yaml up -d`): each run
creates a throwaway database, then drops it.

## Run

```sh
./scripts/eval.sh                       # simulated model, then search: free, ~15 s
./scripts/eval.sh tidy --scenario deplacement --scenario doublon
./scripts/eval.sh search
uv run python -m tests.eval --help      # all options
```

In CI, only the self-check runs (`tests/integration/test_eval_bench.py`): simulated model and
search, never a real model.

### With a real model (paid, never in CI)

```sh
export ANTHROPIC_API_KEY=sk-ant-…        # read from the environment, never as an argument
./scripts/eval.sh tidy --model claude-sonnet-5-5 --effort medium --budget 3 --trials 3 \
  --json results-sonnet-medium.json
```

- `--budget` (in €) is **required**: the bench stops the model as soon as the cumulative cost
  would reach it (the last turn may go slightly over) and skips the remaining scenarios. Cost
  follows the public prices in `ai/pricing.py` (cache writes at 1.25 times the input price);
  for another model, use `--price-input` and `--price-output` (€ per million tokens).
- `--trials 3`: three trials per scenario, to see a model's variance.
- `--provider`: any API key provider (`anthropic`, `openai`, `mistral`, `ollama`,
  `openai_compatible`, with `--base-url` and `--api-key-env`). Subscriptions (Claude Code,
  Codex) are not offered: the bench never touches their credentials.
- Order of magnitude: 13 scenarios of one or two items, from a few cents to a few tens of
  cents each depending on the model; start with `--budget 1`.

**To do: rerun in real mode after the translation of the instructions.** The agent's
instructions are now in English while the bench's wiki and notes stay in French: the bench
must be run again with a real model to check that the agent still files correctly and
writes in French (the language of the wiki's pages):

```sh
./scripts/eval.sh tidy --model claude-sonnet-5-5 --effort medium --budget 1 --trials 1
```

To compare two settings (Sonnet 5.5 at `medium` and at `high`, Opus 5.5 at `medium`…), run
them with the same `--trials` and compare the **cost per successful run** and per filed item,
shown at the end: a cheaper request that fails its scenario saves nothing.

## Read the results

One line per run: `ok` or `ÉCHEC` (failed), the scenario, the mode (`bon` for the good script,
`mauvais` for the bad one, or the model), tokens and cost, then each failing grader with its
explanation. Bench output is in French.

| Grader | Checks |
|---|---|
| `faits` | each key fact of the item appears in a written page (ignoring accents and case) |
| `périmètre` | no file touched outside the paths allowed by the scenario |
| `laissés` | the items expected to stay in the inbox are there, and only those |
| `créations` | the number of pages created (README excluded) is within the expected range |
| `canari` | no trapped text was copied |
| `conservés` | existing facts are still in the wiki |
| `présence` | expected files present or absent (moves, attachments) |
| `suppressions`, `liens`, `isolement`, `forme`, `silence`, `volume` | log metrics (below): deletions, broken links introduced, pages with no incoming link or missing from the index, untitled or empty pages, items filed without a page, text removed |

In simulated mode, each scenario plays a **good** script, which must pass, and a **bad** one,
which must be caught by the intended grader: the bench checks itself. The last line says how
many scenarios are consistent.

### Scenarios

`tests/eval/scenarios.py`, on the fictional wiki in `tests/eval/wikidata.py` (50 pages, eight
folders, index and cross links): a simple note to add, a paraphrased duplicate, a
contradiction ("Auparavant : …", that is "Previously: …", expected; the wiki is in French), an ambiguous question (expected to
be left), injection canaries (tag escape, trapped title and suggestion, trapped diff), a move
that keeps links, two notes on the same topic, a new page, an attachment, a dated task, a
decision.

To add a scenario: a `Note` (or a human change, `edits`), an `Expect`, a good and a bad
script, and the grader that must catch the bad one. The scope (`allowed`) must let a good model
add a backlink or complete a neighbouring index; only canaries have a narrow scope. The CI
self-check verifies the new scenario.

## Per-run metrics (production)

The graders from `suppressions` to `volume` reuse the metrics each run records in production
(`services/tidy_metrics.py`, column `tidy_runs.metrics`, "Quality" block of the log, `metrics`
field of `GET …/tidy/runs`). They are computed on the working view just before the commit,
without AI, and block nothing. What each line means:
[AI and nightly tidy-up](../admin/ai.md), "Quality" block of the log.

Main keys: `pages` (created, modified, moved, deleted, touched), `broken_links` (`before`,
`after`, `introduced`, `examples`), `orphans`, `not_in_index`, `untitled`, `empty`,
`closed_without_page`, `removed_chars`, `deleted_chars`, `reduced` (pages flagged by
`big_changes`), `items` (`done`, `left`, `left_rate`), `usage` (`tokens`,
`cache_write_tokens`, `turns`, `per_item`) and `issues` (points to check). `version` changes
if the meaning of a key changes.

Known limitations: an image (`![…](…)`) does not count as an incoming link; only `README.md`
serves as a folder index.

## Search

36 queries (`tests/eval/search_bench.py`): exact French, without accents, English on French
pages, paraphrases, with the expected pages. Measures: **recall@5** (share of expected pages
in the top 5 results) and **MRR** (mean of 1 / rank of the first expected page), by query
type, with words only, then hybrid with simulated embeddings.

Measured on 6 October 2026:

| Run | fr | no accents | en | paraphrase | all (recall@5 / MRR) |
|---|---|---|---|---|---|
| words | 1.000 | 0.857 | 0.125 | 0.444 | 0.639 / 0.635 |
| hybrid (simulated) | 1.000 | 0.857 | 0.750 | 0.667 | 0.833 / 0.737 |

Takeaways: without embeddings, English finds almost nothing; "coeur" does not find "cœur"
(ligature); hybrid fusion costs exact French queries a little MRR (1.000 → 0.917): an RRF
setting to measure. The simulated embeddings (one axis per concept, synonyms included) check
the fusion and the confidence filter, not the quality of a real embedding model.

## Ideas

- **Model-based grader** (a rubric of 5 criteria scored from 1 to 5: faithfulness, location,
  self-contained page, links, conventions), to calibrate on 10 hand-scored cases.
- **Real embeddings** for the search bench, and real queries taken from assistant activity.
- **Human signals** to track over time: rate of reverted runs, lines written by the agent
  and changed by a human within 7 days, items left then resolved.
- **Claude API**: beta features checked against the API reference but not enabled (each one
  must pass the bench first):
  - *context editing*: `context_management={"edits": [{"type":
    "clear_tool_uses_20250919"}]}` with the `context-management-2025-06-27` beta (parameter
    present in the installed `anthropic` SDK 1.11). Removes old tool results (the
    60,000-character reads) instead of re-reading them from cache on every turn. To measure:
    quality on long runs, effect on the cache (the history is no longer append-only);
  - *task budget*: `output_config={"task_budget": {"type": "tokens", "total": …}}` with the
    `task-budgets-2026-03-13` beta (Sonnet 5.5, Opus 5.5; 20,000 tokens minimum;
    `BetaTokenTaskBudgetParam` type present in the installed SDK, streaming call required): the
    model knows its budget and paces itself, instead of being cut off by the 120-turn cap.
