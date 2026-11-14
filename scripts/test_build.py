"""Tests for build.py, offline. Run: python -m unittest discover -s scripts"""

import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402

MANIFEST = {
    "id": "sample-plugin",
    "name": "Sample Plugin",
    "version": "1.0.0",
    "description": "Used by the tests.",
    "author": "Tellus Consulting LLC",
    "homepage": "https://litus.tellusai.com/",
    "license": "MIT",
    "categories": ["Data"],
    "permissions": ["network"],
    "minLitusVersion": "0.1.0",
    "runtime": "process",
    "python": {"version": ">=3.12", "requirements": "requirements.txt"},
    "run": ["python", "main.py"],
    "i18n": {"de": {"Sample Plugin": "Beispiel", "Count\nLayers": "Ebenen\nzählen"}},
}


def package(manifest: dict, folder: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(folder + "plugin.json", json.dumps(manifest))
        zf.writestr(folder + "main.py", "print('hello')\n")
    return buf.getvalue()


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.reg, self.cache, self.site = root / "registry", root / "cache", root / "site"
        self.reg.mkdir()
        self.cache.mkdir()
        self.blocklist = root / "blocklist.json"
        self.blocklist.write_text('{ "blocked": [] }\n')
        for name, value in (("REGISTRY_DIR", self.reg), ("CACHE_DIR", self.cache),
                            ("BLOCKLIST", self.blocklist)):
            p = mock.patch.object(build, name, value)
            p.start()
            self.addCleanup(p.stop)
        # No test reaches the network: a download is a failure.
        p = mock.patch.object(build, "download", side_effect=AssertionError("network"))
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, manifest: dict, pid: str | None = None, **extra) -> bytes:
        """Puts a package in the cache and its entry in the registry."""
        data = package(manifest)
        sha = hashlib.sha256(data).hexdigest()
        (self.cache / f"{sha}.litusplugin").write_bytes(data)
        pid = pid or manifest["id"]
        entry = {"id": pid, "source": {"url": f"https://example.com/{pid}.litusplugin",
                                       "sha256": sha}, **extra}
        (self.reg / f"{pid}.json").write_text(json.dumps(entry))
        return data

    def published(self):
        return json.loads((self.site / "api/plugins/registry.json").read_text("utf-8"))

    def test_entry_comes_from_the_manifest(self):
        data = self.add(MANIFEST)
        build.run(None, self.site, None)
        entry = self.published()["plugins"][0]
        self.assertEqual(entry["python"], ">=3.12")
        self.assertEqual(entry["i18n"], {"de": {"Sample Plugin": "Beispiel"}})
        self.assertNotIn("run", entry)
        self.assertEqual(entry["package"], "packages/sample-plugin/sample-plugin-1.0.0.litusplugin")
        self.assertEqual(entry["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(entry["size"], len(data))
        copied = self.site / "api/plugins" / entry["package"]
        self.assertEqual(copied.read_bytes(), data)
        self.assertEqual((self.site / "api/plugins/blocklist.json").read_text(),
                         '{ "blocked": [] }\n')

    def test_entry_categories_override_the_manifest(self):
        self.add(MANIFEST, categories=["Coastal"])
        self.assertEqual(build.run(None, None, None)[0]["categories"], ["Coastal"])

    def test_manifest_inside_a_folder_and_no_translations_left(self):
        m = {"id": "wasm-tool", "name": "Wasm Tool", "version": "0.2.0",
             "i18n": {"fr": {"Other": "Autre"}}}
        data = package(m, folder="wasm-tool/")
        sha = hashlib.sha256(data).hexdigest()
        (self.cache / f"{sha}.litusplugin").write_bytes(data)
        (self.reg / "wasm-tool.json").write_text(json.dumps(
            {"id": "wasm-tool", "source": {"url": "https://example.com/w", "sha256": sha}}))
        entry = build.run(None, None, None)[0]
        self.assertNotIn("runtime", entry)
        self.assertNotIn("i18n", entry)

    def test_new_version_keeps_the_old_package(self):
        self.add(MANIFEST)
        build.run(None, self.site, None)
        self.add(dict(MANIFEST, version="1.1.0"))
        build.run(None, self.site, None)
        self.assertEqual([p["version"] for p in self.published()["plugins"]], ["1.1.0"])
        self.assertEqual(sorted(p.name for p in (self.site / "api/plugins/packages/sample-plugin").iterdir()),
                         ["sample-plugin-1.0.0.litusplugin", "sample-plugin-1.1.0.litusplugin"])

    def test_a_published_package_never_changes(self):
        self.add(MANIFEST)
        build.run(None, self.site, None)
        self.add(dict(MANIFEST, description="Changed, same version."))
        with self.assertRaisesRegex(build.BuildError, "new version"):
            build.run(None, self.site, None)

    def test_refuses_what_litus_would_skip_and_writes_nothing(self):
        cases = {
            "bad version": dict(MANIFEST, version="1.0"),
            "bad permission": dict(MANIFEST, permissions=["root"]),
            "bad homepage": dict(MANIFEST, homepage="litus.tellusai.com"),
            "bad runtime": dict(MANIFEST, runtime="docker"),
            "python without process": dict(MANIFEST, runtime="skills"),
            "bad python": dict(MANIFEST, python="python3"),
            "missing name": {k: v for k, v in MANIFEST.items() if k != "name"},
            "id differs from the entry": dict(MANIFEST, id="other-plugin"),
        }
        for label, manifest in cases.items():
            with self.subTest(label):
                for f in self.reg.iterdir():
                    f.unlink()
                self.add(manifest, pid="sample-plugin")
                with self.assertRaises(build.BuildError):
                    build.run(None, self.site, None)
                self.assertFalse(self.site.exists())

    def test_entry_file_rules(self):
        sha = "a" * 64
        cases = {
            "wrong file name": ("other.json", {"id": "sample", "source": {"url": "https://x", "sha256": sha}}),
            "http source": ("sample.json", {"id": "sample", "source": {"url": "http://x", "sha256": sha}}),
            "upper-case hash": ("sample.json", {"id": "sample", "source": {"url": "https://x", "sha256": "A" * 64}}),
            "unknown field": ("sample.json", {"id": "sample", "name": "x", "source": {"url": "https://x", "sha256": sha}}),
            "bad id": ("Sample.json", {"id": "Sample", "source": {"url": "https://x", "sha256": sha}}),
        }
        for label, (name, entry) in cases.items():
            with self.subTest(label):
                path = self.reg / name
                path.write_text(json.dumps(entry))
                self.assertTrue(build.load_source(path)[1])
                path.unlink()

    def test_hash_mismatch_is_refused(self):
        data = package(MANIFEST)
        with mock.patch.object(build, "download", return_value=data):
            with self.assertRaisesRegex(build.BuildError, "SHA-256"):
                build.fetch_package({"url": "https://example.com/p", "sha256": "b" * 64})

    def test_live_package_with_other_contents_is_refused(self):
        self.add(MANIFEST)
        with mock.patch.object(build, "download", return_value=b"something else"):
            with self.assertRaisesRegex(build.BuildError, "new version"):
                build.run(None, None, "https://live.example/")
        with mock.patch.object(build, "download", return_value=None):  # not published yet
            build.run(None, None, "https://live.example/")

    def test_only_the_given_entries_are_downloaded(self):
        self.add(MANIFEST)
        (self.reg / "broken.json").write_text(json.dumps(
            {"id": "broken", "source": {"url": "https://x", "sha256": "c" * 64}}))
        self.assertEqual([e["id"] for e in build.run(["sample-plugin"], None, None)],
                         ["sample-plugin"])
        with mock.patch.object(build, "download", return_value=None):  # 404
            with self.assertRaisesRegex(build.BuildError, "not found"):
                build.run(None, None, None)

    def test_package_too_large_for_litus(self):
        data = self.add(MANIFEST)
        with mock.patch.object(build, "MAX_PACKAGE_BYTES", len(data) - 1):
            with self.assertRaisesRegex(build.BuildError, "at most"):
                build.run(None, None, None)

    def test_site_too_large_for_github_pages(self):
        data = self.add(MANIFEST)
        (self.site / "images").mkdir(parents=True)
        (self.site / "images/big.png").write_bytes(b"x" * 1000)
        (self.site / ".git").mkdir()
        (self.site / ".git/pack").write_bytes(b"x" * 10_000)  # not published: not counted
        limit = 1000 + len(data) + 2000  # the page, the package and room for registry.json
        with mock.patch.object(build, "MAX_SITE_BYTES", limit):
            build.run(None, self.site, None)
        self.add(dict(MANIFEST, version="1.1.0"))
        with mock.patch.object(build, "MAX_SITE_BYTES", limit):
            with self.assertRaisesRegex(build.BuildError, "GitHub Pages"):
                build.run(None, self.site, None)

    def test_blocklist_rules(self):
        good = {"blocked": [{"id": "x", "reason": "Security issue"},
                            {"id": "y", "sha256": "d" * 64, "reason": "Broken"}]}
        self.blocklist.write_text(json.dumps(good))
        self.assertEqual(build.load_blocklist(self.blocklist)[1], [])
        for bad in ({"blocked": [{"id": "x"}]}, {"blocked": [{"id": "x", "reason": "r", "sha256": "z"}]},
                    {"blocked": {}}, []):
            with self.subTest(bad):
                self.blocklist.write_text(json.dumps(bad))
                self.assertTrue(build.load_blocklist(self.blocklist)[1])

    def test_python_versions_litus_accepts(self):
        base = {"id": "p", "name": "P", "version": "1.0.0", "package": "packages/p.litusplugin",
                "sha256": "e" * 64, "runtime": "process"}
        for v in (">=3.12", ">= 3.12", "3.12", "3.12.4", "3"):
            self.assertEqual(build.validate_entry(dict(base, python=v), "p"), [], v)
        for v in ("3.12.4.1", "2.7", ">3.12", "latest"):
            self.assertTrue(build.validate_entry(dict(base, python=v), "p"), v)


class RepositoryTests(unittest.TestCase):
    def test_the_repository_entries_are_well_formed(self):
        _, errors = build.load_sources()
        self.assertEqual(errors, [])
        self.assertEqual(build.load_blocklist()[1], [])


if __name__ == "__main__":
    unittest.main()
