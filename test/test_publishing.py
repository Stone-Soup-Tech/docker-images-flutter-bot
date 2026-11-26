"""Offline checks for the Flutter bot Bitbucket publishing."""
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

from sdk import flutter_bot as bot


class PublishingTests(unittest.TestCase):
    def make_client(self):
        with patch.dict("os.environ", {"BITBUCKET_REPO_FULL_NAME": "workspace/repository",
                                      "BOT_EMAIL": "bot@example.com",
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

    def test_explicit_reviewers_resolve_bitbucket_uuids_and_account_ids(self):
        client = self.make_client()
        identifier = '11111111-2222-3333-4444-555555555555'
        bot_id = '{aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee}'
        account_id = '557058:' + identifier
        legacy_id = '0123456789abcdef01234567'

        def request(path):
            return {'uuid': bot_id if path == 'user' else '{' + identifier + '}'}

        with patch.object(client, 'request', side_effect=request) as api:
            client.configure_reviewers([identifier, identifier.upper(), '{' + identifier + '}', account_id, legacy_id])
        self.assertEqual(client.reviewer_ids, ('{' + identifier + '}',))
        paths = [call.args[0] for call in api.call_args_list]
        self.assertIn('users/557058%3A' + identifier, paths)
        self.assertIn('users/' + legacy_id, paths)
        self.assertEqual(paths.count('user'), 1)
        self.assertIn('users/%7B' + identifier + '%7D', paths)

    def test_explicit_bitbucket_reviewers_reject_email_names_and_paths(self):
        client = self.make_client()
        with patch.object(client, 'request') as request:
            for value in ('you@example.com', 'Jane Smith', 'nickname', '../user', ''):
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'UUID or Atlassian account ID'):
                    client.configure_reviewers([value])
        request.assert_not_called()

    def test_bitbucket_reviewer_lookup_errors_explain_permissions(self):
        client = self.make_client()
        with patch.object(client, 'request', side_effect=RuntimeError('HTTP 404')):
            with self.assertRaisesRegex(RuntimeError, 'Cannot resolve Bitbucket reviewer.*HTTP 404'):
                client.configure_reviewers(['11111111-2222-3333-4444-555555555555'])

    def test_bitbucket_rejects_publishing_account_as_explicit_reviewer(self):
        client = self.make_client()
        identifier = '{11111111-2222-3333-4444-555555555555}'
        with patch.object(client, 'request', return_value={'uuid': identifier}):
            with self.assertRaisesRegex(ValueError, 'cannot review its own'):
                client.configure_reviewers([identifier])

    def test_bitbucket_create_uses_explicit_reviewers_instead_of_defaults(self):
        client = self.make_client()
        client.reviewer_ids = ('{11111111-2222-3333-4444-555555555555}',)
        with patch.object(client, 'reviewers') as defaults, \
                patch.object(client, 'request', return_value={'links': {'html': {'href': 'https://example.test/pr'}}}) as request:
            client.save_pull_request(None, 'branch', 'title', 'body')
        defaults.assert_not_called()
        self.assertEqual(request.call_args.args[1], 'POST')
        self.assertEqual(request.call_args.args[2]['reviewers'], [{'uuid': client.reviewer_ids[0]}])

    def test_bitbucket_refresh_retains_existing_reviewers_when_adding_missing(self):
        client = self.make_client()
        first = '{11111111-2222-3333-4444-555555555555}'
        second = '{aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee}'
        client.reviewer_ids = (first, second)
        with patch.object(client, 'request', side_effect=[
            {'reviewers': [{'uuid': first, 'approved': True}, {'uuid': 'another-reviewer'}]},
            {'links': {'html': {'href': 'https://example.test/pr'}}},
        ]) as request:
            client.save_pull_request({'id': 7}, 'branch', 'title', 'body')
        self.assertEqual(request.call_args.args[1], 'PUT')
        self.assertEqual(request.call_args.args[2]['reviewers'],
                         [{'uuid': first}, {'uuid': 'another-reviewer'}, {'uuid': second}])

    def test_bitbucket_refresh_does_not_rewrite_reviewers_if_already_present(self):
        client = self.make_client()
        identifier = '{aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee}'
        client.reviewer_ids = (identifier,)
        with patch.object(client, 'request', side_effect=[
            {'reviewers': [{'uuid': identifier.upper(), 'approved': True}]},
            {'links': {'html': {'href': 'https://example.test/pr'}}},
        ]) as request:
            client.save_pull_request({'id': 7}, 'branch', 'title', 'body')
        self.assertNotIn('reviewers', request.call_args.args[2])

    def test_bitbucket_refresh_without_explicit_reviewers_omits_reviewer_changes(self):
        client = self.make_client()
        with patch.object(client, 'request', return_value={'links': {'html': {'href': 'https://example.test/pr'}}}) as request:
            client.save_pull_request({'id': 7}, 'branch', 'title', 'body')
        request.assert_called_once()
        self.assertEqual(request.call_args.args[2], {'title': 'title', 'description': 'body'})

    def test_bitbucket_reviewer_dry_run_uses_common_configuration_without_assigning(self):
        client = self.make_client()
        identifier = '{11111111-2222-3333-4444-555555555555}'

        def request(path):
            return {'uuid': '{aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee}' if path == 'user' else identifier}

        with patch.object(bot, 'select_provider', return_value=client), \
                patch.object(bot, 'prepare_checkout', return_value=(Path('unused'), 'base')), \
                patch.object(bot, 'run_updates', return_value={'dependencies': []}), \
                patch.object(client, 'pull_requests', return_value=[]), \
                patch.object(client, 'request', side_effect=request) as api:
            bot.main(['--provider', 'bitbucket', '--reviewer', identifier, '--dry-run'])
        self.assertEqual(client.reviewer_ids, (identifier,))
        self.assertTrue(all(len(call.args) == 1 for call in api.call_args_list))

    def test_pipeline_branch_is_detected_automatically(self):
        with patch.dict("os.environ", {"BITBUCKET_REPO_FULL_NAME": "workspace/repository",
                                      "BOT_EMAIL": "bot@example.com", "BOT_API_TOKEN": "token",
                                      "BITBUCKET_BRANCH": "dev"}, clear=True), \
                patch.object(bot.Bitbucket, "request") as request:
            client = bot.Bitbucket()
        self.assertEqual(client.base_branch, "dev")
        request.assert_not_called()

    def test_default_branch_is_used_outside_pipelines(self):
        with patch.dict("os.environ", {"BITBUCKET_REPO_FULL_NAME": "workspace/repository",
                                      "BOT_EMAIL": "bot@example.com", "BOT_API_TOKEN": "token"}, clear=True), \
                patch.object(bot.Bitbucket, "request", return_value={"mainbranch": {"name": "main"}}):
            client = bot.Bitbucket()
        self.assertEqual(client.base_branch, "main")

    def test_own_update_branch_is_rejected(self):
        with patch.dict("os.environ", {"BITBUCKET_REPO_FULL_NAME": "workspace/repository",
                                      "BOT_EMAIL": "bot@example.com", "BOT_API_TOKEN": "token",
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


