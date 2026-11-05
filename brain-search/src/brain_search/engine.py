"""Hybrid search over one brain folder, in any language: BM25 over language-neutral tokens + multilingual dense
embeddings fused by RRF, optional cross-encoder rerank, opt-in recency, per-page diversity, full sections with citations."""

from __future__ import annotations

import math
import os
import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import bm25s
import numpy as np

from .corpus import Section, file_sections, iter_files

EMBED_MODEL = os.environ.get("BRAIN_SEARCH_EMBED", "BAAI/bge-m3")
RERANK_MODEL = os.environ.get("BRAIN_SEARCH_RERANK", "BAAI/bge-reranker-v2-m3")
QUERY_PREFIX = os.environ.get("BRAIN_SEARCH_QUERY_PREFIX", "")
DOC_PREFIX = os.environ.get("BRAIN_SEARCH_DOC_PREFIX", "")
CACHE = Path(os.environ.get("BRAIN_SEARCH_CACHE", Path.home() / ".cache" / "brain-search"))
BRAINS = Path(os.environ.get("BRAIN_SEARCH_BRAINS", Path.home() / "brains"))

RRF_K = 60
POOL = 100  # candidates per retriever per query
RERANK_POOL = int(os.environ.get("BRAIN_SEARCH_RERANK_POOL", "20"))  # fused candidates sent to the cross-encoder
RERANK_MAXLEN = int(os.environ.get("BRAIN_SEARCH_RERANK_MAXLEN", "320"))  # tokens per (query, section) pair
SCAN_EVERY = 2.0  # seconds between change scans
KIND_PRIOR = {"wiki": 0.0, "raw": 0.0, "dataset": 0.0, "output": 0.0, "inventory": -0.05, "index": -0.1, "log": -0.1, "meta": -0.25}
TOKEN = re.compile(r"\w+", re.UNICODE)
# Scripts written without spaces (CJK, kana, hangul, Thai, Lao, Khmer, Myanmar): indexed as character bigrams.
UNSPACED = re.compile(r"[\u0e00-\u0eff\u1000-\u109f\u1780-\u17ff\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]+")
PREFIX = int(os.environ.get("BRAIN_SEARCH_PREFIX", "5"))  # truncation as a language-neutral stand-in for stemming


def tokenize(text: str) -> list[str]:
    """Casefold + strip diacritics, truncate words to PREFIX chars, bigram unspaced scripts. No per-language rules."""
    stripped = "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c))
    folded = unicodedata.normalize("NFC", stripped)  # recompose (e.g. Hangul syllables) after dropping diacritics
    out = []
    for t in TOKEN.findall(folded):
        for part in UNSPACED.split(t):  # the spaced-script pieces of a mixed token
            if len(part) >= 2 or part.isdigit():
                out.append(part if part.isdigit() else part[:PREFIX])
        for run in UNSPACED.findall(t):
            out += [run[i : i + 2] for i in range(len(run) - 1)] or [run]
    return out


class Models:
    """Process-wide models: loaded once, shared by every brain."""

    _lock = threading.Lock()
    _embed = None
    _rerank = None

    @classmethod
    def device(cls) -> str:
        if os.environ.get("BRAIN_SEARCH_DEVICE"):  # e.g. cpu on a host whose GPUs serve something else
            return os.environ["BRAIN_SEARCH_DEVICE"]
        import torch

        return "mps" if torch.backends.mps.is_available() else ("cuda" if torch.cuda.is_available() else "cpu")

    @classmethod
    def embedder(cls):
        with cls._lock:
            if cls._embed is None:
                from sentence_transformers import SentenceTransformer

                cls._embed = SentenceTransformer(EMBED_MODEL, device=cls.device())
                if cls.device() != "cpu":
                    cls._embed.half()
            return cls._embed

    @classmethod
    def reranker(cls):
        with cls._lock:
            if cls._rerank is None:
                from sentence_transformers import CrossEncoder

                cls._rerank = CrossEncoder(RERANK_MODEL, device=cls.device(), max_length=RERANK_MAXLEN)
                if cls.device() != "cpu":
                    cls._rerank.model.half()
            return cls._rerank

    @classmethod
    def embed(cls, texts: list[str], prefix: str) -> np.ndarray:
        m = cls.embedder()
        v = m.encode([prefix + t for t in texts], batch_size=16, normalize_embeddings=True, convert_to_numpy=True)
        return v.astype(np.float32)


@dataclass
class Hit:
    section: Section
    score: float
    signals: list[str]
    rrf: float
    rerank: float | None


