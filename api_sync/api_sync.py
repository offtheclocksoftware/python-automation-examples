#!/usr/bin/env python3
"""
api_sync.py - sync a price list (CSV) into a REST API, safely: dry run first, retries, and a read-back check.

    SYNC_API_TOKEN=... python api_sync.py --base-url https://api.example.com --source sample_prices.csv            # plan only
    SYNC_API_TOKEN=... python api_sync.py --base-url https://api.example.com --source sample_prices.csv --apply    # write

Steps:
  1. Validate the whole CSV first (sku, name, price, stock). One bad row stops the run before anything is written.
  2. Read every item from the API (follows the "next" page links).
  3. Plan: create / update (with the exact field changes) / unchanged / only in the API (never deleted).
  4. Dry run by default: print the plan and stop. With --apply, send one PUT per change.
  5. Read every written item back and compare it field by field. A write the API answered "200 OK" but did not store
     is reported as a mismatch, not silently counted as done.

Network errors, timeouts and HTTP 429/500/502/503/504 are retried with exponential backoff and jitter, and a
Retry-After header is respected. Retrying is safe here because PUT is idempotent (sending it twice changes nothing).
Other 4xx answers are not retried. The token is read from an environment variable (SYNC_API_TOKEN by default),
sent only in the Authorization header, and never printed or logged.

Standard library only (Python 3.10+). Exit codes: 0 in sync, 1 a write failed or a read-back did not match,
2 usage, source or authentication error.
SPDX-License-Identifier: MIT - Copyright (c) 2026 Off the Clock Software
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import email.utils
import json
import os
import random
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Callable
from urllib.parse import quote

RETRY_STATUSES = {429, 500, 502, 503, 504}
FIELDS = ("sku", "name", "price", "stock")


class ApiError(Exception):
    def __init__(self, status: int | None, message: str):
        super().__init__(message)
        self.status = status


class SourceError(Exception):
    pass


# --------------------------------------------------------------------------------------------- HTTP client
class ApiClient:
    """JSON over HTTP with retries. `sleep` and `rand` can be replaced (tests don't wait)."""

    def __init__(self, base_url: str, token: str, timeout: float = 15.0, max_attempts: int = 5, backoff: float = 0.5,
                 max_wait: float = 60.0, sleep: Callable[[float], None] = time.sleep,
                 rand: Callable[[float, float], float] = random.uniform, log: Callable[[str], None] = print):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.backoff = backoff
        self.max_wait = max_wait
        self.sleep = sleep
        self.rand = rand
        self.log = log
        self.retries = 0

    def __repr__(self) -> str:                                   # never show the token
        return f"ApiClient({self.base_url!r})"

    def _wait_time(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), self.max_wait)
            except ValueError:
                try:
                    when = email.utils.parsedate_to_datetime(retry_after)
                    return min(max((when - dt.datetime.now(dt.timezone.utc)).total_seconds(), 0.0), self.max_wait)
                except (TypeError, ValueError):
                    pass
        return min(self.backoff * 2 ** (attempt - 1) + self.rand(0, self.backoff), self.max_wait)

    def request(self, method: str, path: str, body: object | None = None) -> object:
        url = path if path.startswith("http") else self.base_url + path
        data = None if body is None else json.dumps(body).encode()
        for attempt in range(1, self.max_attempts + 1):
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": f"Bearer {self._token}", "Accept": "application/json",
                **({"Content-Type": "application/json"} if data is not None else {})})
            retry_after, reason = None, ""
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as ex:
                if ex.code not in RETRY_STATUSES:
                    detail = ex.read().decode(errors="replace")[:200]
                    raise ApiError(ex.code, f"{method} {path} -> HTTP {ex.code} {detail}".strip()) from None
                retry_after, reason = ex.headers.get("Retry-After"), f"HTTP {ex.code}"
            except (urllib.error.URLError, socket.timeout, ConnectionError) as ex:
                reason = f"network error ({getattr(ex, 'reason', ex)})"
            except json.JSONDecodeError:
                raise ApiError(None, f"{method} {path} -> the answer is not JSON") from None
            if attempt == self.max_attempts:
                raise ApiError(None, f"{method} {path} failed after {attempt} attempts ({reason})")
            wait = self._wait_time(attempt, retry_after)
            self.retries += 1
            self.log(f"retry {attempt}/{self.max_attempts - 1}: {method} {path} got {reason}, waiting {wait:.1f} s")
            self.sleep(wait)
        raise AssertionError("unreachable")

    def list_items(self) -> list[dict]:
        items, path, pages = [], "/items", 0
        while path:
            page = self.request("GET", path)
            items.extend(page.get("items", []))
            path = page.get("next")
            pages += 1
            if pages > 10_000:
                raise ApiError(None, "more than 10,000 pages: the API's next links look like a loop")
        return items


