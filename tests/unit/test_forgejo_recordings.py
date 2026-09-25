"""Offline checks for the disposable recorder and its committed evidence."""

import importlib.util
import io
import json
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
import urllib.request
import urllib.response


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "forgejo"
SPEC = importlib.util.spec_from_file_location(
    "forgejo_record", FIXTURES / "record.py")
record = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(record)


class RecorderTests(unittest.TestCase):
    def test_preflight_refuses_wrong_version_identity_and_existing_repository(
            self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            for actor in ("author", "approver", "other"):
                (path / actor).write_text("test-secret")
            args = SimpleNamespace(
                base_url="http://127.0.0.1",
                output=path / "output",
                repository="issue54-test",
                author_token_file=path / "author",
                approver_token_file=path / "approver",
                other_token_file=path / "other")
            cases = [(record.run, failure) for failure in
                     ("version", "author", "approver", "other", "existing")]
            cases += [(record.guard_probe, failure) for failure in
                      ("version", "author", "approver")]
            for phase, failure in cases:
                calls = []

                def respond(
                        experiment,
                        name,
                        method,
                        endpoint,
                        actor="author",
                        **kwargs):
                    calls.append(method)
                    self.assertEqual(
                        method, "GET", "preflight allowed a write")
                    if endpoint == "/version":
                        version = ("15.0.0" if failure == "version"
                                   else "16.0.3+gitea-1.22.0")
                        return {"version": version}
                    if endpoint == "/user":
                        return {
                            "login": ("unexpected" if failure == actor
                                      else actor)}
                    return {}

                status = 200 if failure == "existing" else 404
                fake = SimpleNamespace(request=respond, last_status=status)
                expected = {"version": "exactly Forgejo 16.0.3",
                            "existing": "new repository name"}.get(
                                failure, f"disposable login {failure}$")
                with (self.subTest(phase=phase.__name__, failure=failure),
                      patch.object(record, "Recorder", return_value=fake)):
                    with self.assertRaisesRegex(RuntimeError, expected):
                        phase(args)
                self.assertTrue(calls)
                self.assertEqual(set(calls), {"GET"})

    def test_redaction_preserves_protocol_fields_without_mutating_input(self):
        original = {"id": 31, "state": "COMMENT", "stale": False,
                    "rows": [{"email": "alice" + "@example.invalid",
                              "token": "private", "cookie": "session",
                              "body": ("opaque-secret\nContact "
                                       "alice@example.invalid"),
                              "url":
                                  "https://forge.invalid/api/v1/repos/a/b"}],
                    "commit_id": "1" * 40, "position": 2,
                    "extra_lines_count": 2}
        result = record.scrub(original, ("opaque-secret",))
        self.assertEqual(result["commit_id"], original["commit_id"])
        self.assertEqual(result["id"], 31)
        self.assertIs(result["stale"], False)
        self.assertEqual(result["rows"][0]["body"],
                         "<redacted>\nContact <redacted>")
        self.assertEqual(result["rows"][0]["url"],
                         "http://127.0.0.1/api/v1/repos/a/b")
        self.assertEqual(result["rows"][0]["cookie"], "<redacted>")
        self.assertEqual(result["rows"][0]["email"], "<redacted>")
        self.assertEqual(result["rows"][0]["token"], "<redacted>")
        self.assertEqual(original["rows"][0]["token"], "private")

    def test_secret_keys_and_avatar_hashes_are_redacted(self):
        private = {key: "private" for key in (
            "EMAIL", "token", "password", "cookie", "Authorization",
            "Set-Cookie", "secret", "access_token")}
        self.assertEqual(record.scrub(private, ()),
                         dict.fromkeys(private, "<redacted>"))
        avatar = "https://forge.invalid/avatars/" + "aB12" * 8
        self.assertEqual(record.scrub(avatar, ()),
                         "http://127.0.0.1/avatars/<redacted>")
        for scheme in ("http", "https", "ssh", "git"):
            with self.subTest(scheme=scheme):
                self.assertEqual(
                    record.scrub(f"{scheme}://forge.invalid/path", ()),
                    "http://127.0.0.1/path")

    def test_base_url_rejects_credentials_redirect_like_paths_and_remote_http(
            self):
        for url in (
            "http://forge.invalid",
            "https://name:secret@forge.invalid",
            "https://name@forge.invalid",
            "https://:secret@forge.invalid",
            "file://forge.invalid",
            "https:///",
            "https://forge.invalid/path",
            "https://forge.invalid?secret=x",
                "https://forge.invalid#x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                record.validate_base_url(url)
        self.assertEqual(record.validate_base_url("http://127.0.0.1:18554/"),
                         "http://127.0.0.1:18554")
        self.assertEqual(record.validate_base_url("https://forge.invalid"),
                         "https://forge.invalid")

    def test_redirect_cannot_forward_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = record.Recorder(
                "http://127.0.0.1", {"author": "test-secret"},
                Path(tmp) / "out")
            request = urllib.request.Request(
                "http://127.0.0.1/api/v1/user",
                headers={"Authorization": "token test-secret"})
            redirects = [handler for handler in recorder.opener.handlers
                         if isinstance(handler,
                                       urllib.request.HTTPRedirectHandler)]
            self.assertTrue(redirects)
            for handler in redirects:
                with patch.object(recorder.opener, "open") as forward:
                    with self.assertRaisesRegex(RuntimeError,
                                                "redirect refused"):
                        handler.http_error_302(
                            request, io.BytesIO(), 302, "Found",
                            {"location": "https://elsewhere.invalid/user"})
                    forward.assert_not_called()

    def test_saved_response_is_scrubbed(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = record.Recorder(
                "http://127.0.0.1", {"author": "test-secret"},
                Path(tmp) / "out")
            body = json.dumps({
                "note": "echo test-secret", "email": "a@example.invalid",
                "url": "https://forge.invalid/a"}).encode()
            response = urllib.response.addinfourl(
                io.BytesIO(body), {}, "http://127.0.0.1", code=200)
            with patch.object(recorder.opener, "open", return_value=response):
                recorder.request("E1", "private", "GET", "/private")
            saved = json.loads(
                next(recorder.output.glob("*.json")).read_text())
            self.assertEqual(saved["response"]["body"], {
                "note": "echo <redacted>", "email": "<redacted>",
                "url": "http://127.0.0.1/a"})

    def test_recorder_ignores_configured_proxies(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
                urllib.request, "getproxies",
                return_value={"https": "http://proxy.invalid"}):
            recorder = record.Recorder(
                "https://forge.invalid", {"author": "test-secret"},
                Path(tmp) / "out")
            self.assertFalse(any(isinstance(handler,
                                            urllib.request.ProxyHandler)
                                 for handler in recorder.opener.handlers))

    def test_cli_rejects_unsafe_repository_names_before_dispatch(self):
        arguments = ["record.py", "--base-url", "http://127.0.0.1",
                     "--output", "/unused/output", "--git-workdir",
                     "/unused/git"]
        for actor in ("author", "approver", "other"):
            arguments.extend([f"--{actor}-token-file", f"/unused/{actor}"])
        for name in ("../existing", "a/b", "Bad", "a?x", "a" * 65):
            with (self.subTest(name=name),
                  patch.object(sys, "argv", arguments +
                               ["--repository", name]),
                  patch.object(sys, "stderr", io.StringIO()) as error,
                  patch.object(record, "run") as run):
                with self.assertRaises(SystemExit) as stopped:
                    record.main()
                self.assertEqual(stopped.exception.code, 2)
                self.assertIn("simple new disposable repository name",
                              error.getvalue())
                run.assert_not_called()

    def test_browser_draft_count_is_checked_before_deletion(self):
        for count in (0, 2):
            with (self.subTest(count=count),
                  tempfile.TemporaryDirectory() as tmp):
                path = Path(tmp)
                for actor in ("author", "approver", "other"):
                    (path / actor).write_text("test-secret")
                args = SimpleNamespace(
                    base_url="http://127.0.0.1", output=path / "output",
                    git_workdir=path / "git", repository="issue54-test",
                    author_token_file=path / "author",
                    approver_token_file=path / "approver",
                    other_token_file=path / "other")

                def respond(recorder, experiment, name, method, endpoint,
                            actor="author", *rest, **kwargs):
                    self.assertNotEqual(method, "DELETE")
                    recorder.last_status = (
                        404 if name == "repository-absent" else 200)
                    if endpoint == "/version":
                        return {"version": "16.0.3+gitea-1.22.0"}
                    if endpoint == "/user":
                        return {"login": actor}
                    if name == "fresh-draft":
                        return [{"id": n, "state": "PENDING"}
                                for n in range(count)]
                    return {"id": 1, "number": 1}

                result = SimpleNamespace(stdout="1" * 40, stderr="",
                                         returncode=0)
                with (patch.object(record.Recorder, "request", autospec=True,
                                   side_effect=respond),
                      patch.object(record.subprocess, "run",
                                   return_value=result),
                      patch("builtins.input", return_value=""),
                      patch("builtins.print")):
                    with self.assertRaisesRegex(
                            RuntimeError, "exactly one author browser draft"):
                        record.run(args)

    def test_git_output_is_scrubbed_and_credentials_stay_in_child_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            for actor in ("author", "approver", "other"):
                (path / actor).write_text("test-secret")
            args = SimpleNamespace(
                base_url="https://forge.invalid", output=path / "output",
                git_workdir=path / "git", repository="issue54-test",
                author_token_file=path / "author",
                approver_token_file=path / "approver",
                other_token_file=path / "other")

            def respond(recorder, experiment, name, method, endpoint,
                        actor="author", *rest, **kwargs):
                recorder.last_status = (
                    404 if name == "repository-absent" else 200)
                if endpoint == "/version":
                    return {"version": "16.0.3+gitea-1.22.0"}
                if endpoint == "/user":
                    return {"login": actor}
                return {"id": 1}

            def git_result(command, **kwargs):
                if "clone" not in command:
                    raise RuntimeError("stop after clone")
                self.assertIn("credential.helper=", command)
                self.assertNotIn("test-secret", str(command))
                basic = record.base64.b64encode(
                    b"author:test-secret").decode()
                self.assertEqual(kwargs["env"]["GIT_CONFIG_VALUE_0"],
                                 "Authorization: Basic " + basic)
                return SimpleNamespace(
                    stdout=f"test-secret {basic} user@example.invalid",
                    stderr="ssh://forge.invalid/a git://forge.invalid/b",
                    returncode=0)

            with patch.object(record.Recorder, "request", autospec=True,
                              side_effect=respond), patch.object(
                    record.subprocess, "run", side_effect=git_result):
                with self.assertRaisesRegex(RuntimeError, "stop after clone"):
                    record.run(args)
            saved = json.loads(
                (args.output / "git-operations.json").read_text())
            self.assertEqual(saved[0]["stdout"],
                             "<redacted> <redacted> <redacted>")
            self.assertEqual(saved[0]["stderr"],
                             "http://127.0.0.1/a http://127.0.0.1/b")
            self.assertEqual(saved[0]["command"][1],
                             "http://127.0.0.1/approver/issue54-test.git")

    def test_recorder_refuses_existing_output_and_blank_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            with self.assertRaises(FileExistsError):
                record.Recorder("http://127.0.0.1", {"author": "fake"}, path)
            for token in ("", "two words", "line\nbreak", "tab\tvalue"):
                with self.subTest(token=token), self.assertRaises(ValueError):
                    record.Recorder("http://127.0.0.1", {"author": token},
                                    path / "new")
            self.assertFalse((path / "new").exists())

    def test_error_response_is_recorded_before_required_request_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = record.Recorder(
                "http://127.0.0.1", {"author": "test-secret"},
                Path(tmp) / "records")
            error = urllib.error.HTTPError(
                "http://127.0.0.1",
                405,
                "refused",
                {},
                io.BytesIO(b'{"message":"refused"}'))
            with patch.object(recorder.opener, "open",
                              side_effect=error) as request:
                with self.assertRaisesRegex(RuntimeError, "HTTP 405"):
                    recorder.request(
                        "E7",
                        "refusal",
                        "POST",
                        "/merge",
                        body={
                            "head_commit_id": "1" *
                            40},
                        required=True)
            sent = request.call_args.args[0]
            self.assertEqual(sent.get_header(
                "Authorization"), "token test-secret")
            saved = json.loads(
                next(recorder.output.glob("*.json")).read_text())
            self.assertEqual(
                saved["response"], {
                    "status": 405, "body": {
                        "message": "refused"}})
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
                self.assertIn(f"]({path.name})",
                              (path.parent / "README.md").read_text())
                self.assertIsInstance(payload["response"]["status"], int)
        self.assertTrue({f"E{n}" for n in range(1, 10)} <= experiments)
        for path in (FIXTURES / "recordings").rglob("*.json"):
            with self.subTest(privacy=path.name):
                text = path.read_text()
                json.loads(text)
                self.assertNotRegex(text, r"[\w.+-]+@[\w.-]+")
                urls = re.findall(r'(?:https?|ssh|git)://[^\s"<>]+', text,
                                  flags=re.IGNORECASE)
                for url in urls:
                    self.assertEqual(urllib.parse.urlsplit(url).hostname,
                                     "127.0.0.1")
                self.assertNotRegex(text, r"(?i)/avatars/[0-9a-f]{32}")
                self.assertNotIn('"Authorization"', text)
                self.assertNotIn('"Set-Cookie"', text)


if __name__ == "__main__":
    unittest.main()
