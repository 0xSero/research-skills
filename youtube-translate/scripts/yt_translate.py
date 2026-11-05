#!/usr/bin/env python3
"""YouTube / local media -> translated SRT subtitle files.

Inputs (mixed freely):
  - YouTube video, channel (@handle, /videos, /streams), or playlist URLs
  - local folders or files: .srt .vtt .json3 subtitles, or media (.mp4 .mkv .mov
    .webm .mp3 .m4a .wav). A sidecar subtitle with the same stem is used when
    present; otherwise the media is transcribed if an ASR tool is installed.

Output per source item (kept stable for re-runs and QA):
  <out>/<source>/<YYYYMMDD>_<slug>_<id>/
    video.json               metadata + cues + status
    original-script.md       full original transcript in paragraphs
    original.<src>.srt       timed original captions (cue-segmented)
    translations/<lang>.srt  one SRT per target language, same cues/timings

Subcommands:
  run        translate now through an OpenAI-compatible endpoint (env YT_TX_*)
  prepare    ingest only, then write agent work items (no endpoint needed)
  status     list pending agent work items
  assemble   validate agent output items and write the <lang>.srt files

Deps: python3 stdlib + yt-dlp on PATH. The endpoint is an optional accelerator;
in agent mode the calling model does the translation itself.
"""
import argparse, json, os, re, shutil, subprocess, sys, threading, time, unicodedata
import urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

DEFAULT_LANGS = "en,uk,de,fr,es,pl"
LANG_NAMES = {
    "en": "English", "uk": "Ukrainian", "de": "German", "fr": "French", "es": "Spanish", "pl": "Polish",
    "ru": "Russian", "it": "Italian", "pt": "Portuguese", "pt-BR": "Brazilian Portuguese", "tr": "Turkish",
    "ar": "Arabic", "zh-Hans": "Simplified Chinese", "zh-Hant": "Traditional Chinese", "ja": "Japanese",
    "ko": "Korean", "hi": "Hindi", "id": "Indonesian", "nl": "Dutch", "cs": "Czech", "ro": "Romanian",
    "he": "Hebrew", "ka": "Georgian", "kk": "Kazakh", "be": "Belarusian", "lt": "Lithuanian", "lv": "Latvian",
    "et": "Estonian", "fi": "Finnish", "sv": "Swedish", "vi": "Vietnamese", "fa": "Persian",
}
SUB_EXT = {".srt", ".vtt", ".json3"}
MEDIA_EXT = {".mp4", ".mkv", ".mov", ".webm", ".mp3", ".m4a", ".wav"}

log_lock = threading.Lock()
def log(*a):
    with log_lock:
        print(time.strftime("%H:%M:%S"), *a, flush=True)


def lang_name(code):
    return LANG_NAMES.get(code, LANG_NAMES.get(code.split("-")[0], f"the language with code '{code}'"))


def targets_for(src, langs):
    s = (src or "").split("-")[0]
    return [l for l in langs if l.split("-")[0] != s]


def slugify(s, n=50):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s[:n].rstrip("-") or "item"


# --------------------------------------------------------------------------- #
# subtitle / caption parsing
# --------------------------------------------------------------------------- #
def time_to_ms(s):
    s = s.strip().replace(",", ".")
    parts = [float(x) for x in s.split(":")]
    while len(parts) < 3:
        parts = [0.0] + parts
    h, m, sec = parts
    return int(round((h * 3600 + m * 60 + sec) * 1000))


def parse_timed_sub(p: Path):
    """.srt or .vtt -> [{start,end,text}] (tags stripped)."""
    cues = []
    text = p.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"^WEBVTT.*?(?:\n\n|\Z)", "", text, flags=re.S)
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.splitlines()
        ti = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if ti is None:
            continue
        a, b = lines[ti].split("-->")
        start = time_to_ms(a.split()[0])
        end = time_to_ms(b.split()[0]) if b.split() else start
        body = " ".join(l.strip() for l in lines[ti + 1:] if l.strip())
        body = re.sub(r"<[^>]+>", "", body).strip()
        if body:
            cues.append({"start": start, "end": end, "text": body})
    return cues


