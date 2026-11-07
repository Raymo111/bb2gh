#!/usr/bin/env python3
"""Migrate a Bitbucket Cloud repository, including pull requests, to GitHub.

Bitbucket Cloud is not a source that GitHub Enterprise Importer (GEI) supports
directly. This tool fills that gap in three steps:

1. export   Mirror the git repository and download pull request data
            (descriptions, comments, inline comments, approvals) from the
            Bitbucket Cloud REST API into a local cache.
2. build    Convert the cache into a GitHub migration archive: the same
            .tar.gz format that GitHub Enterprise Server produces for GEI.
3. import   Hand the archive to `gh gei migrate-repo`, which uploads it to
            GitHub-owned storage and runs the import.

`migrate` runs all three steps in order.

Bitbucket users become GEI "mannequins" (placeholder accounts) in the target
organization. Reclaim them afterwards with `gh gei reclaim-mannequin`; see
README.md.

Only the Python standard library is used. Python 3.11 or later is required.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

BITBUCKET_API = "https://api.bitbucket.org/2.0"
BITBUCKET_WEB = "https://bitbucket.org"
PR_STATES = ("OPEN", "MERGED", "DECLINED", "SUPERSEDED")
RECORDS_PER_FILE = 100

# Review states used in GitHub migration archives.
REVIEW_COMMENTED = 1
REVIEW_CHANGES_REQUESTED = 30
REVIEW_APPROVED = 40

# Synthetic IDs for approval reviews must not collide with comment IDs.
APPROVAL_REVIEW_ID_BASE = 9_000_000_000


def log(msg: str) -> None:
    print(f"[bb2gh] {msg}", file=sys.stderr, flush=True)


def die(msg: str) -> NoReturn:
    log(f"error: {msg}")
    sys.exit(1)


# --------------------------------------------------------------------------
# Bitbucket Cloud access
# --------------------------------------------------------------------------


@dataclass
class BitbucketAuth:
    """Credentials for the Bitbucket Cloud API and for git over HTTPS."""

    api_header: str
    git_header: str

    @classmethod
    def from_env(cls) -> "BitbucketAuth":
        access_token = os.environ.get("BITBUCKET_ACCESS_TOKEN")
        email = os.environ.get("BITBUCKET_EMAIL")
        api_token = os.environ.get("BITBUCKET_API_TOKEN")
        if access_token:
            # Repository, project, or workspace access token.
            return cls(
                api_header=f"Bearer {access_token}",
                git_header=_basic("x-token-auth", access_token),
            )
        if email and api_token:
            # Personal Atlassian API token with Bitbucket scopes.
            return cls(
                api_header=_basic(email, api_token),
                git_header=_basic("x-bitbucket-api-token-auth", api_token),
            )
        die(
            "set BITBUCKET_EMAIL and BITBUCKET_API_TOKEN, "
            "or BITBUCKET_ACCESS_TOKEN"
        )


def _basic(user: str, secret: str) -> str:
    raw = f"{user}:{secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


class BitbucketClient:
    def __init__(self, auth: BitbucketAuth):
        self.auth = auth

    def get(self, url: str, allow_missing: bool = False) -> dict | None:
        if not url.startswith("http"):
            url = BITBUCKET_API + url
        for attempt in range(8):
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": self.auth.api_header,
                    "Accept": "application/json",
                    "User-Agent": "bb2gh-migrator",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as err:
                if err.code == 429 or err.code >= 500:
                    wait = int(err.headers.get("Retry-After") or 2 ** attempt)
                    log(f"HTTP {err.code} from Bitbucket, retrying in {wait}s")
                    time.sleep(min(wait, 120))
                    continue
                if allow_missing and err.code in (404, 410):
                    return None
                body = err.read().decode(errors="replace")[:500]
                die(f"GET {url} failed with HTTP {err.code}: {body}")
            except urllib.error.URLError as err:
                wait = 2 ** attempt
                log(f"network error ({err.reason}), retrying in {wait}s")
                time.sleep(wait)
        die(f"GET {url} kept failing; giving up")

    def paginate(self, url: str, allow_missing: bool = False) -> list:
        items: list = []
        next_url: str | None = url
        while next_url:
            page = self.get(next_url, allow_missing)
            if page is None:
                log(f"    {next_url} returned 404; continuing without it")
                return items
            items.extend(page.get("values", []))
            next_url = page.get("next")
        return items


# --------------------------------------------------------------------------
# Git helpers
# --------------------------------------------------------------------------


def git(*args: str, cwd: Path | None = None, check: bool = True,
        header: str | None = None) -> subprocess.CompletedProcess:
    cmd = ["git"]
    if header:
        # Pass credentials per command so they are never written to disk.
        cmd += ["-c", f"http.extraHeader=Authorization: {header}"]
    cmd += list(args)
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if check and result.returncode != 0:
        die(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result


def resolve_commit(repo: Path, rev: str | None) -> str | None:
    """Return the full SHA for `rev`, or None if the commit is not present."""
    if not rev:
        return None
    result = git("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}",
                 cwd=repo, check=False)
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and len(sha) == 40 else None


# --------------------------------------------------------------------------
# Step 1: export from Bitbucket Cloud
# --------------------------------------------------------------------------


@dataclass
class Paths:
    root: Path

    @property
    def mirror(self) -> Path:
        return self.root / "mirror.git"

    @property
    def cache(self) -> Path:
        return self.root / "bitbucket"

    @property
    def staging(self) -> Path:
        return self.root / "archive"

    @property
    def archive(self) -> Path:
        return self.root / "migration-archive.tar.gz"

    @property
    def report(self) -> Path:
        return self.root / "build-report.txt"

    @property
    def users_csv(self) -> Path:
        return self.root / "bitbucket-users.csv"


def work_paths(args: argparse.Namespace) -> Paths:
    return Paths(Path(args.work_dir).resolve() / args.workspace / args.repo)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(path)


def read_json(path: Path):
    return json.loads(path.read_text())


def cmd_export(args: argparse.Namespace) -> None:
    auth = BitbucketAuth.from_env()
    client = BitbucketClient(auth)
    paths = work_paths(args)
    ws, slug = args.workspace, args.repo
    repo_api = f"/repositories/{ws}/{slug}"
    clone_url = f"{BITBUCKET_WEB}/{ws}/{slug}.git"

    log(f"reading repository {ws}/{slug}")
    repo_meta = client.get(repo_api)
    write_json(paths.cache / "repository.json", repo_meta)

    sync_mirror(paths, auth, clone_url, args.local, args.no_git_fetch)
    main_branch = (repo_meta.get("mainbranch") or {}).get("name")
    if main_branch and resolve_commit(paths.mirror, f"refs/heads/{main_branch}"):
        git("symbolic-ref", "HEAD", f"refs/heads/{main_branch}", cwd=paths.mirror)

    states = "&".join(f"state={s}" for s in PR_STATES)
    summaries = client.paginate(f"{repo_api}/pullrequests?{states}&pagelen=50")
    log(f"found {len(summaries)} pull requests")

    pr_dir = paths.cache / "pullrequests"
    for i, summary in enumerate(sorted(summaries, key=lambda p: p["id"]), 1):
        pr_id = summary["id"]
        target = pr_dir / f"{pr_id}.json"
        # Open PRs can still change, so always refresh them.
        if target.exists() and not args.refresh and summary["state"] != "OPEN":
            continue
        log(f"  [{i}/{len(summaries)}] pull request #{pr_id}")
        pr_api = f"{repo_api}/pullrequests/{pr_id}"
        record = {
            "pullrequest": client.get(pr_api),
            "comments": client.paginate(f"{pr_api}/comments?pagelen=100"),
            "activity": client.paginate(f"{pr_api}/activity?pagelen=50"),
            "commits": client.paginate(f"{pr_api}/commits?pagelen=100",
                                       allow_missing=True),
        }
        write_json(target, record)

    if not args.no_git_fetch:
        fetch_missing_pr_commits(paths, auth)
    log(f"export complete: {paths.root}")


@dataclass
class LocalRepo:
    """An existing clone on disk, used as the git source."""

    git_dir: Path         # the shared .git directory (or the bare repo)
    remote: str | None    # the remote that points at Bitbucket, if any

    def branch_refspec(self) -> str:
        """Which refs in the clone represent the Bitbucket branches."""
        if self.remote:
            has_tracking = git("for-each-ref", "--count=1", "--format=x",
                               f"refs/remotes/{self.remote}/", cwd=self.git_dir)
            if has_tracking.stdout.strip():
                return f"+refs/remotes/{self.remote}/*:refs/heads/*"
        return "+refs/heads/*:refs/heads/*"  # a bare or mirror clone


BB_REMOTE_RE = re.compile(
    r"bitbucket\.org[:/]+(?P<ws>[^/\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?$")


def open_local_repo(path: str, remote: str | None) -> tuple:
    """Return (LocalRepo, workspace, slug) for a clone on disk.

    The workspace and slug come from the remote that points at
    bitbucket.org, or are None when no such remote exists.
    """
    where = Path(path).expanduser().resolve()
    res = git("rev-parse", "--path-format=absolute", "--git-common-dir",
              cwd=where, check=False)
    if res.returncode != 0:
        die(f"{where} is not a git repository")
    git_dir = Path(res.stdout.strip())
    shallow = git("rev-parse", "--is-shallow-repository", cwd=git_dir)
    if shallow.stdout.strip() == "true":
        die(f"{where} is a shallow clone; run `git fetch --unshallow` in it "
            "first, or leave out --local-repo")

    names = git("remote", cwd=git_dir).stdout.split()
    if remote and remote not in names:
        die(f"{where} has no remote named {remote}")
    order = [remote] if remote else sorted(names, key=lambda n: n != "origin")
    for name in order:
        url = git("remote", "get-url", name, cwd=git_dir).stdout.strip()
        m = BB_REMOTE_RE.search(url)
        if m:
            return (LocalRepo(git_dir, name), m.group("ws").lower(),
                    m.group("repo").lower())
    return LocalRepo(git_dir, remote), None, None


def sync_mirror(paths: Paths, auth: BitbucketAuth, clone_url: str,
                local: LocalRepo | None, no_git_fetch: bool) -> None:
    """Create or update mirror.git, the git data that goes into the archive.

    With a local clone, its objects seed the mirror so only what is missing
    has to come from Bitbucket. The Bitbucket fetch then makes the branches
    and tags match Bitbucket exactly, so unpushed local work is never
    migrated. With --no-git-fetch, the clone's view of Bitbucket (its
    remote-tracking branches) is used as-is.
    """
    mirror = paths.mirror
    if not mirror.exists() and not local:
        log("creating mirror clone (this can take a while)")
        paths.root.mkdir(parents=True, exist_ok=True)
        git("clone", "--mirror", clone_url, str(mirror),
            header=auth.git_header)
        return

    if not mirror.exists():
        paths.root.mkdir(parents=True, exist_ok=True)
        git("init", "--quiet", "--bare", str(mirror))
        git("remote", "add", "--mirror=fetch", "origin", clone_url, cwd=mirror)

    if local:
        log(f"copying git data from {local.git_dir}")
        res = git("fetch", "--quiet", "--no-tags", str(local.git_dir),
                  local.branch_refspec(), "+refs/tags/*:refs/tags/*",
                  cwd=mirror, check=False)
        if res.returncode != 0:
            if no_git_fetch:
                die(f"could not copy from {local.git_dir}:\n{res.stderr.strip()}")
            log("warning: copying from the local clone failed; "
                "fetching everything from Bitbucket instead")
        # A remote's HEAD pointer becomes a stray branch named HEAD.
        git("update-ref", "-d", "refs/heads/HEAD", cwd=mirror, check=False)

    if no_git_fetch:
        log("skipping the Bitbucket git fetch (--no-git-fetch)")
    else:
        log("fetching from Bitbucket")
        git("remote", "update", "--prune", cwd=mirror, header=auth.git_header)

    if local:
        # Keep every local commit available for recovering the heads of
        # pull requests whose branches were deleted on Bitbucket. These
        # refs are dropped when the archive is built.
        git("fetch", "--quiet", "--no-tags", str(local.git_dir),
            "+refs/heads/*:refs/bb2gh/local/heads/*",
            "+refs/remotes/*:refs/bb2gh/local/remotes/*",
            cwd=mirror, check=False)


def fetch_missing_pr_commits(paths: Paths, auth: BitbucketAuth) -> None:
    """Fetch PR head commits that the mirror clone did not include.

    Commits from deleted branches or from forks are not reachable from any
    branch in the mirror. Try fetching them by SHA from the main repository,
    then from the source fork. Bitbucket may refuse; the build step handles
    whatever is still missing.
    """
    for pr_file in sorted((paths.cache / "pullrequests").glob("*.json")):
        pr = read_json(pr_file)["pullrequest"]
        head = _pr_head_candidate(read_json(pr_file))
        if not head or resolve_commit(paths.mirror, head):
            continue
        ref = f"refs/bb2gh/pr/{pr['id']}"
        sources = ["origin"]
        fork = (pr.get("source", {}).get("repository") or {}).get("full_name")
        if fork:
            sources.append(f"{BITBUCKET_WEB}/{fork}.git")
        for source in sources:
            if len(head) == 40:
                res = git("fetch", "--quiet", source, f"{head}:{ref}",
                          cwd=paths.mirror, check=False, header=auth.git_header)
                if res.returncode == 0:
                    break
            branch = pr.get("source", {}).get("branch", {}).get("name")
            if source != "origin" and branch:
                res = git("fetch", "--quiet", source,
                          f"refs/heads/{branch}:{ref}", cwd=paths.mirror,
                          check=False, header=auth.git_header)
                if res.returncode == 0:
                    break


def closed_time(record: dict) -> str | None:
    """When the PR was merged or declined, from its activity log."""
    pr = record["pullrequest"]
    for item in record.get("activity", []):
        update = item.get("update") or {}
        if update.get("state") == pr["state"] and update.get("date"):
            return update["date"]  # activity is newest first
    return pr.get("closed_on") or pr.get("updated_on")


def _pr_head_candidate(record: dict) -> str | None:
    commits = record.get("commits") or []
    if commits and len(commits[0].get("hash", "")) == 40:
        return commits[0]["hash"]  # newest commit first
    return ((record["pullrequest"].get("source") or {}).get("commit")
            or {}).get("hash")


# --------------------------------------------------------------------------
# Step 2: build the GitHub migration archive
# --------------------------------------------------------------------------


def ts(value: str | None) -> str | None:
    """Convert a Bitbucket timestamp to the archive's UTC 'Z' format."""
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class DiffLocation:
    position: int
    diff_hunk: str
    line: int
    side: str


