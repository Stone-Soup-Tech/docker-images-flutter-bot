#!/usr/bin/env python3

"""Run Flutter Bot and maintain one grouped Bitbucket PR."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


GIT_API_TOKEN_USERNAME = "x-bitbucket-api-token-auth"
API_ROOT = "https://api.bitbucket.org/2.0/"


class NoRedirect(HTTPRedirectHandler):
    """Never forward authenticated API requests to a redirect destination."""

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def urlopen(request, timeout):
    return build_opener(NoRedirect).open(request, timeout=timeout)


class Bitbucket:
    """Access Bitbucket with the configured bot account and API token."""

    def __init__(self, base_branch=None):
        self.base_branch = base_branch
        self.repository = os.environ["BITBUCKET_REPO_FULL_NAME"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository):
            raise ValueError("Invalid BITBUCKET_REPO_FULL_NAME")

        self.username = os.environ["BOT_USERNAME"]
        self.token = os.environ["BOT_API_TOKEN"]
        self.authorization = "Basic " + base64.b64encode(
            f"{self.username}:{self.token}".encode()
        ).decode()
        self.repo_path = "repositories/" + quote(self.repository, safe="/")
        if self.base_branch is None:
            self.base_branch = os.environ.get("BITBUCKET_BRANCH")
        if not self.base_branch:
            repository = self.request(self.repo_path)
            self.base_branch = (repository.get("mainbranch") or {}).get("name")
        if not self.base_branch:
            raise ValueError("Repository has no default branch")
        if self.base_branch.startswith("flutter-bot/"):
            raise ValueError("Run the bot on an application branch, not its own update branch")
        git(Path.cwd(), "check-ref-format", "--branch", self.base_branch)

    def request(self, path, method="GET", data=None):
        """Make an authenticated request without following external pagination URLs."""
        url = path if path.startswith("https://") else API_ROOT + path
        if urlparse(url).netloc != "api.bitbucket.org":
            raise ValueError("Unexpected Bitbucket API host")

        body = json.dumps(data).encode() if data is not None else None
        request = Request(url, data=body, method=method, headers={
            "Authorization": self.authorization,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

        try:
            with urlopen(request, timeout=60) as response:
                return json.load(response)
        except HTTPError as error:
            # Retain the API explanation instead of hiding invalid-query details.
            detail = ""
            try:
                payload = json.loads(error.read(16384))
                detail = payload.get("error", {}).get("message", "")
            except (ValueError, AttributeError):
                pass
            finally:
                error.close()

            if isinstance(detail, str) and detail:
                for secret in (self.token, self.authorization,
                               os.environ.get("FLUTTER_BOT_READ_TOKEN")):
                    if secret:
                        detail = detail.replace(secret, "[REDACTED]")
                detail = " — " + " ".join(detail.split())[:1000]
            else:
                detail = ""
            raise RuntimeError(
                f"Bitbucket {method} {urlparse(url).path} failed: HTTP {error.code}{detail}"
            ) from None

    def values(self, path):
        """Read every page from a Bitbucket list endpoint."""
        while path:
            page = self.request(path)
            yield from page["values"]
            path = page.get("next")

    def branch(self, name):
        """Return a branch SHA, or None when the branch does not exist."""
        query = urlencode({"q": f'name = "{name}"', "pagelen": 100})
        branches = list(self.values(f"{self.repo_path}/refs/branches?{query}"))
        return branches[0]["target"]["hash"] if branches else None

    def pull_requests(self, update_branch):
        """Find current and declined PRs owned by this updater."""
        query = urlencode({
            "q": f'source.branch.name = "{update_branch}" AND '
                 f'destination.branch.name = "{self.base_branch}"',
            # This endpoint caps page size at 50 (unlike branch listings).
            "state": ["OPEN", "DECLINED"], "pagelen": 50,
        }, doseq=True)
        return list(self.values(f"{self.repo_path}/pullrequests?{query}"))

    def reviewers(self):
        """Use repository default reviewers, excluding the authenticated account."""
        current_user = self.request("user")["uuid"]
        candidates = self.values(f"{self.repo_path}/default-reviewers?pagelen=100")
        return [{"uuid": user["uuid"]} for user in candidates
                if user["uuid"] != current_user]


def git(directory, *arguments, env=None):
    """Run Git without storing credentials in command arguments or config files."""
    result = subprocess.run(
        ["git", "-C", str(directory), *arguments], env=env,
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def repository_url(client):
    return f"https://bitbucket.org/{client.repository}.git"


def check_base(client, base_sha):
    if client.branch(client.base_branch) != base_sha:
        raise RuntimeError("Base branch changed during the update; rerun the pipeline")


def prepare_checkout(client):
    """Validate the pipeline checkout before changing dependency files."""
    checkout = Path(os.environ.get("BITBUCKET_CLONE_DIR", Path.cwd())).resolve()
    try:
        root = Path(git(checkout, "rev-parse", "--show-toplevel")).resolve()
    except (subprocess.CalledProcessError, FileNotFoundError):
        raise RuntimeError("No Git checkout found; enable the Bitbucket pipeline clone") from None
    if root != checkout:
        raise ValueError("Run flutter-bot at the repository root or set BITBUCKET_CLONE_DIR")
    if git(checkout, "status", "--porcelain"):
        raise RuntimeError("Checkout must be clean before running flutter-bot")
    base_sha = git(checkout, "rev-parse", "HEAD")
    expected_sha = os.environ.get("BITBUCKET_COMMIT")
    if expected_sha and expected_sha != base_sha:
        raise RuntimeError("Checkout does not match BITBUCKET_COMMIT; rerun the pipeline")
    check_base(client, base_sha)
    return checkout, base_sha


def publish_update(checkout, client, update, base_sha, dry_run):
    """Create or refresh one grouped PR, using a lease to protect concurrent pushes."""
    update_branch = f"flutter-bot/dependencies-{client.base_branch}"
    fingerprint = hashlib.sha256(json.dumps(sorted(
        (dependency["name"], dependency.get("version"))
        for dependency in update["dependencies"]
    )).encode()).hexdigest()
    marker = f"<!-- flutter-bot-update:{fingerprint} -->"

    pull_requests = client.pull_requests(update_branch)
    opened = [pr for pr in pull_requests if pr["state"] == "OPEN"]
    if len(opened) > 1:
        raise RuntimeError("Multiple open Flutter Bot PRs found")
    if not opened and any(marker in (pr.get("description") or "") for pr in pull_requests):
        print("This update was declined; skipping until dependency versions change.")
        return

    title = update.get("pr-title") or "Update Flutter dependencies"
    description = (update.get("pr-body") or title) + "\n\n" + marker
    print(title)
    if dry_run:
        print("Dry run: no branches or pull requests changed.")
        return

    reviewers = client.reviewers() if not opened else None
    previous_sha = client.branch(update_branch)
    environment = git_environment(client)
    environment.update({
        "GIT_AUTHOR_NAME": "Flutter Bot", "GIT_COMMITTER_NAME": "Flutter Bot",
        "GIT_AUTHOR_EMAIL": "flutter-bot@users.noreply.github.com",
        "GIT_COMMITTER_EMAIL": "flutter-bot@users.noreply.github.com",
    })
    if git(checkout, "rev-parse", "HEAD") != base_sha:
        raise RuntimeError("Checkout commit changed during resolution; rerun the pipeline")
    if git(checkout, "diff", "--cached", "--name-only"):
        raise RuntimeError("Unexpected staged files; refusing to publish")
    git(checkout, "add", "--", "pubspec.yaml", "pubspec.lock")
    staged = git(checkout, "diff", "--cached", "--name-only").splitlines()
    if not staged:
        print("No file changes to publish.")
        return
    if set(staged) - {"pubspec.yaml", "pubspec.lock"}:
        raise RuntimeError("Only root Pub files may be published")

    tree = git(checkout, "write-tree")
    unchanged = False
    if previous_sha:
        git(checkout, "fetch", "--no-tags", "--depth=1", repository_url(client), update_branch, env=environment)
        if git(checkout, "rev-parse", "FETCH_HEAD") != previous_sha:
            raise RuntimeError("Flutter Bot branch changed during the update; rerun the pipeline")
        unchanged = tree == git(checkout, "rev-parse", f"{previous_sha}^{{tree}}")

    check_base(client, base_sha)
    if not unchanged:
        git(checkout, "commit", "--quiet", "-m",
            update.get("commit-message") or title, env=environment)
        git(checkout, "push", f"--force-with-lease=refs/heads/{update_branch}:{previous_sha or ''}",
            repository_url(client), f"HEAD:refs/heads/{update_branch}", env=environment)

    payload = {"title": title, "description": description}
    if opened:
        result = client.request(f"{client.repo_path}/pullrequests/{opened[0]['id']}",
                                "PUT", payload)
    else:
        payload.update({"source": {"branch": {"name": update_branch}},
                        "destination": {"branch": {"name": client.base_branch}},
                        "close_source_branch": True, "reviewers": reviewers})
        result = client.request(f"{client.repo_path}/pullrequests", "POST", payload)
    print("Pull request: " + result["links"]["html"]["href"])


def git_environment(client):
    """Authenticate Git and private Pub Git dependencies without writing secrets."""
    environment = os.environ.copy()
    environment.update({
        "CI": "true", "PUB_ENVIRONMENT": "flutter_bot",
        "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "http.https://bitbucket.org/.extraHeader",
        # Clear checkout-supplied headers before supplying the publishing account.
        "GIT_CONFIG_VALUE_0": "",
        "GIT_CONFIG_KEY_1": "http.https://bitbucket.org/.extraHeader",
        "GIT_CONFIG_VALUE_1": "Authorization: Basic " + base64.b64encode(
            f"{GIT_API_TOKEN_USERNAME}:{client.token}".encode()).decode(),
    })
    return environment


def pub(checkout, environment, *arguments, capture=False):
    started = time.monotonic()
    print("Running flutter pub " + " ".join(arguments), flush=True)
    result = subprocess.run(["flutter", "pub", *arguments], cwd=checkout,
                            env=environment, text=True, check=True,
                            stdout=subprocess.PIPE if capture else None)
    print(f"Finished in {time.monotonic() - started:.1f}s", flush=True)
    return result.stdout if capture else None


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


def run_updates(checkout, client, base_sha):
    """Resolve dependencies directly in the pipeline's disposable checkout."""
    environment = git_environment(client)
    project = checkout
    paths = [project / name for name in ("pubspec.yaml", "pubspec.lock")]
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(checkout.resolve()):
            raise ValueError("Pub files must not be symlinks or escape the checkout")
    if not paths[0].is_file():
        raise ValueError("No pubspec.yaml in the selected directory")
    original = [path.read_bytes() if path.exists() else None for path in paths]
    # Pub selects stable releases unless a package already uses a prerelease.
    # Enforce stable versions for direct dependencies, including dev dependencies.
    # Transitive prereleases are controlled by the packages that require them.
    pub(project, environment, "upgrade", "--major-versions", "--no-precompile", "--no-example")
    pub(project, environment, "upgrade", "--tighten", "--no-precompile", "--no-example")
    allowed = {str(path.relative_to(checkout)) for path in paths}
    changed_paths = git(checkout, "diff", "--name-only").splitlines()
    if any(Path(name).name in ("pubspec.yaml", "pubspec.lock") and name not in allowed
           for name in changed_paths):
        raise RuntimeError("Multi-package Pub workspaces are unsupported; other Pub files changed")
    report = json.loads(pub(project, environment, "outdated", "--json", "--no-prereleases", "--show-all", capture=True))
    packages = report["packages"]
    prereleases = [p["package"] for p in packages
                   if p.get("kind") in ("direct", "dev") and p.get("current")
                   and "-" in p["current"]["version"].split("+", 1)[0]]
    if prereleases:
        raise RuntimeError("Stable-only policy: direct dependencies contain prereleases: " + ", ".join(prereleases))
    updated = [path.read_bytes() for path in paths]
    if original == updated:
        return None
    previous_versions = lockfile_versions(original[1])
    rows = []
    held_back = []
    dependencies = []
    for package in packages:
        current = (package.get("current") or {}).get("version")
        if current:
            dependencies.append({"name": package["package"], "version": current})
        latest = (package.get("latest") or {}).get("version")
        if current and latest and current != latest:
            held_back.append(f"| {package['package']} | {current} | {latest} |")
        previous = previous_versions.get(package["package"])
        if package.get("kind") in ("direct", "dev") and current and current != previous:
            rows.append(f"| {package['package']} | {previous or 'Not locked'} | {current} |")
    body = "Updates direct dependency constraints to the newest mutually compatible stable versions and refreshes the lockfile."
    body += "\n\nFlutter SDK pins and package compatibility constraints remain in effect."
    if rows:
        body += "\n\nUpdated direct dependencies (including dev dependencies):\n\n| Package | Previous | Updated |\n| --- | --- | --- |\n" + "\n".join(rows)
    else:
        body += "\n\nRefreshed dependency constraints or transitive dependencies; direct dependency versions are unchanged."
    if held_back:
        body += "\n\nVersions held back by compatibility constraints:\n\n| Package | Resolved | Latest stable |\n| --- | --- | --- |\n" + "\n".join(held_back)
    print(body, flush=True)
    return {"dependencies": dependencies, "pr-title": "chore(deps): update Flutter dependencies",
            "commit-message": "chore(deps): update Flutter dependencies", "pr-body": body}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args(argv)
    dry_run = os.environ.get("FLUTTER_BOT_DRY_RUN", "false")
    if dry_run not in ("true", "false"):
        parser.error("FLUTTER_BOT_DRY_RUN must be true or false")
    client = Bitbucket()
    checkout, base_sha = prepare_checkout(client)
    update = run_updates(checkout, client, base_sha)
    if update:
        publish_update(checkout, client, update, base_sha,
                       arguments.dry_run or dry_run == "true")
    else:
        print("No dependency updates found.")


if __name__ == "__main__":
    main()
