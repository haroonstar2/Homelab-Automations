"""GitHub REST API client: recent commits, open PRs, agent/* branches, PR diffs, draft PR creation."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


def parse_pr_reference(ref: str, default_owner: str = "") -> tuple[str, int] | None:
    """Parse 'https://github.com/owner/repo/pull/123' or 'owner/repo#123' or 'repo#123'."""
    ref = ref.strip()
    m = re.search(r"github\.com/([^/]+/[^/]+)/pull/(\d+)", ref)
    if m:
        return m.group(1), int(m.group(2))
    m = re.match(r"^([^/#]+/[^/#]+)#(\d+)$", ref)
    if m:
        return m.group(1), int(m.group(2))
    m = re.match(r"^([^/#]+)#(\d+)$", ref)
    if m and default_owner:
        return f"{default_owner}/{m.group(1)}", int(m.group(2))
    return None


def slugify_branch(task: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower()).strip("-")
    slug = slug[:40].strip("-") or "task"
    return f"agent/{slug}"


@dataclass(frozen=True)
class CommitSummary:
    repo: str
    sha: str
    message: str
    when: str
    url: str


@dataclass(frozen=True)
class PullRequestSummary:
    repo: str
    number: int
    title: str
    branch: str
    draft: bool
    url: str


class GitHubClient:
    def __init__(self, token: str) -> None:
        import httpx

        self.token = token.strip()
        self._username: str | None = None
        self._repos_cache: tuple[float, list[dict[str, Any]]] | None = None
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self._http = httpx.AsyncClient(base_url=GITHUB_API, headers=headers, timeout=30.0)

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    async def close(self) -> None:
        await self._http.aclose()

    async def username(self) -> str:
        if self._username:
            return self._username
        resp = await self._http.get("/user")
        resp.raise_for_status()
        self._username = resp.json()["login"]
        return self._username

    async def resolve_repo(self, repo: str) -> str:
        """Accept either 'owner/repo' or bare 'repo' (resolved with authenticated user)."""
        repo = repo.strip().removeprefix("https://github.com/").strip("/")
        if repo.endswith(".git"):
            repo = repo[:-4]
        if "/" in repo:
            return repo
        owner = await self.username()
        return f"{owner}/{repo}"

    async def list_recent_repos(self, limit: int = 8) -> list[dict[str, Any]]:
        resp = await self._http.get("/user/repos", params={"sort": "pushed", "per_page": limit, "affiliation": "owner,collaborator"})
        resp.raise_for_status()
        return resp.json()

    async def list_all_user_repos(self) -> list[dict[str, Any]]:
        """Return up to 100 user repos, cached for 5 minutes."""
        import time

        now = time.monotonic()
        if self._repos_cache and (now - self._repos_cache[0]) < 300:
            return self._repos_cache[1]
        resp = await self._http.get(
            "/user/repos",
            params={"sort": "pushed", "per_page": 100, "affiliation": "owner,collaborator"},
        )
        resp.raise_for_status()
        repos = resp.json()
        self._repos_cache = (now, repos)
        return repos

    async def find_repo_in_text(self, text: str, explicit_project: str = "") -> str | None:
        """Detect a GitHub repo ('owner/name') from an explicit project parameter, a github.com URL, or repo name in text."""
        if not self.enabled:
            return None

        # 1. Explicit github.com/owner/repo URL in text or explicit_project
        combined = f"{explicit_project} {text}"
        url_match = re.search(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", combined)
        if url_match:
            cleaned = url_match.group(1).removesuffix(".git").rstrip("/")
            return cleaned

        repos = await self.list_all_user_repos()

        # 2. Explicit project parameter
        if explicit_project.strip():
            target = explicit_project.strip().lower()
            for r in repos:
                if r.get("name", "").lower() == target or r.get("full_name", "").lower() == target:
                    return r["full_name"]
            return await self.resolve_repo(explicit_project.strip())

        # 3. Scan text for any of the user's GitHub repository names (longest match first)
        lowered = text.lower()
        for r in sorted(repos, key=lambda item: len(item.get("name", "")), reverse=True):
            name = r.get("name", "")
            full_name = r.get("full_name", "")
            if len(name) < 3:
                continue
            pattern = r"(?<![A-Za-z0-9_])" + re.escape(name.lower()) + r"(?![A-Za-z0-9_])"
            if re.search(pattern, lowered):
                return full_name
        return None

    async def recent_activity(self, hours: int = 24, repo_filter: str | None = None) -> tuple[list[CommitSummary], list[PullRequestSummary], list[str]]:
        """Return (recent_commits, open_prs, agent_branches) across recently pushed repos."""
        if not self.enabled:
            return [], [], []
        since_dt = datetime.now(timezone.utc) - timedelta(hours=hours)
        since_iso = since_dt.isoformat()

        if repo_filter:
            full = await self.resolve_repo(repo_filter)
            resp = await self._http.get(f"/repos/{full}")
            repos = [resp.json()] if resp.status_code == 200 else []
        else:
            repos = await self.list_recent_repos(limit=6)

        commits: list[CommitSummary] = []
        prs: list[PullRequestSummary] = []
        agent_branches: list[str] = []

        for r in repos:
            full_name = r.get("full_name", "")
            short_name = r.get("name", full_name)
            if not full_name:
                continue

            # Commits since threshold
            c_resp = await self._http.get(f"/repos/{full_name}/commits", params={"since": since_iso, "per_page": 10})
            if c_resp.status_code == 200:
                for c in c_resp.json():
                    msg = ((c.get("commit") or {}).get("message") or "").splitlines()[0]
                    date_str = ((c.get("commit") or {}).get("author") or {}).get("date", "")
                    commits.append(
                        CommitSummary(
                            repo=short_name,
                            sha=(c.get("sha") or "")[:7],
                            message=msg[:100],
                            when=date_str,
                            url=c.get("html_url", ""),
                        )
                    )

            # Open PRs
            p_resp = await self._http.get(f"/repos/{full_name}/pulls", params={"state": "open", "per_page": 10})
            if p_resp.status_code == 200:
                for p in p_resp.json():
                    prs.append(
                        PullRequestSummary(
                            repo=short_name,
                            number=p["number"],
                            title=p.get("title", "")[:90],
                            branch=(p.get("head") or {}).get("ref", ""),
                            draft=bool(p.get("draft")),
                            url=p.get("html_url", ""),
                        )
                    )

            # Active agent/* branches
            b_resp = await self._http.get(f"/repos/{full_name}/branches", params={"per_page": 30})
            if b_resp.status_code == 200:
                for b in b_resp.json():
                    bname = b.get("name", "")
                    if bname.startswith("agent/"):
                        agent_branches.append(f"{short_name}:{bname}")

        return commits, prs, agent_branches

    async def get_pr_diff(self, repo_full: str, pr_number: int) -> tuple[dict[str, Any], str]:
        """Return (pr_metadata_json, unified_diff_text)."""
        meta_resp = await self._http.get(f"/repos/{repo_full}/pulls/{pr_number}")
        meta_resp.raise_for_status()
        diff_resp = await self._http.get(
            f"/repos/{repo_full}/pulls/{pr_number}",
            headers={"Accept": "application/vnd.github.v3.diff"},
        )
        diff_resp.raise_for_status()
        return meta_resp.json(), diff_resp.text

    async def create_draft_pr(
        self,
        repo_full: str,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> str:
        """Create a draft PR on GitHub (or return existing PR URL if already open)."""
        resp = await self._http.post(
            f"/repos/{repo_full}/pulls",
            json={
                "title": title[:240],
                "head": head_branch,
                "base": base_branch,
                "body": body[:60000],
                "draft": True,
            },
        )
        if resp.status_code in (200, 201):
            return resp.json().get("html_url", "")
        # Check if PR already exists for this branch
        owner = repo_full.split("/")[0]
        existing = await self._http.get(
            f"/repos/{repo_full}/pulls",
            params={"head": f"{owner}:{head_branch}", "state": "open"},
        )
        if existing.status_code == 200 and existing.json():
            return existing.json()[0].get("html_url", "")
        raise RuntimeError(f"GitHub PR creation failed ({resp.status_code}): {resp.text[:300]}")

    async def get_default_branch(self, repo_full: str) -> str:
        resp = await self._http.get(f"/repos/{repo_full}")
        resp.raise_for_status()
        return resp.json().get("default_branch", "main")
