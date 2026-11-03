"""Offline checks for the Flutter bot Bitbucket publishing."""
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

from sdk import flutter_bot as bot


class PublishingTests(unittest.TestCase):
    def make_client(self):
        with patch.dict("os.environ", {"BITBUCKET_REPO_FULL_NAME": "workspace/repository",
                                      "BOT_USERNAME": "bot@example.com",
                                      "BOT_API_TOKEN": "secret-token"}):
            return bot.Bitbucket("dev")

    def test_pr_query_uses_supported_page_size_and_follows_pagination(self):
        client = self.make_client()
        first = {"id": 1, "state": "OPEN"}
        second = {"id": 2, "state": "DECLINED"}
        next_url = bot.API_ROOT + client.repo_path + "/pullrequests?page=2"
        with patch.object(client, "request", side_effect=[
                {"values": [first], "next": next_url}, {"values": [second]}]) as request:
            self.assertEqual(client.pull_requests("flutter-bot/dependencies-dev"), [first, second])
        query = parse_qs(urlparse(request.call_args_list[0].args[0]).query)
        self.assertEqual(query["pagelen"], ["50"])
        self.assertEqual(query["state"], ["OPEN", "DECLINED"])
        self.assertEqual(query["q"], ['source.branch.name = "flutter-bot/dependencies-dev" AND '
                                     'destination.branch.name = "dev"'])
        self.assertEqual(request.call_args_list[1].args[0], next_url)

    def test_api_errors_keep_message_and_redact_token(self):
        client = self.make_client()
        body = json.dumps({"error": {"message": "Invalid pagelen; secret-token"}}).encode()
        error = HTTPError("https://api.bitbucket.org/2.0/test", 400, "Bad Request", {}, io.BytesIO(body))
        with patch("sdk.flutter_bot.urlopen", side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                client.request("test")
        self.assertIn("HTTP 400 — Invalid pagelen; [REDACTED]", str(caught.exception))
        self.assertNotIn("secret-token", str(caught.exception))

    def test_non_json_api_errors_keep_status(self):
        client = self.make_client()
        error = HTTPError("https://api.bitbucket.org/2.0/test", 502, "Bad Gateway", {},
                          io.BytesIO(b"<html>Bad gateway</html>"))
        with patch("sdk.flutter_bot.urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "HTTP 502"):
                client.request("test")

    def test_dry_run_never_writes(self):
        client = Mock(base_branch="dev")
        client.pull_requests.return_value = []
        with patch("sdk.flutter_bot.git") as git:
            bot.publish_update(Path("unused"), client,
                {"dependencies": [{"name": "example", "version": "2"}]},
                "base", True)
            git.assert_not_called()
        client.pull_requests.assert_called_once_with("flutter-bot/dependencies-dev")
        client.request.assert_not_called()
        client.branch.assert_not_called()

    def test_files_cannot_escape_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for name in ("../escape", ".git/config", ".GIT/config", "/absolute"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    bot.apply_files(directory, [{"directory": "/", "name": name, "content": "bad"}])

    def test_parent_symlink_cannot_modify_git_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / ".git").mkdir()
            (directory / "alias").symlink_to(directory / ".git", target_is_directory=True)
            with self.assertRaises(ValueError):
                bot.apply_files(directory, [{"directory": "/alias", "name": "config", "content": "bad"}])
            self.assertFalse((directory / ".git/config").exists())

    def test_api_rejects_external_pagination_and_redirects(self):
        client = self.make_client()
        with patch("sdk.flutter_bot.urlopen") as request:
            with self.assertRaises(ValueError):
                client.request("https://example.test/steal")
            request.assert_not_called()
        self.assertIsNone(bot.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.test"))

    def test_reviewers_use_repository_defaults_and_exclude_bot(self):
        client = self.make_client()
        with patch.object(client, "request", return_value={"uuid": "bot"}), \
                patch.object(client, "values", return_value=[{"uuid": "bot"}, {"uuid": "human"}]):
            self.assertEqual(client.reviewers(), [{"uuid": "human"}])

    def test_pipeline_branch_is_detected_automatically(self):
        with patch.dict("os.environ", {"BITBUCKET_REPO_FULL_NAME": "workspace/repository",
                                      "BOT_USERNAME": "bot@example.com", "BOT_API_TOKEN": "token",
                                      "BITBUCKET_BRANCH": "dev"}, clear=True), \
                patch.object(bot.Bitbucket, "request") as request:
            client = bot.Bitbucket()
        self.assertEqual(client.base_branch, "dev")
        request.assert_not_called()

    def test_default_branch_is_used_outside_pipelines(self):
        with patch.dict("os.environ", {"BITBUCKET_REPO_FULL_NAME": "workspace/repository",
                                      "BOT_USERNAME": "bot@example.com", "BOT_API_TOKEN": "token"}, clear=True), \
                patch.object(bot.Bitbucket, "request", return_value={"mainbranch": {"name": "main"}}):
            client = bot.Bitbucket()
        self.assertEqual(client.base_branch, "main")

    def test_own_update_branch_is_rejected(self):
        with patch.dict("os.environ", {"BITBUCKET_REPO_FULL_NAME": "workspace/repository",
                                      "BOT_USERNAME": "bot@example.com", "BOT_API_TOKEN": "token",
                                      "BITBUCKET_BRANCH": "flutter-bot/dependencies-dev"}, clear=True):
            with self.assertRaisesRegex(ValueError, "own update branch"):
                bot.Bitbucket()

    def test_declined_fingerprint_prevents_publication(self):
        update = {"dependencies": [{"name": "package", "version": "2"}]}
        fingerprint = bot.hashlib.sha256(json.dumps([("package", "2")]).encode()).hexdigest()
        client = Mock(base_branch="dev")
        client.pull_requests.return_value = [{"state": "DECLINED", "description":
            f"<!-- flutter-bot-update:{fingerprint} -->"}]
        with patch("sdk.flutter_bot.git") as git:
            bot.publish_update(Path("unused"), client, update, "base", False)
            git.assert_not_called()
        client.request.assert_not_called()

    def test_generated_binary_modes_deletion_and_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            file = {"directory": "/", "name": "file", "content": "AAH/",
                    "content_encoding": "base64", "mode": "100755"}
            bot.apply_files(directory, [file])
            self.assertEqual((directory / "file").read_bytes(), b"\x00\x01\xff")
            self.assertEqual((directory / "file").stat().st_mode & 0o777, 0o755)
            bot.apply_files(directory, [dict(file, deleted=True)])
            self.assertFalse((directory / "file").exists())
            (directory / "link").symlink_to(directory / "missing")
            for change in (dict(file, name="link"), dict(file, symlink_target="elsewhere"),
                           dict(file, mode="4755"), dict(file, content="not base64!")):
                with self.subTest(change=change), self.assertRaises(ValueError):
                    bot.apply_files(directory, [change])

    def test_publish_refresh_skips_unchanged_commit_and_uses_lease_for_changes(self):
        for unchanged in (False, True):
            for opened in (False, True):
                with self.subTest(unchanged=unchanged, opened=opened), tempfile.TemporaryDirectory() as temporary:
                    client = Mock(base_branch="dev", repository="workspace/repository", token="secret")
                    client.branch.return_value = "previous"
                    client.pull_requests.return_value = [{"state": "OPEN", "id": 7}] if opened else []
                    client.request.return_value = {"links": {"html": {"href": "https://example.test/pr"}}}
                    update = {"dependencies": [{"name": "example", "version": "2"}],
                              "updated-dependency-files": []}
                    fetches = iter(["base", "previous"])

                    def git_result(directory, *args, **kwargs):
                        if args == ("rev-parse", "FETCH_HEAD"):
                            return next(fetches)
                        if args == ("diff", "--cached", "--name-only"):
                            return "manifest"
                        if args == ("write-tree",):
                            return "new-tree"
                        if args == ("rev-parse", "previous^{tree}"):
                            return "new-tree" if unchanged else "old-tree"
                        return ""

                    with patch("sdk.flutter_bot.git", side_effect=git_result) as git:
                        bot.publish_update(Path(temporary), client, update, "base", False)
                    pushes = [call for call in git.call_args_list if call.args[1] == "push"]
                    self.assertEqual(len(pushes), 0 if unchanged else 1)
                    if pushes:
                        self.assertIn("--force-with-lease=refs/heads/flutter-bot/dependencies-dev:previous", pushes[0].args)
                        self.assertNotIn("secret", str(pushes[0].args))
                    self.assertEqual(client.request.call_args.args[1], "PUT" if opened else "POST")

    def test_base_race_prevents_push_and_pr(self):
        client = Mock(base_branch="dev", repository="workspace/repository", token="secret")
        client.pull_requests.return_value = []
        with tempfile.TemporaryDirectory() as temporary, patch("sdk.flutter_bot.git", return_value="new-base") as git:
            with self.assertRaisesRegex(RuntimeError, "Base branch changed"):
                bot.publish_update(Path(temporary), client,
                    {"dependencies": []}, "old-base", False)
            self.assertFalse(any(call.args[1] == "push" for call in git.call_args_list))
        client.request.assert_not_called()
