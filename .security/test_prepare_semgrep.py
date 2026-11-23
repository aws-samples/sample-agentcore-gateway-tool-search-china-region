"""Offline integrity and selection checks for the scanner rule downloader."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import httpx

SPEC = importlib.util.spec_from_file_location(
    "prepare_semgrep", Path(__file__).with_name("prepare_semgrep.py")
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / ".security").mkdir()
        self.rule = {"id": "synthetic-rule", "pattern": "pass", "languages": ["python"]}
        self.source = json.dumps({"rules": [self.rule, {"id": "unselected-rule"}]}).encode()
        self.lock = {
            "revision": "a" * 40,
            "rules": [
                {
                    "id": "synthetic-rule",
                    "path": "python/synthetic.yaml",
                    "sha256": MODULE.digest(self.source),
                    "rule_sha256": MODULE.digest(
                        json.dumps(self.rule, sort_keys=True, separators=(",", ":")).encode()
                    ),
                }
            ],
        }
        self.write_lock()
        self.requests = []

    def write_lock(self):
        (self.root / ".security" / "semgrep-rules.lock.json").write_text(json.dumps(self.lock))

    def client(self, status=200, body=None):
        def respond(request):
            self.requests.append(str(request.url))
            return httpx.Response(status, content=self.source if body is None else body)

        client = httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=False)
        self.addCleanup(client.close)
        return client

    def test_exact_selection_and_offline_reuse(self):
        client = self.client()
        output = MODULE.prepare(self.root, client)
        self.assertEqual(json.loads(output.read_text()), {"rules": [self.rule]})
        self.assertEqual(self.requests, [MODULE.ORIGIN + "/" + "a" * 40 + "/python/synthetic.yaml"])
        original = output.read_bytes()
        MODULE.prepare(self.root, client, offline=True)
        self.assertEqual(output.read_bytes(), original)
        self.assertEqual(len(self.requests), 1)

    def test_changed_source_fails_before_yaml_parsing(self):
        with self.assertRaisesRegex(ValueError, "checksum"):
            MODULE.prepare(self.root, self.client(body=b"untrusted bytes"))
        self.assertFalse((self.root / ".artifacts/semgrep-rules/selected.yaml").exists())

    def test_oversized_download_fails_before_caching(self):
        with self.assertRaisesRegex(ValueError, "download limit"):
            MODULE.prepare(self.root, self.client(body=b"x" * (MODULE.MAX_BYTES + 1)))
        cache = self.root / ".artifacts/semgrep-rules"
        self.assertFalse((cache / "synthetic-rule.source").exists())
        self.assertFalse((cache / "selected.yaml").exists())

    def test_changed_rule_definition_fails(self):
        self.lock["rules"][0]["rule_sha256"] = "0" * 64
        self.write_lock()
        with self.assertRaisesRegex(ValueError, "reviewed definition"):
            MODULE.prepare(self.root, self.client())

    def test_offline_missing_source_fails_without_network(self):
        with self.assertRaisesRegex(ValueError, "missing"):
            MODULE.prepare(self.root, self.client(), offline=True)
        self.assertEqual(self.requests, [])

    def test_corrupted_cache_removes_stale_selection(self):
        client = self.client()
        output = MODULE.prepare(self.root, client)
        (output.parent / "synthetic-rule.source").write_bytes(b"corrupted")
        with self.assertRaisesRegex(ValueError, "checksum"):
            MODULE.prepare(self.root, client, offline=True)
        self.assertFalse(output.exists())

    def test_redirect_and_http_error_fail(self):
        for status in (302, 404):
            with self.subTest(status=status), self.assertRaises(httpx.HTTPStatusError):
                MODULE.prepare(self.root, self.client(status=status))

    def test_unpinned_revision_and_parent_paths_fail(self):
        for field, value in (("revision", "main"), ("path", "../escape.yaml")):
            with self.subTest(field=field):
                if field == "revision":
                    self.lock["revision"] = value
                else:
                    self.lock["revision"] = "a" * 40
                    self.lock["rules"][0]["path"] = value
                self.write_lock()
                with self.assertRaises(ValueError):
                    MODULE.prepare(self.root, self.client())
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
