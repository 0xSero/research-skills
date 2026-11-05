---
name: brain-search
description: Search a locally synced finite.place brain (markdown raw/, wiki/, datasets/) in any language with hybrid BM25 + multilingual dense retrieval, run SQL over its CSV/JSONL datasets, and validate its data quality with rule-based and model-judged checks. Use for "find in the brain", "what does the brain say about X", "query the datasets", "check the brain's quality". Not for editing or syncing the brain (use the fragment CLI) or for web research.
---
# brain-search: fast multilingual search, SQL and validation for a brain folder

A Python package in this folder (`uv`, Python ≥ 3.12). Two commands: `brain` (search + SQL client) and
`brain-validate` (quality report). Full details: `README.md`.

## Setup (once)
```bash
cd <this folder> && uv sync
fragment sync <brain> --dir ~/brains/<brain> --mode pull     # brains live in ~/brains/<brain> (BRAIN_SEARCH_BRAINS)
uv run brain --brain <brain> status                          # starts the local daemon, embeds every section (cached)
```
The daemon listens on 127.0.0.1:8770 (`BRAIN_SEARCH_PORT`), keeps the models loaded and re-indexes
changed files on query. `BRAIN_SEARCH_DEVICE=cpu` keeps it off busy GPUs. `uv run brain stop` ends it.

## Search
```bash
uv run brain --brain <brain> search "<question>"                       # hybrid BM25 + bge-m3, RRF fusion, ~20 ms
uv run brain --brain <brain> search "<question>" -q "<same question in another language>"   # fused
uv run brain --brain <brain> search "<question>" --deep                # + cross-encoder rerank (~1 s)
uv run brain --brain <brain> search "..." --kind raw,dataset --wiki "<wiki>" --json
```
Results are whole sections with heading path, page date, finite.place URL and (for wiki pages) cited
sources; at most 2 per page (`--per-page`). `--recent` boosts later years. Read the returned page
before answering from it, and obey the brain's own AGENTS.md (quarantined folders stay unread unless
the person explicitly asks for them).

## SQL over datasets
```bash
uv run brain --brain <brain> tables
uv run brain --brain <brain> sql "SELECT category, count(*) FROM <dataset> GROUP BY 1 ORDER BY 2 DESC"
```
Every `datasets/*.csv|tsv|jsonl` is a DuckDB view named after its file (`-` → `_`); queries can only read
files inside the brain folder.

## Validate
```bash
uv run brain-validate --brain <brain> --wiki "<wiki folder>" [--subject "<what it is about>"] \
  [--sample 150] [--rows-per-dataset 6] [--judge grounding,relevance,spotcheck,safety]
```
Rule checks (frontmatter, 1 MiB limit, manifest row counts, unsourced/duplicate rows, bad dates, broken
links, PII patterns, dead URLs) plus model judges on any OpenAI-compatible endpoint (`JUDGE_URL`,
`JUDGE_MODEL`). Writes `<wiki>/output/quality-report-<date>.md` and `quality-*.jsonl`.

## Measure on your brain
`uv run brain --brain <brain> eval questions.jsonl [--deep]`: one `{"q", "expect": ["path#heading"]}` per
line; prints recall@k, MRR, latency and misses.
