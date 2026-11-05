# research-skills

Three agent skills for researching an organisation, a channel or a person and keeping what you find in a
markdown knowledge base (a [Finite](https://finite.chat) brain, an Obsidian vault, or any folder).

| Skill | What it does |
|---|---|
| [`youtube-translate/`](youtube-translate/) | YouTube links (videos, channels, playlists) or local folders → original transcript + per-language subtitles (`.srt`) and readable text (`.md`), default en, uk, de, fr, es, pl, with accuracy checks. Translates through any OpenAI-compatible endpoint, or the calling model does it itself. |
| [`third-party-writing/`](third-party-writing/) | Seed links → everything *others* (press, NGOs, state media, analysts) wrote about the subject: found, fetched, classified, double-checked by a second model, written up as a navigable markdown wiki. |
| [`brain-search/`](brain-search/) | Fast multilingual hybrid search (BM25 + bge-m3, optional rerank), SQL over the brain's datasets, and rule- plus model-judged quality validation for a local brain folder. |

They fit together: `third-party-writing` and `youtube-translate` fill a brain's `raw/`, `brain-search`
finds things in it and checks its quality.

## Install
Each folder is a self-contained skill (a `SKILL.md` plus its scripts). Copy or symlink the ones you want
into your agent's skills directory, for example:

```bash
git clone https://github.com/0xSero/research-skills
cp -R research-skills/youtube-translate ~/.claude/skills/        # Claude Code
cp -R research-skills/third-party-writing ~/.agents/skills/      # omp / generic agents
```
Requirements per skill are in its `SKILL.md`: `python3` + `yt-dlp` (youtube-translate), `uv` + `yt-dlp`
(third-party-writing), `uv` with Python ≥ 3.12 (brain-search).

## Sources
Snapshots of the standalone repos, which stay where they are:
`youtube-translate` ← [0xSero/youtube-translate-skill](https://github.com/0xSero/youtube-translate-skill) @ `51fa937`,
`third-party-writing` ← [0xSero/third-party-writing](https://github.com/0xSero/third-party-writing) @ `3c23202`,
`brain-search` ← 0xSero/brain-search @ `20cc1cd`.

## License
MIT, see [LICENSE](LICENSE).
