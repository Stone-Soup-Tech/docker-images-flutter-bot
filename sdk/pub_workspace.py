"""Pub workspace discovery, file boundaries, and dependency reporting."""

from dataclasses import dataclass
from pathlib import Path
import re


def validate_pub_file(root, path):
    """Reject symlinks in the entire path, including parent directories."""
    root = root.resolve()
    if not path.is_relative_to(root):
        raise ValueError("Pub files must not be symlinks or escape the checkout")
    if any(parent.is_symlink() for parent in (path, *path.parents) if parent.is_relative_to(root)):
        raise ValueError("Pub files must not be symlinks or escape the checkout")
    if not path.resolve().is_relative_to(root):
        raise ValueError("Pub files must not be symlinks or escape the checkout")


@dataclass(frozen=True)
class WorkspacePackage:
    name: str
    directory: Path


@dataclass(frozen=True)
class Workspace:
    root: Path
    packages: tuple[WorkspacePackage, ...]

    @classmethod
    def from_report(cls, checkout, report):
        root = checkout.resolve()
        packages = []
        for entry in report['packages']:
            directory = Path(entry['path'])
            if not directory.is_absolute():
                directory = root / directory
            validate_pub_file(root, directory / 'pubspec.yaml')
            directory = directory.resolve()
            if not (directory / 'pubspec.yaml').is_file():
                raise ValueError(f"Workspace package has no pubspec.yaml: {directory}")
            packages.append(WorkspacePackage(entry['name'], directory))
        directories = [package.directory for package in packages]
        if not directories or directories[0] != root:
            raise ValueError("Run flutter-bot at the Pub workspace root")
        if len(set(directories)) != len(directories):
            raise ValueError("Duplicate workspace package paths")
        workspace = cls(root, tuple(packages))
        for path in workspace.paths:
            validate_pub_file(root, path)
        # Pub removes stray member lockfiles during resolution. Refuse these
        # rather than silently deleting files outside the publishing allowlist.
        for package in packages[1:]:
            lockfile = package.directory / 'pubspec.lock'
            validate_pub_file(root, lockfile)
            if lockfile.exists():
                raise ValueError("Remove member pubspec.lock files; Pub workspaces use one root lockfile")
        return workspace

    @property
    def paths(self):
        return (self.root / 'pubspec.lock',
                *(package.directory / 'pubspec.yaml' for package in self.packages))

    @property
    def files(self):
        return tuple(path.relative_to(self.root).as_posix() for path in self.paths)

    def snapshot(self):
        result = {}
        for path, name in zip(self.paths, self.files):
            validate_pub_file(self.root, path)
            result[name] = path.read_bytes() if path.exists() else None
        return result

    def validate_changes(self, changed_paths):
        allowed = set(self.files)
        unexpected = [name for name in changed_paths
                      if Path(name).name in ('pubspec.yaml', 'pubspec.lock', 'pubspec_overrides.yaml')
                      and name not in allowed]
        if unexpected:
            raise RuntimeError("Pub files outside the workspace changed: " + ', '.join(unexpected))


def lockfile_versions(content):
    """Read package versions from Pub's generated lockfile format."""
    if content is None:
        return {}
    versions = {}
    package = None
    for line in content.decode().splitlines():
        match = re.fullmatch(r"  ([A-Za-z0-9_]+):", line)
        if match:
            package = match[1]
        elif package and line.startswith("    version: "):
            versions[package] = line.split(": ", 1)[1].strip("\"'")
    return versions


def manifest_dependencies(content):
    """Read dependency names/types and simple constraints for display only.

    Pub parses and edits YAML; this reader never controls resolution or writes it.
    Complex constraints retain their lockfile version as the display fallback.
    """
    dependencies = {}
    section = None
    package = None
    for line in content.decode().splitlines():
        if line and not line[0].isspace() and not line.startswith('#'):
            section = line.split(':', 1)[0]
            package = None
        if section not in ('dependencies', 'dev_dependencies'):
            continue
        match = re.fullmatch(r"  ([A-Za-z0-9_]+):\s*(.*)", line)
        if match:
            package, value = match.groups()
            dependencies[(section, package)] = None
        else:
            match = re.fullmatch(r"    version:\s*(.*)", line)
            if not match or not package:
                continue
            value = match[1]
        match = re.fullmatch(r"[\"']?(\^?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.+-]+)?)[\"']?\s*(?:#.*)?", value)
        if match:
            dependencies[(section, package)] = match[1]
    return dependencies