def locate_in_diff(repo: Path, base: str, head: str, path: str,
                   new_line: int | None, old_line: int | None
                   ) -> DiffLocation | None:
    """Find a Bitbucket inline-comment line in GitHub's diff coordinates.

    GitHub positions count lines below the first "@@" header of a file's
    diff (the first line after it is position 1). The diff_hunk runs from
    the enclosing hunk header to the commented line.
    """
    result = git("diff", "--no-color", "--no-ext-diff", "--no-renames",
                 "-U3", base, head, "--", path, cwd=repo, check=False)
    if result.returncode != 0 or "@@" not in result.stdout:
        return None
    lines = result.stdout.split("\n")
    start = next(i for i, l in enumerate(lines) if l.startswith("@@"))
    position = -1
    old_no = new_no = 0
    hunk_start = start
    hunk_re = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
    for idx in range(start, len(lines)):
        text = lines[idx]
        position += 1
        if text.startswith("@@"):
            m = hunk_re.match(text)
            if not m:
                return None
            old_no, new_no = int(m.group(1)), int(m.group(2))
            hunk_start = idx
            continue
        if text.startswith("\\"):
            continue  # "\ No newline at end of file"
        if text.startswith("+"):
            if new_line is not None and new_no == new_line:
                return _loc(lines, hunk_start, idx, position, new_no, "right")
            new_no += 1
        elif text.startswith("-"):
            if new_line is None and old_line is not None and old_no == old_line:
                return _loc(lines, hunk_start, idx, position, old_no, "left")
            old_no += 1
        elif text.startswith(" "):
            if new_line is not None and new_no == new_line:
                return _loc(lines, hunk_start, idx, position, new_no, "right")
            if new_line is None and old_line is not None and old_no == old_line:
                return _loc(lines, hunk_start, idx, position, old_no, "left")
            old_no += 1
            new_no += 1
    return None


