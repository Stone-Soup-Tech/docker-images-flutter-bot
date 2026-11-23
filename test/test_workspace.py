"""Workspace resolution, reporting, and file boundary checks."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from sdk import flutter_bot as bot
from sdk.pub_workspace import Workspace


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='flutter-bot-workspace-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.manifests = {
            'pubspec.yaml': 'name: root\nworkspace:\n  - packages/*\n',
            'packages/app/pubspec.yaml': ('name: app\nresolution: workspace\ndependencies:\n'
                                          '  shared: ^1.0.0\n  helper: ^1.0.0\n'),
            'packages/helper/pubspec.yaml': ('name: helper\nresolution: workspace\n'
                                             'dev_dependencies:\n  shared: ^1.1.0\n'),
        }
        for name, content in self.manifests.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        (self.root / 'pubspec.lock').write_text('packages:\n  shared:\n    version: "1.1.0"\n')
        self.listing = {'packages': [
            {'name': name, 'path': str(self.root / path)}
            for name, path in (('root', '.'), ('app', 'packages/app'), ('helper', 'packages/helper'))
        ]}
        self.client = Mock()
        self.client.git_auth.return_value = ('https://example.test/', 'Bearer secret')

    def resolve(self, changed=True, prerelease=False, unexpected=None):
        version = '2.0.0-dev.1' if prerelease else '2.0.0'
        commands = []

        def pub(checkout, environment, *arguments, **kwargs):
            commands.append(arguments)
            if arguments[0] == 'workspace':
                return json.dumps(self.listing)
            if arguments[0] == 'upgrade' and changed:
                for name, content in self.manifests.items():
                    (self.root / name).write_text(content.replace('shared: ^1.0.0', f'shared: ^{version}')
                                               .replace('shared: ^1.1.0', f'shared: ^{version}'))
                (self.root / 'pubspec.lock').write_text(f'packages:\n  shared:\n    version: "{version}"\n')
            if arguments[0] == 'outdated':
                return json.dumps({'packages': [
                    {'package': 'shared', 'kind': 'direct', 'current': {'version': version},
                     'latest': {'version': '3.0.0'}},
                    {'package': 'helper', 'kind': 'direct', 'current': {'version': '1.0.0'}},
                ]})

        with patch.object(bot, 'pub', side_effect=pub), \
                patch.object(bot, 'git', return_value=unexpected or ''):
            update = bot.run_updates(self.root, self.client, 'base')
        return update, commands

    def test_updates_all_member_manifests_with_one_resolution(self):
        update, commands = self.resolve()
        self.assertEqual([command[0] for command in commands], ['workspace', 'upgrade', 'upgrade', 'outdated'])
        self.assertEqual(set(update['files']), {*self.manifests, 'pubspec.lock'})
        self.assertIn('| `packages/app/pubspec.yaml` | [shared]', update['pr-body'])
        self.assertIn('| dependencies | `^1.0.0` | `^2.0.0` |', update['pr-body'])
        self.assertIn('| dev_dependencies | `^1.1.0` | `^2.0.0` |', update['pr-body'])
        self.assertNotIn('[helper]', update['pr-body'])
        self.assertEqual(update['dependencies'], [{'name': 'shared', 'version': '2.0.0'}])

    def test_unchanged_workspace_has_no_update(self):
        self.assertIsNone(self.resolve(changed=False)[0])

    def test_held_back_versions_list_each_declaring_manifest(self):
        update, _ = self.resolve()
        table = update['pr-body'].split('Versions held back by compatibility constraints:')[1]
        self.assertIn('| Manifest | Package | Type | Resolved | Latest stable |', table)
        for manifest, kind in (('packages/app/pubspec.yaml', 'dependencies'),
                               ('packages/helper/pubspec.yaml', 'dev_dependencies')):
            self.assertIn(f'| `{manifest}` | [shared](https://pub.dev/packages/shared) | '
                          f'{kind} | `2.0.0` | `3.0.0` |', table)
        self.assertEqual(table.count('[shared]'), 2)

    def test_member_prerelease_prevents_publication(self):
        with self.assertRaisesRegex(RuntimeError, 'contain prereleases: shared'):
            self.resolve(prerelease=True)

    def test_unrelated_pub_changes_prevent_publication(self):
        for path in ('other/pubspec.yaml', 'other/pubspec.lock', 'pubspec_overrides.yaml'):
            with self.subTest(path=path), self.assertRaisesRegex(RuntimeError, 'outside the workspace'):
                self.resolve(unexpected=path)

    def test_rejects_workspace_outside_checkout(self):
        self.listing['packages'].append({'name': 'outside', 'path': str(self.root.parent)})
        with self.assertRaisesRegex(ValueError, 'escape the checkout'):
            Workspace.from_report(self.root, self.listing)

    def test_rejects_member_symlink_before_upgrading(self):
        manifest = self.root / 'packages/app/pubspec.yaml'
        manifest.unlink()
        manifest.symlink_to(self.root / 'pubspec.yaml')
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            self.resolve()

    def test_rejects_member_directory_symlink(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.root / 'packages/app', target_is_directory=True)
        self.listing['packages'][1]['path'] = str(alias)
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            Workspace.from_report(self.root, self.listing)

    def test_requires_workspace_root(self):
        self.listing['packages'].reverse()
        with self.assertRaisesRegex(ValueError, 'workspace root'):
            Workspace.from_report(self.root, self.listing)

    def test_rejects_stray_member_lockfile(self):
        (self.root / 'packages/app/pubspec.lock').write_text('old lock')
        with self.assertRaisesRegex(ValueError, 'one root lockfile'):
            self.resolve()

    def test_snapshot_revalidates_files_after_resolution(self):
        workspace = Workspace.from_report(self.root, self.listing)
        manifest = self.root / 'packages/app/pubspec.yaml'
        manifest.unlink()
        manifest.symlink_to(self.root / 'pubspec.yaml')
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            workspace.snapshot()