def build_cues(json3: Path):
    """YouTube json3 word-timed events -> cues split at sentence ends / ~84 chars / pauses / 7 s."""
    words, ends = [], []
    for ev in json.load(open(json3)).get("events", []):
        if "segs" not in ev or ev.get("aAppend"):
            continue
        t0 = ev["tStartMs"]
        for s in ev["segs"]:
            w = s.get("utf8", "").replace("\n", " ").strip()
            if w:
                words.append((t0 + s.get("tOffsetMs", 0), w))
        ends.append(t0 + ev.get("dDurationMs", 0))
    if not words:
        return []
    last_end = max(ends)
    cues, cur = [], []
    def flush(end_ms):
        if cur:
            cues.append({"start": cur[0][0], "end": end_ms, "text": " ".join(w for _, w in cur)})
            cur.clear()
    for i, (t, w) in enumerate(words):
        nxt = words[i + 1][0] if i + 1 < len(words) else last_end
        cur.append((t, w))
        length = sum(len(x) + 1 for _, x in cur)
        gap = nxt - t
        if (w[-1] in ".!?…" and length >= 30) or length >= 84 or gap > 1500 or nxt - cur[0][0] > 7000:
            flush(min(nxt, t + 1500) if gap > 1500 else nxt)
    flush(last_end)
    for c in cues:
        c["end"] = max(c["end"], c["start"] + 500)
    for a, b in zip(cues, cues[1:]):
        a["end"] = min(a["end"], b["start"])
    return cues


def parse_sub(p: Path):
    return build_cues(p) if p.suffix.lower() == ".json3" else parse_timed_sub(p)


def ts(ms):
    ms = int(ms)
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def write_srt(path: Path, cues, texts):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{i}\n{ts(c['start'])} --> {ts(c['end'])}\n{t}\n\n"
                            for i, (c, t) in enumerate(zip(cues, texts), 1)), encoding="utf-8")


def paragraphs(cues, texts):
    """Group cue texts into paragraphs; breaks come from the ORIGINAL cues so every language splits identically."""
    paras, cur = [], []
    for i, (c, t) in enumerate(zip(cues, texts)):
        cur.append((c["text"], t))
        gap = cues[i + 1]["start"] - c["end"] if i + 1 < len(cues) else 0
        if c["text"][-1] in ".!?…" and (gap > 1200 or sum(len(o) for o, _ in cur) > 900):
            paras.append(" ".join(x for _, x in cur))
            cur = []
    if cur:
        paras.append(" ".join(x for _, x in cur))
    return paras


def write_script(path: Path, info, cues):
    head = [f"# {info.get('title_orig') or info.get('title_en') or info.get('id')}", ""]
    if info.get("url"):
        head.append(f"- URL: {info['url']}")
    head.append(f"- Source: {info.get('source')}")
    if info.get("upload_date") and info["upload_date"] not in ("00000000", "NA", "None", ""):
        head.append(f"- Date: {info['upload_date']}")
    if info.get("duration"):
        head.append(f"- Duration: {int(info['duration']) // 60} min")
    track = info.get("caption_track", "")
    asr = track.endswith("-orig") or track.startswith("asr")
    head.append(f"- Captions: {track or 'n/a'}{' (automatic speech recognition, unedited)' if asr else ''}")
    body = "\n\n".join(paragraphs(cues, [c["text"] for c in cues]))
    path.write_text("\n".join(head) + "\n\n---\n\n" + body + "\n", encoding="utf-8")


def write_translation_md(path: Path, info, cues, texts, lang):
    """Readable transcript in the target language; same paragraph breaks as original-script.md."""
    title = (info.get("title_en") if lang == "en" else info.get("title_orig")) \
        or info.get("title_orig") or info.get("title_en") or info.get("id")
    src = info.get("source_lang", "src")
    head = [f"# {title}", "",
            f"- Language: {lang_name(lang)} (`{lang}`), machine translation of `original-script.md`"]
    if info.get("title_orig"):
        head.append(f"- Original title: {info['title_orig']}")
    if info.get("url"):
        head.append(f"- URL: {info['url']}")
    head.append(f"- Source: {info.get('source')}")
    if info.get("upload_date") and info["upload_date"] not in ("00000000", "NA", "None", ""):
        head.append(f"- Published: {info['upload_date']}")
    head.append(f"- Timed version: `{lang}.srt` (same cues as `original.{src}.srt`)")
    body = "\n\n".join(paragraphs(cues, texts))
    path.write_text("\n".join(head) + "\n\n---\n\n" + body + "\n", encoding="utf-8")


