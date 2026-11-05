#!/usr/bin/env python3
"""Accuracy check for yt_translate.py output. Two layers, no human reference required.

1. Structural checks (every cue, every language, no model): cue count/timing parity with the
   original, empty or untranslated cues, wrong script, Russian leakage in Ukrainian,
   length-ratio outliers, repeated-line loops. Leads, not verdicts.

2. GEMBA-MQM judge (optional, on a sample): an LLM grader marks each line's errors by category
   (accuracy/mistranslation, accuracy/omission, accuracy/addition, terminology, fluency/grammar)
   and severity (critical/major/minor). Per-line score = 100 - weighted penalty, clipped at 0
   (critical 25, major 5, minor 1). See GEMBA-MQM (Kocmi & Federmann, 2023, WMT23).
   Use a DIFFERENT model than the translator (--judge-model / YT_QA_*) to avoid self-grading bias.

Subcommands:
  run <out>        structural + endpoint judge (if YT_QA_*/YT_TX_* set and --sample>0) + report
  prepare <out>    structural + write judge work items (agent mode, no endpoint)
  assemble <out>   read judge output items and write the report

Writes <out>/qa/report.md, flags.jsonl, judge.jsonl, review-*.md.
"""
import argparse, json, os, random, re, sys, threading, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPTS = {"latin": r"[A-Za-zÀ-ɏ]", "cyrillic": r"[Ѐ-ӿ]", "arabic": r"[؀-ۿݐ-ݿ]",
           "cjk": r"[一-鿿぀-ヿ가-힯]", "hebrew": r"[֐-׿]", "greek": r"[Ͱ-Ͽ]",
           "georgian": r"[Ⴀ-ჿ]", "devanagari": r"[ऀ-ॿ]"}
LANG_SCRIPT = {"uk": "cyrillic", "ru": "cyrillic", "be": "cyrillic", "kk": "cyrillic", "bg": "cyrillic", "sr": "cyrillic",
               "ar": "arabic", "fa": "arabic", "ur": "arabic", "zh-Hans": "cjk", "zh-Hant": "cjk", "ja": "cjk", "ko": "cjk",
               "he": "hebrew", "el": "greek", "ka": "georgian", "hi": "devanagari"}
LETTER = re.compile(r"[^\W\d_]")
SEVERITY = {"critical": 25, "major": 5, "minor": 1}
CATEGORIES = ["accuracy/mistranslation", "accuracy/omission", "accuracy/addition",
              "terminology", "fluency/grammar", "other"]


def parse_srt(p: Path):
    out = []
    for block in re.split(r"\n\s*\n", p.read_text(encoding="utf-8").strip()):
        lines = block.splitlines()
        ti = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if ti is not None:
            out.append((lines[ti].split("-->")[0].strip(), " ".join(lines[ti + 1:]).strip()))
    return out


def script_share(text, script):
    letters = LETTER.findall(text)
    return sum(1 for ch in letters if re.match(SCRIPTS[script], ch)) / len(letters) if letters else 1.0


def structural(src_cues, tgt_cues, lang):
    flags = []
    if len(src_cues) != len(tgt_cues):
        return [(None, "cue-count", f"{len(tgt_cues)} vs {len(src_cues)} original")]
    expect = LANG_SCRIPT.get(lang, "latin")
    for i, ((st, s), (tt, t)) in enumerate(zip(src_cues, tgt_cues), 1):
        if st != tt:
            flags.append((i, "timing", f"{tt} != {st}"))
        if not t:
            flags.append((i, "empty", ""))
            continue
        if t == s and len(s) > 12:
            flags.append((i, "untranslated", t))
        if len(LETTER.findall(t)) >= 4 and script_share(t, expect) < 0.6:
            flags.append((i, "wrong-script", t))
        if lang == "uk" and re.search(r"[ыэъёЫЭЪЁ]", t):
            flags.append((i, "russian-leak", t))
        if len(s) >= 25 and not (0.35 <= len(t) / len(s) <= 2.6):
            flags.append((i, "length-ratio", f"{len(t) / len(s):.2f}: {t}"))
        if i >= 3 and t == tgt_cues[i - 2][1] == tgt_cues[i - 3][1]:
            flags.append((i, "repeat-loop", t))
    return flags


# --------------------------------------------------------------------------- #
# GEMBA-MQM scoring
# --------------------------------------------------------------------------- #
def score_from_errors(errors):
    if not isinstance(errors, list):
        return 100, []
    clean = []
    for e in errors:
        if isinstance(e, dict) and str(e.get("severity", "")).lower() in SEVERITY:
            clean.append({"category": str(e.get("category", "other")).lower(),
                          "severity": str(e["severity"]).lower(), "note": str(e.get("note", ""))})
    penalty = sum(SEVERITY[e["severity"]] for e in clean)
    return max(0, 100 - penalty), clean


