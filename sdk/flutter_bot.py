#!/usr/bin/env python3

"""Run Flutter Bot and maintain one grouped dependency PR."""

import argparse
from abc import ABC, abstractmethod
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


if __package__:
    from .pub_workspace import Workspace, update_report, validate_pub_file
else:
    from pub_workspace import Workspace, update_report, validate_pub_file


GIT_API_TOKEN_USERNAME = "x-bitbucket-api-token-auth"
API_ROOT = "https://api.bitbucket.org/2.0/"


class NoRedirect(HTTPRedirectHandler):
    """Never forward authenticated API requests to a redirect destination."""

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def urlopen(request, timeout):
    return build_opener(NoRedirect).open(request, timeout=timeout)


class Provider(ABC):
    """Platform boundary; PRs use id, state (OPEN/DECLINED), description."""

    @abstractmethod
    def branch(self, name):
        """Return the exact branch SHA, or None."""

    @abstractmethod
    def pull_requests(self, update_branch):
        """Return open and declined PRs for the source and target branches."""

    @abstractmethod
    def save_pull_request(self, opened, update_branch, title, description):
        """Create or refresh a PR and return its browser URL."""

    @abstractmethod
    def git_auth(self):
        """Return (URL scope, Authorization header value)."""

    @property
    @abstractmethod
    def repository_url(self):
        """Credential-free HTTPS clone URL."""

    def validate_branch(self):
        if not self.base_branch:
            raise ValueError("Repository has no default branch")
        if self.base_branch.startswith("flutter-bot/"):
            raise ValueError("Run the bot on an application branch, not its own update branch")
        git(Path.cwd(), "check-ref-format", "--branch", self.base_branch)


class Bitbucket(Provider):
    """Access Bitbucket with the configured bot account and API token."""

    name = "Bitbucket"
    checkout_variable = "BITBUCKET_CLONE_DIR"
    commit_variable = "BITBUCKET_COMMIT"

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
        self.validate_branch()

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

    @property
    def repository_url(self):
        return f"https://bitbucket.org/{self.repository}.git"

    def git_auth(self):
        encoded = base64.b64encode(f"{GIT_API_TOKEN_USERNAME}:{self.token}".encode()).decode()
        return "https://bitbucket.org/", "Basic " + encoded

    def save_pull_request(self, opened, update_branch, title, description):
        payload = {"title": title, "description": description}
        if opened:
            result = self.request(f"{self.repo_path}/pullrequests/{opened['id']}", "PUT", payload)
        else:
            payload.update({"source": {"branch": {"name": update_branch}},
                            "destination": {"branch": {"name": self.base_branch}},
                            "close_source_branch": True, "reviewers": self.reviewers()})
            result = self.request(f"{self.repo_path}/pullrequests", "POST", payload)
        return result["links"]["html"]["href"]


