# bb2gh: Bitbucket Cloud to GitHub migrator

`bb2gh.py` moves a Bitbucket Cloud repository to GitHub, including its pull requests, comments, inline code comments, and approvals.

GitHub Enterprise Importer (GEI) can't read from Bitbucket Cloud. It only supports Bitbucket Server and Data Center. GEI can import a *migration archive*, though: the `.tar.gz` file that GitHub Enterprise Server produces. `bb2gh.py` builds that archive from Bitbucket Cloud data, then passes it to `gh gei migrate-repo`.

Each Bitbucket user becomes a *mannequin* in the target organization. A mannequin is a placeholder account that holds that user's pull requests and comments until you map it to a real GitHub account. See [Reclaim mannequins](#reclaim-mannequins).

## How it works

The migration has three steps. You can run them one at a time or all at once with `migrate`.

| Step | What it does |
|------|--------------|
| `export` | Mirror-clones the git repository and downloads every pull request (open, merged, declined, and superseded) with its comments, activity, and commit list. The data is cached under `work/<workspace>/<repo>/`. |
| `build` | Converts the cache into `migration-archive.tar.gz`, writes `build-report.txt`, and writes `bitbucket-users.csv`. Makes no network calls. |
| `import` | Runs `gh gei migrate-repo` with the archive. GEI uploads it to GitHub-owned storage and imports it. |

## Requirements

- Python 3.11 or later. The script uses only the standard library.
- `git`.
- The GitHub CLI (`gh`) with the `github/gh-gei` extension.
- A target organization on GitHub Enterprise Cloud (github.com or GHE.com). GEI does not import into GitHub Enterprise Server.

## Set up credentials

### Bitbucket

Use one of these:

- **Atlassian API token (personal).** Create one at <https://id.atlassian.com/manage-profile/security/api-tokens> with the scopes `read:repository:bitbucket` and `read:pullrequest:bitbucket`. Then set both variables:

  ```sh
  export BITBUCKET_EMAIL="you@example.com"
  export BITBUCKET_API_TOKEN="..."
  ```

- **Repository or workspace access token.** Give it the **Repositories: Read** and **Pull requests: Read** permissions.

  ```sh
  export BITBUCKET_ACCESS_TOKEN="..."
  ```

Bitbucket app passwords no longer work.

### GitHub

Create a personal access token (classic) for an owner of the target organization, or for someone with the migrator role there. It needs the `repo`, `admin:org`, and `workflow` scopes. If the organization uses SAML single sign-on, authorize the token for it.

```sh
export GH_PAT="ghp_..."
```

## Migrate a repository

1. Do a trial run against a test organization first. Then review `build-report.txt` and spot-check a few pull requests on GitHub.

2. Run the whole migration:

   ```sh
   python3 bb2gh.py migrate \
     --workspace my-workspace \
     --repo my-repo \
     --github-org my-github-org
   ```

   Useful options:

   - `--github-repo NAME` sets the target repository name. The default is the Bitbucket slug.
   - `--visibility private|internal|public` sets the target visibility. The default is `private`.
   - `--storage azure` or `--storage aws --aws-bucket-name BUCKET` stages the archive in your own cloud storage instead of GitHub-owned storage. `gh gei` reads the cloud credentials from its usual environment variables.
   - `--target-api-url https://api.SUBDOMAIN.ghe.com` targets GHE.com.
   - `--queue-only` starts the import and returns without waiting for it.

3. Or run the steps separately, which lets you inspect the archive before importing:

   ```sh
   python3 bb2gh.py export --workspace my-workspace --repo my-repo
   python3 bb2gh.py build  --workspace my-workspace --repo my-repo
   cat work/my-workspace/my-repo/build-report.txt
   python3 bb2gh.py import --workspace my-workspace --repo my-repo --github-org my-github-org
   ```

Running `export` again is cheap. It updates the mirror and downloads only open pull requests and pull requests that aren't cached yet. Add `--refresh` to download everything again.

To migrate many repositories, loop over them in a shell script. Each repository gets its own folder under `work/`.

## Use a clone you already have

If the repository is already cloned on your machine, pass `--local-repo` so `export` doesn't download the whole history again. Give it the clone's path, or leave the path out to use the current directory:

```sh
cd ~/code/my-repo
python3 ~/Git/bb2gh/bb2gh.py migrate --local-repo --github-org my-github-org
```

```sh
python3 bb2gh.py migrate --local-repo ~/code/my-repo --github-org my-github-org
```

With `--local-repo`, you can leave out `--workspace` and `--repo`. The tool reads them from the clone's Bitbucket remote, preferring `origin`. If the clone has several Bitbucket remotes, choose one with `--remote NAME`. Pass `--local-repo` to every step you run separately (`export`, `build`, `import`), or pass `--workspace` and `--repo` instead.

What `export` does with the clone:

1. Copies its git data into `work/<workspace>/<repo>/mirror.git`. Your clone isn't changed.
2. Fetches from Bitbucket, which downloads only the commits your clone doesn't have yet. After this, the branches and tags match Bitbucket exactly, so local branches and unpushed commits are never migrated.
3. Keeps your clone's local commits available for one purpose: recovering the commits of pull requests whose branches were deleted on Bitbucket. If you still have such a branch locally, its pull request keeps its diff.