# --------------------------------------------------------------------------------------------- data
def normalize(item: dict) -> dict:
    """The comparable form of an item: trimmed name, price with 2 decimals as text, stock as int."""
    return {"sku": str(item.get("sku", "")).strip(),
            "name": " ".join(str(item.get("name", "")).split()),
            "price": str(Decimal(str(item.get("price", "0"))).quantize(Decimal("0.01"))),
            "stock": int(item.get("stock", 0))}


def load_source(path: str) -> dict[str, dict]:
    """Reads and validates the whole CSV. Raises SourceError listing every bad row (nothing is half-synced)."""
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    except OSError as ex:
        raise SourceError(f"cannot read {path}: {ex.strerror}") from None
    if rows and set(FIELDS) - {h.strip().lower() for h in rows[0].keys() if h}:
        raise SourceError(f"{path}: needs the columns {', '.join(FIELDS)}")
    out, errors = {}, []
    for n, raw in enumerate(rows, start=2):
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        try:
            if not r["sku"]:
                raise ValueError("empty sku")
            if r["sku"] in out:
                raise ValueError(f"sku {r['sku']} appears twice")
            if not r["name"]:
                raise ValueError("empty name")
            try:
                price = Decimal(r["price"].replace(",", ""))
            except InvalidOperation:
                raise ValueError(f"price must be a number, got {r['price']!r}") from None
            if not price.is_finite() or price < 0:
                raise ValueError(f"price must be a number >= 0, got {r['price']!r}")
            if not r["stock"].isdigit():
                raise ValueError(f"stock must be a whole number >= 0, got {r['stock']!r}")
            out[r["sku"]] = normalize({**r, "price": price, "stock": int(r["stock"])})
        except ValueError as ex:
            errors.append(f"  line {n}: {ex}")
    if errors:
        raise SourceError(f"{path}: {len(errors)} bad row(s), nothing was sent:\n" + "\n".join(errors))
    return out


@dataclass
class Plan:
    create: list[dict] = field(default_factory=list)
    update: list[tuple[dict, dict[str, tuple]]] = field(default_factory=list)   # (new item, {field: (old, new)})
    unchanged: list[str] = field(default_factory=list)
    only_in_api: list[str] = field(default_factory=list)

    @property
    def changes(self) -> list[dict]:
        return self.create + [item for item, _ in self.update]


def make_plan(source: dict[str, dict], remote_items: list[dict]) -> Plan:
    remote = {}
    for it in remote_items:
        try:
            remote[str(it["sku"])] = normalize(it)
        except (KeyError, InvalidOperation, ValueError, TypeError):
            continue                                            # an item we can't read is left alone
    plan = Plan()
    for sku, item in source.items():
        if sku not in remote:
            plan.create.append(item)
            continue
        diff = {f: (remote[sku][f], item[f]) for f in FIELDS if remote[sku][f] != item[f]}
        if diff:
            plan.update.append((item, diff))
        else:
            plan.unchanged.append(sku)
    plan.only_in_api = sorted(set(remote) - set(source))
    return plan


def print_plan(plan: Plan, out: Callable[[str], None] = print) -> None:
    out(f"plan: {len(plan.create)} to create, {len(plan.update)} to update, {len(plan.unchanged)} unchanged, "
        f"{len(plan.only_in_api)} only in the API (left alone)")
    for item in plan.create:
        out(f"  + {item['sku']}  {item['name']}  price {item['price']}  stock {item['stock']}")
    for item, diff in plan.update:
        out(f"  ~ {item['sku']}  " + ", ".join(f"{f}: {old!r} -> {new!r}" for f, (old, new) in diff.items()))
    for sku in plan.only_in_api:
        out(f"  = {sku}  only in the API, not touched")


