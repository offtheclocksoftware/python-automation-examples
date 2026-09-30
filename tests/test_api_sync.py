"""Tests for api_sync.py against the in-memory fake API (no network beyond 127.0.0.1, no waiting)."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api_sync"))
import api_sync as sync  # noqa: E402
from fake_api import FakeApi  # noqa: E402


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


TOKEN = "test-token-not-a-secret"
SAMPLE = os.path.join(ROOT, "api_sync", "sample_prices.csv")
SEED = {
    "A-100": {"sku": "A-100", "name": "Desk lamp", "price": "24.99", "stock": 12},
    "A-101": {"sku": "A-101", "name": "Monitor stand", "price": "39.00", "stock": 4},
    "B-200": {"sku": "B-200", "name": "USB-C cable 2 m", "price": "9.50", "stock": 80},
    "C-300": {"sku": "C-300", "name": "Old model (discontinued)", "price": "5.00", "stock": 0},
}


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi(token=TOKEN, items={k: dict(v) for k, v in SEED.items()}).start()
        self.addCleanup(self.api.stop)
        self.waits = []
        self.logs = []

    def client(self, base_url=None, token=TOKEN, max_attempts=5):
        return sync.ApiClient(base_url or self.api.base_url, token, timeout=5, max_attempts=max_attempts,
                              sleep=self.waits.append, rand=lambda a, b: 0.0, log=self.logs.append)

    def run_main(self, *args, env_token=TOKEN):
        old = os.environ.get("SYNC_API_TOKEN")
        if env_token is None:
            os.environ.pop("SYNC_API_TOKEN", None)
        else:
            os.environ["SYNC_API_TOKEN"] = env_token
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = sync.main(["--base-url", self.api.base_url, *args],
                                 client_factory=lambda url, tok, **kw: self.client(url, tok, **kw))
        finally:
            if old is None:
                os.environ.pop("SYNC_API_TOKEN", None)
            else:
                os.environ["SYNC_API_TOKEN"] = old
        return code, out.getvalue(), err.getvalue()


class ClientTests(ApiTestCase):
    def test_pages_are_followed(self):
        items = self.client().list_items()
        self.assertEqual(sorted(i["sku"] for i in items), sorted(SEED))
        self.assertEqual([p for m, p in self.api.requests], ["/items", "/items?page=2"])

    def test_retries_503_with_exponential_backoff(self):
        self.api.fail_next(3, 503)
        self.assertEqual(len(self.client().list_items()), 4)
        self.assertEqual(self.waits, [0.5, 1.0, 2.0])
        self.assertIn("got HTTP 503", self.logs[0])

    def test_retry_after_is_respected(self):
        self.api.fail_next(1, 429, retry_after="7")
        self.client().list_items()
        self.assertEqual(self.waits, [7.0])

    def test_gives_up_after_max_attempts(self):
        self.api.fail_next(10, 502)
        with self.assertRaises(sync.ApiError) as ctx:
            self.client(max_attempts=3).list_items()
        self.assertIn("after 3 attempts", str(ctx.exception))
        self.assertEqual(len(self.waits), 2)

    def test_client_errors_are_not_retried(self):
        self.api.fail_next(1, 400)
        with self.assertRaises(sync.ApiError) as ctx:
            self.client().list_items()
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(self.waits, [])

    def test_connection_refused_is_retried_then_reported(self):
        port = self.api.server.server_address[1]
        self.api.stop()
        with self.assertRaises(sync.ApiError) as ctx:
            self.client(base_url=f"http://127.0.0.1:{port}", max_attempts=2).list_items()
        self.assertIn("network error", str(ctx.exception))
        self.assertEqual(len(self.waits), 1)

    def test_token_never_in_repr(self):
        self.assertNotIn(TOKEN, repr(self.client()))


class SourceTests(unittest.TestCase):
    def write(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
        f.write(text)
        f.close()
        self.addCleanup(os.remove, f.name)
        return f.name

    def test_sample_loads_and_normalizes(self):
        src = sync.load_source(SAMPLE)
        self.assertEqual(src["B-200"], {"sku": "B-200", "name": "USB-C cable 2 m", "price": "9.50", "stock": 64})

    def test_every_bad_row_is_listed_and_nothing_loads(self):
        p = self.write("sku,name,price,stock\nX-1,Ok,1.00,1\n,No sku,1,1\nX-2,Bad price,abc,1\nX-3,Neg,-1,1\n"
                       "X-4,Frac stock,1,1.5\nX-1,Dup,1,1\n")
        with self.assertRaises(sync.SourceError) as ctx:
            sync.load_source(p)
        msg = str(ctx.exception)
        self.assertIn("5 bad row(s)", msg)
        for part in ("line 3: empty sku", "line 4: price must be a number", "line 5", "line 6: stock", "line 7: sku X-1 appears twice"):
            self.assertIn(part, msg)

    def test_missing_columns(self):
        with self.assertRaises(sync.SourceError):
            sync.load_source(self.write("sku,name\nX,Y\n"))


class SyncTests(ApiTestCase):
    def test_plan(self):
        plan = sync.make_plan(sync.load_source(SAMPLE), self.client().list_items())
        self.assertEqual([i["sku"] for i in plan.create], ["D-400"])
        self.assertEqual({i["sku"]: d for i, d in plan.update},
                         {"A-100": {"price": ("24.99", "22.99")}, "B-200": {"stock": (80, 64)}})
        self.assertEqual(plan.unchanged, ["A-101"])
        self.assertEqual(plan.only_in_api, ["C-300"])

    def test_dry_run_writes_nothing(self):
        code, out, _ = self.run_main("--source", SAMPLE)
        self.assertEqual(code, 0)
        self.assertIn("dry run: nothing was written", out)
        self.assertFalse(any(m == "PUT" for m, _ in self.api.requests))
        self.assertEqual(self.api.items["A-100"]["price"], "24.99")

    def test_apply_writes_verifies_and_is_idempotent(self):
        report = os.path.join(tempfile.mkdtemp(), "report.json")
        self.addCleanup(os.remove, report)
        code, out, _ = self.run_main("--source", SAMPLE, "--apply", "--report", report)
        self.assertEqual(code, 0, out)
        self.assertIn("3 written and verified", out)
        self.assertEqual(self.api.items["A-100"]["price"], "22.99")
        self.assertEqual(self.api.items["D-400"]["stock"], 25)
        self.assertIn("C-300", self.api.items)                          # never deleted
        summary = json.loads(read_text(report))
        self.assertEqual(summary["written"], ["D-400", "A-100", "B-200"])
        self.assertNotIn(TOKEN, json.dumps(summary))
        self.api.requests.clear()
        code, out, _ = self.run_main("--source", SAMPLE, "--apply")
        self.assertEqual(code, 0)
        self.assertIn("nothing to do", out)
        self.assertFalse(any(m == "PUT" for m, _ in self.api.requests))

    def test_lost_write_is_caught_by_read_back(self):
        self.api.lost_writes.add("A-100")                              # API says 200 but keeps the old price
        code, out, _ = self.run_main("--source", SAMPLE, "--apply")
        self.assertEqual(code, 1)
        self.assertIn("1 read-back mismatch", out)
        self.assertIn('MISMATCH A-100: {"price": {"expected": "22.99", "found": "24.99"}}', out)

    def test_transient_errors_during_apply_are_retried(self):
        self.api.fail_next(2, 503)
        code, out, _ = self.run_main("--source", SAMPLE, "--apply")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.waits), 2)

    def test_wrong_token_stops_with_code_2(self):
        code, out, err = self.run_main("--source", SAMPLE, env_token="tok-DO-NOT-PRINT")
        self.assertEqual(code, 2)
        self.assertIn("HTTP 401", err)
        self.assertNotIn("tok-DO-NOT-PRINT", out + err)                # the token value itself is never printed

    def test_missing_token_or_bad_source_stops_before_any_request(self):
        code, _, err = self.run_main("--source", SAMPLE, env_token=None)
        self.assertEqual(code, 2)
        self.assertIn("SYNC_API_TOKEN", err)
        code, _, _ = self.run_main("--source", "no_such.csv")
        self.assertEqual(code, 2)
        self.assertEqual(self.api.requests, [])


if __name__ == "__main__":
    unittest.main()
