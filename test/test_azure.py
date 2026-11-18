"""Azure provider contract, authentication, and pipeline selection."""
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

from sdk import flutter_bot as bot
import test_checkout


AZURE_ENV = {
    'SYSTEM_COLLECTIONURI': 'https://dev.azure.com/example/',
    'SYSTEM_TEAMPROJECTID': 'project-id', 'BUILD_REPOSITORY_ID': 'repo-id',
    'SYSTEM_ACCESSTOKEN': 'azure-secret', 'BUILD_SOURCEBRANCH': 'refs/heads/dev',
}


class AzureTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, AZURE_ENV, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.client = bot.Azure()

    def test_branch_requires_exact_match_across_pages(self):
        with patch.object(self.client, 'request', side_effect=[
            {'value': [{'name': 'refs/heads/develop', 'objectId': 'wrong'}], '_continuation': 'next'},
            {'value': [{'name': 'refs/heads/dev', 'objectId': 'correct'}]},
        ]) as request:
            self.assertEqual(self.client.branch('dev'), 'correct')
        self.assertIn('continuationToken=next', request.call_args.args[0])
        with patch.object(self.client, 'request', return_value={'value': []}):
            self.assertIsNone(self.client.branch('missing'))

    def test_pr_pagination_and_state_normalization(self):
        with patch.object(self.client, 'request', side_effect=[
            {'value': [{'pullRequestId': 1}]}, {'value': []},
            {'value': [{'pullRequestId': 2, 'description': 'marker'}]}, {'value': []},
        ]) as request:
            prs = self.client.pull_requests('flutter-bot/dependencies-dev')
        self.assertEqual([pr['state'] for pr in prs], ['OPEN', 'DECLINED'])
        query = parse_qs(urlparse(request.call_args_list[1].args[0]).query)
        self.assertEqual(query['$skip'], ['1'])
        self.assertEqual(query['searchCriteria.targetRefName'], ['refs/heads/dev'])

    def test_create_and_refresh(self):
        with patch.object(self.client, 'request', return_value={'pullRequestId': 5}) as request:
            url = self.client.save_pull_request(None, 'flutter-bot/dependencies-dev', 'title', 'body')
            self.assertTrue(url.endswith('/pullrequest/5'))
            self.assertEqual(request.call_args.args[1], 'POST')
            self.assertEqual(request.call_args.args[2]['targetRefName'], 'refs/heads/dev')
            self.client.save_pull_request({'id': 5}, 'branch', 'title', 'body')
            self.assertEqual(request.call_args.args[1], 'PATCH')
            self.assertEqual(request.call_args.args[2], {'title': 'title', 'description': 'body'})

    def test_credentials_are_scoped_and_not_in_clone_url(self):
        environment = bot.git_environment(self.client)
        self.assertEqual(environment['GIT_CONFIG_KEY_1'],
                         'http.https://dev.azure.com/example/.extraHeader')
        self.assertEqual(environment['GIT_CONFIG_VALUE_1'], 'Authorization: Bearer azure-secret')
        self.assertNotIn('azure-secret', self.client.repository_url)
        response = Mock()
        response.headers = {}
        response.read.return_value = b'{"value": []}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(bot, 'urlopen', return_value=response) as open_request:
            self.client.request(self.client.repo_path)
        request = open_request.call_args.args[0]
        self.assertEqual(request.get_header('Authorization'), 'Bearer azure-secret')
        self.assertIn('api-version=7.1', request.full_url)

    def test_errors_redact_credentials_and_external_paths_are_rejected(self):
        error = HTTPError('url', 403, 'Forbidden', {},
                          io.BytesIO(json.dumps({'message': 'Denied azure-secret'}).encode()))
        with patch.object(bot, 'urlopen', side_effect=error):
            with self.assertRaisesRegex(RuntimeError, 'HTTP 403 — Denied \\[REDACTED\\]'):
                self.client.request(self.client.repo_path)
        with patch.object(bot, 'urlopen') as request:
            for path in ('https://evil.test', '//evil.test', '../other'):
                with self.assertRaises(ValueError):
                    self.client.request(path)
            request.assert_not_called()

    def test_abandoned_update_is_not_published(self):
        update = {'dependencies': [{'name': 'example', 'version': '2.0.0'}]}
        fingerprint = bot.hashlib.sha256(json.dumps([('example', '2.0.0')]).encode()).hexdigest()
        with patch.object(self.client, 'request', side_effect=[
            {'value': []},
            {'value': [{'pullRequestId': 1,
                        'description': f'<!-- flutter-bot-update:{fingerprint} -->'}]},
            {'value': []},
        ]) as request, patch.object(bot, 'git') as git:
            bot.publish_update(Path('unused'), self.client, update, 'base', False)
        git.assert_not_called()
        self.assertTrue(all(len(call.args) == 1 for call in request.call_args_list))

    def test_rejects_pr_tag_and_bot_branches(self):
        for branch in ('refs/pull/1/merge', 'refs/tags/v1', 'refs/heads/flutter-bot/update'):
            with self.subTest(branch=branch), patch.dict(os.environ, {'BUILD_SOURCEBRANCH': branch}):
                with self.assertRaises(ValueError):
                    bot.Azure()

    def test_rejects_untrusted_collection_urls(self):
        for url in ('https://evil.test/org/', 'https://dev.azure.com.evil.test/org/',
                    'https://user:secret@dev.azure.com/org/', 'http://dev.azure.com/org/'):
            with self.subTest(url=url), patch.dict(os.environ, {'SYSTEM_COLLECTIONURI': url}):
                with self.assertRaises(ValueError):
                    bot.Azure()

    def test_default_branch_and_explicit_selection(self):
        del os.environ['BUILD_SOURCEBRANCH']
        with patch.object(bot.Azure, 'request', return_value={'defaultBranch': 'refs/heads/main'}):
            self.assertEqual(bot.select_provider('azure').base_branch, 'main')

    def test_detection_and_ambiguity(self):
        with patch.dict(os.environ, {'TF_BUILD': 'True'}):
            self.assertIsInstance(bot.select_provider(None), bot.Azure)
            with patch.dict(os.environ, {'BITBUCKET_REPO_FULL_NAME': 'workspace/repo'}):
                with self.assertRaisesRegex(ValueError, 'Ambiguous'):
                    bot.select_provider(None)
                self.assertIsInstance(bot.select_provider('azure'), bot.Azure)

    def test_no_detection_requires_explicit_provider(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(bot, 'Bitbucket') as bitbucket:
            with self.assertRaisesRegex(ValueError, 'specify --provider'):
                bot.select_provider(None)
            bitbucket.assert_not_called()
            self.assertIs(bot.select_provider('bitbucket'), bitbucket.return_value)

    def test_bitbucket_detection(self):
        with patch.dict(os.environ, {'BITBUCKET_REPO_FULL_NAME': 'workspace/repo'}, clear=True), \
                patch.object(bot, 'Bitbucket') as bitbucket:
            self.assertIs(bot.select_provider(None), bitbucket.return_value)



class AzureCheckoutTests(test_checkout.CheckoutTests):
    """Run the same real Git concurrency and publication checks with Azure."""

    def setUp(self):
        super().setUp()
        with patch.dict(os.environ, AZURE_ENV):
            self.client = bot.Azure()
        self.client.branch = Mock(side_effect=self.remote_branch)
        self.client.pull_requests = Mock(return_value=[])
        self.client.request = Mock(return_value={'pullRequestId': 5})
        # Reuse generic checkout cases, including their deliberately bad values.
        self.client.checkout_variable = 'BITBUCKET_CLONE_DIR'
        self.client.commit_variable = 'BITBUCKET_COMMIT'
        self.client.name = 'Bitbucket'

    def test_unchanged_rerun_refreshes_pr_without_duplicate_commit(self):
        self.change_pub_files()
        self.publish()
        published = self.remote_branch(self.update_branch)
        self.run_git(self.checkout, 'checkout', '--quiet', '--detach', self.base)
        self.change_pub_files()
        self.client.pull_requests.return_value = [{'state': 'OPEN', 'id': 7}]
        with patch.object(bot, 'git', wraps=bot.git) as git:
            self.publish()
        self.assertFalse(any(call.args[1] in ('commit', 'push') for call in git.call_args_list))
        self.assertEqual(self.remote_branch(self.update_branch), published)
        self.assertEqual(self.client.request.call_args.args[1], 'PATCH')

    def test_azure_checkout_variables(self):
        self.client.checkout_variable = 'BUILD_SOURCESDIRECTORY'
        self.client.commit_variable = 'BUILD_SOURCEVERSION'
        with patch.dict(os.environ, {'BUILD_SOURCESDIRECTORY': str(self.checkout),
                                     'BUILD_SOURCEVERSION': self.base}):
            self.assertEqual(bot.prepare_checkout(self.client), (self.checkout, self.base))
