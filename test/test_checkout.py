"""Exercise checkout reuse and Git publishing against real local repositories."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from sdk import flutter_bot as bot


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='flutter-bot-checkout-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.remote = self.root / 'remote.git'
        self.seed = self.root / 'seed'
        self.seed.mkdir()
        self.run_git(self.root, 'init', '--bare', '--quiet', str(self.remote))
        self.run_git(self.seed, 'init', '--quiet', '-b', 'dev')
        self.run_git(self.seed, 'config', 'user.name', 'Test')
        self.run_git(self.seed, 'config', 'user.email', 'test@example.com')
        self.run_git(self.seed, 'config', 'commit.gpgSign', 'false')
        (self.seed / 'pubspec.yaml').write_text('name: example\ndependencies:\n  example: ^1.0.0\n')
        (self.seed / 'pubspec.lock').write_text('version: 1.0.0\n')
        (self.seed / 'generated.txt').write_text('original\n')
        self.run_git(self.seed, 'add', '.')
        self.run_git(self.seed, 'commit', '--quiet', '-m', 'base')
        self.run_git(self.seed, 'push', '--quiet', str(self.remote), 'HEAD:dev')
        self.checkout = self.root / 'checkout'
        self.run_git(self.root, 'clone', '--quiet', '--depth=1', '--branch', 'dev',
                     self.remote.as_uri(), str(self.checkout))
        self.base = self.run_git(self.checkout, 'rev-parse', 'HEAD')
        self.run_git(self.checkout, 'checkout', '--quiet', '--detach', self.base)
        self.run_git(self.checkout, 'config', 'commit.gpgSign', 'false')
        self.client = Mock(repository='workspace/repository', token='secret', base_branch='dev',
                           repo_path='repositories/workspace/repository')
        self.client.branch.side_effect = self.remote_branch
        self.client.pull_requests.return_value = []
        self.client.reviewers.return_value = []
        self.client.request.return_value = {'links': {'html': {'href': 'https://example.test/pr'}}}
        environment = patch.dict(os.environ, {'BITBUCKET_CLONE_DIR': str(self.checkout),
                                              'BITBUCKET_COMMIT': self.base})
        environment.start()
        self.addCleanup(environment.stop)
        remote = patch.object(bot, 'repository_url', return_value=str(self.remote))
        remote.start()
        self.addCleanup(remote.stop)
        self.update = {'dependencies': [{'name': 'example', 'version': '2.0.0'}]}
        self.update_branch = 'flutter-bot/dependencies-dev'

    def run_git(self, directory, *arguments):
        result = subprocess.run(['git', '-C', str(directory), *arguments], text=True,
                                capture_output=True, check=True)
        return result.stdout.strip()

    def remote_branch(self, name):
        result = self.run_git(self.root, 'ls-remote', str(self.remote), f'refs/heads/{name}')
        return result.split()[0] if result else None

    def change_pub_files(self):
        (self.checkout / 'pubspec.yaml').write_text('name: example\ndependencies:\n  example: ^2.0.0\n')
        (self.checkout / 'pubspec.lock').write_text('version: 2.0.0\n')

    def publish(self, dry_run=False):
        bot.publish_update(self.checkout, self.client, self.update, self.base, dry_run)

    def test_detached_shallow_pipeline_checkout_is_reused(self):
        self.assertEqual(bot.prepare_checkout(self.client), (self.checkout, self.base))
        self.assertEqual(self.run_git(self.checkout, 'rev-parse', '--is-shallow-repository'), 'true')

    def test_current_directory_is_used_outside_pipelines(self):
        with patch.dict(os.environ), patch.object(bot.Path, 'cwd', return_value=self.checkout):
            os.environ.pop('BITBUCKET_CLONE_DIR', None)
            self.assertEqual(bot.prepare_checkout(self.client), (self.checkout, self.base))

    def test_symlinked_pubspec_is_rejected_before_pub_runs(self):
        manifest = self.checkout / 'pubspec.yaml'
        manifest.unlink()
        manifest.symlink_to(self.seed / 'pubspec.yaml')
        with patch.object(bot, 'pub') as pub:
            with self.assertRaisesRegex(ValueError, 'must not be symlinks'):
                bot.run_updates(self.checkout, self.client, self.base)
        pub.assert_not_called()

    def test_dirty_checkout_is_rejected(self):
        for staged in (False, True):
            with self.subTest(staged=staged):
                self.change_pub_files()
                if staged:
                    self.run_git(self.checkout, 'add', 'pubspec.yaml')
                with self.assertRaisesRegex(RuntimeError, 'must be clean'):
                    bot.prepare_checkout(self.client)

    def test_wrong_pipeline_commit_is_rejected(self):
        with patch.dict(os.environ, {'BITBUCKET_COMMIT': '0' * 40}):
            with self.assertRaisesRegex(RuntimeError, 'BITBUCKET_COMMIT'):
                bot.prepare_checkout(self.client)

    def test_missing_clone_has_actionable_error(self):
        with patch.dict(os.environ, {'BITBUCKET_CLONE_DIR': str(self.root)}):
            with self.assertRaisesRegex(RuntimeError, 'enable the Bitbucket pipeline clone'):
                bot.prepare_checkout(self.client)

    def test_stale_checkout_is_rejected_before_resolution(self):
        self.client.branch.side_effect = None
        self.client.branch.return_value = 'new-base'
        with self.assertRaisesRegex(RuntimeError, 'Base branch changed'):
            bot.prepare_checkout(self.client)

    def test_only_pub_files_are_committed_and_target_is_not_pushed(self):
        self.change_pub_files()
        (self.checkout / 'generated.txt').write_text('generated by flutter\n')
        with patch.object(bot, 'git', wraps=bot.git) as git:
            self.publish()
        commands = [call.args[1:] for call in git.call_args_list]
        self.assertFalse(any(command[0] in ('init', 'clone', 'checkout', 'fetch') for command in commands))
        updated = self.remote_branch(self.update_branch)
        changed = self.run_git(self.remote, 'diff-tree', '--no-commit-id', '--name-only', '-r', updated)
        self.assertEqual(set(changed.splitlines()), {'pubspec.yaml', 'pubspec.lock'})
        self.assertEqual(self.remote_branch('dev'), self.base)
        self.assertEqual(self.run_git(self.remote, 'show', f'{updated}:generated.txt'), 'original')
        self.assertEqual(self.client.request.call_args.args[1], 'POST')

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
        self.assertEqual(self.client.request.call_args.args[1], 'PUT')

    def test_dry_run_leaves_local_diff_but_never_commits_or_pushes(self):
        self.change_pub_files()
        self.publish(dry_run=True)
        self.assertEqual(self.run_git(self.checkout, 'rev-parse', 'HEAD'), self.base)
        self.assertIsNone(self.remote_branch(self.update_branch))
        self.assertTrue(self.run_git(self.checkout, 'diff', '--name-only'))
        self.assertFalse(self.run_git(self.checkout, 'diff', '--cached', '--name-only'))
        self.client.request.assert_not_called()

    def test_base_advance_during_resolution_prevents_publication(self):
        self.change_pub_files()
        (self.seed / 'new.txt').write_text('new commit\n')
        self.run_git(self.seed, 'add', 'new.txt')
        self.run_git(self.seed, 'commit', '--quiet', '-m', 'advance dev')
        self.run_git(self.seed, 'push', '--quiet', str(self.remote), 'HEAD:dev')
        with self.assertRaisesRegex(RuntimeError, 'Base branch changed'):
            self.publish()
        self.assertEqual(self.run_git(self.checkout, 'rev-parse', 'HEAD'), self.base)
        self.assertIsNone(self.remote_branch(self.update_branch))
        self.client.request.assert_not_called()

    def test_existing_staged_changes_prevent_publication(self):
        self.change_pub_files()
        (self.checkout / 'generated.txt').write_text('external change\n')
        self.run_git(self.checkout, 'add', 'generated.txt')
        with self.assertRaisesRegex(RuntimeError, 'Unexpected staged files'):
            self.publish()
        self.assertIsNone(self.remote_branch(self.update_branch))
        self.client.request.assert_not_called()

    def test_changed_update_branch_is_rejected_before_refresh(self):
        self.change_pub_files()
        self.publish()
        self.run_git(self.checkout, 'checkout', '--quiet', '--detach', self.base)
        self.change_pub_files()
        self.client.request.reset_mock()
        self.client.pull_requests.return_value = [{'state': 'OPEN', 'id': 7}]
        real_git = bot.git

        def competing_writer(directory, *arguments, **kwargs):
            if arguments[0] == 'fetch':
                self.run_git(self.seed, 'checkout', '--quiet', '-b', self.update_branch)
                (self.seed / 'pubspec.lock').write_text('competing change\n')
                self.run_git(self.seed, 'add', 'pubspec.lock')
                self.run_git(self.seed, 'commit', '--quiet', '-m', 'competing update')
                self.run_git(self.seed, 'push', '--quiet', '--force', str(self.remote),
                             f'HEAD:{self.update_branch}')
            return real_git(directory, *arguments, **kwargs)

        with patch.object(bot, 'git', side_effect=competing_writer):
            with self.assertRaisesRegex(RuntimeError, 'Flutter Bot branch changed'):
                self.publish()
        self.client.request.assert_not_called()
        self.assertEqual(self.run_git(self.checkout, 'rev-parse', 'HEAD'), self.base)

    def test_lease_rejects_competing_update_branch_creation(self):
        self.change_pub_files()
        real_git = bot.git

        def competing_writer(directory, *arguments, **kwargs):
            if arguments[0] == 'push':
                self.run_git(self.seed, 'checkout', '--quiet', '-b', self.update_branch)
                (self.seed / 'pubspec.lock').write_text('competing change\n')
                self.run_git(self.seed, 'add', 'pubspec.lock')
                self.run_git(self.seed, 'commit', '--quiet', '-m', 'competing update')
                self.run_git(self.seed, 'push', '--quiet', str(self.remote), f'HEAD:{self.update_branch}')
            return real_git(directory, *arguments, **kwargs)

        with patch.object(bot, 'git', side_effect=competing_writer):
            with self.assertRaises(subprocess.CalledProcessError):
                self.publish()
        published = self.remote_branch(self.update_branch)
        self.assertEqual(self.run_git(self.remote, 'show', f'{published}:pubspec.lock'), 'competing change')
        self.assertEqual(self.remote_branch('dev'), self.base)
        self.client.request.assert_not_called()
