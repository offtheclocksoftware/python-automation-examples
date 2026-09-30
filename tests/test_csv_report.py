"""Tests for csv_report.py (standard library unittest; run: python -m unittest discover -s tests -v)."""
import csv
import datetime as dt
import io
import os
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
import xml.etree.ElementTree as ET
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "csv_report"))
import csv_report as cr  # noqa: E402


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


SAMPLES = os.path.join(ROOT, "csv_report", "sample_data")
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def write(path, text, encoding="utf-8"):
    with open(path, "w", encoding=encoding, newline="") as f:
        f.write(text)
    return path


class ParsingTests(unittest.TestCase):
    def test_headers_are_normalized(self):
        self.assertEqual(cr.normalize_header("  Order ID "), "order_id")
        self.assertEqual(cr.normalize_header("Unit-Price ($)"), "unit_price")
        self.assertEqual(cr.normalize_header("***"), "column")

    def test_numbers(self):
        cases = {"$1,234.50": Decimal("1234.50"), "(5.00)": Decimal("-5.00"), "-$15.00": Decimal("-15.00"),
                 "12%": Decimal("12"), "120,50": Decimal("120.50"), "1.250,00": Decimal("1250.00"),
                 "1,250": Decimal("1250"), "0.5": Decimal("0.5"), "0": Decimal("0")}
        for text, want in cases.items():
            self.assertEqual(cr.parse_number(text), want, text)
        for text in ("twelve", "1.2.3", "04011", "12-3", ""):
            self.assertIsNone(cr.parse_number(text), text)

    def test_dates(self):
        self.assertEqual(cr.parse_date("2026-01-04"), dt.date(2026, 1, 4))
        self.assertEqual(cr.parse_date("01/09/2026"), dt.date(2026, 1, 9))
        self.assertEqual(cr.parse_date("01/09/2026", dayfirst=True), dt.date(2026, 9, 1))
        self.assertEqual(cr.parse_date("Feb 3 2026"), dt.date(2026, 2, 3))
        self.assertIsNone(cr.parse_date("soon"))

    def test_blanks(self):
        for text in ("N/A", " null ", "-", "", None):
            self.assertEqual(cr.clean_text(text), "")
        self.assertEqual(cr.clean_text("  Harbor   Cafe "), "Harbor Cafe")


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = self.tmp.name

    def test_samples_merge_dedupe_and_flag_issues(self):
        paths = cr.expand([os.path.join(SAMPLES, "*.csv")])
        self.assertEqual([os.path.basename(p) for p in paths], ["2026-01_orders.csv", "2026-02_orders.csv"])
        r = cr.build(paths, key="Order ID")
        self.assertEqual(r.types["amount"], "number")
        self.assertEqual(r.types["order_date"], "date")
        self.assertEqual(r.types["zip"], "text")                       # leading zeros kept
        self.assertEqual(r.rows_in, 10)
        self.assertEqual(r.duplicates_removed, 2)
        by_id = {row["order_id"]: row for row in r.rows}
        self.assertEqual(by_id[Decimal(1004)]["amount"], Decimal("120.00"))   # February export overrides January
        self.assertEqual(by_id[Decimal(1004)]["zip"], "04011")
        self.assertEqual(by_id[Decimal(1006)]["customer"], "Café Müller")   # Windows-1252 file read right
        self.assertEqual([row["order_id"] for row in r.rows][:4], [Decimal(n) for n in (1001, 1002, 1003, 1004)])
        self.assertEqual([(i.column, i.value) for i in r.issues], [("amount", "twelve")])

    def test_exact_duplicates_without_key(self):
        p = write(os.path.join(self.d, "a.csv"), "a,b\n1,x\n1,x\n2,y\n")
        r = cr.build([p])
        self.assertEqual((len(r.rows), r.duplicates_removed), (2, 1))

    def test_missing_key_row_is_dropped_and_reported(self):
        p = write(os.path.join(self.d, "a.csv"), "id,v\n1,a\n,b\n")
        r = cr.build([p], key="id")
        self.assertEqual(len(r.rows), 1)
        self.assertEqual(r.issues[0].problem, "missing key (row dropped)")

    def test_unknown_key_is_an_error(self):
        p = write(os.path.join(self.d, "a.csv"), "id,v\n1,a\n")
        with self.assertRaises(ValueError):
            cr.build([p], key="nope")

    def test_columns_union_and_extra_values(self):
        a = write(os.path.join(self.d, "a.csv"), "id,name\n1,x\n")
        b = write(os.path.join(self.d, "b.csv"), "name,id,city\ny,2,Oslo,EXTRA\n")
        r = cr.build([a, b])
        self.assertEqual(r.columns, ["id", "name", "city"])
        self.assertIsNone(r.rows[0]["city"])
        self.assertIn("extra value", r.issues[0].problem)

    def test_group_totals_ignore_case(self):
        r = cr.build(cr.expand([os.path.join(SAMPLES, "*.csv")]), key="order_id")
        totals = {g: (n, t) for g, n, t in cr.group_totals(r, "region", "amount")}
        self.assertEqual(totals["North"], (4, Decimal("1292.00")))
        self.assertNotIn("north", totals)


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, "out")

    def run_cli(self, *extra, files=None):
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            code = cr.main([*(files or [os.path.join(SAMPLES, "*.csv")]), "--key", "order_id", "--out", self.out, *extra])
        self.output = out.getvalue()
        return code

    def test_cli_writes_three_files(self):
        self.assertEqual(self.run_cli("--group-by", "region", "--sum", "amount"), 0)
        self.assertIn("2 duplicate(s) removed; 8 clean row(s)", self.output)
        for name in ("clean.csv", "clean.xlsx", "report.html"):
            self.assertTrue(os.path.isfile(os.path.join(self.out, name)), name)
        with open(os.path.join(self.out, "clean.csv"), encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 8)
        html = read_text(os.path.join(self.out, "report.html"))
        self.assertIn("Total amount by region", html)
        self.assertIn("twelve", html)
        self.assertIn("1 issue(s) to review", html)

    def test_strict_fails_on_issues(self):
        self.assertEqual(self.run_cli("--strict"), 1)

    def test_bad_arguments(self):
        self.assertEqual(self.run_cli(files=["no_such_file.csv"]), 2)
        self.assertEqual(self.run_cli("--group-by", "region", "--sum", "customer"), 2)   # not numeric

    def test_xlsx_is_valid_and_typed(self):
        self.assertEqual(self.run_cli(), 0)
        with zipfile.ZipFile(os.path.join(self.out, "clean.xlsx")) as z:
            self.assertIsNone(z.testzip())
            for part in z.namelist():                                  # every part is well-formed XML
                ET.fromstring(z.read(part))
            sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
        rows = sheet.findall(".//m:sheetData/m:row", NS)
        self.assertEqual(len(rows), 9)                                 # header + 8 rows
        header = [c.find(".//m:t", NS).text for c in rows[0].findall("m:c", NS)]
        self.assertEqual(header[:5], ["order_id", "order_date", "customer", "region", "amount"])
        first = {c.get("r"): c for c in rows[1].findall("m:c", NS)}
        self.assertEqual(first["A2"].find("m:v", NS).text, "1001")      # number, not text
        self.assertIsNone(first["A2"].get("t"))
        self.assertEqual(first["B2"].get("s"), "1")                    # date style
        self.assertEqual(int(first["B2"].find("m:v", NS).text), (dt.date(2026, 1, 4) - dt.date(1899, 12, 30)).days)
        self.assertEqual(first["C2"].get("t"), "inlineStr")
        self.assertIsNotNone(sheet.find("m:autoFilter", NS))

    def test_column_letters(self):
        self.assertEqual([cr.col_letter(i) for i in (0, 25, 26, 51, 52, 701, 702)],
                         ["A", "Z", "AA", "AZ", "BA", "ZZ", "AAA"])


if __name__ == "__main__":
    unittest.main()