def backfill_translation_mds(vdir: Path, info, cues):
    """Write <lang>.md next to any translations/<lang>.srt that lacks one (cue counts must match)."""
    tdir = vdir / "translations"
    if not tdir.is_dir():
        return
    for srt in sorted(tdir.glob("*.srt")):
        md = srt.with_suffix(".md")
        if md.exists():
            continue
        texts = [c["text"] for c in parse_sub(srt)]
        if len(texts) == len(cues):
            write_translation_md(md, info, cues, texts, srt.stem)


def save_meta(vdir: Path, info):
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "video.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# YouTube ingestion
# --------------------------------------------------------------------------- #
def ytdlp(*args):
    # YouTube may require cookies for some videos; set YT_DLP_COOKIES_FROM_BROWSER
    # (e.g. "chrome", "brave:Profile 1") or YT_DLP_COOKIES (a cookies.txt path).
    extra = []
    if os.environ.get("YT_DLP_COOKIES_FROM_BROWSER"):
        extra = ["--cookies-from-browser", os.environ["YT_DLP_COOKIES_FROM_BROWSER"]]
    elif os.environ.get("YT_DLP_COOKIES"):
        extra = ["--cookies", os.environ["YT_DLP_COOKIES"]]
    r = subprocess.run(["yt-dlp", "--no-warnings", *extra, *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-500:])
    return r.stdout


def source_name(url):
    m = re.search(r"/@([^/?#]+)", url)
    return m.group(1) if m else None


def list_videos(url, n):
    if re.search(r"youtube\.com/@[^/]+/?$", url):
        url = url.rstrip("/") + "/videos"
    args = ["--flat-playlist", "--print", "%(id)s\t%(title)s\t%(duration)s\t%(channel,uploader,playlist_uploader)s"]
    if n:
        args += ["--playlist-end", str(n)]
    vids = []
    for line in ytdlp(*args, url).strip().splitlines():
        vid, title, dur, chan = (line.split("\t") + [""] * 4)[:4]
        vids.append({"id": vid, "title_en": title,
                     "duration": float(dur) if dur not in ("NA", "None", "") else 0,
                     "source": source_name(url) or slugify(chan, 40)})
    return vids


def fetch_captions(vid, tmp: Path, src):
    tmp.mkdir(parents=True, exist_ok=True)
    langs = f"{src},{src}-orig" if src != "auto" else ".*-orig"
    out = ytdlp("-q", "--skip-download", "--no-simulate", "--write-auto-subs", "--write-subs",
                "--sub-langs", langs, "--sub-format", "json3", "-o", str(tmp / "%(id)s.%(ext)s"),
                *(["--extractor-args", f"youtube:lang={src}"] if src != "auto" else []),
                "--print", "%(title)s\t%(upload_date)s\t%(duration)s\t%(language)s",
                f"https://www.youtube.com/watch?v={vid}")
    title, date, dur, lang = out.strip().splitlines()[-1].split("\t")
    info = {"title_orig": title, "upload_date": date, "duration": float(dur) if dur not in ("NA", "None") else 0}
    if src == "auto" and lang not in ("NA", "None", ""):
        ytdlp("-q", "--skip-download", "--no-simulate", "--write-subs", "--sub-langs", f"{lang}(-.*)?",
              "--sub-format", "json3", "-o", str(tmp / "%(id)s.%(ext)s"), f"https://www.youtube.com/watch?v={vid}")
    want = src if src != "auto" else (lang if lang not in ("NA", "None", "") else None)
    files = sorted(tmp.glob(f"{vid}.*.json3"))
    code = lambda f: f.name.split(".")[-2]
    in_lang = lambda c: want is None or c == want or c.startswith(want + "-")
    manual = [f for f in files if not code(f).endswith("-orig") and in_lang(code(f))]
    # only the declared-language *-orig track is the original; dubbed audio adds other *-orig tracks
    orig = [f for f in files if code(f).endswith("-orig") and in_lang(code(f).removesuffix("-orig"))]
    pick = (manual or orig or [None])[0]
    if pick is None:
        return info, None
    track = code(pick)
    info["caption_track"] = track
    info["source_lang"] = want or track.removesuffix("-orig")
    return info, pick


