---
name: youtube-translate
description: Given YouTube links (videos, channels, playlists) or local folders/files (subtitles or media), produce per-language translated SRT subtitle files (default en, uk, de, fr, es, pl) ready to upload to YouTube, plus accuracy QA. Works with any OpenAI-compatible endpoint, or with no endpoint at all — the calling model does the translation itself. Not for dubbing/voice-over, video editing, or titles/thumbnails.
---
# YouTube / media → translated subtitles

Everything is in `scripts/`. Requirements: `python3` (stdlib only) and `yt-dlp` on PATH for YouTube
inputs. Translation is done either by an OpenAI-compatible endpoint **or by you, the calling model**.
Local media without subtitles is transcribed only if an ASR tool is installed (`faster-whisper`,
`mlx_whisper`, or the `whisper` CLI); otherwise that item is marked `needs-asr` and the run continues.

## Inputs
Pass any mix of:
- YouTube **video**, **channel** (`https://youtube.com/@Handle`, `/videos`, `/streams`), or **playlist** URLs. `--latest N` = newest N per channel/playlist (0 = all).
- Local **folders or files**: `.srt`, `.vtt`, YouTube `.json3`, or media (`.mp4/.mkv/.mov/.webm/.mp3/.m4a/.wav`). A sidecar subtitle with the same stem is used when present; otherwise media is transcribed (if an ASR tool exists).

`--langs` sets target codes (default `en,uk,de,fr,es,pl`); the source language is skipped automatically.

## Output (per item, stable across re-runs)
```
<out>/<source>/<YYYYMMDD>_<slug>_<id>/
  video.json               metadata, cues, status
  original-script.md       full original transcript in paragraphs
  original.<src>.srt       timed original captions
  translations/<lang>.srt  one SRT per target language, same cues/timings
  translations/<lang>.md   readable transcript in that language (same paragraph breaks as original-script.md)
```

## Mode A — you have an OpenAI-compatible endpoint
```bash
export YT_TX_BASE_URL=http://<host>:<port>/v1 YT_TX_API_KEY=<key> YT_TX_MODEL=<served-model>
python3 scripts/yt_translate.py run <inputs...> --langs en,uk,de --out ./out --concurrency 8
```
Re-running is safe: finished `<lang>.srt` files are skipped and every chunk is cached under `<video>/.cache/`.

## Mode B — no endpoint: you translate (agent mode)
1. **prepare** — ingest and write numbered work items (no network model needed for translation):
   ```bash
   python3 scripts/yt_translate.py prepare <inputs...> --langs en,uk,de --out ./out
   ```
   This writes, per video and language, chunks at `<video>/.work/<lang>/<n>.in.json`:
   ```json
   {"_meta": {"source_language": "...", "target_language": "...", ...},
    "1": "first source line", "2": "second source line", ...}
   ```
2. **translate each work item yourself.** For every `<n>.in.json`:
   - Read it. Translate each numbered value from `_meta.source_language` into `_meta.target_language`.
   - Lines are consecutive speech fragments; a sentence may span lines — translate so they read naturally in order.
   - Write `<n>.out.json` **next to it**: the **same numbered keys** (no `_meta`), each value a **non-empty** translated string.
   - Fan this out with subagents or parallel calls if your harness supports it — the items are independent.
3. **check what's left** (optional): `python3 scripts/yt_translate.py status --out ./out` lists pending items.
4. **assemble** — validate every `.out.json` and write the SRTs:
   ```bash
   python3 scripts/yt_translate.py assemble --out ./out
   ```
   Any item with missing/extra keys or empty values is reported as `invalid`; incomplete languages are
   reported and skipped. Fix those `.out.json` files and run `assemble` again.

## Accuracy QA
```bash
# structural only (always available, no model):
python3 scripts/qa.py run ./out --sample 0
# with an endpoint GEMBA-MQM judge (use a DIFFERENT model than the translator if you can):
export YT_QA_BASE_URL=... YT_QA_MODEL=... YT_QA_API_KEY=...
python3 scripts/qa.py run ./out --sample 40 --review en,uk
# agent-mode judge (no endpoint): prepare -> fill qa/.judge/*/1.out.json -> assemble
python3 scripts/qa.py prepare ./out --sample 40
python3 scripts/qa.py assemble ./out
```
The judge follows **GEMBA-MQM** (Kocmi & Federmann, 2023): per line it lists errors by category
(accuracy/mistranslation, omission, addition, terminology, fluency/grammar) × severity (critical 25 /
major 5 / minor 1). Line score = 100 − penalties, clipped at 0. Outputs: `qa/report.md`, `qa/flags.jsonl`,
`qa/judge.jsonl`, `qa/review-*.md`.

## Upload to YouTube
YouTube Studio → Subtitles → the video → Add language → Upload file → "With timing" → `translations/<lang>.srt`.

## Known failures
- Members-only/private videos fail in yt-dlp ("Join this channel…") and are logged; the rest continue.
- Videos with captions disabled (or music only) end up `no-captions`.
- Dubbed videos expose several `*-orig` ASR tracks; only the declared-language one is the true original.
- Some videos need cookies: set `YT_DLP_COOKIES_FROM_BROWSER` (e.g. `chrome`, `brave:Profile 1`) or `YT_DLP_COOKIES` (a cookies.txt path).
