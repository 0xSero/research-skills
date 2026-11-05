"""Turn a brain folder (markdown wikis + CSV/JSONL datasets) into searchable sections."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^(```|~~~)")
YEAR = re.compile(r"\b(19[89]\d|20[0-4]\d)\b")
MIN_CHARS = 60  # only near-empty sections merge into their neighbour
MAX_CHARS = 1800  # sections longer than this split at paragraph boundaries
ROWS_PER_CHUNK = 15
SKIP_DIRS = {".git", "node_modules", "site", "applib", ".obsidian"}


@dataclass
class Section:
    path: str  # relative to the brain root, posix
    wiki: str  # top-level folder ("" for root files)
    kind: str  # wiki | raw | dataset | index | log | inventory | output | meta
    title: str
    heading: str
    ancestry: list[str]
    anchor: str
    text: str
    ordinal: int
    date: str | None = None  # page-level date (updated/captured/created)
    max_year: int | None = None  # latest year mentioned in the section
    sources: list[str] = field(default_factory=list)
    summary: str | None = None

    @property
    def id(self) -> str:
        return f"{self.path}#{self.ordinal}"

    @property
    def embed_text(self) -> str:
        crumbs = " > ".join([self.title, *self.ancestry[1:], self.heading] if self.heading else [self.title])
        return f"{crumbs}\n{self.text}"

    @property
    def hash(self) -> str:
        return hashlib.sha1(self.embed_text.encode()).hexdigest()


def slugify(heading: str) -> str:
    s = re.sub(r"[^\w\s-]", "", heading.lower(), flags=re.UNICODE)
    return re.sub(r"[\s]+", "-", s.strip())


def kind_of(rel: Path) -> str:
    parts = rel.parts
    if len(parts) == 1:
        return "meta"
    sub = parts[1] if len(parts) > 2 else ""
    name = parts[-1]
    if sub in {"wiki", "raw", "inventory", "output", "datasets"}:
        return "dataset" if sub == "datasets" else sub
    if name == "index.md":
        return "index"
    if name == "log.md":
        return "log"
    return "meta"


def split_frontmatter(text: str) -> tuple[dict, str]:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            try:
                meta = yaml.safe_load(text[3:end]) or {}
            except yaml.YAMLError:
                meta = {}
            if isinstance(meta, dict):
                return meta, text[end + 4 :].lstrip("\n")
    return {}, text


def _str(v) -> str | None:
    return None if v is None else str(v)


def _years(text: str) -> int | None:
    ys = [int(y) for y in YEAR.findall(text)]
    return max(ys) if ys else None


def _split_long(text: str) -> list[str]:
    if len(text) <= MAX_CHARS:
        return [text]
    paras = re.split(r"\n\s*\n|\n(?=[-*|] )", text)
    out, cur = [], ""
    for p in paras:
        while len(p) > MAX_CHARS:  # one giant paragraph: hard-wrap at sentence/space
            cut = max(p.rfind(". ", 0, MAX_CHARS), p.rfind(" ", 0, MAX_CHARS))
            cut = cut + 1 if cut > MAX_CHARS // 2 else MAX_CHARS
            if cur:
                out.append(cur)
                cur = ""
            out.append(p[:cut].strip())
            p = p[cut:].strip()
        if cur and len(cur) + len(p) + 2 > MAX_CHARS:
            out.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        out.append(cur)
    return [c for c in out if c.strip()]


def markdown_sections(rel: str, wiki: str, kind: str, raw: str) -> list[Section]:
    meta, body = split_frontmatter(raw)
    title = _str(meta.get("title")) or Path(rel).stem
    date = _str(meta.get("updated") or meta.get("captured") or meta.get("created"))
    sources = meta.get("sources") or ([meta["source"]] if meta.get("source") else [])
    sources = [str(s) for s in sources] if isinstance(sources, list) else [str(sources)]
    summary = _str(meta.get("summary") or meta.get("description"))

    # (heading, ancestry above it, text) per heading block; text before any heading has heading "".
    triples: list[tuple[str, list[str], str]] = []
    stack: list[tuple[int, str]] = []
    heading, anc, lines = "", [], []
    in_fence = False
    for line in body.splitlines():
        if FENCE.match(line.strip()):
            in_fence = not in_fence
        m = None if in_fence else HEADING.match(line)
        if not m:
            lines.append(line)
            continue
        triples.append((heading, anc, "\n".join(lines).strip()))
        level, heading = len(m.group(1)), m.group(2).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        anc = [h for _, h in stack]
        stack.append((level, heading))
        lines = []
    triples.append((heading, anc, "\n".join(lines).strip()))

    # Heading-only blocks vanish (their children carry them in ancestry); tiny blocks join the
    # previous section (or prefix the next one at page start) under their own sub-heading.
    merged: list[tuple[str, list[str], str]] = []
    prefix = ""
    for heading, anc, text in triples:
        if not text:
            continue
        if len(text) < MIN_CHARS and merged:
            h, a, t = merged[-1]
            merged[-1] = (h, a, f"{t}\n\n### {heading}\n{text}" if heading else f"{t}\n\n{text}")
            continue
        if len(text) < MIN_CHARS:
            prefix = f"{prefix}\n\n{text}".strip()
            continue
        merged.append((heading, anc, f"{prefix}\n\n{text}".strip() if prefix else text))
        prefix = ""
    if prefix:
        merged.append(("", [], prefix))

    out: list[Section] = []
    for heading, anc, text in merged:
        for part in _split_long(text):
            out.append(
                Section(
                    path=rel,
                    wiki=wiki,
                    kind=kind,
                    title=title,
                    heading=heading,
                    ancestry=[title, *(anc[1:] if anc and anc[0] == title else anc)],
                    anchor=slugify(heading) if heading else "",
                    text=part,
                    ordinal=len(out),
                    date=date,
                    max_year=_years(f"{heading}\n{part}"),
                    sources=sources,
                    summary=summary,
                )
            )
    return out


def _row_line(header: list[str], row: list[str]) -> str:
    return "; ".join(f"{h}: {v}" for h, v in zip(header, row) if v not in ("", None))


def table_sections(rel: str, wiki: str, rows: list[list[str]], header: list[str], title: str) -> list[Section]:
    out: list[Section] = []
    for i in range(0, len(rows), ROWS_PER_CHUNK):
        chunk = rows[i : i + ROWS_PER_CHUNK]
        text = "\n".join(_row_line(header, r) for r in chunk)
        out.append(
            Section(
                path=rel,
                wiki=wiki,
                kind="dataset",
                title=title,
                heading=f"rows {i + 1}-{i + len(chunk)}",
                ancestry=[title],
                anchor="",
                text=text,
                ordinal=len(out),
                max_year=_years(text),
            )
        )
    return out


def file_sections(root: Path, path: Path) -> list[Section]:
    rel = path.relative_to(root)
    rel_s = rel.as_posix()
    wiki = rel.parts[0] if len(rel.parts) > 1 else ""
    try:
        raw = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    suffix = path.suffix.lower()
    if suffix == ".md":
        return markdown_sections(rel_s, wiki, kind_of(rel), raw)
    if suffix in {".csv", ".tsv"}:
        reader = csv.reader(io.StringIO(raw), delimiter="\t" if suffix == ".tsv" else ",")
        rows = [r for r in reader if any(c.strip() for c in r)]
        if not rows:
            return []
        return table_sections(rel_s, wiki, rows[1:], rows[0], f"Dataset: {path.stem}")
    if suffix in {".jsonl", ".ndjson"}:
        recs = []
        for line in raw.splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                recs.append(rec)
        if not recs:
            return []
        header = list(dict.fromkeys(k for r in recs for k in r))
        rows = [[_str(r.get(h)) or "" for h in header] for r in recs]
        return table_sections(rel_s, wiki, rows, header, f"Dataset: {path.stem}")
    return []


INDEXED_SUFFIXES = {".md", ".csv", ".tsv", ".jsonl", ".ndjson"}


def iter_files(root: Path):
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in INDEXED_SUFFIXES:
            continue
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts):
            continue
        if p.stat().st_size >= 8 * 1024 * 1024:
            continue
        yield p