def materialize_youtube(v, a):
    info, j3 = fetch_captions(v["id"], a.out / ".captions" / v["source"], a.source_lang)
    info.update(id=v["id"], title_en=v["title_en"], source=v["source"],
                url=f"https://www.youtube.com/watch?v={v['id']}")
    vdir = a.out / v["source"] / f"{info['upload_date']}_{slugify(v['title_en'])}_{v['id']}"
    info["dir"] = str(vdir)
    if j3 is None:
        info.update(status="no-captions", cues=[], cue_count=0)
        save_meta(vdir, info)
        log(f"SKIP no captions: {v['source']} {v['id']} {v['title_en'][:60]}")
        return info, []
    cues = build_cues(j3)
    return finish_materialize(info, cues, a)


# --------------------------------------------------------------------------- #
# Local ingestion
# --------------------------------------------------------------------------- #
def detect_lang_from_name(p: Path):
    m = re.match(r".*\.([a-z]{2,3}(?:-[A-Za-z]+)?)$", p.stem)
    if m and (m.group(1) in LANG_NAMES or m.group(1).split("-")[0] in LANG_NAMES):
        return m.group(1)
    return None


def collect_local(root):
    root = Path(root).expanduser()
    files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
    media = [f for f in files if f.suffix.lower() in MEDIA_EXT]
    subs = [f for f in files if f.suffix.lower() in SUB_EXT]
    used, items = set(), []
    for m in media:
        side = next((s for s in subs if s.stem == m.stem or s.stem.startswith(m.stem + ".")), None)
        if side:
            used.add(side)
        items.append({"media": m, "sub": side, "stem": m.stem})
    for s in subs:
        if s not in used:
            items.append({"media": None, "sub": s, "stem": re.sub(r"\.[a-z]{2,3}(-[A-Za-z]+)?$", "", s.stem)})
    return items


def asr_available(no_asr):
    if no_asr:
        return None
    import importlib.util as u
    if u.find_spec("faster_whisper"):
        return "faster_whisper"
    if u.find_spec("mlx_whisper") or shutil.which("mlx_whisper"):
        return "mlx_whisper"
    if shutil.which("whisper"):
        return "whisper"
    return None


def transcribe(path: Path, backend, model="tiny"):
    if backend == "faster_whisper":
        from faster_whisper import WhisperModel
        segs, _ = WhisperModel(model, compute_type="int8").transcribe(str(path))
        return [{"start": int(s.start * 1000), "end": int(s.end * 1000), "text": s.text.strip()}
                for s in segs if s.text.strip()]
    if backend == "mlx_whisper":
        import mlx_whisper
        repo = model if "/" in model else f"mlx-community/whisper-{model}"
        r = mlx_whisper.transcribe(str(path), path_or_hf_repo=repo)
        return [{"start": int(s["start"] * 1000), "end": int(s["end"] * 1000), "text": s["text"].strip()}
                for s in r["segments"] if s["text"].strip()]
    if backend == "whisper":
        import glob, tempfile
        d = tempfile.mkdtemp()
        subprocess.run(["whisper", str(path), "--model", model, "--output_format", "json", "--output_dir", d],
                       check=True, capture_output=True)
        j = json.load(open(glob.glob(d + "/*.json")[0]))
        return [{"start": int(s["start"] * 1000), "end": int(s["end"] * 1000), "text": s["text"].strip()}
                for s in j["segments"] if s["text"].strip()]
    raise RuntimeError(f"unknown ASR backend {backend}")


