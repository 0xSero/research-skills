# third-party-writing

An agent skill and CLI (`tpw`) that maps what **other people** wrote about an organisation, channel,
project or person. You give it seed links and context; it does the research.

1. **Entities**: reads the seeds (YouTube channels via yt-dlp, websites, local markdown folders) and has
   an LLM work out the entities, every alias in every script, key people, the subject's own domains,
   search queries in the relevant languages, Wikipedia pages and a topic vocabulary. The plan lands in
   `entities.json` for you to review.
2. **Discover**: Google News RSS (date windows that split automatically when saturated), Bing News,
   GDELT and the citations of the entities' Wikipedia articles.
3. **Triage**: an LLM screens titles and URLs for relevance.
4. **Fetch**: resolves links and extracts article text and metadata.
5. **Analyze**: per article, an LLM records outlet type, stance, article type, an English title and summary,
   claims, topics, event, people, organisations, stated relations and a quote.
6. **Verify**: a *different* model re-judges relevance blind and checks the summary, every claim, every
   relation, the people and the quote against the text. Unconfirmed items are dropped; failures are
   listed, never counted.
7. **Synth**: groups articles into stories (events several outlets covered), writes per-topic common
   claims, disagreements and framing, front-page common threads, and links to an existing knowledge base.
8. **Raw / Build**: captures verified originals into a knowledge base's `raw/`, and writes a navigable
   markdown wiki: an index with aggregate tables, plus pages per article, outlet, topic, story, person,
   entity, a timeline, a connections graph, an unverified list and CSV/JSONL datasets.

Third-party material is **quarantined** by default: the generated `AGENTS.md` and page banners tell agents
not to use it unless explicitly asked, and to read it with suspicion when they do.

## Install

```bash
git clone https://github.com/0xSero/third-party-writing ~/.agents/skills/third-party-writing
ln -s ~/.agents/skills/third-party-writing/scripts/tpw ~/.local/bin/tpw
```

Needs [uv](https://docs.astral.sh/uv/) (dependencies install on first run), and `yt-dlp` for YouTube
seeds. Set an LLM key: `ZAI_API_KEY` (default chain), `DEEPSEEK_API_KEY` or `OPENROUTER_API_KEY`.

## Quick start

```bash
tpw init acme --seed https://www.youtube.com/@Acme --seed https://acme.org \
  --context ~/notes/acme --out ~/notes --wiki-name acme-press --raw-dir ~/notes/acme/raw/3rd-party
tpw entities acme        # review ~/third-party-writing/acme/entities.json
tpw run acme             # discover → triage → fetch → analyze → verify → synth → raw → build
tpw status acme
```

Every step is resumable; rerun any step to pick up where it stopped. See [SKILL.md](SKILL.md) for all
options, the output layout, model configuration and known failure modes.

## License

MIT
