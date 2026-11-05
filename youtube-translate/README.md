# youtube-translate-skill

A self-contained **agent skill**: give it YouTube links (videos, channels, playlists) or local
folders/files, and the calling model produces **translated subtitle files** (`.srt`) ready to upload to
YouTube — in several languages at once (default `en, uk, de, fr, es, pl`).

It needs nothing beyond **`python3` (stdlib only)** + **`yt-dlp`** + **the calling model**. An
OpenAI-compatible endpoint is an optional accelerator, not a requirement: in *agent mode* the model that
runs the skill does the translation itself.

## Install as a skill
Copy the folder into your agent's skills directory:

| Agent | Skills directory |
|---|---|
| Claude Code | `~/.claude/skills/youtube-translate/` |
| Codex | `~/.codex/skills/youtube-translate/` |
| omp | `~/.agents/skills/youtube-translate/` |
| generic | any folder your harness scans for `SKILL.md` |

```bash
git clone https://github.com/<you>/youtube-translate-skill
cp -r youtube-translate-skill ~/.claude/skills/youtube-translate   # adjust path per table
```
Then ask your agent to "translate <YouTube link or folder> into <languages>". `SKILL.md` carries the
full instructions the model follows (both modes and the agent loop).

Standalone (no agent) works too — just call the scripts directly as shown below.

## Requirements
- `python3` ≥ 3.8 (standard library only — no pip installs).
- `yt-dlp` on PATH for YouTube inputs: `pipx install yt-dlp` or `brew install yt-dlp`.
- Optional, for local media without subtitles: one of `faster-whisper`, `mlx_whisper`, or the `whisper` CLI. If none is installed, such items are marked `needs-asr` and skipped (the rest of the run continues).
- Optional: an OpenAI-compatible `/chat/completions` endpoint for Mode A.

## Quickstart

### Mode A — OpenAI-compatible endpoint
```bash
export YT_TX_BASE_URL=http://<host>:<port>/v1 YT_TX_API_KEY=<key> YT_TX_MODEL=<served-model>
python3 scripts/yt_translate.py run \
  "https://www.youtube.com/@SomeChannel" --latest 10 --langs en,uk,de --out ./out --concurrency 8
```
Env falls back to `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL`. The key is never printed.

### Mode B — no endpoint, the model translates (agent mode)
```bash
python3 scripts/yt_translate.py prepare ./my-folder --langs en,uk,de --out ./out
# the agent reads each out/<video>/.work/<lang>/<n>.in.json, writes <n>.out.json beside it
python3 scripts/yt_translate.py status   --out ./out      # what's still pending
python3 scripts/yt_translate.py assemble --out ./out      # validate + write the SRTs
```
Each `.in.json` is a numbered map of source lines plus a `_meta` block naming the source and target
language. The matching `.out.json` must keep the **same numbered keys** (no `_meta`) with **non-empty**
translated strings. See `SKILL.md` for the exact loop.

### Inputs
Mix freely in one call:
- YouTube **video** / **channel** (`@handle`, `/videos`, `/streams`) / **playlist** URLs — `--latest N`.
- Local **folders or files**: `.srt`, `.vtt`, YouTube `.json3`, or media (`.mp4 .mkv .mov .webm .mp3 .m4a .wav`). A sidecar subtitle with the same stem is used if present; otherwise media is transcribed when an ASR tool is installed (`--no-asr` disables transcription; `--asr-model` picks the Whisper size).

## Output layout
```
<out>/<source>/<YYYYMMDD>_<slug>_<id>/
  video.json               metadata, caption track used, cues, status
  original-script.md       full original transcript in paragraphs
  original.<src>.srt       timed original captions (cue-segmented)
  translations/<lang>.srt  one SRT per target language, cue-for-cue aligned with the original
  translations/<lang>.md   readable transcript in that language (same paragraph breaks as original-script.md)
<out>/run-summary.json     per-item status (Mode A)
```

## Upload to YouTube
YouTube Studio → **Subtitles** → pick the video → **Add language** → **Upload file** → **"With timing"**
→ choose `translations/<lang>.srt`. Repeat per language. Review and publish.

## Accuracy QA
There is no human reference translation, so `scripts/qa.py` combines the two checks normally used in that
situation:

- **Structural checks** (every cue, no model): cue count & timing parity with the original, empty or
  untranslated lines, wrong script, Russian-letter leakage in Ukrainian, length-ratio outliers, repeat
  loops. These are **leads, not verdicts** — brand and person names routinely trip the script/letter checks.
- **GEMBA-MQM judge** (optional, on a sample): an LLM annotator marks each line's errors by **category**
  (accuracy/mistranslation, accuracy/omission, accuracy/addition, terminology, fluency/grammar) and
  **severity** (critical / major / minor). The per-line score is **100 − weighted penalties**
  (critical 25, major 5, minor 1), clipped at 0. This follows **GEMBA-MQM** (Kocmi & Federmann, 2023),
  which ranked top for correlation with human judgement at WMT23. Use a **different** model than the
  translator (`YT_QA_*`) — a model grading its own output is biased upward. With no endpoint, run the same
  `prepare` → fill `qa/.judge/*/1.out.json` → `assemble` loop.

```bash
python3 scripts/qa.py run ./out --sample 0                 # structural only
python3 scripts/qa.py run ./out --sample 40 --review en,uk # + endpoint GEMBA-MQM judge
python3 scripts/qa.py prepare ./out --sample 40            # agent-mode judge: write judge items
python3 scripts/qa.py assemble ./out                       # ...then score + report
```
Outputs: `qa/report.md`, `qa/flags.jsonl`, `qa/judge.jsonl`, `qa/review-<a>-<b>.md` (side-by-side sheet,
worst lines first, for a bilingual spot-check).

**Limits of the QA:** the LLM judge is an estimate, not a human verdict; it can miss subtle errors and is
biased toward its own style. Structural flags over-report on names and transliteration. Treat both as
triage that points a human reviewer at the riskiest lines, not as a pass/fail gate.

## Known failures
- **Members-only / private videos**: yt-dlp fails ("Join this channel…"); logged, run continues.
- **No captions**: captions disabled or music-only videos end up `no-captions`.
- **Dubbed `-orig` tracks**: a dubbed video exposes several `*-orig` ASR tracks; only the declared-language
  one is the true original. The others are skipped.
- **Cookie-gated videos**: set `YT_DLP_COOKIES_FROM_BROWSER` (e.g. `chrome`, `brave:Profile 1`) or
  `YT_DLP_COOKIES` (a cookies.txt path) if yt-dlp asks you to "confirm you're not a bot".
- **Local media, no ASR tool**: items without a sidecar subtitle are marked `needs-asr`; install
  `faster-whisper`/`mlx_whisper`/`whisper` or add a `.srt`/`.vtt` with the same stem.

## References
- Tom Kocmi and Christian Federmann. *GEMBA-MQM: Detecting Translation Quality Error Spans with GPT-4.*
  WMT 2023. https://aclanthology.org/2023.wmt-1.64/

## License
MIT — see [LICENSE](LICENSE).