def materialize_local_item(it, a):
    stem, sub, media = it["stem"], it["sub"], it["media"]
    src = a.source_lang if a.source_lang != "auto" else None
    if src is None:
        src = (detect_lang_from_name(sub) if sub else None) or (detect_lang_from_name(media) if media else None) or "und"
    anchor = sub or media
    date = time.strftime("%Y%m%d", time.localtime(anchor.stat().st_mtime))
    info = {"id": slugify(stem), "title_orig": stem, "title_en": stem, "source": it["source"],
            "upload_date": date, "duration": 0, "input": str(anchor), "source_lang": src}
    vdir = a.out / it["source"] / f"{date}_{slugify(stem)}_{slugify(stem)}"
    info["dir"] = str(vdir)
    if sub:
        cues = parse_sub(sub)
        info["caption_track"] = sub.name
        return finish_materialize(info, cues, a)
    backend = asr_available(a.no_asr)
    if media and backend:
        try:
            cues = transcribe(media, backend, a.asr_model)
            info["caption_track"] = f"asr:{backend}:{a.asr_model}"
            return finish_materialize(info, cues, a)
        except Exception as e:
            info.update(status="needs-asr", cues=[], cue_count=0,
                        message=f"ASR backend {backend} failed: {repr(e)[:200]}")
            save_meta(vdir, info)
            log(f"NEEDS-ASR (transcribe failed) {it['source']}/{vdir.name}")
            return info, []
    info.update(status="needs-asr", cues=[], cue_count=0,
                message="no subtitle sidecar and no ASR tool installed (looked for faster-whisper, "
                        "mlx_whisper, whisper). Install one or add a .srt/.vtt sidecar with the same stem.")
    save_meta(vdir, info)
    log(f"NEEDS-ASR {it['source']}/{vdir.name}")
    return info, []


def finish_materialize(info, cues, a):
    vdir = Path(info["dir"])
    if not cues:
        info.update(status="no-captions", cues=[], cue_count=0)
        save_meta(vdir, info)
        return info, []
    src = info["source_lang"]
    info.update(cues=cues, cue_count=len(cues), status="ready")
    (vdir / "translations").mkdir(parents=True, exist_ok=True)
    write_script(vdir / "original-script.md", info, cues)
    write_srt(vdir / f"original.{src}.srt", cues, [c["text"] for c in cues])
    save_meta(vdir, info)
    return info, cues


def materialize(kind, obj, a):
    return materialize_youtube(obj, a) if kind == "yt" else materialize_local_item(obj, a)


# --------------------------------------------------------------------------- #
# gather inputs
# --------------------------------------------------------------------------- #
def is_url(s):
    return bool(re.match(r"https?://", s))


def input_source_name(inp):
    p = Path(inp).expanduser()
    name = p.name if p.is_dir() else (p.parent.name or "local")
    return slugify(name, 40)


def gather(a):
    out, seen = [], set()
    for inp in a.inputs:
        if is_url(inp):
            for v in list_videos(inp, a.latest):
                if v["id"] in seen or (a.max_duration and v["duration"] > a.max_duration * 60):
                    continue
                seen.add(v["id"])
                out.append(("yt", v))
        else:
            if not Path(inp).expanduser().exists():
                sys.exit(f"input not found: {inp}")
            src = input_source_name(inp)
            for it in collect_local(inp):
                it["source"] = src
                out.append(("local", it))
    out.sort(key=lambda t: t[1].get("duration", 0) if t[0] == "yt" else 0)
    return out


# --------------------------------------------------------------------------- #
# endpoint translation (run)
# --------------------------------------------------------------------------- #
PROMPT = """You translate {src} video subtitles into {lang} for upload to YouTube.
{context}The source may be automatic speech recognition: silently fix obvious recognition errors from context.
Rules:
- Return ONLY a JSON object mapping each input key to its {lang} translation. Same keys, same count; never merge or split lines. Every key appears exactly once.
- Each line is a fragment of continuous speech; a sentence may continue on the next line. Translate so the lines read naturally in sequence.
- Use the standard {lang} spelling of names of people, organisations and places.
- Neutral, accurate subtitle style. No notes or commentary.

Input:
{payload}"""