JUDGE = """GEMBA-MQM (Kocmi & Federmann, 2023). You are a professional translation quality annotator.
For each numbered line, list the translation errors in the {lang} translation of the {src} source line.
Each error is an object: {{"category": <one of accuracy/mistranslation, accuracy/omission, accuracy/addition, terminology, fluency/grammar, other>, "severity": <critical|major|minor>, "note": "<short English note>"}}.
Severity: critical = meaning destroyed or dangerous; major = clear error changing meaning; minor = style/small slip.
Lines are consecutive speech fragments, so a sentence may span lines; judge meaning in context.
If a line is fully correct, return an empty list [] for it.
Return ONLY JSON mapping each id to its list of error objects (empty list if no error).

{payload}"""


class Judge:
    def __init__(self, base, key, model):
        self.base, self.key, self.model, self.extras = base.rstrip("/"), key, model, True

    def score(self, src, lang, pairs):
        """pairs: list of (id, (source, translation)) -> {id: errors_list}."""
        payload = json.dumps({str(i): {"source": s, "translation": t} for i, (s, t) in pairs}, ensure_ascii=False, indent=0)
        msg = JUDGE.format(src=src, lang=lang, payload=payload)
        last = None
        for attempt in range(4):
            body = {"model": self.model, "temperature": 0, "max_tokens": 8192, "messages": [{"role": "user", "content": msg}]}
            if self.extras:
                body["response_format"] = {"type": "json_object"}
                body["chat_template_kwargs"] = {"thinking": False, "enable_thinking": False}
            req = urllib.request.Request(self.base + "/chat/completions", data=json.dumps(body).encode(),
                                         headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
            try:
                txt = json.loads(urllib.request.urlopen(req, timeout=600).read())["choices"][0]["message"]["content"]
                out = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
                res = {i: (out.get(str(i)) if isinstance(out.get(str(i)), list) else []) for i, _ in pairs}
                if sum(1 for v in res.values() if v is not None) >= 0.9 * len(pairs):
                    return res
                last = "too few ids scored"
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}"
                if e.code == 400 and self.extras:
                    self.extras = False
                    continue
            except Exception as e:
                last = repr(e)[:200]
            time.sleep(2 * (attempt + 1))
        raise RuntimeError(last)


# --------------------------------------------------------------------------- #
def load_videos(root):
    videos = []
    for orig in sorted(root.glob("*/*/original.*.srt")):
        vdir = orig.parent
        tr = {p.stem: parse_srt(p) for p in sorted((vdir / "translations").glob("*.srt"))}
        if tr:
            meta = json.loads((vdir / "video.json").read_text()) if (vdir / "video.json").exists() else {}
            videos.append({"dir": vdir, "src": orig.name.split(".")[1], "orig": parse_srt(orig), "tr": tr, "meta": meta})
    if not videos:
        sys.exit(f"no translated videos under {root}")
    return videos


def run_structural(videos, qa):
    stats, flag_rows = {}, []
    for v in videos:
        for lang, cues in v["tr"].items():
            fl = structural(v["orig"], cues, lang)
            s = stats.setdefault(lang, {"videos": 0, "cues": 0, "flags": {}, "judged": [], "cats": {}})
            s["videos"] += 1
            s["cues"] += len(cues)
            for i, kind, note in fl:
                s["flags"][kind] = s["flags"].get(kind, 0) + 1
                flag_rows.append({"video": v["dir"].name, "lang": lang, "cue": i, "kind": kind, "note": note})
    with open(qa / "flags.jsonl", "w") as f:
        for r in flag_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return stats


def judge_windows(videos, sample, rng):
    """Assign a consecutive window per video, return jobs [(v, lang, [(id,(src,tgt))...])]."""
    jobs = []
    for v in videos:
        n = len(v["orig"])
        start = rng.randrange(0, max(1, n - sample + 1))
        idx = list(range(start + 1, min(n, start + sample) + 1))
        v["window"] = idx
        for lang, cues in v["tr"].items():
            if len(cues) == n:
                jobs.append((v, lang, [(i, (v["orig"][i - 1][1], cues[i - 1][1])) for i in idx]))
    return jobs


