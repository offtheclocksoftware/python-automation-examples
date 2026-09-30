#!/usr/bin/env python3
"""
package_release.py - build a release zip the same checked way every time. Dry run by default.

    python package_release.py path/to/app --name myapp --version 1.4.0                 # show what would happen
    python package_release.py path/to/app --name myapp --version 1.4.0 --go            # build it

Checks first (the run stops at the first failed check and says what to fix):
  - the version is MAJOR.MINOR.PATCH and higher than every release already in the output folder;
  - an existing release is never overwritten;
  - no file that looks like a secret is packed (.env, private keys, *.pem/*.pfx/*.p12, key-looking text);
  - optional: --changelog must mention the version.
Then:
  - builds myapp-1.4.0.zip with forward-slash paths under one top folder (myapp/...), in sorted order and with fixed
    timestamps, so the same input always gives the byte-identical zip (reproducible builds);
  - writes myapp-1.4.0.zip.sha256 (sha256sum format) and myapp-1.4.0.manifest.json (every file's size and SHA-256);
  - verifies the zip it just wrote: reopens it, checks the CRCs and compares every file with the source by SHA-256.
The zip is written to a temporary name first and renamed only after it verified, so a failed run leaves no half file.

Standard library only (Python 3.10+). Exit codes: 0 done (or dry run passed), 1 a check or the verify failed, 2 usage.
SPDX-License-Identifier: MIT - Copyright (c) 2026 Off the Clock Software
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import sys
import zipfile
from dataclasses import dataclass

VERSION_RE = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
DEFAULT_EXCLUDES = ["__pycache__", "*.pyc", ".git", ".gitignore", ".DS_Store", "Thumbs.db", "*.log", "*.tmp",
                    ".vs", ".idea", "node_modules", "*.bak"]
SECRET_NAMES = [".env", ".env.*", "*.pem", "*.pfx", "*.p12", "*.key", "id_rsa", "id_ed25519", "*.keystore", "*.jks",
                "credentials.json", "secrets.*"]
SECRET_TEXT = re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36}|xox[baprs]-[A-Za-z0-9-]{10,}")
FIXED_TIME = (1980, 1, 1, 0, 0, 0)            # the earliest date a zip can store; same bytes on every build
CHUNK = 1 << 20


class CheckFailed(Exception):
    pass


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def parse_version(text: str) -> tuple[int, int, int]:
    m = VERSION_RE.fullmatch(text)
    if not m:
        raise CheckFailed(f"version {text!r} is not MAJOR.MINOR.PATCH (for example 1.4.0)")
    return int(m[1]), int(m[2]), int(m[3])


def matches(rel: str, patterns: list[str]) -> bool:
    parts = rel.split("/")
    return any(fnmatch.fnmatch(p, pat) for p in parts for pat in patterns) or any(fnmatch.fnmatch(rel, pat) for pat in patterns)


@dataclass
class Entry:
    rel: str               # path inside the source folder, forward slashes
    path: str              # path on disk
    size: int


def collect(src: str, excludes: list[str]) -> list[Entry]:
    entries = []
    for dirpath, dirnames, filenames in os.walk(src):
        rel_dir = os.path.relpath(dirpath, src).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        dirnames[:] = sorted(d for d in dirnames if not matches(rel_dir + d, excludes))
        for fn in sorted(filenames):
            rel = rel_dir + fn
            if not matches(rel, excludes):
                p = os.path.join(dirpath, fn)
                entries.append(Entry(rel, p, os.path.getsize(p)))
    return sorted(entries, key=lambda e: e.rel)


def existing_versions(out_dir: str, name: str) -> list[tuple[int, int, int]]:
    found = []
    if os.path.isdir(out_dir):
        for fn in os.listdir(out_dir):
            m = re.fullmatch(re.escape(name) + r"-(\d+\.\d+\.\d+)\.zip", fn)
            if m and VERSION_RE.fullmatch(m[1]):
                found.append(parse_version(m[1]))
    return sorted(found)


def preflight(src: str, name: str, version: str, out_dir: str, entries: list[Entry], changelog: str | None) -> list[str]:
    """Runs every check; returns the list of OK lines or raises CheckFailed with the first problem."""
    ok = []
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        raise CheckFailed(f"name {name!r} may only use letters, digits, '.', '_' and '-'")
    v = parse_version(version)
    ok.append(f"version {version} is MAJOR.MINOR.PATCH")
    zip_path = os.path.join(out_dir, f"{name}-{version}.zip")
    if os.path.exists(zip_path):
        raise CheckFailed(f"{zip_path} already exists; a release is never overwritten")
    older = existing_versions(out_dir, name)
    if older and v <= older[-1]:
        raise CheckFailed(f"version {version} is not higher than the latest release in {out_dir} "
                          f"({'.'.join(map(str, older[-1]))})")
    ok.append(f"version is new ({'.'.join(map(str, older[-1])) if older else 'no earlier release'} before it)")
    if not entries:
        raise CheckFailed(f"no files to pack in {src} (after excludes)")
    secrets = [e.rel for e in entries if matches(e.rel, SECRET_NAMES)]
    for e in entries:
        if e.rel not in secrets and e.size < 5 * CHUNK:
            with open(e.path, "rb") as f:
                if SECRET_TEXT.search(f.read()):
                    secrets.append(e.rel)
    if secrets:
        raise CheckFailed("these files look like secrets and must not ship: " + ", ".join(sorted(secrets))
                          + " (remove them or add --exclude)")
    ok.append(f"no secret-looking files among {len(entries)} file(s)")
    if changelog:
        try:
            text = open(changelog, encoding="utf-8").read()
        except OSError as ex:
            raise CheckFailed(f"cannot read the changelog {changelog}: {ex.strerror}") from None
        if not re.search(r"(?<![\d.])" + re.escape(version) + r"(?![\d.])", text):
            raise CheckFailed(f"{os.path.basename(changelog)} has no entry for {version}")
        ok.append(f"{os.path.basename(changelog)} mentions {version}")
    return ok


def build_zip(entries: list[Entry], top: str, zip_path: str) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for e in entries:
            info = zipfile.ZipInfo(f"{top}/{e.rel}", date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with open(e.path, "rb") as f:
                z.writestr(info, f.read(), compresslevel=9)


def verify_zip(zip_path: str, entries: list[Entry], top: str) -> None:
    """Reopens the zip: CRCs good, exactly the expected names, and every file identical to its source."""
    with zipfile.ZipFile(zip_path) as z:
        bad = z.testzip()
        if bad:
            raise CheckFailed(f"verify: {bad} is corrupt in {zip_path}")
        names = z.namelist()
        want = [f"{top}/{e.rel}" for e in entries]
        if names != want:
            raise CheckFailed(f"verify: the zip holds {len(names)} entries, expected {len(want)}")
        if any("\\" in n for n in names):
            raise CheckFailed("verify: a path inside the zip uses a backslash")
        for e in entries:
            with z.open(f"{top}/{e.rel}") as f:
                if hashlib.sha256(f.read()).hexdigest() != sha256_file(e.path):
                    raise CheckFailed(f"verify: {e.rel} in the zip differs from the source")


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build a verified, reproducible release zip (dry run unless --go).")
    ap.add_argument("source", help="folder to package")
    ap.add_argument("--name", required=True, help="release name, also the top folder inside the zip")
    ap.add_argument("--version", required=True, help="MAJOR.MINOR.PATCH")
    ap.add_argument("--out", default="dist", help="output folder (default: dist)")
    ap.add_argument("--exclude", action="append", default=[], help="extra pattern to leave out (repeatable)")
    ap.add_argument("--changelog", help="changelog file that must mention the version")
    ap.add_argument("--go", action="store_true", help="really build (default: dry run, nothing is written)")
    a = ap.parse_args(argv)

    if not os.path.isdir(a.source):
        print(f"error: {a.source} is not a folder", file=sys.stderr)
        return 2
    entries = collect(a.source, DEFAULT_EXCLUDES + a.exclude)
    try:
        for line in preflight(a.source, a.name, a.version, a.out, entries, a.changelog):
            print(f"ok     {line}")
    except CheckFailed as ex:
        print(f"STOP   {ex}")
        return 1

    base = f"{a.name}-{a.version}"
    zip_path = os.path.join(a.out, base + ".zip")
    total = sum(e.size for e in entries)
    print(f"files  {len(entries)} file(s), {human(total)} before compression:")
    for e in entries[:25]:
        print(f"         {a.name}/{e.rel}  ({human(e.size)})")
    if len(entries) > 25:
        print(f"         ... and {len(entries) - 25} more")
    if not a.go:
        print(f"dry run: nothing was written. Run again with --go to build {zip_path} (+ .sha256, manifest).")
        return 0

    os.makedirs(a.out, exist_ok=True)
    tmp = zip_path + ".partial"
    try:
        build_zip(entries, a.name, tmp)
        verify_zip(tmp, entries, a.name)
        os.replace(tmp, zip_path)
    except CheckFailed as ex:
        print(f"STOP   {ex}")
        return 1
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    digest = sha256_file(zip_path)
    with open(zip_path + ".sha256", "w", encoding="utf-8", newline="\n") as f:
        f.write(f"{digest}  {base}.zip\n")
    manifest = {"name": a.name, "version": a.version, "zip": base + ".zip", "sha256": digest,
                "files": [{"path": f"{a.name}/{e.rel}", "size": e.size, "sha256": sha256_file(e.path)} for e in entries]}
    with open(os.path.join(a.out, base + ".manifest.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    print(f"ok     verified {len(entries)} file(s) inside the zip against the source")
    print(f"built  {zip_path}  {human(os.path.getsize(zip_path))}  sha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