class LLM:
    def __init__(self, base, key, model, context):
        self.base, self.key, self.model, self.context = base.rstrip("/"), key, model, context
        self.extras = True

    def translate(self, src, lang, lines):
        payload = json.dumps({str(i): t for i, t in enumerate(lines, 1)}, ensure_ascii=False, indent=0)
        msg = PROMPT.format(src=lang_name(src), lang=lang_name(lang), payload=payload,
                            context=f"Context: {self.context}\n" if self.context else "")
        last = None
        for attempt in range(5):
            body = {"model": self.model, "temperature": 0.2, "max_tokens": 8192,
                    "messages": [{"role": "user", "content": msg}]}
            if self.extras:
                body["response_format"] = {"type": "json_object"}
                body["chat_template_kwargs"] = {"thinking": False, "enable_thinking": False}
            req = urllib.request.Request(self.base + "/chat/completions", data=json.dumps(body).encode(),
                                         headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
            try:
                j = json.loads(urllib.request.urlopen(req, timeout=600).read())
                txt = j["choices"][0]["message"]["content"].strip()
                txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt)
                out = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
                res = [str(out.get(str(i), "")).strip() for i in range(1, len(lines) + 1)]
                missing = sum(1 for r in res if not r)
                if not missing:
                    return res
                last = f"{missing}/{len(lines)} keys missing"
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}: {e.read()[:200]!r}"
                if e.code == 400 and self.extras:
                    self.extras = False
                    continue
            except Exception as e:
                last = repr(e)[:200]
            time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"{lang}: failed after retries: {last}")


def process_item(kind, obj, a, llm, pool):
    info, cues = materialize(kind, obj, a)
    vdir = Path(info["dir"])
    if not cues:
        return info
    src = info["source_lang"]
    chunks = [cues[i:i + a.chunk] for i in range(0, len(cues), a.chunk)]

    def do_chunk(lang, n, chunk):
        cache = vdir / ".cache" / lang / f"{n:04d}.json"
        if cache.exists():
            return json.loads(cache.read_text())
        res = llm.translate(src, lang, [c["text"] for c in chunk])
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(res, ensure_ascii=False))
        return res

    targets = targets_for(src, a.langs)
    futs = {l: [pool.submit(do_chunk, l, n, ch) for n, ch in enumerate(chunks)] for l in targets
            if not (vdir / "translations" / f"{l}.srt").exists()}
    failed = []
    for lang, fl in futs.items():
        try:
            texts = [t for f in fl for t in f.result()]
            write_srt(vdir / "translations" / f"{lang}.srt", cues, texts)
            write_translation_md(vdir / "translations" / f"{lang}.md", info, cues, texts, lang)
        except Exception as e:
            failed.append(lang)
            log(f"FAIL {vdir.name} {lang}: {e}")
    backfill_translation_mds(vdir, info, cues)
    info.update(status="partial" if failed else "done", failed_langs=failed, target_langs=targets)
    save_meta(vdir, info)
    log(f"{info['status'].upper()} {info['source']}/{vdir.name} cues={len(cues)} langs={len(targets)}")
    return info


