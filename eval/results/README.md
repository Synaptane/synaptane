English | [Français](README.fr.md)

# Results of the tidy-up bench

This folder holds the published results of the bench that measures the nightly tidy-up
(`tests/eval/`, described in [docs/dev/eval.md](../../docs/dev/eval.md)), to compare models,
efforts and prompts in the open.

- `runs/`: one file per real run, written by the bench itself
  (`synaptane-tidy-bench-run`, format 1): Synaptane's commit, the fingerprints of the bench,
  of each scenario, of the prompt and of the tools, the model and its settings, the price
  applied, and every trial with its graders, tokens, cost and duration.
- `results.json`, `results.md`, `results.fr.md`: the report built from those files by
  `./scripts/eval.sh report` (`synaptane-tidy-bench-results`, format 1). Never edited by hand:
  rebuilt from `runs/`, it gives the same bytes.

No result is published yet: the folder fills up with the first real runs. Nothing here is
invented or simulated; the simulated model of the bench only checks the bench itself and never
writes here.

## How they are produced

1. On a clean checkout of the commit to measure (no modified tracked file), with the
   development PostgreSQL (`docker compose -f compose.dev.yaml up -d`):

   ```sh
   ./scripts/eval.sh tidy --estimate --model claude-sonnet-5-5 --trials 5   # free
   export ANTHROPIC_API_KEY=sk-ant-…
   ./scripts/eval.sh tidy --model claude-sonnet-5-5 --effort medium --trials 5 --budget 4
   ```

   One command per configuration (model, effort, `--prompt FILE`, `--label NAME`), on the same
   commit and with the same number of trials. Each writes its file in `runs/`.
2. `./scripts/eval.sh report`, which refuses incomplete runs, fewer than 3 trials, runs on a
   modified tree, different scenarios, and anything that looks like a secret or a path of the
   machine.
3. Commit `runs/` and the report together.

## How to reproduce them

Check out the commit shown for a configuration (Provenance section of `results.md`), check the
bench's fingerprint (`bench.sha256`) and the prompt's (`config.prompt.sha256`), then run the
same command with your own API key. Expect the same pass rates within their intervals, not the
same numbers: a model does not answer twice the same way, and a model name can point to a
different model later at its provider.

## What it measures, and its limits

Each scenario drops notes in the inbox of a new space, a copy of a fictional wiki of 50 pages
in French (`tests/eval/wikidata.py`, public); the agent runs one pass, then graders written in
code check the outcome (facts filed, scope kept, items left, pages created, injection canaries
not copied, links, index, deletions, text removed). The sample is small (13 short scenarios on
one wiki), the graders check invariants of the result and not the quality of the writing, and
the cost is estimated from the tokens at the public list price, not invoiced. No data from an
instance is read or sent.
