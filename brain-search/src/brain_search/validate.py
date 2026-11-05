"""Quality and relevance validation for a brain folder.

Deterministic checks (structure, sourcing, links, duplicates, dates, PII patterns, URL liveness) plus
model-judged checks against any OpenAI-compatible endpoint (JUDGE_URL / JUDGE_MODEL):
  - grounding: each factual claim in wiki/ pages vs the best evidence retrieved from raw/ + datasets/
  - relevance: each raw/ section and dataset chunk vs the wiki's subject
  - spot-check: sampled dataset rows vs the text of the source URL they cite
  - safety: private individuals named in prosecution/donation context (at-risk subject rule)
Judgments are cached by prompt hash, so re-runs only pay for what changed.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import html
import io
import json
import os
import random
import re
import sqlite3
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .corpus import iter_files, split_frontmatter
from .engine import UNSPACED

TODAY = dt.date.today()
CACHE = Path(os.environ.get("BRAIN_SEARCH_CACHE", Path.home() / ".cache" / "brain-search"))
JUDGE_URL = os.environ.get("JUDGE_URL", "http://127.0.0.1:8000/v1")
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "")  # empty: the first model the endpoint lists
JUDGE_KEY = os.environ.get("JUDGE_API_KEY", "none")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"

URL_RE = re.compile(r"https?://[^\s\)\]>\"'|,;]+")
WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
PHONE = re.compile(r"(?<![\w+])\+\d[\d\s().-]{7,18}\d(?!\d)")  # international format; street addresses are left to the safety judge
ISO_DATE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")
FULL_DATE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])(-(0[1-9]|[12]\d|3[01]))?$")  # YYYY-MM or YYYY-MM-DD, not ranges like 2026-2036


# ---------------------------------------------------------------- deterministic

def load_pages(root: Path) -> dict[str, dict]:
    pages = {}
    for p in iter_files(root):
        rel = p.relative_to(root).as_posix()
        if p.suffix.lower() != ".md":
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        meta, body = split_frontmatter(text)
        pages[rel] = {"meta": meta, "body": body, "size": p.stat().st_size}
    return pages


def section_of(rel: str) -> str:
    parts = rel.split("/")
    return parts[1] if len(parts) > 2 else "root"


def structural_checks(root: Path, flags: list[dict]) -> dict:
    pages = load_pages(root)
    titles = {str(v["meta"].get("title", "")).strip().lower(): k for k, v in pages.items() if v["meta"].get("title")}
    stems = defaultdict(list)
    for k in pages:
        stems[Path(k).stem.lower()].append(k)
        stems[k[:-3].lower()].append(k)
        stems["/".join(k[:-3].split("/")[1:]).lower()].append(k)
    files = {p.relative_to(root).as_posix() for p in iter_files(root)}

    def resolves(target: str) -> bool:
        t = target.strip().lower().removesuffix(".md")
        return t in titles or t in stems or any(f.lower().endswith(t) or f.lower().endswith(t + ".md") for f in files)

    def flag(path, check, severity, detail):
        flags.append({"path": path, "check": check, "severity": severity, "detail": detail})

    required = {"raw": [("title",), ("source", "resource"), ("captured",)], "wiki": [("title",), ("summary",), ("sources",), ("updated",)]}
    for rel, pg in pages.items():
        sec, meta, body = section_of(rel), pg["meta"], pg["body"]
        if pg["size"] >= 1024 * 1024:
            flag(rel, "size", "error", f"{pg['size']} bytes: the platform skips pages of 1 MiB+, split it")
        elif pg["size"] >= 400 * 1024:
            flag(rel, "size", "warn", f"{pg['size']} bytes (> 400 KB convention)")
        for alts in required.get(sec, []):
            if not any(meta.get(a) for a in alts):
                flag(rel, "frontmatter", "error", f"missing {'/'.join(alts)}")
        if sec == "wiki":
            for s in meta.get("sources") or []:
                m = WIKILINK.search(str(s))
                tgt = m.group(1) if m else str(s)
                if not tgt.startswith("http") and not resolves(tgt):
                    flag(rel, "sources", "error", f"frontmatter source does not resolve: {s}")
        for m in WIKILINK.finditer(body):
            if not resolves(m.group(1)):
                flag(rel, "wikilink", "warn", f"broken [[{m.group(1)}]]")
        for rx, name in ((EMAIL, "email"), (PHONE, "phone")):
            for m in rx.finditer(body):
                ctx = body[max(0, m.start() - 60) : m.end() + 60].replace("\n", " ")
                flag(rel, "pii-pattern", "review", f"{name}: …{ctx}…")
        cap = meta.get("captured") or meta.get("updated")
        if cap and str(cap) > TODAY.isoformat():
            flag(rel, "date", "error", f"date in the future: {cap}")

    # dataset manifests: claimed row counts vs actual
    ds_rows = {}
    for p in iter_files(root):
        rel = p.relative_to(root).as_posix()
        if p.suffix.lower() == ".csv" and "/datasets/" in f"/{rel}":
            ds_rows[rel] = dataset_checks(p, rel, flags)
    for rel, pg in pages.items():
        if "/datasets/" not in f"/{rel}":
            continue
        csv_rel = rel[:-3] + ".csv"
        if csv_rel not in ds_rows:
            if not any(r.startswith(rel[:-3]) for r in files if r != rel):
                flag(rel, "manifest", "warn", "manifest without a data file of the same name")
            continue
        meta_rows = [pg["meta"].get(k) for k in ("rows", "row_count") if str(pg["meta"].get(k, "")).replace(",", "").isdigit()]
        claimed = [int(str(x).replace(",", "")) for x in meta_rows] or [
            int(x.replace(",", "")) for x in re.findall(r"(\d[\d,]*)\s+(?:data\s+)?rows", pg["body"])
        ]
        actual = ds_rows[csv_rel]
        if claimed and actual not in claimed:
            flag(rel, "manifest", "error", f"manifest claims {sorted(set(claimed))} rows, file has {actual}")
    for rel in ds_rows:
        if rel[:-4] + ".md" not in pages:
            flag(rel, "manifest", "error", "dataset has no manifest")
    return {"pages": len(pages), "datasets": ds_rows}


def dataset_checks(p: Path, rel: str, flags: list[dict]) -> int:
    def flag(check, severity, detail):
        flags.append({"path": rel, "check": check, "severity": severity, "detail": detail})

    try:
        rows = list(csv.reader(io.StringIO(p.read_text(encoding="utf-8"))))
    except (UnicodeDecodeError, csv.Error) as e:
        flag("csv", "error", f"unreadable: {e}")
        return 0
    if not rows:
        flag("csv", "error", "empty file")
        return 0
    header, data = rows[0], [r for r in rows[1:] if any(c.strip() for c in r)]
    bad = [i + 2 for i, r in enumerate(data) if len(r) != len(header)]
    if bad:
        flag("csv", "error", f"{len(bad)} rows with wrong column count (first lines {bad[:5]})")
    src_cols = [i for i, h in enumerate(header) if re.search(r"source|url", h, re.I)]
    if not src_cols:
        flag("sourcing", "error", "no source/url column")
    else:
        unsourced = [i + 2 for i, r in enumerate(data) if not any(i2 < len(r) and "http" in r[i2] for i2 in src_cols)]
        if unsourced:
            flag("sourcing", "error" if len(unsourced) > len(data) * 0.05 else "warn", f"{len(unsourced)}/{len(data)} rows without an http source (lines {unsourced[:8]})")
    dupes = [k for k, n in Counter(tuple(r) for r in data).items() if n > 1]
    if dupes:
        flag("duplicates", "warn", f"{len(dupes)} exact duplicate rows")
    for i, h in enumerate(header):
        vals = [r[i].strip() for r in data if i < len(r) and r[i].strip()]
        dated = [v for v in vals if FULL_DATE.match(v)]
        if not vals or len(dated) < len(vals) * 0.5:  # a date column is one whose values are mostly YYYY-MM(-DD)
            continue
        future = [v for v in dated if v[:10] > TODAY.isoformat()]
        if future:
            flag("date", "error", f"{h}: {len(future)} future dates e.g. {future[:3]}")
        odd = [v for v in vals if not ISO_DATE.match(v)]
        if len(odd) > len(vals) * 0.2:
            flag("date", "warn", f"{h}: {len(odd)}/{len(vals)} values not ISO dates e.g. {odd[:3]}")
    return len(data)


def collect_urls(root: Path) -> dict[str, set[str]]:
    urls: dict[str, set[str]] = defaultdict(set)
    for p in iter_files(root):
        rel = p.relative_to(root).as_posix()
        if "/raw/" not in f"/{rel}" and "/datasets/" not in f"/{rel}" and "/wiki/" not in f"/{rel}":
            continue
        for u in URL_RE.findall(p.read_text(encoding="utf-8", errors="replace")):
            u = u.rstrip(".")
            urls[u].add(rel)
    return urls


_ctx = ssl.create_default_context()


def fetch(url: str, timeout: float = 20, max_bytes: int = 3_000_000) -> tuple[int | str, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en,ru;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as r:
            raw = r.read(max_bytes)
            enc = r.headers.get_content_charset() or "utf-8"
            return r.status, raw.decode(enc, errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:  # DNS, TLS, timeout
        return type(e).__name__, ""


def classify(status) -> str:
    if isinstance(status, int) and status < 400:
        return "ok"
    if status in (401, 403, 405, 406, 429, 451, 503, 999, 202):
        return "blocked"
    if status in (404, 410):
        return "dead"
    if status in ("URLError", "gaierror"):
        return "dead"
    return "error"


def url_checks(root: Path, flags: list[dict], workers: int = 32, cap: int = 400) -> dict:
    """Liveness of cited URLs; per-video YouTube links are skipped and the rest sampled to `cap` (deterministic)."""
    urls = {u: f for u, f in collect_urls(root).items() if "youtube.com/watch" not in u and "youtu.be/" not in u}
    if len(urls) > cap:
        keep = set(random.Random(3).sample(sorted(urls), cap))
        urls = {u: f for u, f in urls.items() if u in keep}
    res: dict[str, str] = {}
    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(fetch, u, 15, 65536): u for u in urls}
        for f in as_completed(futs):
            u = futs[f]
            status, _ = f.result()
            res[u] = classify(status)
            if res[u] == "dead":
                for rel in sorted(urls[u]):
                    flags.append({"path": rel, "check": "url", "severity": "error", "detail": f"dead source URL ({status}): {u}"})
    return dict(Counter(res.values()))


# ---------------------------------------------------------------- judge

class Judge:
    def __init__(self, url: str = JUDGE_URL, model: str = JUDGE_MODEL, workers: int = 8):
        self.url, self.workers = url.rstrip("/"), workers
        self.model = model or self._first_model()
        CACHE.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(CACHE / "judge.sqlite", check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS j (key TEXT PRIMARY KEY, model TEXT, out TEXT, at REAL)")
        self.lock = threading.Lock()
        self.calls = self.cached = 0
        self.tokens = 0

    def _first_model(self) -> str:
        req = urllib.request.Request(f"{self.url}/models", headers={"authorization": f"Bearer {JUDGE_KEY}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())["data"][0]["id"]

    def ask(self, system: str, user: str, max_tokens: int = 300) -> dict:
        key = hashlib.sha1(f"{self.model}\n{system}\n{user}".encode()).hexdigest()
        with self.lock:
            row = self.db.execute("SELECT out FROM j WHERE key=?", (key,)).fetchone()
        if row:
            self.cached += 1
            return json.loads(row[0])
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        for attempt in range(3):
            try:
                req = urllib.request.Request(
                    f"{self.url}/chat/completions",
                    data=json.dumps(body).encode(),
                    headers={"content-type": "application/json", "authorization": f"Bearer {JUDGE_KEY}"},
                )
                with urllib.request.urlopen(req, timeout=600) as r:
                    j = json.loads(r.read())
                text = j["choices"][0]["message"].get("content") or ""
                self.tokens += j.get("usage", {}).get("completion_tokens", 0)
                out = parse_json(text)
                break
            except Exception as e:  # transient endpoint failure: retry, then record the failure
                out = {"verdict": "judge_error", "reason": f"{type(e).__name__}: {e}"}
                time.sleep(2 * (attempt + 1))
        if out.get("verdict") != "judge_error":
            with self.lock:
                self.db.execute("INSERT OR REPLACE INTO j VALUES (?,?,?,?)", (key, self.model, json.dumps(out, ensure_ascii=False), time.time()))
                self.db.commit()
        self.calls += 1
        return out

    def map(self, items: list, fn, label: str) -> list:
        out = [None] * len(items)
        done = 0
        t0 = time.time()
        with ThreadPoolExecutor(self.workers) as ex:
            futs = {ex.submit(fn, it): i for i, it in enumerate(items)}
            for f in as_completed(futs):
                out[futs[f]] = f.result()
                done += 1
                if done % 25 == 0 or done == len(items):
                    print(f"  {label}: {done}/{len(items)} ({time.time() - t0:.0f}s, {self.calls} calls, {self.cached} cached)", file=sys.stderr, flush=True)
        return out


def parse_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    v = re.search(r"\b(supported|partially_supported|contradicted|not_found|core|related|off_topic|private_named|ok)\b", text)
    return {"verdict": v.group(1) if v else "unparsed", "reason": text[:300]}


GROUND_SYS = (
    "You check whether a claim from a knowledge-base page is supported by evidence excerpts. Judge ONLY against the excerpts, "
    "not your own knowledge. Numbers, dates, names and roles must match (allow rounding and translation between languages). "
    'Reply with one JSON object: {"verdict": "supported|partially_supported|contradicted|not_found", "evidence": <excerpt number or null>, '
    '"reason": "<one short sentence>"}'
)
SPOT_SYS = (
    "You check a structured data row against the text of the web page it cites as its source. Judge ONLY against the page text. "
    "A row is supported when its key facts (names, dates, amounts, roles, outcomes) appear in the page; partially_supported when some do and none conflict; "
    "contradicted when the page states something different; not_found when the page does not mention them (or the text is empty/blocked). "
    'Reply with one JSON object: {"verdict": "supported|partially_supported|contradicted|not_found", "wrong_fields": [..], "reason": "<short>"}'
)


def relevance_sys(subject: str) -> str:
    return (
        f"You rate how relevant a text chunk is to a knowledge base about: {subject}. "
        "core = directly about the subject, its people, work or activities; related = useful context with a clear link to the subject; "
        "off_topic = no meaningful link, boilerplate, navigation, ads, or garbled text. "
        'Reply with one JSON object: {"verdict": "core|related|off_topic", "quality": "clean|noisy|garbled", "reason": "<short>"}'
    )


def safety_sys(subject: str) -> str:
    return (
        f"The knowledge base is about: {subject}. Privacy rule: never record names or details of PRIVATE individuals (not public figures, "
        "not officials, not publicly listed staff) in connection with prosecution, detention, investigation, donations or support; "
        "no home addresses or personal phones. Public figures, officials, publicly listed staff, journalists and lawyers acting in their public role are fine. "
        'Reply with one JSON object: {"verdict": "ok|private_named", "names": [..], "reason": "<short>"}'
    )


def wiki_subject(root: Path, wiki: str) -> str:
    """The wiki's own description: its index.md title + summary, else the folder name."""
    idx = root / wiki / "index.md"
    if idx.is_file():
        meta, _ = split_frontmatter(idx.read_text(encoding="utf-8", errors="replace"))
        parts = [str(meta[k]) for k in ("title", "summary") if meta.get(k)]
        if parts:
            return " — ".join(parts)
    return wiki


