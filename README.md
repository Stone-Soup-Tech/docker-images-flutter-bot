# Flutter dependency bot for Bitbucket

A Docker image containing Flutter and a Python script that updates `pubspec.yaml`
and `pubspec.lock`, then maintains one Bitbucket pull request. Repository layout: `sdk/`, `scripts/`, `test/`, `examples/`,
`versions.yml` and GitHub Actions for validation and publishing.

Base image: [`ghcr.io/stone-soup-tech/flutter:latest`](https://github.com/Stone-Soup-Tech/docker-images-flutter).
Published bot: `ghcr.io/stone-soup-tech/flutter-bot:latest` (after initial publication).
Flutter is installed in the image; the consumer pipeline runs the updater directly. The base includes Android tools, so image pulling
can still be substantial. Runtime improvements must be measured in Bitbucket.

## Updating dependencies

```sh
flutter-bot
# Preview without publishing:
flutter-bot --dry-run
```

The runner detects and checks out the base branch in a temporary directory and runs:

```sh
flutter pub upgrade --major-versions --no-precompile --no-example
flutter pub upgrade --tighten --no-precompile --no-example
flutter pub outdated --json --no-prereleases --show-all
```

Targets the newest mutually compatible **stable** versions, across major, minor,
patch and build versions as selected by Pub. `--major-versions` relaxes existing
upper bounds and updates constraints; `--tighten` raises dependency lower bounds
to the resolved versions. The lockfile records the resolved versions of direct,
development and transitive dependencies. SDK dependencies and their pins remain
under Flutter's control. The script does not add dependency overrides, force
incompatible packages, edit SDK constraints or upgrade Flutter during a run.

`outdated` supplies the PR report, including packages whose latest version could
not be selected. Resolution failure prevents publication. Pub can retain a
prerelease when the existing project already requires one; the runner rejects any
prerelease reported in the resolved graph rather than publishing it under the
stable-only policy. Build suffixes such as `1.2.3+4` are supported, following Pub's
version ordering rather than publication-date ordering.

Only the repository root's two Pub files are published. Single application
roots are supported; multi-package Pub workspaces are rejected if resolution
changes other Pub manifests or lockfiles. Git/path dependencies remain governed
by their declarations; this bot does not discover new Git tags or rewrite paths.
Private package registries require their normal Pub authentication configuration.

## Bitbucket setup

See [examples/bitbucket-pipelines.yml](examples/bitbucket-pipelines.yml). Configure
secured repository/workspace variables:

| Variable | Purpose |
| --- | --- |
| `BOT_USERNAME` | Atlassian account email for REST Basic authentication |
| `BOT_API_TOKEN` | API token for Git and REST; repository and PR read/write, user read |
| `FLUTTER_BOT_DRY_RUN` | Optional `true` or `false`, defaults to `false` |

Git uses `x-bitbucket-api-token-auth`, independently of the REST email. Git
credentials are passed in environment configuration, not command arguments or
persistent Git configuration. They also apply to private Bitbucket Git Pub
packages accessible with the token. Do not enable shell tracing around secrets.

The only command option is `--dry-run`. The bot updates the repository root,
uses `BITBUCKET_BRANCH` in Pipelines (the branch the pipeline runs on), and falls
back to the repository's default branch outside Pipelines. It assigns the
repository's default reviewers, excluding the authenticated bot account.
`--dry-run` always enables dry-run. Dry-run resolves in the temporary checkout and
prints the diff but does not push, create or modify PRs. Credentials are still
required to fetch the base and inspect existing PRs.

Cache `/opt/pub-cache` to reuse packages. No Docker service is needed. Schedule the pipeline on the branch you want updated, such as `dev`. Run alongside independent Android and Renovate jobs to reduce elapsed
time. Add validation for `flutter-bot/*` branches in the consuming repository.

Branches use `flutter-bot/dependencies-<base-branch>`.
Unchanged trees do not create another commit. Declining a PR suppresses the same
resolved dependency-version fingerprint until versions change. Updates use
force-with-lease and abort if the source or update branch changes concurrently.
No-update runs leave existing PRs open. Branches are bot-owned. Temporary files
are cleaned up on exit, including handled SIGTERM. Step timeouts rely on CI
container disposal. Existing branch lint/test jobs remain responsible for testing
API-breaking major updates.

## Flutter base-image updates

A published bot contains the Flutter base image used **at build time**. Pulling
`flutter-bot:latest` does not dynamically combine it with `flutter:latest`.

The publish workflow runs daily at 00:00 UTC, on relevant pushes to `main`, and on
manual dispatch. It resolves the current Flutter `latest` digest and compares it
with `flutter_base_digest` in `versions.yml`. A changed base automatically bumps
the bot image patch version, records the digest, validates and publishes a new
version and `latest`. The build uses the resolved digest so the recorded base and
built base match. No Flutter-repository modifications or cross-repository token
are required. Scheduled delivery can lag by a day and GitHub scheduling delays.
Manual dispatch can pick up a base-image change immediately.

A lightweight preflight checks the digest before validation. Scheduled runs with
an unchanged recorded digest skip validation, image builds and publication.
The first run (no recorded digest), pushes and manual runs continue through the
normal release checks. Validation and publication use the same resolved base
digest, even if the upstream `latest` tag changes during the workflow.

The first release records the base digest without a version bump. Subsequent
base changes bump the version. Unchanged base and existing version tags skip
publication. Runner changes intended for release must bump `image_version` or
use manual `increase-type: patch|minor|major`. Version selection and automatic
carry-at-10 behavior are supported. Immutable version tags are
never deliberately overwritten; `latest` follows the newest published release.

Enable Actions on the GitHub repository, permit `GITHUB_TOKEN` to push version
commits to `main`, and grant package write access. Branch protection that blocks
the workflow's version commit will prevent automatic releases; it is not bypassed.
Make the GHCR package public for anonymous Bitbucket pulls. Scheduled workflows
run on the default branch; use `main` as the default branch. GitHub may disable
inactive public-repository schedules. CI must pass before publication. If a build
fails after the version commit, rerun with `increase-type: NONE`.

Consumers using `:latest` pick up the refreshed bot on subsequent image pulls;
consumers pinned to a version or digest must update that pin. Compare the bot's
reported `flutter --version` with your application build environment.

## Development

```sh
python3 -B -m unittest discover -s test -p 'test_*.py'
docker build --pull -f sdk/Dockerfile -t flutter-bot:test .
docker run --rm flutter-bot:test flutter-bot --help
```

CI builds the image and exercises real Pub resolution against a small fixture.
Unit tests cover resolver failures, stable-only enforcement, dry-run, file path
boundaries, Git leases, duplicate commits, declined PRs, reviewer selection and
release version changes. Real Bitbucket authentication, PR publication and
performance require a consumer pipeline run.

References: [Pub upgrade](https://dart.dev/tools/pub/cmd/pub-upgrade),
[Pub outdated](https://dart.dev/tools/pub/cmd/pub-outdated).