def _loc(lines: list, hunk_start: int, idx: int, position: int, line: int,
         side: str) -> DiffLocation:
    return DiffLocation(
        position=position,
        diff_hunk="\n".join(lines[hunk_start:idx + 1]),
        line=line,
        side=side,
    )


class ArchiveBuilder:
    def __init__(self, paths: Paths, workspace: str, slug: str):
        self.paths = paths
        self.ws = workspace
        self.slug = slug
        self.mirror = paths.mirror
        self.repo_url = f"{BITBUCKET_WEB}/{workspace}/{slug}"
        self.org_url = f"{BITBUCKET_WEB}/{workspace}"
        self.users: dict[str, dict] = {}        # Bitbucket key -> user record
        self.logins: set[str] = set()
        self.records: dict[str, list] = {
            "pull_requests": [],
            "issue_comments": [],
            "issue_events": [],
            "pull_request_reviews": [],
            "pull_request_review_comments": [],
            "pull_request_review_threads": [],
        }
        self.pr_refs: dict[int, str] = {}
        self.warnings: list[str] = []
        self.event_id = 0

    # ---- users -----------------------------------------------------------

    def user_url(self, bb_user: dict | None) -> str:
        """Register a Bitbucket user and return their archive URL."""
        if not bb_user:
            bb_user = {"account_id": "former-user",
                       "nickname": "bitbucket-former-user",
                       "display_name": "Former Bitbucket user"}
        key = (bb_user.get("account_id") or bb_user.get("uuid")
               or bb_user.get("nickname") or bb_user.get("display_name")
               or "unknown")
        if key in self.users:
            return self.users[key]["url"]
        login = self._unique_login(
            bb_user.get("nickname") or bb_user.get("display_name") or key)
        url = f"{BITBUCKET_WEB}/{login}"
        self.users[key] = {
            "type": "user",
            "url": url,
            "login": login,
            "name": bb_user.get("display_name") or login,
            "bio": None,
            "company": None,
            "website": None,
            "location": None,
            "emails": [],
            "billing_plan": None,
            "created_at": ts(bb_user.get("created_on")) or "2008-01-01T00:00:00Z",
            # Not part of the archive format; used for bitbucket-users.csv.
            "_bitbucket": {
                "account_id": bb_user.get("account_id"),
                "uuid": bb_user.get("uuid"),
                "nickname": bb_user.get("nickname"),
            },
        }
        return url

    def _unique_login(self, raw: str) -> str:
        # GitHub logins allow letters, digits, and single inner hyphens.
        login = re.sub(r"[^A-Za-z0-9-]+", "-", raw).strip("-")
        login = re.sub(r"-{2,}", "-", login)[:39] or "bitbucket-user"
        candidate, n = login, 2
        while candidate.lower() in self.logins:
            suffix = f"-{n}"
            candidate = login[:39 - len(suffix)] + suffix
            n += 1
        self.logins.add(candidate.lower())
        return candidate

    def mention_names(self, records: list) -> dict:
        names = {}
        for rec in records:
            pr = rec["pullrequest"]
            people = [pr.get("author")] + [p.get("user") for p in
                                            pr.get("participants", [])]
            people += [c.get("user") for c in rec.get("comments", [])]
            for person in people:
                if not person:
                    continue
                name = person.get("display_name") or person.get("nickname")
                for ident in (person.get("account_id"), person.get("uuid")):
                    if ident and name:
                        names[ident] = name
        return names

    def clean_text(self, text: str | None) -> str:
        """Rewrite Bitbucket @{id} mentions as plain names.

        Keeping the @ sign could link or notify unrelated GitHub users who
        happen to have the same login.
        """
        if not text:
            return ""

        def replace(m: re.Match) -> str:
            ident = m.group(1)
            name = self._mentions.get(ident) or self._mentions.get(
                "{" + ident.strip("{}") + "}")
            return f"**{name}**" if name else "**(Bitbucket user)**"

        return re.sub(r"@\{([^}]+)\}", replace, text)

    # ---- pull requests ---------------------------------------------------

    def build(self) -> None:
        repo_meta = read_json(self.paths.cache / "repository.json")
        pr_files = sorted((self.paths.cache / "pullrequests").glob("*.json"),
                          key=lambda p: int(p.stem))
        prs = [read_json(p) for p in pr_files]
        self._mentions = self.mention_names(prs)
        for record in prs:
            self.add_pull_request(record)
        self.write_archive(repo_meta)

    def add_pull_request(self, record: dict) -> None:
        pr = record["pullrequest"]
        number = pr["id"]
        pr_url = f"{self.repo_url}/pull/{number}"
        state = pr["state"]

        base_sha = resolve_commit(self.mirror,
                                  pr["destination"]["commit"]["hash"])
        if not base_sha:
            base_sha = resolve_commit(
                self.mirror, f"refs/heads/{pr['destination']['branch']['name']}")
        if not base_sha:
            self.warn(f"PR #{number}: base commit not found; PR skipped")
            return

        head_sha, head_note = self.resolve_head(record, base_sha)
        if head_note:
            self.warn(f"PR #{number}: {head_note}")
        self.pr_refs[number] = head_sha

        merge_sha = resolve_commit(
            self.mirror, (pr.get("merge_commit") or {}).get("hash"))
        closed_at = None
        merged_at = None
        if state != "OPEN":
            closed_at = ts(closed_time(record))
        if state == "MERGED":
            merged_at = closed_at

        body = self.clean_text(pr.get("description"))
        if head_note:
            body += ("\n\n---\n_Migration note: the original commits of this "
                     "pull request were not available from Bitbucket, so "
                     "its diff is empty._")
        if state == "SUPERSEDED":
            body += "\n\n---\n_This pull request was superseded in Bitbucket._"

        author_url = self.user_url(pr.get("author"))
        self.records["pull_requests"].append({
            "type": "pull_request",
            "url": pr_url,
            "user": author_url,
            "repository": self.repo_url,
            "title": pr.get("title") or f"Pull request {number}",
            "body": body,
            "base": {
                "ref": pr["destination"]["branch"]["name"],
                "sha": base_sha,
                "user": self.org_url,
                "repo": self.repo_url,
            },
            "head": {
                "ref": pr["source"]["branch"]["name"],
                "sha": head_sha,
                "user": self.org_url,
                "repo": self.repo_url,
            },
            "assignee": None,
            "assignees": [],
            "milestone": None,
            "labels": [],
            "reactions": [],
            "review_requests": [],
            "close_issue_references": [],
            "work_in_progress": bool(pr.get("draft")),
            "merged_at": merged_at,
            "closed_at": closed_at,
            "created_at": ts(pr["created_on"]),
            "merge_commit_sha": merge_sha,
        })

        self.add_close_events(pr, pr_url, closed_at, merge_sha)
        self.add_approvals(record, pr_url, head_sha)
        self.add_comments(record, pr_url, base_sha, head_sha,
                          diff_ok=head_note is None)

    def resolve_head(self, record: dict, base_sha: str) -> tuple:
        pr = record["pullrequest"]
        number = pr["id"]
        for candidate in (_pr_head_candidate(record),
                          f"refs/bb2gh/pr/{number}",
                          pr["source"]["commit"]["hash"]):
            sha = resolve_commit(self.mirror, candidate)
            if sha:
                return sha, None
        # A merge commit's second parent is the tip of the merged branch.
        merge = resolve_commit(self.mirror,
                               (pr.get("merge_commit") or {}).get("hash"))
        if merge:
            parent = resolve_commit(self.mirror, f"{merge}^2")
            if parent:
                return parent, None
        return base_sha, "head commit not found; imported with an empty diff"

    def add_close_events(self, pr: dict, pr_url: str, closed_at: str | None,
                         merge_sha: str | None) -> None:
        if not closed_at:
            return
        actor = self.user_url(pr.get("closed_by") or pr.get("author"))
        events = ["merged", "closed"] if pr["state"] == "MERGED" else ["closed"]
        for name in events:
            self.event_id += 1
            event = {
                "type": "issue_event",
                "url": f"{pr_url}#event-{self.event_id}",
                "pull_request": pr_url,
                "actor": actor,
                "event": name,
                "created_at": closed_at,
            }
            if name == "merged" and merge_sha:
                event["commit_id"] = merge_sha
            self.records["issue_events"].append(event)

    def add_approvals(self, record: dict, pr_url: str, head_sha: str) -> None:
        """Turn Bitbucket approvals and change requests into reviews."""
        seen: dict = {}
        for item in record.get("activity", []):
            for kind, state in (("approval", REVIEW_APPROVED),
                                ("changes_requested", REVIEW_CHANGES_REQUESTED)):
                entry = item.get(kind)
                if entry and entry.get("user"):
                    who = entry["user"].get("account_id") or entry["user"].get("uuid")
                    seen[who] = (entry["user"], state, entry.get("date"))
        # Participants carry the final state; fill in anything activity missed.
        for part in record["pullrequest"].get("participants", []):
            pstate = part.get("state")
            if pstate not in ("approved", "changes_requested") and not part.get("approved"):
                continue
            user = part.get("user") or {}
            who = user.get("account_id") or user.get("uuid")
            if who not in seen:
                state = (REVIEW_CHANGES_REQUESTED if pstate == "changes_requested"
                         else REVIEW_APPROVED)
                seen[who] = (user, state, part.get("participated_on"))
        when_default = record["pullrequest"].get("updated_on")
        for n, (user, state, when) in enumerate(seen.values(), 1):
            review_id = APPROVAL_REVIEW_ID_BASE + record["pullrequest"]["id"] * 1000 + n
            created = ts(when or when_default)
            self.records["pull_request_reviews"].append({
                "type": "pull_request_review",
                "url": f"{pr_url}/files#pullrequestreview-{review_id}",
                "pull_request": pr_url,
                "user": self.user_url(user),
                "body": "",
                "head_sha": head_sha,
                "formatter": "markdown",
                "state": state,
                "reactions": [],
                "created_at": created,
                "submitted_at": created,
            })

    def add_comments(self, record: dict, pr_url: str, base_sha: str,
                     head_sha: str, diff_ok: bool) -> None:
        comments = [c for c in record.get("comments", [])
                    if not c.get("deleted")]
        by_id = {c["id"]: c for c in comments}
        comments.sort(key=lambda c: (c.get("created_on") or "", c["id"]))

        def root_of(c: dict) -> dict:
            seen = set()
            while c.get("parent") and c["parent"]["id"] in by_id \
                    and c["id"] not in seen:
                seen.add(c["id"])
                c = by_id[c["parent"]["id"]]
            return c

        # Place each inline thread root in the diff first.
        diff_base = self._merge_base(base_sha, head_sha)
        placed: dict[int, tuple] = {}
        for c in comments:
            if c.get("parent") or not c.get("inline") or not diff_ok:
                continue
            placed_at = self.place_inline(c, diff_base, head_sha)
            if placed_at:
                placed[c["id"]] = placed_at

        for c in comments:
            root = root_of(c)
            if root["id"] in placed:
                self.add_review_comment(c, root, placed[root["id"]], pr_url)
            else:
                self.add_issue_comment(c, root, by_id, pr_url)

    def _merge_base(self, base: str, head: str) -> str:
        res = git("merge-base", base, head, cwd=self.mirror, check=False)
        return res.stdout.strip() if res.returncode == 0 else base

    def place_inline(self, c: dict, diff_base: str, head_sha: str):
        inline = c["inline"]
        path = inline.get("path")
        new_line, old_line = inline.get("to"), inline.get("from")
        if not path or (new_line is None and old_line is None):
            return None  # file-level comment; kept as a regular comment
        # Newer API responses say which commits the comment was made on.
        src = resolve_commit(self.mirror, inline.get("src_rev")) or head_sha
        dest = resolve_commit(self.mirror, inline.get("dest_rev"))
        base = self._merge_base(dest, src) if dest else diff_base
        loc = locate_in_diff(self.mirror, base, src, path, new_line, old_line)
        if not loc and src != head_sha:
            src = head_sha
            loc = locate_in_diff(self.mirror, diff_base, src, path,
                                 new_line, old_line)
        if not loc:
            return None
        return (path, loc, src)

    def add_review_comment(self, c: dict, root: dict, placement: tuple,
                           pr_url: str) -> None:
        path, loc, commit = placement
        comment_url = f"{pr_url}/files#r{c['id']}"
        thread_url = f"{pr_url}/files#pullrequestreviewthread-{root['id']}"
        review_url = f"{pr_url}/files#pullrequestreview-{c['id']}"
        user = self.user_url(c.get("user"))
        created = ts(c.get("created_on"))

        # Every review comment belongs to a review; Bitbucket has no such
        # grouping, so each comment gets its own "commented" review.
        self.records["pull_request_reviews"].append({
            "type": "pull_request_review",
            "url": review_url,
            "pull_request": pr_url,
            "user": user,
            "body": "",
            "head_sha": commit,
            "formatter": "markdown",
            "state": REVIEW_COMMENTED,
            "reactions": [],
            "created_at": created,
            "submitted_at": created,
        })
        if c is root:
            self.records["pull_request_review_threads"].append({
                "type": "pull_request_review_thread",
                "url": thread_url,
                "pull_request": pr_url,
                "pull_request_review": review_url,
                "diff_hunk": loc.diff_hunk,
                "path": path,
                "position": loc.position,
                "original_position": loc.position,
                "commit_id": commit,
                "original_commit_id": commit,
                "start_position_offset": None,
                "blob_position": loc.line,
                "start_line": None,
                "line": loc.line,
                "start_side": None,
                "side": loc.side,
                "original_start_line": None,
                "original_line": loc.line,
                "created_at": created,
                "resolved_at": ts(((c.get("resolution") or {}).get("created_on"))),
                "resolver": (self.user_url(c["resolution"]["user"])
                             if (c.get("resolution") or {}).get("user") else None),
                "subject_type": "line",
                "outdated": bool(c["inline"].get("outdated")),
            })
        self.records["pull_request_review_comments"].append({
            "type": "pull_request_review_comment",
            "url": comment_url,
            "pull_request": pr_url,
            "pull_request_review": review_url,
            "pull_request_review_thread": thread_url,
            "user": user,
            "body": self.clean_text(c["content"].get("raw")),
            "formatter": "markdown",
            "diff_hunk": loc.diff_hunk,
            "path": path,
            "position": loc.position,
            "original_position": loc.position,
            "commit_id": commit,
            "original_commit_id": commit,
            "state": 1,
            # GitHub threads are flat: replies point at the thread's root.
            "in_reply_to": None if c is root else f"{pr_url}/files#r{root['id']}",
            "reactions": [],
            "created_at": created,
            "subject_type": "line",
        })

    def add_issue_comment(self, c: dict, root: dict, by_id: dict,
                          pr_url: str) -> None:
        body = self.clean_text(c["content"].get("raw"))
        prefix = []
        inline = root.get("inline") or {}
        if c is root and inline.get("path"):
            line = inline.get("to") or inline.get("from")
            where = f"`{inline['path']}`" + (f" line {line}" if line else "")
            prefix.append(f"_Comment on {where}:_")
        parent = by_id.get((c.get("parent") or {}).get("id"))
        if parent:
            # GitHub conversation comments are not threaded; quote the parent.
            who = (parent.get("user") or {}).get("display_name", "someone")
            quoted = self.clean_text(parent["content"].get("raw")).strip()
            first = quoted.splitlines()[0] if quoted else ""
            if len(first) > 200:
                first = first[:200] + "…"
            prefix.append(f"> **{who}** wrote: {first}")
        if prefix:
            body = "\n\n".join(prefix + [body])
        self.records["issue_comments"].append({
            "type": "issue_comment",
            "url": f"{pr_url}#issuecomment-{c['id']}",
            "pull_request": pr_url,
            "user": self.user_url(c.get("user")),
            "body": body,
            "formatter": "markdown",
            "reactions": [],
            "created_at": ts(c.get("created_on")),
        })

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        log(f"warning: {msg}")

    # ---- archive ---------------------------------------------------------

    def write_archive(self, repo_meta: dict) -> None:
        staging = self.paths.staging
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)

        git_dir = staging / "repositories" / self.ws / f"{self.slug}.git"
        self.prepare_git(git_dir)

        default_branch = ((repo_meta.get("mainbranch") or {}).get("name")
                          or "main")
        write_json(staging / "schema.json", {"version": "1.2.0"})
        org = {
            "type": "organization",
            "url": self.org_url,
            "login": self.ws,
            "name": self.ws,
            "description": "",
            "website": None,
            "location": None,
            "email": None,
            "members": [],
        }
        repo = {
            "type": "repository",
            "url": self.repo_url,
            "owner": self.org_url,
            "name": self.slug,
            "description": (repo_meta.get("description") or "")[:350],
            "website": repo_meta.get("website") or None,
            "private": bool(repo_meta.get("is_private", True)),
            "has_issues": True,
            "has_wiki": False,
            "has_downloads": True,
            "labels": [],
            "webhooks": [],
            "collaborators": [],
            "created_at": ts(repo_meta.get("created_on")),
            "git_url": f"tarball://root/repositories/{self.ws}/{self.slug}.git",
            "wiki_url": None,
            "default_branch": default_branch,
            "public_keys": [],
            "page": None,
            "repository_topics": [],
            "autolinks": [],
        }
        users = [{k: v for k, v in u.items() if not k.startswith("_")}
                 for u in self.users.values()]
        self.write_records(staging, "users", users)
        self.write_records(staging, "organizations", [org])
        self.write_records(staging, "repositories", [repo])
        for name, records in self.records.items():
            self.write_records(staging, name, records)

        self.write_tarball(staging, self.paths.archive)
        self.write_users_csv()
        self.write_report()

    @staticmethod
    def write_records(staging: Path, name: str, records: list) -> None:
        for i in range(0, max(len(records), 1), RECORDS_PER_FILE):
            chunk = records[i:i + RECORDS_PER_FILE]
            if not chunk:
                return
            n = i // RECORDS_PER_FILE + 1
            write_json(staging / f"{name}_{n:06d}.json", chunk)

    def prepare_git(self, git_dir: Path) -> None:
        log("copying git data into the archive")
        git_dir.parent.mkdir(parents=True, exist_ok=True)
        git("clone", "--quiet", "--mirror", "--no-local",
            str(self.mirror), str(git_dir))
        # Keep every PR head reachable so its commits survive the import.
        refs = "\n".join(f"update refs/pull/{n}/head {sha}"
                         for n, sha in sorted(self.pr_refs.items()))
        if refs:
            subprocess.run(["git", "update-ref", "--stdin"], input=refs + "\n",
                           cwd=git_dir, text=True, check=True)
        stale = git("for-each-ref", "--format=%(refname)", "refs/bb2gh",
                    cwd=git_dir).stdout.split()
        for ref in stale:
            git("update-ref", "-d", ref, cwd=git_dir)
        git("remote", "remove", "origin", cwd=git_dir, check=False)
        git("gc", "--quiet", "--prune=now", cwd=git_dir)
        info = git_dir / "info"
        info.mkdir(exist_ok=True)
        (info / "nwo").write_text(f"{self.ws}/{self.slug}\n")
        (info / "last-sync").write_text(
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "\n")

    @staticmethod
    def write_tarball(staging: Path, target: Path) -> None:
        """Write a gzip tarball without PAX headers, which GEI rejects.

        Entries use the plain USTAR format; only names too long for USTAR
        fall back to GNU long-name entries.
        """
        log(f"writing {target}")
        tmp = target.with_suffix(".tmp")
        with tarfile.open(tmp, "w:gz", format=tarfile.USTAR_FORMAT) as tar:
            for path in sorted(staging.rglob("*")):
                rel = path.relative_to(staging).as_posix()
                info = tar.gettarinfo(str(path), arcname=rel)
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                try:
                    info.tobuf(tarfile.USTAR_FORMAT, "utf-8", "surrogateescape")
                    tar.format = tarfile.USTAR_FORMAT
                except ValueError:
                    tar.format = tarfile.GNU_FORMAT
                if info.isreg():
                    with open(path, "rb") as fh:
                        tar.addfile(info, fh)
                else:
                    tar.addfile(info)
        tmp.replace(target)

    def write_users_csv(self) -> None:
        with self.paths.users_csv.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["mannequin-user", "display-name",
                        "bitbucket-nickname", "bitbucket-account-id"])
            for u in sorted(self.users.values(), key=lambda u: u["login"].lower()):
                bb = u["_bitbucket"]
                w.writerow([u["login"], u["name"], bb.get("nickname") or "",
                            bb.get("account_id") or ""])

    def write_report(self) -> None:
        counts = {k: len(v) for k, v in self.records.items()}
        lines = [f"Archive: {self.paths.archive}",
                 f"Users (future mannequins): {len(self.users)}"]
        lines += [f"{k}: {v}" for k, v in counts.items()]
        lines.append(f"Warnings: {len(self.warnings)}")
        lines += [f"  - {w}" for w in self.warnings]
        self.paths.report.write_text("\n".join(lines) + "\n")
        print("\n".join(lines))