@dataclass
class Outcome:
    written: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    mismatched: dict[str, dict] = field(default_factory=dict)


def apply_plan(client: ApiClient, plan: Plan, out: Callable[[str], None] = print) -> Outcome:
    result = Outcome()
    for item in plan.changes:
        try:
            client.request("PUT", f"/items/{quote(item['sku'], safe='')}", item)
            result.written.append(item["sku"])
        except ApiError as ex:
            if ex.status == 401:
                raise
            result.failed[item["sku"]] = str(ex)
            out(f"  write failed: {ex}")
    for sku in result.written:                                  # read-back: trust what the API stores, not what it says
        want = next(i for i in plan.changes if i["sku"] == sku)
        try:
            got = normalize(client.request("GET", f"/items/{quote(sku, safe='')}"))
        except ApiError as ex:
            result.mismatched[sku] = {"error": str(ex)}
            continue
        diff = {f: {"expected": want[f], "found": got[f]} for f in FIELDS if want[f] != got[f]}
        if diff:
            result.mismatched[sku] = diff
    return result


# --------------------------------------------------------------------------------------------- command line
def main(argv: list[str] | None = None, client_factory: Callable[..., ApiClient] = ApiClient) -> int:
    ap = argparse.ArgumentParser(description="Sync a CSV price list into a REST API (dry run unless --apply).")
    ap.add_argument("--base-url", required=True, help="API root, e.g. https://api.example.com")
    ap.add_argument("--source", required=True, help="CSV with columns sku,name,price,stock")
    ap.add_argument("--apply", action="store_true", help="really write (default: dry run, nothing is sent but GETs)")
    ap.add_argument("--token-env", default="SYNC_API_TOKEN", help="environment variable that holds the API token")
    ap.add_argument("--report", help="write a JSON summary of the run to this file")
    ap.add_argument("--max-attempts", type=int, default=5, help="attempts per request (default 5)")
    a = ap.parse_args(argv)

    token = os.environ.get(a.token_env, "")
    if not token:
        print(f"error: set the API token in the {a.token_env} environment variable", file=sys.stderr)
        return 2
    try:
        source = load_source(a.source)
    except SourceError as ex:
        print(f"error: {ex}", file=sys.stderr)
        return 2

    client = client_factory(a.base_url, token, max_attempts=a.max_attempts)
    summary: dict = {"when": dt.datetime.now().isoformat(timespec="seconds"), "base_url": a.base_url,
                     "source": os.path.basename(a.source), "mode": "apply" if a.apply else "dry-run"}
    try:
        plan = make_plan(source, client.list_items())
        print_plan(plan)
        summary["plan"] = {"create": [i["sku"] for i in plan.create], "update": [i["sku"] for i, _ in plan.update],
                           "unchanged": plan.unchanged, "only_in_api": plan.only_in_api}
        code = 0
        if not a.apply:
            print(f"dry run: nothing was written. Run again with --apply to make {len(plan.changes)} change(s).")
        elif plan.changes:
            res = apply_plan(client, plan)
            summary.update(written=res.written, failed=res.failed, mismatched=res.mismatched)
            ok = len(res.written) - len(res.mismatched)
            print(f"applied: {ok} written and verified, {len(res.failed)} failed, {len(res.mismatched)} read-back mismatch(es)")
            for sku, diff in res.mismatched.items():
                print(f"  MISMATCH {sku}: {json.dumps(diff)}")
            code = 1 if (res.failed or res.mismatched) else 0
        else:
            print("nothing to do: the API already matches the source.")
    except ApiError as ex:
        print(f"error: {ex}" + (" (check the token)" if ex.status == 401 else ""), file=sys.stderr)
        summary["error"] = str(ex)
        code = 2
    summary["retries"] = client.retries
    if a.report:
        with open(a.report, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
    return code


if __name__ == "__main__":
    sys.exit(main())
