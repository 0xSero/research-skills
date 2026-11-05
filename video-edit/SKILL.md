---
name: video-edit
description: Clean up a raw screen/OBS/talk recording into a tight professional upload - transcript-driven cuts, dead-air and visual-static removal, off-limits personal moments, on-screen leak redaction, audio enhancement, strong opening, verified render, then chapters/title/tags/description/subtitles/thumbnail, delivered to ~/Downloads and opened. Use when the user says "edit the video I recorded", "cut the silence out", "remove embarrassing parts", "make sure nothing leaks", "make it YouTube ready". Not for building promo/motion-graphics videos from slides; thumbnails alone go to youtube-pack.
---

# Video edit (screen / OBS / talk recording)

Script kit: `scripts/` in this skill (`prep.sh`, `transcript_txt.py`, `ocr_scan.py`,
`cam_flags.py`, `df_enhance.py`, `build_edl.py`, `render.py`, `verify_output.py`,
`chapters.py`, `overview.py`, `job.example.json`, `selftest.sh`). Run them from a copy in
`<workdir>/scripts/` so a job never depends on the skill dir. `scripts/selftest.sh` proves
an install end to end in a couple of minutes.

## Inputs
- `<src>`: the recording, usually newest `~/Desktop/*.mov` (OBS naming `YYYY-MM-DD HH-MM-SS.mov`). If several recent recordings exist, confirm which one BEFORE starting the long pipeline - editing the wrong video wastes an hour.
- `<target_minutes>`: ask if not given; typical 20-30 from a 45-90 min source.
- `<workdir>`: fresh scratch dir, e.g. `/tmp/<session>/edit`.
- Toolchain: `ffmpeg` 6+, `python3`, `mlx_whisper`, DeepFilterNet3, Pillow/numpy/soundfile, Apple Vision (`pyobjc-framework-Vision`/`-Quartz`).

## Steps
1. **Recon, never touch the source.** `ffprobe <src>` (resolution, fps, audio). Work only from copies/extracts in `<workdir>`.
2. **Prep.** `prep.sh "<src>" $W`: `orig48.wav`, `mono16.wav`, `frames/` (1 per 2 s), `camall/` (webcam crop at 1 fps, geometry auto-derived from the resolution), `sheet_overview.jpg`, `silences_raw.txt` (diagnostic only), and `transcript.json` (mlx_whisper large-v3-turbo, word timestamps, ~4 min). Whisper keeps running in the background after prep returns - start `df_enhance.py orig48.wav df_enhanced.wav` (DeepFilterNet3, ~6 min CPU) NOW, it is independent.
3. **Read the whole transcript** (3 chunks via `transcript_txt.py ... --from/--to`). Write keep ranges (source seconds) into `job.json -> keep`. Silence trimming is minor (~2 of 47 min); the real minutes come from content decisions. Whisper self-informs: a phrase repeated 10+ times or empty-segment runs = dead air, cut it.
4. **Visual-static pass.** "Nothing happening" is often visually static, NOT silent. Frame-difference the 0.5 fps frames: <1% pixels changed over a ~10 s window = inactive; merge runs split by <=1 s blips; require >=4 s to cut; 0.5 s handles. Blinking cursors/spinners fake activity - mask or raise the threshold; check intermediate frames for change-and-revert bursts. Typing is noise, not silence: gates use RMS on the *enhanced* track (`pause.loud_db`), never raw `silencedetect`.
5. **Opening.** Cut everything before the first real content. If the start is slow/boring, start on the most interesting demo; optionally cold-open with 1-2 exciting clips then a speed-up of the intro. Never open on wallpaper/black - a choppy first 30 s reads as trash.
6. **Leak scan (runs while you read).** `ocr_scan.py scan` after `run` on `frames/`: IPv4/v6, `*.ts.net`, emails, API keys, ssh material. Dense 2 fps pass on every terminal/config-heavy range (`ocr_scan.py dense src.mov dense 600:60 ...`). Static text -> `boxes` (solid fill); **scrolling terminal output -> blur the whole pane** (`blur` window + `blur_regions`, in source pixels) for the window - per-line boxes miss text between samples. `127.0.0.1`/`localhost` are fine. Log every window in `job.json`.
7. **Privacy cuts (webcam).** `cam_flags.py` (hand-near-mouth via Vision) + `cam_flags.py --sheet S:DUR` zoomed 2 fps sheets of each flagged cluster. Cut the moments the user would not publish: hand at the face during a pause, leaving the desk, another person in frame, full-screen face segments. Ordinary thinking poses and small gestures are fine. NEVER cut mid-sentence for a gesture - snap to the nearest sentence boundary and note what was lost.
8. **User timestamp cuts are literal.** "cut 0000 - 01:12" -> put it in `job.cuts` (or `keep`), apply exactly, then re-check pacing around the seam.
9. **Audio master.** Sanity: enhanced-vs-original correlation > 0.95 (`df_enhance.py` prints it). The default `audio_chain` is highpass 75, cuts/boosts around 220 Hz / 3.2 kHz / 9 kHz, de-esser, 3:1 compressor; notch any persistent fan tone by adding an `equalizer` term; `render.py` does the two-pass loudnorm to -16 LUFS / -1.5 dBTP. The untouched mic stays as audio track 2.
10. **EDL + render.** `build_edl.py` -> `segments.json` (pauses >0.7 s inside keeps shortened to 0.4 s; snapped to the probed frame rate; <0.3 s dropped). `render.py`: grouped parallel `h264_videotoolbox` encodes, concat `-c copy`, mux both tracks. It is restartable per group - if a group fails, fix and rerun, unchanged groups are skipped. Deliver 1080p unless asked otherwise (4K doubles render time for nothing).
11. **Verify before delivering.** `verify_output.py` exits non-zero unless frame count matches the EDL, both audio tracks match the video duration, and OCR of the *output* at 4 fps across every redaction window finds zero leaks. Also pull output frames at each privacy cut and at 0:00-1:00 and LOOK at them. Use `--full` for a whole-file leak sweep when the material is sensitive.
12. **Re-transcribe the new cut.** Transcript, subtitles and chapters must match the OUTPUT timeline, not the source - re-whisper the cut (or `chapters.py`, which remaps through the EDL) before writing SRT/chapters. Never ship SRT times taken from the source transcript.
13. **Metadata + deliver.** Without being asked twice: chapter timestamps (`chapters.py`, >=3, first at 0:00), 2-3 title options, ~30 tags under 500 chars (+2 tag/description variants if asked), description, SRT per language (~42 chars/line; multilingual on request), all as plain text. Thumbnail via youtube-pack: read the user's last 10 thumbnails for style; default white/creme background + black text, no face unless asked, logos/text big and uncluttered. Then move `<output>.mp4` (and SRTs/metadata) to `~/Downloads` and `open` the file / open the folder in default app.

