"""Tests for package_release.py (temporary folders only)."""
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "release_packager"))
import package_release as pr  # noqa: E402


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


SAMPLE_APP = os.path.join(ROOT, "release_packager", "sample_app")


class PackagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = os.path.join(self.tmp.name, "app")
        shutil.copytree(SAMPLE_APP, self.src)
        self.out = os.path.join(self.tmp.name, "dist")

    def run_cli(self, *args, version="1.4.0", src=None, out=None):
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            code = pr.main([src or self.src, "--name", "sample-app", "--version", version, "--out", out or self.out, *args])
        return code, buf.getvalue()

    def add(self, rel, data=b"x"):
        p = os.path.join(self.src, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)

    def test_dry_run_is_the_default_and_writes_nothing(self):
        code, out = self.run_cli("--changelog", os.path.join(self.src, "CHANGELOG.md"))
        self.assertEqual(code, 0, out)
        self.assertIn("dry run: nothing was written", out)
        self.assertFalse(os.path.exists(self.out))

    def test_build_writes_zip_checksum_and_manifest(self):
        code, out = self.run_cli("--go")
        self.assertEqual(code, 0, out)
        zp = os.path.join(self.out, "sample-app-1.4.0.zip")
        digest = hashlib.sha256(read_bytes(zp)).hexdigest()
        self.assertEqual(read_text(zp + ".sha256"), f"{digest}  sample-app-1.4.0.zip\n")
        manifest = json.loads(read_text(os.path.join(self.out, "sample-app-1.4.0.manifest.json")))
        self.assertEqual(manifest["sha256"], digest)
        with zipfile.ZipFile(zp) as z:
            names = z.namelist()
            self.assertEqual(names, ["sample-app/CHANGELOG.md", "sample-app/README.txt", "sample-app/app.py",
                                     "sample-app/config/settings.json"])
            self.assertTrue(all(i.date_time == (1980, 1, 1, 0, 0, 0) for i in z.infolist()))
        self.assertEqual([f["path"] for f in manifest["files"]], names)
        self.assertNotIn("notes.tmp", "".join(names))                  # default exclude
        self.assertFalse(os.path.exists(zp + ".partial"))

    def test_reproducible(self):
        other = os.path.join(self.tmp.name, "dist2")
        self.assertEqual(self.run_cli("--go")[0], 0)
        self.assertEqual(self.run_cli("--go", out=other)[0], 0)
        a = read_bytes(os.path.join(self.out, "sample-app-1.4.0.zip"))
        b = read_bytes(os.path.join(other, "sample-app-1.4.0.zip"))
        self.assertEqual(a, b)

    def test_never_overwrites_and_version_must_grow(self):
        self.assertEqual(self.run_cli("--go")[0], 0)
        before = read_bytes(os.path.join(self.out, "sample-app-1.4.0.zip"))
        code, out = self.run_cli("--go")
        self.assertEqual(code, 1)
        self.assertIn("never overwritten", out)
        self.assertEqual(read_bytes(os.path.join(self.out, "sample-app-1.4.0.zip")), before)
        code, out = self.run_cli("--go", version="1.3.9")
        self.assertEqual(code, 1)
        self.assertIn("not higher than the latest release", out)
        self.assertEqual(self.run_cli("--go", version="1.10.0")[0], 0)  # numeric, not text, comparison

    def test_bad_versions(self):
        for v in ("1.4", "v1.4.0", "01.4.0", "1.4.0-beta"):
            code, out = self.run_cli(version=v)
            self.assertEqual(code, 1, v)
            self.assertIn("MAJOR.MINOR.PATCH", out)

    def test_secret_files_stop_the_build(self):
        self.add(".env", b"DB_PASSWORD=example\n")
        code, out = self.run_cli("--go")
        self.assertEqual(code, 1)
        self.assertIn(".env", out)
        self.assertFalse(os.path.exists(os.path.join(self.out, "sample-app-1.4.0.zip")))
        self.assertEqual(self.run_cli("--go", "--exclude", ".env")[0], 0)

    def test_secret_text_inside_a_normal_file_stops_the_build(self):
        fake_key = ("-----BEGIN " + "RSA PRIVATE KEY-----\nnot a real key\n").encode()   # built at runtime on purpose
        self.add("config/deploy.txt", fake_key)
        code, out = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("config/deploy.txt", out)

    def test_changelog_must_mention_the_version(self):
        cl = os.path.join(self.src, "CHANGELOG.md")
        self.assertEqual(self.run_cli("--changelog", cl)[0], 0)
        code, out = self.run_cli("--changelog", cl, version="1.5.0")
        self.assertEqual(code, 1)
        self.assertIn("no entry for 1.5.0", out)
        self.assertEqual(self.run_cli("--changelog", cl, version="1.4.00")[0], 1)   # not a version at all
        code, out = self.run_cli("--changelog", cl, version="1.3.0")    # only "1.3.2" is there
        self.assertEqual(code, 1)
        self.assertIn("no entry for 1.3.0", out)

    def test_excludes_and_nested_paths_use_forward_slashes(self):
        self.add("__pycache__/app.cpython-312.pyc")
        self.add("data/deep/file.bin", b"\x00\x01")
        self.add("data/debug.log")
        entries = pr.collect(self.src, pr.DEFAULT_EXCLUDES)
        rels = [e.rel for e in entries]
        self.assertIn("data/deep/file.bin", rels)
        self.assertFalse(any("__pycache__" in r or r.endswith(".log") for r in rels))

    def test_verify_catches_a_changed_file(self):
        entries = pr.collect(self.src, pr.DEFAULT_EXCLUDES)
        zp = os.path.join(self.tmp.name, "t.zip")
        pr.build_zip(entries, "sample-app", zp)
        pr.verify_zip(zp, entries, "sample-app")                       # passes as built
        with open(os.path.join(self.src, "app.py"), "a", encoding="utf-8") as f:
            f.write("# changed after the build\n")
        with self.assertRaises(pr.CheckFailed):
            pr.verify_zip(zp, entries, "sample-app")

    def test_empty_source_and_missing_folder(self):
        empty = os.path.join(self.tmp.name, "empty")
        os.makedirs(empty)
        self.assertEqual(self.run_cli(src=empty)[0], 1)
        self.assertEqual(self.run_cli(src=os.path.join(self.tmp.name, "nope"))[0], 2)


if __name__ == "__main__":
    unittest.main()
