"""Warm local daemon: keeps models and indexes in memory so a search costs milliseconds, not a model load."""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer

from .engine import Brain, Models, hit_json
from .sql import describe, run_sql

HOST = "127.0.0.1"
PORT = int(os.environ.get("BRAIN_SEARCH_PORT", "8770"))
_brains: dict[str, Brain] = {}


def brain(name: str) -> Brain:
    if name not in _brains:
        _brains[name] = Brain(name)
    return _brains[name]


def handle(route: str, body: dict) -> dict:
    if route == "/health":
        return {"ok": True, "pid": os.getpid(), "brains": sorted(_brains)}
    b = brain(body["brain"])
    if route == "/status":
        return b.status()
    if route == "/search":
        t = time.perf_counter()
        hits, timings = b.search(
            body.get("queries") or [body["q"]],
            k=int(body.get("k", 8)),
            rerank=bool(body.get("rerank", False)),
            wiki=body.get("wiki"),
            kinds=body.get("kinds"),
            path=body.get("path"),
            per_page=int(body.get("per_page", 2)),
            recency=body.get("recency"),
        )
        timings["total_ms"] = (time.perf_counter() - t) * 1000
        return {
            "brain": b.name,
            "queries": body.get("queries") or [body["q"]],
            "timings": {k: round(v, 1) for k, v in timings.items()},
            "results": [hit_json(b, h, int(body.get("max_chars", 2400))) for h in hits],
        }
    if route == "/sql":
        return run_sql(b.root, body["sql"], int(body.get("limit", 200)))
    if route == "/tables":
        return describe(b.root)
    raise KeyError(route)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet: the client prints what matters
        pass

    def _reply(self, code: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.do_POST()

    def do_POST(self):
        try:
            n = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            self._reply(200, handle(self.path, body))
        except KeyError as e:
            self._reply(400, {"error": f"missing or unknown: {e}"})
        except FileNotFoundError as e:
            self._reply(404, {"error": str(e)})
        except Exception as e:  # report, keep serving
            traceback.print_exc()
            self._reply(500, {"error": f"{type(e).__name__}: {e}"})


def main() -> None:
    warm = [a for a in sys.argv[1:] if not a.startswith("-")]
    Models.embedder()
    Models.reranker()
    for name in warm:
        brain(name).refresh(force=True)
    # Single-threaded on purpose: requests are serialized, so one model instance on the GPU is safe.
    srv = HTTPServer((HOST, PORT), Handler)
    print(f"brain-search listening on http://{HOST}:{PORT} (warm: {', '.join(warm) or 'none'})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