class EmbeddingCache:
    """Embeddings keyed by section-text hash, so unchanged sections are never re-embedded."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS emb (model TEXT, hash TEXT, vec BLOB, PRIMARY KEY (model, hash))")

    def get(self, hashes: list[str]) -> dict[str, np.ndarray]:
        out = {}
        for i in range(0, len(hashes), 500):
            part = hashes[i : i + 500]
            q = f"SELECT hash, vec FROM emb WHERE model=? AND hash IN ({','.join('?' * len(part))})"
            for h, blob in self.db.execute(q, [EMBED_MODEL, *part]):
                out[h] = np.frombuffer(blob, dtype=np.float16).astype(np.float32)
        return out

    def put(self, items: dict[str, np.ndarray]) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO emb VALUES (?, ?, ?)",
            [(EMBED_MODEL, h, v.astype(np.float16).tobytes()) for h, v in items.items()],
        )
        self.db.commit()


class Brain:
    def __init__(self, name: str, root: Path | None = None):
        self.name = name
        self.root = (root or BRAINS / name).expanduser()
        if not self.root.is_dir():
            raise FileNotFoundError(f"brain folder not found: {self.root}")
        self.cache = EmbeddingCache(CACHE / name / "embeddings.sqlite")
        self.lock = threading.RLock()
        self.files: dict[str, tuple[float, int]] = {}
        self.by_file: dict[str, list[Section]] = {}
        self.sections: list[Section] = []
        self.matrix = np.zeros((0, 1), dtype=np.float32)
        self.bm25: bm25s.BM25 | None = None
        self.last_scan = 0.0
        self.built_at = 0.0
        self.last_refresh_ms = 0.0

    # ---- indexing ----
    def refresh(self, force: bool = False) -> bool:
        """Re-parse changed files; re-embed only new sections. Returns True if the index changed."""
        with self.lock:
            now = time.time()
            if not force and now - self.last_scan < SCAN_EVERY and self.bm25 is not None:
                return False
            self.last_scan = now
            t0 = time.perf_counter()
            seen: dict[str, tuple[float, int]] = {}
            changed = False
            for p in iter_files(self.root):
                rel = p.relative_to(self.root).as_posix()
                st = p.stat()
                sig = (st.st_mtime, st.st_size)
                seen[rel] = sig
                if self.files.get(rel) != sig:
                    self.by_file[rel] = file_sections(self.root, p)
                    changed = True
            for rel in set(self.files) - set(seen):
                self.by_file.pop(rel, None)
                changed = True
            self.files = seen
            if not changed and self.bm25 is not None:
                return False
            sections = [s for rel in sorted(self.by_file) for s in self.by_file[rel]]
            hashes = [s.hash for s in sections]
            have = self.cache.get(list(set(hashes)))
            missing = [s for s in {s.hash: s for s in sections if s.hash not in have}.values()]
            if missing:
                vecs = Models.embed([s.embed_text for s in missing], DOC_PREFIX)
                fresh = {s.hash: v for s, v in zip(missing, vecs)}
                self.cache.put(fresh)
                have.update(fresh)
            self.sections = sections
            self.matrix = np.stack([have[h] for h in hashes]) if hashes else np.zeros((0, 1), np.float32)
            bm = bm25s.BM25(k1=1.2, b=0.75)
            corpus_tokens = [tokenize(s.embed_text) or ["∅"] for s in sections]
            bm.index(corpus_tokens, show_progress=False)
            self.bm25 = bm
            self.built_at = time.time()
            self.last_refresh_ms = (time.perf_counter() - t0) * 1000
            return True

    # ---- retrieval ----
    def _dense(self, qv: np.ndarray, mask: np.ndarray) -> list[int]:
        scores = self.matrix @ qv
        scores[~mask] = -np.inf
        n = min(POOL, int(mask.sum()))
        if n <= 0:
            return []
        idx = np.argpartition(-scores, n - 1)[:n]
        return [int(i) for i in idx[np.argsort(-scores[idx])] if np.isfinite(scores[i])]

    def _lexical(self, q: str, mask: np.ndarray) -> list[int]:
        toks = tokenize(q)
        if not toks or self.bm25 is None:
            return []
        scores = self.bm25.get_scores(toks)
        scores[~mask] = 0
        n = min(POOL, len(scores))
        idx = np.argpartition(-scores, n - 1)[:n]
        return [int(i) for i in idx[np.argsort(-scores[idx])] if scores[i] > 0]

    def _mask(self, wiki: str | None, kinds: list[str] | None, path: str | None) -> np.ndarray:
        m = np.ones(len(self.sections), dtype=bool)
        for i, s in enumerate(self.sections):
            if (wiki and s.wiki != wiki) or (kinds and s.kind not in kinds) or (path and not s.path.startswith(path)):
                m[i] = False
        return m

    def search(
        self,
        queries: list[str],
        k: int = 8,
        rerank: bool = True,
        wiki: str | None = None,
        kinds: list[str] | None = None,
        path: str | None = None,
        per_page: int = 2,
        recency: bool | None = None,
    ) -> tuple[list[Hit], dict]:
        timings: dict[str, float] = {}
        t = time.perf_counter()
        self.refresh()
        timings["refresh_ms"] = (time.perf_counter() - t) * 1000
        queries = [q.strip() for q in queries if q and q.strip()]
        if not queries or not self.sections:
            return [], timings
        with self.lock:
            mask = self._mask(wiki, kinds, path)
            t = time.perf_counter()
            qvs = Models.embed(queries, QUERY_PREFIX)
            timings["embed_ms"] = (time.perf_counter() - t) * 1000
            t = time.perf_counter()
            fused: dict[int, float] = {}
            signals: dict[int, set[str]] = {}
            for q, qv in zip(queries, qvs):
                for name, ranked in (("semantic", self._dense(qv, mask)), ("lexical", self._lexical(q, mask))):
                    for r, i in enumerate(ranked):
                        fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + r + 1)
                        signals.setdefault(i, set()).add(name)
            timings["retrieve_ms"] = (time.perf_counter() - t) * 1000
            pool = sorted(fused, key=fused.get, reverse=True)[: RERANK_POOL if rerank else max(k * 4, 20)]
            if not pool:
                return [], timings
            sections = [self.sections[i] for i in pool]

        rr: list[float] | None = None
        if rerank:
            t = time.perf_counter()
            ce = Models.reranker()
            pairs = [(q, s.embed_text) for q in queries for s in sections]
            logits = np.asarray(ce.predict(pairs, batch_size=64, show_progress_bar=False), dtype=np.float32)
            logits = logits.reshape(len(queries), len(sections)).max(axis=0)
            rr = [1 / (1 + math.exp(-float(x))) for x in logits]
            timings["rerank_ms"] = (time.perf_counter() - t) * 1000

        recency = bool(recency)
        years = [s.max_year for s in sections if s.max_year]
        y_lo, y_hi = (min(years), max(years)) if years else (0, 0)
        top_rrf = fused[pool[0]]
        hits: list[Hit] = []
        for j, (i, s) in enumerate(zip(pool, sections)):
            base = rr[j] if rr is not None else fused[i] / top_rrf
            score = base + KIND_PRIOR.get(s.kind, 0.0)
            if recency and s.max_year and y_hi > y_lo:
                score += 0.15 * (s.max_year - y_lo) / (y_hi - y_lo)
            hits.append(Hit(s, score, sorted(signals[i]), fused[i], rr[j] if rr is not None else None))
        hits.sort(key=lambda h: h.score, reverse=True)

        out, per = [], {}
        for h in hits:
            if per.get(h.section.path, 0) >= per_page:
                continue
            per[h.section.path] = per.get(h.section.path, 0) + 1
            out.append(h)
            if len(out) == k:
                break
        timings["recency"] = float(recency)
        return out, timings

    def url(self, s: Section) -> str:
        frag = f"#{s.anchor}" if s.anchor else ""
        return f"https://{self.name}.finite.place/#/{quote(s.path)}{frag}"

    def status(self) -> dict:
        self.refresh()
        kinds: dict[str, int] = {}
        for s in self.sections:
            kinds[s.kind] = kinds.get(s.kind, 0) + 1
        return {
            "brain": self.name,
            "root": str(self.root),
            "files": len(self.files),
            "sections": len(self.sections),
            "by_kind": kinds,
            "embed_model": EMBED_MODEL,
            "rerank_model": RERANK_MODEL,
            "built_at": self.built_at,
            "last_refresh_ms": round(self.last_refresh_ms, 1),
        }


def hit_json(brain: Brain, h: Hit, max_chars: int) -> dict:
    s = h.section
    text = s.text if len(s.text) <= max_chars else s.text[:max_chars].rsplit(" ", 1)[0] + " …"
    return {
        "path": s.path,
        "url": brain.url(s),
        "wiki": s.wiki,
        "kind": s.kind,
        "title": s.title,
        "heading": s.heading,
        "ancestry": s.ancestry,
        "date": s.date,
        "latest_year": s.max_year,
        "score": round(h.score, 4),
        "rerank": None if h.rerank is None else round(h.rerank, 4),
        "signals": h.signals,
        "summary": s.summary,
        "sources": s.sources,
        "text": text,
    }