def cmd_build(args: argparse.Namespace) -> None:
    paths = work_paths(args)
    if not (paths.cache / "repository.json").exists() or not paths.mirror.exists():
        die(f"no export found in {paths.root}; run the export step first")
    ArchiveBuilder(paths, args.workspace, args.repo).build()


# --------------------------------------------------------------------------
# Step 3: import with GitHub Enterprise Importer
# --------------------------------------------------------------------------


def cmd_import(args: argparse.Namespace) -> None:
    paths = work_paths(args)
    if not paths.archive.exists():
        die(f"{paths.archive} not found; run the build step first")
    if not (os.environ.get("GH_PAT") or args.github_pat):
        die("set GH_PAT (or pass --github-pat) to a token for the target org")
    if shutil.which("gh") is None:
        die("the GitHub CLI (gh) is not on PATH")
    check = subprocess.run(["gh", "gei", "--version"], capture_output=True)
    if check.returncode != 0:
        die("the gh-gei extension is missing; install `github/gh-gei`")

    archive = str(paths.archive)
    cmd = [
        "gh", "gei", "migrate-repo",
        "--github-source-org", args.workspace,
        "--source-repo", args.repo,
        "--github-target-org", args.github_org,
        "--target-repo", args.github_repo or args.repo,
        "--git-archive-path", archive,
        "--metadata-archive-path", archive,
        "--target-repo-visibility", args.visibility,
    ]
    if args.storage == "github":
        cmd.append("--use-github-storage")
    elif args.storage == "azure":
        pass  # gh gei reads AZURE_STORAGE_CONNECTION_STRING
    elif args.storage == "aws":
        if not args.aws_bucket_name:
            die("--aws-bucket-name is required with --storage aws")
        cmd += ["--aws-bucket-name", args.aws_bucket_name]
    if args.github_pat:
        cmd += ["--github-target-pat", args.github_pat]
    if args.target_api_url:
        cmd += ["--target-api-url", args.target_api_url]
    if args.queue_only:
        cmd.append("--queue-only")
    if args.verbose:
        cmd.append("--verbose")
    shown = [("***" if prev == "--github-target-pat" else part)
             for prev, part in zip([""] + cmd, cmd)]
    log("running: " + " ".join(shown))
    sys.exit(subprocess.run(cmd).returncode)


