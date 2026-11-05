# brain-search

Fast hybrid search in any language, SQL over datasets, and model-judged quality validation for [finite.place](https://finite.chat) brain folders (`raw/`, `wiki/`, `datasets/`, synced locally with `fragment sync`).

## Install
```
uv sync
uv run brain --brain <brain> status   # starts the daemon, embeds every section (cached by content hash)
```
Brains live in `~/brains/<brain>` (`BRAIN_SEARCH_BRAINS`). The daemon listens on 127.0.0.1:8770 (`BRAIN_SEARCH_PORT`), keeps the models loaded, and re-indexes changed files when it gets a query (it checks for changes at most every 2 s). `brain stop` ends it. `BRAIN_SEARCH_DEVICE=cpu` keeps the models off GPUs that serve something else; to run the daemon on another host, tunnel its port (`ssh -L 8770:127.0.0.1:8770 <host>`) and the local `brain` client uses it.

## Search
```
brain search "<question>"                          # hybrid: BM25 + multilingual bge-m3 dense, RRF fusion
brain search "<question>" -q "<same question in another language or phrasing>"   # fused
brain search "..." --deep          # + bge-reranker-v2-m3 cross-encoder (~1 s)
brain search "..." --kind raw,dataset --wiki "<wiki>" --json
```
Results are whole sections with their heading path, page date, finite.place URL and, for wiki pages, the page's cited sources. At most 2 sections per page (`--per-page`). `--recent` boosts sections that mention later years.

Nothing is tied to a language. The keyword index casefolds text, strips diacritics and cuts each word to its first 5 characters (`BRAIN_SEARCH_PREFIX`) in place of per-language stemming. Scripts written without spaces (Chinese, Japanese, Korean, Thai, Lao, Khmer, Myanmar) are indexed as character pairs. The embedding and rerank models cover 100+ languages.

## SQL over datasets
Every `datasets/*.csv|tsv|jsonl` is a DuckDB view named after its file (`-` becomes `_`). Queries can read only files inside the brain folder.
```
brain tables
brain sql "SELECT category, count(*) FROM <dataset> GROUP BY 1 ORDER BY 2 DESC"
```

## Validate data quality
```
uv run brain-validate --brain <brain> --wiki "<wiki folder>" [--subject "<what it is about>"] [--sample 150] [--rows-per-dataset 6] [--judge grounding,relevance,spotcheck,safety]
```
- **Rule-based checks:**
  - frontmatter, and file size (the platform skips files of 1 MiB or more);
  - manifest row counts (`rows:` in the manifest's frontmatter) vs the CSV;
  - rows without a source, and duplicate rows;
  - future or non-ISO dates in date-valued columns;
  - broken wikilinks and sources;
  - email and international phone patterns;
  - whether a sample of cited URLs still loads.
- **Model checks:** run against any OpenAI-compatible endpoint (`JUDGE_URL`; `JUDGE_MODEL` defaults to the endpoint's first model). The judges read every language.
  - wiki claims vs evidence found by BM25 in `raw/` and `datasets/`;
  - relevance of each text excerpt to the subject (default: the wiki's `index.md` title and summary);
  - dataset rows vs the text of the source URL they cite;
  - a safety review that flags private individuals named in connection with prosecutions or donations.
- **Output:** writes `<wiki>/output/quality-report-<date>.md` plus `quality-judgments-*.jsonl` and `quality-flags-*.jsonl`. Model verdicts are cached by prompt hash in `~/.cache/brain-search/judge.sqlite`.

## Measured
A 27-page brain in two languages, with 34 hand-labelled questions (question → expected page/section):

| Engine | Right page in top 3 | MRR | Median latency |
| --- | --- | --- | --- |
| finite `search` operation (hybrid, English-only nomic embeddings) | 0.76 | 0.661 | 397 ms |
| finite browser search box (`/__search`, keyword only) | 0.68 | 0.586 | 190 ms |
| brain-search fast (default) | 0.94 | 0.816 | 20 ms |
| brain-search `--deep` | 0.97 | 0.864 | 953 ms |

The brain-search rows were measured with an earlier keyword index that used per-language stemmers. With the current language-neutral tokenizer, the keyword index on its own scores 0.76 (right page in top 3) vs 0.79 before. Re-run `brain eval` for the combined numbers.

## Evaluate on your own brain
Write a JSONL file, one question per line; `expect` holds substrings of `path#heading` that count as a hit:
```
{"q": "when did the budget change", "expect": ["wiki/budget.md#History"], "also": ["optional extra phrasing"]}
```
Then `brain eval questions.jsonl [--deep]` prints recall@1/3/5/10, MRR, latency and the misses.

## Layout
- `src/brain_search/corpus.py`: splits markdown into heading sections and CSV/JSONL datasets into row chunks
- `src/brain_search/engine.py`: BM25 + dense retrieval, RRF fusion, rerank, recency boost, per-page diversity, embedding cache
- `src/brain_search/server.py`: the long-running local daemon that keeps the models loaded
- `src/brain_search/cli.py`: the `brain` client (standard library only, so it starts instantly)
- `src/brain_search/sql.py`: DuckDB views over the datasets
- `src/brain_search/validate.py`: `brain-validate`