def update_report(workspace, before, after, packages):
    """Combine the shared resolution with each member's constraint changes."""
    previous_versions = lockfile_versions(before['pubspec.lock'])
    current_versions = lockfile_versions(after['pubspec.lock'])
    resolved = {package['package']: package for package in packages}
    local_names = {package.name for package in workspace.packages}
    rows = []
    direct_names = {package['package'] for package in packages if package.get('kind') in ('direct', 'dev')}
    multiple = len(workspace.packages) > 1
    for member in workspace.packages:
        manifest = (member.directory / 'pubspec.yaml').relative_to(workspace.root).as_posix()
        previous = manifest_dependencies(before[manifest])
        updated = manifest_dependencies(after[manifest])
        for (kind, name), constraint in updated.items():
            if name in local_names:
                continue
            direct_names.add(name)
            current = current_versions.get(name) or (resolved.get(name, {}).get('current') or {}).get('version')
            old = previous_versions.get(name)
            old_constraint = previous.get((kind, name))
            if current and (current != old or constraint != old_constraint):
                old_display = f"`{old_constraint or old}`" if old_constraint or old else 'Not locked'
                owner = f" `{manifest}` |" if multiple else ''
                rows.append(f"|{owner} [{name}](https://pub.dev/packages/{name}) | {kind} | {old_display} | `{constraint or current}` |")
    prereleases = []
    for name in sorted(direct_names - local_names):
        current = current_versions.get(name) or (resolved.get(name, {}).get('current') or {}).get('version')
        if current and '-' in current.split('+', 1)[0]:
            prereleases.append(name)
    if prereleases:
        raise RuntimeError('Stable-only policy: direct dependencies contain prereleases: ' + ', '.join(prereleases))
    held_back = []
    dependencies = []
    for package in packages:
        name = package['package']
        if name in local_names:
            continue
        current = (package.get('current') or {}).get('version')
        if current:
            dependencies.append({'name': name, 'version': current})
        latest = (package.get('latest') or {}).get('version')
        kind = {'direct': 'dependencies', 'dev': 'dev_dependencies',
                'transitive': 'transitive', 'transitive dev': 'transitive (dev_dependencies)'}.get(package.get('kind'), 'unknown')
        if current and latest and current != latest:
            held_back.append(f"| [{name}](https://pub.dev/packages/{name}) | {kind} | `{current}` | `{latest}` |")
    body = 'Updates direct dependency constraints to the newest mutually compatible stable versions and refreshes the lockfile.'
    body += '\n\nFlutter SDK pins and package compatibility constraints remain in effect.'
    if multiple:
        body += f'\n\nUpdates the shared resolution for {len(workspace.packages)} workspace packages.'
    if rows:
        owner_heading = ' Manifest |' if multiple else ''
        owner_separator = ' --- |' if multiple else ''
        body += ('\n\nUpdated direct dependencies (including dev dependencies):\n\n'
                 f'|{owner_heading} Package | Type | Previous | Updated |\n'
                 f'|{owner_separator} --- | --- | --- | --- |\n' + '\n'.join(rows))
    else:
        body += '\n\nRefreshed dependency constraints or transitive dependencies; direct dependency versions are unchanged.'
    if held_back:
        body += ('\n\nVersions held back by compatibility constraints:\n\n'
                 '| Package | Type | Resolved | Latest stable |\n| --- | --- | --- | --- |\n' + '\n'.join(held_back))
    return {'dependencies': dependencies, 'files': list(workspace.files),
            'pr-title': 'chore(deps): update Flutter dependencies',
            'commit-message': 'chore(deps): update Flutter dependencies', 'pr-body': body}
