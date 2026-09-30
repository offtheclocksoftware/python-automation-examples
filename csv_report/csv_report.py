#!/usr/bin/env python3
"""
csv_report.py - merge messy CSV exports into one clean table, an Excel file and an HTML report.

    python csv_report.py "sample_data/*.csv" --key order_id --group-by region --sum amount --out report_out

What it does, in order:
  1. Reads every file (UTF-8 with or without BOM, falls back to Windows-1252) and detects the delimiter (, ; tab |).
  2. Normalizes headers ("Order ID", "order-id " -> order_id) so files with different column orders or names merge.
  3. Cleans values: trims spaces, turns N/A / null / - into blanks, reads numbers like "$1,234.50" or "(5.00)" and
     dates in the common formats. A column counts as numeric or date when at least 80% of its filled values parse;
     the values that don't parse are listed as issues instead of being guessed.
  4. Removes duplicates: by --key (the last file wins, so a newer export overrides an older one) or exact duplicates.
  5. Writes clean.csv, clean.xlsx (real numbers and dates, bold header, filter, frozen top row) and report.html.

Standard library only (Python 3.10+). Exit codes: 0 done, 1 issues found with --strict, 2 usage or input error.
SPDX-License-Identifier: MIT - Copyright (c) 2026 Off the Clock Software
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import glob
import html
import io
import os
import re
import sys
import zipfile
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from xml.sax.saxutils import escape as xml_escape

BLANKS = {"", "n/a", "na", "null", "none", "-", "--", "?"}
DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d.%m.%Y", "%b %d %Y", "%d %b %Y", "%Y-%m-%dT%H:%M:%S")
DAYFIRST_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d.%m.%Y", "%b %d %Y", "%d %b %Y", "%Y-%m-%dT%H:%M:%S")
TYPE_THRESHOLD = 0.8
MAX_ISSUES_SHOWN = 50


# --------------------------------------------------------------------------------------------- reading and cleaning
def normalize_header(name: str) -> str:
    """'  Order ID ' -> 'order_id'; 'Unit-Price ($)' -> 'unit_price'."""
    name = re.sub(r"[^0-9a-zA-Z]+", "_", name.strip().lower())
    return name.strip("_") or "column"


def clean_text(value: str | None) -> str:
    if value is None:
        return ""
    value = " ".join(value.split())
    return "" if value.lower() in BLANKS else value


def parse_number(text: str) -> Decimal | None:
    """'$1,234.50' -> 1234.50, '(5.00)' and '-$5.00' -> -5.00, '12%' -> 12. None if it is not a number.
    Decimal commas from European exports work too: '120,50' -> 120.50 and '1.250,00' -> 1250.00 ('1,250' stays 1250).
    Values with a leading zero ('01234', a ZIP code or an ID) are text on purpose, so the zero is never lost."""
    t = text.strip()
    negative = t.startswith("(") and t.endswith(")")
    t = t.strip("()").replace(" ", "")
    t = re.sub(r"^([-+]?)[$€£]", r"\1", t)
    t = re.sub(r"[$€£%]$", "", t)
    if "," in t and "." in t and t.rfind(",") > t.rfind("."):
        t = t.replace(".", "").replace(",", ".")             # 1.250,00
    elif "." not in t and re.fullmatch(r"[-+]?\d+,\d{1,2}", t):
        t = t.replace(",", ".")                                # 120,50
    else:
        t = t.replace(",", "")                                 # 1,250.00
    if not re.fullmatch(r"[-+]?\d+(\.\d+)?", t) or re.match(r"[-+]?0\d", t):
        return None
    try:
        n = Decimal(t)
    except InvalidOperation:
        return None
    return -n if negative else n


def parse_date(text: str, dayfirst: bool = False) -> dt.date | None:
    for fmt in DAYFIRST_FORMATS if dayfirst else DATE_FORMATS:
        try:
            return dt.datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    return None


def read_text(path: str) -> str:
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def read_csv(path: str) -> tuple[list[str], list[list[str]]]:
    """Returns (original headers, rows). Detects the delimiter; skips fully empty lines."""
    text = read_text(path)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter) if any(c.strip() for c in r)]
    if not rows:
        return [], []
    return rows[0], rows[1:]


# --------------------------------------------------------------------------------------------- the model
@dataclass
class Issue:
    file: str
    line: int
    column: str
    value: str
    problem: str


@dataclass
class Result:
    columns: list[str]
    types: dict[str, str]                          # column -> "number" | "date" | "text"
    rows: list[dict[str, object]]                  # clean rows (Decimal / date / str / None)
    files: list[tuple[str, int]]                   # (file name, data rows read)
    rows_in: int = 0
    duplicates_removed: int = 0
    issues: list[Issue] = field(default_factory=list)


def build(paths: list[str], key: str | None = None, dayfirst: bool = False) -> Result:
    raw_rows: list[tuple[str, int, dict[str, str]]] = []      # (file, line number, {column: text})
    columns: list[str] = []
    files: list[tuple[str, int]] = []
    issues: list[Issue] = []

    for path in paths:
        headers, rows = read_csv(path)
        name = os.path.basename(path)
        cols = []
        for h in headers:
            c = normalize_header(h)
            while c in cols:                                   # two headers that normalize alike: keep both
                c += "_2"
            cols.append(c)
        for c in cols:
            if c not in columns:
                columns.append(c)
        for i, r in enumerate(rows, start=2):
            if len(r) > len(cols):
                issues.append(Issue(name, i, "", ",".join(r[len(cols):]), f"{len(r) - len(cols)} extra value(s) ignored"))
            raw_rows.append((name, i, {c: clean_text(r[j]) if j < len(r) else "" for j, c in enumerate(cols)}))
        files.append((name, len(rows)))

    if key:
        key = normalize_header(key)
        if key not in columns:
            raise ValueError(f"key column '{key}' not found; columns are: {', '.join(columns)}")

    # Column types by majority: numbers first (a column of years is numeric, not dates), then dates, else text.
    types: dict[str, str] = {}
    for c in columns:
        filled = [v[c] for _, _, v in raw_rows if v.get(c)]
        if not filled:
            types[c] = "text"
        elif sum(parse_number(x) is not None for x in filled) >= TYPE_THRESHOLD * len(filled):
            types[c] = "number"
        elif sum(parse_date(x, dayfirst) is not None for x in filled) >= TYPE_THRESHOLD * len(filled):
            types[c] = "date"
        else:
            types[c] = "text"

    clean: list[dict[str, object]] = []
    for name, line, values in raw_rows:
        row: dict[str, object] = {}
        for c in columns:
            text = values.get(c, "")
            if not text:
                row[c] = None
            elif types[c] == "number":
                row[c] = parse_number(text)
                if row[c] is None:
                    issues.append(Issue(name, line, c, text, "not a number (left blank)"))
            elif types[c] == "date":
                row[c] = parse_date(text, dayfirst)
                if row[c] is None:
                    issues.append(Issue(name, line, c, text, "not a date (left blank)"))
            else:
                row[c] = text
        if key and row.get(key) is None:
            issues.append(Issue(name, line, key, "", "missing key (row dropped)"))
            continue
        clean.append(row)

    rows_in = len(clean)
    if key:
        by_key: dict[object, dict[str, object]] = {}
        for row in clean:                  # a later file's row replaces an earlier one but keeps its position
            k = row[key]
            by_key[k.lower() if isinstance(k, str) else k] = row
        deduped = list(by_key.values())
    else:
        seen, deduped = set(), []
        for row in clean:
            sig = tuple(str(row[c]) for c in columns)
            if sig not in seen:
                seen.add(sig)
                deduped.append(row)
    return Result(columns, types, deduped, files, rows_in, rows_in - len(deduped), issues)


# --------------------------------------------------------------------------------------------- writers
def fmt(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)


def write_csv(result: Result, path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(result.columns)
        for row in result.rows:
            w.writerow([fmt(row[c]) for c in result.columns])


def col_letter(n: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


EXCEL_EPOCH = dt.date(1899, 12, 30)


def write_xlsx(result: Result, path: str, sheet: str = "Clean data") -> None:
    """A minimal but valid .xlsx (Office Open XML) with the standard library: numbers as numbers, dates as dates."""
    def cell(ref: str, value: object, header: bool = False) -> str:
        if value is None:
            return ""
        if header:
            return f'<c r="{ref}" t="inlineStr" s="2"><is><t>{xml_escape(str(value))}</t></is></c>'
        if isinstance(value, Decimal):
            return f'<c r="{ref}"><v>{value}</v></c>'
        if isinstance(value, dt.date):
            return f'<c r="{ref}" s="1"><v>{(value - EXCEL_EPOCH).days}</v></c>'
        return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{xml_escape(str(value))}</t></is></c>'

    rows_xml = []
    for r, values in enumerate([result.columns] + [[row[c] for c in result.columns] for row in result.rows], start=1):
        cells = "".join(cell(f"{col_letter(i)}{r}", v, header=(r == 1)) for i, v in enumerate(values))
        rows_xml.append(f'<row r="{r}">{cells}</row>')
    last = f"{col_letter(max(len(result.columns), 1) - 1)}{len(result.rows) + 1}"
    widths = "".join(
        f'<col min="{i + 1}" max="{i + 1}" width="{min(max(len(c), *(len(fmt(row[c])) for row in result.rows), 8) + 2, 60)}" customWidth="1"/>'
        if result.rows else f'<col min="{i + 1}" max="{i + 1}" width="{len(c) + 2}" customWidth="1"/>'
        for i, c in enumerate(result.columns))
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '</sheetView></sheetViews>'
        f'<cols>{widths}</cols><sheetData>{"".join(rows_xml)}</sheetData>'
        f'<autoFilter ref="A1:{last}"/></worksheet>')
    parts = {
        "[Content_Types].xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '</Types>',
        "_rels/.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>',
        "xl/workbook.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets><sheet name="{xml_escape(sheet)}" sheetId="1" r:id="rId1"/></sheets>'
            f'<definedNames><definedName name="_xlnm._FilterDatabase" localSheetId="0" hidden="1">\'{xml_escape(sheet)}\'!$A$1:${last.rstrip("0123456789")}${len(result.rows) + 1}</definedName></definedNames>'
            '</workbook>',
        "xl/_rels/workbook.xml.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '</Relationships>',
        "xl/styles.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy-mm-dd"/></numFmts>'
            '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
            '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
            '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
            '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
            '</styleSheet>',
        "xl/worksheets/sheet1.xml": sheet_xml,
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in parts.items():
            z.writestr(name, text)


def column_stats(result: Result) -> list[tuple[str, str, str, str]]:
    """(column, type, filled, summary) for the report."""
    out = []
    n = len(result.rows) or 1
    for c in result.columns:
        vals = [r[c] for r in result.rows if r[c] is not None]
        filled = f"{len(vals)}/{len(result.rows)} ({100 * len(vals) // n}%)"
        t = result.types[c]
        if not vals:
            summary = "empty"
        elif t == "number" and (c == "id" or c.endswith("_id")):      # an ID column: a total means nothing
            summary = f"{min(vals)} to {max(vals)}"
        elif t == "number":
            summary = f"min {min(vals)} - max {max(vals)} - total {sum(vals)}"
        elif t == "date":
            summary = f"{min(vals).isoformat()} to {max(vals).isoformat()}"
        else:
            summary = f"{len(set(vals))} distinct"
        out.append((c, t, filled, summary))
    return out


def group_totals(result: Result, group_by: str, sum_col: str) -> list[tuple[str, int, Decimal]]:
    """(group, rows, total) sorted by total. Groups ignore case ("north" = "North"); the most common spelling is shown."""
    totals: dict[str, list] = {}
    spellings: dict[str, dict[str, int]] = {}
    for r in result.rows:
        g = fmt(r[group_by]) or "(blank)"
        spellings.setdefault(g.casefold(), {}).setdefault(g, 0)
        spellings[g.casefold()][g] += 1
        t = totals.setdefault(g.casefold(), [0, Decimal(0)])
        t[0] += 1
        if isinstance(r[sum_col], Decimal):
            t[1] += r[sum_col]
    named = [(max(spellings[k].items(), key=lambda kv: (kv[1], kv[0]))[0], n, s) for k, (n, s) in totals.items()]
    return sorted(named, key=lambda x: (-x[2], x[0]))


def write_html(result: Result, path: str, title: str, groups: list[tuple[str, int, Decimal]] | None,
               group_label: tuple[str, str] | None) -> None:
    e = html.escape
    status = "No issues" if not result.issues else f"{len(result.issues)} issue(s) to review"
    cards = [("Files", len(result.files)), ("Rows read", result.rows_in), ("Duplicates removed", result.duplicates_removed),
             ("Clean rows", len(result.rows)), ("Issues", len(result.issues))]
    parts = [f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{e(title)}</title>
<style>
body{{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:24px;color:#1d1b2e;background:#fafafa}}
h1{{margin:0 0 4px}} .sub{{color:#666;margin-bottom:20px}}
.cards{{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:24px}}
.card{{background:#fff;border:1px solid #ddd;border-radius:8px;padding:12px 16px;min-width:120px}}
.card b{{display:block;font-size:26px}} table{{border-collapse:collapse;background:#fff;margin-bottom:24px}}
th,td{{border:1px solid #ddd;padding:6px 10px;text-align:left}} th{{background:#f0eef7}}
td.num{{text-align:right;font-variant-numeric:tabular-nums}} .ok{{color:#1b7a3a}} .warn{{color:#b35900}}
</style></head><body>
<h1>{e(title)}</h1><div class="sub">Generated {dt.datetime.now().strftime('%Y-%m-%d %H:%M')} &middot;
<span class="{'ok' if not result.issues else 'warn'}">{e(status)}</span></div>
<div class="cards">{''.join(f'<div class="card"><b>{v}</b>{e(k)}</div>' for k, v in cards)}</div>
<h2>Files</h2><table><tr><th>File</th><th>Data rows</th></tr>
{''.join(f'<tr><td>{e(f)}</td><td class="num">{n}</td></tr>' for f, n in result.files)}</table>"""]
    if groups is not None and group_label:
        g, s = group_label
        parts.append(f"<h2>Total {e(s)} by {e(g)}</h2><table><tr><th>{e(g)}</th><th>Rows</th><th>Total {e(s)}</th></tr>"
                     + "".join(f'<tr><td>{e(k)}</td><td class="num">{n}</td><td class="num">{t:,.2f}</td></tr>'
                               for k, n, t in groups) + "</table>")
    parts.append("<h2>Columns</h2><table><tr><th>Column</th><th>Type</th><th>Filled</th><th>Summary</th></tr>"
                 + "".join(f"<tr><td>{e(c)}</td><td>{e(t)}</td><td>{e(f)}</td><td>{e(s)}</td></tr>"
                           for c, t, f, s in column_stats(result)) + "</table>")
    if result.issues:
        shown = result.issues[:MAX_ISSUES_SHOWN]
        more = len(result.issues) - len(shown)
        parts.append("<h2>Issues</h2><table><tr><th>File</th><th>Line</th><th>Column</th><th>Value</th><th>Problem</th></tr>"
                     + "".join(f'<tr><td>{e(i.file)}</td><td class="num">{i.line}</td><td>{e(i.column)}</td>'
                               f'<td>{e(i.value)}</td><td>{e(i.problem)}</td></tr>' for i in shown)
                     + "</table>" + (f"<p>{more} more in the console output.</p>" if more else ""))
    parts.append("</body></html>\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))


# --------------------------------------------------------------------------------------------- command line
def expand(patterns: list[str]) -> list[str]:
    """Expands wildcards itself, so `*.csv` also works in shells that don't (Windows cmd, PowerShell)."""
    out: list[str] = []
    for p in patterns:
        matches = sorted(glob.glob(p)) if any(ch in p for ch in "*?[") else [p]
        out.extend(m for m in matches if m not in out)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Merge and clean CSV files into clean.csv, clean.xlsx and report.html.")
    ap.add_argument("files", nargs="+", help="CSV files or wildcards (data/*.csv), oldest first; wildcards expand in name order")
    ap.add_argument("--out", default="report_out", help="output folder (default: report_out)")
    ap.add_argument("--key", help="column that identifies a record; duplicates keep the last file's row")
    ap.add_argument("--group-by", help="column to total by in the report (use with --sum)")
    ap.add_argument("--sum", dest="sum_col", help="numeric column to total per --group-by value")
    ap.add_argument("--dayfirst", action="store_true", help="read 03/04/2026 as 3 April (default: March 4)")
    ap.add_argument("--title", default="CSV cleanup report")
    ap.add_argument("--strict", action="store_true", help="exit code 1 when any issue was found")
    a = ap.parse_args(argv)

    paths = expand(a.files)
    missing = [p for p in paths if not os.path.isfile(p)]
    if not paths or missing:
        print(f"error: no such file(s): {', '.join(missing) or ' '.join(a.files)}", file=sys.stderr)
        return 2
    try:
        result = build(paths, key=a.key, dayfirst=a.dayfirst)
    except ValueError as ex:
        print(f"error: {ex}", file=sys.stderr)
        return 2

    groups = label = None
    if a.group_by or a.sum_col:
        g, s = normalize_header(a.group_by or ""), normalize_header(a.sum_col or "")
        if g not in result.columns or s not in result.columns or result.types.get(s) != "number":
            print("error: --group-by needs an existing column and --sum a numeric one", file=sys.stderr)
            return 2
        groups, label = group_totals(result, g, s), (g, s)

    os.makedirs(a.out, exist_ok=True)
    write_csv(result, os.path.join(a.out, "clean.csv"))
    write_xlsx(result, os.path.join(a.out, "clean.xlsx"))
    write_html(result, os.path.join(a.out, "report.html"), a.title, groups, label)

    print(f"read {result.rows_in} row(s) from {len(result.files)} file(s); {result.duplicates_removed} duplicate(s) removed; "
          f"{len(result.rows)} clean row(s)")
    print("column types: " + ", ".join(f"{c}={t}" for c, t in result.types.items()))
    for i in result.issues:
        print(f"issue  {i.file}:{i.line}  {i.column or '-'}  {i.value!r}  {i.problem}")
    print(f"wrote {a.out}/clean.csv, clean.xlsx, report.html")
    return 1 if (a.strict and result.issues) else 0


if __name__ == "__main__":
    sys.exit(main())
