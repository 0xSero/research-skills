"""DuckDB over a brain's datasets: every datasets/*.csv|tsv|jsonl becomes a view named after its file."""

from __future__ import annotations

import re
from pathlib import Path

import duckdb

from .corpus import iter_files

DATA_SUFFIXES = {".csv": "read_csv_auto", ".tsv": "read_csv_auto", ".jsonl": "read_json_auto", ".ndjson": "read_json_auto"}


def _ident(s: str) -> str:
    return re.sub(r"\W+", "_", s.lower()).strip("_")


def dataset_views(root: Path) -> dict[str, Path]:
    found = [p for p in iter_files(root) if p.suffix.lower() in DATA_SUFFIXES and "datasets" in p.relative_to(root).parts]
    stems: dict[str, int] = {}
    for p in found:
        stems[_ident(p.stem)] = stems.get(_ident(p.stem), 0) + 1
    views = {}
    for p in found:
        name = _ident(p.stem)
        if stems[name] > 1:  # same file name in two wikis: qualify with the wiki
            name = f"{_ident(p.relative_to(root).parts[0])}__{name}"
        views[name] = p
    return views


def run_sql(root: Path, sql: str, limit: int = 200) -> dict:
    views = dataset_views(root)
    con = duckdb.connect(":memory:")
    try:
        for name, p in views.items():
            fn = DATA_SUFFIXES[p.suffix.lower()]
            src = str(p).replace("'", "''")
            opts = ", delim='\\t'" if p.suffix.lower() == ".tsv" else ""
            con.execute(f"CREATE VIEW \"{name}\" AS SELECT * FROM {fn}('{src}'{opts})")
        root_s = str(root.resolve()).replace("'", "''")
        con.execute(f"SET allowed_directories = ['{root_s}']")
        con.execute("SET enable_external_access = false")  # queries can read only files inside the brain folder
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(limit + 1)
        return {
            "columns": cols,
            "rows": [[None if v is None else (v if isinstance(v, (int, float, str, bool)) else str(v)) for v in r] for r in rows[:limit]],
            "truncated": len(rows) > limit,
            "views": {k: str(v.relative_to(root)) for k, v in views.items()},
        }
    finally:
        con.close()


def describe(root: Path) -> dict:
    out = {}
    con = duckdb.connect(":memory:")
    try:
        for name, p in dataset_views(root).items():
            fn = DATA_SUFFIXES[p.suffix.lower()]
            src = str(p).replace("'", "''")
            try:
                cols = con.execute(f"DESCRIBE SELECT * FROM {fn}('{src}')").fetchall()
                n = con.execute(f"SELECT count(*) FROM {fn}('{src}')").fetchone()[0]
                out[name] = {"path": str(p.relative_to(root)), "rows": n, "columns": [f"{c[0]} {c[1]}" for c in cols]}
            except duckdb.Error as e:
                out[name] = {"path": str(p.relative_to(root)), "error": str(e).splitlines()[0]}
    finally:
        con.close()
    return out