To skip the Bitbucket fetch in step 2, add `--no-git-fetch`. The branches and tags then come from your clone's remote-tracking branches (`origin/*`) as they were at your last `git fetch`. Run `git fetch --prune --tags` in the clone first so they're current. The pull request data still comes from the Bitbucket API.

The clone must not be shallow. If it is, run `git fetch --unshallow` in it first.

## Reclaim mannequins

After the import, each Bitbucket user appears as a mannequin. Its login comes from the user's Bitbucket nickname, and its name from their Bitbucket display name. `build` lists every mannequin it creates in `work/<workspace>/<repo>/bitbucket-users.csv`, with the matching Bitbucket nickname and account ID, to help you match people to GitHub accounts.

Reclaiming sends each person an invitation. Their mannequin's content moves to their GitHub account only after they accept.

The `gh gei` and `gh bbs2gh` extensions have the same mannequin commands. The only difference is the name of the organization flag: `gh gei` uses `--github-target-org`, and `gh bbs2gh` uses `--github-org`. The examples below use `gh gei`.

### Reclaim in bulk

1. Generate a CSV of the mannequins in the organization:

   ```sh
   gh gei generate-mannequin-csv --github-target-org my-github-org --output mannequins.csv
   ```

   With `gh bbs2gh`:

   ```sh
   gh bbs2gh generate-mannequin-csv --github-org my-github-org --output mannequins.csv
   ```

   The file has three columns: `mannequin-user`, `mannequin-id`, and `target-user`.

2. Fill in `target-user` with each person's GitHub login. Delete any rows you don't want to reclaim.

3. Reclaim them:

   ```sh
   gh gei reclaim-mannequin --github-target-org my-github-org --csv mannequins.csv
   ```

   With `gh bbs2gh`:

   ```sh
   gh bbs2gh reclaim-mannequin --github-org my-github-org --csv mannequins.csv
   ```

### Reclaim one user

```sh
gh gei reclaim-mannequin --github-target-org my-github-org \
  --mannequin-user alice-bitbucket --target-user alice-gh
```

If two mannequins share a login, add `--mannequin-id` with the ID from the CSV.

### Options to know

- `--force` remaps a mannequin that was already reclaimed.
- `--skip-invitation` reclaims immediately with no invitation. It works only in Enterprise Managed Users organizations, and **it can't be undone**.
- `generate-mannequin-csv --include-reclaimed` also lists mannequins that are already reclaimed.

Mannequins are per organization. If you migrate into several organizations, reclaim in each one.

You can also reclaim mannequins in the web UI: open the organization's **Settings**, then **Import/Export**.

## What is migrated

| Bitbucket | GitHub |
|-----------|--------|
| Branches and tags | Branches and tags |
| Pull requests in every state | Pull requests. Merged ones show as merged; declined and superseded ones show as closed. Superseded ones get a note in the description. |
| Pull request numbers | The archive uses the Bitbucket numbers. GitHub normally keeps them in an empty repository, but this isn't guaranteed. |
| General comments | Conversation comments |
| Replies to general comments | Conversation comments that start with a quote of the parent, because GitHub conversation comments aren't threaded |
| Inline comments and their replies | Review comments in threads on the diff |
| Resolved inline comments | Resolved review threads |
| Approvals | Approving reviews |
| Requested changes | "Changes requested" reviews |
| `@{user}` mentions | The person's display name in bold, without `@`, so the import doesn't link or notify unrelated GitHub users |

### What is not migrated

- Issues, wiki, Pipelines, deployments, downloads, branch restrictions, webhooks, and permissions.
- Pull request tasks, reactions, and reviewer requests that had no response.
- Git LFS objects. GEI doesn't import them. Push them separately after the migration with `git lfs fetch --all` from Bitbucket and `git lfs push --all` to GitHub.
- Deleted comments.

### When a comment can't be placed on the diff

An inline comment becomes a conversation comment that starts with `Comment on <file> line <N>:` in these cases:

- The comment is on a whole file rather than a line.
- The line is not part of the pull request's diff anymore. This happens when later commits changed it.
- The pull request's commits aren't available.

### Pull requests whose commits are gone

Bitbucket doesn't keep pull request refs, so the commits of a declined pull request disappear when its branch is deleted. `export` tries to fetch them by commit ID, and from the source fork if there is one. `build` also recovers the head of a merged pull request from its merge commit.

If the commits still can't be found, the pull request is imported with an empty diff and a note in its description. Its inline comments become conversation comments. `build-report.txt` lists each affected pull request.

## Limits

- GEI rejects archives over about 30 GiB when you use GitHub-owned storage. It also rejects any single git object over 400 MiB.
- GitHub-owned storage isn't available for GHE.com organizations with data residency. Use `--storage azure` or `--storage aws` for those.
- The archive format is GitHub's internal migration format. GitHub doesn't document it publicly. This tool follows real GitHub exports and two working community tools, and it may need adjustment if GEI changes. If an import fails, run `gh gei download-logs --github-target-org my-github-org --target-repo my-repo` and check the log.

## Alternative

[katiem0/gh-bbc-exporter](https://github.com/katiem0/gh-bbc-exporter) is a maintained `gh` extension that solves the same problem. The main difference is how inline comments are handled. `bb2gh.py` computes real diff hunks and diff positions, so inline comments land on the right line with their replies threaded. If you hit a problem with one tool, try the other.