def cmd_run(a):
    if not a.model:
        sys.exit("set --model or YT_TX_MODEL / OPENAI_MODEL")
    key = os.environ.get("YT_TX_API_KEY") or os.environ.get("OPENAI_API_KEY") or "none"
    llm = LLM(a.base_url, key, a.model, a.context)
    items = gather(a)
    log(f"{len(items)} items, langs={a.langs}, model={a.model}, c={a.concurrency}")
    pool = ThreadPoolExecutor(a.concurrency)
    results = []
    with ThreadPoolExecutor(6) as vp:
        futs = {vp.submit(process_item, k, o, a, llm, pool): o for k, o in items}
        for f in as_completed(futs):
            try:
                results.append(f.result())
            except Exception as e:
                o = futs[f]
                log(f"ERROR {o.get('id') or o.get('stem')}: {e}")
                results.append({**{k: o.get(k) for k in ("id", "source")}, "status": "error", "error": str(e)})
    pool.shutdown()
    (a.out / "run-summary.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    counts = {}
    for r in results:
        counts[r.get("status")] = counts.get(r.get("status"), 0) + 1
    log("ALL DONE", counts, "->", a.out)
    sys.exit(0 if counts.get("done", 0) == len(results) else 3)


# --------------------------------------------------------------------------- #
# agent mode: prepare / status / assemble
# --------------------------------------------------------------------------- #
def numkey(p: Path):
    m = re.match(r"(\d+)", p.name)
    return int(m.group(1)) if m else 0


def write_work(vdir: Path, info, langs, chunk):
    cues, src = info["cues"], info["source_lang"]
    targets, written = targets_for(src, langs), []
    for lang in targets:
        if (vdir / "translations" / f"{lang}.srt").exists():
            continue
        wdir = vdir / ".work" / lang
        wdir.mkdir(parents=True, exist_ok=True)
        meta = {"source_lang": src, "source_language": lang_name(src),
                "target_lang": lang, "target_language": lang_name(lang), "video": vdir.name,
                "instructions": f"Translate each numbered line value from {lang_name(src)} into "
                                f"{lang_name(lang)}. Keep every numbered key, same count, non-empty strings. "
                                f"Lines are consecutive speech fragments; a sentence may span lines. "
                                f"Write the result to the matching .out.json next to this file: same keys, "
                                f"no _meta, each value the translated string."}
        for n, start in enumerate(range(0, len(cues), chunk), 1):
            inp = wdir / f"{n}.in.json"
            outp = wdir / f"{n}.out.json"
            if outp.exists():
                continue
            obj = {"_meta": {**meta, "chunk": n}}
            for idx in range(start, min(start + chunk, len(cues))):
                obj[str(idx + 1)] = cues[idx]["text"]
            inp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
            written.append(inp)
    return targets, written


def cmd_prepare(a):
    items = gather(a)
    infos = []
    with ThreadPoolExecutor(6) as vp:
        for f in as_completed([vp.submit(materialize, k, o, a) for k, o in items]):
            infos.append(f.result())
    prepared, work_items, skipped = 0, 0, []
    for info, cues in infos:
        vdir = Path(info["dir"])
        if not cues:
            skipped.append((info["source"], vdir.name, info["status"], info.get("message", "")))
            continue
        targets, written = write_work(vdir, info, a.langs, a.chunk)
        info.update(target_langs=targets, status="prepared")
        save_meta(vdir, info)
        prepared += 1
        work_items += len(written)
    log(f"PREPARED {prepared} items, {work_items} new work files -> {a.out}")
    for s, name, st, msg in skipped:
        log(f"  skipped {s}/{name}: {st} {('- ' + msg) if msg else ''}")
    print(json.dumps({"prepared": prepared, "work_items": work_items,
                      "skipped": [{"source": s, "video": n, "status": st, "message": m} for s, n, st, m in skipped],
                      "out": str(a.out),
                      "next": "translate every <video>/.work/<lang>/<n>.in.json into <n>.out.json, "
                              "then: yt_translate.py assemble --out " + str(a.out)}, ensure_ascii=False, indent=2))


def validate_out(inp: Path, outp: Path):
    """-> (ok, data_or_reason). data maps id -> translated string."""
    expected = {k for k in json.loads(inp.read_text()).keys() if k != "_meta"}
    if not outp.exists():
        return False, "missing .out.json"
    try:
        raw = json.loads(outp.read_text())
    except Exception as e:
        return False, f"invalid JSON: {repr(e)[:120]}"
    if not isinstance(raw, dict):
        return False, "out.json is not an object"
    got = {k for k in raw.keys() if k != "_meta"}
    if got != expected:
        miss, extra = expected - got, got - expected
        return False, f"key mismatch (missing {sorted(miss)[:5]}, extra {sorted(extra)[:5]})"
    data = {}
    for k in expected:
        v = raw[k]
        if not isinstance(v, str) or not v.strip():
            return False, f"empty/non-string value for key {k}"
        data[k] = v.strip()
    return True, data


def scan_work(out: Path):
    for meta in sorted(out.glob("*/*/video.json")):
        try:
            info = json.loads(meta.read_text())
        except Exception:
            continue
        work = meta.parent / ".work"
        if work.is_dir():
            yield meta.parent, info, work


def cmd_status(a):
    pending, ready, rows = 0, 0, []
    for vdir, info, work in scan_work(a.out):
        for ld in sorted(p for p in work.iterdir() if p.is_dir()):
            for inp in sorted(ld.glob("*.in.json"), key=numkey):
                outp = inp.with_name(inp.name[:-len(".in.json")] + ".out.json")
                ok, _ = validate_out(inp, outp)
                if ok:
                    ready += 1
                else:
                    pending += 1
                    rows.append({"item": str(inp.relative_to(a.out)), "lang": ld.name})
    print(json.dumps({"pending": pending, "done": ready,
                      "pending_items": rows[:200]}, ensure_ascii=False, indent=2))
    sys.exit(0 if pending == 0 else 3)


def cmd_assemble(a):
    assembled, invalid, incomplete = [], [], []
    for vdir, info, work in scan_work(a.out):
        cues = info.get("cues") or parse_sub(next(vdir.glob("original.*.srt")))
        done_langs = list(info.get("failed_langs", []))
        for ld in sorted(p for p in work.iterdir() if p.is_dir()):
            lang = ld.name
            merged, bad, missing = {}, [], False
            for inp in sorted(ld.glob("*.in.json"), key=numkey):
                outp = inp.with_name(inp.name[:-len(".in.json")] + ".out.json")
                ok, res = validate_out(inp, outp)
                if ok:
                    merged.update(res)
                else:
                    if res == "missing .out.json":
                        missing = True
                    else:
                        bad.append({"item": str(inp.relative_to(a.out)), "reason": res})
            need = set(str(i) for i in range(1, len(cues) + 1))
            if bad:
                invalid.extend(bad)
            if missing or merged.keys() != need:
                if not bad:
                    incomplete.append({"lang": lang, "video": vdir.name,
                                       "have": len(merged), "need": len(need)})
                continue
            texts = [merged[str(i)] for i in range(1, len(cues) + 1)]
            write_srt(vdir / "translations" / f"{lang}.srt", cues, texts)
            write_translation_md(vdir / "translations" / f"{lang}.md", info, cues, texts, lang)
            assembled.append({"lang": lang, "video": vdir.name, "cues": len(cues)})
            done_langs.append(lang)
        backfill_translation_mds(vdir, info, cues)
        info["status"] = "done" if info.get("target_langs") and \
            all((vdir / "translations" / f"{l}.srt").exists() for l in info["target_langs"]) else "partial"
        save_meta(vdir, info)
    print(json.dumps({"assembled": assembled, "invalid": invalid, "incomplete": incomplete},
                     ensure_ascii=False, indent=2))
    sys.exit(0 if not invalid and not incomplete else 3)


# --------------------------------------------------------------------------- #
def add_ingest_args(p):
    p.add_argument("inputs", nargs="+", help="YouTube URLs and/or local folders/files")
    p.add_argument("--langs", default=DEFAULT_LANGS, help=f"target language codes (default {DEFAULT_LANGS})")
    p.add_argument("--source-lang", default="auto", help="original language code; auto = detect")
    p.add_argument("--out", type=Path, default=Path("yt-translations"))
    p.add_argument("--context", default="", help="one line about the material; improves names/terminology")
    p.add_argument("--chunk", type=int, default=50, help="cues per translation request / work item")
    p.add_argument("--latest", type=int, default=10, help="newest N videos per channel/playlist (0 = all)")
    p.add_argument("--max-duration", type=float, default=0, help="skip videos longer than N minutes (0 = no limit)")
    p.add_argument("--no-asr", action="store_true", help="never transcribe local media; mark it needs-asr")
    p.add_argument("--asr-model", default="tiny", help="Whisper model size/repo for local media (default tiny)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="translate now via an OpenAI-compatible endpoint")
    add_ingest_args(run)
    run.add_argument("--base-url", default=os.environ.get("YT_TX_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1")
    run.add_argument("--model", default=os.environ.get("YT_TX_MODEL") or os.environ.get("OPENAI_MODEL"))
    run.add_argument("--concurrency", type=int, default=int(os.environ.get("YT_TX_CONCURRENCY", 8)))
    run.set_defaults(func=cmd_run)

    prep = sub.add_parser("prepare", help="ingest and write agent work items (no endpoint)")
    add_ingest_args(prep)
    prep.set_defaults(func=cmd_prepare)

    st = sub.add_parser("status", help="list pending agent work items under --out")
    st.add_argument("--out", type=Path, default=Path("yt-translations"))
    st.set_defaults(func=cmd_status)

    asm = sub.add_parser("assemble", help="validate agent output items and write the SRTs")
    asm.add_argument("--out", type=Path, default=Path("yt-translations"))
    asm.set_defaults(func=cmd_assemble)

    a = ap.parse_args()
    a.out = a.out.expanduser().resolve()
    if hasattr(a, "langs"):
        a.langs = [l.strip() for l in a.langs.split(",") if l.strip()]
    a.func(a)


if __name__ == "__main__":
    main()