# ---------------------------------------------------------------- claim extraction + evidence

def has_claim_signal(text: str) -> bool:
    """A number, or a capitalised word after the first (a name) in cased scripts; uncased scripts always qualify."""
    if any(c.isdigit() for c in text):
        return True
    words = text.split()
    if not any(c.isupper() or c.islower() for c in text):
        return True
    return any(w[:1].isupper() for w in words[1:])


def claims_of(rel: str, body: str) -> list[dict]:
    """Bullets, table rows and sentences carrying a number/date or a proper name; table header rows skipped."""
    claims, heading = [], ""
    lines = body.splitlines()
    for n, line in enumerate(lines):
        s = line.strip()
        if s.startswith("#"):
            heading = s.lstrip("# ").strip()
            continue
        nxt = lines[n + 1].strip() if n + 1 < len(lines) else ""
        if s.startswith("|") and re.fullmatch(r"\|?[\s:|-]+\|?", nxt or "x"):
            continue  # header row of a markdown table
        if not s or s.startswith(("---", "|---", "| ---", "```")) or "[[" in s and len(re.sub(r"\[\[[^\]]+\]\]", "", s).strip(" -—:")) < 25:
            continue
        units = [s] if s.startswith(("-", "*", "|")) or len(s) < 400 else re.split(r"(?<=[.!?])(?<![A-Z]\.[A-Z]\.)(?<!\b[A-Z]\.)\s+|(?<=[。！？])", s)
        for u in units:
            u = u.strip(" -*|").replace("**", "")
            if len(u) < (8 if UNSPACED.search(u) else 20) or not has_claim_signal(u):
                continue
            claims.append({"path": rel, "heading": heading, "claim": u})
    return claims


