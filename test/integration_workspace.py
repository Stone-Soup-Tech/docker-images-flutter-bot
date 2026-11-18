"""Real Pub workspace smoke test. Run explicitly with Flutter/Dart on PATH."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from sdk import flutter_bot as bot


class PubWorkspaceIntegration(unittest.TestCase):
    def test_real_pub_updates_shared_resolution_and_member_constraints(self):
        with tempfile.TemporaryDirectory(prefix='flutter-bot-real-workspace-') as temporary:
            root = Path(temporary).resolve()
            (root / 'pubspec.yaml').write_text(
                'name: bot_workspace_smoke\npublish_to: none\n'
                'environment:\n  sdk: ^3.6.0\nworkspace:\n  - packages/app\n  - packages/helper\n')
            for name, section in (('app', 'dependencies'), ('helper', 'dev_dependencies')):
                package = root / 'packages' / name
                package.mkdir(parents=True)
                (package / 'pubspec.yaml').write_text(
                    f'name: {name}\nversion: 1.0.0\npublish_to: none\nresolution: workspace\n'
                    f'environment:\n  sdk: ^3.6.0\n{section}:\n  collection: ^1.0.0\n'
                    + ('  helper: ^1.0.0\n' if name == 'app' else ''))
            (root / '.gitignore').write_text('.dart_tool/\n')
            bot.git(root, 'init', '--quiet')
            bot.git(root, 'add', '.')
            bot.git(root, '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                    '-c', 'commit.gpgSign=false', 'commit', '--quiet', '-m', 'base')
            # A dependency-free starting manifest plus old constraints makes
            # upgrades observable without pinning a particular latest release.
            environment = os.environ.copy()
            with patch.object(bot, 'git_environment', return_value=environment):
                update = bot.run_updates(root, Mock(), 'base')
                self.assertIsNotNone(update)
                self.assertEqual(set(update['files']), {'pubspec.yaml', 'pubspec.lock',
                    'packages/app/pubspec.yaml', 'packages/helper/pubspec.yaml'})
                for name in ('app', 'helper'):
                    manifest = root / 'packages' / name / 'pubspec.yaml'
                    self.assertNotIn('collection: ^1.0.0', manifest.read_text())
                    self.assertIn(f'packages/{name}/pubspec.yaml', update['pr-body'])
                    self.assertFalse((manifest.parent / 'pubspec.lock').exists())
                self.assertNotIn('[helper]', update['pr-body'])
                self.assertIn('helper: ^1.0.0', (root / 'packages/app/pubspec.yaml').read_text())
                self.assertTrue((root / 'pubspec.lock').is_file())
                self.assertIsNone(bot.run_updates(root, Mock(), 'base'))

    def test_single_package_still_resolves_without_workspace_declaration(self):
        with tempfile.TemporaryDirectory(prefix='flutter-bot-real-package-') as temporary:
            root = Path(temporary).resolve()
            (root / 'pubspec.yaml').write_text(
                'name: standalone_smoke\npublish_to: none\n'
                'environment:\n  sdk: ^3.6.0\ndependencies:\n  collection: ^1.0.0\n')
            bot.git(root, 'init', '--quiet')
            with patch.object(bot, 'git_environment', return_value=os.environ.copy()):
                update = bot.run_updates(root, Mock(), 'base')
            self.assertEqual(set(update['files']), {'pubspec.yaml', 'pubspec.lock'})
            self.assertIn('| [collection]', update['pr-body'])



if __name__ == '__main__':
    unittest.main()
