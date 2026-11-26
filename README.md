# Flutter dependency bot for Bitbucket and Azure DevOps

[![Publish Flutter bot image](https://github.com/Stone-Soup-Tech/docker-images-flutter-bot/actions/workflows/publish.yml/badge.svg)](https://github.com/Stone-Soup-Tech/docker-images-flutter-bot/actions/workflows/publish.yml) [![GHCR version](https://ghcr-badge.egpl.dev/Stone-Soup-Tech/flutter-bot/latest_tag?trim=major&label=latest)](https://ghcr.io/stone-soup-tech/flutter-bot)

Update `pubspec.yaml` and `pubspec.lock` to the newest compatible stable dependency
versions and maintain one grouped pull request in Bitbucket Cloud or Azure Repos Git. Flutter, Python and Git
are installed in the image.

## Provider selection

One image supports both platforms. Select the provider explicitly:

```sh
flutter-bot --provider bitbucket
flutter-bot --provider azure
```

Without `--provider`, the bot detects Azure Pipelines from `TF_BUILD=true` or
Bitbucket from `BITBUCKET_REPO_FULL_NAME`. If neither or both are detected, it
raises an error requiring an explicit `--provider`.
An explicit provider takes precedence over detection. Azure support currently
targets **Azure DevOps Services** at `https://dev.azure.com/<organization>/`;
Azure DevOps Server and legacy collection URLs are not supported.

Both providers accept repeatable `--reviewer IDENTIFIER` arguments through the
same interface. Identifier formats depend on the provider:

| Provider | Reviewer identifiers |
| --- | --- |
| Azure DevOps | Email address or Azure DevOps identity GUID. |
| Bitbucket Cloud | Bitbucket UUID or Atlassian account ID. |

## Bitbucket Pipelines

### Bitbucket Pipelines setup

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

### Bitbucket credentials and setup

Configure these as **secured Bitbucket repository or workspace variables**:

| Variable | Purpose |
| --- | --- |
| `BOT_EMAIL` | Atlassian account email for Bitbucket REST authentication. |
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
`BITBUCKET_BRANCH` is absent. By default, it uses the repository's default reviewers,
excluding the authenticated account. Use `--reviewer` to select reviewers explicitly.

### Explicit Bitbucket reviewers

Use the same repeatable `--reviewer` option with a **Bitbucket UUID** or
**Atlassian account ID**:

```sh
flutter-bot --provider bitbucket --reviewer '{11111111-2222-3333-4444-555555555555}' --reviewer '557058:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
```

UUIDs are accepted with or without braces. The bot resolves identifiers through
Bitbucket's user API before dependency updates, normalizes them to UUIDs, and
ignores duplicates. The API token needs user-read access, and selected reviewers
must have repository access. The publishing account cannot review its own PR.

Bitbucket Cloud does not provide reliable public lookup of other users by email.
Email addresses, nicknames, and display names are therefore rejected; use the
`uuid` or `account_id` from an existing PR's reviewers or the user API instead.
See Atlassian's [user identity changes](https://developer.atlassian.com/cloud/bitbucket/bitbucket-api-changes-gdpr/)
and [user API](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-users/).

On creation, explicit reviewers replace the bot's selection of repository default
reviewers. Without this option, existing default-reviewer behavior is retained.
When refreshing a PR, missing explicit reviewers are added while all existing
reviewers are retained. The bot does not send approval or vote changes. Dry-run
resolves identities but does not assign reviewers or modify PRs.

### Bitbucket scheduling and validation

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

## Azure Pipelines

### Azure Pipelines setup

Use the same `ghcr.io/stone-soup-tech/flutter-bot:latest` image in an Ubuntu
container job. See the complete [Azure pipeline example](examples/azure-pipelines.yml)
for checkout, Pub caching, manual dry-run selection, and an optional schedule.
The image must contain this provider implementation before using that example;
changes in this repository do not update an already published image.

```yaml
pool:
  vmImage: ubuntu-latest
container: ghcr.io/stone-soup-tech/flutter-bot:latest
steps:
  - checkout: self
    clean: true
    persistCredentials: false
  - bash: flutter-bot --provider azure --dry-run
    env:
      SYSTEM_ACCESSTOKEN: $(System.AccessToken)
```

Map `System.AccessToken` explicitly as shown. The bot uses the pipeline job's
Build Service identity for REST and Git access; no bot username or stored PAT is
needed. Grant that identity **Read**, **Contribute**, **Create branch**, and
**Contribute to pull requests** on the repository. It also needs **Force push**
on its `flutter-bot/*` branches to refresh their history. Scope that permission
to bot branches; policy bypass is not required. Private Azure Git dependencies
must be accessible to the same identity, including any necessary cross-project
job authorization. Other Git hosts require their own credentials.

The bot reads `SYSTEM_COLLECTIONURI`, `SYSTEM_TEAMPROJECTID`,
`BUILD_REPOSITORY_ID`, `BUILD_SOURCEBRANCH`, `BUILD_SOURCESDIRECTORY`, and
`BUILD_SOURCEVERSION` from Azure's pipeline environment. Run it on an application
branch; PR merge refs, tags, and the bot's own branches are rejected. It checks
the clean checkout and remote base commit before publishing. Outside Pipelines,
provide those variables and the token; omit `BUILD_SOURCEBRANCH` to use the
repository default branch, and omit the checkout path to use the current directory.

Configure automatic reviewers through Azure branch policies, or use explicit
reviewers as described below. The Azure provider does not copy Bitbucket reviewer
settings or enable auto-complete. An abandoned Azure PR suppresses the same
dependency-version set, like a declined Bitbucket PR; a reviewer's reject vote
alone does not suppress updates. Azure source-branch deletion on merge follows
your completion settings.

Start with a manual dry-run, then disable dry-run to validate publication in a
test repository. Enable the example's schedule only after validation. Avoid
overlapping updater runs on the same branch; Git leases protect against concurrent
pushes, but overlapping runs can still fail and need rerunning. Add your normal
Azure Repos branch-policy build validation for dependency PRs.

Microsoft references: [pipeline job token](https://learn.microsoft.com/en-us/azure/devops/pipelines/build/variables#systemaccesstoken),
[repository permissions](https://learn.microsoft.com/en-us/azure/devops/repos/git/set-git-repository-permissions),
and [container job requirements](https://learn.microsoft.com/en-us/azure/devops/pipelines/process/container-phases).

### Explicit Azure reviewers

Use repeatable `--reviewer` arguments to add people directly to Flutter Bot PRs
without changing organization or branch policies:

```sh
flutter-bot --provider azure --reviewer you@company.com --reviewer colleague@company.com
```

In your existing Azure pipeline step:

```yaml
- bash: flutter-bot --provider azure --reviewer you@company.com
  displayName: Run Flutter Bot
  env:
    SYSTEM_ACCESSTOKEN: $(System.AccessToken)
    FLUTTER_BOT_DRY_RUN: ${{ lower(parameters.dryRun) }}
```

Each argument accepts an email address or an **Azure DevOps identity GUID**.
Display names are unsupported because they can be ambiguous. Emails are resolved
case-insensitively against active identities with an exact mail or account match;
unknown or ambiguous emails fail before dependency updates or publishing. Duplicate
reviewers are ignored. Reviewers must already have access to the project.

Email lookup uses the organization's Azure Identities API and requires identity
read access for the job token. If your organization restricts that API, supply the
Azure DevOps identity GUID instead to skip lookup:

```sh
flutter-bot --provider azure --reviewer 11111111-2222-3333-4444-555555555555
```

Use the Azure DevOps identity `id` (for example, from an existing PR's reviewer
list), not the Microsoft Entra object ID. The pipeline identity still needs
permission to add PR reviewers. This option requests reviewers; it does not make
their approval mandatory or change branch policies.

Configured reviewers are included when creating a PR and added if missing when
refreshing an existing one. Existing reviewers, required-reviewer settings, and
votes are preserved. With no `--reviewer` arguments, only Azure's normal reviewer
policies apply. Dry-run resolves emails but does not create PRs or assign reviewers.
Bitbucket supports the same option with UUIDs or Atlassian account IDs, as
described in the Bitbucket section above.

References: [Azure identity lookup](https://learn.microsoft.com/en-us/rest/api/azure/devops/ims/identities/read-identities?view=azure-devops-rest-7.1)
and [adding PR reviewers](https://learn.microsoft.com/en-us/rest/api/azure/devops/git/pull-request-reviewers/create-pull-request-reviewers?view=azure-devops-rest-7.1).

### Azure scheduling and validation

1. Create an Azure pipeline pointing to the YAML from the
   [Azure example](examples/azure-pipelines.yml). Run it manually on the target
   application branch with `dryRun: true`, then with `dryRun: false` to verify PR
   publication.
2. Uncomment the example's top-level `schedules` block, or add this to the main
   pipeline YAML:

   ```yaml
   schedules:
     - cron: '0 6 * * 1'
       displayName: Weekly Flutter dependency updates
       branches:
         include: [main]
       always: true
   ```

   This runs every Monday at **06:00 UTC**. Replace `main` with the application
   branch you want updated. Cron times use UTC and do not adjust for daylight
   saving time. Keep `always: true` so the bot checks for newly published packages
   even when your repository has no new commits.
3. Change the example's `dryRun` parameter default to `false` when scheduled runs
   should publish. Schedules use parameter defaults; selecting `false` for a
   manual run does not change later scheduled runs. If using the minimal setup
   above instead, remove its hardcoded `--dry-run` to enable publication.
4. Commit the schedule to each included target branch. Each branch's YAML must
   contain a schedule that includes that branch. Define schedules in the main
   pipeline YAML, rather than a template.
5. Remove any schedules configured in the pipeline settings UI, because UI
   schedules override YAML schedules. Push the YAML change after removing them,
   then open the pipeline's **Scheduled runs** view to check upcoming runs.

The example's `trigger: none` disables commit-triggered runs; the YAML schedule
still runs. Keep this updater pipeline separate from your application PR build
validation. Ensure one updater run finishes before the next starts;
`always: true` can start a scheduled run while another run is still in progress.

See Microsoft's [scheduled trigger documentation](https://learn.microsoft.com/en-us/azure/devops/pipelines/process/scheduled-triggers)
for cron syntax, branch evaluation, and schedule troubleshooting.

## Dry-run

```sh
flutter-bot --dry-run
```

Dry-run resolves dependencies in the pipeline checkout, prints the proposed update summary
and checks existing PRs, without committing, pushing branches or creating or
modifying PRs. It leaves the proposed changes in the local checkout. The pipeline
discards that checkout when the job ends; when running locally, use a disposable
checkout if you want to preserve your working files. Credentials and network
access are still required.

Alternatively, set `FLUTTER_BOT_DRY_RUN=true`. It accepts `true` or `false` and
defaults to `false`; `--dry-run` always enables dry-run. Use `--provider` to choose the platform explicitly; branch and grouping are automatic.

## What gets updated

The bot updates a single root package or a native Dart Pub workspace, including
direct and development dependencies and the resolved transitive dependency graph.
For workspaces, it updates every member's `pubspec.yaml` and the shared root
`pubspec.lock` in one grouped PR.
For direct dependencies, including dev dependencies, it selects the newest
mutually compatible **stable** versions across major,
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
resolved versions. The PR description lists changed direct and dev dependencies with their previous
and updated versions, followed by a table of versions held back by compatibility
constraints. Full manifest and lockfile diffs are available in the PR
file changes.

Flutter SDK dependencies and their pins remain controlled by Flutter. The bot
does not force incompatible versions, add dependency overrides, edit SDK
constraints or upgrade Flutter during a run. Git/path dependencies follow their
existing declarations; the bot does not discover new Git tags or rewrite paths.

Resolution failures prevent publication. The bot rejects prereleases in direct
dependencies, including dev dependencies. Transitive dependencies may resolve to
prereleases required by their parent packages; these do not block publication.
Single root packages and native Pub workspaces are supported. Pub file changes
outside the discovered workspace prevent publication.

## Multi-package workspaces and Melos

Run `flutter-bot` from the repository root, which must also be the Pub workspace
root. No extra bot parameter or Melos installation is needed. For example:

```yaml
# pubspec.yaml at the repository root
name: my_workspace
publish_to: none
environment:
  sdk: ^3.6.0
workspace:
  - apps/mobile
  - packages/shared
```

Each member must use native workspace resolution:

```yaml
# packages/shared/pubspec.yaml
name: shared
version: 1.0.0
resolution: workspace
environment:
  sdk: ^3.6.0
```

The bot discovers packages with `dart pub workspace list --json`. Pub interprets
your workspace declarations, including nested workspaces and glob patterns
supported by the image's Dart SDK. The two upgrade commands run once at the root
and update all member constraints together. The PR summary identifies each changed
dependency's manifest and whether it is a dependency or dev dependency. Workspace
packages are excluded from external package update rows.

Commit the shared root lockfile. Remove any leftover member `pubspec.lock` files
before running the bot; it rejects them rather than letting Pub delete them during
resolution. Member paths and Pub files must stay inside the checkout and must not
be symlinks. Only discovered manifests and the shared lockfile are staged;
Melos configuration, scripts, and generated files are left alone.

This covers **Melos 7+** projects using native Pub workspaces. The bot does not run
Melos bootstrap hooks or scripts. Legacy Melos repositories using independent
package resolution, and collections of independent packages with separate
lockfiles, are not supported as multi-package updates. Root-package behavior
continues to work without a `workspace` declaration.

The image must include **Dart 3.13+** for `dart pub workspace list --json`.
Project SDK constraints may still allow older versions (such as `^3.6.0` above);
resolution runs with the SDK installed in the image. Newer workspace syntax and
your Melos version can require a newer SDK. See [Pub workspaces](https://dart.dev/tools/pub/workspaces),
[workspace upgrades](https://dart.dev/tools/pub/cmd/pub-upgrade#in-a-workspace), and
the [Melos migration guide](https://invertase.docs.page/melos/guides/migrations).

## Pull request behavior

Each target branch has one update branch:
`flutter-bot/dependencies-<base-branch>`. The bot creates or refreshes a single PR
with all resolved updates, using `chore(deps)` commit and PR titles.

Unchanged file trees do not create duplicate commits. Declining a PR suppresses
the same resolved dependency-version set until versions change. Runs with no
file changes leave existing PRs open. Bitbucket uses explicit reviewers when
configured, otherwise repository defaults on creation. Azure reviewers follow
branch policies and any explicit `--reviewer` selections.
Azure summaries that exceed its API description limit are shortened, preserving
the update marker; full details remain in pipeline logs and the file diff.

Treat update branches as bot-owned: the bot replaces their contents when
refreshing updates. Before publishing, it checks the remote base branch again.
If a commit arrives while dependencies are being resolved, it stops without
publishing and you should rerun the pipeline. It never pushes to the base branch.
A commit arriving after the final check can still leave the PR behind the base
branch, like any other PR. Force-with-lease protects the bot branch against
concurrent pushes. Running the updater
on its own `flutter-bot/*` branches is rejected.

Only the discovered `pubspec.yaml` files and the root `pubspec.lock` are staged
for the dependency commit.
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
| Authentication or permission errors | Use the account email for `BOT_EMAIL`; check token permissions, repository access and permission to push bot branches. |
| Resolution fails | Check the image's Flutter/Dart versions, application SDK constraints, package conflicts and private dependency authentication. |
| A dependency stays below its latest release | Check dependency constraints and Flutter SDK pins. A newer version may need a newer SDK or changes to other dependencies. |
| Stable-only policy error | Check whether a direct dependency or dev dependency requires a prerelease. |
| No PR appears | Check dry-run settings, whether files changed, and whether the same dependency-version set was previously declined. |
| Missing or dirty checkout | Keep pipeline cloning enabled and run the bot before modifying files. Locally, use a clean repository root. |
| Base or update branch changed | Rerun the pipeline; avoid editing bot-owned update branches. |
| Pipeline is slow | Enable the Pub cache and inspect image-pull and command timings in the step logs. |

References: [Pub upgrade](https://dart.dev/tools/pub/cmd/pub-upgrade),
[Pub outdated](https://dart.dev/tools/pub/cmd/pub-outdated).