class Evidence:
    """Lexical (BM25, language-neutral tokens) evidence index over raw/ and datasets/: CPU only, no model inference."""

    def __init__(self, root: Path):
        import bm25s

        from .corpus import file_sections
        from .engine import tokenize

        self.tokenize = tokenize
        self.sections = [s for p in iter_files(root) for s in file_sections(root, p) if s.kind in ("raw", "dataset")]
        self.bm = bm25s.BM25()
        self.bm.index([tokenize(s.embed_text) or ["∅"] for s in self.sections], show_progress=False)

    def top(self, query: str, wiki: str, k: int = 4, per_page: int = 2) -> list[dict]:
        toks = self.tokenize(query)
        if not toks:
            return []
        scores = self.bm.get_scores(toks)
        out, per = [], Counter()
        for i in scores.argsort()[::-1][:200]:
            s = self.sections[int(i)]
            if scores[i] <= 0 or len(out) == k:
                break
            if s.wiki != wiki or per[s.path] >= per_page:
                continue
            per[s.path] += 1
            out.append({"path": s.path, "heading": s.heading, "text": s.text[:1500]})
        return out


def html_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def excerpt_around(text: str, row: dict, width: int = 3500) -> str:
    """The window of the page with the most hits on the row's distinctive tokens."""
    toks = {t.lower() for v in row.values() if v and not str(v).startswith("http") for t in re.findall(r"\w{4,}", str(v))}
    if not toks or len(text) <= width:
        return text[:width]
    low = text.lower()
    best, best_i = -1, 0
    for i in range(0, max(1, len(low) - width), width // 4):
        win = low[i : i + width]
        score = sum(1 for t in toks if t in win)
        if score > best:
            best, best_i = score, i
    return text[best_i : best_i + width]


# ---------------------------------------------------------------- runner

def run(args) -> None:
    root = Path(args.root).expanduser()
    flags: list[dict] = []
    report: dict = {"brain": args.brain, "root": str(root), "date": TODAY.isoformat()}

    print("structural checks…", file=sys.stderr)
    report["structure"] = structural_checks(root, flags)
    if not args.skip_urls:
        print("URL liveness…", file=sys.stderr)
        report["urls"] = url_checks(root, flags)

    judge = Judge(workers=args.workers) if args.judge else None
    report["judge"] = f"{judge.model} @ {judge.url}" if judge else "none (deterministic checks only)"
    subject = args.subject or wiki_subject(root, args.wiki)
    report["subject"] = subject
    judged: list[dict] = []
    if "grounding" in args.judge:
        claims = []
        for rel, pg in load_pages(root).items():
            if section_of(rel) == "wiki":
                claims += claims_of(rel, pg["body"])
        if args.sample and len(claims) > args.sample:
            claims = random.Random(7).sample(claims, args.sample)
        evidence = Evidence(root)

        def ground(c):
            wiki = c["path"].split("/")[0]
            ev = evidence.top(c["claim"], wiki, k=4)
            ev_txt = "\n\n".join(f"[{i + 1}] ({e['path']} › {e['heading']})\n{e['text']}" for i, e in enumerate(ev)) or "(no evidence found)"
            out = judge.ask(GROUND_SYS, f"Claim (from {c['path']} › {c['heading']}):\n{c['claim']}\n\nEvidence excerpts:\n{ev_txt}")
            n = out.get("evidence")
            best = ev[n - 1]["path"] if isinstance(n, int) and 0 < n <= len(ev) else None
            return {**c, "kind": "grounding", "verdict": out.get("verdict"), "reason": out.get("reason"), "evidence_path": best}

        print(f"grounding {len(claims)} claims…", file=sys.stderr)
        judged += judge.map(claims, ground, "grounding")

    if "relevance" in args.judge or "safety" in args.judge:
        from .corpus import file_sections

        chunks = [s for p in iter_files(root) for s in file_sections(root, p) if s.kind in ("raw", "dataset")]
        if args.sample and len(chunks) > args.sample:
            chunks = random.Random(11).sample(chunks, args.sample)
        if "relevance" in args.judge:
            def rel_fn(s):
                out = judge.ask(relevance_sys(subject), f"Source: {s.path} › {s.heading}\n\n{s.text[:2500]}", 200)
                return {"path": s.path, "heading": s.heading, "kind": "relevance", "verdict": out.get("verdict"), "quality": out.get("quality"), "reason": out.get("reason")}

            print(f"relevance {len(chunks)} chunks…", file=sys.stderr)
            judged += judge.map(chunks, rel_fn, "relevance")
        if "safety" in args.judge:
            risky = chunks  # no keyword prefilter: the judge reads every sampled chunk, in any language

            def safe_fn(s):
                out = judge.ask(safety_sys(subject), f"Source: {s.path} › {s.heading}\n\n{s.text[:3000]}", 250)
                return {"path": s.path, "heading": s.heading, "kind": "safety", "verdict": out.get("verdict"), "names": out.get("names"), "reason": out.get("reason")}

            print(f"safety {len(risky)} chunks…", file=sys.stderr)
            judged += judge.map(risky, safe_fn, "safety")

    if "spotcheck" in args.judge:
        rows = []
        for p in iter_files(root):
            rel = p.relative_to(root).as_posix()
            if p.suffix.lower() != ".csv" or "/datasets/" not in f"/{rel}":
                continue
            data = list(csv.DictReader(io.StringIO(p.read_text(encoding="utf-8"))))
            for r in random.Random(rel).sample(data, min(args.rows_per_dataset, len(data))):
                url = next((v for k, v in r.items() if k and re.search(r"source|url", k, re.I) and v and v.startswith("http")), None)
                if url:
                    rows.append({"path": rel, "row": r, "url": URL_RE.search(url).group(0)})
        pages: dict[str, tuple] = {}
        with ThreadPoolExecutor(16) as ex:
            for u, res in zip({r["url"] for r in rows}, ex.map(lambda u: fetch(u, 25), {r["url"] for r in rows})):
                pages[u] = res

        def spot(r):
            status, body = pages[r["url"]]
            text = html_text(body) if "<" in body[:2000] else body
            if classify(status) != "ok" or len(text) < 200:
                return {"path": r["path"], "kind": "spotcheck", "verdict": "unverifiable", "reason": f"source fetch {status}, {len(text)} chars", "url": r["url"], "row": r["row"]}
            row_txt = "\n".join(f"{k}: {v}" for k, v in r["row"].items() if v)
            out = judge.ask(SPOT_SYS, f"Row from {r['path']}:\n{row_txt}\n\nPage text ({r['url']}):\n{excerpt_around(text, r['row'])}", 250)
            return {"path": r["path"], "kind": "spotcheck", "verdict": out.get("verdict"), "wrong_fields": out.get("wrong_fields"), "reason": out.get("reason"), "url": r["url"], "row": r["row"]}

        print(f"spot-check {len(rows)} rows…", file=sys.stderr)
        judged += judge.map(rows, spot, "spotcheck")

    report["judge_stats"] = {"calls": judge.calls, "cached": judge.cached, "completion_tokens": judge.tokens} if judge else {"calls": 0, "cached": 0, "completion_tokens": 0}
    write_report(root, args, report, flags, judged)


def write_report(root: Path, args, report: dict, flags: list[dict], judged: list[dict]) -> None:
    out_dir = root / args.wiki / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = TODAY.isoformat()
    jl = out_dir / f"quality-judgments-{stamp}.jsonl"
    jl.write_text("".join(json.dumps(j, ensure_ascii=False) + "\n" for j in judged))
    fl = out_dir / f"quality-flags-{stamp}.jsonl"
    fl.write_text("".join(json.dumps(f, ensure_ascii=False) + "\n" for f in flags))

    by_kind: dict[str, Counter] = defaultdict(Counter)
    per_file: dict[str, Counter] = defaultdict(Counter)
    for j in judged:
        by_kind[j["kind"]][j["verdict"]] += 1
        per_file[(j["kind"], j["path"])][j["verdict"]] += 1
    sev = Counter(f["severity"] for f in flags)

    L = [
        "---",
        f'title: "Quality report {stamp}"',
        f'summary: "Validation of sourcing, structure, relevance and claim grounding across the {args.wiki} wiki; judge {report["judge"]}."',
        "tags: [output, quality, validation]",
        f"created: {stamp}",
        f"updated: {stamp}",
        "---",
        "",
        f"# Quality report {stamp}",
        "",
        f"Judge: `{report['judge']}`; evidence retrieval: BM25 (language-neutral tokens) over raw/ and datasets/ of the same wiki, top 4 sections. "
        "Limitation: lexical retrieval misses evidence written in another language, so some `not_found` claims may be supported elsewhere. "
        f"Pages: {report['structure']['pages']}; datasets: {len(report['structure']['datasets'])} "
        f"({sum(report['structure']['datasets'].values())} rows). Judge calls {report['judge_stats']['calls']} (+{report['judge_stats']['cached']} cached).",
        "",
        "## Summary",
        "",
        "| Check | Result |",
        "| --- | --- |",
        f"| Deterministic flags | {', '.join(f'{k} {v}' for k, v in sev.most_common()) or 'none'} |",
    ]
    if "urls" in report:
        L.append(f"| Source URLs | {', '.join(f'{k} {v}' for k, v in sorted(report['urls'].items()))} |")
    for kind, c in by_kind.items():
        n = sum(c.values())
        L.append(f"| {kind} ({n}) | {', '.join(f'{k} {v} ({v / n:.0%})' for k, v in c.most_common())} |")
    L += ["", "## Datasets", "", "| Dataset | Rows |", "| --- | --- |"]
    L += [f"| {k} | {v} |" for k, v in sorted(report["structure"]["datasets"].items())]

    for kind, bad in (("grounding", {"contradicted", "not_found"}), ("spotcheck", {"contradicted", "not_found"})):
        rows = [(p, c) for (k, p), c in per_file.items() if k == kind]
        if not rows:
            continue
        L += ["", f"## {kind.capitalize()} by file", "", "| File | Verdicts | Problem rate |", "| --- | --- | --- |"]
        for p, c in sorted(rows, key=lambda x: -sum(x[1][b] for b in bad) / max(1, sum(x[1].values()))):
            n = sum(c.values())
            L.append(f"| {p} | {', '.join(f'{k} {v}' for k, v in c.most_common())} | {sum(c[b] for b in bad) / n:.0%} |")
    rel_rows = [(p, c) for (k, p), c in per_file.items() if k == "relevance"]
    if rel_rows:
        L += ["", "## Relevance by file", "", "| File | Verdicts |", "| --- | --- |"]
        L += [f"| {p} | {', '.join(f'{k} {v}' for k, v in c.most_common())} |" for p, c in sorted(rel_rows, key=lambda x: -x[1]['off_topic'])]

    def items(kind, verdicts, limit=40):
        sel = [j for j in judged if j["kind"] == kind and j["verdict"] in verdicts][:limit]
        out = []
        for j in sel:
            what = j.get("claim") or j.get("heading") or ""
            if kind == "spotcheck":
                what = "; ".join(f"{k}={v}" for k, v in list(j["row"].items())[:4])
            extra = f" names: {j['names']}" if j.get("names") else ""
            out.append(f"- **{j['verdict']}** `{j['path']}` — {what[:220]} — _{(j.get('reason') or '')[:200]}_{extra}")
        return out

    L += ["", "## Claims to fix (contradicted)", ""] + (items("grounding", {"contradicted"}) or ["none"])
    L += ["", "## Claims without evidence in raw/ or datasets/ (not_found)", ""] + (items("grounding", {"not_found"}, 60) or ["none"])
    L += ["", "## Dataset rows that disagree with their source", ""] + (items("spotcheck", {"contradicted", "not_found"}) or ["none"])
    L += ["", "## Off-topic or garbled chunks", ""] + (items("relevance", {"off_topic"}) or ["none"])
    L += ["", "## Safety review (private individuals)", ""] + (items("safety", {"private_named"}) or ["none"])
    L += ["", "## Deterministic flags (errors and review items)", ""]
    L += [f"- **{f['severity']}** `{f['path']}` {f['check']}: {f['detail'][:240]}" for f in flags if f["severity"] in ("error", "review")] or ["none"]
    L += ["", f"Full records: `output/{jl.name}`, `output/{fl.name}`.", ""]
    md = out_dir / f"quality-report-{stamp}.md"
    md.write_text("\n".join(L))
    print(f"wrote {md}\n      {jl}\n      {fl}")


def main() -> None:
    p = argparse.ArgumentParser(prog="brain-validate", description=__doc__.splitlines()[0])
    p.add_argument("--brain", default=os.environ.get("BRAIN_SEARCH_BRAIN"), required="BRAIN_SEARCH_BRAIN" not in os.environ)
    p.add_argument("--root", default=None, help="brain folder (default ~/brains/<brain>)")
    p.add_argument("--wiki", required=True, help="wiki folder to write the report into (output/)")
    p.add_argument("--subject", help="what the wiki is about, for the relevance/safety judges (default: its index.md title + summary)")
    p.add_argument("--judge", default="grounding,relevance,spotcheck,safety", help="comma list; empty for deterministic only")
    p.add_argument("--sample", type=int, default=0, help="judge at most N items per check (0 = all)")
    p.add_argument("--rows-per-dataset", type=int, default=12)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--skip-urls", action="store_true")
    a = p.parse_args()
    a.root = a.root or str(Path.home() / "brains" / a.brain)
    a.judge = [x for x in a.judge.split(",") if x]
    run(a)


if __name__ == "__main__":
    main()
