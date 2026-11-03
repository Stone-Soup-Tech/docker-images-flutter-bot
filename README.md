# Flutter dependency bot for Bitbucket

[![Publish Flutter bot image](https://github.com/Stone-Soup-Tech/docker-images-flutter-bot/actions/workflows/publish.yml/badge.svg)](https://github.com/Stone-Soup-Tech/docker-images-flutter-bot/actions/workflows/publish.yml) [![GHCR version](https://ghcr-badge.egpl.dev/Stone-Soup-Tech/flutter-bot/latest_tag?trim=major&label=latest)](https://ghcr.io/stone-soup-tech/flutter-bot)

Update `pubspec.yaml` and `pubspec.lock` to the newest compatible stable dependency
versions and maintain one grouped Bitbucket pull request. Flutter, Python and Git
are installed in the image.

## Usage

Image: `ghcr.io/stone-soup-tech/flutter-bot:latest`.
Source: [Stone-Soup-Tech/docker-images-flutter-bot](https://github.com/Stone-Soup-Tech/docker-images-flutter-bot).

```yaml
- step:
    name: Update Flutter dependencies
    image: ghcr.io/stone-soup-tech/flutter-bot:latest
    concurrency-group: flutter-bot-pub
    script:
      - flutter-bot
```

Keep Bitbucket's default repository cloning enabled. The bot updates the existing
checkout directly; it does not clone or fetch another copy of the base repository.
Use the complete [pipeline example](examples/bitbucket-pipelines.yml) for Pub
caching, a manual dry-run toggle and a step timeout. Merge its sections into your
existing pipeline rather than replacing unrelated steps.

## Credentials and setup

Configure these as **secured Bitbucket repository or workspace variables**:

| Variable | Purpose |
| --- | --- |
| `BOT_USERNAME` | Atlassian account email for Bitbucket REST authentication. |
| `BOT_API_TOKEN` | Account API token for Git access and pull request publishing. |

The account needs repository read/write, pull request read/write and user read
permissions, and permission to push `flutter-bot/*` branches. It also needs access
to any private Bitbucket Git dependencies used by the application.

Git uses `x-bitbucket-api-token-auth` with the token; REST uses the account email.
Credentials are passed through environment configuration, not Git command
arguments or persistent Git configuration. Avoid shell tracing around secrets.
Private package registries require their normal Pub authentication configuration.

Repository identity comes from Bitbucket's built-in `BITBUCKET_REPO_FULL_NAME`.
The bot uses `BITBUCKET_CLONE_DIR` as the checkout directory, or the current
directory outside Pipelines. Run it from a clean repository root before other
steps modify files. It verifies `HEAD` against `BITBUCKET_COMMIT` when available
and checks that the target branch still points to the same commit.
The target branch comes from `BITBUCKET_BRANCH`: running the pipeline on `dev`
updates `dev`. Outside Pipelines, provide `BITBUCKET_REPO_FULL_NAME` and the
credentials and a clean local checkout; the bot falls back to the repository's default branch when
`BITBUCKET_BRANCH` is absent. It uses the repository's default reviewers,
excluding the authenticated account.

## Dry-run

```sh
flutter-bot --dry-run
```

Dry-run resolves dependencies in the pipeline checkout, prints the proposed diff
and checks existing PRs, without committing, pushing branches or creating or
modifying PRs. It leaves the proposed changes in the local checkout. Bitbucket
discards that checkout when the step ends; when running locally, use a disposable
checkout if you want to preserve your working files. Credentials and network
access are still required.

Alternatively, set `FLUTTER_BOT_DRY_RUN=true`. It accepts `true` or `false` and
defaults to `false`; `--dry-run` always enables dry-run. This is the only command
option needed for normal use: branch, grouping and reviewers are automatic.

## Scheduling and validation

1. Add the pipeline and secured credentials to your repository.
2. Run it manually on the branch you want updated, with dry-run enabled. The
   complete example defaults its `DRY_RUN` input to `true` for this initial check.
3. Run with `DRY_RUN=false` to create or refresh the dependency PR.
4. In **Pipelines → Schedules**, schedule the custom pipeline on that same branch.
   Committing the YAML does not create a schedule. Set `DRY_RUN=false` on the
   schedule if you want it to publish updates.

Add your normal lint, test and build checks for `flutter-bot/*` branches. Major
updates can require application changes even when dependency resolution succeeds.
The bot opens a PR; it does not merge it or verify application behavior.

Keep a concurrency group to prevent overlapping updater runs on the same branch.
Independent Android or pipeline-image update jobs can run in parallel. Use a tool
such as Renovate for those dependency types; this image updates Pub dependencies.

## What gets updated

The bot updates the repository root's `pubspec.yaml` and `pubspec.lock`, including
direct and development dependencies and the resolved transitive dependency graph.
It selects the newest mutually compatible **stable** versions across major,
minor, patch and build releases. For example, a compatible `1.2.3+4` can replace
`1.2.3+3`. Selection follows Pub's version ordering, rather than publication date.

The update commands are:

```sh
flutter pub upgrade --major-versions --no-precompile --no-example
flutter pub upgrade --tighten --no-precompile --no-example
flutter pub outdated --json --no-prereleases --show-all
```

`--major-versions` allows upgrades beyond existing upper bounds and updates
constraints. `--tighten` raises dependency lower bounds to their resolved
versions, including minor and patch updates. The lockfile records the exact
resolved versions. The PR report lists packages held below their latest stable
release by compatibility constraints.

Flutter SDK dependencies and their pins remain controlled by Flutter. The bot
does not force incompatible versions, add dependency overrides, edit SDK
constraints or upgrade Flutter during a run. Git/path dependencies follow their
existing declarations; the bot does not discover new Git tags or rewrite paths.

Resolution failures prevent publication. Projects that require prereleases can
still resolve to them through Pub; the bot rejects any prerelease reported in the
resolved graph to enforce its stable-only policy. Single application roots are
supported. Multi-package workspaces are rejected if resolution changes Pub
manifests or lockfiles outside the root.

## Pull request behavior

Each target branch has one update branch:
`flutter-bot/dependencies-<base-branch>`. The bot creates or refreshes a single PR
with all resolved updates, using `chore(deps)` commit and PR titles.

Unchanged file trees do not create duplicate commits. Declining a PR suppresses
the same resolved dependency-version set until versions change. Runs with no
file changes leave existing PRs open. Default reviewers are assigned when a PR
is created.

Treat update branches as bot-owned: the bot replaces their contents when
refreshing updates. Before publishing, it checks the remote base branch again.
If a commit arrives while dependencies are being resolved, it stops without
publishing and you should rerun the pipeline. It never pushes to the base branch.
A commit arriving after the final check can still leave the PR behind the base
branch, like any other PR. Force-with-lease protects the bot branch against
concurrent pushes. Running the updater
on its own `flutter-bot/*` branches is rejected.

Only `pubspec.yaml` and `pubspec.lock` are staged for the dependency commit.
Generated files are left in the disposable checkout. Existing staged or unstaged
changes at startup are rejected to avoid mixing them with dependency updates.
The pipeline disposes of the checkout when the step ends.

## Caching and image versions

Cache `/opt/pub-cache` to reuse downloaded packages, as shown in the
[pipeline example](examples/bitbucket-pipelines.yml). No Docker service is needed.
Flutter is preinstalled using the
[Stone-Soup-Tech Flutter image](https://github.com/Stone-Soup-Tech/docker-images-flutter).
Image pulls and dependency resolution still contribute to pipeline runtime.

Use `:latest` to receive newly published bot releases, including refreshed Flutter
environments. The bot's publishing workflow checks for base-image changes daily.
A published bot contains the Flutter version used when it was built; it does not
fetch a newer Flutter SDK when your pipeline starts. New upstream Flutter images
become available to consumers after a refreshed bot image is published.

For reproducible runs, use a published version tag or image digest instead of
`:latest`. Update that pin to adopt a newer release, or select a previously
validated version to roll back. Ensure the bot's Flutter/Dart versions match the
SDK constraints and build environment of your application. You can inspect them
by adding `flutter --version` to the pipeline script.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Authentication or permission errors | Use the account email for `BOT_USERNAME`; check token permissions, repository access and permission to push bot branches. |
| Resolution fails | Check the image's Flutter/Dart versions, application SDK constraints, package conflicts and private dependency authentication. |
| A dependency stays below its latest release | Check the PR's compatibility report and Flutter SDK pins. A newer version may need a newer SDK or changes to other dependencies. |
| Stable-only policy error | Check whether existing constraints or Git/path dependencies require a prerelease. |
| No PR appears | Check dry-run settings, whether files changed, and whether the same dependency-version set was previously declined. |
| Missing or dirty checkout | Keep pipeline cloning enabled and run the bot before modifying files. Locally, use a clean repository root. |
| Base or update branch changed | Rerun the pipeline; avoid editing bot-owned update branches. |
| Pipeline is slow | Enable the Pub cache and inspect image-pull and command timings in the step logs. |

References: [Pub upgrade](https://dart.dev/tools/pub/cmd/pub-upgrade),
[Pub outdated](https://dart.dev/tools/pub/cmd/pub-outdated).
