"""Offline checks for the disposable recorder and its committed evidence."""

import importlib.util
import io
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "forgejo"
SPEC = importlib.util.spec_from_file_location("forgejo_record", FIXTURES / "record.py")
record = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(record)


class RecorderTests(unittest.TestCase):
    def test_preflight_refuses_wrong_version_identity_and_existing_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            for actor in ("author", "approver", "other"):
                (path / actor).write_text("test-secret")
            args = SimpleNamespace(
                base_url="http://127.0.0.1", output=path / "output",
                repository="issue54-test", author_token_file=path / "author",
                approver_token_file=path / "approver", other_token_file=path / "other")
            for failure in ("version", "author", "approver", "other", "existing"):
                calls = []

                def respond(experiment, name, method, endpoint, actor="author", **kwargs):
                    calls.append(method)
                    if endpoint == "/version":
                        version = "15.0.0" if failure == "version" else "16.0.3+gitea-1.22.0"
                        return {"version": version}
                    if endpoint == "/user":
                        return {"login": "unexpected" if failure == actor else actor}
                    return {}

                fake = SimpleNamespace(request=respond, last_status=200)
                with self.subTest(failure=failure), patch.object(
                        record, "Recorder", return_value=fake):
                    with self.assertRaises(RuntimeError):
                        record.run(args)
                self.assertTrue(calls)
                self.assertEqual(set(calls), {"GET"})

    def test_redaction_preserves_protocol_fields_without_mutating_input(self):
        original = {"id": 31, "state": "COMMENT", "stale": False,
                    "rows": [{"email": "alice" + "@example.invalid",
                              "token": "private", "cookie": "session",
                              "body": "opaque-secret\nContact alice@example.invalid",
                              "url": "https://forge.invalid/api/v1/repos/a/b"}],
                    "commit_id": "1" * 40, "position": 2, "extra_lines_count": 2}
        result = record.scrub(original, ("opaque-secret",))
        self.assertEqual(result["commit_id"], original["commit_id"])
        self.assertEqual(result["id"], 31)
        self.assertIs(result["stale"], False)
        self.assertEqual(result["rows"][0]["body"], "<redacted>\nContact <redacted>")
        self.assertEqual(result["rows"][0]["url"], "http://127.0.0.1/api/v1/repos/a/b")
        self.assertEqual(result["rows"][0]["cookie"], "<redacted>")
        self.assertEqual(original["rows"][0]["token"], "private")

    def test_base_url_rejects_credentials_redirect_like_paths_and_remote_http(self):
        for url in ("http://forge.invalid", "https://name:secret@forge.invalid",
                    "file:///tmp/test", "https://forge.invalid/path",
                    "https://forge.invalid?secret=x", "https://forge.invalid#x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                record.validate_base_url(url)
        self.assertEqual(record.validate_base_url("http://127.0.0.1:18554/"),
                         "http://127.0.0.1:18554")
        self.assertEqual(record.validate_base_url("https://forge.invalid"),
                         "https://forge.invalid")

    def test_redirect_cannot_forward_authorization(self):
        with self.assertRaisesRegex(RuntimeError, "redirect refused"):
            record.NoRedirect().redirect_request(None, None, 302, "", {},
                                                  "https://elsewhere.invalid")

    def test_recorder_refuses_existing_output_and_blank_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            with self.assertRaises(FileExistsError):
                record.Recorder("http://127.0.0.1", {"author": "fake"}, path)
            with self.assertRaises(ValueError):
                record.Recorder("http://127.0.0.1", {"author": ""}, path / "new")
            self.assertFalse((path / "new").exists())

    def test_error_response_is_recorded_before_required_request_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = record.Recorder("http://127.0.0.1", {"author": "test-secret"},
                                       Path(tmp) / "records")
            error = urllib.error.HTTPError("http://127.0.0.1", 405, "refused", {},
                                           io.BytesIO(b'{"message":"refused"}'))
            with patch.object(recorder.opener, "open", side_effect=error) as request:
                with self.assertRaisesRegex(RuntimeError, "HTTP 405"):
                    recorder.request("E7", "refusal", "POST", "/merge",
                                     body={"head_commit_id": "1" * 40}, required=True)
            sent = request.call_args.args[0]
            self.assertEqual(sent.get_header("Authorization"), "token test-secret")
            saved = json.loads(next(recorder.output.glob("*.json")).read_text())
            self.assertEqual(saved["response"], {"status": 405,
                                                 "body": {"message": "refused"}})
            self.assertNotIn("test-secret", json.dumps(saved))

    def test_committed_evidence_is_indexed_and_scrubbed(self):
        experiments = set()
        recordings = list((FIXTURES / "recordings").rglob("[0-9]*.json"))
        self.assertGreaterEqual(len(recordings), 75)
        for path in recordings:
            with self.subTest(path=path.name):
                text = path.read_text()
                payload = json.loads(text)
                experiments.add(payload["experiment"])
                self.assertIn(f"]({path.name})", (path.parent / "README.md").read_text())
                self.assertIsInstance(payload["response"]["status"], int)
                self.assertNotRegex(text, r"[\w.+-]+@[\w.-]+")
                for url in re.findall(r'https?://[^\s"<>]+', text):
                    self.assertEqual(urllib.parse.urlsplit(url).hostname, "127.0.0.1")
                self.assertNotIn('"Authorization"', text)
                self.assertNotIn('"Set-Cookie"', text)
        self.assertTrue({f"E{n}" for n in range(1, 10)} <= experiments)


if __name__ == "__main__":
    unittest.main()