def cmd_migrate(args: argparse.Namespace) -> None:
    cmd_export(args)
    cmd_build(args)
    cmd_import(args)


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate a Bitbucket Cloud repository and its pull "
                    "requests to GitHub through GitHub Enterprise Importer.")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--workspace",
                       help="Bitbucket workspace ID (the first part of the repo "
                            "URL); read from --local-repo when left out")
        p.add_argument("--repo",
                       help="Bitbucket repository slug; read from --local-repo "
                            "when left out")
        p.add_argument("--local-repo", nargs="?", const=".", metavar="PATH",
                       help="use an existing clone as the git source instead of "
                            "cloning from scratch; PATH defaults to the current "
                            "directory")
        p.add_argument("--remote", metavar="NAME",
                       help="remote in --local-repo that points at Bitbucket "
                            "(default: the first bitbucket.org remote, "
                            "preferring origin)")
        p.add_argument("--work-dir", default="./work",
                       help="where mirrors, caches, and archives go (default: ./work)")

    def export_opts(p: argparse.ArgumentParser) -> None:
        p.add_argument("--refresh", action="store_true",
                       help="download closed pull requests again even if cached")
        p.add_argument("--no-git-fetch", action="store_true",
                       help="with --local-repo, use the clone's git data as-is "
                            "and skip fetching from Bitbucket")

    def import_opts(p: argparse.ArgumentParser) -> None:
        p.add_argument("--github-org", required=True, help="target GitHub organization")
        p.add_argument("--github-repo", help="target repo name (default: Bitbucket slug)")
        p.add_argument("--visibility", default="private",
                       choices=["private", "internal", "public"])
        p.add_argument("--storage", default="github",
                       choices=["github", "azure", "aws"],
                       help="where gh gei stages the archive (default: github)")
        p.add_argument("--aws-bucket-name")
        p.add_argument("--github-pat", help="target token; defaults to GH_PAT")
        p.add_argument("--target-api-url",
                       help="API URL when the target is not github.com (for example GHE.com)")
        p.add_argument("--queue-only", action="store_true",
                       help="start the migration without waiting for it")
        p.add_argument("--verbose", action="store_true")

    p = sub.add_parser("export", help="mirror git data and download pull requests")
    common(p)
    export_opts(p)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("build", help="build the GitHub migration archive")
    common(p)
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("import", help="import the archive with gh gei")
    common(p)
    import_opts(p)
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("migrate", help="export, build, and import in one go")
    common(p)
    export_opts(p)
    import_opts(p)
    p.set_defaults(func=cmd_migrate)

    args = parser.parse_args()
    args.local = None
    if args.local_repo is not None:
        args.local, ws, slug = open_local_repo(args.local_repo, args.remote)
        args.workspace = args.workspace or ws
        args.repo = args.repo or slug
    if getattr(args, "no_git_fetch", False) and not args.local:
        parser.error("--no-git-fetch needs --local-repo")
    if not args.workspace or not args.repo:
        parser.error("give --workspace and --repo, or --local-repo pointing "
                     "at a clone with a bitbucket.org remote")
    args.func(args)


if __name__ == "__main__":
    main()
