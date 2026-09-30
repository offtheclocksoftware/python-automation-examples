#!/usr/bin/env python3
"""
fake_api.py - a small in-memory REST API to run api_sync.py against (demo and tests). Not for production use.

    python fake_api.py --port 8765          then, in a second terminal, run api_sync.py (see the README)

Endpoints (JSON, bearer token required):
    GET  /items?page=N    {"items": [...], "next": "/items?page=N+1" or null}   (page size 3, so paging is exercised)
    GET  /items/<sku>     one item, or 404
    PUT  /items/<sku>     create or replace an item (idempotent), returns the stored item

Failure injection for tests (see FakeApi): the next N requests can fail with a status (for example 503 or 429 with
Retry-After), and writes for chosen SKUs can be "lost" (answered 200 but not stored), which only a read-back catches.

The token is read from SYNC_API_TOKEN; if it is not set, the demo token "local-demo" is used. It is a made-up value
for this local server only.
SPDX-License-Identifier: MIT - Copyright (c) 2026 Off the Clock Software
"""
from __future__ import annotations

import argparse
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

DEMO_TOKEN = "local-demo"


class FakeApi:
    """The server state. Start with start(), stop with stop(); base_url is set after start()."""

    def __init__(self, token: str = DEMO_TOKEN, page_size: int = 3, items: dict[str, dict] | None = None):
        self.token = token
        self.page_size = page_size
        self.items: dict[str, dict] = dict(items or {})
        self.lock = threading.Lock()
        self.requests: list[tuple[str, str]] = []           # (method, path) log, for tests
        self._fail_queue: list[tuple[int, str | None]] = []
        self.lost_writes: set[str] = set()                  # SKUs whose PUT answers 200 but stores nothing
        self.server: ThreadingHTTPServer | None = None
        self.base_url = ""

    def fail_next(self, count: int, status: int = 503, retry_after: str | None = None) -> None:
        with self.lock:
            self._fail_queue.extend([(status, retry_after)] * count)

    def start(self, port: int = 0) -> "FakeApi":
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):                     # keep test output quiet
                pass

            def _send(self, status: int, body: object | None = None, headers: dict | None = None):
                data = b"" if body is None else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _gate(self) -> bool:
                with api.lock:
                    api.requests.append((self.command, self.path))
                    failure = api._fail_queue.pop(0) if api._fail_queue else None
                if failure:
                    status, retry_after = failure
                    self._send(status, {"error": "injected failure"}, {"Retry-After": retry_after} if retry_after else None)
                    return False
                if self.headers.get("Authorization") != f"Bearer {api.token}":
                    self._send(401, {"error": "missing or wrong token"})
                    return False
                return True

            def do_GET(self):
                if not self._gate():
                    return
                url = urlparse(self.path)
                m = re.fullmatch(r"/items/([^/]+)", url.path)
                sku = unquote(m.group(1)) if m else None
                with api.lock:
                    if url.path == "/items":
                        page = int(parse_qs(url.query).get("page", ["1"])[0])
                        skus = sorted(api.items)
                        chunk = skus[(page - 1) * api.page_size: page * api.page_size]
                        more = page * api.page_size < len(skus)
                        return self._send(200, {"items": [api.items[s] for s in chunk],
                                                "next": f"/items?page={page + 1}" if more else None})
                    if sku in api.items:
                        return self._send(200, api.items[sku])
                return self._send(404, {"error": "not found"})

            def do_PUT(self):
                if not self._gate():
                    return
                m = re.fullmatch(r"/items/([^/]+)", urlparse(self.path).path)
                if not m:
                    return self._send(404, {"error": "not found"})
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                except json.JSONDecodeError:
                    return self._send(400, {"error": "invalid JSON"})
                sku = unquote(m.group(1))
                if not isinstance(body, dict) or body.get("sku") != sku:
                    return self._send(422, {"error": "body.sku must match the URL"})
                with api.lock:
                    if sku not in api.lost_writes:
                        api.items[sku] = body
                return self._send(200, body)

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return self

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the in-memory demo API on 127.0.0.1.")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    seed = {
        "A-100": {"sku": "A-100", "name": "Desk lamp", "price": "24.99", "stock": 12},
        "A-101": {"sku": "A-101", "name": "Monitor stand", "price": "39.00", "stock": 4},
        "B-200": {"sku": "B-200", "name": "USB-C cable 2 m", "price": "9.50", "stock": 80},
        "C-300": {"sku": "C-300", "name": "Old model (discontinued)", "price": "5.00", "stock": 0},
    }
    api = FakeApi(token=os.environ.get("SYNC_API_TOKEN") or DEMO_TOKEN, items=seed).start(a.port)
    print(f"demo API on {api.base_url} with {len(seed)} items - Ctrl+C to stop")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        api.stop()


if __name__ == "__main__":
    main()
