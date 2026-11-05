"""`brain` client: stdlib only so it starts instantly; the daemon (brain_search.server) holds the models."""

from __future__ import annotations

import argparse
import json
import os
import signal
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PORT = int(os.environ.get("BRAIN_SEARCH_PORT", "8770"))
BASE = f"http://127.0.0.1:{PORT}"
BRAINS = Path(os.environ.get("BRAIN_SEARCH_BRAINS", Path.home() / "brains"))
LOG = Path(os.environ.get("BRAIN_SEARCH_CACHE", Path.home() / ".cache" / "brain-search")) / "server.log"


def default_brain() -> str | None:
    if os.environ.get("BRAIN_SEARCH_BRAIN"):
        return os.environ["BRAIN_SEARCH_BRAIN"]
    found = [p.name for p in BRAINS.iterdir() if p.is_dir()] if BRAINS.is_dir() else []
    return found[0] if len(found) == 1 else None


def call(route: str, body: dict | None = None, timeout: float = 300) -> dict:
    req = urllib.request.Request(BASE + route, data=json.dumps(body or {}).encode(), headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        payload = json.loads(e.read() or b"{}")
        sys.exit(f"brain: {payload.get('error', e)}")


def alive() -> dict | None:
    try:
        with urllib.request.urlopen(BASE + "/health", timeout=2) as r:
            body = r.read()
    except (urllib.error.URLError, OSError, TimeoutError):
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        sys.exit(f"brain: port {PORT} is taken by another service; set BRAIN_SEARCH_PORT")


def ensure_server(brain: str | None) -> None:
    if alive():
        return
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "ab") as log:
        subprocess.Popen(
            [sys.executable, "-m", "brain_search.server", *([brain] if brain else [])],
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    print("brain: starting search daemon (first start loads models)…", file=sys.stderr)
    deadline = time.time() + 600
    while time.time() < deadline:
        if alive():
            return
        time.sleep(0.5)
    sys.exit(f"brain: daemon did not come up; see {LOG}")


def print_hits(res: dict, full: bool) -> None:
    t = res["timings"]
    print(f"{len(res['results'])} results in {t.get('total_ms', 0):.0f} ms  ({', '.join(f'{k}={v}' for k, v in t.items() if k != 'total_ms')})")
    for n, h in enumerate(res["results"], 1):
        crumb = " › ".join([*h["ancestry"], h["heading"]] if h["heading"] and h["heading"] not in h["ancestry"] else h["ancestry"])
        meta = [h["kind"], f"score {h['score']:.3f}", "+".join(h["signals"])]
        if h["date"]:
            meta.append(f"page {h['date']}")
        print(f"\n[{n}] {crumb}\n    {h['path']}  ({', '.join(meta)})\n    {h['url']}")
        text = h["text"] if full else (h["text"][:500] + (" …" if len(h["text"]) > 500 else ""))
        print("    " + text.replace("\n", "\n    "))
        if h["kind"] == "wiki" and h["sources"]:
            print(f"    sources: {', '.join(h['sources'])}")


def cmd_search(a) -> None:
    ensure_server(a.brain)
    body = {
        "brain": a.brain,
        "queries": [a.query, *a.also],
        "k": a.k,
        "rerank": a.deep,
        "wiki": a.wiki,
        "kinds": a.kind.split(",") if a.kind else None,
        "path": a.path,
        "per_page": a.per_page,
        "max_chars": a.max_chars,
    }
    if a.recent is not None:
        body["recency"] = a.recent
    res = call("/search", body)
    print(json.dumps(res, ensure_ascii=False, indent=1) if a.json else "", end="")
    if not a.json:
        print_hits(res, a.full)


def cmd_sql(a) -> None:
    ensure_server(a.brain)
    res = call("/sql", {"brain": a.brain, "sql": a.sql, "limit": a.limit})
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return
    cols, rows = res["columns"], res["rows"]
    widths = [min(60, max([len(str(c))] + [len(str(r[i])) for r in rows])) for i, c in enumerate(cols)]
    fmt = " | ".join(f"{{:{w}.{w}}}" for w in widths)
    print(fmt.format(*map(str, cols)))
    print("-+-".join("-" * w for w in widths))
    for r in rows:
        print(fmt.format(*["" if v is None else str(v) for v in r]))
    print(f"({len(rows)} rows{', truncated' if res['truncated'] else ''})")


def cmd_tables(a) -> None:
    ensure_server(a.brain)
    for name, d in call("/tables", {"brain": a.brain}).items():
        if "error" in d:
            print(f"{name}  ({d['path']})  ERROR {d['error']}")
        else:
            print(f"{name}  ({d['path']}, {d['rows']} rows)\n    {', '.join(d['columns'])}")


def cmd_status(a) -> None:
    ensure_server(a.brain)
    print(json.dumps(call("/status", {"brain": a.brain}), indent=1))


def cmd_stop(_a) -> None:
    h = alive()
    if not h:
        print("brain: daemon not running")
        return
    os.kill(h["pid"], signal.SIGTERM)
    print(f"brain: stopped daemon pid {h['pid']}")


def cmd_serve(a) -> None:
    os.execv(sys.executable, [sys.executable, "-m", "brain_search.server", *([a.brain] if a.brain else [])])


def cmd_eval(a) -> None:
    """Questions file (jsonl): {"q": ..., "expect": ["path or path#heading substring", ...], "also": [variants]}"""
    ensure_server(a.brain)
    qs = [json.loads(l) for l in Path(a.file).read_text().splitlines() if l.strip()]
    ranks, lat, misses = [], [], []
    for item in qs:
        res = call("/search", {"brain": a.brain, "queries": [item["q"], *item.get("also", [])], "k": 10, "rerank": a.deep})
        lat.append(res["timings"]["total_ms"])
        keys = [f"{h['path']}#{h['heading']}" for h in res["results"]]
        rank = next((i + 1 for i, k in enumerate(keys) if any(e in k for e in item["expect"])), None)
        ranks.append(rank)
        if rank is None or rank > 3:
            misses.append((item["q"], rank, keys[:3]))
    n = len(ranks)
    r_at = lambda k: sum(1 for r in ranks if r and r <= k) / n
    mrr = sum(1 / r for r in ranks if r) / n
    lat.sort()
    print(f"{n} questions  mode={'deep (rerank)' if a.deep else 'fast (hybrid)'}")
    print(f"recall@1 {r_at(1):.2f}  @3 {r_at(3):.2f}  @5 {r_at(5):.2f}  @10 {r_at(10):.2f}  MRR {mrr:.3f}")
    print(f"latency p50 {statistics.median(lat):.0f} ms  p95 {lat[max(0, int(n * 0.95) - 1)]:.0f} ms  max {lat[-1]:.0f} ms")
    for q, r, keys in misses:
        print(f"  miss@3 (rank {r}): {q}\n      got: {keys}")


def main() -> None:
    p = argparse.ArgumentParser(prog="brain", description="Fast hybrid search over a local brain folder (~/brains/<name>).")
    p.add_argument("--brain", default=default_brain(), help="brain folder name under ~/brains (env BRAIN_SEARCH_BRAIN)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="hybrid search; returns whole sections with citations")
    s.add_argument("query")
    s.add_argument("-q", "--also", action="append", default=[], help="extra phrasing/translation of the same question (repeatable)")
    s.add_argument("-k", type=int, default=8)
    s.add_argument("--deep", action="store_true", help="add the cross-encoder rerank (~1 s, more precise ordering)")
    s.add_argument("--wiki", help="only this wiki (top-level folder)")
    s.add_argument("--kind", help="comma list: wiki,raw,dataset,inventory,output,index,log,meta")
    s.add_argument("--path", help="only paths starting with this prefix")
    s.add_argument("--per-page", type=int, default=2, help="max sections per page (diversity)")
    s.add_argument("--recent", dest="recent", action="store_true", default=None, help="force recency boost")
    s.add_argument("--no-recent", dest="recent", action="store_false", help="disable recency boost")
    s.add_argument("--max-chars", type=int, default=2400)
    s.add_argument("--full", action="store_true", help="print whole sections")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_search)

    q = sub.add_parser("sql", help="DuckDB SQL over datasets/*.csv|jsonl (views named after files)")
    q.add_argument("sql")
    q.add_argument("--limit", type=int, default=200)
    q.add_argument("--json", action="store_true")
    q.set_defaults(fn=cmd_sql)

    sub.add_parser("tables", help="list dataset views and columns").set_defaults(fn=cmd_tables)
    sub.add_parser("status", help="index status").set_defaults(fn=cmd_status)
    sub.add_parser("serve", help="run the daemon in the foreground").set_defaults(fn=cmd_serve)
    sub.add_parser("stop", help="stop the daemon").set_defaults(fn=cmd_stop)
    e = sub.add_parser("eval", help="recall/MRR/latency over a jsonl question set")
    e.add_argument("file")
    e.add_argument("--deep", action="store_true")
    e.set_defaults(fn=cmd_eval)

    a = p.parse_args()
    if a.cmd not in {"stop", "serve"} and not a.brain:
        sys.exit("brain: name a brain with --brain or BRAIN_SEARCH_BRAIN")
    a.fn(a)


if __name__ == "__main__":
    main()