def record_judge(qa, judged, stats, videos):
    (qa / "judge.jsonl").unlink(missing_ok=True)
    byname = {v["dir"].name: v for v in videos}
    with open(qa / "judge.jsonl", "w") as f:
        for (vname, lang), res in judged.items():
            v = byname[vname]
            for i, (sc, errs) in sorted(res.items()):
                stats[lang]["judged"].append(sc)
                for e in errs:
                    cat = e["category"]
                    stats[lang]["cats"][cat] = stats[lang]["cats"].get(cat, 0) + 1
                f.write(json.dumps({"video": vname, "lang": lang, "cue": i, "score": sc, "errors": errs,
                                    "source": v["orig"][i - 1][1], "translation": v["tr"][lang][i - 1][1]},
                                   ensure_ascii=False) + "\n")


def write_report(root, qa, videos, stats, judged, a):
    kinds = sorted({k for s in stats.values() for k in s["flags"]})
    L = [f"# Translation QA — {root.name}", "",
         f"{len(videos)} videos. GEMBA-MQM judge: `{a.judge_model or 'none'}` "
         f"({a.sample} consecutive cues per video x language). Score = 100 - penalties "
         f"(critical 25 / major 5 / minor 1), clipped at 0.", "",
         "| lang | videos | cues | judged | mean score | lines < 70 | " + " | ".join(kinds) + " |",
         "|---|---|---|---|---|---|" + "---|" * len(kinds)]
    for lang, s in sorted(stats.items()):
        j = s["judged"]
        mean = f"{sum(j) / len(j):.1f}" if j else "–"
        low = f"{100 * sum(1 for x in j if x < 70) / len(j):.1f}%" if j else "–"
        L.append(f"| {lang} | {s['videos']} | {s['cues']} | {len(j)} | {mean} | {low} | " +
                 " | ".join(str(s["flags"].get(k, 0)) for k in kinds) + " |")
    L += ["", "GEMBA-MQM error categories from judge:", ""]
    for lang, s in sorted(stats.items()):
        cats = ", ".join(f"{k} {v}" for k, v in sorted(s["cats"].items(), key=lambda kv: -kv[1]))
        L.append(f"- **{lang}**: {cats or '–'}")
    worst = sorted((sc, vn, lg, i, "; ".join(e["note"] or e["category"] for e in errs))
                   for (vn, lg), res in judged.items() for i, (sc, errs) in res.items())[:25]
    if worst:
        L += ["", "## Lowest-scored lines", "", "| score | lang | video | cue | errors |", "|---|---|---|---|---|"]
        L += [f"| {sc:.0f} | {lg} | {vn} | {i} | {note.replace('|', '/')} |" for sc, vn, lg, i, note in worst]
    (qa / "report.md").write_text("\n".join(L) + "\n", encoding="utf-8")

    la, lb = (a.review.split(",") + [""])[:2]
    rng = random.Random(a.seed)
    R = [f"# Side-by-side review: {la} vs {lb}", "",
         "Check that both columns say the same thing as each other and the original. Scores are the GEMBA-MQM judge's.", ""]
    for v in videos:
        if la not in v["tr"] or lb not in v["tr"]:
            continue
        n = len(v["orig"])
        sa = {i: sc for i, (sc, _) in judged.get((v["dir"].name, la), {}).items()}
        sb = {i: sc for i, (sc, _) in judged.get((v["dir"].name, lb), {}).items()}
        worst_ids = sorted(set(sa) | set(sb), key=lambda i: min(sa.get(i, 100), sb.get(i, 100)))[: a.review_lines // 2]
        pool = [i for i in range(1, n + 1) if i not in worst_ids]
        ids = sorted(set(worst_ids) | set(rng.sample(pool, min(len(pool), a.review_lines - len(worst_ids)))))
        title = v["meta"].get("title_orig") or v["dir"].name
        R += [f"## {title}", f"`{v['dir'].relative_to(root)}`", "",
              f"| # | time | {v['src']} (original) | {la} | {lb} | score {la}/{lb} |", "|---|---|---|---|---|---|"]
        for i in ids:
            cell = lambda s: s.replace("|", "/")
            sc = (f"{sa[i]:.0f}" if i in sa else "–", f"{sb[i]:.0f}" if i in sb else "–")
            R.append(f"| {i} | {v['orig'][i - 1][0][:8]} | {cell(v['orig'][i - 1][1])} | {cell(v['tr'][la][i - 1][1])} | "
                     f"{cell(v['tr'][lb][i - 1][1])} | {sc[0]}/{sc[1]} |")
        R.append("")
    if la and lb:
        (qa / f"review-{la}-{lb}.md").write_text("\n".join(R), encoding="utf-8")
    print((qa / "report.md").read_text())


# --------------------------------------------------------------------------- #
def cmd_run(a):
    root = a.out.expanduser().resolve()
    qa = root / "qa"
    qa.mkdir(exist_ok=True)
    videos = load_videos(root)
    stats = run_structural(videos, qa)
    judged = {}
    if a.sample and a.judge_model and a.judge_base_url:
        rng = random.Random(a.seed)
        key = os.environ.get("YT_QA_API_KEY") or os.environ.get("YT_TX_API_KEY") or os.environ.get("OPENAI_API_KEY") or "none"
        judge = Judge(a.judge_base_url, key, a.judge_model)
        jobs = judge_windows(videos, a.sample, rng)
        lock = threading.Lock()
        def run(job):
            v, lang, pairs = job
            try:
                raw = judge.score(v["src"], lang, pairs)
            except Exception as e:
                print(f"judge failed {v['dir'].name} {lang}: {e}", file=sys.stderr)
                return
            scored = {i: score_from_errors(errs) for i, errs in raw.items()}
            with lock:
                judged[(v["dir"].name, lang)] = scored
        print(f"judging {len(jobs)} windows x {a.sample} cues with {a.judge_model}", file=sys.stderr)
        with ThreadPoolExecutor(a.concurrency) as ex:
            list(ex.map(run, jobs))
        record_judge(qa, judged, stats, videos)
    write_report(root, qa, videos, stats, judged, a)


def cmd_prepare(a):
    root = a.out.expanduser().resolve()
    qa = root / "qa"
    qa.mkdir(exist_ok=True)
    videos = load_videos(root)
    run_structural(videos, qa)
    rng = random.Random(a.seed)
    jobs = judge_windows(videos, a.sample, rng)
    jroot = qa / ".judge"
    written = 0
    for v, lang, pairs in jobs:
        d = jroot / f"{v['dir'].name}__{lang}"
        d.mkdir(parents=True, exist_ok=True)
        inp = d / "1.in.json"
        if (d / "1.out.json").exists():
            continue
        obj = {"_meta": {"source_lang": v["src"], "target_lang": lang, "protocol": "GEMBA-MQM",
                         "categories": CATEGORIES, "severities": list(SEVERITY),
                         "instructions": "For each id, write a list of error objects {category, severity, note} "
                                         "for the translation vs the source (empty list if correct). "
                                         "Write to 1.out.json: same ids, value = list of error objects."}}
        for i, (s, t) in pairs:
            obj[str(i)] = {"source": s, "translation": t}
        inp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
        written += 1
    print(json.dumps({"judge_items": written, "dir": str(jroot),
                      "next": "write each <name>/1.out.json ({id: [error objects]}), then: qa.py assemble --out " + str(root)},
                     ensure_ascii=False, indent=2))


def cmd_assemble(a):
    root = a.out.expanduser().resolve()
    qa = root / "qa"
    videos = load_videos(root)
    stats = run_structural(videos, qa)
    jroot = qa / ".judge"
    judged, missing = {}, []
    for inp in sorted(jroot.glob("*/1.in.json")) if jroot.is_dir() else []:
        name = inp.parent.name
        vname, lang = name.rsplit("__", 1)
        outp = inp.parent / "1.out.json"
        if not outp.exists():
            missing.append(name)
            continue
        indata = json.loads(inp.read_text())
        try:
            raw = json.loads(outp.read_text())
        except Exception as e:
            missing.append(f"{name} (bad JSON: {repr(e)[:80]})")
            continue
        scored = {}
        for k in indata:
            if k == "_meta":
                continue
            scored[int(k)] = score_from_errors(raw.get(k, []))
        judged[(vname, lang)] = scored
    record_judge(qa, judged, stats, videos)
    write_report(root, qa, videos, stats, judged, a)
    if missing:
        print("pending judge items: " + ", ".join(missing), file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("run", cmd_run), ("prepare", cmd_prepare), ("assemble", cmd_assemble)):
        p = sub.add_parser(name)
        p.add_argument("out", type=Path, help="output root used with yt_translate.py --out")
        p.add_argument("--sample", type=int, default=40, help="consecutive cues judged per video x language (0 = skip judge)")
        p.add_argument("--review", default="en,uk", help="two languages for the side-by-side review sheet")
        p.add_argument("--review-lines", type=int, default=12)
        p.add_argument("--seed", type=int, default=7)
        if name == "run":
            p.add_argument("--judge-base-url", default=os.environ.get("YT_QA_BASE_URL") or os.environ.get("YT_TX_BASE_URL") or os.environ.get("OPENAI_BASE_URL"))
            p.add_argument("--judge-model", default=os.environ.get("YT_QA_MODEL") or os.environ.get("YT_TX_MODEL"))
            p.add_argument("--concurrency", type=int, default=int(os.environ.get("YT_TX_CONCURRENCY", 8)))
        else:
            p.set_defaults(judge_base_url=None, judge_model="agent", concurrency=8)
        p.set_defaults(func=fn)
    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