## Outputs
- `<Topic> - YYYY-MM-DD - edit.mp4` (1080p, 2 audio tracks) in `~/Downloads`, opened in the default app.
- `<workdir>`: `job.json`, `segments.json`, `group_bounds.json`, `vf_<n>.txt`, redaction/privacy logs.
- `~/Downloads`: SRTs, `metadata.txt` (titles, description, tags, chapters).
- Optional on request: topical sub-videos split from the same source, each with its own chapters.

## Composes with
`whisper/DFN3 (in-kit) -> video-edit (verified mp4 in ~/Downloads) -> youtube-pack (thumbnail.png) -> browser upload to YouTube Studio configured like the user's other videos`.

## Rules
- Never modify the source recording.
- Target length is a target; if content won't shrink without gutting the talk, deliver slightly over and name the next two cuts.
- Cut whole tangents before trimming more pauses; keep hardware talk, live demos, honest caveats, the recap.
- Always end with: move to ~/Downloads + open.
- Report faithfully: you cannot hear the result - say so and point at track 2.
- Separately recorded audio (Discord/Craig) usually does not exist; check Desktop/Downloads/iCloud/Voice Memos, if absent use the OBS mic and say so.
- When asked to free space: delete old unaccepted drafts/variants, keep the source and the final.
- Multi-person podcasts: find the RIGHT audio tracks (usually 2 of many), map audio to the correct speaker, alternate video with the person talking, clean transitions.
- Never claim the edit is done because the render finished; only `verify_output.py` + eyeballed frames settle it.

## Verify
- `verify_output.py` prints PASS (frame count, two audio tracks, no leaks in windows) and exits 0.
- Eyeballed frames at 0:00-1:00 and every privacy cut (`verify_output.py --sheet 0:60`).
- SRT re-transcription spot-check against the cut.
- `ffprobe` the delivered file: duration, resolution, fps, 2 audio streams.

## Known failures
- Wrong video edited -> confirm newest/topic match before the pipeline.
- Subtitles/chapters on the source timeline -> re-transcribe after cutting (step 12).
- Hostname visible in output -> scrolling text between OCR samples; blur whole pane, widen the window, then output-side OCR.
- Silencedetect cuts typing/video playback -> gate on enhanced-audio RMS (`pause.loud_db`), not silence alone.
- Choppy black opening -> verify output 0:00-1:00 frames, cold-open on real content.
- `cp -i` overwrite prompt / sandbox TCC blocks `~/Downloads` -> use `/bin/cp` with sandbox disabled.
- ffmpeg filter dies ("Cannot allocate memory") past ~100 `between()` terms -> fewer `render_groups`.
- Blinking cursor counts as activity in frame differencing -> mask/tune threshold, check intermediate frames.
- `render.py` frame mismatch -> it refuses to concat; read `render_<n>.log` (`-ss` before a non-keyframe, broken decoder).
- Whisper still running when you read `transcript.json` -> `transcript_txt.py` reports empty segments; wait for `whisper.log` to finish.

## Examples
- 2026-09-02 talk, 47 min -> 26 min: 32 keep ranges, 2 privacy cuts (9:52-9:58, 26:08-26:14), 2 blur windows + 1 box for a `*.ts.net` hostname, render ~2 h wall clock, leak caught only by output-side OCR.
- 2026-09-24 teaching OBS session: user rejected first cut ("background noise, typing, beginning highly boring") -> removed typing noise, picked a better opening, re-did transcripts, then literal head cut `0:00-1:12`, leak scan, white/creme thumbnail with bigger logos, 2 metadata variants, moved to Downloads.
- Podcast (2026-09-18): 1h40m video + 5 candidate audio files -> identified the 2 right per-speaker tracks, mapped voice to video, 1080p, trim head, SRT + ES/AR/HI translations, chapter timestamps, thumbnail with both speakers, text on top, no black background.