class Azure(Provider):
    """Azure DevOps Services using the pipeline job's OAuth token."""

    name = "Azure DevOps"
    checkout_variable = "BUILD_SOURCESDIRECTORY"
    commit_variable = "BUILD_SOURCEVERSION"

    def __init__(self):
        collection = os.environ["SYSTEM_COLLECTIONURI"]
        # Constrain both REST and Git credentials to a single cloud organization.
        if not re.fullmatch(r"https://dev\.azure\.com/[A-Za-z0-9_-]+/?", collection):
            raise ValueError("SYSTEM_COLLECTIONURI must be https://dev.azure.com/<organization>/")
        self.collection = collection.rstrip("/") + "/"
        project = quote(os.environ["SYSTEM_TEAMPROJECTID"], safe="")
        repository = quote(os.environ["BUILD_REPOSITORY_ID"], safe="")
        self.repo_path = f"{project}/_apis/git/repositories/{repository}"
        self.token = os.environ["SYSTEM_ACCESSTOKEN"]
        if not self.token:
            raise ValueError("Map System.AccessToken to SYSTEM_ACCESSTOKEN")
        self.authorization = "Bearer " + self.token
        self._repository_url = f"{self.collection}{project}/_git/{repository}"
        branch = os.environ.get("BUILD_SOURCEBRANCH")
        if not branch:
            branch = self.request(self.repo_path).get("defaultBranch")
        if not branch or not branch.startswith("refs/heads/"):
            raise ValueError("Run Azure updates on a branch (refs/heads/...), not a PR or tag")
        self.base_branch = branch[len("refs/heads/"):]
        self.validate_branch()

    @property
    def repository_url(self):
        return self._repository_url

    def git_auth(self):
        return self.collection, self.authorization

    def request(self, path, method="GET", data=None):
        # Paths are generated internally; never accept a remote pagination URL.
        if path.startswith("/") or ":" in path or ".." in path.split("/"):
            raise ValueError("Unexpected Azure API path")
        url = self.collection + path + ("&" if "?" in path else "?") + "api-version=7.1"
        request = Request(url, data=json.dumps(data).encode() if data is not None else None,
                          method=method, headers={"Authorization": self.authorization,
                          "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urlopen(request, timeout=60) as response:
                payload = json.load(response)
                continuation = response.headers.get("x-ms-continuationtoken")
                if continuation:
                    payload["_continuation"] = continuation
                return payload
        except HTTPError as error:
            detail = ""
            try:
                detail = json.loads(error.read(16384)).get("message", "")
            except (ValueError, AttributeError):
                pass
            finally:
                error.close()
            if isinstance(detail, str):
                for secret in (self.authorization, self.token):
                    detail = detail.replace(secret, "[REDACTED]")
                detail = " — " + " ".join(detail.split())[:1000] if detail else ""
            else:
                detail = ""
            raise RuntimeError(f"Azure {method} request failed: HTTP {error.code}{detail}") from None

    def branch(self, name):
        continuation = None
        while True:
            parameters = {"filter": "heads/" + name, "$top": 1000}
            if continuation:
                parameters["continuationToken"] = continuation
            page = self.request(f"{self.repo_path}/refs?{urlencode(parameters)}")
            for ref in page["value"]:
                if ref["name"] == "refs/heads/" + name:
                    return ref["objectId"]
            continuation = page.get("_continuation")
            if not continuation:
                return None

    def pull_requests(self, update_branch):
        result = []
        for status, state in (("active", "OPEN"), ("abandoned", "DECLINED")):
            skip = 0
            while True:
                query = urlencode({"searchCriteria.status": status,
                    "searchCriteria.sourceRefName": "refs/heads/" + update_branch,
                    "searchCriteria.targetRefName": "refs/heads/" + self.base_branch,
                    "$top": 100, "$skip": skip})
                page = self.request(f"{self.repo_path}/pullrequests?{query}")["value"]
                for pr in page:
                    # List responses truncate descriptions to 400 characters.
                    # The suppression marker can be at the end of a long body.
                    detail = self.request(f"{self.repo_path}/pullrequests/{pr['pullRequestId']}") if state == "DECLINED" else pr
                    result.append({"id": pr["pullRequestId"], "state": state,
                                   "description": detail.get("description", "")})
                if not page:
                    break
                skip += len(page)
        return result

    @staticmethod
    def description_for_api(description):
        """Keep the suppression marker while fitting Azure's 4000-character limit."""
        if len(description.encode("utf-16-le")) // 2 <= 4000:
            return description
        marker = re.search(r"\n\n<!-- flutter-bot-update:[a-f0-9]{64} -->$", description)
        suffix = "\n\nSummary shortened; see file changes and pipeline logs for the full update."
        if marker:
            suffix += marker.group()
        budget = 4000 - len(suffix.encode("utf-16-le")) // 2
        prefix = description.encode("utf-16-le")[:budget * 2].decode("utf-16-le", errors="ignore")
        return prefix.rsplit("\n", 1)[0] + suffix

    def save_pull_request(self, opened, update_branch, title, description):
        payload = {"title": title, "description": self.description_for_api(description)}
        path = f"{self.repo_path}/pullrequests"
        if opened:
            path += f"/{opened['id']}"
        else:
            payload.update({"sourceRefName": "refs/heads/" + update_branch,
                            "targetRefName": "refs/heads/" + self.base_branch})
        result = self.request(path, "PATCH" if opened else "POST", payload)
        return self.repository_url + "/pullrequest/" + str(result["pullRequestId"])


def select_provider(explicit):
    if explicit == "azure":
        return Azure()
    if explicit == "bitbucket":
        return Bitbucket()
    azure = os.environ.get("TF_BUILD", "").lower() == "true"
    bitbucket = bool(os.environ.get("BITBUCKET_REPO_FULL_NAME"))
    if azure and bitbucket:
        raise ValueError("Ambiguous pipeline environment; specify --provider")
    if azure:
        return Azure()
    if bitbucket:
        return Bitbucket()
    raise ValueError("No pipeline provider detected; specify --provider bitbucket or --provider azure")


def git(directory, *arguments, env=None):
    """Run Git without storing credentials in command arguments or config files."""
    result = subprocess.run(
        ["git", "-C", str(directory), *arguments], env=env,
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def repository_url(client):
    return client.repository_url


def check_base(client, base_sha):
    if client.branch(client.base_branch) != base_sha:
        raise RuntimeError("Base branch changed during the update; rerun the pipeline")


def prepare_checkout(client):
    """Validate the pipeline checkout before changing dependency files."""
    checkout = Path(os.environ.get(client.checkout_variable, Path.cwd())).resolve()
    try:
        root = Path(git(checkout, "rev-parse", "--show-toplevel")).resolve()
    except (subprocess.CalledProcessError, FileNotFoundError):
        raise RuntimeError(f"No Git checkout found; enable the {client.name} pipeline clone") from None
    if root != checkout:
        raise ValueError(f"Run flutter-bot at the repository root or set {client.checkout_variable}")
    if git(checkout, "status", "--porcelain"):
        raise RuntimeError("Checkout must be clean before running flutter-bot")
    base_sha = git(checkout, "rev-parse", "HEAD")
    expected_sha = os.environ.get(client.commit_variable)
    if expected_sha and expected_sha != base_sha:
        raise RuntimeError(f"Checkout does not match {client.commit_variable}; rerun the pipeline")
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
    allowed = set(update.get("files", ("pubspec.yaml", "pubspec.lock")))
    if not allowed or "pubspec.yaml" not in allowed or "pubspec.lock" not in allowed:
        raise ValueError("Update must include the root Pub files")
    for name in allowed:
        path = checkout / name
        if (Path(name).is_absolute() or ".." in Path(name).parts
                or path.name not in ("pubspec.yaml", "pubspec.lock")
                or (path.name == "pubspec.lock" and name != "pubspec.lock")):
            raise ValueError("Invalid Pub file in update")
        validate_pub_file(checkout.resolve(), path)
    git(checkout, "--literal-pathspecs", "add", "--", *sorted(allowed))
    staged = [name for name in git(checkout, "diff", "--cached", "--name-only", "-z").split("\0") if name]
    if not staged:
        print("No file changes to publish.")
        return
    if set(staged) - allowed:
        raise RuntimeError("Only discovered workspace Pub files may be published")

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

    url = client.save_pull_request(opened[0] if opened else None, update_branch, title, description)
    print("Pull request: " + url)


def git_environment(client):
    """Authenticate Git and private Pub Git dependencies without writing secrets."""
    scope, authorization = client.git_auth()
    environment = os.environ.copy()
    environment.update({
        "CI": "true", "PUB_ENVIRONMENT": "flutter_bot",
        "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": f"http.{scope}.extraHeader",
        "GIT_CONFIG_VALUE_0": "",
        "GIT_CONFIG_KEY_1": f"http.{scope}.extraHeader",
        "GIT_CONFIG_VALUE_1": "Authorization: " + authorization,
    })
    return environment


def pub(checkout, environment, *arguments, capture=False, tool="flutter"):
    started = time.monotonic()
    print(f"Running {tool} pub " + " ".join(arguments), flush=True)
    result = subprocess.run([tool, "pub", *arguments], cwd=checkout,
                            env=environment, text=True, check=True,
                            stdout=subprocess.PIPE if capture else None)
    print(f"Finished in {time.monotonic() - started:.1f}s", flush=True)
    return result.stdout if capture else None


def run_updates(checkout, client, base_sha):
    """Resolve the entire Pub workspace once, in the disposable checkout."""
    checkout = checkout.resolve()
    environment = git_environment(client)
    validate_pub_file(checkout, checkout / "pubspec.yaml")
    validate_pub_file(checkout, checkout / "pubspec.lock")
    if not (checkout / "pubspec.yaml").is_file():
        raise ValueError("No pubspec.yaml in the selected directory")
    try:
        listing = json.loads(pub(checkout, environment, "workspace", "list", "--json",
                                 capture=True, tool="dart"))
    except subprocess.CalledProcessError:
        raise RuntimeError("Cannot list Pub workspace packages; use Dart 3.13+ and a valid root pubspec.yaml") from None
    workspace = Workspace.from_report(checkout, listing)
    original = workspace.snapshot()
    # Both commands operate on the shared resolution and every member manifest.
    pub(checkout, environment, "upgrade", "--major-versions", "--no-precompile", "--no-example")
    pub(checkout, environment, "upgrade", "--tighten", "--no-precompile", "--no-example")
    changed = git(checkout, "diff", "--name-only", "-z").split("\0")
    untracked = git(checkout, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    workspace.validate_changes(name for name in (*changed, *untracked) if name)
    report = json.loads(pub(checkout, environment, "outdated", "--json", "--no-prereleases",
                            "--show-all", capture=True))
    updated = workspace.snapshot()
    update = update_report(workspace, original, updated, report["packages"])
    if original == updated:
        return None
    print(update["pr-body"], flush=True)
    return update


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--provider", choices=("bitbucket", "azure"),
                        help="Git hosting provider (required when no pipeline provider is detected)")
    arguments = parser.parse_args(argv)
    dry_run = os.environ.get("FLUTTER_BOT_DRY_RUN", "false")
    if dry_run not in ("true", "false"):
        parser.error("FLUTTER_BOT_DRY_RUN must be true or false")
    client = select_provider(arguments.provider)
    checkout, base_sha = prepare_checkout(client)
    update = run_updates(checkout, client, base_sha)
    if update:
        publish_update(checkout, client, update, base_sha,
                       arguments.dry_run or dry_run == "true")
    else:
        print("No dependency updates found.")


if __name__ == "__main__":
    main()
