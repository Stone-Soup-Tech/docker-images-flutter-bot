"""Resolution and publication boundaries, without registry or Bitbucket writes."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from sdk import flutter_bot as bot
from scripts.base_image import record_digest


class ResolutionTests(unittest.TestCase):
    def resolve(self, *, changed=True, prerelease=False, prerelease_kind='direct', failing=False, missing_lock=False, workspace=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.commands = []
        self.environment = None
        client = Mock(repository='workspace/repo', token='secret', base_branch='dev')

        self.root = root
        (root / 'pubspec.yaml').write_text('name: example\n')
        if not missing_lock:
            (root / 'pubspec.lock').write_text('packages:\n  example:\n    version: \"1.0.0\"\n  build_package:\n    version: \"1.0.0+1\"\n')
        self.git_commands = []

        def git(checkout, *arguments, **kwargs):
            self.git_commands.append(arguments)
            if arguments[:2] == ('diff', '--name-only') and workspace:
                return 'other/pubspec.yaml'
            return ''

        def pub(checkout, environment, *arguments, **kwargs):
            self.commands.append(arguments)
            self.environment = environment
            if failing:
                raise subprocess.CalledProcessError(1, ['flutter', 'pub', *arguments])
            if arguments[0] == 'upgrade' and changed:
                (checkout / 'pubspec.yaml').write_text('name: example\ndependencies:\n  example: ^2.0.0\n')
                (checkout / 'pubspec.lock').write_text('new lock\n')
            if arguments[0] == 'outdated':
                return json.dumps({'packages': [
                    {'package': 'example', 'kind': prerelease_kind,
                     'current': {'version': '2.0.0-rc.1' if prerelease else '2.0.0'},
                     'latest': {'version': '3.0.0'}},
                    {'package': 'build_package', 'kind': 'transitive',
                     'current': {'version': '1.0.0+2'}, 'latest': {'version': '1.0.0+2'}}]})

        with patch.object(bot, 'git', side_effect=git), patch.object(bot, 'pub', side_effect=pub):
            return bot.run_updates(root, client, 'base')

    def test_major_minor_patch_and_build_resolution_updates_both_files(self):
        update = self.resolve()
        self.assertIn('^2.0.0', (self.root / 'pubspec.yaml').read_text())
        self.assertEqual((self.root / 'pubspec.lock').read_text(), 'new lock\n')
        self.assertTrue(all(command[0] == 'diff' for command in self.git_commands))
        self.assertIn('--major-versions', self.commands[0])
        self.assertIn('--tighten', self.commands[1])
        self.assertIn('--no-prereleases', self.commands[2])
        self.assertIn('--show-all', self.commands[2])
        self.assertIn('| [example](https://pub.dev/packages/example) | dependencies | `1.0.0` | `^2.0.0` |', update['pr-body'])
        self.assertNotIn('| [build_package](https://pub.dev/packages/build_package) |', update['pr-body'])
        self.assertNotIn('```diff', update['pr-body'])
        self.assertIn('| [example](https://pub.dev/packages/example) | dependencies | `2.0.0` | `3.0.0` |', update['pr-body'])
        self.assertLess(update['pr-body'].index('Updated direct dependencies'),
                        update['pr-body'].index('Versions held back'))
        self.assertEqual(update['dependencies'][1]['version'], '1.0.0+2')
        self.assertNotIn('secret', json.dumps(update))
        self.assertIn('GIT_CONFIG_VALUE_0', self.environment)

    def test_dev_dependency_type_in_both_tables(self):
        update = self.resolve(prerelease_kind='dev')
        self.assertIn('| dev_dependencies | `1.0.0` | `^2.0.0` |', update['pr-body'])
        self.assertIn('| dev_dependencies | `2.0.0` | `3.0.0` |', update['pr-body'])

    def test_pubspec_constraint_styles(self):
        constraints = bot.pubspec_constraints(b"""name: example
dependencies:
  caret: ^1.2.3
  fixed: '2.3.4' # pinned
  hosted:
    hosted: https://pub.dev
    version: "^3.4.5"
dev_dependencies:
  tool: ^4.5.6
""")
        self.assertEqual(constraints, {'caret': '^1.2.3', 'fixed': '2.3.4',
                                       'hosted': '^3.4.5', 'tool': '^4.5.6'})

    def test_workspace_partial_updates_are_not_published(self):
        with self.assertRaisesRegex(RuntimeError, 'workspaces are unsupported'):
            self.resolve(workspace=True)

    def test_no_changes_has_no_publishable_result(self):
        self.assertIsNone(self.resolve(changed=False))

    def test_direct_and_dev_prereleases_are_not_published(self):
        for kind in ('direct', 'dev'):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(RuntimeError, 'direct dependencies contain prereleases: example'):
                    self.resolve(prerelease=True, prerelease_kind=kind)

    def test_transitive_prereleases_are_published(self):
        for kind in ('transitive', 'transitive dev'):
            with self.subTest(kind=kind):
                update = self.resolve(prerelease=True, prerelease_kind=kind)
                self.assertEqual(update['dependencies'][0]['version'], '2.0.0-rc.1')

    def test_solver_failure_has_no_publishable_result(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.resolve(failing=True)

    def test_missing_lockfile_can_be_created(self):
        update = self.resolve(missing_lock=True)
        self.assertIn('| [example](https://pub.dev/packages/example) | dependencies | Not locked | `^2.0.0` |', update['pr-body'])

    def test_dry_run_resolves_but_never_publishes(self):
        client = Mock()
        client.branch.return_value = 'base'
        with patch.object(bot, 'Bitbucket', return_value=client), \
                patch.object(bot, 'prepare_checkout', return_value=(Path('checkout'), 'base')), \
                patch.object(bot, 'run_updates', return_value={'dependencies': []}), \
                patch.object(bot, 'publish_update') as publish:
            bot.main(['--dry-run'])
        self.assertTrue(publish.call_args.args[4])

    def test_removed_options_are_rejected(self):
        for option in ('--base-branch', '--group', '--reviewer', '--directory'):
            with self.subTest(option=option), patch.object(bot, 'Bitbucket') as client:
                with self.assertRaises(SystemExit):
                    bot.main([option, 'unused'])
                client.assert_not_called()

    def test_no_arguments_resolves_and_publishes(self):
        client = Mock()
        client.branch.return_value = 'base'
        with patch.object(bot, 'Bitbucket', return_value=client), \
                patch.object(bot, 'prepare_checkout', return_value=(Path('checkout'), 'base')), \
                patch.object(bot, 'run_updates', return_value={'dependencies': []}), \
                patch.object(bot, 'publish_update') as publish:
            bot.main([])
        self.assertFalse(publish.call_args.args[4])

    def test_base_image_digest_changes_only_when_needed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'versions.yml'
            path.write_text('{"image_version":"1.0.0"}')
            digest = 'sha256:' + 'a' * 64
            record_digest(digest, path)
            self.assertFalse(record_digest(digest, path))
            self.assertTrue(record_digest('sha256:' + 'b' * 64, path))
            self.assertEqual(json.loads(path.read_text())['image_version'], '1.0.0')
            with self.assertRaises(ValueError):
                record_digest('latest', path)
